"""Integration seams (items 1-4 and 7-9 of the integration brief). Each test failed on
the code before the fix (see the comment on each)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest

from db import Database
from mirror_api.app import create_app
from mirror_api.auth import Auth, AuthUnavailable
from mirror_api.jobs import HANDLERS, JobQueue, job
from tests.conftest import LAN_CLIENT, LOCAL_CLIENT, http_client

DEAD_URL = "postgresql://mirror:mirror@127.0.0.1:1/none"


def dead_db() -> Database:
    return Database(DEAD_URL, max_size=1, max_idle=1, connect_timeout_s=1,
                    acquire_timeout_s=5, reconnect_timeout_s=1)


# ---- 1. authentication with the database down ---------------------------------------------


async def test_auth_answers_fast_with_database_down(app, authed, paired):
    """Before: the token check waited ~30 s for the pool and returned 500."""
    auth = app.state.services.auth
    assert (await authed.get("/api/today")).status_code == 200  # list loaded once
    real = auth.db
    dead = dead_db()
    await dead.open()
    auth.db, auth._loaded_at = dead, -1e9  # cache expired, database gone
    try:
        t0 = time.perf_counter()
        r = await authed.get("/api/today")
        first = time.perf_counter() - t0
        assert r.status_code == 200 and first < 2.8, (r.status_code, first)
        assert auth.degraded
        t0 = time.perf_counter()
        r = await authed.get("/api/today")
        assert r.status_code == 200 and time.perf_counter() - t0 < 0.3  # no retry yet
        async with http_client(app, LAN_CLIENT, token="not-a-token") as bad:
            assert (await bad.get("/api/today")).status_code == 401
    finally:
        auth.db = real
        await dead.close()


async def test_device_snapshot_survives_restart(app, authed, paired, settings, client):
    """Before: there was no local copy, so a restart during an outage locked every phone out."""
    device_id, token = paired
    await authed.get("/api/devices")  # loads and saves the list
    path = settings.cache_dir / "auth" / "devices.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    body = path.read_text()
    assert token not in body and device_id in body
    # a second, revoked phone must not come back from the snapshot
    code = (await client.post("/api/pair/code")).json()["code"]
    async with http_client(app, LAN_CLIENT) as anon:
        other = (await anon.post("/api/pair", json={"code": code})).json()
    assert (await authed.delete(f"/api/devices/{other['device_id']}")).status_code == 204

    dead = dead_db()
    await dead.open()
    try:
        fresh = Auth(dead, snapshot_path=path, db_timeout_s=1.0)
        t0 = time.perf_counter()
        dev = await fresh.authenticate(token)
        assert dev is not None and str(dev.id) == device_id
        assert time.perf_counter() - t0 < 2.0
        assert await fresh.authenticate(other["token"]) is None
        nothing = Auth(dead, snapshot_path=path.with_name("missing.json"), db_timeout_s=0.5)
        with pytest.raises(AuthUnavailable):
            await nothing.authenticate(token)
    finally:
        await dead.close()


async def test_no_device_list_at_all_is_503_not_401(make_app, settings):
    dead = dead_db()
    app = await make_app(start_workers=False, db=dead)
    app.state.services.auth.snapshot_path = settings.cache_dir / "none.json"
    app.state.services.auth.db_timeout_s = 0.5
    async with http_client(app, LAN_CLIENT, token="x") as c:
        r = await c.get("/api/today")
    assert r.status_code == 503
    await dead.close()


async def test_revocation_in_database_takes_effect_after_ttl(app, authed, paired, settings):
    device_id, _ = paired
    auth = app.state.services.auth
    assert (await authed.get("/api/devices")).status_code == 200
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (device_id,))
    auth._loaded_at -= auth.cache_ttl_s  # the minute passes
    assert (await authed.get("/api/devices")).status_code == 401


class Proxy:
    """TCP forwarder in front of Postgres that can be switched off, like Neon going away."""

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.server: asyncio.Server | None = None
        self.port = 0
        self.writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        for w in list(self.writers):
            w.close()
        self.writers.clear()

    async def _handle(self, reader, writer) -> None:
        try:
            r2, w2 = await asyncio.open_connection(*self.target)
        except OSError:
            writer.close()
            return
        self.writers.update({writer, w2})

        async def pipe(a, b):
            try:
                while data := await a.read(65536):
                    b.write(data)
                    await b.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                b.close()

        await asyncio.gather(pipe(reader, w2), pipe(r2, writer))


async def test_pool_connects_at_once_when_database_returns(settings, clean_db):
    """Before: psycopg_pool kept backing off (up to 5 min of retries), so the first query
    after an outage waited ~20 s; and a query during the outage waited 30 s."""
    u = urlsplit(settings.database_url)
    proxy = Proxy(u.hostname, u.port or 5432)
    await proxy.start()
    url = urlunsplit(u._replace(netloc=f"{u.username}:{u.password}@127.0.0.1:{proxy.port}"))
    db = Database(url, max_size=2, max_idle=1, connect_timeout_s=2, acquire_timeout_s=3,
                  reconnect_timeout_s=1)
    await db.open()
    try:
        assert await db.fetchval("SELECT 1") == 1
        await proxy.stop()
        await asyncio.sleep(1.2)  # idle connections expire; the next query must connect
        for _ in range(2):
            t0 = time.perf_counter()
            with pytest.raises(Exception):  # noqa: B017 - PoolTimeout / OperationalError
                await db.fetchval("SELECT 1")
            assert time.perf_counter() - t0 < 4.0
        await asyncio.sleep(2.5)  # let any retry backoff grow
        await proxy.start()
        t0 = time.perf_counter()
        assert await db.fetchval("SELECT 1") == 1
        assert time.perf_counter() - t0 < 2.0
    finally:
        await db.close()
        await proxy.stop()


async def test_connection_killed_while_idle_is_replaced(settings, clean_db):
    """Before: after a database restart the pool handed out a dead connection and the
    request failed with AdminShutdown (500)."""
    db = Database(settings.database_url, max_size=1, max_idle=60)
    await db.open()
    try:
        pid = await db.fetchval("SELECT pg_backend_pid()")
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        await asyncio.sleep(0.2)  # within CHECK_AFTER_IDLE_S: the socket check catches it
        assert await db.fetchval("SELECT pg_backend_pid()") != pid
    finally:
        await db.close()


async def test_database_errors_answer_503(make_app):
    dead = dead_db()
    app = await make_app(start_workers=False, db=dead)
    async with http_client(app, LOCAL_CLIENT) as c:
        r = await c.get("/api/items")
    assert r.status_code == 503 and r.headers["retry-after"]
    await dead.close()


# ---- 2. shortcut tokens ---------------------------------------------------------------


async def test_shortcut_token_only_pushes_reminders(app, authed):
    """Before: a shortcut token could call every route except token management."""
    tok = (await authed.post("/api/shortcut/tokens", json={})).json()["token"]
    async with http_client(app, LAN_CLIENT, token=tok) as sc:
        r = await sc.post("/api/reminders", json={"list": "Errands", "reminders": [
            {"title": "Buy milk"}]})
        assert r.status_code == 200, r.text
        for method, path in [("GET", "/api/today"), ("GET", "/api/reminders"),
                             ("GET", "/api/items"), ("GET", "/api/devices"),
                             ("POST", "/api/capture"), ("GET", "/api/health"),
                             ("POST", "/api/shortcut/tokens")]:
            r = await sc.request(method, path)
            assert r.status_code == 403, (method, path, r.status_code)


def test_shortcut_token_refused_on_websocket(settings, clean_db, storage):
    from fastapi.testclient import TestClient

    from mirror_api.auth import hash_token

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO devices (name, kind, token_hash) VALUES ('s', 'shortcut', %s)",
                     (hash_token("shortcut-tok"),))
    app = create_app(settings, start_workers=False)
    with TestClient(app, client=("192.168.1.9", 40001)) as tc, \
            tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "shortcut-tok"})
        with pytest.raises(Exception):  # noqa: B017 - closed with 4401
            ws.receive_json()


# ---- 3. job queue ---------------------------------------------------------------------


async def test_enqueue_in_transaction_wakes_after_commit(db):
    """Before: the wake fired before COMMIT; the worker found nothing and the job sat
    queued until some unrelated wake."""
    done = []
    name = f"t.seam.{uuid4().hex[:6]}"

    @job(name)
    async def h(ctx, payload):
        done.append(payload)

    q = JobQueue(db, lambda: None)
    await q.start()
    try:
        await q.wait_idle()
        async with db.transaction() as conn:
            await q.enqueue(name, {"n": 1}, conn=conn)
            await asyncio.sleep(0.3)  # the worker would have looked by now
        for _ in range(100):
            if done:
                break
            await asyncio.sleep(0.02)
        assert done == [{"n": 1}]
    finally:
        await q.stop()
        HANDLERS.pop(name)


class SlowMinDb:
    """Holds the queue's "next delayed job" query until released, so an enqueue lands
    while a dispatch pass is finishing."""

    def __init__(self, inner: Database) -> None:
        self.inner = inner
        self.hold = asyncio.Event()
        self.entered = asyncio.Event()
        self.armed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def fetchval(self, sql, params=None):
        if self.armed and "min(run_after)" in sql:
            self.armed = False
            self.entered.set()
            await self.hold.wait()
        return await self.inner.fetchval(sql, params)


async def test_wait_idle_does_not_return_early(db):
    """Before: a wake during the end of a pass was followed by _idle.set(), so wait_idle
    returned while the new job was still queued."""
    done = []
    name = f"t.seam.{uuid4().hex[:6]}"

    @job(name)
    async def h(ctx, payload):
        done.append(1)

    slow = SlowMinDb(db)
    q = JobQueue(slow, lambda: None)
    await q.start()
    try:
        await q.wait_idle()
        slow.armed = True
        q.wake()
        await slow.entered.wait()
        await q.enqueue(name, {})  # arrives mid-pass
        slow.hold.set()
        await q.wait_idle(5)
        assert done == [1]
    finally:
        await q.stop()
        HANDLERS.pop(name)


async def test_worker_before_migrations_is_quiet(admin_url, caplog, settings):
    """Before: a fresh stack logged 'job dispatch failed' with a traceback and retried
    every minute until migrated."""
    from db.migrate import apply_migrations

    name = f"nomig_{uuid4().hex[:8]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    url = urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    db = Database(url, max_size=2, max_idle=5)
    await db.open()
    done = []
    jname = f"t.seam.{uuid4().hex[:6]}"

    @job(jname)
    async def h(ctx, payload):
        done.append(1)

    q = JobQueue(db, lambda: None)
    try:
        with caplog.at_level(logging.INFO, logger="mirror_api.jobs"):
            await q.start()
            await q.wait_idle(5)
        assert not [r for r in caplog.records
                    if r.name == "mirror_api.jobs" and r.levelno >= logging.WARNING]
        assert any("migrations not applied" in r.getMessage() for r in caplog.records)
        assert q._timer is None  # no retry timer: nothing polls
        await asyncio.to_thread(apply_migrations, url)
        await q.enqueue(jname, {})
        await q.wait_idle(5)
        assert done == [1]
        # the connection opened before migrations now adapts vectors too
        vec = await db.fetchval("SELECT '[1,2,3]'::vector")
        assert not isinstance(vec, str) and vec.to_list() == [1, 2, 3]
    finally:
        await q.stop()
        await db.close()
        HANDLERS.pop(jname)
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE {name} WITH (FORCE)")


# ---- 4. app wiring --------------------------------------------------------------------


async def test_provider_overrides_and_fields(make_app, settings):
    """Before: bg_remover/segmenter/embedder/face_blurrer/fal were not accepted by
    create_app, and feature state hung off ad-hoc attributes."""
    sentinel = object()
    app = await make_app(start_workers=False, bg_remover=sentinel, embedder=sentinel)
    s = app.state.services
    assert s.bg_remover is sentinel and s.embedder is sentinel
    from integrations import today
    from recommender import service

    assert today.get_state(s) is s.integrations
    assert service._state(s) is s.recommender
    from mirror_api.app import build_services

    with pytest.raises(ValueError):
        build_services(settings, {"nope": 1})


async def test_feature_startup_respects_start_workers(make_app):
    """Before: the integrations timers started from a router lifespan even with
    start_workers=False."""
    off = await make_app(start_workers=False)
    s = off.state.services
    assert s.integrations is None or s.integrations._task is None
    assert s.presence is None
    on = await make_app(start_workers=True)
    s = on.state.services
    assert s.integrations._task is not None and s.presence is not None
    assert s.capture is not None


# ---- 7. one wear-log writer -----------------------------------------------------------


def test_only_one_module_writes_wear_log():
    root = Path(__file__).resolve().parent.parent
    writers = []
    for p in root.rglob("*.py"):
        if ".venv" in p.parts or "tests" in p.parts:
            continue
        text = p.read_text()
        if "INSERT INTO wear_log" in text or "UPDATE wear_log" in text:
            writers.append(p.relative_to(root).as_posix())
    assert writers == ["recommender/store.py"]


async def test_wear_log_records_outfit_and_photo(db):
    from recommender import store

    item = await db.fetchval("INSERT INTO items (name, category) VALUES ('t', 'top') RETURNING id")
    outfit = await db.fetchval("INSERT INTO outfits (for_date, source)"
                               " VALUES ('2026-11-25', 'rules') RETURNING id")
    photo = await db.fetchval(
        "INSERT INTO photos (source, storage_key, sha256) VALUES ('mirror_checkin', 'k', 's')"
        " RETURNING id")
    day = date(2026, 11, 25)
    assert await store.log_wear(db, [item], day, source="outfit", outfit_id=outfit) == [item]
    # the check-in confirmation of the same wear adds its photo, logs nothing new
    assert await store.log_wear(db, [item, item], day, source="checkin", photo_id=photo) == []
    rows = await db.fetch("SELECT source, outfit_id, photo_id FROM wear_log")
    assert rows == [{"source": "outfit", "outfit_id": outfit, "photo_id": photo}]
    assert await db.fetchval("SELECT status FROM outfits WHERE id = %s", (outfit,)) == "worn"
    assert await db.fetchval("SELECT last_worn_on FROM items") == day


# ---- 9. phone app -------------------------------------------------------------------


@pytest.fixture
def dist(tmp_path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>phone</title>")
    (d / "assets" / "app-123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside")
    return d


async def test_phone_app_served_without_shadowing_api(make_app, settings, dist):
    """Before: nothing served phone/dist."""
    app = await make_app(start_workers=False,
                         settings_=settings.model_copy(update={"phone_dist_dir": dist}))
    async with http_client(app, LAN_CLIENT) as c:
        r = await c.get("/")
        assert r.status_code == 200 and "<title>phone" in r.text
        r = await c.get("/pair?code=ABCD2345")
        assert r.status_code == 200 and "<title>phone" in r.text
        r = await c.get("/closet/items/1")
        assert "<title>phone" in r.text and r.headers["cache-control"] == "no-cache"
        r = await c.get("/assets/app-123.js")
        assert r.text == "console.log(1)" and "immutable" in r.headers["cache-control"]
        assert (await c.get("/../secret.txt")).text != "outside"
        assert (await c.get("/assets/../../secret.txt")).text != "outside"
        r = await c.get("/api/nope")
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert (await c.get("/api/today")).status_code == 401  # API still guarded
        assert (await c.get("/openapi.json")).json()["info"]["title"]
        assert "swagger" in (await c.get("/docs")).text.lower()
        assert (await c.get("/ws")).status_code == 404


async def test_no_dist_no_catch_all(make_app, settings, tmp_path):
    app = await make_app(start_workers=False, settings_=settings.model_copy(
        update={"phone_dist_dir": tmp_path / "nothing"}))
    async with http_client(app, LOCAL_CLIENT) as c:
        assert (await c.get("/")).status_code == 404


async def test_pair_qr_points_at_phone_app(client):
    body = (await client.post("/api/pair/code", json={"base_url": "http://10.0.0.5:8000"})).json()
    assert body["pair_url"] == f"http://10.0.0.5:8000/pair?code={body['code']}"


def test_access_log_redacts_codes():
    from mirror_api.app import _RedactQuery

    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "GET", "/pair?code=ABCD2345", "1.1", 200), None)
    _RedactQuery().filter(rec)
    assert "ABCD2345" not in rec.getMessage()


# ---- dev controls and scripted LLM ------------------------------------------------------


async def test_dev_controls_only_in_dev_with_mock(make_app, settings):
    dev = await make_app(start_workers=False)
    prod = await make_app(start_workers=False,
                          settings_=settings.model_copy(update={"app_env": "prod"}))
    async with http_client(dev, LAN_CLIENT) as c:
        assert (await c.post("/api/dev/pair-code")).status_code == 200
        assert (await c.post("/api/dev/hardware/cover", json={"open": True})).status_code == 401
    async with http_client(prod, LAN_CLIENT) as c:
        assert (await c.post("/api/dev/pair-code")).status_code == 404
    async with http_client(dev, LOCAL_CLIENT) as c:
        r = await c.post("/api/dev/hardware/cover", json={"open": True})
        assert r.status_code == 200
        assert not dev.state.services.hardware.cover.is_closed()
        assert (await c.post("/api/dev/hardware/camera/frame",
                             json={"kind": "half_covered"})).status_code == 200
        st = (await c.get("/api/dev/hardware")).json()
        assert st["cover_open"] and st["camera"]["frame_set"]


def test_scripted_llm_is_selectable(settings):
    from providers.llm import build_llm

    llm = build_llm(settings.model_copy(update={"llm": "scripted"}), None)
    assert llm.name == "scripted" and llm.configured


async def test_scripted_llm_answers_each_purpose():
    from integrations.formality import SCHEMA
    from providers.llm import text_block
    from providers.llm.scripted import build_scripted_llm
    from recommender.composer import response_schema

    llm = build_scripted_llm()
    out = await llm.complete_structured(
        model="m", system="s", content=[text_block("an outer layer is required (cold)")],
        json_schema=response_schema(["T1", "T2", "B1", "B2", "S1", "O1"]),
        purpose="compose_outfits", effort="low")
    assert len(out["outfits"]) == 3
    assert all("O1" in o["items"] and "S1" in o["items"] for o in out["outfits"])
    assert len({tuple(o["items"]) for o in out["outfits"]}) == 3
    out = await llm.complete_structured(
        model="m", system="s", content=[text_block(json.dumps(
            [{"i": 0, "title": "Client presentation"}, {"i": 1, "title": "Gym"}]))],
        json_schema=SCHEMA, purpose="event_formality", effort="low")
    assert out == {"ratings": [{"i": 0, "formality": 4}, {"i": 1, "formality": 2}]}
