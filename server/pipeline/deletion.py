"""Deleting a photo and everything derived from it (HANDOFF section 14).

Removes: the photo row, its detections (cascade), and every object under
photos/<id>/, cutouts/<id>/, masks/<id>/ plus any key the rows name. Closet items made from
the photo are kept with cutout_key cleared, unless delete_items=True (then the items and
their wear history go too). wear_log rows from a check-in keep the wear, with photo_id
cleared by the foreign key.

Order: storage first (so a failure leaves the rows and the call can be repeated), then the
rows, then one more sweep in case a running job wrote a file in between.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pipeline import keys

log = logging.getLogger(__name__)


@dataclass
class DeletionReport:
    photo_id: str
    objects_deleted: list[str] = field(default_factory=list)
    detections_deleted: int = 0
    items_deleted: list[str] = field(default_factory=list)
    items_kept_without_cutout: list[str] = field(default_factory=list)


async def _sweep(storage: Any, photo_id: UUID, extra: set[str]) -> list[str]:
    found: set[str] = set(k for k in extra if k)
    for prefix in keys.photo_prefixes(photo_id):
        found.update(await storage.list(prefix))
    for k in sorted(found):
        await storage.delete(k)
    return sorted(found)


async def delete_photo(services: Any, photo_id: UUID, *, delete_items: bool = False
                       ) -> DeletionReport | None:
    db, storage = services.db, services.storage
    photo = await db.fetchrow("SELECT * FROM photos WHERE id = %s", (photo_id,))
    if photo is None:
        return None
    dets = await db.fetch("SELECT id, cutout_key, mask_key FROM detections WHERE photo_id = %s",
                          (photo_id,))
    items = await db.fetch("SELECT id, cutout_key FROM items WHERE source_photo_id = %s"
                           " OR cutout_key LIKE %s", (photo_id, f"cutouts/{photo_id}/%"))
    named = {photo["storage_key"], photo["original_key"]}
    for d in dets:
        named.update((d["cutout_key"], d["mask_key"]))
    # Only keys inside this photo's prefixes are ours to delete; an item from another photo
    # never shares them.
    named = {k for k in named if keys.belongs_to_photo(k, photo_id)}
    report = DeletionReport(str(photo_id), detections_deleted=len(dets))
    report.objects_deleted = await _sweep(storage, photo_id, named)

    async with db.transaction() as conn:
        for it in items:
            if delete_items:
                await conn.execute("DELETE FROM items WHERE id = %s", (it["id"],))
                report.items_deleted.append(str(it["id"]))
            else:
                await conn.execute(
                    "UPDATE items SET cutout_key = CASE WHEN cutout_key LIKE %s THEN NULL"
                    " ELSE cutout_key END, updated_at = now() WHERE id = %s",
                    (f"cutouts/{photo_id}/%", it["id"]))
                if keys.belongs_to_photo(it["cutout_key"], photo_id):
                    report.items_kept_without_cutout.append(str(it["id"]))
        await conn.execute("DELETE FROM photos WHERE id = %s", (photo_id,))

    late = await _sweep(storage, photo_id, set())
    if late:
        log.warning("deleted %d object(s) written during deletion of photo %s", len(late),
                    photo_id)
        report.objects_deleted = sorted(set(report.objects_deleted) | set(late))
    return report
