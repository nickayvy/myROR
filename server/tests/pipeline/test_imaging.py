from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from pipeline import imaging
from pipeline.imaging import ImageRejected, crop_cutout, prepare_upload, white_balance
from providers.bg_remover.fake import threshold_cutout
from tests.pipeline import synth


def _open(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg))


def _no_metadata(jpeg: bytes) -> None:
    im = _open(jpeg)
    exif = im.getexif()
    assert len(exif) == 0, dict(exif)
    assert not exif.get_ifd(0x8825)  # GPS
    assert "exif" not in im.info and "xmp" not in im.info and "XML:com.adobe.xmp" not in im.info
    assert b"GPS" not in jpeg and b"Exif\x00\x00" not in jpeg


def test_heic_decoded_gps_stripped():
    heic = synth.heic_with_gps()
    src = Image.open(io.BytesIO(heic))
    assert src.format == "HEIF" and src.getexif().get_ifd(0x8825)  # the fixture has GPS
    p = prepare_upload(heic)
    assert _open(p.jpeg).format == "JPEG"
    assert (p.width, p.height) == (1000, 1400)  # upright
    assert p.flags["exif_orientation"] == 6
    im = _open(p.jpeg).convert("RGB")
    top = np.asarray(im.crop((440, 30, 560, 50)), dtype=float).reshape(-1, 3).mean(axis=0)
    assert np.linalg.norm(top - synth.PALETTE["yellow"]) < 50
    assert p.taken_at is not None and p.taken_at.year == 2026  # read before stripping
    _no_metadata(p.jpeg)


def test_jpeg_gps_stripped():
    p = prepare_upload(synth.jpeg_with_gps())
    _no_metadata(p.jpeg)


def test_exif_orientation_applied_then_dropped():
    raw = synth.rotated_jpeg()
    assert _open(raw).size == (1600, 1200)  # stored sideways
    p = prepare_upload(raw)
    im = _open(p.jpeg).convert("RGB")
    assert im.size == (1200, 1600)  # upright portrait
    assert p.flags["exif_orientation"] == 6
    # the yellow marker is at the top after correction
    top = np.asarray(im.crop((530, 30, 670, 50)), dtype=float).reshape(-1, 3).mean(axis=0)
    bottom = np.asarray(im.crop((530, 1550, 670, 1570)), dtype=float).reshape(-1, 3).mean(0)
    assert np.linalg.norm(top - synth.PALETTE["yellow"]) < 50
    assert np.linalg.norm(bottom - synth.PALETTE["yellow"]) > 100
    _no_metadata(p.jpeg)


def test_downscale_to_stored_maximum():
    big = synth.garment_jpeg("red", "tee", size=(4000, 3000))
    p = prepare_upload(big)
    assert max(p.width, p.height) == imaging.STORED_MAX_EDGE
    assert p.flags["original_size"] == [4000, 3000]


def test_white_balance_neutralises_a_warm_sheet():
    warm = synth.garment("navy", "tee", sheet=(250, 225, 190))
    out, info = white_balance(warm)
    assert info["applied"] is True
    corner = np.asarray(out, dtype=float)[:40, :40].reshape(-1, 3).mean(axis=0)
    assert corner.max() - corner.min() < 8  # neutral now


def test_white_balance_skipped_without_a_sheet():
    im = Image.open(io.BytesIO(synth.person_jpeg())).convert("RGB")  # grey wall, no sheet
    out, info = white_balance(im)
    assert info["applied"] is False and out is im


@pytest.mark.parametrize("data,code", [
    (b"not an image", "unsupported_type"),
    (b"GIF89a" + b"\0" * 100, "unsupported_type"),
])
def test_rejects(data, code):
    with pytest.raises(ImageRejected) as e:
        prepare_upload(data)
    assert e.value.code == code


def test_rejects_tiny_and_gif():
    out = io.BytesIO()
    Image.new("RGB", (32, 32)).save(out, "JPEG")
    with pytest.raises(ImageRejected) as e:
        prepare_upload(out.getvalue())
    assert e.value.code == "too_small"
    out = io.BytesIO()
    Image.new("RGB", (200, 200)).save(out, "GIF")
    with pytest.raises(ImageRejected) as e:
        prepare_upload(out.getvalue())
    assert e.value.code == "unsupported_type"


def test_crop_cutout_to_garment():
    png = threshold_cutout(synth.garment_jpeg("red", "tee"))
    png2, box, coverage = crop_cutout(png)
    im = _open(png2)
    assert im.mode == "RGBA"
    assert 0.1 < coverage < 0.6
    assert box.x_min > 200 and box.x_max < 1400  # tight around the shirt
    blank = io.BytesIO()
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(blank, "PNG")
    assert crop_cutout(blank.getvalue()) is None


def test_dominant_labs_measures_colour():
    png, _, _ = crop_cutout(threshold_cutout(synth.garment_jpeg("red", "tee")))
    (lab, share), *_ = imaging.dominant_labs(png)
    assert share > 0.8 and lab[1] > 40  # strongly red (positive a*)
