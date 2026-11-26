"""GET /api/calendar.ics: one merged, deduplicated feed for MagicMirror's calendar
module. Localhost only (the feed URLs and events stay on the Pi). Built from the cached
feeds; no network, no database."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Response

from integrations.calendars import to_ics
from integrations.today import UPCOMING_DAYS, get_state
from mirror_api.deps import ServicesDep

AUTH = "localhost"
router = APIRouter(prefix="/api", tags=["calendar"])
WINDOW_DAYS = 4 * UPCOMING_DAYS  # four weeks ahead, plus yesterday


@router.get("/calendar.ics")
async def merged_calendar(services: ServicesDep) -> Response:
    state = get_state(services)
    if not state.loaded:
        state.load()
    day = state.today()
    events = state.calendars.events_between(day - timedelta(days=1),
                                            day + timedelta(days=WINDOW_DAYS))
    return Response(
        to_ics(events, state.now()),
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
