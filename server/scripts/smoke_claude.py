"""One real, tiny Claude call through ClaudeLLM, for the admin to run once a key exists.

    cd server && uv run python -m scripts.smoke_claude            # Haiku 5.5, ~$0.0002
    cd server && uv run python -m scripts.smoke_claude --model claude-sonnet-5-5

Reads the repo-root .env (ANTHROPIC_API_KEY, DATABASE_URL, SPEND_LEDGER). The call
goes through the spend cap and is recorded in api_spend like any other, so a
database is required. ANTHROPIC_BASE_URL from the shell is ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from db import Database
from mirror_api.config import Settings
from mirror_api.spend import SpendLedger
from providers.llm import ClaudeLLM, LLMError, image_block, text_block

SCHEMA = {
    "type": "object",
    "properties": {
        "color": {"type": "string", "enum": ["red", "green", "blue", "other"]},
        "shape": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["color", "shape", "confidence"],
}


def red_square_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=None, help="default: LLM_TAG_MODEL")
    p.add_argument("--effort", default="low")
    args = p.parse_args()

    s = Settings()
    if not s.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set in .env", file=sys.stderr)
        return 2
    if not s.database_url:
        print("DATABASE_URL is required so the call is recorded in api_spend", file=sys.stderr)
        return 2
    db = Database(s.database_url, max_size=1)
    await db.open()
    spend = SpendLedger.from_settings(db, s)
    llm = ClaudeLLM(api_key=s.anthropic_api_key, spend=spend)
    try:
        before = await spend.status()
        print(f"ledger={before.ledger} spent=${before.spent_usd:.4f} of ${before.cap_usd:.2f}")
        result = await llm.complete_structured(
            model=args.model or s.llm_tag_model,
            system="You describe simple images. Answer only with the requested JSON.",
            content=[image_block(red_square_png(), "image/png"),
                     text_block("What colour and shape is this?")],
            json_schema=SCHEMA,
            purpose="smoke_test",
            effort=args.effort,
            max_tokens=1024,
        )
        after = await spend.status()
        print("result:", result)
        print(f"cost: ${after.spent_usd - before.spent_usd:.6f}  mode={after.mode}")
        return 0
    except LLMError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        await llm.close()
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
