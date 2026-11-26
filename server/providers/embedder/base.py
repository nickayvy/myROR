"""Embedder protocol: an RGBA or RGB image in, a 512-float32 unit vector out."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

DIM = 512


class EmbedderUnavailable(RuntimeError):
    """The real embedder's optional dependencies are not installed (the `ml` extra)."""


@runtime_checkable
class Embedder(Protocol):
    name: str  # stored in *.embedding_model; vectors from different names never compare

    @property
    def available(self) -> bool: ...

    async def embed(self, image: bytes) -> np.ndarray:
        """float32, shape (512,), L2 norm 1."""
        ...


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    if v.shape != (DIM,):
        raise ValueError(f"expected {DIM} dims, got {v.shape}")
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n == 0:
        raise ValueError("zero or non-finite embedding")
    return (v / n).astype(np.float32)
