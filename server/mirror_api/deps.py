"""FastAPI dependency providers. Use the Annotated aliases in route signatures:

    from mirror_api.deps import DbDep, StorageDep, LlmDep, SpendDep, JobsDep, DeviceDep

    @router.get("/things")
    async def things(db: DbDep, who: DeviceDep) -> list[dict]:
        return await db.fetch("SELECT ...")

Routers in mirror_api/routers already require a device token by default (see
app.py), so DeviceDep is only needed when the handler wants to know *who* called.
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status

from db import Database
from hardware import Hardware
from mirror_api.auth import Auth, AuthUnavailable, Principal
from mirror_api.config import Settings
from mirror_api.jobs import JobQueue
from mirror_api.services import Services
from mirror_api.spend import SpendLedger
from mirror_api.ws import Hub
from providers.llm import LLM
from storage import Storage


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_settings(request: Request) -> Settings:
    return request.app.state.services.settings


def get_db(request: Request) -> Database:
    db = request.app.state.services.db
    if not db.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not configured")
    return db


def get_storage(request: Request) -> Storage:
    storage = request.app.state.services.storage
    if storage is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage not configured")
    return storage


def get_llm(request: Request) -> LLM:
    return request.app.state.services.llm


def get_spend(request: Request) -> SpendLedger:
    return request.app.state.services.spend


def get_jobs(request: Request) -> JobQueue:
    return request.app.state.services.jobs


def get_hub(request: Request) -> Hub:
    return request.app.state.services.hub


def get_hardware(request: Request) -> Hardware:
    return request.app.state.services.hardware


def get_auth(request: Request) -> Auth:
    return request.app.state.services.auth


def is_local(conn: Request | WebSocket) -> bool:
    """True when the peer is this machine (the mirror). Uses the socket peer only;
    X-Forwarded-For is not trusted."""
    client = conn.client
    if client is None or not client.host:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return client.host == "localhost"


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


# What a device of kind `shortcut` (the iPhone Reminders Shortcut) may call. Everything
# else, the WebSocket included, answers 403 for its token.
SHORTCUT_ROUTES = frozenset({("POST", "/api/reminders")})


def shortcut_may_call(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    return (request.method.upper(), path) in SHORTCUT_ROUTES


async def require_localhost(request: Request) -> None:
    if not is_local(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only available on the mirror itself")


async def require_device(request: Request) -> Principal:
    """A paired, unrevoked device token, or a request from localhost (the mirror)."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = bearer_token(request)
    if token:
        try:
            device = await get_auth(request).authenticate(token)
        except AuthUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "cannot check the token right now (database unreachable)",
                                headers={"Retry-After": "30"}) from None
        if device is None:
            raise _unauthorized()
        if device.kind == "shortcut" and not shortcut_may_call(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "a shortcut token may only push reminders")
        principal = Principal("device", device)
    elif is_local(request):
        principal = Principal("mirror")
    else:
        raise _unauthorized()
    request.state.principal = principal
    return principal


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, "device token required",
        headers={"WWW-Authenticate": "Bearer"},
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
ServicesDep = Annotated[Services, Depends(get_services)]
DbDep = Annotated[Database, Depends(get_db)]
StorageDep = Annotated[Storage, Depends(get_storage)]
LlmDep = Annotated[LLM, Depends(get_llm)]
SpendDep = Annotated[SpendLedger, Depends(get_spend)]
JobsDep = Annotated[JobQueue, Depends(get_jobs)]
HubDep = Annotated[Hub, Depends(get_hub)]
HardwareDep = Annotated[Hardware, Depends(get_hardware)]
AuthDep = Annotated[Auth, Depends(get_auth)]
DeviceDep = Annotated[Principal, Depends(require_device)]
LocalOnly = Depends(require_localhost)
