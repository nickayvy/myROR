"""Segmenter protocol: boxes in, one mask per box out (same order)."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

import numpy as np
from PIL import Image

Method = Literal["multi", "per_box", "box", "fake"]


@dataclass(frozen=True)
class Box:
    """Integer pixel corners, x_max/y_max exclusive."""

    x_min: int
    y_min: int
    x_max: int
    y_max: int

    @property
    def area(self) -> int:
        return max(0, self.x_max - self.x_min) * max(0, self.y_max - self.y_min)

    def clamp(self, width: int, height: int) -> Box:
        x0, x1 = sorted((max(0, min(width, self.x_min)), max(0, min(width, self.x_max))))
        y0, y1 = sorted((max(0, min(height, self.y_min)), max(0, min(height, self.y_max))))
        return Box(x0, y0, x1, y1)

    def expand(self, frac: float, width: int, height: int) -> Box:
        dx = int((self.x_max - self.x_min) * frac)
        dy = int((self.y_max - self.y_min) * frac)
        return Box(self.x_min - dx, self.y_min - dy, self.x_max + dx,
                   self.y_max + dy).clamp(width, height)

    def as_dict(self) -> dict[str, int]:
        return {"x_min": self.x_min, "y_min": self.y_min, "x_max": self.x_max,
                "y_max": self.y_max}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Box:
        return cls(int(d["x_min"]), int(d["y_min"]), int(d["x_max"]), int(d["y_max"]))


@dataclass
class SegMask:
    mask_png: bytes  # mode-L PNG at the image size, 0 or 255
    box: Box  # the requested box
    mask_box: Box | None  # bounding box of the mask pixels
    iou: float  # IoU of mask_box with the requested box
    method: Method
    score: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Segmenter(Protocol):
    name: str

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
        """One SegMask per requested box, in request order."""
        ...


def iou(a: Box, b: Box) -> float:
    ix = max(0, min(a.x_max, b.x_max) - max(a.x_min, b.x_min))
    iy = max(0, min(a.y_max, b.y_max) - max(a.y_min, b.y_min))
    inter = ix * iy
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def mask_bbox(mask: np.ndarray) -> Box | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return Box(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def decode_mask(png: bytes, size: tuple[int, int]) -> np.ndarray:
    """Boolean mask at `size` (w, h) from a mask image. Uses the alpha channel when the
    image has a varying one, otherwise luminance > 127."""
    im = Image.open(io.BytesIO(png))
    im.load()
    if im.mode in ("RGBA", "LA"):
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        im = alpha if lo != hi else im.convert("L")
    else:
        im = im.convert("L")
    if im.size != size:
        im = im.resize(size, Image.NEAREST)
    return np.asarray(im) > 127


def encode_mask(mask: np.ndarray) -> bytes:
    out = io.BytesIO()
    Image.fromarray(mask.astype(np.uint8) * 255, "L").save(out, "PNG", optimize=True)
    return out.getvalue()


def box_mask(box: Box, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    m = np.zeros((h, w), dtype=bool)
    m[box.y_min:box.y_max, box.x_min:box.x_max] = True
    return m
