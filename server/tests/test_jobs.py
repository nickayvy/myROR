"""Job queue: success, retry with backoff, exhaustion, crash recovery, delays,
spend deferral, no idle polling; daily scheduler DST maths."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mirror_api.jobs import (
    DailyScheduler,
    DailySpec,
    JobQueue,
    JobSpec,
    PermanentJobError,
    RetryLater,
    next_run,
)
from mirror_api.spend import SpendLedger

NY = ZoneInfo("America/New_York")


class Box:
    """Stand-in for Services in queue tests."""


@pytest.fixture
def handlers():
    return {}


@pytest.fixture
async def queue(db, handlers):
    q = JobQueue(db, lambda: Box(), concurrency=2, backoff=lambda n: 0.05, handlers=handlers)
    yield q
    await q.stop()


async def wait_status(db, job_id, status, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        row = await db.fetchrow("SELECT * FROM jobs WHERE id = %s", (job_id,))
        if row["status"] == status:
            return row
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job stuck in {row['status']}: {row['error']}")
        await asyncio.sleep(0.02)


def register(handlers, name, fn, **kw):
    handlers[name] = JobSpec(name, fn, **kw)


async def test_runs_job(queue, handlers, db):
    seen = []

    async def h(ctx, payload):
        seen.append((payload, ctx.attempt, ctx.job_type))

    register(handlers, "t.ok", h)
    await queue.start()
    jid = await queue.enqueue("t.ok", {"n": 1})
    row = await wait_status(db, jid, "done")
    assert seen == [({"n": 1}, 1, "t.ok")] and row["attempts"] == 1


async def test_unknown_type_rejected_at_enqueue(queue):
    with pytest.raises(KeyError):
        await queue.enqueue("t.nope")


async def test_retries_then_succeeds(queue, handlers, db):
    calls = []

    async def flaky(ctx, payload):
        calls.append(ctx.attempt)
        if ctx.attempt < 3:
            raise RuntimeError(f"boom {ctx.attempt}")

    register(handlers, "t.flaky", flaky)
    await queue.start()
    jid = await queue.enqueue("t.flaky", max_attempts=3)
    row = await wait_status(db, jid, "done")
    assert calls == [1, 2, 3] and row["attempts"] == 3


async def test_exhausts_attempts(queue, handlers, db):
    async def bad(ctx, payload):
        raise ValueError("always")

    register(handlers, "t.bad", bad)
    await queue.start()
    jid = await queue.enqueue("t.bad", max_attempts=2)
    row = await wait_status(db, jid, "failed")
    assert row["attempts"] == 2 and "ValueError: always" in row["error"]


async def test_permanent_error_skips_retries(queue, handlers, db):
    async def h(ctx, payload):
        raise PermanentJobError("bad input")

    register(handlers, "t.perm", h)
    await queue.start()
    row = await wait_status(db, await queue.enqueue("t.perm", max_attempts=5), "failed")
    assert row["attempts"] == 1


async def test_retry_later_does_not_use_attempt(queue, handlers, db):
    n = []

    async def h(ctx, payload):
        n.append(ctx.attempt)
        if len(n) == 1:
            raise RetryLater(0.05)

    register(handlers, "t.later", h)
    await queue.start()
    row = await wait_status(db, await queue.enqueue("t.later", max_attempts=1), "done")
    assert n == [1, 1] and row["attempts"] == 1


async def test_crash_recovery(db, handlers):
    ran = []

    async def h(ctx, payload):
        ran.append(payload["i"])

    register(handlers, "t.rec", h)
    a = await db.fetchval(
        "INSERT INTO jobs (type, payload, status, attempts, max_attempts)"
        " VALUES ('t.rec', '{\"i\": 1}', 'running', 1, 3) RETURNING id")
    b = await db.fetchval(
        "INSERT INTO jobs (type, payload, status, attempts, max_attempts)"
        " VALUES ('t.rec', '{\"i\": 2}', 'running', 3, 3) RETURNING id")
    q = JobQueue(db, lambda: Box(), handlers=handlers)
    try:
        await q.start()
        row = await wait_status(db, a, "done")
        assert row["attempts"] == 2
        row_b = await wait_status(db, b, "failed")
        assert "interrupted" in row_b["error"]
        assert ran == [1]
    finally:
        await q.stop()


async def test_delayed_job_uses_timer(queue, handlers, db):
    ran = asyncio.Event()

    async def h(ctx, payload):
        ran.set()

    register(handlers, "t.delay", h)
    await queue.start()
    await queue.enqueue("t.delay", delay_s=0.4)
    await asyncio.sleep(0.2)
    assert not ran.is_set()
    await asyncio.wait_for(ran.wait(), 3)


async def test_no_database_access_while_idle(queue, handlers, db):
    async def h(ctx, payload):
        pass

    register(handlers, "t.idle", h)
    await queue.start()
    await wait_status(db, await queue.enqueue("t.idle"), "done")
    await queue.wait_idle()
    before = db.checkouts
    await asyncio.sleep(1.0)
    assert db.checkouts == before


async def test_spend_cap_defers_job(db, handlers):
    spend = SpendLedger(db, ledger="dev", live_cap_usd=5, dev_cap_usd=1, tz=NY)
    await spend.record(provider="p", model="m", purpose="x", est_usd=Decimal("1"))

    async def h(ctx, payload):
        await spend.check("checkin")

    register(handlers, "t.spend", h)
    q = JobQueue(db, lambda: Box(), handlers=handlers)
    try:
        await q.start()
        jid = await q.enqueue("t.spend", max_attempts=1)
        await asyncio.sleep(0.3)
        row = await db.fetchrow("SELECT * FROM jobs WHERE id = %s", (jid,))
        assert row["status"] == "queued" and row["attempts"] == 0
        assert "spend cap" in row["error"]
        assert (row["run_after"] - datetime.now(UTC)).total_seconds() > 3600
    finally:
        await q.stop()


async def test_concurrency_limit(db, handlers):
    running, peak = [0], [0]

    async def h(ctx, payload):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.1)
        running[0] -= 1

    register(handlers, "t.conc", h)
    q = JobQueue(db, lambda: Box(), concurrency=2, handlers=handlers)
    try:
        await q.start()
        ids = [await q.enqueue("t.conc") for _ in range(5)]
        for jid in ids:
            await wait_status(db, jid, "done")
        assert peak[0] == 2
    finally:
        await q.stop()


async def test_enqueue_in_transaction_rolls_back(queue, handlers, db):
    async def h(ctx, payload):
        pass

    register(handlers, "t.tx", h)
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await queue.enqueue("t.tx", conn=conn)
            raise RuntimeError("abort")
    assert await db.fetchval("SELECT count(*) FROM jobs") == 0


# ---- daily scheduler ----


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_next_run_same_day_and_next_day():
    six = time(6, 0)
    assert next_run(utc(2026, 11, 25, 10, 0), six, NY) == utc(2026, 11, 25, 11, 0)  # 05:00 EST
    assert next_run(utc(2026, 11, 25, 11, 0), six, NY) == utc(2026, 11, 26, 11, 0)  # exactly 6:00


def test_next_run_across_dst():
    six = time(6, 0)
    # Spring forward 2026-03-08: 06:00 EDT is 10:00 UTC (was 11:00 the day before).
    assert next_run(utc(2026, 3, 7, 12, 0), six, NY) == utc(2026, 3, 8, 10, 0)
    # Fall back 2026-11-01: 06:00 EST is 11:00 UTC.
    assert next_run(utc(2026, 10, 31, 11, 0), six, NY) == utc(2026, 11, 1, 11, 0)


def test_next_run_in_gap_and_overlap():
    # 02:30 does not exist on 2026-03-08: runs at 03:30 EDT (07:30 UTC).
    assert next_run(utc(2026, 3, 8, 5, 0), time(2, 30), NY) == utc(2026, 3, 8, 7, 30)
    # 01:30 happens twice on 2026-11-01: run at the first (EDT, 05:30 UTC) only.
    first = next_run(utc(2026, 11, 1, 4, 0), time(1, 30), NY)
    assert first == utc(2026, 11, 1, 5, 30)
    assert next_run(first, time(1, 30), NY) == utc(2026, 11, 2, 6, 30)


async def test_daily_scheduler_fires(settings):
    fired = asyncio.Event()

    async def fn(ctx):
        assert ctx.job_type == "daily:t"
        fired.set()

    hhmmss = datetime.fromtimestamp(datetime.now(UTC).timestamp() + 2, NY).strftime("%H:%M:%S")
    sched = DailyScheduler(lambda: Box(), settings, NY, specs={"t": DailySpec("t", fn, hhmmss)})
    await sched.start()
    try:
        await asyncio.wait_for(fired.wait(), 5)
        await asyncio.sleep(0.05)
        assert sched.next_runs["t"] > datetime.now(UTC)
    finally:
        await sched.stop()


def test_daily_decorator_validates():
    from mirror_api.jobs import daily

    with pytest.raises(ValueError):
        daily("25:00")
    with pytest.raises(ValueError):
        daily()
    name = f"t{uuid.uuid4().hex}"
    from mirror_api.jobs import DAILY

    @daily(setting="precompute_time", name=name)
    async def f(ctx):
        pass

    try:
        assert DAILY[name].resolve(type("S", (), {"precompute_time": "06:00"})) == time(6)
    finally:
        DAILY.pop(name)
