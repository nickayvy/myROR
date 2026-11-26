"""Check-in pipeline (job pipeline.checkin): "did you wear these?" from one waist-up photo.

  one Claude vision call (garments: label + pixel box + taxonomy tags)
  -> one SAM 3 call with every box (masks) -> masked cutouts -> embed -> match per garment

The vision result is saved as detection rows (stage 'processing') in the same transaction
that marks the call done in photos.pipeline_state, so a retry never repeats it; segmentation
only runs for detections without a mask. A refusal ends the job with "could not analyse this
photo" and no retry.

Vision call cost: the photo is sent at most 1024 px on the long edge (<= ~1,400 image
tokens), plus ~950 prompt tokens and the schema; about 2,000-2,800 input and 300-900 output
tokens at effort "medium", roughly $0.0005-0.0007 per check-in on Haiku 5.5.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from mirror_api.taxonomy import TaxonomyError, item_tags_schema, tagger_guide, validate_tags
from pipeline import keys
from pipeline.imaging import for_llm, masked_cutout
from pipeline.matching import match_checkin
from pipeline.status import set_status
from pipeline.tagging import partial_tags
from pipeline.wiring import pipeline_providers
from providers.llm import (
    LLMNotConfigured,
    LLMRefusal,
    LLMRequestError,
    LLMSchemaError,
    LLMTruncated,
    image_block,
    text_block,
)
from providers.segmenter import Box

log = logging.getLogger(__name__)

MAX_GARMENTS = 8
COULD_NOT_ANALYSE = "could not analyse this photo"

SYSTEM = f"""You help the owner of a wardrobe app log which of their clothes they are wearing \
today. The image is a photo they took of themselves in their mirror, usually chest or waist up.

List each garment or accessory the person is wearing that is visible in the photo. For each \
one give taxonomy tags (choose every value from the allowed lists; the name is a short plain \
description such as "grey crew-neck sweater") and a bounding box in pixels of this image: \
origin at the top-left corner, x to the right, y downwards, tight around the visible part of \
that garment only. Layered garments (a jacket over a shirt) are separate entries with their \
own boxes.

Describe the clothes only. Never comment on the person's body, shape, size, weight, skin, \
face, hair or attractiveness, and do not judge how they look. Never guess or mention age, \
gender, ethnicity, health, religion, sexuality or any other personal or sensitive trait, and \
do not try to identify the person. Ignore other people, clothes nobody is wearing (towels, \
hangers, laundry) and objects in the room. If no garment is visible or the photo is too dark \
or blurred to tell, return an empty list and set image_usable to false.

{tagger_guide()}"""


def vision_schema() -> dict[str, Any]:
    box = {
        "type": "object",
        "properties": {k: {"type": "integer"} for k in ("x_min", "y_min", "x_max", "y_max")},
        "required": ["x_min", "y_min", "x_max", "y_max"],
        "additionalProperties": False,
    }
    garment = {
        "type": "object",
        "properties": {
            "tags": item_tags_schema(include_name=True),
            "box": box,
            "visibility": {"type": "string", "enum": ["mostly_visible", "partly_visible"]},
        },
        "required": ["tags", "box", "visibility"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"image_usable": {"type": "boolean"},
                       "garments": {"type": "array", "items": garment}},
        "required": ["image_usable", "garments"],
        "additionalProperties": False,
    }


class CheckinFailed(Exception):
    """Ends the job without retry; the photo is marked failed with this message."""


def _box_from_model(raw: dict[str, Any], scale: float, width: int, height: int) -> Box | None:
    try:
        b = Box(round(raw["x_min"] * scale), round(raw["y_min"] * scale),
                round(raw["x_max"] * scale), round(raw["y_max"] * scale)).clamp(width, height)
    except (KeyError, TypeError, ValueError):
        return None
    if (b.x_max - b.x_min) < 8 or (b.y_max - b.y_min) < 8:
        return None
    return b


async def _analyse(services: Any, photo: dict[str, Any], job_id: UUID | None) -> None:
    """The one vision call; stores detection rows and marks it done, atomically."""
    db, storage, settings = services.db, services.storage, services.settings
    data = await storage.get(photo["storage_key"])
    small, scale, w, h = await asyncio.to_thread(for_llm, data)
    content = [image_block(small, "image/jpeg"),
               text_block(f"The image is {w} x {h} pixels. List the garments being worn.")]
    try:
        result = await services.llm.complete_structured(
            model=settings.llm_checkin_model, system=SYSTEM, content=content,
            json_schema=vision_schema(), purpose="checkin_vision", effort="medium",
            max_tokens=4096, job_id=job_id)
    except LLMRefusal as e:
        log.info("check-in vision refused: %s", e)
        raise CheckinFailed(COULD_NOT_ANALYSE) from e
    except (LLMSchemaError, LLMTruncated, LLMRequestError, LLMNotConfigured) as e:
        log.warning("check-in vision failed: %s", e)
        raise CheckinFailed(COULD_NOT_ANALYSE) from e

    garments = result.get("garments") or []
    rows = []
    for g in garments[:MAX_GARMENTS]:
        box = _box_from_model(g.get("box") or {}, scale, photo["width"], photo["height"])
        if box is None:
            continue
        raw = g.get("tags") or {}
        try:
            tags, status, errors = validate_tags(raw), "ok", []
        except TaxonomyError as e:
            tags, status, errors = partial_tags(raw, e.errors), "partial", e.errors
        rows.append((box, tags, {"label": tags.get("name") or raw.get("name") or "garment",
                                 "visibility": g.get("visibility"),
                                 "tagging": {"status": status, "errors": errors[:10],
                                             "model": settings.llm_checkin_model}}))
    state = {"vision": {"model": settings.llm_checkin_model, "garments": len(rows),
                        "returned": len(garments),
                        "image_usable": bool(result.get("image_usable"))}}
    async with db.transaction() as conn:
        for box, tags, notes in rows:
            await conn.execute(
                "INSERT INTO detections (photo_id, box, raw_tags, notes, stage)"
                " VALUES (%s, %s, %s, %s, 'processing')",
                (photo["id"], Jsonb(box.as_dict()), Jsonb(tags), Jsonb(notes)))
        await conn.execute(
            "UPDATE photos SET pipeline_state = pipeline_state || %s WHERE id = %s",
            (Jsonb(state), photo["id"]))


async def run_checkin(services: Any, photo_id: UUID, *, job_id: UUID | None = None) -> None:
    db, storage = services.db, services.storage
    photo = await db.fetchrow("SELECT * FROM photos WHERE id = %s", (photo_id,))
    if photo is None:
        return
    prov = pipeline_providers(services)

    if "vision" not in (photo["pipeline_state"] or {}):
        await set_status(services, photo_id, "processing", "analyse")
        await _analyse(services, photo, job_id)

    dets = await db.fetch("SELECT * FROM detections WHERE photo_id = %s ORDER BY created_at,"
                          " id", (photo_id,))
    todo = [d for d in dets if d["mask_key"] is None]
    if todo:
        await set_status(services, photo_id, "processing", "segment")
        data = await storage.get(photo["storage_key"])
        masks = await prov.segmenter.segment(
            data=data, key=photo["storage_key"], boxes=[Box.from_dict(d["box"]) for d in todo],
            labels=[(d["notes"] or {}).get("label", "") for d in todo],
            content_type="image/jpeg", purpose="checkin_segment", job_id=job_id)
        for d, m in zip(todo, masks, strict=True):
            if not await _photo_exists(db, photo_id):
                return
            mkey = keys.mask(photo_id, d["id"])
            await storage.put(mkey, m.mask_png, "image/png")
            cut = await asyncio.to_thread(masked_cutout, data, m.mask_png)
            ckey = None
            if cut is not None:
                ckey = keys.cutout(photo_id, d["id"])
                await storage.put(ckey, cut[0], "image/png")
            seg = {"method": m.method, "iou": round(m.iou, 3), "segmenter": prov.segmenter.name}
            await db.execute(
                "UPDATE detections SET mask_key = %s, cutout_key = %s,"
                " notes = notes || jsonb_build_object('segment', %s::jsonb) WHERE id = %s",
                (mkey, ckey, Jsonb(seg), d["id"]))
        dets = await db.fetch("SELECT * FROM detections WHERE photo_id = %s ORDER BY"
                              " created_at, id", (photo_id,))

    emb = prov.embedder
    if emb is not None:
        need = [d for d in dets if d["cutout_key"] and (
            d["embedding"] is None or d["embedding_model"] != emb.name)]
        if need:
            await set_status(services, photo_id, "processing", "embed")
        for d in need:
            vec = await emb.embed(await storage.get(d["cutout_key"]))
            await db.execute("UPDATE detections SET embedding = %s, embedding_model = %s"
                             " WHERE id = %s", (vec, emb.name, d["id"]))
        if need:
            dets = await db.fetch("SELECT * FROM detections WHERE photo_id = %s ORDER BY"
                                  " created_at, id", (photo_id,))

    await set_status(services, photo_id, "processing", "match")
    for d in dets:
        m = await match_checkin(db, embedding=d["embedding"],
                                embedding_model=d["embedding_model"], tags=d["raw_tags"] or {})
        await db.execute(
            "UPDATE detections SET matched_item_id = %s, match_score = %s, stage = 'ready',"
            " notes = notes || jsonb_build_object('match', %s::jsonb) WHERE id = %s",
            (m.item_id if m else None, m.score if m else None, Jsonb(m.notes() if m else None),
             d["id"]))
    await set_status(services, photo_id, "done")
    await services.hub.broadcast("checkin.ready", {"photo_id": str(photo_id),
                                                   "garments": len(dets)}, audience="phone")


async def _photo_exists(db, photo_id: UUID) -> bool:
    return await db.fetchval("SELECT 1 FROM photos WHERE id = %s", (photo_id,)) is not None
