"""Dev-only controls for driving the mock hardware (and a few internals) from outside the
process, for ops/dev/e2e.py. Included by create_app ONLY when APP_ENV=dev and
HARDWARE=mock; in any other configuration these paths do not exist.

POST /api/dev/pair-code                 a pairing code without being on localhost (no token)
GET  /api/dev/hardware                  mock hardware state
POST /api/dev/hardware/cover            {"open": bool}
POST /api/dev/hardware/presence         {"present": bool}
POST /api/dev/hardware/camera/frame     {"kind": "good|dark|half_covered|blurred|overexposed"}
                                        or a raw image/jpeg body (the next frames)
POST /api/dev/hardware/camera/fail      {"fail": bool}
POST /api/dev/daily/{name}              run a daily job now (e.g. recommender.morning)
POST /api/dev/jobs/wait-idle            wait until the job queue is idle (?timeout=s)
POST /api/dev/auth/expire-cache         the next token check re-reads the device list
                                        (as if the minute had passed)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from hardware.mock import MOCK_FRAME_KINDS, mock_frame
from mirror_api.deps import AuthDep, HubDep, ServicesDep, require_device
from mirror_api.jobs import DAILY, JobContext

router = APIRouter(prefix="/api/dev", tags=["dev"])
_guard = [Depends(require_device)]


class CoverBody(BaseModel):
    open: bool


class PresenceBody(BaseModel):
    present: bool


class FailBody(BaseModel):
    fail: bool


@router.post("/pair-code")
async def pair_code(request: Request, auth: AuthDep, hub: HubDep) -> dict[str, Any]:
    code, ttl = auth.codes.issue()
    await hub.broadcast("pair.code", {"expires_in": int(ttl)}, audience="mirror")
    base = str(request.base_url).rstrip("/")
    return {"code": code, "expires_in": int(ttl), "pair_url": f"{base}/pair?code={code}"}


@router.get("/hardware", dependencies=_guard)
async def hardware_state(services: ServicesDep) -> dict[str, Any]:
    hw = services.hardware
    return {
        "kind": hw.kind,
        "cover_open": not hw.cover.is_closed(),
        "present": hw.presence.is_present(),
        "camera": {"open": hw.camera.is_open, "captures": hw.camera.captures,
                   "frame_set": hw.camera.frame is not None,
                   "failing": hw.camera.fail_with is not None},
        "leds": {"level": hw.leds.level,
                 "history": [{"level": lv, "full_duty": fd} for lv, fd in hw.leds.history[-20:]]},
        "display": {"on": hw.display.is_on},
    }


@router.post("/hardware/cover", dependencies=_guard)
async def set_cover(body: CoverBody, services: ServicesDep) -> dict[str, Any]:
    services.hardware.cover.set_closed(not body.open)
    return {"cover_open": body.open}


@router.post("/hardware/presence", dependencies=_guard)
async def set_presence(body: PresenceBody, services: ServicesDep) -> dict[str, Any]:
    services.hardware.presence.set_present(body.present)
    return {"present": body.present}


@router.post("/hardware/camera/frame", dependencies=_guard)
async def set_frame(request: Request, services: ServicesDep) -> dict[str, Any]:
    cam = services.hardware.camera
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype in ("image/jpeg", "image/jpg", "application/octet-stream"):
        data = await request.body()
        if not data.startswith(b"\xff\xd8"):
            raise HTTPException(422, "body is not a JPEG")
        _BASE[id(cam)] = data  # the "good" frame that the other kinds are made from
        cam.frame = data
        return {"frame": "uploaded", "bytes": len(data)}
    body = await request.json()
    kind = (body or {}).get("kind", "good")
    if kind not in MOCK_FRAME_KINDS:
        raise HTTPException(422, f"kind must be one of {list(MOCK_FRAME_KINDS)}")
    cam.frame = mock_frame(kind, _BASE.get(id(cam)))
    return {"frame": kind}


_BASE: dict[int, bytes] = {}


@router.post("/hardware/camera/fail", dependencies=_guard)
async def set_fail(body: FailBody, services: ServicesDep) -> dict[str, Any]:
    services.hardware.camera.fail_with = RuntimeError("mock camera failure") if body.fail \
        else None
    return {"failing": body.fail}


@router.post("/daily/{name}", dependencies=_guard)
async def run_daily(name: str, services: ServicesDep) -> dict[str, Any]:
    spec = DAILY.get(name)
    if spec is None:
        raise HTTPException(404, {"message": "no such daily job", "known": sorted(DAILY)})
    await spec.fn(JobContext(services, f"daily:{name}"))
    return {"ran": name}


@router.post("/jobs/wait-idle", dependencies=_guard)
async def wait_idle(services: ServicesDep, timeout: float = 30.0) -> dict[str, Any]:
    try:
        await services.jobs.wait_idle(min(timeout, 120.0))
    except TimeoutError:
        raise HTTPException(504, "job queue still busy") from None
    return {"idle": True}


@router.post("/auth/expire-cache", dependencies=_guard)
async def expire_auth_cache(auth: AuthDep) -> dict[str, Any]:
    auth._loaded_at = None
    auth._failed_at = None
    return {"expired": True}
