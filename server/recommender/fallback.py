"""Rules-only composer: builds outfits deterministically from the scored candidates.

Used when the LLM is blocked by the spend cap, refuses, is cut off, is unreachable, or
fails validation twice, and to top up an LLM answer that had fewer than 3 valid outfits.
Every item it returns comes from RulesResult.candidates, so it cannot invent items.
"""

from __future__ import annotations

from collections.abc import Iterable

from mirror_api import taxonomy as tx
from recommender.outfit import Outfit, sort_items, structure_errors
from recommender.rules import Candidate, RulesConfig, RulesResult

_PLAIN_PATTERNS = {"solid", "textured", None}


def _harmony(a: Candidate, b: Candidate) -> float:
    pa, pb = a.item, b.item
    s = 0.0
    loud_a = pa.primary_color not in tx.NEUTRAL_COLORS
    loud_b = pb.primary_color not in tx.NEUTRAL_COLORS
    if loud_a and loud_b and pa.primary_color != pb.primary_color:
        s -= 0.5
    if pa.pattern not in _PLAIN_PATTERNS and pb.pattern not in _PLAIN_PATTERNS:
        s -= 0.6
    return s


def _nth(cands: list[Candidate], n: int, exclude: set) -> Candidate | None:
    pool = [c for c in cands if c.item.id not in exclude]
    if not pool:
        return None
    return pool[n % min(len(pool), 2)]


def _reason(core: tuple[Candidate, ...], extras: list[Candidate], result: RulesResult,
            cfg: RulesConfig) -> str:
    names = " with ".join(c.item.name for c in core)
    wf = result.weather
    bits = []
    if wf.known and wf.outer_needed:
        bits.append(f"layered up for {cfg.display_temp(wf.cold_ref_c or 0)}")
    elif wf.known and wf.hot:
        bits.append(f"kept light for {cfg.display_temp(wf.hot_ref_c or 0)}")
    if wf.wet and any(c.item.rain_layer or c.item.subcategory in tx.RAIN_FRIENDLY_SHOES
                      for c in extras):
        bits.append("ready for rain")
    if result.requirements.min_formality >= 4:
        bits.append("dressed up for today's plans")
    text = names[0].upper() + names[1:]
    if bits:
        text += ", " + " and ".join(bits)
    return (text + ".")[:160]


def compose_rules_only(
    result: RulesResult,
    cfg: RulesConfig | None = None,
    *,
    n: int = 3,
    avoid: Iterable[frozenset] = (),
) -> list[Outfit]:
    """Up to `n` valid outfits, best first, skipping item sets in `avoid` when possible."""
    cfg = cfg or RulesConfig()
    if not result.can_form:
        return []
    c = result.candidates
    req = result.requirements
    wf = result.weather
    tops = c.get("top", [])
    mids = [t for t in tops if t.item.layer_role == "mid"]

    cores: list[tuple[float, tuple[Candidate, ...]]] = []
    for t in tops[:6]:
        for b in c.get("bottom", [])[:5]:
            cores.append((t.score + b.score + _harmony(t, b), (t, b)))
    for op in c.get("one_piece", [])[:4]:
        cores.append((op.score * 2, (op,)))
    cores.sort(key=lambda x: (-x[0], [str(k.item.id) for k in x[1]]))

    avoid_set = set(avoid)
    outfits: list[Outfit] = []
    used: dict = {}

    def build(core: tuple[Candidate, ...], k: int) -> Outfit | None:
        chosen = list(core)
        ids = {x.item.id for x in chosen}
        extras: list[Candidate] = []
        has_base = any(x.item.layer_role == "base" for x in core) or any(
            x.item.slot == "one_piece" for x in core)
        has_mid = any(x.item.layer_role == "mid" for x in core)
        if wf.warm_layer and has_base and not has_mid and mids:
            m = _nth(mids, k, ids)
            if m:
                extras.append(m)
        if req.outer_required or (wf.wet and any(o.item.rain_layer for o in c.get("outer", []))):
            outer_pool = c.get("outer", [])
            if wf.wet:
                outer_pool = sorted(outer_pool, key=lambda o: (not o.item.rain_layer, -o.score))
            o = _nth(outer_pool, k, ids)
            if o:
                extras.append(o)
        if req.shoes_required:
            s = _nth(c.get("shoes", []), k, ids)
            if s:
                extras.append(s)
        if wf.outer_needed:
            cold_acc = [a for a in c.get("accessory", [])
                        if a.item.subcategory in tx.COLD_ACCESSORIES]
            if cold_acc:
                extras.append(cold_acc[0])
        items = [x.item for x in chosen + extras]
        if structure_errors(items, req):
            return None
        return Outfit(sort_items(items), _reason(core, extras, result, cfg), "rules")

    def pick(allow_avoided: bool) -> None:
        while len(outfits) < n:
            best = None
            for score, core in cores:
                penalty = sum(used.get(x.item.id, 0) for x in core) * 1.0
                adj = score - penalty
                if best is not None and adj <= best[0]:
                    continue
                o = build(core, len(outfits))
                if o is None or any(o.key == p.key for p in outfits):
                    continue
                if not allow_avoided and o.key in avoid_set:
                    continue
                best = (adj, o)
            if best is None:
                return
            outfits.append(best[1])
            for it in best[1].items:
                used[it.id] = used.get(it.id, 0) + 1

    pick(allow_avoided=False)
    if not outfits:  # the mirror must still get an outfit, even a repeat
        pick(allow_avoided=True)
    return outfits
