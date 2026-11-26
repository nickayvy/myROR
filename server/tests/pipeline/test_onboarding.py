from __future__ import annotations

import io
from uuid import UUID

import pytest
from PIL import Image

from pipeline.onboarding import run_onboarding
from pipeline.wiring import NO_EMBEDDER
from providers.llm import LLMRefusal
from tests.pipeline import synth
from tests.pipeline.conftest import settle, upload


async def _inbox(phone):
    r = await phone.get("/api/inbox")
    assert r.status_code == 200, r.text
    return r.json()["entries"]


async def test_upload_reaches_inbox_with_tags_and_cutout(phone, services, storage):
    r = await upload(phone, synth.garment_jpeg("red", "tee"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "queued" and body["duplicate_upload"] is False
    pid = body["photo_id"]
    await settle(services)
    st = (await phone.get(f"/api/photos/{pid}")).json()
    assert st["status"] == "done" and st["error"] is None
    assert st["quality_flags"]["white_balance"]["applied"] is True
    (entry,) = await _inbox(phone)
    assert entry["photo_id"] == pid
    assert entry["proposed_tags"]["category"] == "top"
    assert entry["proposed_tags"]["primary_color"] == "red"
    assert entry["tagging"] == "ok" and entry["needs_review"] is False
    assert entry["match"] is None
    assert entry["cutout_url"].startswith("http://minio:9000/") and "X-Amz-Signature" in \
        entry["cutout_url"]
    # a SigV4 link names the access key id (as every presigned S3 link does), never the
    # secret; it expires in 15 minutes
    assert "X-Amz-Expires=900" in entry["cutout_url"]
    assert "Secret" not in entry["cutout_url"] and "Security-Token" not in entry["cutout_url"]
    keys = await storage.list("")
    assert sorted(k.split("/")[0] for k in keys) == ["cutouts", "photos"]
    cut = Image.open(io.BytesIO(await storage.get([k for k in keys if k.startswith("cutouts")][0])))
    assert cut.mode == "RGBA" and cut.getchannel("A").getextrema() == (0, 255)
    # tagger saw a small image
    call = services.llm.calls[0]
    assert call.purpose == "tag_item" and call.model == "claude-haiku-5-5"
    assert call.effort == "low" and call.image_count == 1
    import base64

    thumb = Image.open(io.BytesIO(base64.b64decode(call.content[0]["source"]["data"])))
    assert max(thumb.size) <= 448


async def test_reupload_is_idempotent(phone, services):
    data = synth.garment_jpeg("navy", "trousers")
    first = await upload(phone, data)
    again = await upload(phone, data)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["photo_id"] == first.json()["photo_id"]
    assert again.json()["duplicate_upload"] is True
    await settle(services)
    assert len(await _inbox(phone)) == 1
    assert await services.db.fetchval("SELECT count(*) FROM photos") == 1
    assert await services.db.fetchval("SELECT count(*) FROM jobs") == 1


async def test_upload_limits(phone, services):
    r = await upload(phone, b"%PDF-1.4", name="x.pdf", ctype="application/pdf")
    assert r.status_code == 415
    r = await upload(phone, b"definitely not an image")
    assert r.status_code == 415 and r.json()["detail"]["code"] == "unsupported_type"
    services.settings.upload_max_mb = 0.01
    r = await upload(phone, synth.garment_jpeg())
    assert r.status_code == 413
    r = await phone.post("/api/items/upload")
    assert r.status_code == 422
    assert await services.db.fetchval("SELECT count(*) FROM photos") == 0


async def test_requires_token(papp):
    from tests.conftest import LAN_CLIENT, http_client

    async with http_client(papp, LAN_CLIENT) as anon:
        assert (await upload(anon, synth.garment_jpeg())).status_code == 401
        assert (await anon.get("/api/inbox")).status_code == 401


async def test_invalid_tags_retry_once_with_errors_then_keep_valid_fields(phone, services):
    bad = synth.tag_response("red", "tee")
    # Structured outputs keep every value inside its enum, so taxonomy errors are the
    # cross-field and count rules the schema cannot express.
    bad["tags"]["subcategory"] = "jeans"  # not a top
    bad["tags"]["style_tags"] = ["casual", "sporty", "classic", "preppy", "edgy"]  # > 4
    services.llm.push(bad, bad)
    await upload(phone, synth.garment_jpeg("red", "tee"))
    await settle(services)
    calls = [c for c in services.llm.calls if c.purpose == "tag_item"]
    assert len(calls) == 2
    assert "Problems:" in calls[1].text and "jeans" in calls[1].text
    (entry,) = await _inbox(phone)
    assert entry["tagging"] == "partial" and entry["needs_review"] is True
    assert entry["proposed_tags"]["category"] == "top"
    assert entry["proposed_tags"]["primary_color"] == "red"
    assert "subcategory" not in entry["proposed_tags"]
    assert "style_tags" not in entry["proposed_tags"]
    # confirming without fixing is refused; with the fix it works
    r = await phone.post(f"/api/inbox/{entry['id']}/confirm")
    assert r.status_code == 422 and r.json()["detail"]["errors"]
    r = await phone.post(f"/api/inbox/{entry['id']}/confirm",
                         json={"tags": {"subcategory": "t_shirt", "style_tags": ["casual"]}})
    assert r.status_code == 201, r.text
    assert r.json()["item"]["tags"]["subcategory"] == "t_shirt"


async def test_retry_fixes_tags(phone, services):
    bad = synth.tag_response("red", "tee")
    bad["tags"]["subcategory"] = "sneakers"
    services.llm.push(bad, synth.tag_response("red", "tee"))
    await upload(phone, synth.garment_jpeg("red", "tee"))
    await settle(services)
    (entry,) = await _inbox(phone)
    assert entry["tagging"] == "ok"


async def test_refusal_still_lands_in_inbox(phone, services):
    services.llm.push(LLMRefusal("other", "no"))
    await upload(phone, synth.garment_jpeg("green", "tee"))
    await settle(services)
    (entry,) = await _inbox(phone)
    assert entry["tagging"] == "refused" and entry["needs_review"]
    assert entry["cutout_url"]
    assert len([c for c in services.llm.calls if c.purpose == "tag_item"]) == 1


async def test_low_confidence_flagged(phone, services):
    services.llm.push(synth.tag_response("teal", "tee", confidence="low"))
    await upload(phone, synth.garment_jpeg("teal", "tee"))
    await settle(services)
    (entry,) = await _inbox(phone)
    assert entry["confidence"] == pytest.approx(0.3) and entry["needs_review"] is True


async def test_duplicate_detection_against_closet(phone, services):
    await upload(phone, synth.garment_jpeg("red", "tee"))
    await settle(services)
    (entry,) = await _inbox(phone)
    item = (await phone.post(f"/api/inbox/{entry['id']}/confirm")).json()["item"]
    # the same shirt photographed again, a little shifted: a duplicate
    await upload(phone, synth.garment_jpeg("red", "tee", offset=(50, -30), scale=0.95))
    # a different garment: no match
    await upload(phone, synth.garment_jpeg("yellow", "trousers"))
    await settle(services)
    entries = {e["proposed_tags"]["primary_color"]: e for e in await _inbox(phone)}
    dup = entries["red"]["match"]
    assert dup["item"]["id"] == item["id"] and dup["kind"] == "duplicate"
    assert dup["method"] == "embedding" and dup["score"] >= 0.92
    assert entries["yellow"]["match"] is None
    # bulk confirm skips the likely duplicate unless asked
    r = await phone.post("/api/inbox/confirm", json={"entries": [
        {"id": entries["red"]["id"]}, {"id": entries["yellow"]["id"]}]})
    out = r.json()
    assert [c["id"] for c in out["confirmed"]] == [entries["yellow"]["id"]]
    assert [s["id"] for s in out["skipped"]] == [entries["red"]["id"]]
    # merge it as another photo of the item instead
    r = await phone.post(f"/api/inbox/{entries['red']['id']}/merge",
                         json={"item_id": item["id"]})
    assert r.status_code == 200 and r.json()["item"]["id"] == item["id"]
    assert await _inbox(phone) == []
    assert await services.db.fetchval("SELECT count(*) FROM items") == 2


async def test_without_embedder_tags_match_and_backfill(phone, services):
    services.embedder = NO_EMBEDDER
    await upload(phone, synth.garment_jpeg("navy", "trousers"))
    await settle(services)
    (entry,) = await _inbox(phone)
    item = (await phone.post(f"/api/inbox/{entry['id']}/confirm")).json()["item"]
    assert item["has_embedding"] is False
    await upload(phone, synth.garment_jpeg("navy", "trousers", offset=(20, 20)))
    await settle(services)
    (entry,) = await _inbox(phone)
    assert entry["match"]["method"] == "tags" and entry["match"]["kind"] == "possible"
    assert entry["match"]["item"]["id"] == item["id"]
    # the embedder arrives later: backfill fills items and pending detections
    from providers.embedder import FakeEmbedder

    services.embedder = FakeEmbedder()
    r = await phone.post("/api/items/backfill-embeddings")
    assert r.status_code == 202
    await settle(services)
    assert (await phone.get(f"/api/items/{item['id']}")).json()["has_embedding"] is True
    assert await services.db.fetchval(
        "SELECT count(*) FROM detections WHERE embedding IS NULL AND status = 'pending'") == 0
    assert await services.db.fetchval(
        "SELECT DISTINCT embedding_model FROM items") == "fake-colorshape-v1"


async def test_rerun_does_not_repeat_paid_steps(phone, services):
    r = await upload(phone, synth.garment_jpeg("black", "tee"))
    await settle(services)
    bg_calls = services.bg_remover.calls
    llm_calls = len(services.llm.calls)
    await run_onboarding(services, UUID(r.json()["photo_id"]))
    assert services.bg_remover.calls == bg_calls
    assert len(services.llm.calls) == llm_calls
    assert len(await _inbox(phone)) == 1


async def test_resume_after_crash_mid_chain(phone, services):
    """A crash after background removal: the retry tags but does not remove again."""
    calls = {"n": 0}

    class Boom(Exception):
        pass

    async def flaky_tag(*a, **kw):
        calls["n"] += 1
        raise Boom("crash after bg removal")

    import pipeline.onboarding as onb

    orig = onb.tag_image
    onb.tag_image = flaky_tag
    try:
        r = await upload(phone, synth.garment_jpeg("orange", "tee"))
        await settle(services)
    finally:
        onb.tag_image = orig
    pid = r.json()["photo_id"]
    real = services.bg_remover
    st = (await phone.get(f"/api/photos/{pid}")).json()
    assert st["status"] == "queued" and st["step"] == "retrying"
    assert real.calls == 1
    await run_onboarding(services, UUID(pid))
    assert real.calls == 1  # not removed again
    assert (await _inbox(phone))[0]["tagging"] == "ok"


async def test_spend_cap_defers_without_failing(phone, services, db):
    from decimal import Decimal

    await db.execute("INSERT INTO api_spend (ledger, provider, model, purpose, est_usd)"
                     " VALUES ('dev', 'x', 'x', 'x', %s)", (Decimal("10"),))
    services.llm.spend = services.spend
    r = await upload(phone, synth.garment_jpeg("red", "tee"))
    await settle(services)
    st = (await phone.get(f"/api/photos/{r.json()['photo_id']}")).json()
    assert st["status"] == "queued" and st["step"] == "waiting_for_budget"
    job = await db.fetchrow("SELECT status, attempts, run_after > now() AS later FROM jobs")
    assert job["status"] == "queued" and job["attempts"] == 0 and job["later"]
    assert await _inbox(phone) == []


async def test_reject_and_status_listing(phone, services):
    r = await upload(phone, synth.garment_jpeg("green", "tee"))
    await settle(services)
    (entry,) = await _inbox(phone)
    assert (await phone.post(f"/api/inbox/{entry['id']}/reject")).status_code == 204
    assert (await phone.post(f"/api/inbox/{entry['id']}/reject")).status_code == 409
    assert await _inbox(phone) == []
    listing = (await phone.get("/api/photos", params={"status": "done"})).json()
    assert listing["counts"] == {"done": 1}
    assert listing["photos"][0]["photo_id"] == r.json()["photo_id"]


async def test_progress_events_on_websocket(papp, phone, services):
    from mirror_api.ws import WsClient

    sent = []

    class FakeWs:
        async def send_json(self, msg):
            sent.append(msg)

        async def close(self, code=1000):
            pass

    services.hub.add(WsClient(FakeWs(), "phone"))
    await upload(phone, synth.garment_jpeg("purple", "tee"))
    await settle(services)
    steps = [(m["data"]["status"], m["data"]["step"]) for m in sent
             if m["type"] == "pipeline.photo"]
    assert steps[0] == ("queued", None)
    assert ("processing", "remove_background") in steps and ("processing", "tag") in steps
    assert steps[-1] == ("done", None)
    assert any(m["type"] == "inbox.updated" for m in sent)
