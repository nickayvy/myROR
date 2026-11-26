"""Outfits: today's suggestions, reroll, feedback, worn, shown.

    GET  /api/outfits?date=YYYY-MM-DD    current suggestions (default: today)
    POST /api/outfits/reroll             {"date"?} -> a different set
    POST /api/outfits/refresh            {"date"?} -> rebuild only if the context changed
    POST /api/outfits/{id}/feedback      {"value": 1 | -1, "note"?}
    POST /api/outfits/{id}/worn          {"date"?} -> wear_log for every item, idempotent
    POST /api/outfits/{id}/shown         the mirror or phone displayed it
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from mirror_api.deps import ServicesDep
from recommender import service, store

router = APIRouter(prefix="/api/outfits", tags=["outfits"])


class DateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: Date | None = None


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal[1, -1, "up", "down"]
    note: str | None = Field(default=None, max_length=500)


async def _items(services, ids: set[UUID]) -> dict[UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = await services.db.fetch(
        "SELECT id, name, category, subcategory, primary_color, state, cutout_key FROM items"
        " WHERE id = ANY(%s)", (list(ids),))
    return {r["id"]: r for r in rows}


async def _presign(services, key: str | None) -> str | None:
    if not key or services.storage is None:
        return None
    try:
        return await services.storage.presign_get(key)
    except Exception:  # noqa: BLE001 - a missing link is not worth a 500
        return None


async def view(services, outfits: list[store.StoredOutfit]) -> list[dict[str, Any]]:
    details = await _items(services, {i.item_id for o in outfits for i in o.items})
    out = []
    for o in outfits:
        items = []
        for i in o.items:
            d = details.get(i.item_id, {})
            items.append({
                "item_id": str(i.item_id), "slot": i.slot, "name": d.get("name"),
                "category": d.get("category"), "subcategory": d.get("subcategory"),
                "color": d.get("primary_color"), "state": d.get("state"),
                "cutout_url": await _presign(services, d.get("cutout_key")),
            })
        out.append({
            "id": str(o.id), "for_date": o.for_date.isoformat(), "rank": o.rank,
            "status": o.status, "source": o.source, "reasoning": o.reasoning, "items": items,
            "collage_key": o.collage_key, "collage_url": await _presign(services, o.collage_key),
        })
    return out


def _payload(day: Date, outfits_view, message, notes, **extra) -> dict[str, Any]:
    return {"date": day.isoformat(), "outfits": outfits_view, "message": message,
            "notes": notes, **extra}


@router.get("")
async def list_outfits(services: ServicesDep, date: Date | None = None) -> dict[str, Any]:
    day = date or service.today_for(services.settings)
    current = await store.current_outfits(services.db, day)
    st = service.last_status(services, day) or {}
    notes = current[0].context.get("notes", []) if current else st.get("notes", [])
    message = None if current else (st.get("message") or "No outfits yet for this day.")
    return _payload(day, await view(services, current), message, notes)


@router.post("/reroll")
async def reroll(services: ServicesDep, body: DateBody | None = None) -> dict[str, Any]:
    day = (body.date if body else None) or service.today_for(services.settings)
    res = await service.reroll(services, day)
    return _payload(day, await view(services, res.outfits), res.message, res.notes,
                    source=res.source, fallback_reason=res.fallback_reason)


@router.post("/refresh")
async def refresh(services: ServicesDep, body: DateBody | None = None) -> dict[str, Any]:
    day = (body.date if body else None) or service.today_for(services.settings)
    rebuilt = await service.ensure_fresh(services, day)
    current = await store.current_outfits(services.db, day)
    st = service.last_status(services, day) or {}
    return _payload(day, await view(services, current),
                    None if current else st.get("message"), st.get("notes", []),
                    rebuilt=rebuilt)


async def _outfit(services, outfit_id: UUID) -> store.StoredOutfit:
    o = await store.get_outfit(services.db, outfit_id)
    if o is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "outfit not found")
    return o


@router.post("/{outfit_id}/feedback", status_code=status.HTTP_201_CREATED)
async def feedback(outfit_id: UUID, body: FeedbackBody, services: ServicesDep) -> dict[str, Any]:
    await _outfit(services, outfit_id)
    value = {"up": 1, "down": -1}.get(body.value, body.value)  # type: ignore[arg-type]
    note = " ".join(body.note.split()) if body.note else None
    fid = await store.add_feedback(services.db, outfit_id, int(value), note or None)
    await services.hub.broadcast("outfits.feedback", {"outfit_id": str(outfit_id),
                                                      "value": value})
    return {"id": str(fid), "outfit_id": str(outfit_id), "value": value}


@router.post("/{outfit_id}/worn")
async def worn(outfit_id: UUID, services: ServicesDep,
               body: DateBody | None = None) -> dict[str, Any]:
    o = await _outfit(services, outfit_id)
    day = (body.date if body else None) or o.for_date
    ids = [i.item_id for i in o.items]
    new = await store.log_wear(services.db, ids, day, source="outfit", outfit_id=outfit_id)
    await services.hub.broadcast("outfits.worn", {"outfit_id": str(outfit_id),
                                                  "date": day.isoformat()})
    return {"outfit_id": str(outfit_id), "worn_on": day.isoformat(),
            "logged": [str(i) for i in new],
            "already_logged": [str(i) for i in ids if i not in set(new)]}


@router.post("/{outfit_id}/shown", status_code=status.HTTP_204_NO_CONTENT)
async def shown(outfit_id: UUID, services: ServicesDep) -> Response:
    await _outfit(services, outfit_id)
    await store.mark_shown(services.db, [outfit_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
