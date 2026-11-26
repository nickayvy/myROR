"""GET /api/today (served from memory, never the database), POST /api/today/refresh,
GET /api/today/status. The integration timers start from integrations/lifecycle.py."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from integrations import today
from mirror_api.contracts import TodayPayload
from mirror_api.deps import ServicesDep

router = APIRouter(prefix="/api/today", tags=["today"])


@router.get("", response_model=TodayPayload)
async def get_today(services: ServicesDep) -> TodayPayload:
    return await today.with_links(services, today.current_payload(services))


@router.post("/refresh", response_model=TodayPayload)
async def refresh(services: ServicesDep, force: bool = False) -> TodayPayload:
    return await today.with_links(services, await today.refresh_if_stale(services, force=force))


@router.get("/status")
async def integration_status(services: ServicesDep) -> dict[str, Any]:
    """Freshness of each source; booleans, times and short error classes only."""
    return today.get_state(services).status()
