"""Integration jobs. The weather/calendar timers are not jobs: they run in-process
from the /api/today router's lifespan (integrations.today.start) and never touch the
database. This module registers the one database-backed refresh as a durable job, so
other packages can request it without importing integrations.today:

    await jobs.enqueue("integrations.refresh", {"force": False})
"""

from __future__ import annotations

from mirror_api.jobs import JobContext, job


@job("integrations.refresh", max_attempts=1)
async def refresh(ctx: JobContext, payload: dict) -> None:
    from integrations.today import refresh_if_stale

    await refresh_if_stale(ctx.services, force=bool(payload.get("force")))
