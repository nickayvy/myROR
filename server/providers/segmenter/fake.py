"""Offline segmenter: inside each box, pixels close to the colour at the box centre."""

from __future__ import annotations

import io
from uuid import UUID

import numpy as np
from PIL import Image

from providers.segmenter.base import Box, SegMask, encode_mask, iou, mask_bbox


def color_mask(rgb: np.ndarray, box: Box, tol: float = 45.0) -> np.ndarray:
    h, w, _ = rgb.shape
    m = np.zeros((h, w), dtype=bool)
    if box.area == 0:
        return m
    cx0 = box.x_min + (box.x_max - box.x_min) * 2 // 5
    cx1 = box.x_min + (box.x_max - box.x_min) * 3 // 5 + 1
    cy0 = box.y_min + (box.y_max - box.y_min) * 2 // 5
    cy1 = box.y_min + (box.y_max - box.y_min) * 3 // 5 + 1
    ref = np.median(rgb[cy0:cy1, cx0:cx1].reshape(-1, 3), axis=0)
    region = rgb[box.y_min:box.y_max, box.x_min:box.x_max].astype(np.float32)
    close = np.linalg.norm(region - ref, axis=2) <= tol
    m[box.y_min:box.y_max, box.x_min:box.x_max] = close
    return m


class FakeSegmenter:
    name = "fake"

    def __init__(self) -> None:
        self.requests = 0

    async def segment(
        self,
        *,
        data: bytes,
        key: str | None,
        boxes: list[Box],
        labels: list[str],
        content_type: str = "image/jpeg",
        purpose: str = "segment",
        job_id: UUID | None = None,
    ) -> list[SegMask]:
        if not boxes:
            return []
        self.requests += 1
        im = Image.open(io.BytesIO(data)).convert("RGB")
        rgb = np.asarray(im)
        out = []
        for b in boxes:
            b = b.clamp(*im.size)
            m = color_mask(rgb, b)
            mb = mask_bbox(m)
            out.append(SegMask(encode_mask(m), b, mb, iou(b, mb) if mb else 0.0, "fake"))
        return out
