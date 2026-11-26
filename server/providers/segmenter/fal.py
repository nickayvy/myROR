"""SAM 3 on fal (`fal-ai/sam-3/image`, or `fal-ai/sam-3-1/image`, same schema).

Plan (HANDOFF A10): one request per check-in with every garment box in `box_prompts`
(priced per request), then map each returned mask back to a requested box by the IoU of the
mask's own pixel bounding box. Never map through `metadata[].index` or array order: in the
admin's live test `metadata[]` came back sorted by score with `index` not matching the mask
position. Boxes that cannot be mapped unambiguously get one request each; a box whose
per-box request still yields nothing usable falls back to the plain rectangle.

Schema (fal OpenAPI, 2026-11-25): input image_url, prompt (defaults to "wheel": always set),
box_prompts [{x_min, y_min, x_max, y_max, object_id?}] ints, apply_mask (default true),
return_multiple_masks (default false), max_masks 1-32 (default 3), include_scores,
include_boxes, output_format, sync_mode. Output masks[] (Image{url,...}), metadata[]
{index, score, box(cxcywh norm)}, scores[], boxes[] (cxcywh norm).
"""

from __future__ import annotations

import io
import logging
from uuid import UUID

import numpy as np
from PIL import Image

from providers.fal import FalClient, FalResponseError
from providers.segmenter.base import (
    Box,
    SegMask,
    box_mask,
    decode_mask,
    encode_mask,
    iou,
    mask_bbox,
)

log = logging.getLogger(__name__)

MIN_IOU = 0.5  # a mask must overlap its box at least this much to count
AMBIGUITY_MARGIN = 0.05  # a runner-up mask this close to the winner makes a box ambiguous
SPAN_SHARE = 0.2  # a mask with this share of its pixels in each of two boxes spans both
CLIP_EXPAND = 0.15  # masks are clipped to the requested box grown by this fraction
MULTI_PROMPT = "clothing"
SAM3 = "fal-ai/sam-3/image"
SAM31 = "fal-ai/sam-3-1/image"


def spans_two_boxes(mask: np.ndarray, boxes: list[Box], share: float = SPAN_SHARE) -> bool:
    """True if a sizeable part of the mask lies in box i but not box j, and another sizeable
    part in box j but not box i: one mask covering two garments (shirt and trousers).
    Nested boxes (a shirt box inside a jacket box) do not trigger it."""
    total = int(mask.sum())
    if total == 0 or len(boxes) < 2:
        return False
    h, w = mask.shape
    inside = [box_mask(b, (w, h)) for b in boxes]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            only_i = int((mask & inside[i] & ~inside[j]).sum())
            only_j = int((mask & inside[j] & ~inside[i]).sum())
            if only_i >= share * total and only_j >= share * total:
                return True
    return False


def assign(boxes: list[Box], masks: list[np.ndarray]) -> dict[int, int]:
    """Greedy one-to-one assignment box index -> mask index by IoU of the mask's own
    bounding box with each requested box. Masks spanning two boxes are unusable; a box is
    left out if its best mask is below MIN_IOU or a second mask fits it about as well."""
    mask_boxes = [mask_bbox(m) for m in masks]
    usable = [mi for mi, mb in enumerate(mask_boxes)
              if mb is not None and not spans_two_boxes(masks[mi], boxes)]
    pairs = sorted(((iou(b, mask_boxes[mi]), bi, mi) for bi, b in enumerate(boxes)
                    for mi in usable), reverse=True)
    out: dict[int, int] = {}
    used: set[int] = set()
    for score, bi, mi in pairs:
        if score < MIN_IOU:
            break
        if bi in out or mi in used:
            continue
        rivals = [s for s, b2, m2 in pairs if b2 == bi and m2 != mi and m2 not in used]
        if rivals and max(rivals) >= score - AMBIGUITY_MARGIN:
            continue  # two masks fit this box about equally well
        out[bi] = mi
        used.add(mi)
    return out


class FalSamSegmenter:
    def __init__(self, fal: FalClient, endpoint: str = SAM3, *, timeout_s: float = 120.0,
                 multi_prompt: str = MULTI_PROMPT, multi_box: bool = True) -> None:
        self.fal = fal
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.multi_prompt = multi_prompt
        self.multi_box = multi_box
        self.name = f"fal:{endpoint}"
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
        size = Image.open(io.BytesIO(data)).size
        boxes = [b.clamp(*size) for b in boxes]
        image_url = await self.fal.image_input(data=data, key=key, content_type=content_type)
        results: dict[int, SegMask] = {}
        if self.multi_box and len(boxes) > 1:
            masks = await self._call(image_url, boxes, self.multi_prompt, size, purpose,
                                     job_id, max_masks=min(32, len(boxes) + 4))
            mapping = assign(boxes, masks)
            for bi, mi in mapping.items():
                results[bi] = self._result(boxes[bi], masks[mi], size, "multi")
            if len(mapping) < len(boxes):
                log.info("SAM multi-box mapped %d of %d boxes; per-box for the rest",
                         len(mapping), len(boxes))
        for bi, box in enumerate(boxes):
            if bi in results:
                continue
            prompt = (labels[bi] if bi < len(labels) and labels[bi] else self.multi_prompt)
            masks = await self._call(image_url, [box], prompt, size, purpose, job_id,
                                     max_masks=3)
            best = max(
                ((iou(box, mb), m) for m in masks if (mb := mask_bbox(m)) is not None),
                key=lambda t: t[0], default=(0.0, None),
            )
            if best[1] is not None and best[0] >= 0.1:
                results[bi] = self._result(box, best[1], size, "per_box")
            else:
                results[bi] = self._result(box, box_mask(box, size), size, "box")
        return [results[i] for i in range(len(boxes))]

    def _result(self, box: Box, mask: np.ndarray, size, method) -> SegMask:
        clip = box.expand(CLIP_EXPAND, *size)
        clipped = mask & box_mask(clip, size)
        mb = mask_bbox(clipped)
        return SegMask(encode_mask(clipped), box, mb, iou(box, mb) if mb else 0.0, method)

    async def _call(self, image_url, boxes, prompt, size, purpose, job_id, *, max_masks):
        args = {
            "image_url": image_url,
            "prompt": prompt,
            "box_prompts": [b.as_dict() for b in boxes],  # no object_id: separate objects
            "apply_mask": False,
            "return_multiple_masks": True,
            "max_masks": max(1, min(32, max_masks)),
            "include_scores": True,
            "include_boxes": True,
            "output_format": "png",
        }
        self.requests += 1
        result = await self.fal.run(self.endpoint, args, purpose=purpose, job_id=job_id,
                                    timeout_s=self.timeout_s, units={"boxes": len(boxes)})
        items = result.get("masks")
        if not isinstance(items, list):
            raise FalResponseError(f"{self.endpoint}: response has no masks list")
        out = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("url"), str):
                out.append(decode_mask(await self.fal.fetch_output(it["url"]), size))
        return out
