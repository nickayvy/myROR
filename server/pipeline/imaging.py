"""Image preparation: decode (HEIC too), orientation, metadata stripping, downscale, white
balance, cutout cropping, colour measurement. All functions are synchronous and CPU-bound;
call them through asyncio.to_thread.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from providers.segmenter.base import Box

# ---- limits -----------------------------------------------------------------------------

STORED_MAX_EDGE = 2048  # stored photo: enough for BiRefNet at 2048/2304 in the bake-off
CUTOUT_MAX_EDGE = 1024  # stored cutout PNG
TAG_MAX_EDGE = 448  # image sent to the tagger: <= 448*448/750 ~ 268 image tokens
CHECKIN_LLM_MAX_EDGE = 1024  # image sent to the check-in vision call: <= ~1,400 tokens
MAX_PIXELS = 50_000_000  # refuse decompression bombs before decoding
ACCEPTED_FORMATS = {"JPEG", "MPO", "PNG", "HEIF", "WEBP"}
JPEG_QUALITY = 90

_heif_registered = False


def register_heif() -> None:
    global _heif_registered
    if not _heif_registered:
        from pillow_heif import register_heif_opener  # noqa: PLC0415

        register_heif_opener()
        _heif_registered = True


class ImageRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Prepared:
    jpeg: bytes
    width: int
    height: int
    taken_at: datetime | None
    flags: dict[str, Any] = field(default_factory=dict)


# ---- upload preparation -----------------------------------------------------------------


def open_image(data: bytes) -> Image.Image:
    register_heif()
    try:
        im = Image.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - anything Pillow cannot identify
        raise ImageRejected("unsupported_type", "not a supported image (JPEG, PNG, HEIC, "
                            "WebP)") from e
    fmt = (im.format or "").upper()
    if fmt not in ACCEPTED_FORMATS:
        raise ImageRejected("unsupported_type", f"unsupported image format {fmt or '?'}")
    w, h = im.size
    if w * h > MAX_PIXELS:
        raise ImageRejected("too_large", f"image is {w}x{h}; at most {MAX_PIXELS} pixels")
    if min(w, h) < 64:
        raise ImageRejected("too_small", f"image is {w}x{h}; at least 64 px per side")
    try:
        im.load()
    except Exception as e:  # noqa: BLE001
        raise ImageRejected("corrupt", f"image could not be decoded: {e}") from e
    return im


def _taken_at(im: Image.Image) -> datetime | None:
    try:
        exif = im.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
        return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S") if raw else None
    except Exception:  # noqa: BLE001 - metadata is best effort
        return None


def to_rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


def downscale(im: Image.Image, max_edge: int) -> Image.Image:
    if max(im.size) <= max_edge:
        return im
    im = im.copy()
    im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return im


def clean_copy(im: Image.Image) -> Image.Image:
    """A pixel-only copy: no EXIF, XMP, comments or other info survive."""
    return Image.fromarray(np.asarray(im), im.mode)


def encode_jpeg(im: Image.Image, quality: int = JPEG_QUALITY, icc: bytes | None = None) -> bytes:
    out = io.BytesIO()
    kw: dict[str, Any] = {"quality": quality, "optimize": True}
    if icc:
        kw["icc_profile"] = icc
    clean_copy(im.convert("RGB")).save(out, "JPEG", **kw)
    return out.getvalue()


def encode_png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    clean_copy(im).save(out, "PNG")
    return out.getvalue()


def white_balance(im: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    """Neutralise the colour cast using the white sheet visible around the garment.

    The reference is the border ring (outer 8%). Guard: the ring must be mostly bright and
    nearly neutral, i.e. look like a sheet; otherwise the photo is left alone (a garment
    on a bed, a mirror selfie)."""
    a = np.asarray(im.convert("RGB"), dtype=np.float32)
    h, w, _ = a.shape
    bw, bh = max(2, int(w * 0.08)), max(2, int(h * 0.08))
    ring = np.concatenate([
        a[:bh].reshape(-1, 3), a[-bh:].reshape(-1, 3),
        a[bh:-bh, :bw].reshape(-1, 3), a[bh:-bh, -bw:].reshape(-1, 3),
    ])
    lum = ring.mean(axis=1)
    p90 = float(np.percentile(lum, 90))
    bright = ring[lum >= 0.85 * p90]
    bright_share = len(bright) / len(ring)
    ref = np.median(bright, axis=0) if len(bright) else np.zeros(3)
    chroma = float((ref.max() - ref.min()) / max(ref.max(), 1.0))
    info: dict[str, Any] = {"sheet_rgb": [round(float(x), 1) for x in ref],
                            "bright_share": round(bright_share, 3), "chroma": round(chroma, 3)}
    if p90 < 170 or bright_share < 0.55 or chroma > 0.30:
        info.update(applied=False, reason="no white sheet detected")
        return im, info
    target = min(245.0, float(ref.max()))
    gains = np.clip(target / np.maximum(ref, 1.0), 0.8, 1.5)
    out = np.clip(a * gains, 0, 255).astype(np.uint8)
    info.update(applied=True, gains=[round(float(g), 3) for g in gains])
    return Image.fromarray(out, "RGB"), info


def prepare_upload(data: bytes, *, max_edge: int = STORED_MAX_EDGE,
                   balance: bool = True) -> Prepared:
    """Decode, apply EXIF orientation, drop all metadata, downscale, white-balance."""
    im = open_image(data)
    taken_at = _taken_at(im)
    # pillow-heif applies HEIF rotation itself and keeps the EXIF value here
    orientation = im.info.get("original_orientation") or im.getexif().get(0x0112, 1)
    icc = im.info.get("icc_profile")
    original_size = im.size
    im = ImageOps.exif_transpose(im) or im
    im = downscale(to_rgb(im), max_edge)
    flags: dict[str, Any] = {"exif_orientation": int(orientation),
                             "original_size": list(original_size)}
    if balance:
        im, wb = white_balance(im)
        flags["white_balance"] = wb
    return Prepared(encode_jpeg(im, icc=icc), im.width, im.height, taken_at, flags)


# ---- cutouts ------------------------------------------------------------------------------


def alpha_box(rgba: Image.Image, min_alpha: int = 24) -> Box | None:
    bbox = rgba.getchannel("A").point(lambda v: 255 if v >= min_alpha else 0).getbbox()
    return Box(*bbox) if bbox else None


def crop_cutout(png: bytes, *, pad: float = 0.04, max_edge: int = CUTOUT_MAX_EDGE,
                min_coverage: float = 0.005) -> tuple[bytes, Box, float] | None:
    """Crop an RGBA cutout to its foreground (plus padding). Returns (png, box in the source
    image's pixels, foreground share of the image), or None if there is no foreground."""
    rgba = Image.open(io.BytesIO(png)).convert("RGBA")
    box = alpha_box(rgba)
    if box is None:
        return None
    alpha = np.asarray(rgba.getchannel("A"))
    coverage = float((alpha >= 24).mean())
    if coverage < min_coverage:
        return None
    padded = box.expand(pad, *rgba.size)
    crop = downscale(rgba.crop((padded.x_min, padded.y_min, padded.x_max, padded.y_max)),
                     max_edge)
    return encode_png(crop), box, coverage


def masked_cutout(photo: bytes, mask_png: bytes, *, pad: float = 0.03,
                  max_edge: int = CUTOUT_MAX_EDGE) -> tuple[bytes, Box] | None:
    """Apply a mode-L mask to a photo and crop to the mask."""
    im = Image.open(io.BytesIO(photo)).convert("RGB")
    mask = Image.open(io.BytesIO(mask_png)).convert("L")
    if mask.size != im.size:
        mask = mask.resize(im.size, Image.NEAREST)
    rgba = im.convert("RGBA")
    rgba.putalpha(mask)
    box = alpha_box(rgba, 128)
    if box is None:
        return None
    padded = box.expand(pad, *im.size)
    crop = downscale(rgba.crop((padded.x_min, padded.y_min, padded.x_max, padded.y_max)),
                     max_edge)
    return encode_png(crop), box


def on_white_jpeg(png: bytes, max_edge: int = TAG_MAX_EDGE, quality: int = 85) -> bytes:
    im = Image.open(io.BytesIO(png))
    return encode_jpeg(downscale(to_rgb(im), max_edge), quality=quality)


def for_llm(jpeg: bytes, max_edge: int = CHECKIN_LLM_MAX_EDGE) -> tuple[bytes, float, int, int]:
    """Downscale for a vision call. Returns (jpeg, scale back to the source, width, height)."""
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    small = downscale(im, max_edge)
    scale = im.width / small.width
    return encode_jpeg(small, quality=85), scale, small.width, small.height


def image_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


# ---- colour ------------------------------------------------------------------------------


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]),
                     200 * (f[:, 1] - f[:, 2])], axis=1)


def dominant_labs(png: bytes, k: int = 2, iters: int = 12) -> list[tuple[list[float], float]]:
    """Up to k (Lab, share) clusters of the opaque pixels, largest first. Lab is measured,
    never chosen by the model (taxonomy)."""
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    im.thumbnail((96, 96))
    a = np.asarray(im, dtype=np.float32).reshape(-1, 4)
    px = a[a[:, 3] >= 128][:, :3]
    if len(px) < 10:
        return []
    lab = _srgb_to_lab(px)
    rng = np.random.default_rng(0)
    centers = lab[rng.choice(len(lab), size=min(k, len(lab)), replace=False)]
    for _ in range(iters):
        d = np.linalg.norm(lab[:, None, :] - centers[None, :, :], axis=2)
        lbl = d.argmin(axis=1)
        centers = np.array([lab[lbl == i].mean(axis=0) if (lbl == i).any() else centers[i]
                            for i in range(len(centers))])
    shares = np.bincount(lbl, minlength=len(centers)) / len(lab)
    order = np.argsort(-shares)
    return [([round(float(x), 2) for x in centers[i]], float(shares[i]))
            for i in order if shares[i] > 0]
