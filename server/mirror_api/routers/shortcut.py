"""Tokens for the iPhone Reminders Shortcut (a device of kind `shortcut`).

POST   /api/shortcut/tokens        a paired phone (or the mirror) mints one; shown once
GET    /api/shortcut/tokens        list them (no token material)
DELETE /api/shortcut/tokens/{id}   revoke one (DELETE /api/devices/{id} works too)

A shortcut token cannot mint or revoke tokens itself.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from mirror_api.auth import Device
from mirror_api.deps import AuthDep, DeviceDep, HubDep

router = APIRouter(prefix="/api/shortcut", tags=["shortcut"])


class MintRequest(BaseModel):
    name: str = Field(default="Reminders Shortcut", min_length=1, max_length=64)


class MintResponse(BaseModel):
    device_id: UUID
    name: str
    token: str


def _require_owner(who) -> None:
    if who.kind == "device" and who.device is not None and who.device.kind == "shortcut":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "a shortcut token cannot manage tokens")


@router.post("/tokens", response_model=MintResponse, status_code=status.HTTP_201_CREATED)
async def mint(who: DeviceDep, auth: AuthDep, body: MintRequest | None = None) -> MintResponse:
    _require_owner(who)
    device, token = await auth.issue_token((body or MintRequest()).name, "shortcut")
    return MintResponse(device_id=device.id, name=device.name, token=token)


@router.get("/tokens", response_model=list[Device])
async def list_tokens(who: DeviceDep, auth: AuthDep) -> list[Device]:
    _require_owner(who)
    return [d for d in await auth.list_devices() if d.kind == "shortcut"]


@router.delete("/tokens/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(device_id: UUID, who: DeviceDep, auth: AuthDep, hub: HubDep) -> Response:
    _require_owner(who)
    if not any(d.id == device_id for d in await auth.list_devices() if d.kind == "shortcut"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active shortcut token with that id")
    await auth.revoke(device_id)
    await hub.disconnect_device(str(device_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
