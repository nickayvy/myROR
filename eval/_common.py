"""Shared helpers for the bake-off scripts: settings/db/storage/fal setup, photo loading,
contact sheets. Run the scripts from server/ so its packages import:

    cd server && uv run python ../eval/bakeoff_bg.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

EVAL_DIR = Path(__file__).resolve().parent
PHOTOS = EVAL_DIR / "photos"
OUTPUTS = EVAL_DIR / "outputs"
SERVER = EVAL_DIR.parent / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
CELL = 320
LABEL_H = 22
COLOURS = [(230, 60, 60), (60, 140, 230), (60, 200, 90), (240, 180, 40), (170, 80, 220),
           (40, 200, 200), (240, 120, 30), (200, 200, 200)]


def list_photos(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def checker(size: tuple[int, int], tile: int = 16) -> Image.Image:
    """Mid-grey checkerboard, so both white and black garment edges show."""
    w, h = size
    im = Image.new("RGB", size, (128, 128, 128))
    d = ImageDraw.Draw(im)
    for y in range(0, h, tile):
        for x in range((y // tile) % 2 * tile, w, tile * 2):
            d.rectangle([x, y, x + tile - 1, y + tile - 1], fill=(150, 150, 150))
    return im


def on_checker(png: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    bg = checker(im.size)
    bg.paste(im, mask=im.getchannel("A"))
    return bg


def fit(im: Image.Image, size: int = CELL) -> Image.Image:
    im = im.copy()
    im.thumbnail((size, size))
    cell = Image.new("RGB", (size, size), (40, 40, 40))
    cell.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return cell


def contact_sheet(rows: list[tuple[str, list[tuple[str, Image.Image | None]]]]) -> Image.Image:
    """rows: (row label, [(column label, image or None for a failure)])."""
    ncols = max(len(cols) for _, cols in rows)
    sheet = Image.new("RGB", (ncols * CELL, len(rows) * (CELL + LABEL_H)), (25, 25, 25))
    d = ImageDraw.Draw(sheet)
    for r, (row_label, cols) in enumerate(rows):
        y = r * (CELL + LABEL_H)
        for c, (label, im) in enumerate(cols):
            x = c * CELL
            if im is not None:
                sheet.paste(fit(im), (x, y + LABEL_H))
            else:
                d.text((x + 10, y + LABEL_H + CELL // 2), "failed", fill=(255, 80, 80))
            d.text((x + 4, y + 4), f"{row_label[:18]} | {label}"[:52], fill=(230, 230, 230))
    return sheet


def overlay(photo: Image.Image, masks: list[bytes], boxes=None) -> Image.Image:
    base = photo.convert("RGBA")
    for i, m in enumerate(masks):
        mask = Image.open(io.BytesIO(m)).convert("L").resize(base.size)
        colour = COLOURS[i % len(COLOURS)]
        layer = Image.new("RGBA", base.size, colour + (0,))
        layer.putalpha(mask.point(lambda v: 110 if v > 127 else 0))
        base = Image.alpha_composite(base, layer)
    d = ImageDraw.Draw(base)
    for i, b in enumerate(boxes or []):
        d.rectangle([b.x_min, b.y_min, b.x_max, b.y_max], outline=COLOURS[i % len(COLOURS)],
                    width=4)
    return base.convert("RGB")


async def setup(input_mode: str | None):
    """Settings from the repo-root .env, an open Database, SpendLedger, storage, FalClient."""
    from db import Database
    from mirror_api.config import Settings
    from mirror_api.spend import SpendLedger
    from providers.fal import build_fal
    from storage import build_storage

    s = Settings()
    if input_mode:
        s.fal_input_mode = input_mode
    if not s.fal_key:
        raise SystemExit("FAL_KEY is not set in .env")
    if not s.database_url:
        raise SystemExit("DATABASE_URL is required: every call is recorded in api_spend")
    storage = build_storage(s)
    if storage is None and s.fal_input_mode == "presign":
        print("storage not configured; using data_uri input")
        s.fal_input_mode = "data_uri"
    db = Database(s.database_url, max_size=1)
    await db.open()
    spend = SpendLedger.from_settings(db, s)
    return s, db, spend, storage, build_fal(s, spend, storage)
