"""Review inbox: nothing enters the closet unconfirmed.

GET  /api/inbox                    pending onboarding detections that finished processing
POST /api/inbox/{id}/confirm       {"tags"?: {...}, "name"?: str} -> creates the item
POST /api/inbox/{id}/merge         {"item_id": ..., "use_cutout"?: bool} -> another photo of
                                   an item already owned
POST /api/inbox/{id}/reject
POST /api/inbox/confirm            bulk: {"entries": [{"id", "tags"?, "name"?}, ...],
                                   "include_matches": false}. Entries that look like an item
                                   already owned are skipped unless include_matches is true.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mirror_api.deps import DbDep, ServicesDep, StorageDep
from pipeline.closet import (
    INBOX_CANDIDATE,
    INBOX_SQL,
    ITEM_COLUMNS,
    InboxError,
    confirm_detection,
    item_out,
    link,
    merge_detection,
    reject_detection,
)

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: dict[str, Any] | None = None
    name: str | None = None


class BulkEntry(ConfirmBody):
    id: UUID


class BulkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[BulkEntry] = Field(min_length=1, max_length=200)
    include_matches: bool = False


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    use_cutout: bool = False


def _err(e: InboxError) -> HTTPException:
    return HTTPException(e.status, {"message": str(e), "errors": e.errors})


@router.get("")
async def inbox(db: DbDep, storage: StorageDep, limit: int = 200) -> dict[str, Any]:
    rows = await db.fetch(
        "SELECT d.id, d.photo_id, d.raw_tags, d.confidence, d.cutout_key, d.notes,"
        " d.matched_item_id, d.match_score, d.created_at, p.storage_key"
        " FROM detections d JOIN photos p ON p.id = d.photo_id"
        f" WHERE {INBOX_SQL} AND d.status = 'pending' AND d.stage = 'ready'"
        " ORDER BY d.created_at, d.id LIMIT %s", (min(max(limit, 1), 500),))
    item_ids = [r["matched_item_id"] for r in rows if r["matched_item_id"]]
    items = {r["id"]: r for r in await db.fetch(
        f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ANY(%s)", (item_ids,))} if item_ids \
        else {}
    out = []
    for r in rows:
        notes = r["notes"] or {}
        tagging = notes.get("tagging") or {}
        match = None
        if r["matched_item_id"] and r["matched_item_id"] in items:
            m = notes.get("match") or {}
            match = {"item": await item_out(items[r["matched_item_id"]], storage),
                     "score": r["match_score"], "kind": m.get("kind"),
                     "method": m.get("method"), "candidates": m.get("candidates", [])}
        out.append({
            "id": str(r["id"]),
            "photo_id": str(r["photo_id"]),
            "proposed_tags": r["raw_tags"],
            "confidence": r["confidence"],
            "tagging": tagging.get("status", "unknown"),
            "tag_errors": tagging.get("errors", []),
            "needs_review": bool(tagging.get("needs_review", True)),
            "problem": (notes.get("bg") or {}).get("error"),
            "cutout_url": await link(storage, r["cutout_key"]),
            "photo_url": await link(storage, r["storage_key"]),
            "match": match,
            "created_at": r["created_at"].isoformat(),
            # additive: "checkin" for a garment sent from a check-in, else "onboarding"
            "origin": "checkin" if INBOX_CANDIDATE in notes else "onboarding",
        })
    return {"count": len(out), "entries": out}


@router.post("/confirm")
async def bulk_confirm(body: BulkBody, services: ServicesDep, db: DbDep,
                       storage: StorageDep) -> dict[str, Any]:
    confirmed, skipped, failed = [], [], []
    for e in body.entries:
        if not body.include_matches:
            matched = await db.fetchval(
                "SELECT matched_item_id FROM detections WHERE id = %s", (e.id,))
            if matched is not None:
                skipped.append({"id": str(e.id), "reason": "looks like an item already owned",
                                "item_id": str(matched)})
                continue
        try:
            item = await confirm_detection(services, e.id, e.tags, e.name)
        except InboxError as err:
            failed.append({"id": str(e.id), "status": err.status, "message": str(err),
                           "errors": err.errors})
            continue
        confirmed.append({"id": str(e.id), "item": await item_out(item, storage)})
    return {"confirmed": confirmed, "skipped": skipped, "failed": failed}


@router.post("/{det_id}/confirm", status_code=201)
async def confirm(det_id: UUID, services: ServicesDep, storage: StorageDep,
                  body: ConfirmBody | None = None) -> dict[str, Any]:
    body = body or ConfirmBody()
    try:
        item = await confirm_detection(services, det_id, body.tags, body.name)
    except InboxError as e:
        raise _err(e) from None
    return {"item": await item_out(item, storage)}


@router.post("/{det_id}/merge")
async def merge(det_id: UUID, body: MergeBody, services: ServicesDep,
                storage: StorageDep) -> dict[str, Any]:
    try:
        item = await merge_detection(services, det_id, body.item_id, body.use_cutout)
    except InboxError as e:
        raise _err(e) from None
    return {"item": await item_out(item, storage)}


@router.post("/{det_id}/reject", status_code=204)
async def reject(det_id: UUID, services: ServicesDep) -> None:
    try:
        await reject_detection(services, det_id)
    except InboxError as e:
        raise _err(e) from None
