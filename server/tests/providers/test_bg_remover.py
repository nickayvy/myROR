from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from providers.bg_remover import VARIANTS, FakeBgRemover, FalBgRemover, build_bg_remover
from providers.bg_remover.base import BgRemovalError
from providers.bg_remover.fal import build_arguments
from providers.fal import FalClient
from tests.pipeline import synth
from tests.providers.fal_mock import FakeSpend, FalMock, assert_private


def fal_for(mock: FalMock, storage=None, mode="data_uri") -> FalClient:
    return FalClient("k", FakeSpend(), storage=storage, input_mode=mode,
                     transport=mock.transport, retry_delay_s=0)


async def test_fake_makes_a_cutout_from_near_white():
    jpeg = synth.garment_jpeg("navy", "tee")
    res = await FakeBgRemover().remove(data=jpeg)
    im = Image.open(io.BytesIO(res.png))
    assert im.mode == "RGBA" and im.size == (1600, 1200)
    a = im.getchannel("A")
    assert a.getpixel((10, 10)) == 0  # sheet is transparent
    assert a.getpixel((800, 600)) == 255  # garment is opaque


@pytest.mark.parametrize("name", sorted(VARIANTS))
async def test_every_variant_round_trips(name):
    endpoint, options = VARIANTS[name]
    mock = FalMock()
    bg = FalBgRemover(fal_for(mock), endpoint, options)
    res = await bg.remove(data=synth.garment_jpeg("red", "tee"))
    assert Image.open(io.BytesIO(res.png)).mode == "RGBA"
    assert res.meta["sync_data_uri"] is True
    body = mock.bodies[0]
    assert body["image_url"].startswith("data:image/jpeg;base64,")
    if endpoint == "fal-ai/birefnet/v2":
        assert body["output_format"] == "png" and body["refine_foreground"] is True
        assert body["model"] == options["model"]
    if endpoint == "pixelcut/background-removal":
        assert body["output_format"] == "rgba"
    assert_private(mock)


async def test_presigned_input_is_fetchable(storage):
    jpeg = synth.garment_jpeg("green", "tee")
    await storage.put("photos/p1/original.jpg", jpeg, "image/jpeg")
    mock = FalMock()
    bg = FalBgRemover(fal_for(mock, storage, "presign"))
    await bg.remove(data=jpeg, key="photos/p1/original.jpg")
    assert mock.fetched_inputs == ["presigned"]
    assert "X-Amz-Expires=300" in mock.bodies[0]["image_url"]
    assert_private(mock)


def test_birefnet_option_validation():
    with pytest.raises(ValueError, match="Dynamic"):
        build_arguments("fal-ai/birefnet/v2", "u", {"model": "Matting",
                                                    "operating_resolution": "2304x2304"})
    with pytest.raises(ValueError):
        build_arguments("fal-ai/birefnet/v2", "u", {"model": "Nope"})


async def test_output_without_alpha_is_rejected():
    def handler(req):
        out = io.BytesIO()
        Image.new("RGB", (10, 10)).save(out, "PNG")
        import base64

        return httpx.Response(200, json={"image": {
            "url": "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()}})

    fal = FalClient("k", FakeSpend(), transport=httpx.MockTransport(handler), input_mode="data_uri")
    with pytest.raises(BgRemovalError, match="alpha"):
        await FalBgRemover(fal).remove(data=synth.garment_jpeg())


async def test_hosted_output_fallback_is_fetched_and_flagged():
    mock = FalMock(hosted_output=True)
    res = await FalBgRemover(fal_for(mock)).remove(data=synth.garment_jpeg())
    assert res.meta["sync_data_uri"] is False
    assert any(r.url.host == "v3.fal.media" for r in mock.requests)
    assert_private(mock)


def test_build_from_settings(settings):
    assert isinstance(build_bg_remover(settings.model_copy(update={"bg_remover": "fake"}), None),
                      FakeBgRemover)
    bg = build_bg_remover(settings.model_copy(update={"bg_remover": "fal"}), object())
    assert isinstance(bg, FalBgRemover) and bg.endpoint == "fal-ai/birefnet/v2"
    assert bg.options == {"model": "General Use (Light)", "operating_resolution": "1024x1024"}
