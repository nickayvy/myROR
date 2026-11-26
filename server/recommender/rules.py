"""Stage 1: deterministic filter and scorer (HANDOFF section 10).

    result = apply_rules(closet, ctx, config)      # pure: same inputs, same output
    result.candidates["top"]                       # [Candidate], best first
    result.requirements.outer_required
    result.can_form, result.why_not

Hard exclusions: dirty, retired, unknown category. Soft exclusions (off-season, below
the day's minimum formality, rain-unsuitable shoes) are lifted for a slot only when that
slot would otherwise be empty, and the result says so in `notes`. Everything else is a
score. Temperatures are Celsius throughout; only reason text uses the display unit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from mirror_api import taxonomy as tx
from mirror_api.contracts import DayContext, TempUnit, c_to_f
from recommender.closet import ClosetItem

SETTINGS_KEY = "recommender.rules"  # jsonb overrides in the `settings` table

CORE_SLOTS = ("top", "bottom", "one_piece", "shoes")


@dataclass(frozen=True)
class RulesConfig:
    """Starting thresholds (HANDOFF P9). Override any field through the settings table:
    PUT /api/settings {"recommender.rules": {"outer_required_below_c": 8}}."""

    outer_required_below_c: float = 10.0
    hot_above_c: float = 25.0
    cold_open_below_c: float = 15.0   # shorts, sandals, tank tops penalised below this
    warm_layer_below_c: float = 14.0  # sweaters and other mid layers favoured below this
    summer_above_c: float = 22.0      # weather-implied season, for the off-season override
    winter_below_c: float = 7.0
    # Down-rank an item worn within this many days, per slot (0 = never).
    recent_days: Mapping[str, int] = field(default_factory=lambda: {
        "top": 4, "bottom": 2, "one_piece": 7, "outer": 0, "shoes": 1, "accessory": 0,
    })
    # Candidates kept per slot (bounds the composer's prompt).
    max_candidates: Mapping[str, int] = field(default_factory=lambda: {
        "top": 12, "bottom": 10, "one_piece": 6, "outer": 6, "shoes": 6, "accessory": 6,
    })
    southern_hemisphere: bool = False
    temp_unit: TempUnit = "F"  # for reason text only

    @classmethod
    def from_overrides(cls, overrides: Any, **base: Any) -> tuple[RulesConfig, list[str]]:
        """Apply a dict of overrides; unknown keys and bad values are skipped and reported."""
        cfg = cls(**base)
        warnings: list[str] = []
        if not overrides:
            return cfg, warnings
        if not isinstance(overrides, dict):
            return cfg, [f"{SETTINGS_KEY} must be an object"]
        changes: dict[str, Any] = {}
        fields = {f.name: f for f in dataclasses.fields(cls)}
        for k, v in overrides.items():
            if k not in fields or k in ("temp_unit",):
                warnings.append(f"unknown rules setting {k!r} ignored")
                continue
            current = getattr(cfg, k)
            if isinstance(current, Mapping):
                if not isinstance(v, dict) or any(
                    s not in tx.SLOTS or not isinstance(n, int) or isinstance(n, bool) or n < 0
                    for s, n in v.items()
                ):
                    warnings.append(f"{k} must map slots to non-negative integers; ignored")
                    continue
                changes[k] = {**current, **v}
            elif isinstance(current, bool):
                if not isinstance(v, bool):
                    warnings.append(f"{k} must be true or false; ignored")
                    continue
                changes[k] = v
            else:
                if isinstance(v, bool) or not isinstance(v, int | float):
                    warnings.append(f"{k} must be a number; ignored")
                    continue
                changes[k] = float(v)
        return dataclasses.replace(cfg, **changes), warnings

    def display_temp(self, celsius: float) -> str:
        if self.temp_unit == "F":
            return f"{round(c_to_f(celsius))}°F"
        return f"{round(celsius)}°C"


# ---- weather -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherFacts:
    known: bool = False
    now_c: float | None = None
    cold_ref_c: float | None = None  # coldest it gets for the rest of today
    hot_ref_c: float | None = None   # warmest
    wet: bool = False
    outer_needed: bool = False
    hot: bool = False
    cold_open: bool = False
    warm_layer: bool = False
    seasons: frozenset[str] = frozenset()  # weather-implied season(s)
    summary: str | None = None

    def band(self) -> dict[str, Any]:
        """Coarse description for the context fingerprint: small temperature changes
        within a 4 °C band do not trigger a rebuild."""
        if not self.known:
            return {"known": False}
        return {
            "cold": math.floor((self.cold_ref_c or 0) / 4),
            "hot": math.floor((self.hot_ref_c or 0) / 4),
            "wet": self.wet,
            "flags": [self.outer_needed, self.hot, self.cold_open, self.warm_layer],
        }


def weather_facts(ctx: DayContext, cfg: RulesConfig) -> WeatherFacts:
    w = ctx.weather
    if w is None:
        return WeatherFacts()
    now = w.feels_like_c if w.feels_like_c is not None else w.temp_c
    hourly = [h.temp_c for h in w.hourly]
    cold = min([now, *hourly])
    hot = max([now, *hourly, *([w.high_c] if w.high_c is not None else [])])
    mid = (cold + hot) / 2
    if mid >= cfg.summer_above_c:
        seasons = frozenset({"summer"})
    elif mid <= cfg.winter_below_c:
        seasons = frozenset({"winter"})
    else:
        seasons = frozenset({"spring", "fall"})
    return WeatherFacts(
        known=True,
        now_c=now,
        cold_ref_c=cold,
        hot_ref_c=hot,
        wet=w.is_wet,
        outer_needed=cold < cfg.outer_required_below_c,
        hot=hot > cfg.hot_above_c,
        cold_open=cold < cfg.cold_open_below_c,
        warm_layer=cold < cfg.warm_layer_below_c,
        seasons=seasons,
        summary=w.summary,
    )


def calendar_season(d: date, southern: bool = False) -> str:
    north = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
             6: "summer", 7: "summer", 8: "summer"}.get(d.month, "fall")
    if not southern:
        return north
    return {"winter": "summer", "summer": "winter", "spring": "fall", "fall": "spring"}[north]


# ---- results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    item: ClosetItem
    score: float
    reasons: tuple[str, ...] = ()
    relaxed: tuple[str, ...] = ()  # soft rules lifted to keep this slot non-empty

    @property
    def slot(self) -> str:
        return self.item.slot or ""


@dataclass(frozen=True)
class Requirements:
    outer_required: bool = False      # cold and a clean outer layer exists
    shoes_required: bool = False      # the closet has clean shoes
    rain_layer_suggested: bool = False
    min_formality: int = 1


@dataclass
class RulesResult:
    for_date: date
    weather: WeatherFacts
    requirements: Requirements
    candidates: dict[str, list[Candidate]]
    excluded: dict[UUID, str]
    notes: list[str]
    can_form: bool
    why_not: str | None = None
    season: str = ""

    def all_candidates(self) -> list[Candidate]:
        return [c for slot in tx.SLOTS for c in self.candidates.get(slot, [])]

    def by_id(self) -> dict[UUID, Candidate]:
        return {c.item.id: c for c in self.all_candidates()}


# ---- the rules ---------------------------------------------------------------------------

_SOFT_ORDER = ("off_season", "rain_shoes", "below_formality")
_SOFT_TEXT = {
    "off_season": "off-season",
    "rain_shoes": "not ideal in rain",
    "below_formality": "below today's formality",
}


def _jitter(d: date, item_id: UUID) -> float:
    h = hashlib.sha256(f"{d.isoformat()}:{item_id}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32 * 0.15


def _score(item: ClosetItem, ctx: DayContext, wf: WeatherFacts, cfg: RulesConfig,
           season: str) -> tuple[float, list[str], list[str]]:
    slot = item.slot or ""
    score = 1.0
    why: list[str] = []
    soft: list[str] = []

    # season (soft). Clearly summer or winter weather overrides the calendar both ways:
    # shorts on a November heatwave, a wool coat on a cold May morning, and no wool
    # trousers at 31 °C in fall. Mild weather leaves the calendar season in charge.
    allowed = set(wf.seasons) if wf.seasons in ({"summer"}, {"winter"}) else {season}
    if not item.all_season and not (set(item.season) & allowed):
        soft.append("off_season")

    # formality
    target = max(ctx.min_formality, 2)
    f = item.eff_formality
    if slot in CORE_SLOTS and f < ctx.min_formality:
        soft.append("below_formality")
    weight = 0.4 if slot in CORE_SLOTS else 0.2
    if f != target:
        score -= weight * abs(f - target)
    if ctx.min_formality >= 4 and f >= ctx.min_formality and slot in CORE_SLOTS:
        why.append("dressy enough for today")

    # temperature
    if wf.known:
        if wf.hot and slot != "outer":
            if item.sleeve in ("short", "none"):
                score += 1.0
                why.append("short sleeves for the heat")
            elif item.sleeve == "long" and slot in ("top", "one_piece"):
                score -= 0.7
            if item.light_fabric:
                score += 0.6
                why.append("light fabric")
            if item.warmth >= 2 and slot != "shoes":
                score -= 1.0
            if item.subcategory in ("shorts", "skirt", "sandals", "dress"):
                score += 0.4
        if wf.hot and slot == "outer" and not (wf.wet and item.rain_layer):
            score -= 1.5
        if wf.cold_open and item.subcategory in tx.OPEN_ITEMS:
            score -= 1.5
        if wf.outer_needed:
            if slot == "outer":
                score += 0.5 * item.warmth
                if item.warmth >= 2:
                    why.append("warm outer layer")
            elif slot in ("top", "bottom", "one_piece") and item.warmth >= 2:
                score += 0.4
            if item.subcategory in tx.COLD_ACCESSORIES:
                score += 0.5
        if wf.warm_layer and item.layer_role == "mid":
            score += 0.5
            why.append("warm layer")
        if not wf.outer_needed and slot == "outer" and item.warmth >= 3:
            score -= 1.0
        if wf.wet:
            if item.rain_layer:
                score += 1.5
                why.append("rain layer")
            if slot == "shoes":
                if item.rain_unsuitable:
                    soft.append("rain_shoes")
                elif item.subcategory in tx.RAIN_FRIENDLY_SHOES:
                    score += 0.8
                    why.append("good in rain")

    # recently worn
    n = cfg.recent_days.get(slot, 0)
    if n and item.last_worn_on is not None:
        days = (ctx.date - item.last_worn_on).days
        if 0 <= days < n:
            score -= 2.0 * (1 - days / n)
            why.append("worn today" if days == 0 else f"worn {days}d ago")

    score += _jitter(ctx.date, item.id)
    return round(score, 4), why, soft


def apply_rules(
    closet: Iterable[ClosetItem], ctx: DayContext, cfg: RulesConfig | None = None
) -> RulesResult:
    cfg = cfg or RulesConfig()
    wf = weather_facts(ctx, cfg)
    season = calendar_season(ctx.date, cfg.southern_hemisphere)
    items = list(closet)
    excluded: dict[UUID, str] = {}
    notes: list[str] = []
    scored: dict[str, list[tuple[ClosetItem, float, list[str], list[str]]]] = {
        s: [] for s in tx.SLOTS
    }
    clean_counts: dict[str, int] = {s: 0 for s in tx.SLOTS}
    dirty_counts: dict[str, int] = {s: 0 for s in tx.SLOTS}

    for item in items:
        slot = item.slot
        if slot is None:
            excluded[item.id] = f"unknown category {item.category!r}"
            continue
        if item.state != "clean":
            excluded[item.id] = item.state
            if item.state == "dirty":
                dirty_counts[slot] += 1
            continue
        clean_counts[slot] += 1
        s, why, soft = _score(item, ctx, wf, cfg, season)
        scored[slot].append((item, s, why, soft))

    def pick(slot: str, lift: frozenset[str]) -> list[Candidate]:
        out = []
        for item, s, why, soft in scored[slot]:
            if set(soft) - lift:
                continue
            relaxed = tuple(x for x in _SOFT_ORDER if x in soft)
            out.append(Candidate(item, round(s - 0.8 * len(relaxed), 4), tuple(why), relaxed))
        out.sort(key=lambda c: (-c.score, str(c.item.id)))
        return out

    candidates: dict[str, list[Candidate]] = {s: pick(s, frozenset()) for s in tx.SLOTS}

    def relax(slot: str) -> None:
        if candidates[slot] or not scored[slot]:
            return
        lift: set[str] = set()
        for rule in _SOFT_ORDER:
            lift.add(rule)
            found = pick(slot, frozenset(lift))
            if found:
                candidates[slot] = found
                lifted = sorted({r for c in found for r in c.relaxed}, key=_SOFT_ORDER.index)
                notes.append(f"No ideal {slot.replace('_', ' ')}; including items that are "
                             + " or ".join(_SOFT_TEXT[r] for r in lifted) + ".")
                return

    has_two_piece = bool(candidates["top"] and candidates["bottom"])
    if not has_two_piece and not candidates["one_piece"]:
        for slot in ("top", "bottom", "one_piece"):
            relax(slot)
    relax("shoes")
    if wf.outer_needed:
        relax("outer")

    # requirements
    shoes_required = clean_counts["shoes"] > 0
    outer_required = wf.outer_needed and bool(candidates["outer"])
    if wf.outer_needed and not candidates["outer"]:
        notes.append(f"It's cold ({cfg.display_temp(wf.cold_ref_c or 0)}) but there is no "
                     "clean outer layer.")
    if wf.wet and not any(c.item.rain_layer for c in candidates["outer"]):
        notes.append("Rain expected and no rain layer is available; take an umbrella.")
    reqs = Requirements(
        outer_required=outer_required,
        shoes_required=shoes_required,
        rain_layer_suggested=wf.wet,
        min_formality=ctx.min_formality,
    )

    # a mid-layer top alone is a valid top, so top+bottom needs any top
    can_form = bool(candidates["top"] and candidates["bottom"]) or bool(candidates["one_piece"])
    why_not = None
    if not can_form:
        why_not = _why_not(clean_counts, dirty_counts, candidates)

    for slot, n in cfg.max_candidates.items():
        if slot in candidates:
            candidates[slot] = candidates[slot][: max(int(n), 1)]
    return RulesResult(
        for_date=ctx.date, weather=wf, requirements=reqs, candidates=candidates,
        excluded=excluded, notes=notes, can_form=can_form, why_not=why_not, season=season,
    )


def _why_not(clean: dict[str, int], dirty: dict[str, int],
             cands: dict[str, list[Candidate]]) -> str:
    def part(slot: str, label: str) -> str:
        d = dirty[slot]
        extra = f" ({d} in the laundry)" if d else ""
        return f"no clean {label}{extra}"

    missing = []
    if not cands["top"]:
        missing.append(part("top", "tops"))
    if not cands["bottom"]:
        missing.append(part("bottom", "bottoms"))
    missing.append(part("one_piece", "dresses or one-pieces"))
    total_clean = sum(clean.values())
    if total_clean == 0:
        return ("Can't suggest an outfit: the closet has no clean items"
                + (f" ({sum(dirty.values())} in the laundry)." if sum(dirty.values()) else
                   ". Add some clothes first."))
    return "Can't suggest an outfit: " + ", ".join(missing) + "."
