"""Check-in segmentation bake-off (needs FAL_KEY; costs money).

    mkdir -p eval/photos/checkins && cp <waist-up mirror photos> eval/photos/checkins/
    # boxes: either a sidecar <name>.boxes.json next to each photo,
    #   [{"label": "grey sweater", "x_min": 10, "y_min": 20, "x_max": 300, "y_max": 400}, ...]
    #   (pixel coordinates of the photo after EXIF orientation),
    # or --boxes-from claude to get them from the real check-in vision call (~$0.0006 each).
    cd server && uv run python ../eval/bakeoff_seg.py --dry-run
    cd server && uv run python ../eval/bakeoff_seg.py --boxes-from claude

Modes, each a column in eval/outputs/bakeoff_seg_<run>.png (masks overlaid in colour, the
requested boxes outlined):
  sam3-multi    one fal-ai/sam-3/image request with every box (the production plan)
  sam3-perbox   one request per box
  sam31-multi   one fal-ai/sam-3-1/image request with every box
  sam3-text     text prompt only ("clothing"), no boxes, up to 8 masks
Prints requests, cost, time and, for the box modes, how many boxes the multi-box response
mapped cleanly (the rest fall back to per-box requests, which also cost).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
import uuid
from collections import defaultdict

from _common import OUTPUTS, PHOTOS, contact_sheet, list_photos, overlay, setup

MODES = ("sam3-multi", "sam3-perbox", "sam31-multi", "sam3-text")


async def boxes_for(path, prepared, args, services_like):
    from providers.segmenter import Box

    side = path.with_suffix(".boxes.json")
    if side.exists():
        raw = json.loads(side.read_text())
        return [Box.from_dict(b) for b in raw], [b.get("label", "garment") for b in raw]
    if args.boxes_from != "claude":
        raise SystemExit(f"no {side.name}; add one or pass --boxes-from claude")
    from pipeline.checkin import SYSTEM, _box_from_model, vision_schema
    from pipeline.imaging import for_llm
    from providers.llm import image_block, text_block

    s, llm = services_like
    small, scale, w, h = for_llm(prepared.jpeg)
    result = await llm.complete_structured(
        model=s.llm_checkin_model, system=SYSTEM, purpose="bakeoff_seg_vision",
        content=[image_block(small), text_block(f"The image is {w} x {h} pixels. "
                                                "List the garments being worn.")],
        json_schema=vision_schema(), effort="medium", max_tokens=4096)
    boxes, labels = [], []
    for g in result["garments"]:
        b = _box_from_model(g["box"], scale, prepared.width, prepared.height)
        if b:
            boxes.append(b)
            labels.append(g["tags"].get("name", "garment"))
    return boxes, labels


async def main() -> int:
    from PIL import Image

    from pipeline.imaging import prepare_upload
    from providers.fal import FalError, fal_price
    from providers.llm import ClaudeLLM
    from providers.segmenter import SAM3, SAM31, FalSamSegmenter
    from providers.segmenter.base import decode_mask, encode_mask

    p = argparse.ArgumentParser()
    p.add_argument("--modes", nargs="*", default=list(MODES), choices=MODES)
    p.add_argument("--boxes-from", choices=["sidecar", "claude"], default="sidecar")
    p.add_argument("--input", choices=["presign", "data_uri"], default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    folder = PHOTOS / "checkins"
    photos = list_photos(folder)
    if not photos:
        raise SystemExit(f"no images in {folder}")
    per = {"sam3-multi": 0.005, "sam3-perbox": 0.005 * 3, "sam31-multi": 0.01,
           "sam3-text": 0.005}
    est = sum(per[m] for m in args.modes) * len(photos)
    print(f"{len(photos)} photo(s); about ${est:.3f} at listed prices (per-box assumes 3 boxes)")
    if args.dry_run:
        return 0

    s, db, spend, storage, fal = await setup(args.input)
    llm = ClaudeLLM(api_key=s.anthropic_api_key, spend=spend) if args.boxes_from == "claude" \
        else None
    run = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    keys, rows = [], []
    stats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    before = (await spend.status()).spent_usd
    try:
        for path in photos:
            prepared = await asyncio.to_thread(prepare_upload, path.read_bytes(), balance=False)
            photo = Image.open(io.BytesIO(prepared.jpeg))
            key = f"eval/{run}/{path.stem}.jpg"
            if storage is not None:
                await storage.put(key, prepared.jpeg, "image/jpeg")
                keys.append(key)
            boxes, labels = await boxes_for(path, prepared, args, (s, llm))
            cols = [("boxes", overlay(photo, [], boxes))]
            for mode in args.modes:
                t0 = time.monotonic()
                try:
                    if mode == "sam3-text":
                        url = await fal.image_input(data=prepared.jpeg,
                                                    key=key if storage else None)
                        out = await fal.run(SAM3, {
                            "image_url": url, "prompt": "clothing", "apply_mask": False,
                            "return_multiple_masks": True, "max_masks": 8,
                            "include_scores": True, "include_boxes": True,
                            "output_format": "png"}, purpose="bakeoff_seg",
                            timeout_s=s.fal_seg_timeout_s)
                        masks = [encode_mask(decode_mask(await fal.fetch_output(m["url"]),
                                                         photo.size)) for m in out["masks"]]
                        reqs, mapped = 1, None
                        stats[mode]["usd"] += float(fal_price(SAM3))
                    else:
                        endpoint = SAM31 if mode == "sam31-multi" else SAM3
                        seg = FalSamSegmenter(fal, endpoint, timeout_s=s.fal_seg_timeout_s,
                                              multi_box=(mode != "sam3-perbox"))
                        res = await seg.segment(data=prepared.jpeg, key=key if storage else None,
                                                boxes=boxes, labels=labels,
                                                purpose="bakeoff_seg")
                        masks = [m.mask_png for m in res]
                        reqs = seg.requests
                        mapped = sum(1 for m in res if m.method == "multi")
                        stats[mode]["usd"] += float(fal_price(endpoint)) * reqs
                        stats[mode]["mapped"] += mapped
                        stats[mode]["boxes"] += len(boxes)
                    stats[mode]["requests"] += reqs
                    cols.append((f"{mode} r={reqs}", overlay(photo, masks, boxes)))
                except FalError as e:
                    print(f"  {path.name} {mode}: {type(e).__name__}: {e}")
                    cols.append((mode, None))
                    stats[mode]["failures"] += 1
                stats[mode]["seconds"] += time.monotonic() - t0
            rows.append((path.stem, cols))
            print(f"done {path.name}: {len(boxes)} box(es)")
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUTS / f"bakeoff_seg_{run}.png"
        contact_sheet(rows).save(out_path)
        print(f"\ncontact sheet: {out_path}")
        n = len(photos)
        print(f"{'mode':<14}{'requests':>9}{'est. $':>9}{'avg s':>8}{'mapped':>10}{'fail':>6}")
        for mode in args.modes:
            st = stats[mode]
            mapped = f"{int(st['mapped'])}/{int(st['boxes'])}" if st["boxes"] else "-"
            print(f"{mode:<14}{int(st['requests']):>9}{st['usd']:>9.4f}"
                  f"{st['seconds'] / n:>8.1f}{mapped:>10}{int(st['failures']):>6}")
        print(f"recorded in api_spend this run: ${(await spend.status()).spent_usd - before:.4f}")
        return 0
    finally:
        for k in keys:
            await storage.delete(k)
        await fal.close()
        if llm is not None:
            await llm.close()
        if storage is not None:
            await storage.close()
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
