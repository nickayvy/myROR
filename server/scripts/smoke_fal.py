"""Two real fal calls on synthetic images, for the admin to run once FAL_KEY exists.

    cd server && uv run python -m scripts.smoke_fal                # presigned input
    cd server && uv run python -m scripts.smoke_fal --input data_uri

1. Background removal (FAL_BG_ENDPOINT, default fal-ai/birefnet/v2) on a generated red
   shirt shape on a white field.
2. One SAM request (FAL_SEG_ENDPOINT) with two boxes on a generated stick figure (red top,
   navy trousers).
No personal photo is used. Inputs are uploaded to the private bucket under smoke/ and
passed to fal as presigned links (or inline data URIs), then deleted. Both calls go through
the spend cap and are recorded in api_spend, so DATABASE_URL is required. Expected cost
under a cent (SAM 3 $0.005 per request; BiRefNet's real price unknown).
Prints what came back, whether the presigned-link input worked, and the recorded spend.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import uuid

import numpy as np
from PIL import Image, ImageDraw

from db import Database
from mirror_api.config import Settings
from mirror_api.spend import SpendLedger
from providers.bg_remover import build_bg_remover
from providers.fal import FalError, build_fal
from providers.segmenter import Box, FalSamSegmenter
from storage import build_storage


def shirt_jpeg() -> bytes:
    im = Image.new("RGB", (1024, 768), (244, 243, 238))
    ImageDraw.Draw(im).polygon([(380, 160), (644, 160), (800, 260), (740, 330), (660, 290),
                                (660, 640), (364, 640), (364, 290), (284, 330), (224, 260)],
                               fill=(200, 30, 40))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def figure_jpeg() -> tuple[bytes, list[Box]]:
    w, h = 768, 1024
    im = Image.new("RGB", (w, h), (150, 160, 170))
    d = ImageDraw.Draw(im)
    d.ellipse([310, 60, 458, 240], fill=(205, 160, 130))
    top, bottom = Box(230, 280, 538, 740), Box(246, 740, 522, 1024)
    d.rectangle([top.x_min, top.y_min, top.x_max, top.y_max], fill=(200, 30, 40))
    d.rectangle([bottom.x_min, bottom.y_min, bottom.x_max, bottom.y_max], fill=(25, 35, 90))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue(), [top, bottom]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", choices=["presign", "data_uri"], default=None,
                   help="default: FAL_INPUT_MODE")
    args = p.parse_args()
    s = Settings()
    if args.input:
        s.fal_input_mode = args.input
    if not s.fal_key:
        print("FAL_KEY is not set in .env", file=sys.stderr)
        return 2
    if not s.database_url:
        print("DATABASE_URL is required so the calls are recorded in api_spend", file=sys.stderr)
        return 2
    storage = build_storage(s)
    if storage is None and s.fal_input_mode == "presign":
        print("storage not configured; using data_uri input")
        s.fal_input_mode = "data_uri"
    db = Database(s.database_url, max_size=1)
    await db.open()
    spend = SpendLedger.from_settings(db, s)
    fal = build_fal(s, spend, storage)
    run = uuid.uuid4().hex[:8]
    keys = [f"smoke/{run}/shirt.jpg", f"smoke/{run}/figure.jpg"]
    shirt = shirt_jpeg()
    figure, boxes = figure_jpeg()
    status = 0
    try:
        before = await spend.status()
        print(f"ledger={before.ledger} spent=${before.spent_usd:.4f} of ${before.cap_usd:.2f}"
              f"  input={s.fal_input_mode}")
        if storage is not None:
            await storage.put(keys[0], shirt, "image/jpeg")
            await storage.put(keys[1], figure, "image/jpeg")
        bg = build_bg_remover(s.model_copy(update={"bg_remover": "fal"}), fal)
        try:
            res = await bg.remove(data=shirt, key=keys[0] if storage else None,
                                  purpose="smoke_test")
            im = Image.open(io.BytesIO(res.png))
            alpha = np.asarray(im.getchannel("A"))
            print(f"[bg] {res.model}: {im.mode} {im.size}, returned as data URI: "
                  f"{res.meta.get('sync_data_uri')}, opaque share {float((alpha > 128).mean()):.3f}"
                  f" (shirt is about 0.33), corner alpha {int(alpha[5, 5])}")
        except FalError as e:
            status = 1
            print(f"[bg] FAILED {type(e).__name__}: {e}")
        seg = FalSamSegmenter(fal, s.fal_seg_endpoint, timeout_s=s.fal_seg_timeout_s)
        try:
            masks = await seg.segment(data=figure, key=keys[1] if storage else None,
                                      boxes=boxes, labels=["red top", "navy trousers"],
                                      purpose="smoke_test")
            for name, m in zip(("top", "trousers"), masks, strict=True):
                print(f"[sam] {name}: method={m.method} IoU with box={m.iou:.3f} "
                      f"mask box={m.mask_box}")
            print(f"[sam] requests made: {seg.requests} (1 means multi-box mapped cleanly)")
        except FalError as e:
            status = 1
            print(f"[sam] FAILED {type(e).__name__}: {e}")
        if s.fal_input_mode == "presign":
            verdict = "worked" if status == 0 else "FAILED - try --input data_uri"
            print(f"presigned-link input: {verdict}")
        after = await spend.status()
        rows = await db.fetch("SELECT model, purpose, est_usd FROM api_spend WHERE provider ="
                              " 'fal' ORDER BY id DESC LIMIT 4")
        print(f"recorded spend this run: ${after.spent_usd - before.spent_usd:.6f}")
        for r in rows:
            print(f"  {r['model']:<32} {r['purpose']:<12} ${float(r['est_usd']):.6f}")
        return status
    finally:
        if storage is not None:
            for k in keys:
                await storage.delete(k)
            await storage.close()
        await fal.close()
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
