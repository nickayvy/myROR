"""Closet items as the recommender sees them, and loading them from the database."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from db import Database
from mirror_api import taxonomy as tx

ITEM_COLUMNS = (
    "id, name, category, subcategory, primary_color, secondary_color, pattern,"
    " material_guess, formality, season, fit, style_tags, state, last_worn_on, cutout_key,"
    " updated_at"
)


@dataclass(frozen=True)
class ClosetItem:
    id: UUID
    name: str
    category: str
    subcategory: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    material: str | None = None
    formality: int | None = None
    season: tuple[str, ...] = ()
    fit: str | None = None
    style_tags: tuple[str, ...] = ()
    state: str = "clean"
    last_worn_on: date | None = None
    cutout_key: str | None = None
    updated_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    # -- derived (taxonomy) --
    @property
    def slot(self) -> str | None:
        return tx.slot_for(self.category)

    @property
    def warmth(self) -> int:
        return tx.warmth(self.subcategory, self.material)

    @property
    def sleeve(self) -> str:
        return tx.sleeve(self.subcategory)

    @property
    def layer_role(self) -> str | None:
        return tx.layer_role(self.category, self.subcategory)

    @property
    def light_fabric(self) -> bool:
        return tx.is_light_fabric(self.material, self.subcategory, self.warmth)

    @property
    def rain_unsuitable(self) -> bool:
        return tx.rain_unsuitable(self.category, self.material, self.subcategory)

    @property
    def rain_layer(self) -> bool:
        return tx.is_rain_layer(self.category, self.subcategory, self.material)

    @property
    def all_season(self) -> bool:
        return not self.season or set(self.season) >= set(tx.SEASONS)

    @property
    def eff_formality(self) -> int:
        """Unknown formality is treated as casual (2)."""
        return self.formality if self.formality in tx.FORMALITY else 2

    @property
    def colour_words(self) -> str:
        c = (self.primary_color or "?").replace("_", " ")
        if self.secondary_color:
            c += "/" + self.secondary_color.replace("_", " ")
        return c

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> ClosetItem:
        return cls(
            id=r["id"],
            name=r.get("name") or "item",
            category=r.get("category") or "",
            subcategory=r.get("subcategory"),
            primary_color=r.get("primary_color"),
            secondary_color=r.get("secondary_color"),
            pattern=r.get("pattern"),
            material=r.get("material_guess"),
            formality=r.get("formality"),
            season=tuple(r.get("season") or ()),
            fit=r.get("fit"),
            style_tags=tuple(r.get("style_tags") or ()),
            state=r.get("state") or "clean",
            last_worn_on=r.get("last_worn_on"),
            cutout_key=r.get("cutout_key"),
            updated_at=r.get("updated_at"),
        )


async def load_closet(db: Database, ids: Sequence[UUID] | None = None) -> list[ClosetItem]:
    """Every item that is not retired (dirty ones too, so the rules can say why an outfit
    is impossible). `ids` restricts to those items (the smoke script uses it)."""
    if ids is not None:
        rows = await db.fetch(
            f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ANY(%s) ORDER BY id", (list(ids),)
        )
    else:
        rows = await db.fetch(
            f"SELECT {ITEM_COLUMNS} FROM items WHERE state <> 'retired' ORDER BY id"
        )
    return [ClosetItem.from_row(r) for r in rows]
