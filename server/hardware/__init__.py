"""Hardware seam: protocols in base.py, mocks in mock.py. Pi drivers: not written yet."""

from __future__ import annotations

from hardware.base import Camera, CoverSensor, Display, Hardware, Leds, PresenceSensor
from hardware.mock import MockHardware, build_mock_hardware, fixture_frame

__all__ = [
    "Camera",
    "CoverSensor",
    "Display",
    "Hardware",
    "Leds",
    "MockHardware",
    "PresenceSensor",
    "build_hardware",
    "build_mock_hardware",
    "fixture_frame",
]


def build_hardware(settings) -> Hardware:
    if settings.hardware == "mock":
        return build_mock_hardware()
    raise NotImplementedError("HARDWARE=pi: Pi drivers are not written yet (M0/M4)")
