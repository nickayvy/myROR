"""Weather from Open-Meteo (https://open-meteo.com/en/docs, no key).

    raw = await fetch_forecast(client, settings)       # dict, the API response
    snap = snapshot_for(raw, day, now, settings)       # WeatherSnapshot | None

The raw response is what gets cached on disk, so a snapshot for "today" can be rebuilt
offline. Times are requested as unix seconds (`timeformat=unixtime`), which keeps them
exact across DST changes; they are converted to TIMEZONE here. Everything is stored
in Celsius; `display_unit` comes from TEMP_UNIT.

Weather is None (never an exception) when WEATHER_LAT/LON or TIMEZONE is not set.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from mirror_api.config import Settings
from mirror_api.contracts import HourlyWeather, WeatherCondition, WeatherSnapshot, c_to_f

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 3  # today plus two, so the recommender can plan a day ahead
HOURLY_STEP = 3  # "coarse": one entry every 3 hours
DRESS_HOUR = 8  # for a future day, "current" conditions are taken at 08:00 local
WET_PROBABILITY = 0.5

_CURRENT = ("temperature_2m", "apparent_temperature", "weather_code", "precipitation")
_HOURLY = ("temperature_2m", "apparent_temperature", "precipitation_probability",
           "precipitation", "weather_code")
_DAILY = ("temperature_2m_max", "temperature_2m_min", "precipitation_sum",
          "precipitation_probability_max", "weather_code")


class WeatherError(RuntimeError):
    """The forecast could not be fetched or understood."""


def configured(settings: Settings) -> bool:
    return (
        settings.weather_lat is not None
        and settings.weather_lon is not None
        and settings.timezone_configured
    )


# WMO weather interpretation codes, as listed in the Open-Meteo docs.
_WMO: dict[int, WeatherCondition] = {
    0: "clear", 1: "clear", 2: "partly_cloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "sleet", 57: "sleet",  # freezing drizzle
    61: "rain", 63: "rain", 65: "rain", 80: "rain", 81: "rain", 82: "rain",
    66: "sleet", 67: "sleet",  # freezing rain
    71: "snow", 73: "snow", 75: "snow", 77: "snow", 85: "snow", 86: "snow",
    95: "thunderstorm", 96: "thunderstorm", 97: "thunderstorm", 99: "thunderstorm",
}

_WORDS: dict[WeatherCondition, str] = {
    "clear": "Clear", "partly_cloudy": "Partly cloudy", "cloudy": "Cloudy", "fog": "Fog",
    "drizzle": "Drizzle", "rain": "Rain", "snow": "Snow", "sleet": "Sleet",
    "thunderstorm": "Thunderstorms", "unknown": "",
}
_PRECIP_NOUN: dict[WeatherCondition, str] = {
    "snow": "snow", "sleet": "sleet", "thunderstorm": "storms", "drizzle": "drizzle",
}
_SEVERITY: tuple[WeatherCondition, ...] = ("thunderstorm", "snow", "sleet", "rain", "drizzle")


def wmo_condition(code: Any) -> WeatherCondition:
    try:
        return _WMO.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


async def fetch_forecast(client: httpx.AsyncClient, settings: Settings) -> dict[str, Any]:
    """One request to Open-Meteo. Raises WeatherError on any failure."""
    params = {
        "latitude": f"{settings.weather_lat:.4f}",
        "longitude": f"{settings.weather_lon:.4f}",
        "timezone": settings.timezone,
        "timeformat": "unixtime",
        "forecast_days": FORECAST_DAYS,
        "current": ",".join(_CURRENT),
        "hourly": ",".join(_HOURLY),
        "daily": ",".join(_DAILY),
    }
    try:
        r = await client.get(FORECAST_URL, params=params, timeout=15.0)
    except httpx.HTTPError as e:
        raise WeatherError(type(e).__name__) from None
    if r.status_code != 200:
        reason = ""
        try:
            reason = str(r.json().get("reason", ""))[:200]
        except ValueError:
            pass
        raise WeatherError(f"HTTP {r.status_code} {reason}".strip())
    try:
        data = r.json()
    except ValueError:
        raise WeatherError("invalid JSON") from None
    if not isinstance(data, dict) or "hourly" not in data or "daily" not in data:
        raise WeatherError("unexpected response shape")
    return data


# ---- parsing -------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prob(v: Any) -> float | None:
    n = _num(v)
    return None if n is None else min(max(n / 100.0, 0.0), 1.0)


def _series(block: dict[str, Any], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Columns -> rows, with `time` as an aware datetime in tz."""
    times = block.get("time") or []
    rows = []
    for i, t in enumerate(times):
        try:
            at = datetime.fromtimestamp(int(t), UTC).astimezone(tz)
        except (TypeError, ValueError, OverflowError):
            continue
        row = {"time": at}
        for key, values in block.items():
            if key != "time" and isinstance(values, list) and i < len(values):
                row[key] = values[i]
        rows.append(row)
    return rows


def snapshot_for(
    raw: dict[str, Any] | None,
    day: date,
    now: datetime,
    settings: Settings,
    fetched_at: datetime | None = None,
) -> WeatherSnapshot | None:
    """A WeatherSnapshot for `day` from a cached Open-Meteo response, or None when the
    response does not cover that day. For today, current conditions come from the
    `current` block (or the latest hour if `current` is missing or too old); for a
    later day, from 08:00 local."""
    if not raw or not configured(settings):
        return None
    tz = settings.tz
    try:
        hourly = _series(raw.get("hourly") or {}, tz)
        daily = _series(raw.get("daily") or {}, tz)
    except AttributeError:
        return None
    day_row = next((d for d in daily if d["time"].date() == day), None)
    day_hours = [h for h in hourly if h["time"].date() == day]
    if day_row is None or not day_hours:
        return None

    now_local = now.astimezone(tz)
    is_today = now_local.date() == day
    current = raw.get("current") or {}
    cur_time = None
    try:
        cur_time = datetime.fromtimestamp(int(current["time"]), UTC)
    except (KeyError, TypeError, ValueError, OverflowError):
        pass

    if is_today and cur_time is not None and abs((now - cur_time).total_seconds()) < 3 * 3600:
        temp = _num(current.get("temperature_2m"))
        feels = _num(current.get("apparent_temperature"))
        code = current.get("weather_code")
    else:
        ref = now_local if is_today else datetime.combine(day, time(DRESS_HOUR), tz)
        hour = min(day_hours, key=lambda h: abs((h["time"] - ref).total_seconds()))
        temp = _num(hour.get("temperature_2m"))
        feels = _num(hour.get("apparent_temperature"))
        code = hour.get("weather_code")
    if temp is None:
        return None

    # The coarse list covers the rest of the day (all of it for a future day).
    start = now_local.replace(minute=0, second=0, microsecond=0) if is_today else None
    upcoming = [h for h in day_hours if start is None or h["time"] >= start]
    coarse = []
    for h in upcoming[::HOURLY_STEP]:
        t = _num(h.get("temperature_2m"))
        if t is None:
            continue
        coarse.append(HourlyWeather(
            time=h["time"], temp_c=t, precip_probability=_prob(h.get("precipitation_probability")),
            condition=wmo_condition(h.get("weather_code")),
        ))

    precip_mm = _num(day_row.get("precipitation_sum"))
    snap = WeatherSnapshot(
        fetched_at=fetched_at or now,
        temp_c=temp,
        feels_like_c=feels,
        high_c=_num(day_row.get("temperature_2m_max")),
        low_c=_num(day_row.get("temperature_2m_min")),
        precip_probability=_prob(day_row.get("precipitation_probability_max")),
        precip_mm=None if precip_mm is None else max(precip_mm, 0.0),
        condition=wmo_condition(code),
        hourly=coarse,
        display_unit=settings.temp_unit,
    )
    return snap.model_copy(update={"summary": summarize(snap, upcoming, tz)})


def _deg(c: float | None, unit: str) -> str:
    if c is None:
        return "?"
    return f"{round(c_to_f(c) if unit == 'F' else c)}°"


def _clock(t: datetime) -> str:
    h = t.hour % 12 or 12
    return f"{h} {'am' if t.hour < 12 else 'pm'}"


def summarize(snap: WeatherSnapshot, hours: list[dict[str, Any]], tz: ZoneInfo) -> str:
    """One line for the mirror, e.g. "Cloudy, 44°/31°. Rain likely from 3 pm"."""
    parts = []
    word = _WORDS.get(snap.condition, "")
    temps = f"{_deg(snap.high_c, snap.display_unit)}/{_deg(snap.low_c, snap.display_unit)}"
    parts.append(f"{word}, {temps}" if word else temps)
    wet_now = snap.condition in ("drizzle", "rain", "sleet", "snow", "thunderstorm")
    first_wet = next(
        (h for h in hours if (_prob(h.get("precipitation_probability")) or 0) >= WET_PROBABILITY),
        None,
    )
    if first_wet is not None:
        # Name the heaviest kind of precipitation still to come, not just the first.
        later = [h for h in hours if h["time"] >= first_wet["time"]]
        conds = {wmo_condition(h.get("weather_code")) for h in later}
        cond = next((c for c in _SEVERITY if c in conds), "rain")
        noun = _PRECIP_NOUN.get(cond, "rain")
        when = first_wet["time"]
        if wet_now and hours and when <= hours[0]["time"] + timedelta(hours=1):
            parts.append(f"{noun.capitalize()} now")
        else:
            parts.append(f"{noun.capitalize()} likely from {_clock(when)}")
    return ". ".join(parts)
