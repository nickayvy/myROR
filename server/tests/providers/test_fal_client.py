from __future__ import annotations

import base64
import json

import httpx
import pytest

from providers.fal import (
    FalClient,
    FalNotConfigured,
    FalRequestError,
    FalResponseError,
    FalUnavailable,
    data_uri,
    decode_data_uri,
    fal_price,
)
from tests.providers.fal_mock import FakeSpend


def client(handler, spend=None, **kw) -> FalClient:
    return FalClient("test-key", spend or FakeSpend(), transport=httpx.MockTransport(handler),
                     retry_delay_s=0, **kw)


async def test_headers_sync_mode_and_spend():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"image": {"url": "data:image/png;base64,AAAA"}})

    spend = FakeSpend()
    fal = client(handler, spend)
    out = await fal.run("fal-ai/birefnet/v2", {"image_url": "x", "sync_mode": False},
                        purpose="bg_remove")
    assert out["image"]["url"].startswith("data:")
    (req,) = seen
    assert str(req.url) == "https://fal.run/fal-ai/birefnet/v2"
    assert req.headers["authorization"] == "Key test-key"
    assert req.headers["x-fal-store-io"] == "0"
    assert json.loads(req.content)["sync_mode"] is True  # a caller cannot turn it off
    assert spend.checks == ["bg_remove"]
    assert spend.records[0]["provider"] == "fal"
    assert spend.records[0]["model"] == "fal-ai/birefnet/v2"
    assert spend.records[0]["est_usd"] == fal_price("fal-ai/birefnet/v2")


async def test_one_retry_on_transient_then_success():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, json={"ok": True})

    spend = FakeSpend()
    assert await client(handler, spend).run("fal-ai/feynobg", {}, purpose="p") == {"ok": True}
    assert len(calls) == 2
    assert len(spend.records) == 1 and spend.records[0]["units"]["attempt"] == 2


async def test_gives_up_after_one_retry():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ConnectError("down", request=req)

    spend = FakeSpend()
    with pytest.raises(FalUnavailable):
        await client(handler, spend).run("fal-ai/feynobg", {}, purpose="p")
    assert len(calls) == 2
    assert spend.records == []  # nothing recorded for a failed call


async def test_4xx_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(422, json={"detail": [{"msg": "bad image_url"}]})

    with pytest.raises(FalRequestError) as e:
        await client(handler).run("fal-ai/feynobg", {}, purpose="p")
    assert e.value.status == 422 and "bad image_url" in e.value.detail
    assert len(calls) == 1


async def test_not_configured_and_blocked_spend_send_nothing():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    fal = FalClient("", FakeSpend(), transport=httpx.MockTransport(handler))
    with pytest.raises(FalNotConfigured):
        await fal.run("fal-ai/feynobg", {}, purpose="p")
    from mirror_api.spend import SpendCapExceeded

    with pytest.raises(SpendCapExceeded):
        await client(handler, FakeSpend(blocked=True)).run("fal-ai/feynobg", {}, purpose="p")
    assert calls == []


async def test_bad_json_and_bad_endpoint():
    fal = client(lambda req: httpx.Response(200, content=b"<html>"))
    with pytest.raises(FalResponseError):
        await fal.run("fal-ai/feynobg", {}, purpose="p")
    with pytest.raises(Exception, match="bad fal endpoint"):
        await fal.run("https://evil.example/x", {}, purpose="p")


async def test_image_input_modes(local_storage):
    await local_storage.put("photos/a/original.jpg", b"jpegbytes")
    fal = client(lambda r: httpx.Response(200), storage=local_storage)
    url = await fal.image_input(data=b"jpegbytes", key="photos/a/original.jpg")
    assert "photos/a/original.jpg" in url and "sig=" in url  # presigned, short-lived
    fal.input_mode = "data_uri"
    uri = await fal.image_input(data=b"jpegbytes", key="photos/a/original.jpg")
    assert decode_data_uri(uri) == (b"jpegbytes", "image/jpeg")
    uri = await fal.image_input(data=None, key="photos/a/original.jpg")
    assert decode_data_uri(uri)[0] == b"jpegbytes"


async def test_fetch_output_only_from_fal_hosts():
    payload = b"\x89PNG..."

    def handler(req):
        return httpx.Response(200, content=payload)

    fal = client(handler)
    uri = "data:image/png;base64," + base64.b64encode(payload).decode()
    assert await fal.fetch_output(uri) == payload and fal.last_output_was_data_uri
    assert await fal.fetch_output("https://v3.fal.media/files/a.png") == payload
    assert fal.last_output_was_data_uri is False
    for bad in ("http://v3.fal.media/a.png", "https://example.com/a.png",
                "https://fal.media.evil.com/a.png"):
        with pytest.raises(FalResponseError):
            await fal.fetch_output(bad)


def test_data_uri_roundtrip():
    assert decode_data_uri(data_uri(b"abc", "image/png")) == (b"abc", "image/png")
    with pytest.raises(FalResponseError):
        decode_data_uri("https://x")


async def test_real_spend_ledger_records(db, settings):
    from mirror_api.spend import SpendLedger

    spend = SpendLedger.from_settings(db, settings)
    fal = client(lambda r: httpx.Response(200, json={"masks": []}), spend)
    await fal.run("fal-ai/sam-3/image", {}, purpose="checkin_segment")
    rows = await db.fetch("SELECT provider, model, purpose, est_usd FROM api_spend")
    assert [(r["provider"], r["model"], r["purpose"], float(r["est_usd"])) for r in rows] == [
        ("fal", "fal-ai/sam-3/image", "checkin_segment", 0.005)]
