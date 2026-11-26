"""Pairing. POST /api/pair/code (mirror only) and POST /api/pair (phone, no token)."""

from __future__ import annotations

import base64
import io
import socket
from typing import Literal
from uuid import UUID

import qrcode
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from mirror_api.auth import PairingError
from mirror_api.deps import AuthDep, HubDep, LocalOnly

AUTH = "none"
router = APIRouter(prefix="/api", tags=["pairing"])


class CodeRequest(BaseModel):
    # Base URL the phone should open, e.g. "http://192.168.1.20:8000". Defaults to
    # this machine's LAN address and the port this request came in on.
    base_url: str | None = Field(default=None, max_length=200)


class CodeResponse(BaseModel):
    code: str
    expires_in: int
    pair_url: str
    qr_png: str  # data:image/png;base64,...


class PairRequest(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    name: str = Field(default="iPhone", min_length=1, max_length=64)
    kind: Literal["phone", "shortcut"] = "phone"


class PairResponse(BaseModel):
    device_id: UUID
    token: str


def lan_ip() -> str:
    """This machine's address on the LAN. A UDP connect sends no packets."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def qr_data_url(text: str) -> str:
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.post("/pair/code", dependencies=[LocalOnly], response_model=CodeResponse)
async def new_code(
    request: Request, auth: AuthDep, hub: HubDep, body: CodeRequest | None = None
) -> CodeResponse:
    code, ttl = auth.codes.issue()
    base = (body.base_url if body and body.base_url else None) or (
        f"http://{lan_ip()}:{request.url.port or 80}"
    )
    # The phone app's pairing screen reads ?code=. The code is single use and lives
    # 5 minutes; the access log redacts query strings that carry it (app.py).
    pair_url = f"{base.rstrip('/')}/pair?code={code}"
    await hub.broadcast("pair.code", {"expires_in": int(ttl)}, audience="mirror")
    return CodeResponse(code=code, expires_in=int(ttl), pair_url=pair_url,
                        qr_png=qr_data_url(pair_url))


@router.post("/pair", response_model=PairResponse, status_code=status.HTTP_201_CREATED)
async def pair(body: PairRequest, auth: AuthDep, hub: HubDep) -> PairResponse:
    try:
        device, token = await auth.pair(body.code, body.name, body.kind)
    except PairingError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired code") from None
    await hub.broadcast("pair.done", {"device_id": str(device.id), "name": device.name})
    return PairResponse(device_id=device.id, token=token)
