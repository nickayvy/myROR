from __future__ import annotations

from datetime import date

from tests.pipeline import synth
from tests.pipeline.conftest import settle, upload


async def _closet(phone, services, garments):
    for color, kind in garments:
        await upload(phone, synth.garment_jpeg(color, kind))
    await settle(services)
    entries = (await phone.get("/api/inbox")).json()["entries"]
    r = await phone.post("/api/inbox/confirm", json={"entries": [{"id": e["id"]} for e in entries],
                                                     "include_matches": True})
    return {c["item"]["tags"]["primary_color"]: c["item"] for c in r.json()["confirmed"]}


async def test_list_filters_get_patch_state(phone, services):
    items = await _closet(phone, services, [("red", "tee"), ("navy", "trousers"),
                                            ("green", "tee")])
    assert len(items) == 3
    red = items["red"]
    assert red["cutout_url"] and red["primary_lab"] is not None
    listing = (await phone.get("/api/items")).json()
    assert listing["total"] == 3
    tops = (await phone.get("/api/items", params={"category": "top"})).json()
    assert {i["tags"]["primary_color"] for i in tops["items"]} == {"red", "green"}
    assert (await phone.get("/api/items", params={"color": "navy"})).json()["total"] == 1
    assert (await phone.get("/api/items", params={"q": "green"})).json()["total"] == 1
    assert (await phone.get("/api/items", params={"season": "summer"})).json()["total"] == 3

    r = await phone.patch(f"/api/items/{red['id']}", json={"name": "Old red tee",
                                                           "fit": "relaxed"})
    assert r.status_code == 200 and r.json()["name"] == "Old red tee"
    assert r.json()["tags"]["fit"] == "relaxed"
    r = await phone.patch(f"/api/items/{red['id']}", json={"subcategory": "jeans"})
    assert r.status_code == 422 and "subcategory" in r.json()["detail"]["errors"][0]
    r = await phone.patch(f"/api/items/{red['id']}", json={"brand": "x"})
    assert r.status_code == 422
    assert (await phone.get(f"/api/items/{red['id']}")).json()["tags"]["subcategory"] == "t_shirt"

    r = await phone.post(f"/api/items/{red['id']}/state", json={"state": "dirty"})
    assert r.json()["state"] == "dirty"
    r = await phone.post(f"/api/items/{red['id']}/state", json={"state": "retired"})
    assert (await phone.get("/api/items")).json()["total"] == 2
    assert (await phone.get("/api/items", params={"state": "retired"})).json()["total"] == 1
    assert (await phone.post(f"/api/items/{red['id']}/state",
                             json={"state": "lost"})).status_code == 422
    assert (await phone.get("/api/items/00000000-0000-0000-0000-000000000000")).status_code \
        == 404


async def test_merge_two_items_moves_history(phone, services, db):
    items = await _closet(phone, services, [("red", "tee"), ("green", "tee")])
    a, b = items["red"]["id"], items["green"]["id"]
    await db.execute("INSERT INTO wear_log (item_id, worn_on, source) VALUES"
                     " (%s, '2026-11-01', 'manual'), (%s, '2026-11-01', 'manual'),"
                     " (%s, '2026-11-02', 'manual')", (a, b, a))
    await db.execute("UPDATE items SET last_worn_on = '2026-11-02' WHERE id = %s", (a,))
    r = await phone.post(f"/api/items/{a}/merge", json={"into": b})
    assert r.status_code == 200, r.text
    merged = r.json()["item"]
    assert merged["id"] == b and merged["last_worn_on"] == "2026-11-02"
    assert (await phone.get(f"/api/items/{a}")).status_code == 404
    days = [r["worn_on"] for r in await db.fetch(
        "SELECT worn_on FROM wear_log WHERE item_id = %s ORDER BY worn_on", (b,))]
    assert days == [date(2026, 11, 1), date(2026, 11, 2)]
    assert (await phone.post(f"/api/items/{b}/merge", json={"into": b})).status_code == 422
