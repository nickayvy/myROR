"""Embedding providers: `Embedder` protocol, `FashionClipEmbedder` (lazy, `ml` extra),
`FakeEmbedder`. Selected by EMBEDDER (fashionclip | fake).

`build_embedder` returns None when EMBEDDER=fashionclip but the `ml` extra is missing: the
pipeline then stores null embeddings, matches by tags, and pipeline.backfill_embeddings fills
them in later."""

from __future__ import annotations

import logging

from providers.embedder.base import DIM, Embedder, EmbedderUnavailable, unit
from providers.embedder.fake import FakeEmbedder
from providers.embedder.fashionclip import FashionClipEmbedder, ml_installed

log = logging.getLogger(__name__)

__all__ = [
    "DIM",
    "Embedder",
    "EmbedderUnavailable",
    "FakeEmbedder",
    "FashionClipEmbedder",
    "build_embedder",
    "ml_installed",
    "unit",
]


def build_embedder(settings) -> Embedder | None:
    if settings.embedder == "fake":
        return FakeEmbedder()
    if not ml_installed():
        log.warning("EMBEDDER=fashionclip but torch/open_clip are not installed; "
                    "items get null embeddings and tag-based matching")
        return None
    return FashionClipEmbedder(settings.embedder_model)
