"""The whole pipeline (onboarding and a check-in) with the real fal providers on a mocked
transport: every request carries X-Fal-Store-IO: 0 and sync_mode: true, goes only to
fal.run's run endpoint (never an upload endpoint), and the inputs are presigned links from
our bucket (or data URIs in data_uri mode)."""

from __future__ import annotations

import pytest

from providers.bg_remover import FalBgRemover
from providers.fal import FalClient
from providers.segmenter import FalSamSegmenter
from tests.pipeline import synth
from tests.pipeline.conftest import settle, upload
from tests.providers.fal_mock import FalMock, assert_private


@pytest.mark.parametrize("mode", ["presign", "data_uri"])
async def test_pipeline_through_fal_is_private(phone, services, mode):
    mock = FalMock()
    fal = FalClient("test-key", services.spend, storage=services.storage, input_mode=mode,
                    transport=mock.transport, retry_delay_s=0)
    services.fal = fal
    services.bg_remover = FalBgRemover(fal)
    services.segmenter = FalSamSegmenter(fal)
    await upload(phone, synth.garment_jpeg("red", "tee"))
    await settle(services)
    (entry,) = (await phone.get("/api/inbox")).json()["entries"]
    assert entry["cutout_url"] and entry["tagging"] == "ok"
    await phone.post(f"/api/inbox/{entry['id']}/confirm")
    r = await upload(phone, synth.person_jpeg("red", "navy"), path="/api/checkins/upload")
    await settle(services)
    c = (await phone.get(f"/api/checkins/{r.json()['photo_id']}")).json()
    assert c["status"] == "done" and [g["segmentation"] for g in c["garments"]] == \
        ["multi", "multi"]
    assert_private(mock)
    assert len(mock.bodies) == 2  # one background removal, one SAM request for both boxes
    assert set(mock.fetched_inputs) == {"presigned" if mode == "presign" else "data_uri"}
    if mode == "presign":
        for b in mock.bodies:
            assert "X-Amz-Expires=300" in b["image_url"]
    spent = await services.db.fetch("SELECT provider, model, purpose FROM api_spend"
                                    " WHERE provider = 'fal' ORDER BY id")
    assert [(s["model"], s["purpose"]) for s in spent] == [
        ("fal-ai/birefnet/v2", "bg_remove"), ("fal-ai/sam-3/image", "checkin_segment")]


async def test_retry_after_fal_outage_does_not_double_charge(phone, services, db):
    mock = FalMock(fail_times=2)  # first attempt and its one retry both fail
    fal = FalClient("k", services.spend, storage=services.storage, transport=mock.transport,
                    retry_delay_s=0)
    services.fal, services.bg_remover = fal, FalBgRemover(fal)
    r = await upload(phone, synth.garment_jpeg("green", "tee"))
    await settle(services)
    st = (await phone.get(f"/api/photos/{r.json()['photo_id']}")).json()
    assert st["status"] == "queued" and st["step"] == "retrying"
    assert await db.fetchval("SELECT count(*) FROM api_spend WHERE provider = 'fal'") == 0
    # run the queued retry now
    await db.execute("UPDATE jobs SET run_after = now()")
    services.jobs.wake()
    await settle(services)
    st = (await phone.get(f"/api/photos/{r.json()['photo_id']}")).json()
    assert st["status"] == "done"
    assert await db.fetchval("SELECT count(*) FROM api_spend WHERE provider = 'fal'") == 1


async def test_fal_4xx_fails_photo_permanently(phone, services, db):
    mock = FalMock(fail_times=5, fail_status=422)
    fal = FalClient("k", services.spend, storage=services.storage, transport=mock.transport)
    services.fal, services.bg_remover = fal, FalBgRemover(fal)
    r = await upload(phone, synth.garment_jpeg("green", "tee"))
    await settle(services)
    st = (await phone.get(f"/api/photos/{r.json()['photo_id']}")).json()
    assert st["status"] == "failed" and "422" in st["error"]
    assert len(mock.bodies) == 1
    # the user can retry once the problem is fixed
    mock.fail_times = 0
    assert (await phone.post(f"/api/photos/{r.json()['photo_id']}/retry")).status_code == 202
    await settle(services)
    assert (await phone.get(f"/api/photos/{r.json()['photo_id']}")).json()["status"] == "done"
