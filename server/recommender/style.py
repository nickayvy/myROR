"""Style profile: an append-only, plain-language text the user can edit (HANDOFF section 10).

- `style_profile` rows are versions; the current profile is the highest version.
- Fit notes are only ever the user's own words (U32). Nothing here infers fit, body
  shape or anything else from photos or data, and fit notes are never sent to the LLM
  when writing or updating the profile text.
- The quiz produces the first profile (LLM, with a template fallback).
- The weekly update never rewrites the user's words. The profile text is split at
  LEARNED_MARKER: everything above it is the user's (or the quiz's) text and is copied
  verbatim; only the section below the marker is replaced with fresh observations.
  The new row has source='weekly_update', and a `style.updated` WebSocket event tells
  the phone so it can show "your profile was updated" with the diff. If the user deletes
  the learned section, the next weekly run adds a new one, still clearly labelled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from db import Database
from mirror_api import taxonomy as tx
from mirror_api.spend import SpendCapExceeded
from providers.llm import LLM, LLMError, text_block
from recommender.validator import BODY_WORDS

log = logging.getLogger(__name__)

TEXT_MAX = 4000
FIT_NOTES_MAX = 1000
LEARNED_MARKER = "Learned from your feedback (updated weekly; edit or delete freely):"
WEEKLY_LOOKBACK_DAYS = 28
WEEKLY_MIN_SIGNALS = 3
WEEKLY_MIN_GAP_DAYS = 6
EFFORT = "low"


@dataclass(frozen=True)
class StyleProfile:
    version: int | None
    text: str
    fit_notes: str | None = None
    quiz: dict[str, Any] = field(default_factory=dict)
    source: str = "user"
    updated_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "text": self.text, "fit_notes": self.fit_notes,
            "quiz": self.quiz, "source": self.source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _row(r: dict[str, Any]) -> StyleProfile:
    return StyleProfile(r["version"], r["text"], r["fit_notes"], r["quiz"] or {}, r["source"],
                        r["updated_at"])


async def current_profile(db: Database) -> StyleProfile | None:
    r = await db.fetchrow("SELECT * FROM style_profile ORDER BY version DESC LIMIT 1")
    return _row(r) if r else None


async def profile_history(db: Database, limit: int = 20) -> list[StyleProfile]:
    rows = await db.fetch("SELECT * FROM style_profile ORDER BY version DESC LIMIT %s", (limit,))
    return [_row(r) for r in rows]


async def save_profile(db: Database, *, text: str, fit_notes: str | None, quiz: dict,
                       source: str) -> StyleProfile:
    r = await db.fetchrow(
        "INSERT INTO style_profile (text, fit_notes, quiz, source) VALUES (%s, %s, %s, %s)"
        " RETURNING *",
        (text[:TEXT_MAX], (fit_notes or None) and fit_notes[:FIT_NOTES_MAX], Jsonb(quiz or {}),
         source),
    )
    return _row(r)


def split_learned(text: str) -> tuple[str, str]:
    """(user part, learned part) of a profile text."""
    head, sep, tail = text.partition(LEARNED_MARKER)
    return head.rstrip(), tail.strip() if sep else ""


# ---- quiz -----------------------------------------------------------------------------------

def _opts(values, labels: dict | None = None) -> list[dict[str, str]]:
    return [{"value": v, "label": (labels or {}).get(v, v.replace("_", " "))} for v in values]


QUIZ: list[dict[str, Any]] = [
    {"id": "style_words", "kind": "multi", "max_choices": 3, "required": True,
     "prompt": "Which words describe the style you like? (up to 3)",
     "options": _opts(tx.STYLE_TAGS)},
    {"id": "usual_formality", "kind": "single", "required": True,
     "prompt": "What do you usually wear on a normal weekday?",
     "options": [{"value": str(k), "label": v} for k, v in tx.FORMALITY.items()]},
    {"id": "favourite_colors", "kind": "multi", "max_choices": 5, "required": False,
     "prompt": "Colours you love wearing (up to 5)", "options": _opts(tx.COLORS)},
    {"id": "avoid_colors", "kind": "multi", "max_choices": 5, "required": False,
     "prompt": "Colours you'd rather not wear (up to 5)", "options": _opts(tx.COLORS)},
    {"id": "patterns", "kind": "single", "required": True,
     "prompt": "How do you feel about patterns?",
     "options": _opts(("mostly_solid", "some_pattern", "bold_patterns"),
                      {"mostly_solid": "Mostly solid colours",
                       "some_pattern": "A patterned piece now and then",
                       "bold_patterns": "Love bold patterns"})},
    {"id": "fit_preference", "kind": "single", "required": True,
     "prompt": "How do you like clothes to fit?", "options": _opts(tx.FITS)},
    {"id": "layering", "kind": "single", "required": True,
     "prompt": "Layering?",
     "options": _opts(("minimal", "practical", "love_it"),
                      {"minimal": "As few layers as possible",
                       "practical": "Layers when the weather calls for it",
                       "love_it": "Love a layered look"})},
    {"id": "comfort_vs_polish", "kind": "scale", "min": 1, "max": 5, "required": True,
     "prompt": "Comfort first (1) or polished first (5)?"},
    {"id": "avoid_items", "kind": "text", "max_length": 200, "required": False,
     "prompt": "Anything you never want suggested?"},
    {"id": "fit_notes", "kind": "text", "max_length": 300, "required": False,
     "prompt": "Fit notes, in your own words (for example 'trousers are often too long on "
               "me'). Used as written; never guessed from photos."},
    {"id": "anything_else", "kind": "text", "max_length": 200, "required": False,
     "prompt": "Anything else your stylist should know?"},
]
_QUIZ_BY_ID = {q["id"]: q for q in QUIZ}


class QuizError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_answers(answers: Any) -> dict[str, Any]:
    if not isinstance(answers, dict):
        raise QuizError(["answers must be an object keyed by question id"])
    errors: list[str] = []
    out: dict[str, Any] = {}
    for k in answers:
        if k not in _QUIZ_BY_ID:
            errors.append(f"unknown question {k!r}")
    for q in QUIZ:
        qid, v = q["id"], answers.get(q["id"])
        if v in (None, "", []):
            if q["required"]:
                errors.append(f"{qid} is required")
            continue
        allowed = {o["value"] for o in q.get("options", [])}
        if q["kind"] == "single":
            v = str(v)
            if v not in allowed:
                errors.append(f"{qid}: {v!r} is not an option")
        elif q["kind"] == "multi":
            if not isinstance(v, list) or any(str(x) not in allowed for x in v):
                errors.append(f"{qid}: expected a list of options")
            elif len(v) > q["max_choices"]:
                errors.append(f"{qid}: at most {q['max_choices']} choices")
            else:
                v = list(dict.fromkeys(str(x) for x in v))
        elif q["kind"] == "scale":
            if isinstance(v, bool) or not isinstance(v, int) or not q["min"] <= v <= q["max"]:
                errors.append(f"{qid}: expected an integer {q['min']}-{q['max']}")
        elif q["kind"] == "text":
            if not isinstance(v, str):
                errors.append(f"{qid}: expected text")
            else:
                v = " ".join(v.split())[: q["max_length"]]
        out[qid] = v
    if errors:
        raise QuizError(errors)
    return out


def _words(values) -> str:
    return ", ".join(str(v).replace("_", " ") for v in values)


def template_profile(answers: dict[str, Any]) -> str:
    """Deterministic profile text, used when the LLM is unavailable."""
    parts = [f"Style: {_words(answers.get('style_words', []))}."]
    f = answers.get("usual_formality")
    if f:
        parts.append(f"Usual weekday level: {tx.FORMALITY[int(f)]}.")
    if answers.get("favourite_colors"):
        parts.append(f"Favourite colours: {_words(answers['favourite_colors'])}.")
    if answers.get("avoid_colors"):
        parts.append(f"Avoid: {_words(answers['avoid_colors'])}.")
    pat = {"mostly_solid": "Mostly solid colours.", "some_pattern": "A pattern now and then.",
           "bold_patterns": "Enjoys bold patterns."}.get(answers.get("patterns", ""))
    if pat:
        parts.append(pat)
    if answers.get("fit_preference"):
        parts.append(f"Prefers a {answers['fit_preference']} fit.")
    lay = {"minimal": "Keeps layers to a minimum.", "practical": "Layers when needed.",
           "love_it": "Likes layered looks."}.get(answers.get("layering", ""))
    if lay:
        parts.append(lay)
    s = answers.get("comfort_vs_polish")
    if s:
        parts.append("Comfort comes first." if s <= 2 else
                     "Likes to look polished." if s >= 4 else "Balances comfort and polish.")
    if answers.get("avoid_items"):
        parts.append(f"Never suggest: {answers['avoid_items']}.")
    if answers.get("anything_else"):
        parts.append(f"Also: {answers['anything_else']}.")
    return " ".join(parts)


PROFILE_SYSTEM = """\
You write a short style profile for one person's outfit recommender, from their quiz \
answers. Write 3-6 plain sentences in the second person ("You like..."), covering style, \
colours, patterns, preferred fit and layering, comfort versus polish, and anything to avoid. \
Use only what the answers say; do not guess. Never comment on body, weight, shape or \
attractiveness, and never infer sensitive traits such as health, ethnicity, religion, \
gender identity, sexual orientation or age."""

PROFILE_SCHEMA = {"type": "object", "properties": {"profile": {"type": "string"}},
                  "required": ["profile"], "additionalProperties": False}


def _safe(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    if BODY_WORDS.search(text):
        return ""
    return text[:limit]


async def profile_from_quiz(llm: LLM, model: str, answers: dict[str, Any]) -> tuple[str, bool]:
    """(profile text, whether the LLM wrote it). Fit notes are not sent."""
    shown = {k: v for k, v in answers.items() if k != "fit_notes"}
    lines = []
    for q in QUIZ:
        if q["id"] in shown:
            v = shown[q["id"]]
            if q["id"] == "usual_formality":
                v = tx.FORMALITY[int(v)]
            lines.append(f"- {q['prompt']} {v if isinstance(v, str | int) else _words(v)}")
    try:
        raw = await llm.complete_structured(
            model=model, system=PROFILE_SYSTEM,
            content=[text_block("Quiz answers:\n" + "\n".join(lines))],
            json_schema=PROFILE_SCHEMA, purpose="style_profile_init", effort=EFFORT,
            max_tokens=1500,
        )
        text = _safe(raw.get("profile"), TEXT_MAX)
        if text:
            return text, True
    except (LLMError, SpendCapExceeded) as e:
        log.warning("style profile from quiz: %s; using template", e)
    return template_profile(answers), False


# ---- weekly update ------------------------------------------------------------------------------

WEEKLY_SYSTEM = """\
You maintain the "learned" notes of a style profile for one person's outfit recommender. \
From their recent outfit feedback and wear history, write up to 5 short observations about \
their clothing preferences (combinations, colours, formality, items they reach for or avoid). \
Only state what the data supports; return an empty list if nothing is clear. Do not repeat \
what the profile already says. Never comment on body, weight, shape or attractiveness, and \
never infer sensitive traits such as health, ethnicity, religion, gender identity, sexual \
orientation or age."""

WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {"observations": {"type": "array", "items": {"type": "string"}}},
    "required": ["observations"],
    "additionalProperties": False,
}


async def _weekly_signals(db: Database, since: date) -> tuple[list[str], int]:
    lines: list[str] = []
    fb = await db.fetch(
        "SELECT f.value, f.note, o.for_date, string_agg(i.name || ' (' || i.category || ')',"
        " ' + ' ORDER BY oi.slot) AS items"
        " FROM feedback f JOIN outfits o ON o.id = f.outfit_id"
        " JOIN outfit_items oi ON oi.outfit_id = o.id JOIN items i ON i.id = oi.item_id"
        " WHERE o.for_date >= %s GROUP BY f.id, o.for_date ORDER BY f.at DESC LIMIT 30",
        (since,),
    )
    for r in fb:
        note = f' note: "{" ".join((r["note"] or "").split())[:120]}"' if r["note"] else ""
        lines.append(f"{'liked' if r['value'] > 0 else 'disliked'}: {r['items']}{note}")
    worn = await db.fetch(
        "SELECT i.name, i.category, i.primary_color, count(*) AS n FROM wear_log w"
        " JOIN items i ON i.id = w.item_id WHERE w.worn_on >= %s"
        " GROUP BY i.id ORDER BY n DESC, i.name LIMIT 15",
        (since,),
    )
    for r in worn:
        lines.append(f"worn {r['n']}x: {r['name']} ({r['category']}, {r['primary_color']})")
    skipped = await db.fetchval(
        "SELECT count(*) FROM outfits WHERE for_date >= %s AND status = 'skipped'", (since,))
    if skipped:
        lines.append(f"suggestions shown but not worn: {skipped}")
    return lines, len(fb) + len(worn)


async def weekly_update(services, today: date) -> StyleProfile | None:
    """Propose new learned notes; returns the new version, or None if skipped."""
    db, llm, settings = services.db, services.llm, services.settings
    current = await current_profile(db)
    last = await db.fetchval(
        "SELECT max(updated_at) FROM style_profile WHERE source = 'weekly_update'")
    if last is not None and (today - last.astimezone(settings.tz).date()).days < \
            WEEKLY_MIN_GAP_DAYS:
        return None
    lines, n = await _weekly_signals(db, today - timedelta(days=WEEKLY_LOOKBACK_DAYS))
    if n < WEEKLY_MIN_SIGNALS:
        log.info("weekly style update skipped: %d signals", n)
        return None
    if (await services.spend.status()).mode != "normal":
        log.info("weekly style update skipped: spend mode not normal")
        return None
    user_part, learned = split_learned(current.text if current else "")
    prompt = ("<style_profile>\n" + (user_part or "(none yet)") + "\n</style_profile>\n"
              + ("<previous_learned_notes>\n" + learned + "\n</previous_learned_notes>\n"
                 if learned else "")
              + "Recent activity (last 4 weeks):\n- " + "\n- ".join(lines))
    try:
        raw = await llm.complete_structured(
            model=settings.llm_outfit_model, system=WEEKLY_SYSTEM,
            content=[text_block(prompt)], json_schema=WEEKLY_SCHEMA,
            purpose="style_weekly_update", effort=EFFORT, max_tokens=1500,
        )
    except (LLMError, SpendCapExceeded) as e:
        log.warning("weekly style update failed: %s", e)
        return None
    obs = [o for o in (_safe(x, 200) for x in (raw.get("observations") or [])[:5]) if o]
    if not obs:
        return None
    text = (user_part + "\n\n" if user_part else "") + LEARNED_MARKER + "\n" + "\n".join(
        f"- {o}" for o in obs)
    new = await save_profile(
        db, text=text, fit_notes=current.fit_notes if current else None,
        quiz=current.quiz if current else {}, source="weekly_update",
    )
    await services.hub.broadcast("style.updated", {"version": new.version,
                                                   "source": "weekly_update"})
    return new
