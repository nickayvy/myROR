"""What makes a set of items a wearable outfit. Shared by the validator (LLM output)
and the rules-only composer, so both enforce exactly the same structure."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from recommender.closet import ClosetItem
from recommender.rules import Requirements

MAX_PER_SLOT = {"top": 2, "bottom": 1, "one_piece": 1, "outer": 1, "shoes": 1, "accessory": 3}


@dataclass(frozen=True)
class Outfit:
    """A validated outfit: items in slot order and a one-line reason."""

    items: tuple[ClosetItem, ...]
    reasoning: str
    source: str  # "llm" | "rules"

    @property
    def key(self) -> frozenset:
        return frozenset(i.id for i in self.items)


SLOT_ORDER = ("outer", "top", "one_piece", "bottom", "shoes", "accessory")


def sort_items(items: Sequence[ClosetItem]) -> tuple[ClosetItem, ...]:
    def k(i: ClosetItem):
        role = 0 if i.layer_role == "base" else 1
        return (SLOT_ORDER.index(i.slot) if i.slot in SLOT_ORDER else 9, role, str(i.id))

    return tuple(sorted(items, key=k))


def structure_errors(items: Sequence[ClosetItem], req: Requirements) -> list[str]:
    """Empty list when `items` form a complete outfit under `req`."""
    errs: list[str] = []
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        errs.append("the same item is used twice")
    slots = Counter(i.slot for i in items)
    for slot, n in slots.items():
        cap = MAX_PER_SLOT.get(slot or "", 0)
        if n > cap:
            errs.append(f"{n} items in slot {slot}; at most {cap}")
    tops = [i for i in items if i.slot == "top"]
    if len(tops) == 2 and sorted(t.layer_role or "" for t in tops) != ["base", "mid"]:
        errs.append("two tops must be one base top plus one layering top (sweater, cardigan, "
                    "sweatshirt or hoodie)")
    if slots["one_piece"]:
        if slots["bottom"]:
            errs.append("a one-piece cannot be combined with a bottom")
        if any(t.layer_role == "base" for t in tops):
            errs.append("a one-piece can only be layered with a sweater, cardigan, sweatshirt "
                        "or hoodie, not another top")
    else:
        if not tops:
            errs.append("missing a top (or a one-piece)")
        if not slots["bottom"]:
            errs.append("missing a bottom (or a one-piece)")
    if req.shoes_required and not slots["shoes"]:
        errs.append("missing shoes")
    if req.outer_required and not slots["outer"]:
        errs.append("missing an outer layer, which is required today because it is cold")
    return errs
