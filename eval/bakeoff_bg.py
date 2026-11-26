"""Background-removal bake-off on your own garment photos (needs FAL_KEY; costs money).

    mkdir -p eval/photos && cp ~/some-garment-shots/*.jpg eval/photos/   # git-ignored
    cd server && uv run python ../eval/bakeoff_bg.py --dry-run           # cost estimate only
    cd server && uv run python ../eval/bakeoff_bg.py                     # every variant
    cd server && uv run python ../eval/bakeoff_bg.py --variants birefnet-light rmbg-3

Each photo is prepared exactly as the pipeline does (HEIC decode, orientation, EXIF strip,
2048 px max, white balance), stored under eval/<run>/ in the private bucket, sent to every
variant by presigned link (or --input data_uri), and deleted afterwards. Writes a contact
sheet (original, then each variant's cutout on a mid-grey checker) to
eval/outputs/bakeoff_bg_<run>.png and prints cost and time per model. Every call goes through
the spend cap and is recorded in api_spend (purpose "bakeoff_bg").

Variants (providers/bg_remover/fal.py VARIANTS): birefnet-light, birefnet-light-2k,
birefnet-heavy, birefnet-matting, birefnet-dynamic-2304, rmbg-3, pixelcut, feynobg.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from collections import defaultdict

from _common import OUTPUTS, PHOTOS, contact_sheet, list_photos, on_checker, setup


async def main() -> int:
    from PIL import Image

    from pipeline.imaging import prepare_upload
    from providers.bg_remover import VARIANTS, FalBgRemover
    from providers.fal import FalError, fal_price

    p = argparse.ArgumentParser()
    p.add_argument("--variants", nargs="*", default=sorted(VARIANTS), choices=sorted(VARIANTS))
    p.add_argument("--input", choices=["presign", "data_uri"], default=None)
    p.add_argument("--limit", type=int, default=0, help="only the first N photos")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    photos = list_photos(PHOTOS)[: args.limit or None]
    if not photos:
        raise SystemExit(f"no images in {PHOTOS}")
    est = sum(float(fal_price(VARIANTS[v][0])) for v in args.variants) * len(photos)
    print(f"{len(photos)} photo(s) x {len(args.variants)} variant(s); listed prices total "
          f"${est:.3f} (BiRefNet price is a placeholder: fal lists '$0 per compute seconds')")
    if args.dry_run:
        return 0

    s, db, spend, storage, fal = await setup(args.input)
    run = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    keys: list[str] = []
    rows = []
    cost: dict[str, float] = defaultdict(float)
    seconds: dict[str, list[float]] = defaultdict(list)
    failures: dict[str, int] = defaultdict(int)
    before = (await spend.status()).spent_usd
    try:
        for path in photos:
            prepared = await asyncio.to_thread(prepare_upload, path.read_bytes())
            key = f"eval/{run}/{path.stem}.jpg"
            if storage is not None:
                await storage.put(key, prepared.jpeg, "image/jpeg")
                keys.append(key)
            import io

            cols = [("prepared", Image.open(io.BytesIO(prepared.jpeg)))]
            for name in args.variants:
                endpoint, options = VARIANTS[name]
                bg = FalBgRemover(fal, endpoint, options)
                t0 = time.monotonic()
                try:
                    res = await bg.remove(data=prepared.jpeg, key=key if storage else None,
                                          purpose="bakeoff_bg")
                    cols.append((name, on_checker(res.png)))
                    cost[name] += float(fal_price(endpoint))
                except FalError as e:
                    print(f"  {path.name} {name}: {type(e).__name__}: {e}")
                    cols.append((name, None))
                    failures[name] += 1
                seconds[name].append(time.monotonic() - t0)
            rows.append((path.stem, cols))
            print(f"done {path.name}")
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        out = OUTPUTS / f"bakeoff_bg_{run}.png"
        contact_sheet(rows).save(out)
        print(f"\ncontact sheet: {out}")
        print(f"{'variant':<24}{'est. $':>9}{'avg s':>8}{'fail':>6}")
        for name in args.variants:
            t = seconds[name]
            print(f"{name:<24}{cost[name]:>9.4f}{sum(t) / max(len(t), 1):>8.2f}"
                  f"{failures[name]:>6}")
        after = (await spend.status()).spent_usd
        print(f"recorded in api_spend this run: ${after - before:.4f}")
        return 0
    finally:
        for k in keys:
            await storage.delete(k)
        await fal.close()
        if storage is not None:
            await storage.close()
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
