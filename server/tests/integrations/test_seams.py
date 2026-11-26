"""Integration seams 5 (presence watcher) and 6 (stale flags), plus collage links."""

from __future__ import annotations

import asyncio
import types
from datetime import timedelta

from integrations import today as today_mod
from mirror_api.presence import PresenceWatcher
from tests.conftest import LAN_CLIENT, http_client
from tests.integrations.conftest import FORECAST_NOW
from tests.integrations.test_today import make_state, outfit


async def test_stale_flags_in_payload(fake_services, net, clock):
    """Before: TodayPayload had no way to say a part was an old copy."""
    clock.now = FORECAST_NOW
    fake_services.db = types.SimpleNamespace(configured=True)
    state = make_state(fake_services, net, clock)
    p = await state.tick()
    assert p.stale == []
    clock.now = FORECAST_NOW + timedelta(hours=3)
    net.fail |= {"open-meteo", "google-secret", "outlook-secret", "icloud-secret"}
    p = await state.tick()
    assert set(p.stale) == {"weather", "calendars"}
    state.db_error = "OperationalError"
    assert "database" in state.build().stale
    assert fake_services.hub.of("today.updated")[-1]["stale"] == ["weather", "calendars"]


async def test_presence_watcher_broadcasts_debounced_edges(fake_services, net, clock, hardware):
    fake_services.db = types.SimpleNamespace(configured=False)
    fake_services.hardware = hardware
    state = make_state(fake_services, net, clock)
    setattr(fake_services, today_mod.STATE_ATTR, state)
    w = PresenceWatcher(fake_services, arrive_debounce_s=0.05, leave_debounce_s=0.15)
    w.start()
    hardware.presence.set_present(True)
    hardware.presence.set_present(False)  # a flap shorter than the debounce
    await asyncio.sleep(0.1)
    assert fake_services.hub.of("presence") == []
    hardware.presence.set_present(True)
    await asyncio.sleep(0.1)
    assert fake_services.hub.of("presence") == [{"present": True}]
    hardware.presence.set_present(False)
    await asyncio.sleep(0.05)
    assert fake_services.hub.of("presence") == [{"present": True}]  # leave debounce longer
    await asyncio.sleep(0.2)
    assert fake_services.hub.of("presence") == [{"present": True}, {"present": False}]
    await w.stop()


async def test_presence_arrival_refreshes_then_stays_off_the_database(make_app, settings):
    """Before: presence only refreshed integrations; it never called the recommender, and
    no `presence` event was sent."""
    s = settings.model_copy(update={"presence_arrive_debounce_s": 0.01,
                                    "presence_leave_debounce_s": 0.01})
    app = await make_app(settings_=s)
    services = app.state.services
    hub_events = []
    orig = services.hub.broadcast

    async def spy(type_, data=None, **kw):
        hub_events.append((type_, data))
        return await orig(type_, data, **kw)

    services.hub.broadcast = spy
    w = services.presence
    calls = []

    async def ensure(day):
        calls.append(day)

    w._ensure_fresh = ensure
    services.hardware.presence.set_present(True)
    await asyncio.sleep(0.05)
    await w.wait_idle()
    assert ("presence", {"present": True}) in hub_events
    assert w.refreshes == 1 and calls  # stale on the first arrival: both ran
    before = services.db.checkouts
    for _ in range(3):
        services.hardware.presence.set_present(False)
        await asyncio.sleep(0.05)
        services.hardware.presence.set_present(True)
        await asyncio.sleep(0.05)
        await w.wait_idle()
    assert services.db.checkouts == before  # nothing stale: no database at all
    assert w.refreshes == 1 and len(calls) == 1
    assert hub_events.count(("presence", {"present": False})) == 3


async def test_today_carries_collage_link(make_app, settings, storage):
    app = await make_app(start_workers=False)
    services = app.state.services
    state = today_mod.get_state(services)
    await storage.put("collages/x.png", b"\x89PNG fake", "image/png")
    o = outfit().model_copy(update={"collage_key": "collages/x.png"})
    state.outfits[state.today().isoformat()] = [o.model_copy(update={"for_date": state.today()})]
    state.reminders_loaded = state.spend_known = True
    await state.publish()
    async with http_client(app, LAN_CLIENT) as anon:
        code = (await anon.post("/api/dev/pair-code")).json()["code"]
        tok = (await anon.post("/api/pair", json={"code": code})).json()["token"]
    async with http_client(app, LAN_CLIENT, token=tok) as c:
        body = (await c.get("/api/today")).json()
    url = body["outfits"][0]["collage_url"]
    assert url and "collages/x.png" in url
    import httpx

    async with httpx.AsyncClient() as h:
        assert (await h.get(url)).content == b"\x89PNG fake"
    assert "collage_url\": \"http" not in state.cache_file.read_text()
    assert state.payload.outfits[0].collage_url is None  # never cached
