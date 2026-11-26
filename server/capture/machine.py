"""The mirror capture flow (HANDOFF section 9), in the API process.

    idle -> cover_wait -> preview -> countdown -> flash -> quality_check -> review
         -> cover_reminder -> idle

- `cover_wait`: "Slide the camera cover open" until the cover sensor stops reading
  closed, 60 s, or cancel. Skipped when the cover is already open.
- `preview`: camera open, about 1.5 s. Closing the cover aborts (also in countdown).
- `countdown`: 3-2-1, one `capture.state` event per tick (`seconds_left`).
- `flash`: the event tells the mirror to ramp its screen to white (its job); after
  ~200 ms the LEDs go to 100 % duty (no PWM), one frame is taken, the LEDs go off.
- `quality_check`: dark / occluded (covers "sensor says open but the cover is half
  closed") / overexposed / blurred, each with its own message, then back to preview for
  another try after a short pause (at most `max_attempts` frames per capture).
- `review`: a short-lived `review_url` to the frame (held in memory only); keep, retake,
  cancel, or 60 s with no answer (discarded). Keep hands the bytes to the check-in
  pipeline (`on_keep`).
- `cover_reminder`: after a capture ends with the cover open, until it reads closed.
  A new capture may start from here (it goes straight to preview).

One capture at a time. State is in memory only: a restart returns to idle. Calibration
and move detection are a seam (capture/calibration.py); person detection is a seam in
capture/quality.py.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from capture.calibration import Calibration, NoCalibration
from capture.clock import Clock, RealClock
from capture.quality import MESSAGES, PersonDetector, QualityResult, check_frame

log = logging.getLogger(__name__)

IDLE, COVER_WAIT, PREVIEW, COUNTDOWN, FLASH, QUALITY, REVIEW, REMINDER = (
    "idle", "cover_wait", "preview", "countdown", "flash", "quality_check", "review",
    "cover_reminder")
STATES = (IDLE, COVER_WAIT, PREVIEW, COUNTDOWN, FLASH, QUALITY, REVIEW, REMINDER)
EVENT = "capture.state"


@dataclass(frozen=True)
class Timings:
    cover_wait_s: float = 60.0
    preview_s: float = 1.5
    countdown_from: int = 3
    tick_s: float = 1.0
    flash_ramp_s: float = 0.2
    retry_pause_s: float = 2.0
    review_s: float = 60.0
    max_attempts: int = 3


class CaptureError(Exception):
    status = 409


class CaptureBusy(CaptureError):
    status = 409


class WrongState(CaptureError):
    status = 409


class UnknownCapture(CaptureError):
    status = 404


class SaveFailed(CaptureError):
    status = 503


@dataclass
class _Event:
    kind: str  # cover_open | cover_closed | keep | retake | cancel
    future: asyncio.Future | None = None


KeepHandler = Callable[[bytes], Awaitable[Any]]


class CaptureMachine:
    def __init__(
        self,
        hardware: Any,
        hub: Any,
        *,
        on_keep: KeepHandler,
        clock: Clock | None = None,
        timings: Timings | None = None,
        quality: Callable[..., QualityResult] = check_frame,
        person_detector: PersonDetector | None = None,
        calibration: Calibration | None = None,
        threaded_checks: bool = True,
    ) -> None:
        self.hw = hardware
        self.hub = hub
        self.on_keep = on_keep
        self.clock = clock or RealClock()
        self.t = timings or Timings()
        self.quality = quality
        self.person_detector = person_detector
        self.calibration = calibration or NoCalibration()
        self.threaded_checks = threaded_checks

        self.state = IDLE
        self.capture_id: str | None = None
        self.message: str | None = None
        self.seconds_left: int | None = None
        self._deadline: float | None = None
        self.attempts = 0
        self.last_quality: QualityResult | None = None
        self.kept_photo_id: Any = None
        self._pending_message: str | None = None
        self._frame: bytes | None = None
        self._review_token: str | None = None
        self._queue: asyncio.Queue[_Event] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self.history: list[str] = []  # states entered (tests)

    # ---- lifecycle

    def open(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self.hw.cover.subscribe(self._on_cover)

    async def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self._stop_task()
        self._reset()

    async def _stop_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _reset(self) -> None:
        self.state, self.capture_id, self.message = IDLE, None, None
        self.seconds_left = self._deadline = None
        self._frame = self._review_token = None
        self._fail_pending()

    @property
    def cover_open(self) -> bool:
        return not self.hw.cover.is_closed()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _on_cover(self, closed: bool) -> None:
        if self.running:
            self._queue.put_nowait(_Event("cover_closed" if closed else "cover_open"))

    # ---- commands (validated synchronously so the API can answer 404/409 at once)

    async def start(self) -> dict[str, Any]:
        if self.running and self.state != REMINDER:
            raise CaptureBusy("a capture is already running")
        await self._stop_task()  # ends a cover reminder
        self._fail_pending()
        self.state, self.message, self._deadline, self.seconds_left = IDLE, None, None, None
        self._queue = asyncio.Queue()
        self.capture_id = uuid4().hex
        self.attempts = 0
        self.kept_photo_id = None
        first = PREVIEW if self.cover_open else COVER_WAIT
        cid = self.capture_id
        self._task = asyncio.ensure_future(self._run(cid, first))
        await self._yield_until_entered()
        return {"capture_id": cid, "state": self.state}

    async def _yield_until_entered(self) -> None:
        for _ in range(20):
            if self.state != IDLE or self._task is None or self._task.done():
                return
            await asyncio.sleep(0)

    def _check(self, capture_id: str, allowed: tuple[str, ...]) -> None:
        if not self.running or capture_id != self.capture_id:
            raise UnknownCapture("no such capture running")
        if self.state not in allowed:
            raise WrongState(f"capture is in {self.state}")

    async def keep(self, capture_id: str) -> Any:
        self._check(capture_id, (REVIEW,))
        fut = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Event("keep", fut))
        photo_id = await fut
        await self._settle_after(REVIEW)
        return photo_id

    async def retake(self, capture_id: str) -> dict[str, Any]:
        self._check(capture_id, (REVIEW,))
        fut = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Event("retake", fut))
        await fut
        await self._settle_after(REVIEW)
        return self.snapshot_short()

    async def cancel(self, capture_id: str) -> dict[str, Any]:
        self._check(capture_id, STATES)
        if self.state != REMINDER:  # the reminder persists until the cover closes
            fut = asyncio.get_running_loop().create_future()
            self._queue.put_nowait(_Event("cancel", fut))
            before = self.state
            await fut
            await self._settle_after(before)
        return {"capture_id": capture_id, "state": self.state if self.running else IDLE}

    async def _settle_after(self, old: str) -> None:
        """Let the flow enter its next state (no clock time passes in between)."""
        for _ in range(20):
            if self.state != old or not self.running:
                return
            await asyncio.sleep(0)

    def image(self, capture_id: str, token: str) -> bytes | None:
        if (self.state == REVIEW and capture_id == self.capture_id and self._frame
                and self._review_token and secrets.compare_digest(token, self._review_token)):
            return self._frame
        return None

    # ---- views

    def snapshot_short(self) -> dict[str, Any]:
        return {"capture_id": self.capture_id, "state": self.state}

    def current(self) -> dict[str, Any]:
        if self.state == IDLE or not self.running:
            return {"state": IDLE}
        return self._payload()

    def _live_seconds(self) -> int | None:
        if self._deadline is not None:
            return max(0, math.ceil(self._deadline - self.clock.now() - 1e-9))
        return self.seconds_left

    def _payload(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "state": self.state,
            "message": self.message,
            "seconds_left": self._live_seconds(),
            "review_url": self._review_url() if self.state == REVIEW else None,
        }

    def _review_url(self) -> str | None:
        if not self._review_token:
            return None
        return f"/api/capture/{self.capture_id}/image?t={self._review_token}"

    async def _enter(self, state: str, message: str | None = None, *,
                     seconds: float | None = None, countdown: int | None = None) -> None:
        self.state, self.message = state, message
        self._deadline = self.clock.now() + seconds if seconds is not None else None
        self.seconds_left = countdown
        self.history.append(state)
        await self._emit()

    async def _emit(self) -> None:
        try:
            await self.hub.broadcast(EVENT, self._payload())
        except Exception:  # noqa: BLE001 - a broken hub must not stop the capture
            log.exception("capture.state broadcast failed")

    # ---- events

    def _fail_pending(self) -> None:
        while not self._queue.empty():
            ev = self._queue.get_nowait()
            if ev.future is not None and not ev.future.done():
                ev.future.set_exception(WrongState("the capture moved on"))

    async def _next(self, timeout: float | None) -> _Event | None:
        if not self._queue.empty():
            return self._queue.get_nowait()
        if timeout is not None and timeout <= 0:
            return None
        get = asyncio.ensure_future(self._queue.get())
        waiters = {get}
        sleeper = None
        if timeout is not None:
            sleeper = asyncio.ensure_future(self.clock.sleep(timeout))
            waiters.add(sleeper)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if sleeper is not None and not sleeper.done():
                sleeper.cancel()
            if not get.done():
                get.cancel()
        if get.done() and not get.cancelled():
            return get.result()
        return None

    async def _wait(self, kinds: set[str], timeout: float | None) -> _Event | None:
        """The next event of one of `kinds`, or None at the timeout. Commands that do not
        apply here are refused (their caller gets 409)."""
        deadline = None if timeout is None else self.clock.now() + timeout
        while True:
            remaining = None if deadline is None else deadline - self.clock.now()
            if remaining is not None and remaining <= 1e-9:
                return None
            ev = await self._next(remaining)
            if ev is None:
                return None
            if ev.kind in kinds:
                return ev
            if ev.future is not None and not ev.future.done():
                ev.future.set_exception(WrongState(f"capture is in {self.state}"))

    @staticmethod
    def _ack(ev: _Event | None, result: Any = None) -> None:
        if ev is not None and ev.future is not None and not ev.future.done():
            ev.future.set_result(result)

    # ---- the flow

    async def _run(self, cid: str, first: str) -> None:
        state: str | None = first
        try:
            while state is not None and cid == self.capture_id:
                state = await getattr(self, "_s_" + state)()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("capture %s failed in %s", cid, self.state)
            await self._end("Something went wrong with the camera. Try again.")
        finally:
            await self._lights_off()
            await self._camera_close()
            self._fail_pending()

    async def _end(self, message: str | None) -> None:
        """Back to idle; the last event carries the capture id and a closing message."""
        self._frame = self._review_token = None
        await self._enter(IDLE, message)
        await self._camera_close()

    async def _after_capture(self, message: str | None) -> str | None:
        self._frame = self._review_token = None
        if self.cover_open:
            self._pending_message = message
            return REMINDER
        await self._end(message)
        return None

    async def _abort_cover_closed(self) -> None:
        await self._end("The camera cover closed, so the photo was cancelled.")

    async def _s_cover_wait(self) -> str | None:
        await self._enter(COVER_WAIT, "Slide the camera cover open", seconds=self.t.cover_wait_s)
        ev = await self._wait({"cover_open", "cancel"}, self.t.cover_wait_s)
        if ev is None:
            await self._end("No photo: the camera cover stayed closed.")
            return None
        if ev.kind == "cancel":
            await self._end("Cancelled.")
            self._ack(ev)
            return None
        return PREVIEW

    async def _s_preview(self) -> str | None:
        await self.hw.camera.open()
        await self._enter(PREVIEW, "Get ready")
        if not self.cover_open:
            await self._abort_cover_closed()
            return None
        ev = await self._wait({"cover_closed", "cancel"}, self.t.preview_s)
        return await self._interrupted(ev) if ev else COUNTDOWN

    async def _interrupted(self, ev: _Event) -> str | None:
        if ev.kind == "cover_closed":
            await self._abort_cover_closed()
            return None
        nxt = await self._after_capture("Cancelled.")  # cancel
        self._ack(ev)
        return nxt

    async def _s_countdown(self) -> str | None:
        for n in range(self.t.countdown_from, 0, -1):
            await self._enter(COUNTDOWN, None, countdown=n)
            ev = await self._wait({"cover_closed", "cancel"}, self.t.tick_s)
            if ev is not None:
                return await self._interrupted(ev)
        return FLASH

    async def _s_flash(self) -> str | None:
        await self._enter(FLASH, "Hold still")
        await self.calibration.lock(self.hw.camera)
        await self.clock.sleep(self.t.flash_ramp_s)  # the mirror ramps its screen to white
        self._frame = None
        try:
            await self.hw.leds.set_level(1.0, full_duty=True)
            self._frame = await self.hw.camera.capture()
        except Exception as e:  # noqa: BLE001 - reported by the quality check
            log.warning("capture failed: %s", type(e).__name__)
        finally:
            await self._lights_off()
        return QUALITY

    async def _s_quality_check(self) -> str | None:
        await self._enter(QUALITY, "Checking the photo")
        self.attempts += 1
        if self._frame is None:
            result = QualityResult(False, "camera_error")
        elif self.threaded_checks:
            result = await asyncio.to_thread(self.quality, self._frame, self.person_detector)
        else:
            result = self.quality(self._frame, self.person_detector)
        self.last_quality = result
        if result.ok:
            note = await self.calibration.check(self._frame)  # seam; None for now
            self._pending_message = note
            return REVIEW
        self._frame = None
        message = result.message or MESSAGES["unreadable"]
        if self.attempts >= self.t.max_attempts:
            return await self._after_capture(f"No photo taken. {message}")
        await self._enter(QUALITY, message)
        ev = await self._wait({"cover_closed", "cancel"}, self.t.retry_pause_s)
        return await self._interrupted(ev) if ev else PREVIEW

    async def _s_review(self) -> str | None:
        self._review_token = secrets.token_urlsafe(18)
        note = getattr(self, "_pending_message", None)
        self._pending_message = None
        await self._enter(REVIEW, note or "Keep this photo?", seconds=self.t.review_s)
        deadline = self.clock.now() + self.t.review_s
        while True:
            ev = await self._wait({"keep", "retake", "cancel"}, deadline - self.clock.now())
            if ev is None:
                return await self._after_capture("No answer, so the photo was discarded.")
            if ev.kind == "cancel":
                nxt = await self._after_capture("Photo discarded.")
                self._ack(ev)
                return nxt
            if ev.kind == "retake":
                self._frame = self._review_token = None
                self.attempts = 0
                self._ack(ev)
                return PREVIEW if self.cover_open else COVER_WAIT
            # keep
            frame = self._frame
            try:
                photo_id = await self.on_keep(frame)
            except Exception as e:  # noqa: BLE001 - e.g. database or bucket unreachable
                log.warning("keeping the capture failed: %s", type(e).__name__)
                if ev.future is not None and not ev.future.done():
                    ev.future.set_exception(SaveFailed("could not save the photo"))
                self.message = "Could not save the photo. Try Keep again."
                await self._emit()
                continue
            self.kept_photo_id = photo_id
            self._ack(ev, photo_id)
            return await self._after_capture("Saved. Checking what you are wearing.")

    async def _s_cover_reminder(self) -> str | None:
        msg = getattr(self, "_pending_message", None)
        self._pending_message = None
        await self._camera_close()
        await self._enter(REMINDER, f"{msg} Close the camera cover." if msg
                          else "Close the camera cover.")
        if not self.cover_open:
            await self._end(msg)
            return None
        await self._wait({"cover_closed"}, None)
        await self._end(None)
        return None

    # ---- hardware helpers

    async def _lights_off(self) -> None:
        try:
            if self.hw.leds.level != 0.0:
                await self.hw.leds.set_level(0.0)
        except Exception:  # noqa: BLE001
            log.exception("could not switch the capture LEDs off")

    async def _camera_close(self) -> None:
        try:
            if self.hw.camera.is_open:
                await self.hw.camera.close()
        except Exception:  # noqa: BLE001
            log.exception("could not close the camera")
