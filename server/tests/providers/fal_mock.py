"""A stand-in for https://fal.run behind httpx.MockTransport.

Response shapes follow fal's schema pages and the admin's live test (2026-11-25):
- BiRefNet: {"image": {"url": "data:image/png;base64,...", "width", "height",
  "content_type", "file_name", "file_size"}, "mask_image": {...}} (RGBA at input size).
- SAM 3: {"masks": [{"url": "data:image/png;base64,..." (mode L, input size), ...}],
  "metadata": [{"index", "score", "box"}] sorted by score with indexes that do NOT match
  the mask positions, "scores": [...], "boxes": [[cx, cy, w, h] normalised], "image": null}.

The mock fetches presigned inputs itself (MinIO is reachable from the test container), so a
presigned link that does not work fails the test.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
from PIL import Image

from providers.bg_remover.fake import threshold_cutout
from providers.fal import decode_data_uri
from providers.segmenter.base import Box
from providers.segmenter.fake import color_mask

BG_ENDPOINTS = {"fal-ai/birefnet/v2", "bria/rmbg-3", "pixelcut/background-removal",
                "fal-ai/feynobg"}
SAM_ENDPOINTS = {"fal-ai/sam-3/image", "fal-ai/sam-3-1/image"}


def _png_uri(im: Image.Image) -> str:
    out = io.BytesIO()
    im.save(out, "PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


@dataclass
class FalMock:
    """Configure behaviour per test, then use `.transport`."""

    shuffle_masks: bool = False
    extra_mask: bool = False  # SAM also returns an unrelated object
    merge_first_two: bool = False  # SAM returns one mask covering boxes 0 and 1
    fail_times: int = 0  # first N calls answer 503
    fail_status: int = 503
    hosted_output: bool = False  # ignore sync_mode and return an https URL
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    fetched_inputs: list[str] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _input(self, url: str) -> bytes:
        if url.startswith("data:"):
            self.fetched_inputs.append("data_uri")
            return decode_data_uri(url)[0]
        self.fetched_inputs.append("presigned")
        r = httpx.get(url, timeout=10)  # MinIO, the real presigned link
        assert r.status_code == 200, f"presigned input not fetchable: {r.status_code}"
        return r.content

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "fal.run":
            if request.url.host.endswith("fal.media"):
                return httpx.Response(200, content=self._hosted_payload)
            return httpx.Response(404)
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(self.fail_status, json={"detail": "busy"})
        endpoint = request.url.path.lstrip("/")
        data = self._input(body["image_url"])
        if endpoint in BG_ENDPOINTS:
            return httpx.Response(200, json=self._bg(data))
        if endpoint in SAM_ENDPOINTS:
            return httpx.Response(200, json=self._sam(data, body))
        return httpx.Response(404, json={"detail": f"unknown endpoint {endpoint}"})

    def _bg(self, data: bytes) -> dict[str, Any]:
        cut = Image.open(io.BytesIO(threshold_cutout(data)))
        uri = _png_uri(cut)
        if self.hosted_output:
            self._hosted_payload = base64.b64decode(uri.split(",", 1)[1])
            uri = "https://v3.fal.media/files/x/out.png"
        img = {"url": uri, "width": cut.width, "height": cut.height,
               "content_type": "image/png", "file_name": "out.png", "file_size": 1}
        return {"image": img, "mask_image": img}

    def _sam(self, data: bytes, body: dict[str, Any]) -> dict[str, Any]:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        rgb = np.asarray(im)
        w, h = im.size
        boxes = [Box(b["x_min"], b["y_min"], b["x_max"], b["y_max"])
                 for b in body.get("box_prompts", [])]
        masks = [color_mask(rgb, b, tol=60) for b in boxes]
        if self.merge_first_two and len(masks) >= 2:
            union = masks[0] | masks[1]
            masks = [union] + masks[2:]
        if self.extra_mask:
            extra = np.zeros((h, w), dtype=bool)
            extra[: h // 10, : w // 10] = True
            masks.append(extra)
        masks = masks[: body.get("max_masks", 3)]
        order = list(range(len(masks)))
        if self.shuffle_masks:
            order = order[::-1]
        masks = [masks[i] for i in order]
        scores = [round(0.9 - 0.05 * i, 3) for i in range(len(masks))]
        geo = []
        for m in masks:
            ys, xs = np.nonzero(m)
            if len(xs) == 0:
                geo.append([0, 0, 0, 0])
                continue
            geo.append([float((xs.min() + xs.max()) / 2 / w), float((ys.min() + ys.max()) / 2 / h),
                        float((xs.max() - xs.min()) / w), float((ys.max() - ys.min()) / h)])
        # metadata sorted by score descending, with deliberately wrong indexes
        meta = [{"index": (i + 1) % max(1, len(masks)), "score": s, "box": geo[i]}
                for i, s in sorted(enumerate(scores), key=lambda t: -t[1])][::-1]
        items = []
        for m in masks:
            uri = _png_uri(Image.fromarray(m.astype(np.uint8) * 255, "L"))
            items.append({"url": uri, "content_type": "image/png", "file_name": "m.png",
                          "file_size": 1, "width": w, "height": h})
        return {"masks": items, "metadata": meta, "scores": scores, "boxes": geo,
                "image": None}

    _hosted_payload: bytes = b""


class FakeSpend:
    """Records check/record calls; can be set to block."""

    def __init__(self, blocked: bool = False) -> None:
        self.blocked = blocked
        self.checks: list[str] = []
        self.records: list[dict[str, Any]] = []

    async def check(self, purpose: str):
        self.checks.append(purpose)
        if self.blocked:
            from mirror_api.spend import SpendCapExceeded, SpendStatus

            raise SpendCapExceeded(purpose, SpendStatus(
                ledger="dev", cap_usd=10, spent_usd=10, percent=100, mode="blocked",
                period_start=None, period_end=None))

    async def record(self, **kw):
        self.records.append(kw)


def assert_private(mock: FalMock) -> None:
    """Every request went to fal.run's run endpoint with the privacy header and sync_mode,
    and nothing went to an upload/storage endpoint."""
    assert mock.requests, "no fal requests were made"
    run_requests = [r for r in mock.requests if r.url.host == "fal.run"]
    for r in mock.requests:
        path = r.url.path.lower()
        assert "upload" not in path and "storage" not in path, r.url
        assert r.url.host in ("fal.run",) or r.url.host.endswith("fal.media"), r.url
        if r.url.host.endswith("fal.media"):
            assert r.method == "GET"
    for r, body in zip(run_requests, mock.bodies, strict=True):
        assert r.method == "POST"
        assert r.url.scheme == "https"
        assert r.headers["x-fal-store-io"] == "0"
        assert r.headers["authorization"].startswith("Key ")
        assert body["sync_mode"] is True
        endpoint = r.url.path.lstrip("/")
        assert endpoint in BG_ENDPOINTS | SAM_ENDPOINTS, endpoint
        if endpoint in SAM_ENDPOINTS:
            assert body["prompt"] and body["prompt"] != "wheel"
            assert body["apply_mask"] is False
            assert body["max_masks"] >= len(body["box_prompts"])
