"""Matching a new garment against the closet.

Scores are similarities in [0, 1] (1 - pgvector cosine distance, or a tag-agreement score);
detections.match_score stores that number. Vectors are only compared with vectors from the
same embedder (embedding_model).

Starting thresholds [UNTESTED: tune on the bake-off and the first real closet]:
- Onboarding, embeddings: >= 0.92 "duplicate" (cosine distance <= 0.08), 0.82-0.92
  "possible" (distance 0.08-0.18), below that no match. Two photos of the same garment on
  the same sheet under the same light usually score well above 0.9 with CLIP-family image
  embeddings, while two different items of the same kind and colour (two navy tees) can
  reach the mid-0.8s, which is why 0.82-0.92 is a band for a human to decide, not a merge.
- Onboarding, tags only (no embedder): never "duplicate"; >= 0.85 tag agreement within the
  same category is "possible". Tags cannot tell two navy tees apart.
- Check-in (a garment on a body vs a flat cutout, a much harder comparison): same category
  only; best candidate proposed if >= 0.55 (embedding) or >= 0.6 (tags); "likely" at
  >= 0.75 (embedding) or >= 0.8 (tags).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import numpy as np

ONBOARD_DUPLICATE = 0.92
ONBOARD_POSSIBLE = 0.82
ONBOARD_TAG_POSSIBLE = 0.85
CHECKIN_PROPOSE = 0.55
CHECKIN_LIKELY = 0.75
CHECKIN_TAG_PROPOSE = 0.60
CHECKIN_TAG_LIKELY = 0.80

TAG_WEIGHTS = {"subcategory": 0.35, "primary_color": 0.25, "pattern": 0.15,
               "material_guess": 0.10, "secondary_color": 0.10, "fit": 0.05}

Kind = Literal["duplicate", "possible", "likely", "uncertain"]


@dataclass
class Match:
    item_id: UUID
    score: float
    kind: Kind
    method: Literal["embedding", "tags"]
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def notes(self) -> dict[str, Any]:
        return {"kind": self.kind, "method": self.method, "score": round(self.score, 4),
                "candidates": self.candidates}


def tag_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    if not a.get("category") or a.get("category") != b.get("category"):
        return 0.0
    total = got = 0.0
    for f, w in TAG_WEIGHTS.items():
        va, vb = a.get(f), b.get(f)
        if va is None and vb is None:
            continue
        total += w
        if va is not None and va == vb:
            got += w
    return got / total if total else 0.0


async def _vector_candidates(db, embedding: np.ndarray, model: str, category: str | None,
                             limit: int = 3, exclude: UUID | None = None):
    sql = ("SELECT id, name, 1 - (embedding <=> %s) AS sim FROM items"
           " WHERE state <> 'retired' AND embedding IS NOT NULL AND embedding_model = %s")
    params: list[Any] = [embedding, model]
    if category:
        sql += " AND category = %s"
        params.append(category)
    if exclude:
        sql += " AND id <> %s"
        params.append(exclude)
    sql += " ORDER BY embedding <=> %s LIMIT %s"
    params += [embedding, limit]
    return await db.fetch(sql, params)


async def _tag_candidates(db, tags: dict[str, Any], limit: int = 3):
    if not tags.get("category"):
        return []
    rows = await db.fetch(
        "SELECT id, name, category, subcategory, primary_color, secondary_color, pattern,"
        " material_guess, fit FROM items WHERE state <> 'retired' AND category = %s",
        (tags["category"],),
    )
    scored = sorted(({"id": r["id"], "name": r["name"], "sim": tag_similarity(tags, r)}
                     for r in rows), key=lambda r: -r["sim"])
    return scored[:limit]


def _cands(rows) -> list[dict[str, Any]]:
    return [{"item_id": str(r["id"]), "name": r["name"], "score": round(float(r["sim"]), 4)}
            for r in rows]


async def match_onboarding(db, *, embedding: np.ndarray | None, embedding_model: str | None,
                           tags: dict[str, Any]) -> Match | None:
    if embedding is not None and embedding_model:
        rows = await _vector_candidates(db, embedding, embedding_model, None)
        if rows:
            best = rows[0]
            sim = float(best["sim"])
            if sim >= ONBOARD_POSSIBLE:
                kind: Kind = "duplicate" if sim >= ONBOARD_DUPLICATE else "possible"
                return Match(best["id"], sim, kind, "embedding", _cands(rows))
            return None
        # No comparable vectors in the closet yet (e.g. all items await backfill): tags.
    rows = await _tag_candidates(db, tags)
    if rows and rows[0]["sim"] >= ONBOARD_TAG_POSSIBLE:
        return Match(rows[0]["id"], float(rows[0]["sim"]), "possible", "tags", _cands(rows))
    return None


async def match_checkin(db, *, embedding: np.ndarray | None, embedding_model: str | None,
                        tags: dict[str, Any]) -> Match | None:
    category = tags.get("category")
    if not category:
        return None
    if embedding is not None and embedding_model:
        rows = await _vector_candidates(db, embedding, embedding_model, category)
        if rows:
            sim = float(rows[0]["sim"])
            if sim < CHECKIN_PROPOSE:
                return None
            kind: Kind = "likely" if sim >= CHECKIN_LIKELY else "uncertain"
            return Match(rows[0]["id"], sim, kind, "embedding", _cands(rows))
    rows = await _tag_candidates(db, tags)
    if rows and rows[0]["sim"] >= CHECKIN_TAG_PROPOSE:
        sim = float(rows[0]["sim"])
        kind = "likely" if sim >= CHECKIN_TAG_LIKELY else "uncertain"
        return Match(rows[0]["id"], sim, kind, "tags", _cands(rows))
    return None
