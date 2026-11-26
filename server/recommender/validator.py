"""Validate the composer's raw LLM output against the candidate table.

The LLM sees short codes ("T3", "B1"), never database IDs. The only path from LLM
output to an item is `CandidateTable.lookup(code)`, which returns an item from the
rules' candidate list or nothing; that is the "zero invented items" guarantee. An
outfit with any unknown code is rejected whole rather than repaired, because dropping
the bad code could silently change what the outfit is.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from recommender.outfit import Outfit, sort_items, structure_errors
from recommender.rules import Candidate, RulesResult

SLOT_PREFIX = {"top": "T", "bottom": "B", "one_piece": "D", "outer": "O", "shoes": "S",
               "accessory": "A"}
REASON_MAX = 160
_CODE_RE = re.compile(r"^[TBDOSA][0-9]{1,2}$")
# A reason that strays into the person's body gets replaced (the prompt forbids it too).
BODY_WORDS = re.compile(
    r"\b(slimming|slim(?:s|mer)?\s+(?:you|your)|weight|body|bodies|figure|curv\w*|"
    r"flatter\w*|fat|skinny\s+(?:you|your)|attractiv\w*|sexy|hips?|belly|"
    r"tummy|waistline|bust|physique|height|short\s+(?:people|person)|tall\s+(?:people|person))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CandidateTable:
    """Code -> candidate, built from a RulesResult. Codes are stable within one call."""

    by_code: dict[str, Candidate]

    @classmethod
    def from_result(cls, result: RulesResult) -> CandidateTable:
        by_code: dict[str, Candidate] = {}
        for slot, prefix in SLOT_PREFIX.items():
            for n, cand in enumerate(result.candidates.get(slot, []), start=1):
                by_code[f"{prefix}{n}"] = cand
        return cls(by_code)

    @property
    def codes(self) -> list[str]:
        return list(self.by_code)

    def code_for(self, item_id) -> str | None:
        for code, c in self.by_code.items():
            if c.item.id == item_id:
                return code
        return None

    def lookup(self, code: Any) -> Candidate | None:
        if not isinstance(code, str):
            return None
        code = code.strip().upper()
        if not _CODE_RE.match(code):
            return None
        return self.by_code.get(code)


def _show(value: Any) -> str:
    """A hostile value quoted back into the retry prompt: short and inert."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def clean_reason(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in value)
    text = " ".join(text.split())
    if BODY_WORDS.search(text):
        return ""
    if len(text) > REASON_MAX:
        text = text[: REASON_MAX - 1].rstrip() + "…"
    return text


@dataclass
class Validation:
    outfits: list[Outfit]
    errors: list[str]


def validate_response(
    raw: Any,
    table: CandidateTable,
    result: RulesResult,
    *,
    avoid: Iterable[frozenset] = (),
    n: int = 3,
    default_reason: str = "Picked from your closet for today.",
) -> Validation:
    errors: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("outfits"), list):
        return Validation([], ["the answer must be an object with an 'outfits' list"])
    avoid_set = set(avoid)
    valid: list[Outfit] = []
    for idx, entry in enumerate(raw["outfits"], start=1):
        if len(valid) >= n:
            break
        label = f"outfit {idx}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object with 'items' and 'reason'")
            continue
        codes = entry.get("items")
        if not isinstance(codes, list) or not codes:
            errors.append(f"{label}: 'items' must be a non-empty list of codes")
            continue
        items = []
        bad = []
        for code in codes:
            cand = table.lookup(code)
            if cand is None:
                bad.append(_show(code))
            else:
                items.append(cand.item)
        if bad:
            errors.append(f"{label}: not in the candidate table: {', '.join(bad)}")
            continue
        problems = structure_errors(items, result.requirements)
        if problems:
            errors.append(f"{label}: " + "; ".join(problems))
            continue
        key = frozenset(i.id for i in items)
        if any(key == o.key for o in valid):
            errors.append(f"{label}: duplicates an earlier outfit in this answer")
            continue
        if key in avoid_set:
            errors.append(f"{label}: was already suggested today; choose a different combination")
            continue
        reason = clean_reason(entry.get("reason")) or default_reason
        valid.append(Outfit(sort_items(items), reason, "llm"))
    if len(valid) < n and not errors:
        errors.append(f"only {len(valid)} outfit(s) returned; {n} are needed")
    return Validation(valid, errors)
