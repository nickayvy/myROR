"""Background-removal providers: `BgRemover` protocol, `FalBgRemover`, `FakeBgRemover`.
Selected by BG_REMOVER (fal | fake); the fal endpoint by FAL_BG_ENDPOINT."""

from __future__ import annotations

from providers.bg_remover.base import BgRemovalError, BgRemover, CutoutResult
from providers.bg_remover.fake import FakeBgRemover
from providers.bg_remover.fal import BIREFNET, VARIANTS, FalBgRemover

__all__ = [
    "BIREFNET",
    "VARIANTS",
    "BgRemovalError",
    "BgRemover",
    "CutoutResult",
    "FakeBgRemover",
    "FalBgRemover",
    "build_bg_remover",
]


def build_bg_remover(settings, fal) -> BgRemover:
    if settings.bg_remover == "fake":
        return FakeBgRemover()
    options = {}
    if settings.fal_bg_endpoint == BIREFNET:
        options = {"model": settings.fal_bg_model,
                   "operating_resolution": settings.fal_bg_resolution}
    return FalBgRemover(fal, settings.fal_bg_endpoint, options)
