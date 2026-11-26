from __future__ import annotations

from tests.pipeline import synth
from tests.pipeline.conftest import settle, upload


async def _confirmed(phone, services, color="red", kind="tee"):
    r = await upload(phone, synth.garment_jpeg(color, kind))
    await settle(services)
    entry = [e for e in (await phone.get("/api/inbox")).json()["entries"]
             if e["photo_id"] == r.json()["photo_id"]][0]
    item = (await phone.post(f"/api/inbox/{entry['id']}/confirm")).json()["item"]
    return r.json()["photo_id"], item


async def test_delete_onboarding_photo_keeps_item_without_cutout(phone, services, storage):
    pid, item = await _confirmed(phone, services)
    other_pid, other = await _confirmed(phone, services, "navy", "trousers")
    assert any(k.startswith(f"cutouts/{pid}/") for k in await storage.list(""))
    r = await phone.delete(f"/api/photos/{pid}")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["items_kept_without_cutout"] == [item["id"]]
    assert sorted(k.split("/")[0] for k in rep["objects_deleted"]) == ["cutouts", "photos"]
    remaining = await storage.list("")
    assert not any(pid in k for k in remaining), remaining  # nothing of that photo is left
    assert any(other_pid in k for k in remaining)  # the other photo is untouched
    kept = (await phone.get(f"/api/items/{item['id']}")).json()
    assert kept["cutout_url"] is None and kept["source_photo_id"] is None
    assert (await phone.get(f"/api/items/{other['id']}")).json()["cutout_url"]
    assert await services.db.fetchval("SELECT count(*) FROM detections WHERE photo_id = %s",
                                      (pid,)) == 0
    assert (await phone.get(f"/api/photos/{pid}")).status_code == 404
    assert (await phone.delete(f"/api/photos/{pid}")).status_code == 404


async def test_delete_with_items(phone, services, storage):
    pid, item = await _confirmed(phone, services)
    r = await phone.delete(f"/api/photos/{pid}", params={"delete_items": "true"})
    assert r.json()["items_deleted"] == [item["id"]]
    assert (await phone.get(f"/api/items/{item['id']}")).status_code == 404
    assert await storage.list("") == []


async def test_delete_pending_and_checkin_photo_cleans_bucket(phone, services, storage, db):
    pid, item = await _confirmed(phone, services)
    await upload(phone, synth.garment_jpeg("green", "tee"))  # stays pending in the inbox
    r = await upload(phone, synth.person_jpeg("red", "navy"), path="/api/checkins/upload")
    cid = r.json()["photo_id"]
    await settle(services)
    await phone.post(f"/api/checkins/{cid}/confirm", json={"item_ids": [item["id"]]})
    assert any(k.startswith(f"masks/{cid}/") for k in await storage.list(""))
    rep = (await phone.delete(f"/api/photos/{cid}")).json()
    assert {k.split("/")[0] for k in rep["objects_deleted"]} == {"photos", "cutouts", "masks"}
    assert not any(cid in k for k in await storage.list(""))
    # the wear stays logged, without the photo
    row = await db.fetchrow("SELECT photo_id FROM wear_log WHERE item_id = %s", (item["id"],))
    assert row is not None and row["photo_id"] is None
    # delete everything else; the bucket must end up empty
    for p in (await phone.get("/api/photos")).json()["photos"]:
        assert (await phone.delete(f"/api/photos/{p['photo_id']}",
                                   params={"delete_items": "true"})).status_code == 200
    assert await storage.list("") == []
    assert await db.fetchval("SELECT count(*) FROM photos") == 0
    assert await db.fetchval("SELECT count(*) FROM detections") == 0
