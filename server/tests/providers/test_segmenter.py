from __future__ import annotations

import io

import numpy as np
from PIL import Image

from providers.fal import FalClient
from providers.segmenter import Box, FakeSegmenter, FalSamSegmenter, assign
from tests.pipeline import synth
from tests.providers.fal_mock import FakeSpend, FalMock, assert_private

W, H = 1200, 1600
TOP = Box(360, 448, 840, 1152)
BOTTOM = Box(384, 1152, 816, 1600)


def seg(mock: FalMock, spend=None) -> FalSamSegmenter:
    fal = FalClient("k", spend or FakeSpend(), input_mode="data_uri", transport=mock.transport,
                    retry_delay_s=0)
    return FalSamSegmenter(fal)


def mask_of(m) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(m.mask_png))) > 127


async def run(mock, boxes=(TOP, BOTTOM), labels=("red tee", "navy trousers")):
    s = seg(mock)
    out = await s.segment(data=synth.person_jpeg(), key=None, boxes=list(boxes),
                          labels=list(labels))
    return s, out


async def test_multi_box_one_request_maps_by_iou_not_metadata():
    mock = FalMock()  # metadata sorted by score with wrong indexes
    s, out = await run(mock)
    assert s.requests == 1 and len(mock.bodies) == 1
    body = mock.bodies[0]
    assert body["prompt"] == "clothing" and len(body["box_prompts"]) == 2
    assert all("object_id" not in b for b in body["box_prompts"])
    assert [m.method for m in out] == ["multi", "multi"]
    assert out[0].iou > 0.9 and out[1].iou > 0.9
    # the top mask is red pixels, the bottom mask navy
    rgb = np.asarray(Image.open(io.BytesIO(synth.person_jpeg())).convert("RGB"))
    for m, color in zip(out, ("red", "navy"), strict=True):
        px = rgb[mask_of(m)]
        assert np.linalg.norm(px.mean(axis=0) - synth.PALETTE[color]) < 30
    assert_private(mock)


async def test_shuffled_masks_and_an_extra_object_still_map():
    mock = FalMock(shuffle_masks=True, extra_mask=True)
    s, out = await run(mock)
    assert s.requests == 1
    assert [m.method for m in out] == ["multi", "multi"]
    assert out[0].mask_box.y_min < out[1].mask_box.y_min  # top stays top


async def test_ambiguous_multi_falls_back_per_box():
    mock = FalMock(merge_first_two=True)  # one mask spanning both boxes
    s, out = await run(mock)
    assert s.requests == 3  # one multi + one per box
    assert [m.method for m in out] == ["per_box", "per_box"]
    assert [b["prompt"] for b in mock.bodies[1:]] == ["red tee", "navy trousers"]
    assert_private(mock)


async def test_box_fallback_when_nothing_usable():
    mock = FalMock()
    s, out = await run(mock, boxes=[Box(0, 0, 40, 40)], labels=["hat"])  # wall only
    # colour mask of the wall corner is the whole box, so per-box returns it; make it empty:
    assert out[0].method in ("per_box", "box")
    mock2 = FalMock()
    mock2._sam = lambda data, body: {"masks": []}  # type: ignore[method-assign]
    s2, out2 = await run(mock2, boxes=[TOP], labels=["tee"])
    assert out2[0].method == "box" and out2[0].iou == 1.0


def _m(box: Box, size=(400, 200)) -> np.ndarray:
    from providers.segmenter.base import box_mask

    return box_mask(box, size)


def test_assign_rules():
    a, b = Box(0, 0, 100, 100), Box(200, 0, 300, 100)
    assert assign([a, b], [_m(b), _m(a)]) == {0: 1, 1: 0}
    assert assign([a], [np.zeros((200, 400), bool), _m(Box(0, 0, 10, 10))]) == {}  # too small
    assert assign([a], [_m(Box(0, 0, 100, 98)), _m(Box(0, 0, 99, 100))]) == {}  # ambiguous
    assert assign([a, b], [_m(a) | _m(b)]) == {}  # one mask spanning two garments
    jacket, shirt = Box(0, 0, 300, 200), Box(100, 0, 200, 200)  # nested: still fine
    jm = _m(jacket) & ~_m(shirt)
    assert assign([jacket, shirt], [_m(shirt), jm]) == {0: 1, 1: 0}


async def test_fake_segmenter():
    out = await FakeSegmenter().segment(data=synth.person_jpeg(), key=None,
                                        boxes=[TOP, BOTTOM], labels=["", ""])
    assert [m.method for m in out] == ["fake", "fake"]
    assert out[0].iou > 0.9
