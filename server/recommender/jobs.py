"""Recommender jobs: the morning precompute and the weekly style-profile update."""

from __future__ import annotations

from datetime import date

from mirror_api.jobs import JobContext, daily, job
from recommender import service, store, style

PRECOMPUTE = "recommender.precompute"
STYLE_WEEKLY = "recommender.style_weekly"
STYLE_WEEKLY_WEEKDAY = 6  # Sunday (date.weekday())


def _day(ctx: JobContext, payload: dict) -> date:
    raw = (payload or {}).get("date")
    return date.fromisoformat(raw) if raw else service.today_for(ctx.settings)


@job(PRECOMPUTE, max_attempts=3)
async def precompute_job(ctx: JobContext, payload: dict) -> None:
    await service.precompute(ctx.services, _day(ctx, payload), job_id=ctx.job_id)


@daily(setting="precompute_time", name="recommender.morning")
async def morning(ctx: JobContext) -> None:
    """Close out earlier days (shown, not worn -> skipped), then build today's outfits
    as a durable job so a crash mid-build is retried."""
    today = service.today_for(ctx.settings)
    await store.close_out_days(ctx.db, today)
    await ctx.jobs.enqueue(PRECOMPUTE, {"date": today.isoformat()})


@job(STYLE_WEEKLY, max_attempts=2)
async def style_weekly_job(ctx: JobContext, payload: dict) -> None:
    await style.weekly_update(ctx.services, _day(ctx, payload))


@daily("05:00", name="recommender.style_weekly")
async def weekly(ctx: JobContext) -> None:
    today = service.today_for(ctx.settings)
    if today.weekday() == STYLE_WEEKLY_WEEKDAY:
        await ctx.jobs.enqueue(STYLE_WEEKLY, {"date": today.isoformat()})
