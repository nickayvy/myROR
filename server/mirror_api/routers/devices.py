"""GET /api/devices, DELETE /api/devices/{id} (revoke)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from mirror_api.auth import Device
from mirror_api.deps import AuthDep, HubDep

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("", response_model=list[Device])
async def list_devices(auth: AuthDep, include_revoked: bool = False) -> list[Device]:
    return await auth.list_devices(include_revoked)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(device_id: UUID, auth: AuthDep, hub: HubDep) -> Response:
    if not await auth.revoke(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active device with that id")
    await hub.disconnect_device(str(device_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
