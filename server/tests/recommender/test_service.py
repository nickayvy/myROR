"""Precompute, ensure_fresh, reroll, spend modes, the integrations seams, persistence."""

from __future__ import annotations

import sys
import time
import types
from datetime import timedelta

import pytest

from providers.llm import LLMRefusal, LLMTruncated, LLMUnavailable
from recommender import service, store
from recommender.jobs import PRECOMPUTE
from recommender.today import get_outfits
from tests.recommender.conftest import DAY, good_answer, item_id


@pytest.fixture
async def services(make_app, seeded, fake_context, fake_llm):
    fake_llm.default = good_answer
    app = await make_app(start_workers=False)
    return app.state.services


async def spend(services, usd):
    await services.db.execute(
        "INSERT INTO api_spend (ledger, provider, model, purpose, est_usd)"
        " VALUES ('dev', 'test', 'm', 'seed', %s)", (usd,))


async def test_precompute_stores_three_llm_outfits_with_context(services, fake_llm):
    res = await service.precompute(services, DAY)
    assert res.built and res.source == "llm" and len(res.outfits) == 3
    assert len(fake_llm.calls) == 1
    o = res.outfits[0]
    assert [x.rank for x in res.outfits] == [1, 2, 3]
    ctx = o.context
    assert ctx["fingerprint"] and ctx["trigger"] == "precompute"
    assert ctx["fingerprint_parts"]["closet_clean"] == 30
    assert "cold" in ctx["fingerprint_parts"]["weather"]
    assert all(x.collage_key and x.collage_key.startswith("collages/2026-11-25/")
               for x in res.outfits)
    assert await services.storage.exists(o.collage_key)
    rows = await services.db.fetch("SELECT slot FROM outfit_items WHERE outfit_id = %s", (o.id,))
    assert {r["slot"] for r in rows} >= {"top", "bottom", "shoes", "outer"}  # 8 °C: cold


async def test_get_outfits_reads_stored_only_and_is_fast(services, fake_llm):
    assert await get_outfits(services, DAY) == []
    await service.precompute(services, DAY)
    calls = len(fake_llm.calls)
    t0 = time.perf_counter()
    for _ in range(20):
        out = await get_outfits(services, DAY)
    per_call_ms = (time.perf_counter() - t0) / 20 * 1000
    assert len(out) == 3 and out[0].rank == 1 and out[0].source == "llm"
    assert len(fake_llm.calls) == calls  # never calls the LLM
    print(f"\nget_outfits: {per_call_ms:.2f} ms per call (local Postgres)")
    assert per_call_ms < 100


async def test_ensure_fresh_skips_when_unchanged(services, fake_llm, fake_context):
    assert await service.ensure_fresh(services, DAY) is True
    assert await service.ensure_fresh(services, DAY) is False
    # a small temperature change inside the band: still no rebuild
    fake_context.set(temp_c=8.5)
    assert await service.ensure_fresh(services, DAY) is False
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("change", ["weather", "formality", "closet", "profile"])
async def test_ensure_fresh_rebuilds_on_change(services, fake_llm, fake_context, change):
    await service.ensure_fresh(services, DAY)
    before = {o.id for o in await store.current_outfits(services.db, DAY)}
    if change == "weather":
        fake_context.set(temp_c=24.0, condition="rain")
    elif change == "formality":
        fake_context.set(temp_c=8.0, min_formality=4)
    elif change == "closet":
        await services.db.execute("UPDATE items SET state = 'dirty' WHERE id = %s",
                                  (item_id("dark wash jeans"),))
    else:
        await services.db.execute("INSERT INTO style_profile (text) VALUES ('likes navy')")
    assert await service.ensure_fresh(services, DAY) is True
    after = await store.current_outfits(services.db, DAY)
    assert len(after) == 3 and not before & {o.id for o in after}
    superseded = await services.db.fetchval(
        "SELECT count(*) FROM outfits WHERE superseded_at IS NOT NULL")
    assert superseded == 3


async def test_ensure_fresh_stops_once_worn(services, fake_context):
    await service.ensure_fresh(services, DAY)
    o = (await store.current_outfits(services.db, DAY))[0]
    await store.log_wear(services.db, [i.item_id for i in o.items], DAY, source="outfit",
                         outfit_id=o.id)
    fake_context.set(temp_c=30.0)
    assert await service.ensure_fresh(services, DAY) is False


async def test_missing_or_broken_integrations_mean_no_weather(services, monkeypatch, fake_llm):
    monkeypatch.setitem(sys.modules, "integrations.context", None)  # absent
    res = await service.precompute(services, DAY)
    assert res.built and res.outfits[0].context["fingerprint_parts"]["weather"] == {
        "known": False}
    broken = types.ModuleType("integrations.context")

    async def boom(services, d):
        raise RuntimeError("half-written")
    broken.get_day_context = boom
    monkeypatch.setitem(sys.modules, "integrations.context", broken)
    assert (await service.get_day_context(services, DAY)).weather is None
    wrong = types.ModuleType("integrations.context")

    async def wrong_type(services, d):
        return {"date": d}
    wrong.get_day_context = wrong_type
    monkeypatch.setitem(sys.modules, "integrations.context", wrong)
    assert (await service.get_day_context(services, DAY)).weather is None


async def test_invalidate_and_broadcast_after_store(services, monkeypatch):
    seen = []
    mod = types.ModuleType("integrations.today")

    async def invalidate_outfits(for_date):
        seen.append(for_date)
    mod.invalidate_outfits = invalidate_outfits
    monkeypatch.setitem(sys.modules, "integrations.today", mod)
    sent = []

    async def broadcast(type_, data=None, **kw):
        sent.append((type_, data))
        return 0
    monkeypatch.setattr(services.hub, "broadcast", broadcast)
    res = await service.precompute(services, DAY)
    assert seen == [DAY]
    assert sent[-1][0] == "outfits.updated"
    assert sent[-1][1]["outfit_ids"] == [str(o.id) for o in res.outfits]


async def test_invalidate_signature_variants(services, monkeypatch):
    calls = []
    for fn in (lambda: calls.append("none"),
               lambda services, for_date: calls.append(("both", for_date)),
               lambda d: calls.append(("one", d))):
        mod = types.ModuleType("integrations.today")
        mod.invalidate_outfits = fn
        monkeypatch.setitem(sys.modules, "integrations.today", mod)
        await service._invalidate(services, DAY)
    assert calls == ["none", ("both", DAY), ("one", DAY)]


# ---- spend modes and failures -------------------------------------------------------------------


async def test_blocked_is_rules_only(services, fake_llm):
    await spend(services, 10.0)
    res = await service.precompute(services, DAY)
    assert fake_llm.calls == [] and res.source == "rules" and len(res.outfits) == 3
    assert res.outfits[0].context["spend_mode"] == "blocked"


@pytest.mark.parametrize("exc", [LLMRefusal("general_harms", None), LLMTruncated("x"),
                                 LLMUnavailable("x")])
async def test_llm_failure_still_gives_outfits(services, fake_llm, exc):
    fake_llm.push(exc)
    res = await service.precompute(services, DAY)
    assert res.source == "rules" and len(res.outfits) == 3
    assert res.outfits[0].context["fallback_reason"] == f"llm {type(exc).__name__}"


async def test_reroll_normal_calls_llm_and_avoids_earlier(services, fake_llm):
    first = await service.precompute(services, DAY)
    res = await service.reroll(services, DAY)
    assert res.built and len(fake_llm.calls) == 2
    assert "Already suggested today" in fake_llm.calls[1].text
    assert not {o.key for o in first.outfits} & {o.key for o in res.outfits}
    shown = await services.db.fetchval(
        "SELECT count(*) FROM outfits WHERE status = 'shown' AND superseded_at IS NOT NULL")
    assert shown == 3  # the set the user rerolled away from was shown


async def test_reroll_degraded_reuses_stored_without_llm(services, fake_llm):
    await service.precompute(services, DAY)
    first_set = {o.id for o in await store.current_outfits(services.db, DAY)}
    await service.reroll(services, DAY)  # normal: a second stored set
    second_set = {o.id for o in await store.current_outfits(services.db, DAY)}
    await spend(services, 8.5)  # degraded
    calls = len(fake_llm.calls)
    res = await service.reroll(services, DAY)
    assert len(fake_llm.calls) == calls  # no LLM
    assert res.source == "stored" and {o.id for o in res.outfits} == first_set
    # cycles back to the second set next time
    res = await service.reroll(services, DAY)
    assert {o.id for o in res.outfits} == second_set and len(fake_llm.calls) == calls
    # reactivated outfits carry the current fingerprint: presence does not undo the reroll
    assert await service.ensure_fresh(services, DAY) is False


async def test_reroll_degraded_skips_disliked_and_dirty(services, fake_llm):
    await service.precompute(services, DAY)
    old = await store.current_outfits(services.db, DAY)
    await service.reroll(services, DAY)
    await store.add_feedback(services.db, old[0].id, -1, "no")
    dirty = next(iter(old[1].key))
    await services.db.execute("UPDATE items SET state='dirty' WHERE id=%s", (dirty,))
    await spend(services, 8.5)
    res = await service.reroll(services, DAY)
    ids = {o.id for o in res.outfits}
    assert old[0].id not in ids and old[1].id not in ids


async def test_reroll_degraded_with_nothing_stored_uses_rules(services, fake_llm):
    await spend(services, 8.5)
    res = await service.reroll(services, DAY)
    assert fake_llm.calls == [] and res.source == "rules" and len(res.outfits) == 3


async def test_reroll_blocked_is_rules(services, fake_llm):
    await service.precompute(services, DAY)
    await spend(services, 10.0)
    calls = len(fake_llm.calls)
    res = await service.reroll(services, DAY)
    assert len(fake_llm.calls) == calls and res.source == "rules"


async def test_cannot_form_stores_nothing_and_explains(services, fake_llm):
    await service.precompute(services, DAY)
    await services.db.execute("UPDATE items SET state = 'dirty'")
    assert await service.ensure_fresh(services, DAY) is True
    assert await store.current_outfits(services.db, DAY) == []
    st = service.last_status(services, DAY)
    assert "30 in the laundry" in st["message"]
    calls = len(fake_llm.calls)
    assert await service.ensure_fresh(services, DAY) is False  # same fingerprint, no retry
    assert len(fake_llm.calls) == calls


async def test_close_out_marks_shown_unworn_as_skipped(services):
    yesterday = DAY - timedelta(days=1)
    await service.precompute(services, yesterday)
    rows = await store.current_outfits(services.db, yesterday)
    await store.mark_shown(services.db, [rows[0].id, rows[1].id])
    await store.log_wear(services.db, [i.item_id for i in rows[1].items], yesterday,
                         source="outfit", outfit_id=rows[1].id)
    await service.precompute(services, DAY)  # closes out earlier days
    st = {r["id"]: r["status"] for r in await services.db.fetch(
        "SELECT id, status FROM outfits WHERE for_date = %s", (yesterday,))}
    assert st[rows[0].id] == "skipped" and st[rows[1].id] == "worn"
    assert st[rows[2].id] == "suggested"  # never shown: not a skip


async def test_precompute_job_and_daily(make_app, seeded, fake_context, fake_llm):
    fake_llm.default = good_answer
    app = await make_app()
    services = app.state.services
    await services.jobs.enqueue(PRECOMPUTE, {"date": DAY.isoformat()})
    await services.jobs.wait_idle(15)
    assert len(await get_outfits(services, DAY)) == 3
    job = await services.db.fetchrow("SELECT status FROM jobs WHERE type = %s", (PRECOMPUTE,))
    assert job["status"] == "done"
    from mirror_api.jobs import DAILY
    assert DAILY["recommender.morning"].setting == "precompute_time"
