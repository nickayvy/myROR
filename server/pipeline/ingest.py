"""Storing an uploaded photo: idempotent by content hash, metadata stripped, job enqueued.

`ingest_photo` is the single entry point for every source:
- phone_onboarding  (POST /api/items/upload): white-balanced, job pipeline.onboard
- phone_fullbody    (POST /api/checkins/upload): job pipeline.checkin
- mirror_checkin    (pipeline.checkin_from_bytes, for the capture flow): job pipeline.checkin

With FACE_BLUR on, full-body and mirror photos go through the FaceBlurrer first; if it is
unavailable or finds no face the call raises and nothing is stored or sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from pipeline import keys
from pipeline.imaging import prepare_upload
from pipeline.wiring import pipeline_providers

log = logging.getLogger(__name__)

JOB_FOR_SOURCE = {
    "phone_onboarding": "pipeline.onboard",
    "phone_fullbody": "pipeline.checkin",
    "mirror_checkin": "pipeline.checkin",
}


@dataclass
class Ingested:
    photo: dict[str, Any]
    created: bool


async def ingest_photo(services: Any, data: bytes, *, source: str) -> Ingested:
    if source not in JOB_FOR_SOURCE:
        raise ValueError(f"unknown photo source {source!r}")
    db, storage = services.db, services.storage
    sha = hashlib.sha256(data).hexdigest()
    existing = await db.fetchrow("SELECT * FROM photos WHERE sha256 = %s", (sha,))
    if existing is not None:
        return Ingested(existing, False)

    prepared = await asyncio.to_thread(
        prepare_upload, data, balance=(source == "phone_onboarding"))
    photo_id = uuid4()
    original_key = keys.photo_original(photo_id)
    storage_key, stored_original_key, blurred = original_key, None, False
    to_store = [(original_key, prepared.jpeg)]
    if source != "phone_onboarding" and services.settings.face_blur:
        blurrer = pipeline_providers(services).face_blurrer
        blurred_jpeg = await blurrer.blur(prepared.jpeg)  # raises: nothing stored yet
        storage_key, stored_original_key, blurred = keys.photo_blurred(photo_id), \
            original_key, True
        to_store.append((storage_key, blurred_jpeg))

    for key, body in to_store:
        await storage.put(key, body, "image/jpeg")
    taken_at = prepared.taken_at.replace(tzinfo=services.settings.tz).astimezone(UTC) \
        if prepared.taken_at else None
    try:
        async with db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO photos (id, source, storage_key, original_key, sha256, width,"
                " height, face_blurred, taken_at, quality_flags, pipeline_status)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')"
                " ON CONFLICT (sha256) DO NOTHING RETURNING *",
                (photo_id, source, storage_key, stored_original_key, sha, prepared.width,
                 prepared.height, blurred, taken_at, Jsonb(prepared.flags)),
            )
            row = await cur.fetchone()
            if row is not None:
                await services.jobs.enqueue(JOB_FOR_SOURCE[source],
                                            {"photo_id": str(photo_id)}, conn=conn)
    except BaseException:
        for key, _ in to_store:
            await storage.delete(key)
        raise
    if row is None:  # lost a race with an identical upload
        for key, _ in to_store:
            await storage.delete(key)
        return Ingested(await db.fetchrow("SELECT * FROM photos WHERE sha256 = %s", (sha,)),
                        False)
    await services.hub.broadcast(
        "pipeline.photo", {"photo_id": str(photo_id), "source": source, "status": "queued",
                           "step": None, "error": None}, audience="phone")
    return Ingested(row, True)


async def checkin_from_bytes(services: Any, data: bytes,
                             source: str = "mirror_checkin") -> UUID:
    """For the capture flow: store a mirror photo and start the check-in pipeline.
    Returns the photo id (an existing one if these exact bytes were already stored)."""
    result = await ingest_photo(services, data, source=source)
    return result.photo["id"]
