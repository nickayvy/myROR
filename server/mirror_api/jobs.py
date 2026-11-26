"""Durable in-process job queue on the `jobs` table, plus a daily wall-clock scheduler.

Register handlers anywhere that is imported at startup (by convention
`<package>/jobs.py` in pipeline, recommender, integrations or capture):

    from mirror_api.jobs import JobContext, daily, job

    @job("pipeline.tag_item", max_attempts=3)
    async def tag_item(ctx: JobContext, payload: dict) -> None:
        row = await ctx.db.fetchrow("SELECT ... WHERE id = %s", (payload["item_id"],))
        ...

    @daily(setting="precompute_time")          # or @daily("06:00")
    async def morning(ctx: JobContext) -> None:
        await ctx.jobs.enqueue("recommender.precompute", {})

    # from a route or another job:
    await jobs.enqueue("pipeline.tag_item", {"item_id": str(item_id)})

Rules:
- Nothing polls the database while idle (Neon must scale to zero, HANDOFF A3).
  enqueue() wakes the worker through an asyncio.Event; delayed retries use one
  in-process timer set to the earliest queued run_after.
- Retries: on an exception the job is re-queued with backoff until max_attempts,
  then marked failed. Raise RetryLater(seconds) to defer without using an attempt,
  PermanentJobError to fail immediately. SpendCapExceeded defers the job to the end
  of the spend period (check-ins queue at 100%, HANDOFF section 15).
- On startup, rows left in `running` by a crash go back to `queued` (or `failed` if
  they had used all attempts) and the queue drains once.
- Handlers must be idempotent: a crash after the work but before the status update
  runs the job again.
- Daily jobs run in-process at a local wall-clock time in TIMEZONE, DST-safe: a
  time inside the spring-forward gap runs when the gap ends, and a time repeated at
  fall-back runs once (first occurrence). A run missed while the server was down is
  not caught up; have the daily job enqueue durable work if that matters.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.errors import UndefinedTable
from psycopg.types.json import Jsonb

from mirror_api.config import parse_hhmm
from mirror_api.spend import SpendCapExceeded

if TYPE_CHECKING:
    from mirror_api.services import Services

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Handler = Callable[["JobContext", dict[str, Any]], Awaitable[None]]
DailyHandler = Callable[["JobContext"], Awaitable[None]]


def utcnow() -> datetime:
    return datetime.now(UTC)


def default_backoff(attempt: int) -> float:
    """Seconds before retry number `attempt` (1-based): 30 s, 60 s, 120 s ... max 1 h."""
    return float(min(30 * 2 ** (attempt - 1), 3600))


class RetryLater(Exception):  # noqa: N818 - control flow, not an error
    """Raise from a handler to re-run later without consuming an attempt."""

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"retry in {seconds}s")
        self.seconds = seconds


class PermanentJobError(Exception):
    """Raise from a handler to fail the job now, with no retries."""


# ---- registry -----------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    type: str
    fn: Handler
    max_attempts: int | None = None
    backoff: Callable[[int], float] | None = None


@dataclass(frozen=True)
class DailySpec:
    name: str
    fn: DailyHandler
    at: str | None = None
    setting: str | None = None

    def resolve(self, settings: Any) -> time:
        value = self.at if self.at is not None else getattr(settings, self.setting or "")
        return parse_hhmm(value)


HANDLERS: dict[str, JobSpec] = {}
DAILY: dict[str, DailySpec] = {}


def job(
    type_: str, *, max_attempts: int | None = None, backoff: Callable[[int], float] | None = None
) -> Callable[[Handler], Handler]:
    """Register `async def handler(ctx, payload)` for job type `type_`."""

    def deco(fn: Handler) -> Handler:
        existing = HANDLERS.get(type_)
        if existing is not None and existing.fn is not fn:
            raise ValueError(f"job type {type_!r} already registered by {existing.fn.__module__}")
        HANDLERS[type_] = JobSpec(type_, fn, max_attempts, backoff)
        return fn

    return deco


def daily(
    at: str | None = None, *, setting: str | None = None, name: str | None = None
) -> Callable[[DailyHandler], DailyHandler]:
    """Run `async def fn(ctx)` every day at local time `at` ("HH:MM"), or at the time in
    the Settings attribute `setting` (e.g. "precompute_time")."""
    if (at is None) == (setting is None):
        raise ValueError("give exactly one of `at` or `setting`")
    if at is not None:
        parse_hhmm(at)

    def deco(fn: DailyHandler) -> DailyHandler:
        key = name or f"{fn.__module__}.{fn.__qualname__}"
        DAILY[key] = DailySpec(key, fn, at, setting)
        return fn

    return deco


# ---- context --------------------------------------------------------------------


@dataclass
class JobContext:
    services: Services
    job_type: str
    job_id: UUID | None = None
    attempt: int = 1
    max_attempts: int = 1

    @property
    def db(self):
        return self.services.db

    @property
    def storage(self):
        return self.services.storage

    @property
    def llm(self):
        return self.services.llm

    @property
    def spend(self):
        return self.services.spend

    @property
    def hub(self):
        return self.services.hub

    @property
    def hardware(self):
        return self.services.hardware

    @property
    def settings(self):
        return self.services.settings

    @property
    def jobs(self) -> JobQueue:
        return self.services.jobs

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


# ---- queue ----------------------------------------------------------------------


@dataclass
class _Timer:
    at: datetime
    handle: asyncio.TimerHandle


@dataclass
class JobQueue:
    db: Any
    services: Callable[[], Services]
    concurrency: int = 2
    backoff: Callable[[int], float] = default_backoff
    clock: Clock = utcnow
    handlers: dict[str, JobSpec] = field(default_factory=lambda: HANDLERS)
    shutdown_grace_s: float = 5.0

    def __post_init__(self) -> None:
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()
        self._task: asyncio.Task | None = None
        self._timer: _Timer | None = None
        self._recovered = False
        self._stopping = False

    # -- lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="job-queue")
            self._wake.set()  # recovery + one drain

    async def stop(self) -> None:
        self._stopping = True
        if self._timer:
            self._timer.handle.cancel()
            self._timer = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._inflight:
            _, pending = await asyncio.wait(self._inflight, timeout=self.shutdown_grace_s)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Test helper: wait until nothing is ready to run and nothing is running.

        `_idle` is only set by a dispatch pass that found nothing to claim, nothing in
        flight and no wake pending, and every wake() clears it, so this cannot return
        while a job enqueued before the call is still waiting."""
        await asyncio.wait_for(self._idle.wait(), timeout)

    @property
    def idle(self) -> bool:
        return self._idle.is_set()

    # -- producer

    async def enqueue(
        self,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        max_attempts: int | None = None,
        delay_s: float = 0.0,
        conn: Any = None,
    ) -> UUID:
        """Insert a job and wake the worker. Pass `conn` to enqueue inside your own
        transaction (the job then only exists if that transaction commits); the worker is
        then woken when that connection is released, after the commit, so it cannot look
        before the row is visible."""
        spec = self.handlers.get(type_)
        if spec is None:
            raise KeyError(f"no handler registered for job type {type_!r}")
        attempts = max_attempts or spec.max_attempts or 3
        run_after = self.clock() + timedelta(seconds=delay_s)
        sql = (
            "INSERT INTO jobs (type, payload, max_attempts, run_after)"
            " VALUES (%s, %s, %s, %s) RETURNING id"
        )
        params = (type_, Jsonb(payload or {}), attempts, run_after)
        if delay_s > 0:
            def notify() -> None:
                self._schedule_wake(run_after)
        else:
            notify = self.wake
        if conn is not None:
            cur = await conn.execute(sql, params)
            job_id = (await cur.fetchone())["id"]
            after_release = getattr(self.db, "after_release", None)
            if after_release is not None:
                after_release(conn, notify)
            else:
                notify()
        else:
            job_id = await self.db.fetchval(sql, params)
            notify()
        return job_id

    def wake(self) -> None:
        self._idle.clear()
        self._wake.set()

    # -- worker

    async def _run(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._dispatch()
            except asyncio.CancelledError:
                raise
            except UndefinedTable:
                # A fresh stack before `python -m db.migrate`: there is no jobs table, so
                # there is nothing to run. No retry timer: the first enqueue() (which
                # needs the table too) wakes the worker, and recovery runs then.
                log.info("jobs table not there yet (migrations not applied); worker idle")
                self._idle.set()
            except Exception:
                # Typically the database is unreachable (internet down). Try again in a
                # minute; this only happens while there is work, never while idle.
                log.exception("job dispatch failed; retrying in 60 s")
                self._schedule_wake(self.clock() + timedelta(seconds=60))

    async def _dispatch(self) -> None:
        if not self._recovered:
            await self._recover()
            self._recovered = True
        while len(self._inflight) < self.concurrency:
            row = await self._claim()
            if row is None:
                break
            task = asyncio.create_task(self._execute(row), name=f"job:{row['type']}")
            self._inflight.add(task)
            task.add_done_callback(self._on_done)
        if len(self._inflight) < self.concurrency:
            # Nothing ready. Arm one timer for the earliest delayed job, if any.
            nxt = await self.db.fetchval(
                "SELECT min(run_after) FROM jobs WHERE status = 'queued'"
            )
            if nxt is not None:
                self._schedule_wake(nxt)
            # A wake() that arrived during this pass (an enqueue while we were awaiting
            # the queries above) means there may be new work: let the next pass decide.
            if not self._inflight and not self._wake.is_set():
                self._idle.set()

    def _on_done(self, task: asyncio.Task) -> None:
        self._inflight.discard(task)
        if not self._stopping:
            self._wake.set()

    async def _recover(self) -> None:
        rows = await self.db.fetch(
            "UPDATE jobs SET"
            " status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,"
            " error = coalesce(error, '') || CASE WHEN attempts >= max_attempts"
            "   THEN 'interrupted by restart on final attempt' ELSE '' END,"
            " finished_at = CASE WHEN attempts >= max_attempts THEN %s ELSE NULL END,"
            " run_after = %s"
            " WHERE status = 'running' RETURNING id, status",
            (self.clock(), self.clock()),
        )
        if rows:
            log.warning("recovered %d interrupted job(s)", len(rows))

    async def _claim(self) -> dict[str, Any] | None:
        now = self.clock()
        return await self.db.fetchrow(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = %s,"
            " finished_at = NULL"
            " WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= %s"
            "             ORDER BY run_after, created_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
            " RETURNING id, type, payload, attempts, max_attempts",
            (now, now),
        )

    async def _execute(self, row: dict[str, Any]) -> None:
        job_id, type_ = row["id"], row["type"]
        attempt, max_attempts = row["attempts"], row["max_attempts"]
        spec = self.handlers.get(type_)
        if spec is None:
            await self._finish(job_id, "failed", f"no handler registered for {type_!r}")
            return
        ctx = JobContext(self.services(), type_, job_id, attempt, max_attempts)
        try:
            await spec.fn(ctx, row["payload"])
        except asyncio.CancelledError:
            # Shutdown mid-job: give the attempt back and leave it queued.
            await asyncio.shield(self._requeue(job_id, 0, "interrupted by shutdown", refund=True))
            raise
        except RetryLater as e:
            await self._requeue(job_id, e.seconds, str(e), refund=True)
        except SpendCapExceeded as e:
            end = e.status.period_end
            delay = (end - self.clock()).total_seconds() if end else 24 * 3600
            await self._requeue(job_id, max(delay, 60), str(e), refund=True)
        except PermanentJobError as e:
            await self._finish(job_id, "failed", _err(e))
        except Exception as e:
            if attempt >= max_attempts:
                log.error("job %s %s failed permanently: %s", type_, job_id, e)
                await self._finish(job_id, "failed", _err(e))
            else:
                backoff = (spec.backoff or self.backoff)(attempt)
                log.warning("job %s %s attempt %d failed; retry in %.0fs", type_, job_id,
                            attempt, backoff)
                await self._requeue(job_id, backoff, _err(e))
        else:
            await self._finish(job_id, "done", None)

    async def _finish(self, job_id: UUID, status: str, error: str | None) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = %s, error = %s, finished_at = %s WHERE id = %s",
            (status, error, self.clock(), job_id),
        )

    async def _requeue(
        self, job_id: UUID, delay_s: float, error: str, refund: bool = False
    ) -> None:
        run_after = self.clock() + timedelta(seconds=delay_s)
        await self.db.execute(
            "UPDATE jobs SET status = 'queued', run_after = %s, error = %s,"
            " attempts = attempts - %s WHERE id = %s",
            (run_after, error, 1 if refund else 0, job_id),
        )
        if not self._stopping:
            self._schedule_wake(run_after)

    def _schedule_wake(self, at: datetime) -> None:
        if self._stopping:
            return
        if self._timer is not None:
            if self._timer.at <= at and not self._timer.handle.cancelled():
                return  # an earlier (or equal) wake is already armed
            self._timer.handle.cancel()
        delay = max((at - self.clock()).total_seconds(), 0.0)
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._timer = None
            self.wake()

        self._timer = _Timer(at, loop.call_later(delay, fire))


def _err(e: BaseException) -> str:
    tb = "".join(traceback.format_exception_only(type(e), e)).strip()
    return tb[:2000]


# ---- daily scheduler ------------------------------------------------------------


def resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of wall-clock `at` on `day` in `tz`.

    fold=0 picks the first occurrence of an ambiguous (fall-back) time. A time in the
    spring-forward gap is interpreted with the pre-transition offset, which lands at
    the same distance past the end of the gap (02:30 -> 03:30).
    """
    local = datetime.combine(day, at).replace(tzinfo=tz, fold=0)
    return local.astimezone(UTC)


def next_run(now: datetime, at: time, tz: ZoneInfo) -> datetime:
    """First instant strictly after `now` whose local wall-clock time is `at`."""
    day = now.astimezone(tz).date() - timedelta(days=1)
    for _ in range(4):
        cand = resolve_local(day, at, tz)
        if cand > now:
            return cand
        day += timedelta(days=1)
    raise AssertionError("unreachable")


@dataclass
class DailyScheduler:
    services: Callable[[], Services]
    settings: Any
    tz: ZoneInfo
    specs: dict[str, DailySpec] = field(default_factory=lambda: DAILY)
    clock: Clock = utcnow
    # Re-check the wall clock at least this often so clock jumps (NTP at boot) and
    # long sleeps stay accurate. Local timers only; no database access.
    max_sleep_s: float = 3600.0

    def __post_init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self.next_runs: dict[str, datetime] = {}

    async def start(self) -> None:
        for spec in self.specs.values():
            at = spec.resolve(self.settings)
            self._tasks.append(asyncio.create_task(self._loop(spec, at), name=f"daily:{spec.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, spec: DailySpec, at: time) -> None:
        while True:
            target = next_run(self.clock(), at, self.tz)
            self.next_runs[spec.name] = target
            while (remaining := (target - self.clock()).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, self.max_sleep_s))
            ctx = JobContext(self.services(), f"daily:{spec.name}")
            try:
                log.info("running daily job %s", spec.name)
                await spec.fn(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily job %s failed", spec.name)
