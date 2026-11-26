"""Prices for every paid call, in one table. USD.

Claude prices are per million tokens, from the `claude-api` skill (pricing table
cached 2026-11-03). Re-check https://platform.claude.com/docs/en/about-claude/pricing
before relying on them for a budget decision.

Cache rules (same source): reads 0.1x input (0.05x on Sonnet 5.5 and Opus 5.5),
5-minute writes 1.25x, 1-hour writes 2x. Thinking tokens bill as output tokens and
are already included in usage.output_tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

log = logging.getLogger(__name__)

MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal
    # Haiku 5.5 has a second rate card for prompts over 100K tokens.
    long_threshold: int | None = None
    long_input: Decimal | None = None
    long_output: Decimal | None = None


def _p(inp: str, out: str, read_mult: str = "0.1", **kw) -> TokenPrice:
    i, o = Decimal(inp), Decimal(out)
    return TokenPrice(i, o, i * Decimal(read_mult), i * Decimal("1.25"), **kw)


CLAUDE: dict[str, TokenPrice] = {
    "claude-haiku-5-5": _p(
        "0.10", "0.50", long_threshold=100_000, long_input=Decimal("0.50"),
        long_output=Decimal("2.50"),
    ),
    "claude-sonnet-5-5": _p("2.00", "10.00", read_mult="0.05"),
    "claude-opus-5-5": _p("4.00", "20.00", read_mult="0.05"),
    "claude-sonnet-5": _p("2.00", "10.00"),
    "claude-opus-5": _p("5.00", "25.00"),
}
# Unknown models are charged at the most expensive known rate, so the cap errs safe.
_FALLBACK = max(CLAUDE.values(), key=lambda p: p.output)

# fal, per request. [UNTESTED] against real invoices; see HANDOFF section 10/15.
FAL_PER_REQUEST: dict[str, Decimal] = {
    "fal-ai/sam-3/image": Decimal("0.005"),
    "fal-ai/sam-3-1/image": Decimal("0.01"),
    # BiRefNet is listed as "$0 per compute seconds"; real price unconfirmed. Charge a
    # placeholder so it still shows up in the ledger.
    "fal-ai/birefnet": Decimal("0.002"),
}


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def claude_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Decimal:
    price = CLAUDE.get(model)
    if price is None:
        log.warning("no price for model %s; charging the highest known rate", model)
        price = _FALLBACK
    prompt = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    inp, out = price.input, price.output
    read, write = price.cache_read, price.cache_write_5m
    if price.long_threshold is not None and prompt > price.long_threshold:
        assert price.long_input is not None and price.long_output is not None
        scale = price.long_input / price.input
        inp, out = price.long_input, price.long_output
        read, write = read * scale, write * scale
    total = (
        input_tokens * inp
        + output_tokens * out
        + cache_read_input_tokens * read
        + cache_creation_input_tokens * write
    ) / MTOK
    return _q(total)


def fal_cost(endpoint: str, requests: int = 1) -> Decimal:
    price = FAL_PER_REQUEST.get(endpoint)
    if price is None:
        log.warning("no price for fal endpoint %s; charging $0.05/request", endpoint)
        price = Decimal("0.05")
    return _q(price * requests)
