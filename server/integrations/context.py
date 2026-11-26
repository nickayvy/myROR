"""The seam the recommender codes against:

    from integrations.context import get_day_context
    ctx = await get_day_context(services, for_date)   # DayContext, never raises

Fast from cache when the weather (< 1 h) and calendars (< 30 min) are fresh. Otherwise
one HTTP refresh, bounded to NETWORK_TIMEOUT_S (6 s); on failure the last good data is
used. Event titles never rated are sent to the tagging model in one call, awaited up
to TAG_WAIT_S (6 s); if that is impossible or slower, their formality stays None (the
result is cached for next time) and min_formality falls back to 1. Weather is None when
it is not configured or `for_date` is outside the cached forecast (today + 2 days).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from integrations.formality import day_formality
from integrations.today import get_state
from mirror_api.contracts import DayContext

if TYPE_CHECKING:
    from mirror_api.services import Services

log = logging.getLogger(__name__)

# The recommender gives this call 15 s in all (recommender/service.py); stay under it.
NETWORK_TIMEOUT_S = 6.0
TAG_WAIT_S = 6.0  # tagging keeps running in the background after this (never cancelled
#                   mid-call, so a billed response is always recorded and cached)
FRESH_WEATHER = timedelta(hours=1)
FRESH_CALENDARS = timedelta(minutes=30)


async def get_day_context(services: Services, for_date: date) -> DayContext:
    state = get_state(services)
    try:
        if not state.loaded:
            state.load()
        if not (state.weather_fresh(FRESH_WEATHER) and state.calendars_fresh(FRESH_CALENDARS)):
            await asyncio.wait_for(state.refresh_network(), NETWORK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - stale data beats no context
        log.info("day context: refresh skipped (%s)", type(e).__name__)
    try:
        task = state.spawn(state.tag_events(state.calendars.events_for_day(for_date)))
        await asyncio.wait({task}, timeout=TAG_WAIT_S)
    except Exception as e:  # noqa: BLE001
        log.info("day context: formality tagging skipped (%s)", type(e).__name__)
    try:
        events = state.events_for(for_date)
    except Exception as e:  # noqa: BLE001
        log.warning("day context: events unavailable (%s)", type(e).__name__)
        events = []
    try:
        weather = state.weather_for(for_date)
    except Exception as e:  # noqa: BLE001
        log.warning("day context: weather unavailable (%s)", type(e).__name__)
        weather = None
    return DayContext(date=for_date, weather=weather, events=events,
                      min_formality=day_formality(events))
