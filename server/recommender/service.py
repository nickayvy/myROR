"""Orchestration: context -> rules -> composer -> validator -> store -> collage -> notify.

Entry points (all take the process Services):
    await precompute(services, day)      # morning job; skips if nothing changed
    await ensure_fresh(services, day)    # presence sensor; rebuilds only on a changed fingerprint
    await reroll(services, day)          # "show me something else"
Spend modes (HANDOFF section 15): normal -> LLM; degraded (>= 80%) -> rerolls reuse stored
outfits, no LLM; blocked -> rules only. Any LLM failure -> rules. The mirror gets an
outfit whenever the closet can form one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from mirror_api.contracts import DayContext
from mirror_api.routers.settings import get_setting
from recommender import store
from recommender.closet import ClosetItem, load_closet
from recommender.collage import build_collage
from recommender.composer import compose
from recommender.outfit import Outfit
from recommender.rules import SETTINGS_KEY, RulesConfig, RulesResult, apply_rules
from recommender.style import StyleProfile, current_profile

log = logging.getLogger(__name__)

CONTEXT_TIMEOUT_S = 15.0
FINGERPRINT_VERSION = 1
AVOID_MAX = 9


def today_for(settings) -> date:
    return datetime.now(settings.tz).date()


# ---- per-process state (locks, last build status) -------------------------------------------


@dataclass
class _State:
    locks: dict[date, asyncio.Lock] = field(default_factory=dict)
    status: dict[date, dict[str, Any]] = field(default_factory=dict)


def _state(services) -> _State:
    """Lives on the Services field `recommender` (created on first use)."""
    st = getattr(services, "recommender", None)
    if st is None:
        st = _State()
        services.recommender = st
    return st


def _lock(services, day: date) -> asyncio.Lock:
    locks = _state(services).locks
    if day not in locks:
        locks[day] = asyncio.Lock()
    return locks[day]


def last_status(services, day: date) -> dict[str, Any] | None:
    return _state(services).status.get(day)


# ---- inputs ------------------------------------------------------------------------------------


async def get_day_context(services, day: date) -> DayContext:
    """integrations.context.get_day_context, imported lazily. Missing, broken, slow or
    wrong-typed -> a DayContext with no weather and no events."""
    try:
        mod = importlib.import_module("integrations.context")
        fn = mod.get_day_context
    except Exception as e:  # noqa: BLE001 - absent or half-written module
        log.info("integrations.context unavailable (%s); no weather or events", e)
        return DayContext(date=day)
    try:
        ctx = await asyncio.wait_for(fn(services, day), CONTEXT_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log.warning("get_day_context failed (%s: %s); no weather or events", type(e).__name__, e)
        return DayContext(date=day)
    if not isinstance(ctx, DayContext):
        log.warning("get_day_context returned %s, not DayContext", type(ctx).__name__)
        return DayContext(date=day)
    return ctx


async def load_config(services) -> tuple[RulesConfig, list[str]]:
    s = services.settings
    base = {"temp_unit": s.temp_unit,
            "southern_hemisphere": bool(s.weather_lat is not None and s.weather_lat < 0)}
    try:
        overrides = await get_setting(services.db, SETTINGS_KEY, None)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read %s: %s", SETTINGS_KEY, e)
        overrides = None
    cfg, warnings = RulesConfig.from_overrides(overrides, **base)
    for w in warnings:
        log.warning("rules config: %s", w)
    return cfg, warnings


def fingerprint(ctx: DayContext, closet: list[ClosetItem], cfg: RulesConfig,
                result: RulesResult, profile: StyleProfile | None) -> tuple[str, dict[str, Any]]:
    """What a rebuild depends on: weather band, formality, clean closet, profile, rules."""
    clean = sorted(f"{i.id}:{i.updated_at.isoformat() if i.updated_at else ''}"
                   for i in closet if i.state == "clean")
    cfg_json = json.dumps(dataclasses.asdict(cfg), sort_keys=True, default=str)
    parts = {
        "v": FINGERPRINT_VERSION,
        "date": ctx.date.isoformat(),
        "weather": result.weather.band(),
        "min_formality": ctx.min_formality,
        "closet": hashlib.sha256("\n".join(clean).encode()).hexdigest()[:16],
        "closet_clean": len(clean),
        "profile": profile.version if profile else None,
        "rules": hashlib.sha256(cfg_json.encode()).hexdigest()[:12],
    }
    fp = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]
    return fp, parts


@dataclass
class Inputs:
    ctx: DayContext
    closet: list[ClosetItem]
    cfg: RulesConfig
    profile: StyleProfile | None
    result: RulesResult
    fingerprint: str
    fp_parts: dict[str, Any]


async def gather(services, day: date, *, ctx: DayContext | None = None) -> Inputs:
    ctx = ctx or await get_day_context(services, day)
    closet = await load_closet(services.db)
    cfg, _ = await load_config(services)
    profile = await current_profile(services.db)
    result = apply_rules(closet, ctx, cfg)
    fp, parts = fingerprint(ctx, closet, cfg, result, profile)
    return Inputs(ctx, closet, cfg, profile, result, fp, parts)


# ---- build -------------------------------------------------------------------------------------


@dataclass
class BuildResult:
    outfits: list[store.StoredOutfit]
    built: bool
    trigger: str
    message: str | None = None
    notes: list[str] = field(default_factory=list)
    source: str | None = None
    fallback_reason: str | None = None
    llm_calls: int = 0


async def _notify(services, day: date, ids: list[UUID], source: str | None) -> None:
    await _invalidate(services, day)
    await services.hub.broadcast("outfits.updated", {
        "date": day.isoformat(), "outfit_ids": [str(i) for i in ids], "source": source})


async def _invalidate(services, day: date) -> None:
    """integrations.today.invalidate_outfits, if the integrations side provides it."""
    try:
        mod = importlib.import_module("integrations.today")
        fn = mod.invalidate_outfits
    except Exception:  # noqa: BLE001 - not written yet
        return
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
        by_name = {"services": services, "for_date": day, "date": day, "day": day}
        if params and all(p.name in by_name for p in params):
            out = fn(**{p.name: by_name[p.name] for p in params})
        else:
            out = fn(*((services, day)[2 - min(len(params), 2):]))
        if inspect.isawaitable(out):
            await out
    except Exception as e:  # noqa: BLE001 - a cache miss is harmless
        log.warning("invalidate_outfits failed: %s", e)


async def _collages(services, day: date, ids: list[UUID], outfits: list[Outfit]) -> None:
    if services.storage is None:
        return
    for oid, o in zip(ids, outfits, strict=True):
        try:
            key = await build_collage(services.storage, day, oid, o.items)
            await store.set_collage(services.db, oid, key)
        except Exception as e:  # noqa: BLE001 - an outfit without a collage still shows
            log.warning("collage for %s failed: %s", oid, e)


async def _spend_mode(services) -> str:
    try:
        return (await services.spend.status()).mode
    except Exception as e:  # noqa: BLE001 - unknown spend: do not risk the cap
        log.warning("spend status unavailable (%s); rules only", e)
        return "blocked"


async def build(
    services, day: date, *, trigger: str, inputs: Inputs | None = None,
    avoid: list[frozenset] | None = None, use_llm: bool | None = None,
    job_id: UUID | None = None,
) -> BuildResult:
    inputs = inputs or await gather(services, day)
    result = inputs.result
    mode = await _spend_mode(services)
    if use_llm is None:
        use_llm = mode != "blocked"
    use_llm = use_llm and getattr(services.llm, "configured", False)
    profile = inputs.profile
    outcome = await compose(
        services.llm, model=services.settings.llm_outfit_model, result=result, cfg=inputs.cfg,
        profile_text=profile.text if profile else None,
        fit_notes=profile.fit_notes if profile else None,
        avoid=avoid or [], use_llm=use_llm, job_id=job_id,
    )
    context = {
        "fingerprint": inputs.fingerprint,
        "fingerprint_parts": inputs.fp_parts,
        "trigger": trigger,
        "spend_mode": mode,
        "llm_calls": outcome.llm_calls,
        "llm_outfits": outcome.llm_outfits,
        "fallback_reason": outcome.fallback_reason,
        "notes": result.notes,
        "requirements": dataclasses.asdict(result.requirements),
        "built_at": datetime.now(UTC).isoformat(),
    }
    ids = await store.replace_current(services.db, day, outcome.outfits, context)
    message = None if outcome.outfits else (result.why_not or "No outfit could be formed.")
    _state(services).status[day] = {
        "fingerprint": inputs.fingerprint, "message": message, "notes": result.notes,
        "at": datetime.now(UTC).isoformat(),
    }
    await _collages(services, day, ids, outcome.outfits)
    await _notify(services, day, ids, outcome.source if outcome.outfits else None)
    stored = await store.current_outfits(services.db, day)
    log.info("outfits for %s (%s): %d stored, source=%s, llm_calls=%d, fallback=%s", day,
             trigger, len(stored), outcome.source, outcome.llm_calls, outcome.fallback_reason)
    return BuildResult(stored, True, trigger, message, result.notes,
                       outcome.source if outcome.outfits else None, outcome.fallback_reason,
                       outcome.llm_calls)


async def _refresh(services, day: date, trigger: str, job_id: UUID | None = None,
                   ctx: DayContext | None = None) -> BuildResult:
    async with _lock(services, day):
        inputs = await gather(services, day, ctx=ctx)
        current = await store.current_outfits(services.db, day)
        day_rows = await store.day_outfits(services.db, day) if current else []
        if any(o.status == "worn" for o in day_rows):
            return BuildResult(current, False, trigger, notes=inputs.result.notes)
        if current and current[0].fingerprint == inputs.fingerprint:
            return BuildResult(current, False, trigger, notes=inputs.result.notes)
        st = last_status(services, day)
        if not current and st and st["fingerprint"] == inputs.fingerprint:
            return BuildResult([], False, trigger, st["message"], inputs.result.notes)
        return await build(services, day, trigger=trigger, inputs=inputs, job_id=job_id)


async def precompute(services, day: date, *, job_id: UUID | None = None) -> BuildResult:
    await store.close_out_days(services.db, day)
    return await _refresh(services, day, "precompute", job_id)


async def ensure_fresh(services, for_date: date) -> bool:
    """For the presence sensor: rebuild today's outfits only if the context fingerprint
    (weather band, formality, clean closet, profile, rules) changed, or none exist yet.
    Returns True when it rebuilt. Not run once something was marked worn today."""
    res = await _refresh(services, for_date, "refresh")
    return res.built


async def reroll(services, day: date) -> BuildResult:
    async with _lock(services, day):
        db = services.db
        current = await store.current_outfits(db, day)
        await store.mark_shown(db, [o.id for o in current])
        mode = await _spend_mode(services)
        inputs = await gather(services, day)
        day_rows = await store.day_outfits(db, day)
        avoid = list(dict.fromkeys(o.key for o in day_rows))[:AVOID_MAX]
        if mode == "degraded":
            reused = await _reuse_stored(services, day, current, day_rows, inputs)
            if reused is not None:
                return reused
        return await build(services, day, trigger="reroll", inputs=inputs, avoid=avoid,
                           use_llm=(mode == "normal"))


async def _reuse_stored(services, day, current, day_rows, inputs) -> BuildResult | None:
    clean = {i.id for i in inputs.closet if i.state == "clean"}
    current_keys = {o.key for o in current}
    disliked = {r["outfit_id"] for r in await services.db.fetch(
        "SELECT DISTINCT ON (outfit_id) outfit_id, value FROM feedback"
        " WHERE outfit_id = ANY(%s) ORDER BY outfit_id, at DESC",
        ([o.id for o in day_rows],)) if r["value"] < 0}
    seen: set = set()
    picks = []
    # least recently shown first, so repeated rerolls cycle through everything stored
    for o in sorted(day_rows, key=lambda o: (o.shown_at is not None, o.shown_at or o.created_at)):
        if (o.superseded_at is None or o.status in ("worn", "skipped") or o.id in disliked
                or not o.key <= clean or o.key in current_keys or o.key in seen):
            continue
        seen.add(o.key)
        picks.append(o.id)
        if len(picks) == 3:
            break
    if not picks:
        return None
    await store.reactivate(services.db, day, picks, inputs.fingerprint)
    await _notify(services, day, picks, "stored")
    stored = await store.current_outfits(services.db, day)
    return BuildResult(stored, False, "reroll", notes=inputs.result.notes, source="stored",
                       fallback_reason="spend degraded: reused stored outfits")
