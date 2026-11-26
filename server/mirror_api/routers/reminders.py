"""POST /api/reminders (the iPhone Shortcut's push) and GET /api/reminders.

The payload contract and the Shortcut build steps are in server/integrations/README.md.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel

from integrations import today
from integrations.reminders import PayloadError, load_all, normalise, replace_lists
from mirror_api.contracts import Reminder
from mirror_api.deps import DbDep, ServicesDep

router = APIRouter(prefix="/api/reminders", tags=["reminders"])
DB_TIMEOUT_S = 3.0


class PushResult(BaseModel):
    lists: list[str]
    stored: int
    open: int
    due_unparsed: int
    synced_at: datetime


class RemindersResponse(BaseModel):
    reminders: list[Reminder]
    lists: dict[str, str]  # list name -> last push (ISO 8601)
    cached: bool = False  # true when the database was unreachable


@router.post("", response_model=PushResult)
async def push(services: ServicesDep, db: DbDep, body: Annotated[Any, Body()]) -> PushResult:
    try:
        lists, rows, unparsed = normalise(body, services.settings.tz)
    except PayloadError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    synced_at = await replace_lists(db, lists, rows)
    reminders, _ = await load_all(db)
    await today.reminders_changed(services, reminders, synced_at)
    return PushResult(lists=lists, stored=len(rows),
                      open=sum(1 for r in rows if not r.completed),
                      due_unparsed=unparsed, synced_at=synced_at)


@router.get("", response_model=RemindersResponse)
async def read(services: ServicesDep) -> RemindersResponse:
    try:
        reminders, synced = await asyncio.wait_for(load_all(services.db), DB_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - database down or not configured: serve the cache
        state = today.get_state(services)
        return RemindersResponse(reminders=state.reminders, lists={}, cached=True)
    return RemindersResponse(reminders=reminders, lists=synced)
