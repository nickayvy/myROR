"""Fixtures for integration tests. No real network: feeds and Open-Meteo are served by
httpx.MockTransport from the files in fixtures/ (synthetic calendars; the forecast is
one real Open-Meteo response for a public New York coordinate)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mirror_api.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
# The forecast fixture was fetched at 2026-11-25 22:15 UTC (17:15 in New York).
FORECAST_NOW = datetime(2026, 11, 25, 22, 20, tzinfo=UTC)
MORNING = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)  # 07:00 New York

FEED_URLS = {
    "google": "webcal://calendar.example/feeds/google-secret-abc.ics",
    "outlook": "https://calendar.example/feeds/outlook-secret-def.ics",
    "icloud": "webcal://calendar.example/feeds/icloud-secret-ghi.ics",
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def forecast_json() -> dict[str, Any]:
    return json.loads(fixture_bytes("open_meteo_nyc.json"))


@pytest.fixture
def cal_settings(tmp_path) -> Settings:
    """Weather and all three feeds configured; no database needed."""
    return Settings(
        _env_file=None,
        llm="fake",
        timezone="America/New_York",
        weather_lat=40.71,
        weather_lon=-74.01,
        temp_unit="F",
        local_data_dir=tmp_path / "data",
        cal_google_ics_url=FEED_URLS["google"],
        cal_outlook_ics_url=FEED_URLS["outlook"],
        cal_icloud_ics_url=FEED_URLS["icloud"],
        hardware="mock",
    )


@dataclass
class FakeNet:
    """Programmable MockTransport. `responses` maps a path suffix to a Response factory
    or bytes; `fail` holds path suffixes that raise ConnectError."""

    forecast: dict[str, Any]
    responses: dict[str, Any] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        for source in ("google", "outlook", "icloud"):
            self.responses.setdefault(f"{source}-secret", fixture_bytes(f"{source}.ics"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.open-meteo.com":
            if "open-meteo" in self.fail:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json=self.forecast)
        for key in self.fail:
            if key in request.url.path:
                raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        for key, value in self.responses.items():
            if key in request.url.path:
                if callable(value):
                    return value(request)
                return httpx.Response(200, content=value,
                                      headers={"content-type": "text/calendar"})
        return httpx.Response(404, request=request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def count(self, host_or_path: str) -> int:
        return sum(1 for r in self.requests if host_or_path in str(r.url))


@pytest.fixture
def net(forecast_json) -> FakeNet:
    return FakeNet(forecast_json)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(MORNING)


class RecordingHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def broadcast(self, type_: str, data: Any = None, **_: Any) -> int:
        self.events.append((type_, data))
        return 0

    def of(self, type_: str) -> list[Any]:
        return [d for t, d in self.events if t == type_]


class NoDb:
    """Database stand-in that fails loudly if anything touches it."""

    configured = True
    checkouts = 0

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"database touched: {name}")


@pytest.fixture
def fake_services(cal_settings, fake_llm):
    """A Services-shaped object without a database (attribute access to db fails)."""
    return SimpleNamespace(
        settings=cal_settings, db=NoDb(), llm=fake_llm, hub=RecordingHub(),
        spend=SimpleNamespace(), hardware=None,
    )
