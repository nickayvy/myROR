"""Startup/shutdown hook (mirror_api.app FEATURE_PACKAGES): the integration timers run
only when the app starts its workers."""

from __future__ import annotations

from integrations import today


async def startup(services) -> None:
    await today.start(services)


async def shutdown(services) -> None:
    await today.stop(services)
