"""Spend cap thresholds, ledgers and the live month boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mirror_api.pricing import claude_cost, fal_cost
from mirror_api.spend import SpendCapExceeded, SpendLedger, mode_for, month_bounds

NY = ZoneInfo("America/New_York")


def ledger(db, kind="dev", now=None, **kw):
    clock = (lambda: now) if now else (lambda: datetime.now(UTC))
    return SpendLedger(db, ledger=kind, live_cap_usd=5.0, dev_cap_usd=10.0, tz=NY,
                       clock=clock, **kw)


@pytest.mark.parametrize(
    ("percent", "mode"),
    [(0, "normal"), (79.99, "normal"), (80, "degraded"), (99.99, "degraded"),
     (100, "blocked"), (150, "blocked")],
)
def test_mode_thresholds(percent, mode):
    assert mode_for(percent) == mode


async def test_dev_ledger_modes_and_check(db):
    sp = ledger(db)
    assert (await sp.check("x")).mode == "normal"
    await sp.record(provider="anthropic", model="m", purpose="x", est_usd=Decimal("7.99"))
    st = await sp.status()
    assert (st.mode, st.spent_usd, st.percent) == ("normal", 7.99, 79.9)
    await sp.record(provider="anthropic", model="m", purpose="x", est_usd=Decimal("0.01"))
    assert (await sp.status()).mode == "degraded"
    assert (await sp.check("x")).mode == "degraded"  # degraded does not block
    await sp.record(provider="anthropic", model="m", purpose="x", est_usd=Decimal("2.00"))
    with pytest.raises(SpendCapExceeded) as ei:
        await sp.check("outfit")
    assert ei.value.status.mode == "blocked" and ei.value.purpose == "outfit"


async def test_ledgers_are_separate(db):
    dev = ledger(db, "dev")
    live = ledger(db, "live")
    await dev.record(provider="p", model="m", purpose="x", est_usd=9)
    assert (await live.status()).spent_usd == 0
    assert (await dev.status()).spent_usd == 9


def test_month_bounds_in_timezone():
    # 03:30 UTC on Nov 1 is still Oct 31 in New York.
    start, end = month_bounds(datetime(2026, 11, 1, 3, 30, tzinfo=UTC), NY)
    assert start == datetime(2026, 10, 1, tzinfo=NY)
    assert end == datetime(2026, 11, 1, tzinfo=NY)
    assert end.astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # EDT


async def test_live_ledger_counts_only_current_local_month(db):
    late_oct = datetime(2026, 11, 1, 3, 30, tzinfo=UTC)  # Oct 31 23:30 local
    early_nov = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)  # Nov 1 00:30 local
    await ledger(db, "live", now=late_oct).record(provider="p", model="m", purpose="x",
                                                   est_usd=3)
    await ledger(db, "live", now=early_nov).record(provider="p", model="m", purpose="x",
                                                    est_usd=1)
    st = await ledger(db, "live", now=datetime(2026, 11, 15, tzinfo=UTC)).status()
    assert st.spent_usd == 1 and st.mode == "normal"
    st = await ledger(db, "live", now=datetime(2026, 10, 20, tzinfo=UTC)).status()
    assert st.spent_usd == 3 and st.percent == 60
    # Next month starts fresh.
    st = await ledger(db, "live", now=datetime(2026, 12, 1, 6, tzinfo=UTC)).status()
    assert st.spent_usd == 0


def test_pricing():
    # Sonnet 5.5: 5K in / 1.5K out = 0.01 + 0.015
    assert claude_cost("claude-sonnet-5-5", input_tokens=5000, output_tokens=1500) == Decimal(
        "0.025000")
    # Haiku 5.5 at the 100K threshold: short rate card ($0.10/M)
    assert claude_cost("claude-haiku-5-5", input_tokens=100_000, output_tokens=0) == Decimal(
        "0.010000")
    # Haiku 5.5 over 100K tokens uses the long rate card
    assert claude_cost("claude-haiku-5-5", input_tokens=200_000, output_tokens=0) == Decimal(
        "0.100000")
    # cache reads at 0.05x on Opus 5.5
    assert claude_cost("claude-opus-5-5", input_tokens=0, output_tokens=0,
                       cache_read_input_tokens=1_000_000) == Decimal("0.200000")
    # unknown model charged at the most expensive rate
    assert claude_cost("claude-unknown", input_tokens=0, output_tokens=1_000_000) == Decimal(
        "25.000000")
    assert fal_cost("fal-ai/sam-3/image", 5) == Decimal("0.025000")
