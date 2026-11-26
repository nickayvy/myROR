"""WS /ws. The mirror connects from localhost and is accepted at once. A phone sends
{"type": "auth", "token": "<device token>"} as its first message within 5 s (the
token stays out of URLs and access logs). The server replies {"type": "hello"}.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mirror_api.auth import AuthUnavailable
from mirror_api.deps import is_local
from mirror_api.ws import WsClient

AUTH = "none"
router = APIRouter(tags=["ws"])
AUTH_TIMEOUT_S = 5.0


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    services = ws.app.state.services
    hub, auth = services.hub, services.auth
    await ws.accept()
    if is_local(ws):
        client = WsClient(ws, "mirror")
    else:
        try:
            msg = await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_S)
            is_auth = isinstance(msg, dict) and msg.get("type") == "auth"
            token = msg.get("token") if is_auth else None
            device = await auth.authenticate(token or "")
        except (TimeoutError, WebSocketDisconnect, ValueError, AuthUnavailable):
            device = None
        if device is None or device.kind == "shortcut":
            with contextlib.suppress(Exception):
                await ws.close(code=4401)
            return
        client = WsClient(ws, "phone", device_id=str(device.id))
    hub.add(client)
    await hub.send(client, "hello", {"kind": client.kind})
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await hub.send(client, "pong")
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        hub.remove(client)
