"""SEAM, NOT IMPLEMENTED: camera calibration and mirror-move detection (HANDOFF section 9).

What belongs here later:
- `lock(camera)`: before the flash, lock white balance, exposure and gain to the values
  measured under capture light (stored in the `calibrations` table).
- `check(jpeg)`: after the quality check, compare a background reference patch (person
  masked out): same structure with a colour shift -> lighting change (auto-correct);
  structure changed -> "New room detected. Recalibrate?"; all matched items drifting the
  same way -> flag for recalibration. Returns a message to show, or None.

The capture state machine calls both through this protocol; the default does nothing.
"""

from __future__ import annotations

from typing import Any, Protocol


class Calibration(Protocol):
    async def lock(self, camera: Any) -> None: ...

    async def check(self, jpeg: bytes) -> str | None: ...


class NoCalibration:
    async def lock(self, camera: Any) -> None:
        return None

    async def check(self, jpeg: bytes) -> str | None:
        return None
