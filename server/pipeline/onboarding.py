"""Onboarding chain for one garment photo (job pipeline.onboard):

  remove background -> crop + store cutout -> tag -> embed -> duplicate check -> inbox

The detection row is created first (stage 'processing') and each step writes its result
onto it, so a retried job skips every step already done (no paid call is repeated once its
result is saved). The inbox only lists stage 'ready'.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from pipeline import keys
from pipeline.imaging import crop_cutout, on_white_jpeg
from pipeline.matching import match_onboarding
from pipeline.status import set_status
from pipeline.tagging import tag_image
from pipeline.wiring import pipeline_providers

log = logging.getLogger(__name__)


async def _detection(db, photo_id: UUID) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT * FROM detections WHERE photo_id = %s ORDER BY created_at LIMIT 1", (photo_id,))
    if row is None:
        row = await db.fetchrow(
            "INSERT INTO detections (photo_id, stage) VALUES (%s, 'processing') RETURNING *",
            (photo_id,))
    return row


async def _note(db, det_id: UUID, key: str, value: Any) -> None:
    await db.execute(
        "UPDATE detections SET notes = notes || jsonb_build_object(%s::text, %s::jsonb)"
        " WHERE id = %s", (key, Jsonb(value), det_id))


async def photo_exists(db, photo_id: UUID) -> bool:
    return await db.fetchval("SELECT 1 FROM photos WHERE id = %s", (photo_id,)) is not None


async def run_onboarding(services: Any, photo_id: UUID, *, job_id: UUID | None = None) -> None:
    db, storage, settings = services.db, services.storage, services.settings
    photo = await db.fetchrow("SELECT * FROM photos WHERE id = %s", (photo_id,))
    if photo is None:
        return  # deleted before the job ran
    prov = pipeline_providers(services)
    det = await _detection(db, photo_id)
    det_id = det["id"]
    notes = det["notes"] or {}

    # 1. background removal + crop
    if det["cutout_key"] is None and "bg" not in notes:
        await set_status(services, photo_id, "processing", "remove_background")
        data = await storage.get(photo["storage_key"])
        result = await prov.bg_remover.remove(data=data, key=photo["storage_key"],
                                              content_type="image/jpeg", job_id=job_id)
        cropped = await asyncio.to_thread(crop_cutout, result.png)
        bg_note = {"provider": result.provider, "model": result.model, **result.meta}
        if cropped is None:
            bg_note["error"] = "no garment found after background removal"
            await _note(db, det_id, "bg", bg_note)
        else:
            png, box, coverage = cropped
            key = keys.cutout(photo_id, det_id)
            if not await photo_exists(db, photo_id):
                return
            await storage.put(key, png, "image/png")
            bg_note["coverage"] = round(coverage, 4)
            await db.execute(
                "UPDATE detections SET cutout_key = %s, box = %s,"
                " notes = notes || jsonb_build_object('bg', %s::jsonb) WHERE id = %s",
                (key, Jsonb(box.as_dict()), Jsonb(bg_note), det_id))
        det = await db.fetchrow("SELECT * FROM detections WHERE id = %s", (det_id,))
        notes = det["notes"] or {}

    cutout_png = await storage.get(det["cutout_key"]) if det["cutout_key"] else None

    # 2. tagging (a failure still lands in the inbox, marked for manual tagging)
    if "tagging" not in notes:
        if cutout_png is None:
            await _note(db, det_id, "tagging", {"status": "skipped", "errors": ["no cutout"],
                                                "needs_review": True})
        else:
            await set_status(services, photo_id, "processing", "tag")
            thumb = await asyncio.to_thread(on_white_jpeg, cutout_png)
            outcome = await tag_image(services.llm, settings.llm_tag_model, thumb,
                                      job_id=job_id)
            await db.execute(
                "UPDATE detections SET raw_tags = %s, confidence = %s,"
                " notes = notes || jsonb_build_object('tagging', %s::jsonb) WHERE id = %s",
                (Jsonb(outcome.tags), outcome.confidence,
                 Jsonb(outcome.notes(settings.llm_tag_model)), det_id))
        det = await db.fetchrow("SELECT * FROM detections WHERE id = %s", (det_id,))

    # 3. embedding (skipped when the real embedder is not installed; backfilled later)
    emb = prov.embedder
    if cutout_png is not None and emb is not None and (
            det["embedding"] is None or det["embedding_model"] != emb.name):
        await set_status(services, photo_id, "processing", "embed")
        vec = await emb.embed(cutout_png)
        await db.execute("UPDATE detections SET embedding = %s, embedding_model = %s"
                         " WHERE id = %s", (vec, emb.name, det_id))
        det = await db.fetchrow("SELECT * FROM detections WHERE id = %s", (det_id,))

    # 4. duplicate check, then into the inbox
    await set_status(services, photo_id, "processing", "match")
    match = await match_onboarding(db, embedding=det["embedding"],
                                   embedding_model=det["embedding_model"],
                                   tags=det["raw_tags"] or {})
    await db.execute(
        "UPDATE detections SET matched_item_id = %s, match_score = %s, stage = 'ready',"
        " notes = notes || jsonb_build_object('match', %s::jsonb) WHERE id = %s",
        (match.item_id if match else None, match.score if match else None,
         Jsonb(match.notes() if match else None), det_id))
    await set_status(services, photo_id, "done")
    await services.hub.broadcast("inbox.updated", {"photo_id": str(photo_id),
                                                   "detection_id": str(det_id)},
                                 audience="phone")
