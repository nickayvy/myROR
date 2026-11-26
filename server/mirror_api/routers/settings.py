"""GET/PUT /api/settings: user-editable settings in the `settings` table (jsonb values).

PUT merges: keys in the body are upserted, a null value deletes the key, other
keys are untouched. Feature code reads with `get_setting(db, key, default)`.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, status
from psycopg.types.json import Jsonb

from db import Database
from mirror_api.deps import DbDep, HubDep

router = APIRouter(prefix="/api/settings", tags=["settings"])
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


async def get_setting(db: Database, key: str, default: Any = None) -> Any:
    row = await db.fetchrow("SELECT value FROM settings WHERE key = %s", (key,))
    return default if row is None else row["value"]


async def all_settings(db: Database) -> dict[str, Any]:
    rows = await db.fetch("SELECT key, value FROM settings ORDER BY key")
    return {r["key"]: r["value"] for r in rows}


@router.get("")
async def read(db: DbDep) -> dict[str, Any]:
    return await all_settings(db)


@router.put("")
async def write(body: dict[str, Any], db: DbDep, hub: HubDep) -> dict[str, Any]:
    if len(body) > 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "too many keys")
    bad = [k for k in body if not KEY_RE.match(k)]
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid keys: {bad}")
    async with db.transaction() as conn:
        for key, value in body.items():
            if value is None:
                await conn.execute("DELETE FROM settings WHERE key = %s", (key,))
            else:
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s)"
                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (key, Jsonb(value)),
                )
    result = await all_settings(db)
    await hub.broadcast("settings.updated", {"keys": sorted(body)})
    return result
