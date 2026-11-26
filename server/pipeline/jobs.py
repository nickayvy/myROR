"""Job handlers for the closet pipeline (imported at startup by mirror_api.app)."""

from __future__ import annotations

import logging
from uuid import UUID

from mirror_api.jobs import JobContext, PermanentJobError, job
from mirror_api.spend import SpendCapExceeded
from pipeline.checkin import CheckinFailed, run_checkin
from pipeline.faceblur import FaceBlurUnavailable
from pipeline.onboarding import run_onboarding
from pipeline.status import set_status
from pipeline.wiring import pipeline_providers
from providers.bg_remover import BgRemovalError
from providers.fal import FalNotConfigured, FalRequestError, FalResponseError

log = logging.getLogger(__name__)

# Not worth retrying: configuration or input problems.
_PERMANENT = (FalNotConfigured, FalRequestError, FalResponseError, BgRemovalError,
              FaceBlurUnavailable)


async def _guarded(ctx: JobContext, photo_id: UUID, runner) -> None:
    try:
        await runner(ctx.services, photo_id, job_id=ctx.job_id)
    except SpendCapExceeded:
        await set_status(ctx.services, photo_id, "queued", "waiting_for_budget",
                         "spend cap reached; will resume next period")
        raise
    except CheckinFailed as e:
        await set_status(ctx.services, photo_id, "failed", None, str(e))
        raise PermanentJobError(str(e)) from e
    except _PERMANENT as e:
        await set_status(ctx.services, photo_id, "failed", None, f"{type(e).__name__}: {e}")
        raise PermanentJobError(str(e)) from e
    except Exception as e:
        if ctx.is_last_attempt:
            await set_status(ctx.services, photo_id, "failed", None,
                             f"{type(e).__name__}: {e}"[:500])
        else:
            await set_status(ctx.services, photo_id, "queued", "retrying",
                             f"{type(e).__name__}: {e}"[:500])
        raise


@job("pipeline.onboard", max_attempts=3)
async def onboard(ctx: JobContext, payload: dict) -> None:
    await _guarded(ctx, UUID(payload["photo_id"]), run_onboarding)


@job("pipeline.checkin", max_attempts=3)
async def checkin(ctx: JobContext, payload: dict) -> None:
    await _guarded(ctx, UUID(payload["photo_id"]), run_checkin)


@job("pipeline.backfill_embeddings", max_attempts=2)
async def backfill_embeddings(ctx: JobContext, payload: dict) -> None:
    """Embed every item and pending detection that has a cutout but no vector from the
    current embedder. A no-op while the real embedder is not installed."""
    services = ctx.services
    emb = pipeline_providers(services).embedder
    if emb is None:
        log.info("backfill_embeddings: no embedder installed; nothing to do")
        return
    db, storage = services.db, services.storage
    done = 0
    for table in ("items", "detections"):
        extra = " AND status = 'pending'" if table == "detections" else ""
        rows = await db.fetch(
            f"SELECT id, cutout_key FROM {table} WHERE cutout_key IS NOT NULL"
            f" AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s){extra}",
            (emb.name,))
        for r in rows:
            try:
                png = await storage.get(r["cutout_key"])
            except KeyError:
                continue
            vec = await emb.embed(png)
            await db.execute(f"UPDATE {table} SET embedding = %s, embedding_model = %s"
                             " WHERE id = %s", (vec, emb.name, r["id"]))
            done += 1
    log.info("backfill_embeddings: embedded %d image(s) with %s", done, emb.name)
