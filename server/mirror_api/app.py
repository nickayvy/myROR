"""App factory. Run: `uvicorn --factory mirror_api.app:create_app`.

Extension points (add files, don't edit this one):
- Routers: any mirror_api/routers/<name>.py exposing `router` is included. Guarded by
  require_device unless the module sets AUTH = "localhost" or "none".
- Jobs: <pkg>/jobs.py for pkg in JOB_PACKAGES is imported at startup, so its
  @job and @daily decorators register.
- Startup/shutdown: <pkg>/lifecycle.py for pkg in FEATURE_PACKAGES may define
  `async def startup(services)` and `async def shutdown(services)`. startup runs only
  when start_workers=True (it is where timers and watchers start); shutdown always runs,
  in reverse order, and must be safe when startup did not run.
- Dev controls: mirror_api/devtools.py is included only when APP_ENV=dev and HARDWARE=mock.
- Phone app: when settings.phone_dist exists it is served at / (see mirror_api/spa.py).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from db import Database, DatabaseNotConfigured
from hardware import build_hardware
from mirror_api.auth import Auth
from mirror_api.config import Settings
from mirror_api.deps import require_device, require_localhost
from mirror_api.jobs import DailyScheduler, JobQueue
from mirror_api.services import Services
from mirror_api.spend import SpendLedger
from mirror_api.ws import Hub
from providers.llm import build_llm
from storage import build_storage

log = logging.getLogger("mirror_api")

ROUTERS_PACKAGE = "mirror_api.routers"
JOB_PACKAGES = ("pipeline", "recommender", "integrations", "capture")
# Startup order; shutdown runs in reverse.
FEATURE_PACKAGES = ("integrations", "recommender", "pipeline", "capture", "mirror_api.presence")
_GUARDS = {"device": [Depends(require_device)], "localhost": [Depends(require_localhost)],
           "none": []}


def import_job_modules(packages: tuple[str, ...] = JOB_PACKAGES) -> list[str]:
    """Import <pkg>.jobs where it exists. A missing module is fine; an import error
    inside an existing module is raised (a broken handler file must not be silent)."""
    loaded = []
    for pkg in packages:
        name = f"{pkg}.jobs"
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name in (name, pkg):
                continue
            raise
        loaded.append(name)
    return loaded


def discover_routers(package: str = ROUTERS_PACKAGE) -> list[ModuleType]:
    pkg = importlib.import_module(package)
    modules = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{package}.{info.name}")
        if getattr(mod, "router", None) is not None:
            modules.append(mod)
    return modules


def include_routers(app: FastAPI, package: str = ROUTERS_PACKAGE) -> list[str]:
    names = []
    for mod in discover_routers(package):
        auth = getattr(mod, "AUTH", "device")
        if auth not in _GUARDS:
            raise ValueError(f"{mod.__name__}.AUTH must be one of {sorted(_GUARDS)}")
        app.include_router(mod.router, dependencies=_GUARDS[auth])
        names.append(mod.__name__)
    return names


def feature_hooks(packages: tuple[str, ...] = FEATURE_PACKAGES) -> list[ModuleType]:
    """<pkg>.lifecycle modules (or the module itself for a dotted module name) that define
    startup and/or shutdown."""
    mods = []
    for pkg in packages:
        mod = None
        for name in (f"{pkg}.lifecycle", pkg):
            try:
                mod = importlib.import_module(name)
            except ModuleNotFoundError as e:
                if e.name in (name, pkg, f"{pkg}.lifecycle"):
                    continue
                raise
            if hasattr(mod, "startup") or hasattr(mod, "shutdown"):
                break
            mod = None
        if mod is not None:
            mods.append(mod)
    return mods


def build_services(settings: Settings, overrides: dict[str, Any]) -> Services:
    unknown = set(overrides) - set(Services.OVERRIDABLE)
    if unknown:
        raise ValueError(f"unknown overrides: {sorted(unknown)}")
    db = overrides.get("db") or Database(
        settings.database_url,
        max_size=settings.db_pool_max_size,
        max_idle=settings.db_pool_max_idle_s,
        connect_timeout_s=settings.db_connect_timeout_s,
        acquire_timeout_s=settings.db_acquire_timeout_s,
        reconnect_timeout_s=settings.db_reconnect_timeout_s,
    )
    spend = overrides.get("spend") or SpendLedger.from_settings(db, settings)
    services = Services(
        settings=settings,
        db=db,
        storage=overrides["storage"] if "storage" in overrides else build_storage(settings),
        spend=spend,
        llm=overrides.get("llm") or build_llm(settings, spend),
        hub=overrides.get("hub") or Hub(),
        hardware=overrides.get("hardware") or build_hardware(settings),
        auth=overrides.get("auth") or Auth.from_settings(db, settings),
    )
    for name in Services.PROVIDER_FIELDS:
        if name in overrides:
            setattr(services, name, overrides[name])
    services.jobs = JobQueue(db, lambda: services, concurrency=settings.job_concurrency)
    services.scheduler = DailyScheduler(lambda: services, settings, settings.tz)
    return services


def create_app(
    settings: Settings | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    start_workers: bool = True,
) -> FastAPI:
    """Build the app. `overrides` replaces services by name (see Services.OVERRIDABLE);
    overridden objects are opened but not closed by the app (the caller owns them).
    start_workers=False skips the job queue and daily scheduler (unit tests)."""
    settings = settings or Settings()
    overrides = dict(overrides or {})
    import_job_modules()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = build_services(settings, overrides)
        app.state.services = services
        await services.db.open()  # no network I/O with min_size=0
        unwire = _wire_hardware_events(services)
        hooks = feature_hooks()
        started: list[ModuleType] = []
        try:
            if start_workers:
                if services.db.configured:
                    await services.jobs.start()
                await services.scheduler.start()
                for mod in hooks:
                    if hasattr(mod, "startup"):
                        await mod.startup(services)
                    started.append(mod)
            log.info("mirror-api up: env=%s storage=%s llm=%s hardware=%s", settings.app_env,
                     settings.storage_backend, services.llm.name, services.hardware.kind)
            yield
        finally:
            for mod in reversed(hooks):
                if hasattr(mod, "shutdown"):
                    try:
                        await mod.shutdown(services)
                    except Exception:  # noqa: BLE001 - keep shutting the rest down
                        log.exception("shutdown hook %s failed", mod.__name__)
            unwire()
            await services.hub.close_all()
            await services.scheduler.stop()
            await services.jobs.stop()
            if hasattr(services.auth, "close"):
                await services.auth.close()
            if "hardware" not in overrides:
                await services.hardware.close()
            if "llm" not in overrides and hasattr(services.llm, "close"):
                await services.llm.close()
            if "storage" not in overrides and services.storage is not None:
                await services.storage.close()
            if "db" not in overrides:
                await services.db.close()

    app = FastAPI(title="MY-ror mirror API", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(psycopg.OperationalError, _db_unavailable)
    app.add_exception_handler(PoolTimeout, _db_unavailable)
    app.add_exception_handler(DatabaseNotConfigured, _db_unavailable)
    app.state.routers = include_routers(app)
    if settings.dev_controls:
        from mirror_api import devtools

        app.include_router(devtools.router)
        app.state.routers.append(devtools.__name__)
    install_access_log_redaction()
    from mirror_api import spa

    app.state.phone_app = spa.mount_phone_app(app, settings.phone_dist)
    return app


async def _db_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """The database is unreachable (Neon asleep or the internet down): 503 with a short
    reason instead of a 500 with a traceback. The phone can retry; nothing is half done
    because each request's writes are in one transaction or one statement."""
    log.warning("%s %s: database unavailable (%s)", request.method, request.url.path,
                type(exc).__name__)
    return JSONResponse({"detail": "database unavailable, try again shortly"},
                        status_code=503, headers={"Retry-After": "5"})


def _wire_hardware_events(services: Services):
    """Raw sensor edges to the hub. `cover` {open} is the current event; `camera.cover`
    {closed} and `presence.changed` {present} are kept for existing clients. The
    debounced `presence` {present} event comes from mirror_api.presence."""
    hub, hw = services.hub, services.hardware

    async def on_presence(present: bool) -> None:
        await hub.broadcast("presence.changed", {"present": present})

    async def on_cover(closed: bool) -> None:
        await hub.broadcast("cover", {"open": not closed})
        await hub.broadcast("camera.cover", {"closed": closed})

    unsubs = [hw.presence.subscribe(on_presence), hw.cover.subscribe(on_cover)]

    def unwire() -> None:
        for u in unsubs:
            u()

    return unwire


class _RedactQuery(logging.Filter):
    """Keep pairing codes and capture-image tokens out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if "?" in path and ("code=" in path or "t=" in path):
                record.args = (*args[:2], path.split("?", 1)[0] + "?<redacted>", *args[3:])
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactQuery) for f in logger.filters):
        logger.addFilter(_RedactQuery())
