"""HTTP routes: outfits, reroll, feedback, worn (idempotent), shown, item worn."""

from __future__ import annotations

from datetime import timedelta

import pytest

from recommender import service
from tests.conftest import LAN_CLIENT, http_client
from tests.recommender.conftest import DAY, good_answer, item_id


@pytest.fixture
async def api(make_app, seeded, fake_context, fake_llm, paired):
    fake_llm.default = good_answer
    app = await make_app(start_workers=False)
    async with http_client(app, LAN_CLIENT, token=paired[1]) as c:
        c.services = app.state.services
        yield c


async def test_requires_token(make_app, seeded):
    app = await make_app(start_workers=False)
    async with http_client(app, LAN_CLIENT) as c:
        assert (await c.get("/api/outfits")).status_code == 401
        assert (await c.post(f"/api/items/{item_id('black jeans')}/worn")).status_code == 401
        assert (await c.get("/api/style")).status_code == 401


async def test_get_outfits_empty_then_after_refresh(api):
    r = await api.get("/api/outfits", params={"date": DAY.isoformat()})
    assert r.status_code == 200 and r.json()["outfits"] == []
    assert r.json()["message"]
    r = await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})
    assert r.status_code == 200 and r.json()["rebuilt"] is True
    body = (await api.get("/api/outfits", params={"date": DAY.isoformat()})).json()
    assert len(body["outfits"]) == 3 and body["message"] is None
    o = body["outfits"][0]
    assert o["rank"] == 1 and o["status"] == "suggested" and o["source"] == "llm"
    assert o["collage_url"] and o["items"][0]["name"]
    assert {i["slot"] for i in o["items"]} >= {"top", "bottom", "shoes"}


async def test_reroll_route(api):
    await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})
    first = {o["id"] for o in (await api.get(
        "/api/outfits", params={"date": DAY.isoformat()})).json()["outfits"]}
    r = await api.post("/api/outfits/reroll", json={"date": DAY.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["outfits"]) == 3 and not first & {o["id"] for o in body["outfits"]}
    assert body["source"] == "llm"


async def test_feedback(api):
    await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})
    oid = (await api.get("/api/outfits", params={"date": DAY.isoformat()})).json()[
        "outfits"][0]["id"]
    r = await api.post(f"/api/outfits/{oid}/feedback", json={"value": "up", "note": " love\nit "})
    assert r.status_code == 201 and r.json()["value"] == 1
    r = await api.post(f"/api/outfits/{oid}/feedback", json={"value": -1})
    assert r.status_code == 201
    rows = await api.services.db.fetch("SELECT value, note FROM feedback ORDER BY at")
    assert [(x["value"], x["note"]) for x in rows] == [(1, "love it"), (-1, None)]
    st = await api.services.db.fetchval("SELECT status FROM outfits WHERE id = %s", (oid,))
    assert st == "shown"
    for bad in ({"value": 2}, {"value": "meh"}, {"value": 1, "note": "x" * 501}, {}):
        assert (await api.post(f"/api/outfits/{oid}/feedback", json=bad)).status_code == 422
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await api.post(f"/api/outfits/{missing}/feedback",
                           json={"value": 1})).status_code == 404


async def test_outfit_worn_is_idempotent(api):
    await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})
    o = (await api.get("/api/outfits", params={"date": DAY.isoformat()})).json()["outfits"][0]
    n = len(o["items"])
    r1 = (await api.post(f"/api/outfits/{o['id']}/worn")).json()
    assert len(r1["logged"]) == n and r1["already_logged"] == [] and r1["worn_on"] == "2026-11-25"
    r2 = (await api.post(f"/api/outfits/{o['id']}/worn")).json()
    assert r2["logged"] == [] and len(r2["already_logged"]) == n
    db = api.services.db
    assert await db.fetchval("SELECT count(*) FROM wear_log") == n
    assert await db.fetchval("SELECT status FROM outfits WHERE id = %s", (o["id"],)) == "worn"
    worn_on = await db.fetch("SELECT DISTINCT last_worn_on FROM items WHERE id = ANY(%s)",
                             ([i["item_id"] for i in o["items"]],))
    assert [r["last_worn_on"] for r in worn_on] == [DAY]


async def test_item_worn_idempotent_and_never_moves_back(api):
    iid = item_id("black jeans")
    r = await api.post(f"/api/items/{iid}/worn", json={"date": DAY.isoformat()})
    assert r.status_code == 200 and r.json()["logged"] is True
    r = await api.post(f"/api/items/{iid}/worn", json={"date": DAY.isoformat()})
    assert r.json()["logged"] is False
    earlier = (DAY - timedelta(days=3)).isoformat()
    r = await api.post(f"/api/items/{iid}/worn", json={"date": earlier})
    assert r.json()["logged"] is True
    db = api.services.db
    assert await db.fetchval("SELECT count(*) FROM wear_log WHERE item_id = %s", (iid,)) == 2
    assert await db.fetchval("SELECT last_worn_on FROM items WHERE id = %s", (iid,)) == DAY
    src = await db.fetchval("SELECT DISTINCT source FROM wear_log")
    assert src == "manual"
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await api.post(f"/api/items/{missing}/worn")).status_code == 404
    await db.execute("UPDATE items SET state='retired' WHERE id=%s", (iid,))
    assert (await api.post(f"/api/items/{iid}/worn")).status_code == 409


async def test_outfit_worn_after_item_worn_keeps_one_row(api):
    await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})
    o = (await api.get("/api/outfits", params={"date": DAY.isoformat()})).json()["outfits"][0]
    first = o["items"][0]["item_id"]
    await api.post(f"/api/items/{first}/worn", json={"date": DAY.isoformat()})
    r = (await api.post(f"/api/outfits/{o['id']}/worn")).json()
    assert r["already_logged"] == [first]
    assert await api.services.db.fetchval("SELECT count(*) FROM wear_log") == len(o["items"])


async def test_shown_route(api):
    await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})
    o = (await api.get("/api/outfits", params={"date": DAY.isoformat()})).json()["outfits"][0]
    assert (await api.post(f"/api/outfits/{o['id']}/shown")).status_code == 204
    row = await api.services.db.fetchrow("SELECT status, shown_at FROM outfits WHERE id = %s",
                                         (o["id"],))
    assert row["status"] == "shown" and row["shown_at"] is not None


async def test_cannot_form_message_via_api(api):
    await api.services.db.execute("UPDATE items SET state = 'dirty'")
    r = (await api.post("/api/outfits/refresh", json={"date": DAY.isoformat()})).json()
    assert r["outfits"] == [] and "laundry" in r["message"]
    body = (await api.get("/api/outfits", params={"date": DAY.isoformat()})).json()
    assert "laundry" in body["message"]


async def test_rules_settings_override_via_settings_api(api):
    r = await api.put("/api/settings", json={"recommender.rules": {"outer_required_below_c": 2}})
    assert r.status_code == 200
    cfg, warnings = await service.load_config(api.services)
    assert cfg.outer_required_below_c == 2.0 and warnings == []
