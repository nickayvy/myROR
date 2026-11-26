"""Presence watcher: what happens when someone walks up to the mirror (HANDOFF section 11).

A small in-process watcher over the `PresenceSensor` interface (subscribe, no polling).
Edges are debounced: an arrival must hold `presence_arrive_debounce_s`, a departure
`presence_leave_debounce_s` (PIR sensors flap). Then:

- arrival: broadcast `presence` {"present": true} at once (the mirror wakes its
  display), then, in the background and one at a time:
    * integrations.today.refresh_if_stale, only when the weather or calendars are due, or
      the day's database parts (reminders, spend mode, outfits) were never loaded;
    * recommender.ensure_fresh, only on the first arrival of the process, when the day's
      context signature (date, weather band, event formality, all from memory) changed
      since the last call, or the last call is older than ENSURE_MAX_AGE_S
      (MISSING_RECHECK_S while today has no outfits).
  When nothing is stale an arrival touches no database at all.
- departure: broadcast `presence` {"present": false}. Blanking the display after a
  delay is the mirror's job.

Started by the app's startup hooks (start_workers=True) and stopped at shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

ENSURE_MAX_AGE_S = 3 * 3600.0  # re-check outfits at least this often while people come by
MISSING_RECHECK_S = 10 * 60.0  # while today has no outfits (e.g. an empty closet)


class PresenceWatcher:
    def __init__(
        self,
        services: Any,
        *,
        arrive_debounce_s: float = 0.5,
        leave_debounce_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.services = services
        self.sensor = services.hardware.presence
        self.arrive_debounce_s = arrive_debounce_s
        self.leave_debounce_s = leave_debounce_s
        self.clock = clock
        self.present = False  # the debounced state last reported
        self._raw = False
        self._timer: asyncio.TimerHandle | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._work: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._last_signature: str | None = None
        self._last_ensure: float | None = None
        self.refreshes = 0  # integrations refreshes started (tests)
        self.ensures = 0  # recommender ensure_fresh calls (tests)

    # -- lifecycle

    def start(self) -> None:
        if self._unsubscribe is None:
            self._raw = bool(self.sensor.is_present())
            self._unsubscribe = self.sensor.subscribe(self._on_edge)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        tasks = [t for t in (self._work, *self._tasks) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._work = None
        self._tasks.clear()

    async def wait_idle(self) -> None:
        """Tests: wait for the background refresh started by the last arrival."""
        if self._work is not None:
            await asyncio.gather(self._work, return_exceptions=True)

    # -- edges

    def _on_edge(self, present: bool) -> None:
        self._raw = bool(present)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._raw == self.present:
            return  # a flap that returned to the reported state
        delay = self.arrive_debounce_s if self._raw else self.leave_debounce_s
        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(delay, self._settled)

    def _settled(self) -> None:
        self._timer = None
        if self._raw == self.present:
            return
        self.present = self._raw
        self._spawn(self._broadcast(self.present))
        if self.present and (self._work is None or self._work.done()):
            self._work = asyncio.ensure_future(self._on_arrival())

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _broadcast(self, present: bool) -> None:
        await self.services.hub.broadcast("presence", {"present": present})

    # -- arrival work

    async def _on_arrival(self) -> None:
        try:
            from integrations import today

            state = today.get_state(self.services)
            if not state.loaded:
                state.load()
            if self._integrations_stale(state):
                self.refreshes += 1
                await today.refresh_if_stale(self.services)
            if self._outfits_stale(state):
                self.ensures += 1
                self._last_ensure = self.clock()
                self._last_signature = self._signature(state)
                await self._ensure_fresh(state.today())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - presence must never break anything
            log.exception("presence refresh failed")

    def _integrations_stale(self, state) -> bool:
        db_parts_unknown = self.services.db.configured and not (
            state.reminders_loaded and state.spend_known)
        return not (state.weather_fresh() and state.calendars_fresh()) or db_parts_unknown

    def _outfits_stale(self, state) -> bool:
        if not self.services.db.configured:
            return False
        if self._last_ensure is None or self._signature(state) != self._last_signature:
            return True
        age = self.clock() - self._last_ensure
        if not state.outfits.get(state.today().isoformat()):
            return age >= MISSING_RECHECK_S
        return age >= ENSURE_MAX_AGE_S

    def _signature(self, state) -> str:
        """What the recommender's fingerprint depends on that is known without the
        database: the date, the weather band and the day's minimum formality."""
        from integrations.formality import day_formality
        from mirror_api.contracts import DayContext
        from recommender.rules import RulesConfig, weather_facts

        day = state.today()
        events = state.events_for(day)
        ctx = DayContext(date=day, weather=state.weather_for(day), events=events,
                         min_formality=day_formality(events))
        band = weather_facts(ctx, RulesConfig(temp_unit=self.services.settings.temp_unit)).band()
        return json.dumps([day.isoformat(), band, ctx.min_formality], sort_keys=True,
                          default=str)

    async def _ensure_fresh(self, day) -> None:
        from recommender.service import ensure_fresh

        await ensure_fresh(self.services, day)


def get_watcher(services: Any) -> PresenceWatcher:
    w = getattr(services, "presence", None)
    if w is None:
        s = services.settings
        w = PresenceWatcher(services, arrive_debounce_s=s.presence_arrive_debounce_s,
                            leave_debounce_s=s.presence_leave_debounce_s)
        services.presence = w
    return w


async def startup(services: Any) -> None:
    get_watcher(services).start()


async def shutdown(services: Any) -> None:
    w = getattr(services, "presence", None)
    if w is not None:
        await w.stop()
