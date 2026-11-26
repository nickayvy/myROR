"""Health, settings, extension points, WebSocket hub, hardware mocks, contracts."""

from __future__ import annotations

import sys
import textwrap
from datetime import UTC, date, datetime
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hardware import fixture_frame
from mirror_api import app as app_module
from mirror_api.auth import hash_token
from mirror_api.contracts import (
    OutfitItem,
    OutfitSuggestion,
    TodayPayload,
    WeatherSnapshot,
)


async def test_health(client, settings):
    settings_with_key = settings.model_copy(update={"anthropic_api_key": "sk-secret-value"})
    r = await client.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert h["status"] == "ok"
    assert h["db"]["ok"] and h["storage"]["ok"]
    assert h["spend"]["mode"] == "normal" and h["spend"]["ledger"] == "dev"
    assert h["providers"]["llm"] is True and h["providers"]["fal"] is False
    assert h["providers"]["calendars"] == {"google": False, "outlook": False, "icloud": False}
    assert settings_with_key.providers_configured()["llm"] is True
    assert "sk-secret" not in r.text and "minioadmin" not in r.text
    assert settings.database_url not in r.text


async def test_health_requires_device_from_lan(remote):
    assert (await remote.get("/api/health")).status_code == 401


async def test_settings_roundtrip(authed):
    assert (await authed.get("/api/settings")).json() == {}
    r = await authed.put("/api/settings", json={"temp_unit": "C", "display.sleep": {"after": 60}})
    assert r.status_code == 200
    assert r.json() == {"display.sleep": {"after": 60}, "temp_unit": "C"}
    r = await authed.put("/api/settings", json={"temp_unit": None})
    assert r.json() == {"display.sleep": {"after": 60}}
    assert (await authed.put("/api/settings", json={"Bad Key": 1})).status_code == 422


def test_settings_unconfigured_never_crash():
    from mirror_api.config import Settings

    s = Settings(_env_file=None, database_url="", storage_endpoint="", anthropic_api_key="",
                 timezone="", weather_lat="", weather_lon="")
    assert not s.db_configured and not s.storage_configured and not s.timezone_configured
    assert str(s.tz) == "UTC" and s.weather_lat is None and s.face_blur is False


def test_settings_parse_env_example_style(monkeypatch):
    from mirror_api.config import Settings

    monkeypatch.setenv("FACE_BLUR", "on")
    monkeypatch.setenv("TEMP_UNIT", "C")
    s = Settings(_env_file=None)
    assert s.face_blur is True and s.temp_unit == "C"


# ---- extension points ----


@pytest.fixture
def plugin_pkg(tmp_path, monkeypatch):
    """A throwaway package with routers/ and jobs.py, as another engineer would add."""
    name = f"plug_{uuid4().hex[:8]}"
    root = tmp_path / name
    (root / "routers").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "routers" / "__init__.py").write_text("")
    (root / "routers" / "hello.py").write_text(textwrap.dedent("""
        from fastapi import APIRouter
        router = APIRouter(prefix="/api/hello")
        @router.get("")
        async def hello():
            return {"hi": True}
    """))
    (root / "routers" / "open.py").write_text(textwrap.dedent("""
        from fastapi import APIRouter
        AUTH = "none"
        router = APIRouter(prefix="/api/open")
        @router.get("")
        async def open_():
            return {"open": True}
    """))
    (root / "routers" / "_private.py").write_text("router = object()")
    (root / "routers" / "helpers.py").write_text("X = 1")
    (root / "jobs.py").write_text(textwrap.dedent(f"""
        from mirror_api.jobs import job
        @job("{name}.ping")
        async def ping(ctx, payload):
            pass
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    for mod in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[mod]
    from mirror_api.jobs import HANDLERS
    HANDLERS.pop(f"{name}.ping", None)


async def test_router_discovery_and_default_guard(plugin_pkg):
    import httpx

    from tests.conftest import LAN_CLIENT

    app = FastAPI()
    names = app_module.include_routers(app, f"{plugin_pkg}.routers")
    assert names == [f"{plugin_pkg}.routers.hello", f"{plugin_pkg}.routers.open"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=LAN_CLIENT),
                                 base_url="http://t") as c:
        assert (await c.get("/api/open")).json() == {"open": True}
        # Default AUTH="device": guarded even though the module never mentioned auth.
        assert (await c.get("/api/hello")).status_code == 401


def test_job_module_autoimport(plugin_pkg):
    from mirror_api.jobs import HANDLERS

    loaded = app_module.import_job_modules((plugin_pkg, "no_such_pkg_xyz"))
    assert loaded == [f"{plugin_pkg}.jobs"]
    assert f"{plugin_pkg}.ping" in HANDLERS


def test_job_module_import_errors_are_raised(tmp_path, monkeypatch):
    name = f"broken_{uuid4().hex[:8]}"
    (tmp_path / name).mkdir()
    (tmp_path / name / "__init__.py").write_text("")
    (tmp_path / name / "jobs.py").write_text("import does_not_exist_abc\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):
        app_module.import_job_modules((name,))


async def test_core_routers_included(app):
    assert "mirror_api.routers.health" in app.state.routers
    assert "mirror_api.routers.pairing" in app.state.routers


async def test_jobs_reachable_from_app(app, db):
    from mirror_api.jobs import HANDLERS, job

    done = []
    name = f"t.app.{uuid4().hex[:6]}"

    @job(name)
    async def h(ctx, payload):
        assert ctx.db is db and ctx.settings.llm == "fake"
        done.append(payload)

    try:
        services = app.state.services
        await services.jobs.enqueue(name, {"x": 1})
        await services.jobs.wait_idle()
        assert done == [{"x": 1}]
    finally:
        HANDLERS.pop(name)


# ---- WebSocket ----


def _sync_app(settings):
    return app_module.create_app(settings, start_workers=False)


def test_ws_mirror_and_phone(settings, clean_db, storage):
    token = "phone-token-for-ws-test"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO devices (name, token_hash) VALUES ('p', %s)",
                     (hash_token(token),))
    app = _sync_app(settings)
    with TestClient(app, client=("127.0.0.1", 40000)) as mirror_tc:
        with mirror_tc.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"
            mirror_tc.portal.call(app.state.services.hub.broadcast, "test.event", {"n": 1})
            msg = ws.receive_json()
            assert msg["type"] == "test.event" and msg["data"] == {"n": 1} and msg["at"]
            # hardware events reach the mirror
            mirror_tc.portal.call(_set_present, app)
            msg = ws.receive_json()
            assert msg == {**msg, "type": "presence.changed", "data": {"present": True}}


async def _set_present(app):
    app.state.services.hardware.presence.set_present(True)
    import asyncio
    await asyncio.sleep(0.05)


def test_ws_phone_auth(settings, clean_db, storage):
    token = "phone-token-for-ws-test-2"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO devices (name, token_hash) VALUES ('p', %s)",
                     (hash_token(token),))
    app = _sync_app(settings)
    with TestClient(app, client=("192.168.1.9", 40001)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "wrong"})
            with pytest.raises(Exception):  # noqa: B017 - closed with 4401
                ws.receive_json()
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["data"] == {"kind": "phone"}
            n = tc.portal.call(lambda: app.state.services.hub.broadcast("x", None,
                                                                         audience="mirror"))
            assert n == 0
            n = tc.portal.call(lambda: app.state.services.hub.broadcast("y", None,
                                                                         audience="phone"))
            assert n == 1 and ws.receive_json()["type"] == "y"


# ---- hardware and contracts ----


async def test_mock_hardware(hardware):
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(await hardware.camera.capture()))
    assert img.format == "JPEG" and img.size == (480, 640)
    assert hardware.camera.captures == 1
    hardware.camera.fail_with = RuntimeError("busy")
    with pytest.raises(RuntimeError):
        await hardware.camera.capture()
    seen = []
    unsub = hardware.cover.subscribe(seen.append)
    hardware.cover.set_closed(False)
    hardware.cover.set_closed(False)  # no change, no event
    unsub()
    hardware.cover.set_closed(True)
    assert seen == [False]
    await hardware.leds.set_level(1.0, full_duty=True)
    assert hardware.leds.history == [(1.0, True)]
    assert fixture_frame() == fixture_frame()


def test_contracts():
    w = WeatherSnapshot(fetched_at=datetime.now(UTC), temp_c=10, condition="rain",
                        display_unit="F")
    assert w.display(10) == 50 and w.display(None) is None and w.is_wet
    o = OutfitSuggestion(for_date=date(2026, 11, 25), reasoning="warm",
                         source="rules", items=[OutfitItem(item_id=uuid4(), slot="top")])
    p = TodayPayload(date=date(2026, 11, 25), generated_at=datetime.now(UTC), weather=w,
                     outfits=[o], spend_mode="degraded")
    assert TodayPayload.model_validate_json(p.model_dump_json()) == p
    with pytest.raises(ValueError):
        WeatherSnapshot(fetched_at=datetime(2026, 1, 1), temp_c=1)  # naive datetime
    with pytest.raises(ValueError):
        OutfitSuggestion(for_date=date.today(), reasoning="", source="rules", items=[])
