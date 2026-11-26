"""Builds the closet-pipeline providers from settings, once, on first use, and caches them on
the Services container (services.bg_remover / segmenter / embedder / face_blurrer / fal).

Nothing here runs at import or startup, so a missing `ml` extra or FAL_KEY never stops the
app. Tests assign fakes directly: `app.state.services.bg_remover = FakeBgRemover()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.faceblur import build_face_blurrer
from providers.bg_remover import build_bg_remover
from providers.embedder import build_embedder
from providers.fal import build_fal

_UNSET = object()


@dataclass
class Providers:
    bg_remover: Any
    segmenter: Any
    embedder: Any  # None when the real embedder is not installed
    face_blurrer: Any


def _fal(services) -> Any:
    if services.fal is None:
        services.fal = build_fal(services.settings, services.spend, services.storage)
    return services.fal


def pipeline_providers(services) -> Providers:
    s = services.settings
    if services.bg_remover is None:
        services.bg_remover = build_bg_remover(s, _fal(services) if s.bg_remover == "fal"
                                               else None)
    if services.segmenter is None:
        from providers.segmenter import build_segmenter  # noqa: PLC0415

        services.segmenter = build_segmenter(s, _fal(services) if s.segmenter == "fal"
                                             else None)
    if services.embedder is None:
        built = build_embedder(s)
        services.embedder = built if built is not None else _NoEmbedder
    if services.face_blurrer is None:
        services.face_blurrer = build_face_blurrer(s)
    emb = None if services.embedder is _NoEmbedder else services.embedder
    return Providers(services.bg_remover, services.segmenter, emb, services.face_blurrer)


class _NoEmbedderType:
    """Cached marker: the real embedder is not installed (avoids re-checking every call)."""

    name = "none"
    available = False


_NoEmbedder = _NoEmbedderType()
# Assign this to services.embedder to simulate "ml extra not installed".
NO_EMBEDDER = _NoEmbedder
