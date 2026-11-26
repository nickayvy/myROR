"""Photos: per-photo pipeline progress, retry, and complete deletion.

GET    /api/photos                     recent photos with status (?source=, ?status=, ?limit=)
GET    /api/photos/{id}                status + its detections
POST   /api/photos/{id}/retry          re-run a failed photo (finished steps are not redone)
DELETE /api/photos/{id}[?delete_items=true]
       Deletes the photo row, its detections and every stored file derived from it
       (photo, cutouts, masks). Closet items made from it are kept without a cutout unless
       delete_items=true.

Live progress also arrives on WS /ws as `pipeline.photo` events (see pipeline/status.py).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from mirror_api.deps import DbDep, JobsDep, ServicesDep, StorageDep
from pipeline.closet import link
from pipeline.deletion import delete_photo
from pipeline.ingest import JOB_FOR_SOURCE
from pipeline.status import set_status
from pipeline.uploads import photo_status

router = APIRouter(prefix="/api/photos", tags=["photos"])


@router.get("")
async def list_photos(db: DbDep, source: str | None = None,
                      status_: Annotated[str | None, Query(alias="status")] = None,
                      limit: int = 100) -> dict[str, Any]:
    where, params = ["true"], []
    if source:
        where.append("source = %s")
        params.append(source)
    if status_:
        where.append("pipeline_status = %s")
        params.append(status_)
    rows = await db.fetch(f"SELECT * FROM photos WHERE {' AND '.join(where)}"
                          " ORDER BY uploaded_at DESC LIMIT %s",
                          [*params, min(max(limit, 1), 500)])
    counts = {r["pipeline_status"]: r["n"] for r in await db.fetch(
        "SELECT pipeline_status, count(*) AS n FROM photos GROUP BY 1")}
    return {"counts": counts, "photos": [photo_status(r) for r in rows]}


@router.get("/{photo_id}")
async def get_photo(photo_id: UUID, db: DbDep, storage: StorageDep) -> dict[str, Any]:
    row = await db.fetchrow("SELECT * FROM photos WHERE id = %s", (photo_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such photo")
    dets = await db.fetch("SELECT id, status, stage, cutout_key, raw_tags, matched_item_id,"
                          " match_score FROM detections WHERE photo_id = %s ORDER BY created_at",
                          (photo_id,))
    return {
        **photo_status(row),
        "photo_url": await link(storage, row["storage_key"]),
        "quality_flags": row["quality_flags"],
        "detections": [{
            "id": str(d["id"]), "status": d["status"], "stage": d["stage"],
            "cutout_url": await link(storage, d["cutout_key"]),
            "category": (d["raw_tags"] or {}).get("category"),
            "matched_item_id": str(d["matched_item_id"]) if d["matched_item_id"] else None,
            "match_score": d["match_score"],
        } for d in dets],
    }


@router.post("/{photo_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry(photo_id: UUID, services: ServicesDep, db: DbDep,
                jobs: JobsDep) -> dict[str, Any]:
    row = await db.fetchrow("SELECT id, source, pipeline_status FROM photos WHERE id = %s",
                            (photo_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such photo")
    if row["pipeline_status"] != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"photo is {row['pipeline_status']}")
    await set_status(services, photo_id, "queued")
    job_id = await jobs.enqueue(JOB_FOR_SOURCE[row["source"]], {"photo_id": str(photo_id)})
    return {"photo_id": str(photo_id), "job_id": str(job_id)}


@router.delete("/{photo_id}")
async def delete(photo_id: UUID, services: ServicesDep,
                 delete_items: bool = False) -> dict[str, Any]:
    report = await delete_photo(services, photo_id, delete_items=delete_items)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such photo")
    await services.hub.broadcast("photo.deleted", {"photo_id": str(photo_id)},
                                 audience="phone")
    return asdict(report)
