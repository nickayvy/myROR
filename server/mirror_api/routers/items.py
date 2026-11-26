"""Closet: upload one garment photo, list/read/edit items, state, merge.

POST  /api/items/upload               one file (multipart field "file"); 201 new, 200 if the
                                      same bytes were uploaded before (idempotent by SHA-256)
GET   /api/items                      filters: category, subcategory, state (repeatable;
                                      default clean+dirty), color, season, q, limit, offset
GET   /api/items/{id}
PATCH /api/items/{id}                 name and/or taxonomy fields, validated
POST  /api/items/{id}/state           {"state": "clean" | "dirty" | "retired"}
POST  /api/items/{id}/merge           {"into": "<item id>"}: fold this item into another
POST  /api/items/backfill-embeddings  enqueue pipeline.backfill_embeddings

POST /api/items/{id}/worn belongs to the recommender (not here).
Image links in responses are presigned for 15 minutes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict

from mirror_api.deps import DbDep, JobsDep, ServicesDep, StorageDep
from mirror_api.taxonomy import TAG_FIELDS, TaxonomyError, validate_tags
from pipeline.closet import ITEM_COLUMNS, InboxError, item_out, merge_items, tags_of
from pipeline.uploads import handle_upload

router = APIRouter(prefix="/api/items", tags=["items"])

States = Literal["clean", "dirty", "retired"]


class StateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: States


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: UUID


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(services: ServicesDep, response: Response,
                 file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    return await handle_upload(services, file, "phone_onboarding", response)


@router.post("/backfill-embeddings", status_code=status.HTTP_202_ACCEPTED)
async def backfill(jobs: JobsDep) -> dict[str, str]:
    job_id = await jobs.enqueue("pipeline.backfill_embeddings", {})
    return {"job_id": str(job_id)}


@router.get("")
async def list_items(
    db: DbDep, storage: StorageDep,
    category: str | None = None, subcategory: str | None = None,
    state: Annotated[list[States] | None, Query()] = None, color: str | None = None,
    season: str | None = None, q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    where = ["state = ANY(%s)"]
    params: list[Any] = [list(state or ["clean", "dirty"])]
    for col, val in (("category", category), ("subcategory", subcategory)):
        if val:
            where.append(f"{col} = %s")
            params.append(val)
    if color:
        where.append("(primary_color = %s OR secondary_color = %s)")
        params += [color, color]
    if season:
        where.append("(%s = ANY(season) OR cardinality(season) = 0)")
        params.append(season)
    if q:
        where.append("name ILIKE %s")
        params.append(f"%{q}%")
    sql = f"SELECT {ITEM_COLUMNS} FROM items WHERE {' AND '.join(where)}"
    total = await db.fetchval(f"SELECT count(*) FROM items WHERE {' AND '.join(where)}", params)
    rows = await db.fetch(sql + " ORDER BY category, name, id LIMIT %s OFFSET %s",
                          [*params, limit, offset])
    return {"total": total, "items": [await item_out(r, storage) for r in rows]}


async def _get(db, item_id: UUID) -> dict[str, Any]:
    row = await db.fetchrow(f"SELECT {ITEM_COLUMNS} FROM items WHERE id = %s", (item_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item")
    return row


@router.get("/{item_id}")
async def get_item(item_id: UUID, db: DbDep, storage: StorageDep) -> dict[str, Any]:
    return await item_out(await _get(db, item_id), storage)


@router.patch("/{item_id}")
async def patch_item(item_id: UUID, body: dict[str, Any], db: DbDep,
                     storage: StorageDep) -> dict[str, Any]:
    unknown = sorted(set(body) - {*TAG_FIELDS, "name"})
    if unknown:
        raise HTTPException(422, {"errors": [f"unknown field(s) {unknown}"]})
    row = await _get(db, item_id)
    merged = {**tags_of(row), "name": row["name"], **body}
    try:
        tags = validate_tags(merged)
    except TaxonomyError as e:
        raise HTTPException(422, {"errors": e.errors}) from None
    sets = [f"{f} = %s" for f in (*TAG_FIELDS, "name")]
    params = [tags.get(f) if f not in ("season", "style_tags") else (tags.get(f) or [])
              for f in TAG_FIELDS] + [tags["name"]]
    if body.get("primary_color") and body["primary_color"] != row["primary_color"]:
        sets.append("primary_lab = NULL")  # the measured colour no longer matches the tag
    row = await db.fetchrow(
        f"UPDATE items SET {', '.join(sets)}, updated_at = now() WHERE id = %s"
        f" RETURNING {ITEM_COLUMNS}", [*params, item_id])
    return await item_out(row, storage)


@router.post("/{item_id}/state")
async def set_state(item_id: UUID, body: StateBody, db: DbDep,
                    storage: StorageDep) -> dict[str, Any]:
    row = await db.fetchrow(f"UPDATE items SET state = %s, updated_at = now() WHERE id = %s"
                            f" RETURNING {ITEM_COLUMNS}", (body.state, item_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item")
    return await item_out(row, storage)


@router.post("/{item_id}/merge")
async def merge(item_id: UUID, body: MergeBody, services: ServicesDep,
                storage: StorageDep) -> dict[str, Any]:
    try:
        row = await merge_items(services, item_id, body.into)
    except InboxError as e:
        raise HTTPException(e.status, str(e)) from None
    return {"merged_from": str(item_id), "item": await item_out(row, storage)}
