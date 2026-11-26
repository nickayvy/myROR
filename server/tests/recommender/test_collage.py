"""Collage layout and rendering (Pillow), stored under collages/."""

from __future__ import annotations

import io

from PIL import Image

from recommender.collage import H, W, build_collage, collage_key, layout, render_collage
from tests.recommender.conftest import DAY, cutout_png, wardrobe_items


def by_name(*names):
    items = {i.name: i for i in wardrobe_items()}
    return [items[n] for n in names]


def overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def test_layout_boxes_inside_and_disjoint():
    for names in [
        ("white crew tee", "navy cardigan", "dark wash jeans", "black leather boots",
         "camel wool coat", "grey wool scarf", "brown leather belt"),
        ("black shift dress", "navy cardigan", "black oxfords", "navy blazer"),
        ("white crew tee", "dark wash jeans"),
        ("black shift dress",),
    ]:
        boxes = layout(by_name(*names))
        assert len(boxes) == len(names)
        for _, b in boxes:
            assert 0 <= b[0] < b[2] <= W and 0 <= b[1] < b[3] <= H
        for i, (_, a) in enumerate(boxes):
            for _, b in boxes[i + 1:]:
                assert not overlap(a, b), names


def test_render_light_card_with_cutouts_and_placeholder():
    items = by_name("white crew tee", "dark wash jeans", "black leather boots",
                    "camel wool coat", "brown leather belt")
    cutouts = {i.id: cutout_png(i) for i in items if i.name != "brown leather belt"}
    data = render_collage(items, cutouts)
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG" and im.size == (W, H)
    grey = im.convert("L")
    # light card: corners and overall mean are bright (dark reads as mirror glass)
    for xy in [(5, 5), (W - 5, 5), (5, H - 5), (W - 5, H - 5)]:
        assert grey.getpixel(xy) > 220
    assert sum(grey.get_flattened_data()) / (W * H) > 150
    # the navy jeans cutout is drawn in the main column, lower half
    px = im.convert("RGB").getpixel((340, 680))
    assert px[2] > px[0] + 20, px


def test_corrupt_cutout_gets_placeholder():
    items = by_name("white crew tee", "dark wash jeans")
    data = render_collage(items, {items[0].id: b"not a png"})
    assert Image.open(io.BytesIO(data)).size == (W, H)


async def test_build_collage_stores_under_collages(storage):
    items = by_name("white crew tee", "dark wash jeans")
    for i in items:
        await storage.put(i.cutout_key, cutout_png(i), "image/png")
    import uuid
    oid = uuid.uuid4()
    key = await build_collage(storage, DAY, oid, items)
    assert key == collage_key(DAY, oid) == f"collages/2026-11-25/{oid}.jpg"
    assert (await storage.get(key))[:3] == b"\xff\xd8\xff"
