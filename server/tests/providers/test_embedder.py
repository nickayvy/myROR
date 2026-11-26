from __future__ import annotations

import io
import subprocess
import sys

import numpy as np
from PIL import Image

from providers.bg_remover.fake import threshold_cutout
from providers.embedder import FakeEmbedder, FashionClipEmbedder, build_embedder, ml_installed
from tests.pipeline import synth


def cut(jpeg: bytes) -> bytes:
    return threshold_cutout(jpeg)


async def test_fake_is_unit_deterministic_and_colour_shape_sensitive():
    e = FakeEmbedder()
    a = await e.embed(cut(synth.garment_jpeg("red", "tee")))
    assert a.dtype == np.float32 and a.shape == (512,)
    assert abs(float(np.linalg.norm(a)) - 1) < 1e-5
    assert np.allclose(a, await e.embed(cut(synth.garment_jpeg("red", "tee"))))
    shifted = await e.embed(cut(synth.garment_jpeg("red", "tee", offset=(60, -40),
                                                    scale=0.92)))
    other_colour = await e.embed(cut(synth.garment_jpeg("green", "tee")))
    other_shape = await e.embed(cut(synth.garment_jpeg("red", "trousers")))
    near = float(a @ shifted)
    assert near > 0.95
    assert float(a @ other_colour) < near - 0.15
    assert float(a @ other_shape) < near - 0.1


async def test_fake_handles_rgb_input():
    out = io.BytesIO()
    Image.new("RGB", (50, 50), (10, 200, 10)).save(out, "PNG")
    v = await FakeEmbedder().embed(out.getvalue())
    assert v.shape == (512,)


def test_fashionclip_is_lazy_and_build_returns_none_without_ml(settings):
    # Importing the provider package must not import torch/open_clip.
    code = ("import sys, providers.embedder, pipeline.wiring, mirror_api.app;"
            "print('torch' in sys.modules, 'open_clip' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "False False"
    e = FashionClipEmbedder()
    assert not e.loaded
    if not ml_installed():
        assert build_embedder(settings.model_copy(update={"embedder": "fashionclip"})) is None
        assert e.available is False
    assert isinstance(build_embedder(settings.model_copy(update={"embedder": "fake"})),
                      FakeEmbedder)
