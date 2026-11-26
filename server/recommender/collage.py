"""Outfit collage for the mirror: item cutouts on one light card, arranged by slot.

Portrait 1080 x 1440, sized for a 1080-wide mirror region. The whole image is a light
card (dark pixels read as mirror glass, HANDOFF section 11). Main column: top (or a
one-piece spanning top and bottom), bottom, shoes. Side column: outer layer, a second
(mid-layer) top, accessories. An item without a cutout gets a labelled placeholder.
Pillow only; about 100-300 ms per collage on a Pi 4 [UNTESTED].
"""

from __future__ import annotations

import asyncio
import io
import logging
import textwrap
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from recommender.closet import ClosetItem
from recommender.outfit import sort_items
from storage import Storage, StorageError

log = logging.getLogger(__name__)

W, H = 1080, 1440
PAD = 40
GAP = 24
BG = (246, 243, 238)
PLACEHOLDER_BG = (232, 228, 221)
PLACEHOLDER_LINE = (190, 184, 175)
LABEL = (96, 90, 84)
TEXT = (60, 56, 52)
LABEL_H = 34
JPEG_QUALITY = 88

Box = tuple[int, int, int, int]  # x0, y0, x1, y1


def collage_key(for_date: date, outfit_id: UUID) -> str:
    return f"collages/{for_date.isoformat()}/{outfit_id}.jpg"


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # noqa: BLE001 - Pillow without FreeType
        return ImageFont.load_default()


def layout(items: Sequence[ClosetItem]) -> list[tuple[ClosetItem, Box]]:
    """Assign each item a box. Pure, so it is testable without images."""
    items = sort_items(items)
    by = {s: [i for i in items if i.slot == s] for s in
          ("outer", "top", "one_piece", "bottom", "shoes", "accessory")}
    tops = by["top"]
    base = [t for t in tops if t.layer_role == "base"]
    mid = [t for t in tops if t.layer_role == "mid"]
    # a layer over a one-piece goes to the side column
    main_top = [] if by["one_piece"] else (base or mid)[:1]
    side_top = [t for t in tops if t not in main_top]
    side = by["outer"] + side_top + by["accessory"]

    main_x0, main_x1 = PAD, (W - PAD if not side else 640)
    side_x0, side_x1 = 640 + GAP, W - PAD
    out: list[tuple[ClosetItem, Box]] = []

    # main column, top to bottom
    y = PAD
    shoes_h = 260 if by["shoes"] else 0
    body_h = H - PAD - y - (shoes_h + GAP if shoes_h else 0)
    if by["one_piece"]:
        out.append((by["one_piece"][0], (main_x0, y, main_x1, y + body_h)))
    else:
        half = (body_h - GAP) // 2
        if main_top:
            out.append((main_top[0], (main_x0, y, main_x1, y + half)))
        if by["bottom"]:
            out.append((by["bottom"][0], (main_x0, y + half + GAP, main_x1, y + body_h)))
    if by["shoes"]:
        sy = H - PAD - shoes_h
        out.append((by["shoes"][0], (main_x0, sy, main_x1, H - PAD)))

    # side column: big boxes for garments, small ones for accessories
    if side:
        garments = [i for i in side if i.slot != "accessory"]
        acc = [i for i in side if i.slot == "accessory"]
        avail = H - 2 * PAD
        acc_h = 0 if not acc else min(240 * len(acc), avail // 2 if garments else avail)
        g_h = avail - (acc_h + GAP if acc and garments else acc_h)
        y = PAD
        if garments:
            each = (g_h - GAP * (len(garments) - 1)) // len(garments)
            for g in garments:
                out.append((g, (side_x0, y, side_x1, y + each)))
                y += each + GAP
        if acc:
            each = (acc_h - GAP * (len(acc) - 1)) // len(acc)
            for a in acc:
                out.append((a, (side_x0, y, side_x1, y + each)))
                y += each + GAP
    return out


def _fit_cutout(png: bytes, box_w: int, box_h: int) -> Image.Image:
    im = Image.open(io.BytesIO(png))
    im.load()
    im = im.convert("RGBA")
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    scale = min(box_w / im.width, box_h / im.height)
    size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
    return im.resize(size, Image.Resampling.LANCZOS)


def _shadow(im: Image.Image) -> Image.Image:
    alpha = im.getchannel("A").point(lambda a: a * 0.22)
    sh = Image.new("RGBA", (im.width + 40, im.height + 40), (0, 0, 0, 0))
    sh.paste((60, 50, 40, 255), (20, 26), alpha)
    return sh.filter(ImageFilter.GaussianBlur(10))


def _label(draw: ImageDraw.ImageDraw, text: str, x0: int, x1: int, y: int, font) -> None:
    """Centred under the cutout (not at the bottom of a tall box)."""
    text = text if len(text) <= 40 else text[:39] + "…"
    tw = draw.textlength(text, font=font)
    draw.text(((x0 + x1 - tw) / 2, y), text, fill=LABEL, font=font)


def _placeholder(draw: ImageDraw.ImageDraw, item: ClosetItem, box: Box, fonts) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0 + 8, y0 + 8, x1 - 8, y1 - 8), radius=28, fill=PLACEHOLDER_BG,
                           outline=PLACEHOLDER_LINE, width=3)
    big, small = fonts
    width_chars = max(8, int((x1 - x0 - 60) / (big.size * 0.55))) if hasattr(big, "size") else 20
    lines = textwrap.wrap(item.name, width=width_chars)[:3]
    sub = (item.subcategory or item.category or "").replace("_", " ")
    line_h = (big.size if hasattr(big, "size") else 12) + 10
    total = line_h * len(lines) + 40
    y = (y0 + y1 - total) / 2
    for line in lines:
        tw = draw.textlength(line, font=big)
        draw.text(((x0 + x1 - tw) / 2, y), line, fill=TEXT, font=big)
        y += line_h
    tw = draw.textlength(sub, font=small)
    draw.text(((x0 + x1 - tw) / 2, y + 8), sub, fill=LABEL, font=small)


def render_collage(items: Sequence[ClosetItem], cutouts: dict[UUID, bytes]) -> bytes:
    """JPEG bytes. `cutouts` maps item id -> PNG with alpha; missing ids get placeholders."""
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    label_font, big, small = _font(26), _font(40), _font(28)
    for item, box in layout(items):
        x0, y0, x1, y1 = box
        png = cutouts.get(item.id)
        im = None
        if png:
            try:
                im = _fit_cutout(png, x1 - x0 - 40, y1 - y0 - LABEL_H - 40)
            except Exception as e:  # noqa: BLE001 - a corrupt cutout gets a placeholder
                log.warning("collage: bad cutout for %s: %s", item.id, e)
        if im is None:
            _placeholder(draw, item, box, (big, small))
            continue
        px = x0 + (x1 - x0 - im.width) // 2
        py = y0 + (y1 - y0 - LABEL_H - im.height) // 2
        sh = _shadow(im)
        canvas.paste(sh, (px - 20, py - 20), sh)
        canvas.paste(im, (px, py), im)
        _label(draw, item.name, x0, x1, py + im.height + 10, label_font)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


async def build_collage(storage: Storage, for_date: date, outfit_id: UUID,
                        items: Sequence[ClosetItem]) -> str:
    """Render and store; returns the storage key."""
    cutouts: dict[UUID, bytes] = {}
    for it in items:
        if it.cutout_key:
            try:
                cutouts[it.id] = await storage.get(it.cutout_key)
            except (StorageError, KeyError, ValueError) as e:
                log.info("collage: no cutout for %s (%s)", it.id, e)
    data = await asyncio.to_thread(render_collage, items, cutouts)
    key = collage_key(for_date, outfit_id)
    await storage.put(key, data, "image/jpeg")
    return key
