"""Mock hardware, controllable from tests and dev tools.

    hw = build_mock_hardware()
    hw.cover.set_closed(False)        # fires subscribers
    hw.presence.set_present(True)
    hw.camera.frame = some_jpeg       # or hw.camera.fail_with = RuntimeError("busy")
    hw.leds.history / hw.display.history
"""

from __future__ import annotations

import asyncio
import inspect
import io
from functools import lru_cache

from hardware.base import Hardware, Listener, Unsubscribe


@lru_cache(maxsize=1)
def fixture_frame() -> bytes:
    """A deterministic 480x640 portrait JPEG: light background, dark 'person' block."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (480, 640), (232, 230, 226))
    d = ImageDraw.Draw(img)
    d.ellipse((190, 90, 290, 200), fill=(170, 140, 120))  # head
    d.rectangle((150, 210, 330, 470), fill=(40, 70, 120))  # shirt
    d.rectangle((165, 470, 315, 640), fill=(60, 60, 60))  # trousers
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


MOCK_FRAME_KINDS = ("good", "dark", "half_covered", "blurred", "overexposed")


def mock_frame(kind: str = "good", base: bytes | None = None) -> bytes:
    """Synthetic camera frames for the capture flow's quality check (dev and tests).
    `half_covered` is the cover partly closed while the sensor reads open."""
    from PIL import Image, ImageFilter

    src = base if base is not None else fixture_frame()
    if kind == "good":
        return src
    img = Image.open(io.BytesIO(src)).convert("RGB")
    w, h = img.size
    if kind == "dark":
        img = Image.new("RGB", (w, h), (6, 6, 8))
    elif kind == "half_covered":
        img.paste((4, 4, 4), (0, 0, w, int(h * 0.55)))
    elif kind == "blurred":
        img = img.filter(ImageFilter.GaussianBlur(radius=max(w, h) / 40))
    elif kind == "overexposed":
        img = img.point(lambda v: min(255, int(v * 2.6) + 60))
    else:
        raise ValueError(f"unknown mock frame kind {kind!r}; one of {MOCK_FRAME_KINDS}")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class _Notifier:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Unsubscribe:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self, value: bool) -> None:
        for fn in list(self._listeners):
            result = fn(value)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)


class MockCamera:
    def __init__(self, frame: bytes | None = None) -> None:
        self.frame = frame
        self.fail_with: Exception | None = None
        self.captures = 0
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False

    async def capture(self) -> bytes:
        if self.fail_with is not None:
            raise self.fail_with
        if not self._open:
            await self.open()
        self.captures += 1
        return self.frame if self.frame is not None else fixture_frame()


class MockCoverSensor(_Notifier):
    def __init__(self, closed: bool = True) -> None:
        super().__init__()
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed

    def set_closed(self, closed: bool) -> None:
        if closed != self._closed:
            self._closed = closed
            self._notify(closed)


class MockPresenceSensor(_Notifier):
    def __init__(self, present: bool = False) -> None:
        super().__init__()
        self._present = present

    def is_present(self) -> bool:
        return self._present

    def set_present(self, present: bool) -> None:
        if present != self._present:
            self._present = present
            self._notify(present)


class MockLeds:
    def __init__(self) -> None:
        self._level = 0.0
        self.history: list[tuple[float, bool]] = []

    @property
    def level(self) -> float:
        return self._level

    async def set_level(self, level: float, *, full_duty: bool = False) -> None:
        if not 0.0 <= level <= 1.0:
            raise ValueError("level must be within 0..1")
        self._level = level
        self.history.append((level, full_duty))


class MockDisplay:
    def __init__(self, on: bool = True) -> None:
        self._on = on
        self.history: list[bool] = []

    @property
    def is_on(self) -> bool:
        return self._on

    async def set_power(self, on: bool) -> None:
        self._on = on
        self.history.append(on)


class MockHardware(Hardware):
    camera: MockCamera
    cover: MockCoverSensor
    presence: MockPresenceSensor
    leds: MockLeds
    display: MockDisplay


def build_mock_hardware() -> MockHardware:
    return MockHardware(
        kind="mock",
        camera=MockCamera(),
        cover=MockCoverSensor(),
        presence=MockPresenceSensor(),
        leds=MockLeds(),
        display=MockDisplay(),
    )
