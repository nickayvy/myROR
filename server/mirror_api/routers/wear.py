"""POST /api/items/{id}/worn: log one item as worn (idempotent per item per day).

Only this route lives here; the rest of /api/items belongs to the closet pipeline.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from mirror_api.deps import ServicesDep
from recommender import service, store

router = APIRouter(prefix="/api/items", tags=["wear"])


class WornBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: Date | None = None


@router.post("/{item_id}/worn")
async def item_worn(item_id: UUID, services: ServicesDep,
                    body: WornBody | None = None) -> dict[str, Any]:
    state = await services.db.fetchval("SELECT state FROM items WHERE id = %s", (item_id,))
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    if state == "retired":
        raise HTTPException(status.HTTP_409_CONFLICT, "item is retired")
    day = (body.date if body else None) or service.today_for(services.settings)
    new = await store.log_wear(services.db, [item_id], day, source="manual")
    await services.hub.broadcast("items.worn", {"item_id": str(item_id),
                                                "date": day.isoformat()})
    return {"item_id": str(item_id), "worn_on": day.isoformat(), "logged": bool(new)}
