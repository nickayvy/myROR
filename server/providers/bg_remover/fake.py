"""Offline background remover: near-white pixels (the sheet) become transparent."""

from __future__ import annotations

import asyncio
import io
from uuid import UUID

import numpy as np
from PIL import Image, ImageFilter

from providers.bg_remover.base import CutoutResult


def threshold_cutout(data: bytes, *, min_level: int = 215, max_chroma: int = 28) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    a = np.asarray(im, dtype=np.int16)
    lo, hi = a.min(axis=2), a.max(axis=2)
    background = (lo >= min_level) & ((hi - lo) <= max_chroma)
    alpha = Image.fromarray(np.where(background, 0, 255).astype(np.uint8), "L")
    # Close pinholes, then soften the edge a little like a real matting model would.
    alpha = alpha.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.GaussianBlur(0.8))
    rgba = im.convert("RGBA")
    rgba.putalpha(alpha)
    out = io.BytesIO()
    rgba.save(out, "PNG")
    return out.getvalue()


class FakeBgRemover:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def remove(
        self,
        *,
        data: bytes,
        key: str | None = None,
        content_type: str = "image/jpeg",
        purpose: str = "bg_remove",
        job_id: UUID | None = None,
    ) -> CutoutResult:
        self.calls += 1
        png = await asyncio.to_thread(threshold_cutout, data)
        return CutoutResult(png, self.name, "threshold")
