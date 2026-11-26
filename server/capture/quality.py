"""Frame quality check after the flash (HANDOFF section 9).

Rejects, each with its own message for the mirror and phone:
- `dark`: the frame is (nearly) black: the cover is closed although the sensor says
  open, or the sensor is faulty.
- `occluded`: a large part of the frame is black: the cover is half closed, a finger or
  towel is over the lens.
- `overexposed`: a large part is blown out to white.
- `blurred`: too little fine detail: motion, fog on the lens, or focus.
Person detection is not done yet; `PersonDetector` is the seam for it (a detector
returning False rejects with `no_person`).

Thresholds are starting values, not measured on the real camera [UNTESTED]: tune them
with frames from the bathroom's real lighting.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from PIL import Image

ANALYSE_WIDTH = 240
DARK_MEAN = 22.0  # mean luminance 0-255 below this: black frame
DARK_PIXEL = 18  # a pixel this dark counts as "covered"
OCCLUDED_FRACTION = 0.30  # this much of the frame covered: occluded
BRIGHT_PIXEL = 250
OVEREXPOSED_FRACTION = 0.35
BLUR_MIN = 12.0  # variance of the Laplacian (on the analysed frame) below this: blurred

MESSAGES = {
    "dark": "The photo came out black. Slide the camera cover fully open.",
    "occluded": "Part of the lens is covered. Slide the camera cover fully open.",
    "overexposed": "The photo is too bright. Turn the lights down a little.",
    "blurred": "The photo is blurred. Hold still, and wipe the camera if it is fogged.",
    "no_person": "Nobody in the photo. Stand in front of the mirror.",
    "unreadable": "The camera returned an unreadable photo. Trying again.",
    "camera_error": "The camera did not take a photo. Trying again.",
}


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    code: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def message(self) -> str | None:
        return MESSAGES.get(self.code) if self.code else None


class PersonDetector(Protocol):
    """Seam: True when a person is in the frame. Not implemented yet (skipped)."""

    def has_person(self, jpeg: bytes) -> bool: ...


def _laplacian_var(g: np.ndarray) -> float:
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def check_frame(jpeg: bytes, person_detector: PersonDetector | None = None) -> QualityResult:
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.draft("L", (ANALYSE_WIDTH * 2, ANALYSE_WIDTH * 2))
        g = img.convert("L")
    except Exception:  # noqa: BLE001 - corrupt or not an image
        return QualityResult(False, "unreadable")
    if g.width > ANALYSE_WIDTH:
        g = g.resize((ANALYSE_WIDTH, max(1, round(g.height * ANALYSE_WIDTH / g.width))))
    a = np.asarray(g, dtype=np.float32)
    metrics = {
        "mean": round(float(a.mean()), 2),
        "dark_fraction": round(float((a <= DARK_PIXEL).mean()), 4),
        "bright_fraction": round(float((a >= BRIGHT_PIXEL).mean()), 4),
    }
    if metrics["mean"] < DARK_MEAN:
        return QualityResult(False, "dark", metrics)
    if metrics["dark_fraction"] >= OCCLUDED_FRACTION:
        return QualityResult(False, "occluded", metrics)
    if metrics["bright_fraction"] >= OVEREXPOSED_FRACTION:
        return QualityResult(False, "overexposed", metrics)
    # Sharpness on the uncovered, unsaturated part only.
    metrics["sharpness"] = round(_laplacian_var(a), 2)
    if metrics["sharpness"] < BLUR_MIN:
        return QualityResult(False, "blurred", metrics)
    if person_detector is not None and not person_detector.has_person(jpeg):
        return QualityResult(False, "no_person", metrics)
    return QualityResult(True, None, metrics)
