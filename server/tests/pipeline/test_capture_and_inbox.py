"""Capture API -> check-in pipeline, and seam 8: unmatched check-in garments to the inbox."""

from __future__ import annotations

from capture import CaptureMachine, Timings
from capture.clock import FakeClock
from capture.clock import settle as tick
from pipeline.ingest import checkin_from_bytes
from tests.conftest import LAN_CLIENT, http_client
from tests.pipeline import synth
from tests.pipeline.conftest import settle, upload


async def _owned(phone, services, garments):
    for color, kind in garments:
        await upload(phone, synth.garment_jpeg(color, kind))
    await settle(services)
    entries = (await phone.get("/api/inbox")).json()["entries"]
    r = await phone.post("/api/inbox/confirm", json={"entries": [{"id": e["id"]} for e in entries],
                                                     "include_matches": True})
    return {c["item"]["tags"]["primary_color"]: c["item"] for c in r.json()["confirmed"]}


async def test_unmatched_checkin_garment_goes_to_inbox(phone, services, db):
    """Before: a check-in garment that matched nothing could not become a closet item."""
    owned = await _owned(phone, services, [("red", "tee")])
    pid = (await upload(phone, synth.person_jpeg("red", "teal"),
                        path="/api/checkins/upload")).json()["photo_id"]
    await settle(services)
    c = (await phone.get(f"/api/checkins/{pid}")).json()
    by = {g["tags"]["primary_color"]: g for g in c["garments"]}
    assert by["red"]["match"] and by["teal"]["match"] is None
    matched = await phone.post(f"/api/checkins/{pid}/garments/{by['red']['detection_id']}/inbox")
    assert matched.status_code == 409
    r = await phone.post(f"/api/checkins/{pid}/garments/{by['teal']['detection_id']}/inbox")
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    again = await phone.post(f"/api/checkins/{pid}/garments/{by['teal']['detection_id']}/inbox")
    assert again.status_code == 200 and again.json()["inbox_id"] == inbox_id
    entries = (await phone.get("/api/inbox")).json()["entries"]
    assert [e["id"] for e in entries] == [inbox_id] and entries[0]["origin"] == "checkin"
    assert entries[0]["needs_review"] and entries[0]["cutout_url"]
    # the check-in still shows its two garments, and confirming it leaves the entry alone
    c = (await phone.get(f"/api/checkins/{pid}")).json()
    assert len(c["garments"]) == 2
    assert {g["inbox_id"] for g in c["garments"]} == {None, inbox_id}
    r = await phone.post(f"/api/checkins/{pid}/confirm", json={"item_ids": [owned["red"]["id"]]})
    assert r.status_code == 200
    assert (await phone.get("/api/inbox")).json()["count"] == 1
    item = (await phone.post(f"/api/inbox/{inbox_id}/confirm")).json()["item"]
    assert item["tags"]["primary_color"] == "teal" and item["source_photo_id"] == pid


async def test_capture_api_keep_runs_checkin(papp, phone, services):
    clock = FakeClock()

    async def keep(jpeg):
        return await checkin_from_bytes(services, jpeg)

    m = CaptureMachine(services.hardware, services.hub, on_keep=keep, clock=clock,
                       timings=Timings(), threaded_checks=False)
    m.open()
    services.capture = m
    services.hardware.camera.frame = synth.person_jpeg("red", "navy", size=(600, 800))
    services.hardware.cover.set_closed(False)
    r = await phone.post("/api/capture")
    assert r.status_code == 200 and r.json()["state"] == "preview"
    cid = r.json()["capture_id"]
    assert (await phone.post("/api/capture")).status_code == 409
    await clock.advance(4.7)
    cur = (await phone.get("/api/capture/current")).json()
    assert cur["state"] == "review" and cur["review_url"]
    async with http_client(papp, LAN_CLIENT) as anon:  # the link needs no token
        img = await anon.get(cur["review_url"])
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
        assert (await anon.get(f"/api/capture/{cid}/image?t=wrong")).status_code == 404
    assert (await phone.post("/api/capture/nope/keep")).status_code == 404
    r = await phone.post(f"/api/capture/{cid}/keep")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "cover_reminder" and body["photo_id"]
    assert (await phone.post(f"/api/capture/{cid}/retake")).status_code == 409
    services.hardware.cover.set_closed(True)
    await tick()
    assert (await phone.get("/api/capture/current")).json() == {"state": "idle"}
    await settle(services)
    c = (await phone.get(f"/api/checkins/{body['photo_id']}")).json()
    assert c["status"] == "done" and c["source"] == "mirror_checkin"
    assert {g["tags"]["primary_color"] for g in c["garments"]} == {"red", "navy"}
    await m.close()
