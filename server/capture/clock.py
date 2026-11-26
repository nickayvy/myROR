"""Time for the capture state machine: a real clock, and a fake one for tests.

    clock = FakeClock()
    machine = CaptureMachine(..., clock=clock)
    await clock.advance(1.5)       # fires every sleep due within 1.5 s, in order
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))


async def settle(rounds: int = 50) -> None:
    """Let every ready task run (no real time passes)."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    """Time moves only when a test calls advance()."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.t + seconds, next(self._seq), fut))
        await fut

    @property
    def pending(self) -> int:
        return sum(1 for _, _, f in self._sleepers if not f.done())

    async def advance(self, seconds: float) -> None:
        target = self.t + seconds
        await settle()
        while self._sleepers and self._sleepers[0][0] <= target + 1e-9:
            at, _, fut = heapq.heappop(self._sleepers)
            self.t = max(self.t, at)
            if not fut.done():
                fut.set_result(None)
            await settle()
        self.t = target
        await settle()
