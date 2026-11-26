"""Startup/shutdown hook: a restart mid-capture comes back idle (state is in memory), and
shutdown switches the LEDs off and closes the camera."""

from __future__ import annotations

from capture import get_machine


async def startup(services) -> None:
    get_machine(services)


async def shutdown(services) -> None:
    m = getattr(services, "capture", None)
    if m is not None:
        await m.close()
        services.capture = None
