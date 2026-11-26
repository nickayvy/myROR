"""One real outfit-composer call, for the admin to run once an Anthropic key exists.

    cd server && uv run python -m scripts.smoke_outfits            # ~$0.01 on Sonnet 5.5
    cd server && uv run python -m scripts.smoke_outfits --temp-c 4 --rain --formality 3

What it does:
1. Inserts a synthetic 14-item closet into `items`, every row named "[smoke] ..." with a
   random suffix, so it can never be confused with real clothes.
2. Runs the rules on those items only (the real closet is not read), then one composer
   call through ClaudeLLM with the real spend cap, validates the answer, and prints the
   outfits, the token usage and the recorded cost from api_spend.
3. Deletes the smoke items in a finally block. Nothing is written to `outfits`, no
   collage is stored and nothing is broadcast. The api_spend row stays: it is real spend.

Reads the repo-root .env (ANTHROPIC_API_KEY, DATABASE_URL, SPEND_LEDGER, LLM_OUTFIT_MODEL).
ANTHROPIC_BASE_URL from the shell is ignored (ClaudeLLM pins the base URL).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import UTC, datetime

from db import Database
from mirror_api.config import Settings
from mirror_api.contracts import DayContext, WeatherSnapshot
from mirror_api.spend import SpendLedger
from providers.llm import ClaudeLLM
from recommender import composer
from recommender.closet import load_closet
from recommender.rules import RulesConfig, apply_rules

CLOSET = [
    # name, category, subcategory, colour, pattern, material, formality, season
    ("white tee", "top", "t_shirt", "white", "solid", "cotton", 2, []),
    ("navy oxford shirt", "top", "shirt", "navy", "solid", "cotton", 3, []),
    ("grey wool sweater", "top", "sweater", "grey", "solid", "wool", 3, ["fall", "winter"]),
    ("striped long-sleeve tee", "top", "long_sleeve_tee", "navy", "striped", "cotton", 2, []),
    ("dark jeans", "bottom", "jeans", "navy", "solid", "denim", 2, []),
    ("khaki chinos", "bottom", "chinos", "beige", "solid", "cotton", 3, []),
    ("black skirt", "bottom", "skirt", "black", "solid", "polyester", 3, []),
    ("black knit dress", "one_piece", "dress", "black", "solid", "knit", 3, []),
    ("wool coat", "outerwear", "coat", "charcoal", "solid", "wool", 4, ["fall", "winter"]),
    ("rain jacket", "outerwear", "raincoat", "olive", "solid", "nylon", 2, []),
    ("white sneakers", "shoes", "sneakers", "white", "solid", "leather", 2, []),
    ("brown boots", "shoes", "boots", "brown", "solid", "leather", 3, []),
    ("suede loafers", "shoes", "loafers", "tan", "solid", "suede", 3, []),
    ("grey scarf", "accessory", "scarf", "grey", "solid", "wool", 3, ["fall", "winter"]),
]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--temp-c", type=float, default=9.0)
    p.add_argument("--rain", action="store_true")
    p.add_argument("--formality", type=int, default=2, choices=range(1, 6))
    p.add_argument("--model", default=None, help="default: LLM_OUTFIT_MODEL")
    args = p.parse_args()

    s = Settings()
    if not s.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set in .env", file=sys.stderr)
        return 2
    if not s.database_url:
        print("DATABASE_URL is required (items rows and the api_spend record)", file=sys.stderr)
        return 2
    db = Database(s.database_url, max_size=1)
    await db.open()
    spend = SpendLedger.from_settings(db, s)
    llm = ClaudeLLM(api_key=s.anthropic_api_key, spend=spend)
    tag = secrets.token_hex(3)
    ids = []
    started = datetime.now(UTC)
    try:
        before = await spend.status()
        print(f"ledger={before.ledger} spent=${before.spent_usd:.4f} of ${before.cap_usd:.2f}"
              f" mode={before.mode}")
        if before.mode == "blocked":
            print("spend cap reached; not calling", file=sys.stderr)
            return 1
        for name, cat, sub, col, pat, mat, f, season in CLOSET:
            ids.append(await db.fetchval(
                "INSERT INTO items (name, category, subcategory, primary_color, pattern,"
                " material_guess, formality, season) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING id",
                (f"[smoke {tag}] {name}", cat, sub, col, pat, mat, f, season)))
        closet = await load_closet(db, ids)
        weather = WeatherSnapshot(
            fetched_at=datetime.now(UTC), temp_c=args.temp_c, feels_like_c=args.temp_c,
            condition="rain" if args.rain else "cloudy", precip_probability=0.8 if args.rain
            else 0.1, summary="Rain on and off" if args.rain else None, display_unit=s.temp_unit)
        ctx = DayContext(date=datetime.now(s.tz).date(), weather=weather,
                         min_formality=args.formality)
        cfg = RulesConfig(temp_unit=s.temp_unit)
        result = apply_rules(closet, ctx, cfg)
        prompt = composer.build_prompt(
            result, composer.CandidateTable.from_result(result), cfg,
            profile_text="You like simple, classic outfits in navy, grey and white.",
            fit_notes="Prefers a relaxed fit in tops.")
        print(f"prompt ~{composer.estimate_tokens(prompt + composer.SYSTEM)} tokens (estimate)")
        outcome = await composer.compose(
            llm, model=args.model or s.llm_outfit_model, result=result, cfg=cfg,
            profile_text="You like simple, classic outfits in navy, grey and white.",
            fit_notes="Prefers a relaxed fit in tops.")
        print(f"\nsource={outcome.source} llm_calls={outcome.llm_calls} "
              f"llm_outfits={outcome.llm_outfits} fallback={outcome.fallback_reason}")
        for e in outcome.errors:
            print(f"  validation: {e}")
        for n, o in enumerate(outcome.outfits, 1):
            print(f"\n{n}. [{o.source}] {o.reasoning}")
            for it in o.items:
                print(f"   - {it.slot:9} {it.name.split('] ', 1)[-1]}")
        rows = await db.fetch(
            "SELECT model, purpose, units, est_usd FROM api_spend WHERE at >= %s"
            " AND purpose = %s ORDER BY at", (started, composer.PURPOSE))
        total = sum(float(r["est_usd"]) for r in rows)
        for r in rows:
            u = r["units"]
            print(f"\nrecorded: {r['model']} in={u.get('input_tokens')} "
                  f"out={u.get('output_tokens')} stop={u.get('stop_reason')} "
                  f"${float(r['est_usd']):.6f}")
        print(f"total recorded cost: ${total:.6f}")
        return 0
    finally:
        if ids:
            n = await db.execute("DELETE FROM items WHERE id = ANY(%s)", (ids,))
            print(f"cleaned up {n} smoke item(s)")
        await llm.close()
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
