"""GET /api/health: database, storage, spend mode, which providers are configured.

Booleans and short error classes only: no URLs, keys or hostnames. Each call queries
the database, so nothing should poll this endpoint (Neon must be allowed to sleep).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from mirror_api.deps import ServicesDep

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(services: ServicesDep) -> dict[str, Any]:
    s = services.settings
    out: dict[str, Any] = {
        "status": "ok",
        "app_env": s.app_env,
        "db": {"configured": services.db.configured, "ok": False},
        "storage": {
            "configured": services.storage is not None,
            "backend": s.storage_backend,
            "ok": False,
        },
        "spend": None,
        "providers": s.providers_configured(),
        "hardware": services.hardware.kind,
        "ws_clients": services.hub.counts,
    }
    if services.db.configured:
        try:
            await services.db.fetchval("SELECT 1")
            out["db"]["ok"] = True
            st = await services.spend.status()
            out["spend"] = {
                "ledger": st.ledger,
                "mode": st.mode,
                "percent": st.percent,
                "spent_usd": st.spent_usd,
                "cap_usd": st.cap_usd,
            }
        except Exception as e:  # noqa: BLE001 - report, don't fail
            out["db"]["error"] = type(e).__name__
    if services.storage is not None:
        try:
            ping = getattr(services.storage, "ping", None)
            if ping is not None:
                await ping()
            out["storage"]["ok"] = True
        except Exception as e:  # noqa: BLE001
            out["storage"]["error"] = type(e).__name__
    if not (out["db"]["ok"] and out["storage"]["ok"]):
        out["status"] = "degraded"
    return out
