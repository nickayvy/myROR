"""Seam for the integrations side (/api/today): read today's stored outfits.

    from recommender.today import get_outfits
    outfits = await get_outfits(services, date)   # list[OutfitSuggestion], rank order

Reads stored rows only (one query); never calls the LLM or builds anything. An empty
list means nothing is stored yet or the closet cannot form an outfit.

`ensure_fresh(services, for_date)` (re-exported from recommender.service) is for the
presence sensor: it rebuilds only when the context fingerprint changed.
"""

from __future__ import annotations

from datetime import date

from mirror_api.contracts import OutfitSuggestion
from recommender import store
from recommender.service import ensure_fresh

__all__ = ["ensure_fresh", "get_outfits"]


async def get_outfits(services, for_date: date) -> list[OutfitSuggestion]:
    return [o.suggestion() for o in await store.current_outfits(services.db, for_date)]
