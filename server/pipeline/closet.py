"""Closet and inbox operations shared by the routers: creating items from confirmed
detections, merging, response shapes with short-lived presigned links."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from mirror_api.taxonomy import TAG_FIELDS, TaxonomyError, validate_tags
from pipeline.imaging import dominant_labs
from recommender.store import transfer_wears

log = logging.getLogger(__name__)

LINK_TTL_S = 900  # presigned image links in API responses
ITEM_COLUMNS = (
    "id, name, category, subcategory, primary_color, primary_lab, secondary_color,"
    " secondary_lab, pattern, material_guess, formality, season, fit, style_tags, cutout_key,"
    " source_photo_id, (embedding IS NOT NULL) AS has_embedding, embedding_model, state,"
    " last_worn_on, created_at, updated_at"
)


class InboxError(Exception):
    def __init__(self, status: int, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []


async def link(storage: Any, key: str | None) -> str | None:
    if not key:
        return None
    return await storage.presign_get(key, expires_in=LINK_TTL_S)


def tags_of(row: dict[str, Any]) -> dict[str, Any]:
    return {f: row.get(f) for f in TAG_FIELDS}


async def item_out(row: dict[str, Any], storage: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "tags": {f: (list(row[f]) if isinstance(row.get(f), list) else row.get(f))
                 for f in TAG_FIELDS},
        "primary_lab": row.get("primary_lab"),
        "secondary_lab": row.get("secondary_lab"),
        "state": row["state"],
        "last_worn_on": row["last_worn_on"].isoformat() if row.get("last_worn_on") else None,
        "cutout_url": await link(storage, row.get("cutout_key")),
        "source_photo_id": str(row["source_photo_id"]) if row.get("source_photo_id") else None,
        "has_embedding": bool(row.get("has_embedding")),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def default_name(tags: dict[str, Any]) -> str:
    parts = [tags.get("primary_color"), tags.get("subcategory") or tags.get("category")]
    return " ".join(p.replace("_", " ") for p in parts if p) or "item"


def merged_tags(raw: dict[str, Any], edits: dict[str, Any] | None) -> dict[str, Any]:
    """Proposed tags with the user's edits on top, validated (raises TaxonomyError)."""
    base = {k: v for k, v in (raw or {}).items() if k in (*TAG_FIELDS, "name")}
    base.update(edits or {})
    return validate_tags(base)


async def measured_labs(storage: Any, cutout_key: str | None, tags: dict[str, Any]):
    if not cutout_key:
        return None, None
    try:
        png = await storage.get(cutout_key)
    except KeyError:
        return None, None
    clusters = await asyncio.to_thread(dominant_labs, png)
    primary = clusters[0][0] if clusters else None
    secondary = clusters[1][0] if tags.get("secondary_color") and len(clusters) > 1 \
        and clusters[1][1] >= 0.15 else None
    return primary, secondary


async def confirm_detection(services: Any, det_id: UUID, edits: dict[str, Any] | None,
                            name: str | None) -> dict[str, Any]:
    """Create a closet item from a pending onboarding detection. Returns the item row."""
    db, storage = services.db, services.storage
    det = await _pending_onboarding(db, det_id)
    try:
        tags = merged_tags(det["raw_tags"], {**(edits or {}), **({"name": name} if name else {})})
    except TaxonomyError as e:
        raise InboxError(422, "tags are incomplete or invalid; send corrected tags",
                         e.errors) from e
    primary_lab, secondary_lab = await measured_labs(storage, det["cutout_key"], tags)
    now = datetime.now(UTC)
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE detections SET status = 'confirmed', resolved_at = %s"
            " WHERE id = %s AND status = 'pending' RETURNING id", (now, det_id))
        if await cur.fetchone() is None:
            raise InboxError(409, "detection is no longer pending")
        cur = await conn.execute(
            "INSERT INTO items (name, category, subcategory, primary_color, primary_lab,"
            " secondary_color, secondary_lab, pattern, material_guess, formality, season, fit,"
            " style_tags, cutout_key, source_photo_id, embedding, embedding_model)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {ITEM_COLUMNS}",
            (tags.get("name") or default_name(tags), tags["category"], tags.get("subcategory"),
             tags.get("primary_color"), primary_lab, tags.get("secondary_color"), secondary_lab,
             tags.get("pattern"), tags.get("material_guess"), tags.get("formality"),
             tags.get("season") or [], tags.get("fit"), tags.get("style_tags") or [],
             det["cutout_key"], det["photo_id"], det["embedding"], det["embedding_model"]))
        item = await cur.fetchone()
        await conn.execute(
            "UPDATE detections SET notes = notes || jsonb_build_object('item_id', %s::text)"
            " WHERE id = %s", (str(item["id"]), det_id))
    return item


async def merge_detection(services: Any, det_id: UUID, item_id: UUID,
                          use_cutout: bool = False) -> dict[str, Any]:
    """Resolve a pending detection as another photo of an existing item."""
    db = services.db
    det = await _pending_onboarding(db, det_id)
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT id FROM items WHERE id = %s FOR UPDATE", (item_id,))
        if await cur.fetchone() is None:
            raise InboxError(404, "no such item")
        cur = await conn.execute(
            "UPDATE detections SET status = 'merged', matched_item_id = %s, resolved_at = %s"
            " WHERE id = %s AND status = 'pending' RETURNING id",
            (item_id, datetime.now(UTC), det_id))
        if await cur.fetchone() is None:
            raise InboxError(409, "detection is no longer pending")
        if use_cutout and det["cutout_key"]:
            await conn.execute("UPDATE items SET cutout_key = %s, updated_at = now()"
                               " WHERE id = %s", (det["cutout_key"], item_id))
        if det["embedding"] is not None:
            # Fill a missing vector; never overwrite one from the same embedder.
            await conn.execute(
                "UPDATE items SET embedding = %s, embedding_model = %s, updated_at = now()"
                " WHERE id = %s AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s)",
                (det["embedding"], det["embedding_model"], item_id, det["embedding_model"]))
        cur = await conn.execute(f"SELECT {ITEM_COLUMNS} FROM items WHERE id = %s", (item_id,))
        return await cur.fetchone()


async def reject_detection(services: Any, det_id: UUID) -> None:
    db = services.db
    await _pending_onboarding(db, det_id)
    n = await db.execute("UPDATE detections SET status = 'rejected', resolved_at = %s"
                         " WHERE id = %s AND status = 'pending'", (datetime.now(UTC), det_id))
    if n == 0:
        raise InboxError(409, "detection is no longer pending")


INBOX_CANDIDATE = "inbox_candidate"  # detections.notes key: a check-in garment sent to review
# SQL: detections that belong in the review inbox (onboarding, or a sent check-in garment)
INBOX_SQL = f"(p.source = 'phone_onboarding' OR d.notes ? '{INBOX_CANDIDATE}')"


async def _pending_onboarding(db, det_id: UUID) -> dict[str, Any]:
    det = await db.fetchrow(
        "SELECT d.*, p.source FROM detections d JOIN photos p ON p.id = d.photo_id"
        " WHERE d.id = %s", (det_id,))
    if det is None or not (det["source"] == "phone_onboarding"
                           or INBOX_CANDIDATE in (det["notes"] or {})):
        raise InboxError(404, "no such inbox entry")
    if det["status"] != "pending":
        raise InboxError(409, f"already {det['status']}")
    if det["stage"] != "ready":
        raise InboxError(409, "still processing")
    return det


async def merge_items(services: Any, source_id: UUID, target_id: UUID) -> dict[str, Any]:
    """Fold item `source_id` into `target_id`: wear history, outfit memberships and
    detections move to the target (keeping the target's row where both have one), then the
    source item is deleted. Its cutout file stays with its photo (deleted with the photo)."""
    if source_id == target_id:
        raise InboxError(422, "cannot merge an item into itself")
    db = services.db
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT id, embedding IS NOT NULL AS has_emb FROM items"
                                 " WHERE id = ANY(%s) FOR UPDATE", ([source_id, target_id],))
        found = {r["id"]: r for r in await cur.fetchall()}
        if source_id not in found or target_id not in found:
            raise InboxError(404, "no such item")
        await transfer_wears(conn, source_id, target_id)
        await conn.execute(
            "INSERT INTO outfit_items (outfit_id, item_id, slot)"
            " SELECT outfit_id, %s, slot FROM outfit_items WHERE item_id = %s"
            " ON CONFLICT (outfit_id, item_id) DO NOTHING", (target_id, source_id))
        await conn.execute("UPDATE detections SET matched_item_id = %s WHERE matched_item_id = %s",
                           (target_id, source_id))
        await conn.execute(
            "UPDATE items t SET last_worn_on = greatest(t.last_worn_on, s.last_worn_on),"
            " cutout_key = coalesce(t.cutout_key, s.cutout_key),"
            " embedding = coalesce(t.embedding, s.embedding),"
            " embedding_model = CASE WHEN t.embedding IS NULL THEN s.embedding_model"
            "   ELSE t.embedding_model END,"
            " updated_at = now() FROM items s WHERE t.id = %s AND s.id = %s",
            (target_id, source_id))
        await conn.execute("DELETE FROM items WHERE id = %s", (source_id,))
        cur = await conn.execute(f"SELECT {ITEM_COLUMNS} FROM items WHERE id = %s",
                                 (target_id,))
        return await cur.fetchone()


def jsonb(v: Any) -> Jsonb:
    return Jsonb(v)


async def checkin_detection_to_inbox(services: Any, photo_id: UUID,
                                     det_id: UUID) -> dict[str, Any]:
    """Send a check-in garment that matched nothing in the closet to the review inbox as a
    candidate new item. The check-in detection itself is left alone (its confirm/reject
    belongs to the check-in); a copy with the same cutout, tags and vector becomes a
    pending inbox entry. Idempotent: a second call returns the same entry.
    Returns {"inbox_id", "created"}."""
    db = services.db
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT d.*, p.source FROM detections d JOIN photos p ON p.id = d.photo_id"
            " WHERE d.id = %s AND d.photo_id = %s FOR UPDATE OF d", (det_id, photo_id))
        det = await cur.fetchone()
        if det is None or det["source"] not in ("phone_fullbody", "mirror_checkin") \
                or INBOX_CANDIDATE in (det["notes"] or {}):
            raise InboxError(404, "no such check-in garment")
        notes = det["notes"] or {}
        if notes.get("inbox_id"):
            return {"inbox_id": notes["inbox_id"], "created": False}
        if det["stage"] != "ready":
            raise InboxError(409, "check-in is still processing")
        if det["matched_item_id"] is not None:
            raise InboxError(409, "this garment matched an item already in the closet")
        if not det["cutout_key"]:
            raise InboxError(409, "this garment has no cutout to review")
        tagging = {**(notes.get("tagging") or {}), "needs_review": True}
        new_notes = {INBOX_CANDIDATE: True, "from_detection": str(det_id),
                     "label": notes.get("label"), "tagging": tagging,
                     "segment": notes.get("segment")}
        cur = await conn.execute(
            "INSERT INTO detections (photo_id, box, cutout_key, raw_tags, embedding,"
            " embedding_model, confidence, notes, stage, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ready', 'pending') RETURNING id",
            (photo_id, Jsonb(det["box"]) if det["box"] is not None else None,
             det["cutout_key"], Jsonb(det["raw_tags"] or {}), det["embedding"],
             det["embedding_model"], det["confidence"], Jsonb(new_notes)))
        inbox_id = (await cur.fetchone())["id"]
        await conn.execute(
            "UPDATE detections SET notes = notes || jsonb_build_object('inbox_id', %s::text)"
            " WHERE id = %s", (str(inbox_id), det_id))
    await services.hub.broadcast("inbox.updated", {"photo_id": str(photo_id),
                                                   "detection_id": str(inbox_id)},
                                 audience="phone")
    return {"inbox_id": str(inbox_id), "created": True}
