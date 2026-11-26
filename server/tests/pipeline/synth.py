"""Synthetic test images: garment-like shapes on a white sheet, a "person" for check-ins,
and a scripted vision/tagging responder for FakeLLM. No real photos anywhere."""

from __future__ import annotations

import base64
import io
import zlib
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

PALETTE: dict[str, tuple[int, int, int]] = {
    "red": (200, 30, 40),
    "navy": (25, 35, 90),
    "green": (40, 140, 60),
    "yellow": (230, 200, 40),
    "black": (20, 20, 20),
    "purple": (120, 50, 150),
    "orange": (235, 120, 30),
    "teal": (20, 130, 130),
}
SHEET = (242, 240, 232)  # slightly warm white sheet
SKIN = (205, 160, 130)
WALL = (150, 160, 170)


def _jpeg(im: Image.Image, **kw) -> bytes:
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92, **kw)
    return out.getvalue()


def garment(color: str = "red", kind: str = "tee", size=(1600, 1200), sheet=SHEET,
            offset=(0, 0), scale: float = 1.0) -> Image.Image:
    """A flat-lay garment silhouette on a sheet. kind: tee | trousers | scarf | skirt."""
    w, h = size
    im = Image.new("RGB", size, sheet)
    d = ImageDraw.Draw(im)
    c = PALETTE[color]
    cx, cy = w // 2 + offset[0], h // 2 + offset[1]
    s = min(w, h) / 1000 * scale

    def pts(rel):
        return [(cx + x * s, cy + y * s) for x, y in rel]

    if kind == "tee":
        d.polygon(pts([(-180, -300), (-60, -330), (60, -330), (180, -300), (360, -150),
                       (280, -60), (190, -130), (190, 320), (-190, 320), (-190, -130),
                       (-280, -60), (-360, -150)]), fill=c)
    elif kind == "trousers":
        d.polygon(pts([(-160, -380), (160, -380), (190, 380), (40, 380), (0, -100),
                       (-40, 380), (-190, 380)]), fill=c)
    elif kind == "skirt":
        d.polygon(pts([(-140, -250), (140, -250), (300, 250), (-300, 250)]), fill=c)
    else:  # scarf
        d.rectangle([cx - 400 * s, cy - 70 * s, cx + 400 * s, cy + 70 * s], fill=c)
    # a little texture so JPEG and the embedder have something to chew on
    rng = np.random.default_rng(zlib.crc32(f"{color}:{kind}".encode()))
    a = np.asarray(im, dtype=np.int16)
    a = np.clip(a + rng.integers(-3, 4, a.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(a)


def garment_jpeg(*args, **kw) -> bytes:
    return _jpeg(garment(*args, **kw))


def rotated_jpeg(color: str = "green", kind: str = "trousers") -> bytes:
    """Pixels stored rotated 90 degrees CCW with EXIF orientation 6 ("rotate 90 CW to
    display"), the way a phone held sideways saves them. A yellow marker sits at the top."""
    up = garment(color, kind, size=(1200, 1600))
    ImageDraw.Draw(up).rectangle([500, 20, 700, 60], fill=PALETTE["yellow"])
    stored = up.rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6
    return _jpeg(stored, exif=exif.tobytes())


def heic_with_gps(color: str = "purple", kind: str = "tee") -> bytes:
    """A HEIC stored sideways with EXIF orientation 6 and a GPS position, as an iPhone
    held sideways would save it. Upright it is 1000x1400 with a yellow marker at the top."""
    from pillow_heif import register_heif_opener

    register_heif_opener()
    up = garment(color, kind, size=(1000, 1400))
    ImageDraw.Draw(up).rectangle([420, 20, 580, 60], fill=PALETTE["yellow"])
    stored = up.rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "SyntheticCam"
    exif[0x0132] = "2026:11:20 08:15:00"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 44.0, 30.0)
    gps[3] = "W"
    gps[4] = (73.0, 59.0, 10.0)
    out = io.BytesIO()
    stored.save(out, "HEIF", exif=exif.tobytes(), quality=90)
    return out.getvalue()


def jpeg_with_gps(color: str = "orange", kind: str = "skirt") -> bytes:
    im = garment(color, kind)
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    return _jpeg(im, exif=exif.tobytes())


def person_jpeg(top: str = "red", bottom: str = "navy", outer: str | None = None,
                size=(1200, 1600)) -> bytes:
    """A crude waist-up figure: head, a top, optionally an open jacket, trousers at the
    bottom edge. Flat colours on a grey wall."""
    w, h = size
    im = Image.new("RGB", size, WALL)
    d = ImageDraw.Draw(im)
    d.ellipse([w * 0.40, h * 0.06, w * 0.60, h * 0.24], fill=SKIN)
    d.rectangle([w * 0.47, h * 0.23, w * 0.53, h * 0.28], fill=SKIN)
    torso = [w * 0.30, h * 0.28, w * 0.70, h * 0.72]
    if outer:
        d.rectangle([w * 0.24, h * 0.27, w * 0.76, h * 0.74], fill=PALETTE[outer])
        d.rectangle([w * 0.40, h * 0.28, w * 0.60, h * 0.72], fill=PALETTE[top])
    else:
        d.rectangle(torso, fill=PALETTE[top])
    d.rectangle([w * 0.32, h * 0.72, w * 0.68, h], fill=PALETTE[bottom])
    return _jpeg(im)


# ---- scripted LLM responses ------------------------------------------------------------------


def _decode_image(call) -> Image.Image:
    for b in call.content:
        if b.get("type") == "image":
            return Image.open(io.BytesIO(base64.b64decode(b["source"]["data"]))).convert("RGB")
    raise AssertionError("no image in call")


def _nearest(rgb) -> tuple[str, float]:
    best = min(PALETTE.items(), key=lambda kv: np.linalg.norm(np.subtract(kv[1], rgb)))
    return best[0], float(np.linalg.norm(np.subtract(best[1], rgb)))


TAGS_BY_KIND = {
    "tee": {"category": "top", "subcategory": "t_shirt", "pattern": "solid",
            "material_guess": "cotton", "formality": 2, "season": ["spring", "summer"],
            "fit": "regular", "style_tags": ["casual"]},
    "trousers": {"category": "bottom", "subcategory": "trousers", "pattern": "solid",
                 "material_guess": "wool", "formality": 3, "season": [], "fit": "slim",
                 "style_tags": ["smart_casual"]},
    "jacket": {"category": "outerwear", "subcategory": "jacket", "pattern": "solid",
               "material_guess": "cotton", "formality": 3, "season": ["fall"],
               "fit": "regular", "style_tags": ["casual"]},
}


def tag_response(color: str, kind: str, confidence: str = "high") -> dict[str, Any]:
    t = {"name": f"{color} {kind}", "primary_color": color, "secondary_color": None,
         **TAGS_BY_KIND[kind]}
    return {"tags": t, "confidence": confidence, "single_item": True}


def tagger(call) -> dict[str, Any]:
    """Tags a synthetic garment: colour from the dominant non-white pixels, kind from shape."""
    im = _decode_image(call)
    a = np.asarray(im, dtype=np.int16)
    fg = (a.min(axis=2) < 200) | ((a.max(axis=2) - a.min(axis=2)) > 40)
    if fg.sum() < 50:
        return {"tags": {"name": "unclear", "category": "accessory", "subcategory": "scarf",
                         "primary_color": "white", "secondary_color": None,
                         "pattern": "solid", "material_guess": "unknown", "formality": 2,
                         "season": [], "fit": None, "style_tags": []},
                "confidence": "low", "single_item": False}
    color, _ = _nearest(np.median(a[fg], axis=0))
    ys, xs = np.nonzero(fg)
    tall = (ys.max() - ys.min()) > 1.3 * (xs.max() - xs.min())
    return tag_response(color, "trousers" if tall else "tee")


def vision(call) -> dict[str, Any]:
    """Finds palette-coloured regions in a synthetic person photo and reports each as a
    garment with a pixel box in the coordinates of the image it was sent."""
    im = _decode_image(call)
    a = np.asarray(im, dtype=np.float32)
    garments = []
    for name, rgb in PALETTE.items():
        m = np.linalg.norm(a - np.array(rgb), axis=2) < 40
        if m.sum() < 0.01 * m.size:
            continue
        ys, xs = np.nonzero(m)
        box = {"x_min": int(xs.min()), "y_min": int(ys.min()), "x_max": int(xs.max()) + 1,
               "y_max": int(ys.max()) + 1}
        h, w = m.shape
        bottom = box["y_min"] > 0.6 * h
        wide = (box["x_max"] - box["x_min"]) > 0.48 * w
        kind = "trousers" if bottom else "jacket" if wide else "tee"
        tags = tag_response(name, kind)["tags"]
        garments.append({"tags": tags, "box": box, "visibility": "partly_visible" if bottom
                         else "mostly_visible"})
    return {"image_usable": bool(garments), "garments": garments}


def responder(call) -> dict[str, Any]:
    if call.purpose == "checkin_vision":
        return vision(call)
    return tagger(call)
