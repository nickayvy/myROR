"""Segmenter providers: `Segmenter` protocol, `FalSamSegmenter` (SAM 3 on fal), `FakeSegmenter`.
Selected by SEGMENTER (fal | fake); the endpoint by FAL_SEG_ENDPOINT."""

from __future__ import annotations

from providers.segmenter.base import Box, SegMask, Segmenter, iou, mask_bbox
from providers.segmenter.fake import FakeSegmenter
from providers.segmenter.fal import SAM3, SAM31, FalSamSegmenter, assign

__all__ = [
    "SAM3",
    "SAM31",
    "Box",
    "FakeSegmenter",
    "FalSamSegmenter",
    "SegMask",
    "Segmenter",
    "assign",
    "build_segmenter",
    "iou",
    "mask_bbox",
]


def build_segmenter(settings, fal) -> Segmenter:
    if settings.segmenter == "fake":
        return FakeSegmenter()
    return FalSamSegmenter(fal, settings.fal_seg_endpoint, timeout_s=settings.fal_seg_timeout_s)
