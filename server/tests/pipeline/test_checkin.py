from __future__ import annotations

from datetime import date

from pipeline.ingest import checkin_from_bytes
from providers.llm import LLMRefusal
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


async def test_checkin_proposes_matches_and_logs_wear(phone, services, db, storage):
    owned = await _owned(phone, services, [("red", "tee"), ("navy", "trousers"),
                                           ("green", "tee")])
    r = await upload(phone, synth.person_jpeg("red", "navy"), path="/api/checkins/upload")
    assert r.status_code == 201, r.text
    pid = r.json()["photo_id"]
    assert r.json()["source"] == "phone_fullbody"
    await settle(services)
    c = (await phone.get(f"/api/checkins/{pid}")).json()
    assert c["status"] == "done", c
    labels = {g["tags"]["primary_color"]: g for g in c["garments"]}
    assert set(labels) == {"red", "navy"}
    for color in ("red", "navy"):
        g = labels[color]
        assert g["cutout_url"] and g["segmentation"] == "fake"
        assert g["match"]["item"]["id"] == owned[color]["id"], g["match"]
        assert g["match"]["score"] >= 0.55
    # the green tee (same category, other colour) was not proposed for the red one
    vision_calls = [x for x in services.llm.calls if x.purpose == "checkin_vision"]
    assert len(vision_calls) == 1  # one call, tags included: no per-garment tagging call
    assert services.segmenter.requests == 1  # one segmentation request for every box
    v = vision_calls[0]
    assert "Never comment on the person's body" in v.system
    assert "gender" in v.system and "attractiveness" in v.system
    assert v.model == "claude-haiku-5-5" and v.image_count == 1
    assert "pixels" in v.text
    assert sorted(k.split("/")[0] for k in await storage.list(f"masks/{pid}/")) == ["masks"] * 2

    ids = [owned["red"]["id"], owned["navy"]["id"]]
    r = await phone.post(f"/api/checkins/{pid}/confirm", json={"item_ids": ids,
                                                               "worn_on": "2026-11-25"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["logged"]) == sorted(ids)
    again = (await phone.post(f"/api/checkins/{pid}/confirm",
                              json={"item_ids": ids, "worn_on": "2026-11-25"})).json()
    assert again["logged"] == [] and sorted(again["already_logged"]) == sorted(ids)
    rows = await db.fetch("SELECT item_id, worn_on, source, photo_id FROM wear_log")
    assert len(rows) == 2 and all(r["source"] == "checkin" and r["worn_on"] == date(2026, 11, 25)
                                  and str(r["photo_id"]) == pid for r in rows)
    red = (await phone.get(f"/api/items/{owned['red']['id']}")).json()
    assert red["last_worn_on"] == "2026-11-25"
    # an earlier day never moves last_worn_on back
    await phone.post(f"/api/checkins/{pid}/confirm", json={"item_ids": [owned["red"]["id"]],
                                                           "worn_on": "2026-11-01"})
    red = (await phone.get(f"/api/items/{owned['red']['id']}")).json()
    assert red["last_worn_on"] == "2026-11-25"
    statuses = {g["status"] for g in (await phone.get(f"/api/checkins/{pid}")).json()["garments"]}
    assert statuses == {"confirmed"}
    # check-in detections never show up in the onboarding inbox
    assert (await phone.get("/api/inbox")).json()["count"] == 0
    r = await phone.post(f"/api/checkins/{pid}/confirm",
                         json={"item_ids": ["00000000-0000-0000-0000-000000000000"]})
    assert r.status_code == 404


async def test_layered_outfit(phone, services):
    await _owned(phone, services, [("red", "tee")])
    r = await upload(phone, synth.person_jpeg("red", "navy", outer="black"),
                     path="/api/checkins/upload")
    await settle(services)
    c = (await phone.get(f"/api/checkins/{r.json()['photo_id']}")).json()
    cats = sorted(g["tags"]["category"] for g in c["garments"])
    assert cats == ["bottom", "outerwear", "top"]


async def test_mirror_function_entry_point(services):
    pid = await checkin_from_bytes(services, synth.person_jpeg("green", "black"))
    await settle(services)
    row = await services.db.fetchrow("SELECT source, pipeline_status, face_blurred, original_key"
                                     " FROM photos WHERE id = %s", (pid,))
    assert row["source"] == "mirror_checkin" and row["pipeline_status"] == "done"
    assert row["face_blurred"] is False and row["original_key"] is None
    assert await checkin_from_bytes(services, synth.person_jpeg("green", "black")) == pid


async def test_refusal_reports_could_not_analyse_without_retry(phone, services, db):
    services.llm.push(LLMRefusal("other", "declined"))
    r = await upload(phone, synth.person_jpeg(), path="/api/checkins/upload")
    await settle(services)
    c = (await phone.get(f"/api/checkins/{r.json()['photo_id']}")).json()
    assert c["status"] == "failed" and c["error"] == "could not analyse this photo"
    assert c["garments"] == []
    assert len([x for x in services.llm.calls if x.purpose == "checkin_vision"]) == 1
    job = await db.fetchrow("SELECT status, attempts FROM jobs WHERE type = 'pipeline.checkin'")
    assert job["status"] == "failed" and job["attempts"] == 1
    assert services.segmenter is None or services.segmenter.requests == 0


async def test_no_garments(phone, services):
    services.llm.push({"image_usable": False, "garments": []})
    r = await upload(phone, synth.person_jpeg(), path="/api/checkins/upload")
    await settle(services)
    c = (await phone.get(f"/api/checkins/{r.json()['photo_id']}")).json()
    assert c["status"] == "done" and c["garments"] == []


async def test_face_blur_on_without_blurrer_stores_and_sends_nothing(phone, services, storage):
    services.settings.face_blur = True
    r = await upload(phone, synth.person_jpeg(), path="/api/checkins/upload")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "face_blur_unavailable"
    assert await storage.list("") == []
    assert await services.db.fetchval("SELECT count(*) FROM photos") == 0
    assert services.llm.calls == []
    # onboarding photos (garments only) are not affected by FACE_BLUR
    assert (await upload(phone, synth.garment_jpeg())).status_code == 201


async def test_face_blur_on_with_a_blurrer(phone, services, storage):
    class Blur:
        name = "test"

        async def blur(self, jpeg):
            return jpeg + b""  # stand-in: a real blurrer returns a new JPEG

    services.settings.face_blur = True
    services.face_blurrer = Blur()
    r = await upload(phone, synth.person_jpeg(), path="/api/checkins/upload")
    assert r.status_code == 201
    pid = r.json()["photo_id"]
    row = await services.db.fetchrow("SELECT * FROM photos WHERE id = %s", (pid,))
    assert row["face_blurred"] is True
    assert row["storage_key"] == f"photos/{pid}/blurred.jpg"
    assert row["original_key"] == f"photos/{pid}/original.jpg"
    await settle(services)
    assert (await phone.get(f"/api/checkins/{pid}")).json()["status"] == "done"


async def test_same_file_as_onboarding_and_checkin_conflicts(phone, services):
    data = synth.garment_jpeg("red", "tee")
    assert (await upload(phone, data)).status_code == 201
    r = await upload(phone, data, path="/api/checkins/upload")
    assert r.status_code == 409
