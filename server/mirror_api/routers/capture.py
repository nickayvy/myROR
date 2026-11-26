"""Mirror capture flow (capture/machine.py).

POST /api/capture                 start -> {capture_id, state}; 409 if one is running
GET  /api/capture/current         {capture_id, state, message, seconds_left, review_url}
                                  or {"state": "idle"}
POST /api/capture/{id}/keep       review only -> {capture_id, state, photo_id}; the photo
                                  goes to the check-in pipeline (watch checkin.ready)
POST /api/capture/{id}/retake     review only -> {capture_id, state}
POST /api/capture/{id}/cancel     any state -> {capture_id, state}
404 for an unknown or finished capture id, 409 for a command the state does not allow,
503 when the photo could not be saved (try keep again).

WebSocket: `capture.state` (same shape as /current) on every transition and countdown
tick; `cover` {open} on every cover sensor change.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from capture import CaptureError, get_machine
from mirror_api.deps import ServicesDep

router = APIRouter(prefix="/api/capture", tags=["capture"])


def _http(e: CaptureError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.post("")
async def start(services: ServicesDep) -> dict[str, Any]:
    try:
        return await get_machine(services).start()
    except CaptureError as e:
        raise _http(e) from None


@router.get("/current")
async def current(services: ServicesDep) -> dict[str, Any]:
    return get_machine(services).current()


@router.post("/{capture_id}/keep")
async def keep(capture_id: str, services: ServicesDep) -> dict[str, Any]:
    m = get_machine(services)
    try:
        photo_id = await m.keep(capture_id)
    except CaptureError as e:
        raise _http(e) from None
    return {"capture_id": capture_id, "state": m.current()["state"],
            "photo_id": str(photo_id) if photo_id is not None else None}


@router.post("/{capture_id}/retake")
async def retake(capture_id: str, services: ServicesDep) -> dict[str, Any]:
    try:
        return await get_machine(services).retake(capture_id)
    except CaptureError as e:
        raise _http(e) from None


@router.post("/{capture_id}/cancel")
async def cancel(capture_id: str, services: ServicesDep) -> dict[str, Any]:
    try:
        return await get_machine(services).cancel(capture_id)
    except CaptureError as e:
        raise _http(e) from None
