"""Outfit persistence: current suggestions per day, feedback, wears, skips.

A day's *current* outfits are its rows with superseded_at IS NULL (migration 0004).
Rebuilds and rerolls supersede the current set rather than delete it, so feedback,
wears and skip logging keep pointing at what was actually shown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from db import Database
from mirror_api.contracts import OutfitItem, OutfitSuggestion
from recommender.outfit import Outfit

_SLOT_ARRAY = "ARRAY['outer','top','one_piece','bottom','shoes','accessory']"

_SELECT = f"""
SELECT o.id, o.for_date, o.rank, o.reasoning, o.source, o.status, o.collage_key, o.context,
       o.created_at, o.shown_at, o.superseded_at,
       coalesce(json_agg(json_build_object('item_id', oi.item_id, 'slot', oi.slot)
                ORDER BY array_position({_SLOT_ARRAY}, oi.slot), oi.item_id)
                FILTER (WHERE oi.item_id IS NOT NULL), '[]') AS items
FROM outfits o LEFT JOIN outfit_items oi ON oi.outfit_id = o.id
"""


@dataclass(frozen=True)
class StoredOutfit:
    id: UUID
    for_date: date
    rank: int
    items: tuple[OutfitItem, ...]
    reasoning: str
    source: str
    status: str
    collage_key: str | None
    context: dict[str, Any]
    created_at: datetime
    shown_at: datetime | None
    superseded_at: datetime | None

    @property
    def key(self) -> frozenset:
        return frozenset(i.item_id for i in self.items)

    @property
    def fingerprint(self) -> str | None:
        return self.context.get("fingerprint")

    def suggestion(self) -> OutfitSuggestion:
        return OutfitSuggestion(
            id=self.id, for_date=self.for_date, rank=self.rank, items=list(self.items),
            reasoning=self.reasoning, source=self.source, collage_key=self.collage_key,
        )

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> StoredOutfit:
        raw_items = r["items"]
        if isinstance(raw_items, str):
            raw_items = json.loads(raw_items)
        return cls(
            id=r["id"], for_date=r["for_date"], rank=r["rank"],
            items=tuple(OutfitItem(item_id=UUID(str(i["item_id"])), slot=i["slot"])
                        for i in raw_items),
            reasoning=r["reasoning"] or "", source=r["source"], status=r["status"],
            collage_key=r["collage_key"], context=r["context"] or {},
            created_at=r["created_at"], shown_at=r["shown_at"],
            superseded_at=r["superseded_at"],
        )


async def current_outfits(db: Database, for_date: date) -> list[StoredOutfit]:
    rows = await db.fetch(
        _SELECT + " WHERE o.for_date = %s AND o.superseded_at IS NULL"
        " GROUP BY o.id ORDER BY o.rank, o.created_at",
        (for_date,),
    )
    return [StoredOutfit.from_row(r) for r in rows if r["items"]]


async def day_outfits(db: Database, for_date: date) -> list[StoredOutfit]:
    """Every outfit stored for the day, newest first (current and superseded)."""
    rows = await db.fetch(
        _SELECT + " WHERE o.for_date = %s GROUP BY o.id ORDER BY o.created_at DESC, o.rank",
        (for_date,),
    )
    return [StoredOutfit.from_row(r) for r in rows]


async def get_outfit(db: Database, outfit_id: UUID) -> StoredOutfit | None:
    r = await db.fetchrow(_SELECT + " WHERE o.id = %s GROUP BY o.id", (outfit_id,))
    return StoredOutfit.from_row(r) if r else None


async def replace_current(
    db: Database, for_date: date, outfits: list[Outfit], context: dict[str, Any]
) -> list[UUID]:
    """Supersede the day's current set and store `outfits` as the new one (ranks 1..n),
    in one transaction. With an empty list, the day simply has no current outfits."""
    ids: list[UUID] = []
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE outfits SET superseded_at = now() WHERE for_date = %s"
            " AND superseded_at IS NULL", (for_date,))
        for rank, o in enumerate(outfits, start=1):
            cur = await conn.execute(
                "INSERT INTO outfits (for_date, rank, context, reasoning, source)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (for_date, rank, Jsonb(context), o.reasoning, o.source),
            )
            oid = (await cur.fetchone())["id"]
            for it in o.items:
                await conn.execute(
                    "INSERT INTO outfit_items (outfit_id, item_id, slot) VALUES (%s, %s, %s)",
                    (oid, it.id, it.slot),
                )
            ids.append(oid)
    return ids


async def reactivate(db: Database, for_date: date, outfit_ids: list[UUID],
                     fingerprint: str) -> None:
    """Make earlier stored outfits the current set again (degraded-mode reroll). They take
    the current fingerprint, so ensure_fresh does not immediately rebuild them away."""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE outfits SET superseded_at = now() WHERE for_date = %s"
            " AND superseded_at IS NULL", (for_date,))
        for rank, oid in enumerate(outfit_ids, start=1):
            await conn.execute(
                "UPDATE outfits SET superseded_at = NULL, rank = %s,"
                " context = context || jsonb_build_object('fingerprint', %s::text,"
                " 'reactivated_at', now()) WHERE id = %s",
                (rank, fingerprint, oid))


async def set_collage(db: Database, outfit_id: UUID, key: str) -> None:
    await db.execute("UPDATE outfits SET collage_key = %s WHERE id = %s", (key, outfit_id))


async def mark_shown(db: Database, outfit_ids: list[UUID]) -> int:
    if not outfit_ids:
        return 0
    return await db.execute(
        "UPDATE outfits SET shown_at = coalesce(shown_at, now()),"
        " status = CASE WHEN status = 'suggested' THEN 'shown' ELSE status END"
        " WHERE id = ANY(%s)", (list(outfit_ids),))


async def add_feedback(db: Database, outfit_id: UUID, value: int, note: str | None) -> UUID:
    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO feedback (outfit_id, value, note) VALUES (%s, %s, %s) RETURNING id",
            (outfit_id, value, note))
        fid = (await cur.fetchone())["id"]
        # feedback implies the outfit was seen
        await conn.execute(
            "UPDATE outfits SET shown_at = coalesce(shown_at, now()),"
            " status = CASE WHEN status = 'suggested' THEN 'shown' ELSE status END"
            " WHERE id = %s", (outfit_id,))
    return fid


async def log_wear(
    db: Database, item_ids: list[UUID], worn_on: date, *, source: str,
    outfit_id: UUID | None = None, photo_id: UUID | None = None, conn: Any = None,
) -> list[UUID]:
    """THE writer of wear_log (manual wears, outfits marked worn, check-in confirmations).

    Idempotent per item per day: returns the ids newly logged. A repeat for the same item
    and day keeps the first row and its source, and fills in an outfit_id or photo_id it
    did not have yet. items.last_worn_on only ever moves forward. With `outfit_id`, that
    outfit is marked worn. Pass `conn` to write inside the caller's transaction."""
    if conn is None:
        async with db.transaction() as c:
            return await log_wear(db, item_ids, worn_on, source=source, outfit_id=outfit_id,
                                  photo_id=photo_id, conn=c)
    new: list[UUID] = []
    for iid in dict.fromkeys(item_ids):
        cur = await conn.execute(
            "INSERT INTO wear_log (item_id, worn_on, source, outfit_id, photo_id)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (item_id, worn_on) DO UPDATE SET"
            " outfit_id = coalesce(wear_log.outfit_id, EXCLUDED.outfit_id),"
            " photo_id = coalesce(wear_log.photo_id, EXCLUDED.photo_id)"
            " RETURNING (xmax = 0) AS inserted", (iid, worn_on, source, outfit_id, photo_id))
        row = await cur.fetchone()
        if row and row["inserted"]:
            new.append(iid)
        await conn.execute(
            "UPDATE items SET last_worn_on = greatest(coalesce(last_worn_on, %s), %s)"
            " WHERE id = %s", (worn_on, worn_on, iid))
    if outfit_id is not None:
        await conn.execute(
            "UPDATE outfits SET status = 'worn', shown_at = coalesce(shown_at, now())"
            " WHERE id = %s", (outfit_id,))
    return new


async def transfer_wears(conn: Any, from_item: UUID, to_item: UUID) -> None:
    """Copy one item's wear history onto another (closet merge), keeping the target's row
    where both have one for the same day. The only other statement that writes wear_log."""
    await conn.execute(
        "INSERT INTO wear_log (item_id, worn_on, source, outfit_id, photo_id)"
        " SELECT %s, worn_on, source, outfit_id, photo_id FROM wear_log WHERE item_id = %s"
        " ON CONFLICT (item_id, worn_on) DO UPDATE SET"
        " outfit_id = coalesce(wear_log.outfit_id, EXCLUDED.outfit_id),"
        " photo_id = coalesce(wear_log.photo_id, EXCLUDED.photo_id)", (to_item, from_item))


async def close_out_days(db: Database, before: date) -> int:
    """Outfits shown and not worn on days before `before` become 'skipped'."""
    return await db.execute(
        "UPDATE outfits SET status = 'skipped' WHERE for_date < %s AND status = 'shown'",
        (before,))
