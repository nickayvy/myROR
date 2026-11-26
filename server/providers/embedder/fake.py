"""Deterministic offline embedder from colour and shape.

The image is composited on white, cut to its alpha bounding box, and sampled on an 8x8 grid:
per cell the mean RGB of the foreground and the foreground coverage, centred to [-1, 1].
That 256-dim feature goes through a fixed random projection to 512 dims and is normalised.
Near-identical images (re-encoded, slightly shifted or scaled) land close together; a
differently coloured or shaped garment lands far away. Not a semantic embedding.
"""

from __future__ import annotations

import asyncio
import io

import numpy as np
from PIL import Image

from providers.embedder.base import DIM, unit

GRID = 8
_PROJ = np.random.default_rng(20261125).standard_normal((GRID * GRID * 4, DIM)).astype(
    np.float32
)


def features(image: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(image)).convert("RGBA")
    alpha = im.getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 32 else 0).getbbox()
    if bbox:
        im = im.crop(bbox)
    small = im.resize((GRID * 4, GRID * 4), Image.BILINEAR)
    a = np.asarray(small, dtype=np.float32) / 255.0
    rgb, cover = a[..., :3], a[..., 3:4]
    rgb_cells = (rgb * cover).reshape(GRID, 4, GRID, 4, 3).sum(axis=(1, 3))
    cov_cells = cover.reshape(GRID, 4, GRID, 4, 1).sum(axis=(1, 3))
    mean_rgb = np.where(cov_cells > 0, rgb_cells / np.maximum(cov_cells, 1e-6), 0.5)
    feats = np.concatenate([(mean_rgb - 0.5) * 2.0, (cov_cells / 16.0 - 0.5) * 2.0], axis=2)
    return feats.reshape(-1).astype(np.float32)


class FakeEmbedder:
    name = "fake-colorshape-v1"

    @property
    def available(self) -> bool:
        return True

    async def embed(self, image: bytes) -> np.ndarray:
        f = await asyncio.to_thread(features, image)
        return unit(f @ _PROJ)
