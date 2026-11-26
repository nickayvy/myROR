"""Today payload cache, timers, seams. Unit tests use a Services stand-in whose database
raises on any access, which is how "timers never touch the database" is proven."""

import asyncio
import sys
import time
import types
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from integrations import today as today_mod
from integrations.context import get_day_context
from integrations.today import IntegrationsState
from mirror_api.contracts import OutfitItem, OutfitSuggestion, TodayPayload

from .conftest import FORECAST_NOW, MORNING, RecordingHub

DAY = date(2026, 11, 25)


def outfit(day=DAY, rank=1):
    return OutfitSuggestion(for_date=day, rank=rank, reasoning="Warm layers.", source="rules",
                            items=[OutfitItem(item_id=uuid4(), slot="top")])


def make_state(services, net, clock, outfits=None, **kw):
    async def provider(svc, day):
        if isinstance(outfits, Exception):
            raise outfits
        return [o for o in (outfits or []) if o.for_date == day]

    return IntegrationsState(services, transport=net.transport, clock=clock,
                             outfits_provider=provider, **kw)


async def test_timer_tick_builds_payload_without_database(fake_services, net, clock):
    clock.now = FORECAST_NOW
    state = make_state(fake_services, net, clock)
    await state.start(timers=False)
    p = await state.tick()
    assert fake_services.db.touched == []
    assert p.date == DAY and p.weather is not None and p.weather.temp_c == 4.2
    assert [e.title for e in p.events][:2] == ["Water the plants", "Quarterly review"]
    assert p.last_sync.weather == FORECAST_NOW and p.last_sync.calendars == FORECAST_NOW
    assert p.spend_mode == "normal" and p.outfits == [] and p.reminders == []
    hub = fake_services.hub
    assert len(hub.of("today.updated")) == 1
    assert TodayPayload(**hub.of("today.updated")[0]) == p

    # A second tick a minute later: nothing is due, nothing is fetched or broadcast.
    n = len(net.requests)
    clock.now += timedelta(minutes=1)
    await state.tick()
    assert len(net.requests) == n and len(hub.of("today.updated")) == 1
    # After the TTLs, both are fetched again; data identical -> still no broadcast.
    clock.now += timedelta(minutes=31)
    await state.tick()
    assert net.count("open-meteo") == 2 and net.count("google-secret") == 2
    assert fake_services.db.touched == []
    await state.stop()


async def test_cache_survives_restart_with_network_down(fake_services, net, clock):
    state = make_state(fake_services, net, clock)
    await state.start(timers=False)
    state.reminders_loaded = state.spend_known = True  # a mirror that has bootstrapped once
    first = await state.tick()
    await state.stop()
    assert (state.dir / "today.json").exists()

    net.fail |= {"open-meteo", "google-secret", "outlook-secret", "icloud-secret"}
    clock.now += timedelta(hours=2)
    again = make_state(fake_services, net, clock)
    await again.start(timers=False)
    assert again.current() == first  # straight from disk
    p = await again.tick()  # every fetch fails; the last good data stays
    assert p.events == first.events and p.weather is not None
    st = again.status()
    assert st["weather"]["last_error"] == "ConnectError"
    assert all(st["calendars"][s]["stale"] for s in ("google", "outlook", "icloud"))
    assert fake_services.db.touched == []
    await again.stop()


async def test_midnight_rollover(fake_services, net, clock):
    state = make_state(fake_services, net, clock, outfits=[outfit()])
    state.services.db = types.SimpleNamespace(configured=False)
    await state.start(timers=False)
    await state.refresh()
    assert state.payload.outfits and state.payload.date == DAY
    clock.now = datetime(2026, 11, 26, 5, 1, tzinfo=UTC)  # 00:01 New York
    p = await state.tick()
    assert p.date == date(2026, 11, 26)
    assert [e.title for e in p.events] == ["Thanksgiving"] and p.outfits == []
    assert fake_services.hub.of("today.updated")[-1]["date"] == "2026-11-26"
    await state.stop()


async def test_outfits_seam_and_invalidate(fake_services, net, clock):
    fake_services.db = types.SimpleNamespace(configured=False)
    o1 = outfit()
    box = [o1]
    state = make_state(fake_services, net, clock, outfits=box)
    setattr(fake_services, today_mod.STATE_ATTR, state)
    await state.start(timers=False)
    p = await today_mod.refresh_if_stale(fake_services)
    assert p.outfits == [o1] and p.last_sync.outfits == MORNING
    o2 = outfit(rank=2)
    box.append(o2)
    before = len(fake_services.hub.of("today.updated"))
    await today_mod.invalidate_outfits(fake_services)
    assert [o.rank for o in state.payload.outfits] == [1, 2]
    assert len(fake_services.hub.of("today.updated")) == before + 1
    await state.stop()


async def test_recommender_missing_or_failing(fake_services, net, clock, monkeypatch):
    fake_services.db = types.SimpleNamespace(configured=False)
    monkeypatch.setitem(sys.modules, "recommender.today", None)  # ImportError
    state = IntegrationsState(fake_services, transport=net.transport, clock=clock)
    await state.start(timers=False)
    assert (await state.refresh()).outfits == []
    await state.stop()

    # Cached outfits survive a failing recommender (e.g. database down).
    good = make_state(fake_services, net, clock, outfits=[outfit()])
    await good.start(timers=False)
    await good.refresh()
    await good.stop()
    bad = make_state(fake_services, net, clock, outfits=RuntimeError("db down"))
    await bad.start(timers=False)
    assert len((await bad.refresh(force=True)).outfits) == 1
    await bad.stop()


async def test_real_seam_module_is_used(fake_services, net, clock, monkeypatch):
    fake_services.db = types.SimpleNamespace(configured=False)
    mod = types.ModuleType("recommender.today")
    o = outfit()

    async def get_outfits(services, for_date):
        return [o]

    mod.get_outfits = get_outfits
    monkeypatch.setitem(sys.modules, "recommender.today", mod)
    state = IntegrationsState(fake_services, transport=net.transport, clock=clock)
    await state.start(timers=False)
    assert (await state.refresh()).outfits == [o]
    await state.stop()


async def test_presence_triggers_refresh(fake_services, net, clock, hardware):
    # Presence handling moved from IntegrationsState to mirror_api.presence.PresenceWatcher
    # (integration seam 5); an arrival still refreshes the stale integrations.
    from mirror_api.presence import PresenceWatcher

    fake_services.db = types.SimpleNamespace(configured=False)
    fake_services.hardware = hardware
    state = make_state(fake_services, net, clock, outfits=[outfit()])
    setattr(fake_services, today_mod.STATE_ATTR, state)
    await state.start(timers=False)
    watcher = PresenceWatcher(fake_services, arrive_debounce_s=0, leave_debounce_s=0)
    watcher.start()
    hardware.presence.set_present(True)
    for _ in range(50):
        await asyncio.sleep(0.02)
        if state.payload.outfits:
            break
    assert state.payload.outfits
    await watcher.stop()
    await state.stop()


async def test_unconfigured_everything(fake_services, net, clock):
    s = fake_services.settings.model_copy(update={
        "weather_lat": None, "cal_google_ics_url": "", "cal_outlook_ics_url": "",
        "cal_icloud_ics_url": ""})
    fake_services.settings = s
    fake_services.db = types.SimpleNamespace(configured=False)
    state = make_state(fake_services, net, clock)
    setattr(fake_services, today_mod.STATE_ATTR, state)
    await state.start(timers=False)
    p = await state.refresh()
    assert p.weather is None and p.events == [] and net.requests == []
    ctx = await get_day_context(fake_services, DAY)
    assert ctx.weather is None and ctx.events == [] and ctx.min_formality == 1
    await state.stop()


async def test_day_context_fresh_cache_and_formality(fake_services, net, clock):
    import json

    def rate(call):
        items = json.loads(call.text)
        return {"ratings": [{"i": it["i"], "formality": 4 if "Client" in it["title"] else 2}
                            for it in items]}

    fake_services.llm.default = rate
    state = make_state(fake_services, net, clock)
    setattr(fake_services, today_mod.STATE_ATTR, state)
    await state.start(timers=False)
    await state.tick()
    n = len(net.requests)
    t0 = time.perf_counter()
    ctx = await get_day_context(fake_services, DAY)
    assert len(net.requests) == n  # fresh: no HTTP
    assert ctx.min_formality == 4 and all(e.formality for e in ctx.events)
    assert len(fake_services.llm.calls) == 1
    t1 = time.perf_counter()
    again = await get_day_context(fake_services, DAY)
    assert time.perf_counter() - t1 < 0.2 and again == ctx
    assert len(fake_services.llm.calls) == 1  # cached by title
    assert t1 - t0 < 1.0
    tomorrow = await get_day_context(fake_services, date(2026, 11, 26))
    assert tomorrow.weather is not None and tomorrow.events[0].title == "Thanksgiving"
    # Stale cache -> one refresh
    clock.now += timedelta(hours=2)
    await get_day_context(fake_services, DAY)
    assert len(net.requests) > n
    assert fake_services.db.touched == []
    await state.stop()


async def test_day_context_tagging_failure_falls_back(fake_services, net, clock):
    from providers.llm import LLMUnavailable

    fake_services.llm.default = LLMUnavailable("down")
    state = make_state(fake_services, net, clock)
    setattr(fake_services, today_mod.STATE_ATTR, state)
    ctx = await get_day_context(fake_services, DAY)
    assert ctx.events and all(e.formality is None for e in ctx.events)
    assert ctx.min_formality == 1
    await get_day_context(fake_services, DAY)
    assert len(fake_services.llm.calls) == 1  # not retried within the hour
    await state.stop()


def test_hub_is_optional():
    assert RecordingHub().of("x") == []


async def test_slow_tagging_does_not_block_context(fake_services, net, clock, monkeypatch):
    import json

    from integrations import context

    monkeypatch.setattr(context, "TAG_WAIT_S", 0.1)
    release = asyncio.Event()

    class SlowLLM:
        configured = True

        async def complete_structured(self, **kw):
            await release.wait()
            items = json.loads(kw["content"][0]["text"])
            return {"ratings": [{"i": it["i"], "formality": 3} for it in items]}

    fake_services.llm = SlowLLM()
    state = make_state(fake_services, net, clock)
    setattr(fake_services, today_mod.STATE_ATTR, state)
    t0 = time.perf_counter()
    ctx = await get_day_context(fake_services, DAY)
    assert time.perf_counter() - t0 < 1.0
    assert ctx.events and ctx.min_formality == 1
    release.set()  # the call finishes in the background and is cached
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(state.formality):
            break
    assert (await get_day_context(fake_services, DAY)).min_formality == 3
    await state.stop()
