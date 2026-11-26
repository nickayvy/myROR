"""Background removal on fal, one class for every endpoint we bake off.

Request shapes from fal's model pages (llms.txt, 2026-11-25):
- fal-ai/birefnet/v2: image_url, model, operating_resolution, output_format, refine_foreground,
  output_mask, mask_only, sync_mode. Output image.url (+ mask_image when output_mask).
- bria/rmbg-3: image_url, prompt (empty = standard cutout), sync_mode. Output image.url.
- pixelcut/background-removal: image_url, output_format rgba|alpha|zip, sync_mode (default
  true). Output image.url.
- fal-ai/feynobg: image_url, seed, sync_mode, enable_safety_checker. Output image.url.
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

from PIL import Image

from providers.bg_remover.base import BgRemovalError, CutoutResult, normalise_rgba
from providers.fal import FalClient

BIREFNET = "fal-ai/birefnet/v2"
BIREFNET_MODELS = (
    "General Use (Light)", "General Use (Light 2K)", "General Use (Heavy)", "Matting",
    "Portrait", "General Use (Dynamic)",
)
BIREFNET_RESOLUTIONS = ("1024x1024", "2048x2048", "2304x2304")

# name -> (endpoint, options). Used by eval/bakeoff_bg.py and selectable by FAL_BG_ENDPOINT.
VARIANTS: dict[str, tuple[str, dict[str, Any]]] = {
    "birefnet-light": (BIREFNET, {"model": "General Use (Light)"}),
    "birefnet-light-2k": (BIREFNET, {"model": "General Use (Light 2K)",
                                     "operating_resolution": "2048x2048"}),
    "birefnet-heavy": (BIREFNET, {"model": "General Use (Heavy)"}),
    "birefnet-matting": (BIREFNET, {"model": "Matting"}),
    "birefnet-dynamic-2304": (BIREFNET, {"model": "General Use (Dynamic)",
                                         "operating_resolution": "2304x2304"}),
    "rmbg-3": ("bria/rmbg-3", {}),
    "pixelcut": ("pixelcut/background-removal", {}),
    "feynobg": ("fal-ai/feynobg", {}),
}


def build_arguments(endpoint: str, image_url: str, options: dict[str, Any]) -> dict[str, Any]:
    if endpoint == BIREFNET:
        model = options.get("model", "General Use (Light)")
        res = options.get("operating_resolution", "1024x1024")
        if model not in BIREFNET_MODELS:
            raise ValueError(f"unknown BiRefNet model {model!r}")
        if res not in BIREFNET_RESOLUTIONS:
            raise ValueError(f"unknown BiRefNet resolution {res!r}")
        if res == "2304x2304" and model != "General Use (Dynamic)":
            raise ValueError("2304x2304 is only available with 'General Use (Dynamic)'")
        return {
            "image_url": image_url, "model": model, "operating_resolution": res,
            "output_format": "png", "refine_foreground": options.get("refine_foreground", True),
            "output_mask": False, "mask_only": False,
        }
    if endpoint == "pixelcut/background-removal":
        return {"image_url": image_url, "output_format": "rgba"}
    if endpoint == "bria/rmbg-3":
        args: dict[str, Any] = {"image_url": image_url}
        if options.get("prompt"):
            args["prompt"] = options["prompt"]
        return args
    # feynobg and anything else that takes just an image
    return {"image_url": image_url, **{k: v for k, v in options.items() if k != "image_url"}}


class FalBgRemover:
    def __init__(self, fal: FalClient, endpoint: str = BIREFNET,
                 options: dict[str, Any] | None = None) -> None:
        self.fal = fal
        self.endpoint = endpoint
        self.options = dict(options or {})
        build_arguments(endpoint, "data:,", self.options)  # validate options now
        self.name = f"fal:{endpoint}"

    @property
    def model(self) -> str:
        if self.endpoint == BIREFNET:
            return f"{self.endpoint}:{self.options.get('model', 'General Use (Light)')}"
        return self.endpoint

    async def remove(
        self,
        *,
        data: bytes,
        key: str | None = None,
        content_type: str = "image/jpeg",
        purpose: str = "bg_remove",
        job_id: UUID | None = None,
    ) -> CutoutResult:
        size = Image.open(io.BytesIO(data)).size
        image_url = await self.fal.image_input(data=data, key=key, content_type=content_type)
        args = build_arguments(self.endpoint, image_url, self.options)
        result = await self.fal.run(self.endpoint, args, purpose=purpose, job_id=job_id)
        image = result.get("image")
        if not isinstance(image, dict) or not isinstance(image.get("url"), str):
            raise BgRemovalError(f"{self.endpoint}: response has no image.url")
        png = await self.fal.fetch_output(image["url"])
        return CutoutResult(
            normalise_rgba(png, size), self.name, self.model,
            meta={"sync_data_uri": self.fal.last_output_was_data_uri},
        )
