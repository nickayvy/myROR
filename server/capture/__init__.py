"""Capture flow (HANDOFF section 9): state machine (machine.py), quality gate (quality.py),
calibration seam (calibration.py). `get_machine(services)` is the process-wide machine;
routes in mirror_api/routers/capture.py and capture_image.py."""

from __future__ import annotations

from typing import Any

from capture.machine import (
    STATES,
    CaptureBusy,
    CaptureError,
    CaptureMachine,
    SaveFailed,
    Timings,
    UnknownCapture,
    WrongState,
)

__all__ = ["STATES", "CaptureBusy", "CaptureError", "CaptureMachine", "SaveFailed",
           "Timings", "UnknownCapture", "WrongState", "get_machine"]


def get_machine(services: Any) -> CaptureMachine:
    """The machine on the Services field `capture`, created (and subscribed to the cover
    sensor) on first use. Kept photos go to the check-in pipeline."""
    m = getattr(services, "capture", None)
    if m is None:
        async def keep(jpeg: bytes):
            from pipeline.ingest import checkin_from_bytes

            return await checkin_from_bytes(services, jpeg)

        m = CaptureMachine(services.hardware, services.hub, on_keep=keep)
        m.open()
        services.capture = m
    return m
