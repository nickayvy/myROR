"""Check-ins: "did you wear these?"

POST /api/checkins/upload             phone full-body/waist-up photo (multipart "file");
                                      201 new, 200 same bytes again. 503 when FACE_BLUR is on
                                      and no blurrer is available (nothing stored or sent).
GET  /api/checkins/{photo_id}         status and one proposal per visible garment
POST /api/checkins/{photo_id}/confirm {"item_ids": [...], "worn_on"?: "YYYY-MM-DD"}
                                      writes wear_log (source 'checkin', idempotent per item
                                      and day) and moves items.last_worn_on forward
POST /api/checkins/{photo_id}/garments/{detection_id}/inbox
                                      a garment that matched nothing goes to the review
                                      inbox as a candidate new item -> {"inbox_id", "created"}
                                      (201 new, 200 already sent; 409 if it matched an item)

The capture flow calls pipeline.ingest.checkin_from_bytes(services, jpeg) directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from mirror_api.deps import DbDep, ServicesDep, StorageDep
from pipeline.closet import (
    INBOX_CANDIDATE,
    ITEM_COLUMNS,
    InboxError,
    checkin_detection_to_inbox,
    item_out,
    link,
)
from pipeline.uploads import handle_upload, photo_status
from recommender import store

router = APIRouter(prefix="/api/checkins", tags=["checkins"])
CHECKIN_SOURCES = ("phone_fullbody", "mirror_checkin")


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[UUID] = Field(default_factory=list, max_length=20)
    worn_on: date | None = None


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(services: ServicesDep, response: Response,
                 file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    return await handle_upload(services, file, "phone_fullbody", response)


async def _photo(db, photo_id: UUID) -> dict[str, Any]:
    row = await db.fetchrow("SELECT * FROM photos WHERE id = %s AND source = ANY(%s)",
                            (photo_id, list(CHECKIN_SOURCES)))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such check-in")
    return row


@router.get("/{photo_id}")
async def get_checkin(photo_id: UUID, db: DbDep, storage: StorageDep) -> dict[str, Any]:
    photo = await _photo(db, photo_id)
    dets = await db.fetch("SELECT * FROM detections WHERE photo_id = %s AND stage = 'ready'"
                          f" AND NOT (notes ? '{INBOX_CANDIDATE}')"
                          " ORDER BY created_at, id", (photo_id,))
    ids = [d["matched_item_id"] for d in dets if d["matched_item_id"]]
    items = {r["id"]: r for r in await db.fetch(
        f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ANY(%s)", (ids,))} if ids else {}
    garments = []
    for d in dets:
        notes = d["notes"] or {}
        m = notes.get("match") or {}
        match = None
        if d["matched_item_id"] in items:
            match = {"item": await item_out(items[d["matched_item_id"]], storage),
                     "score": d["match_score"], "kind": m.get("kind"),
                     "method": m.get("method"), "candidates": m.get("candidates", [])}
        garments.append({
            "detection_id": str(d["id"]),
            "label": notes.get("label"),
            "box": d["box"],
            "tags": d["raw_tags"],
            "status": d["status"],
            "segmentation": (notes.get("segment") or {}).get("method"),
            "cutout_url": await link(storage, d["cutout_key"]),
            "match": match,
            "inbox_id": notes.get("inbox_id"),  # set once sent to the review inbox
        })
    return {**photo_status(photo), "photo_url": await link(storage, photo["storage_key"]),
            "garments": garments}


@router.post("/{photo_id}/confirm")
async def confirm(photo_id: UUID, body: ConfirmBody, services: ServicesDep,
                  db: DbDep) -> dict[str, Any]:
    photo = await _photo(db, photo_id)
    if photo["pipeline_status"] not in ("done", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "check-in is still processing")
    ids = list(dict.fromkeys(body.item_ids))
    if ids:
        found = {r["id"] for r in await db.fetch("SELECT id FROM items WHERE id = ANY(%s)",
                                                 (ids,))}
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"missing_item_ids": missing})
    tz = services.settings.tz
    worn_on = body.worn_on or (photo["taken_at"] or photo["uploaded_at"]).astimezone(tz).date()
    now = datetime.now(UTC)
    async with db.transaction() as conn:
        new = set(await store.log_wear(db, ids, worn_on, source="checkin", photo_id=photo_id,
                                       conn=conn))
        logged = [str(i) for i in ids if i in new]
        already = [str(i) for i in ids if i not in new]
        await conn.execute(
            "UPDATE detections SET status = CASE WHEN matched_item_id = ANY(%s::uuid[])"
            " THEN 'confirmed' ELSE 'rejected' END, resolved_at = coalesce(resolved_at, %s)"
            " WHERE photo_id = %s AND status = 'pending'"
            f" AND NOT (notes ? '{INBOX_CANDIDATE}')", (ids, now, photo_id))
    await services.hub.broadcast("wear.logged", {"item_ids": ids, "worn_on": worn_on.isoformat(),
                                                 "source": "checkin"})
    return {"worn_on": worn_on.isoformat(), "logged": logged, "already_logged": already}


@router.post("/{photo_id}/garments/{detection_id}/inbox", status_code=status.HTTP_201_CREATED)
async def garment_to_inbox(photo_id: UUID, detection_id: UUID, services: ServicesDep,
                           response: Response, db: DbDep) -> dict[str, Any]:
    await _photo(db, photo_id)
    try:
        out = await checkin_detection_to_inbox(services, photo_id, detection_id)
    except InboxError as e:
        raise HTTPException(e.status, str(e)) from None
    if not out["created"]:
        response.status_code = status.HTTP_200_OK
    return out
