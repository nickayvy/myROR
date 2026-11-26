"""FashionCLIP image embeddings via open_clip (the optional `ml` extra).

Nothing heavy is imported at module import or app startup: torch and open_clip load on the
first `embed()` call, in a worker thread, once per process. Default weights are
`hf-hub:Marqo/marqo-fashionCLIP` (ViT-B-16, 512 dims), downloaded from Hugging Face on first
use; set EMBEDDER_MODEL to change. [UNTESTED] on the Pi: speed and memory unmeasured.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import threading
from typing import Any

import numpy as np
from PIL import Image

from providers.embedder.base import EmbedderUnavailable, unit

log = logging.getLogger(__name__)


def ml_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "open_clip"))


class FashionClipEmbedder:
    def __init__(self, model_name: str = "hf-hub:Marqo/marqo-fashionCLIP") -> None:
        self.model_name = model_name
        self.name = f"open_clip:{model_name}"
        self._lock = threading.Lock()
        self._model: Any = None
        self._preprocess: Any = None
        self._torch: Any = None

    @property
    def available(self) -> bool:
        return ml_installed()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if not ml_installed():
                raise EmbedderUnavailable("torch/open_clip not installed (uv sync --extra ml)")
            import open_clip  # noqa: PLC0415 - lazy on purpose
            import torch  # noqa: PLC0415

            log.info("loading embedder %s", self.model_name)
            model, _, preprocess = open_clip.create_model_and_transforms(self.model_name)
            model.eval()
            torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
            self._model, self._preprocess, self._torch = model, preprocess, torch

    def _embed_sync(self, image: bytes) -> np.ndarray:
        self._load()
        im = Image.open(io.BytesIO(image))
        if im.mode in ("RGBA", "LA", "PA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").getchannel("A"))
            im = bg
        x = self._preprocess(im.convert("RGB")).unsqueeze(0)
        with self._torch.no_grad():
            v = self._model.encode_image(x)
        return unit(v[0].float().cpu().numpy())

    async def embed(self, image: bytes) -> np.ndarray:
        return await asyncio.to_thread(self._embed_sync, image)
