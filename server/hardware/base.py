"""Hardware protocols. HARDWARE=mock selects hardware/mock.py; Pi drivers come later
and, per HANDOFF section 7, will live behind the camera-daemon socket.

Sensors push changes to subscribers; nothing polls. A subscriber is a plain
callable taking the new value (sync or async); `subscribe` returns an unsubscribe
function.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

Listener = Callable[[bool], None | Awaitable[None]]
Unsubscribe = Callable[[], None]


@runtime_checkable
class Camera(Protocol):
    @property
    def is_open(self) -> bool: ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def capture(self) -> bytes:
        """One still frame as JPEG bytes. Opens the camera if needed."""
        ...


@runtime_checkable
class CoverSensor(Protocol):
    """Hall sensor on the lens cover. It can only detect *closed* (HANDOFF C9)."""

    def is_closed(self) -> bool: ...

    def subscribe(self, listener: Listener) -> Unsubscribe: ...


@runtime_checkable
class PresenceSensor(Protocol):
    def is_present(self) -> bool: ...

    def subscribe(self, listener: Listener) -> Unsubscribe: ...


@runtime_checkable
class Leds(Protocol):
    @property
    def level(self) -> float: ...

    async def set_level(self, level: float, *, full_duty: bool = False) -> None:
        """0.0-1.0. full_duty=True means no PWM (capture, HANDOFF C7)."""
        ...


@runtime_checkable
class Display(Protocol):
    @property
    def is_on(self) -> bool: ...

    async def set_power(self, on: bool) -> None: ...


@dataclass
class Hardware:
    kind: str
    camera: Camera
    cover: CoverSensor
    presence: PresenceSensor
    leds: Leds
    display: Display

    async def close(self) -> None:
        await self.camera.close()
