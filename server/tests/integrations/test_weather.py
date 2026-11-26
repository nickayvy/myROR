from datetime import date

import httpx
import pytest

from integrations import weather as wx

from .conftest import FORECAST_NOW, MORNING

TODAY = date(2026, 11, 25)
TOMORROW = date(2026, 11, 26)


@pytest.mark.parametrize(("code", "cond"), [
    (0, "clear"), (1, "clear"), (2, "partly_cloudy"), (3, "cloudy"), (45, "fog"),
    (53, "drizzle"), (57, "sleet"), (63, "rain"), (81, "rain"), (67, "sleet"),
    (75, "snow"), (86, "snow"), (95, "thunderstorm"), (99, "thunderstorm"),
    (42, "unknown"), (None, "unknown"), ("x", "unknown"),
])
def test_wmo_mapping(code, cond):
    assert wx.wmo_condition(code) == cond


def test_snapshot_today_from_real_response(forecast_json, cal_settings):
    s = wx.snapshot_for(forecast_json, TODAY, FORECAST_NOW, cal_settings)
    assert s is not None
    assert s.temp_c == 4.2 and s.feels_like_c == 0.7  # from the `current` block
    assert s.high_c == 6.3 and s.low_c == -0.5
    assert s.precip_probability == pytest.approx(0.01)
    assert s.precip_mm == 0.0
    assert s.condition == "cloudy"
    assert s.display_unit == "F" and s.display(s.high_c) == 43
    # coarse hourly list: rest of today only, every 3 hours, local times
    assert s.hourly and all(h.time.date() == TODAY for h in s.hourly)
    assert s.hourly[0].time.hour == 17 and s.hourly[1].time.hour == 20
    assert s.summary == "Cloudy, 43°/31°"
    assert not s.is_wet


def test_snapshot_tomorrow_uses_morning_hour(forecast_json, cal_settings):
    s = wx.snapshot_for(forecast_json, TOMORROW, FORECAST_NOW, cal_settings)
    assert s.high_c == 14.0 and s.precip_probability == 1.0 and s.precip_mm == 55.45
    assert s.is_wet
    assert len(s.hourly) == 8  # whole day, every 3 hours
    assert "Rain likely from" in s.summary or "Rain now" in s.summary


def test_stale_current_block_falls_back_to_hourly(forecast_json, cal_settings):
    s = wx.snapshot_for(forecast_json, TODAY, MORNING, cal_settings)  # 07:00 local
    assert s.temp_c == forecast_json["hourly"]["temperature_2m"][7]


def test_unconfigured_or_out_of_range(forecast_json, cal_settings):
    assert wx.snapshot_for(forecast_json, date(2026, 12, 25), FORECAST_NOW, cal_settings) is None
    assert wx.snapshot_for(None, TODAY, FORECAST_NOW, cal_settings) is None
    no_loc = cal_settings.model_copy(update={"weather_lat": None})
    assert not wx.configured(no_loc)
    assert wx.snapshot_for(forecast_json, TODAY, FORECAST_NOW, no_loc) is None
    no_tz = cal_settings.model_copy(update={"timezone": ""})
    assert not wx.configured(no_tz)


def test_celsius_display(forecast_json, cal_settings):
    s = wx.snapshot_for(forecast_json, TODAY, FORECAST_NOW,
                        cal_settings.model_copy(update={"temp_unit": "C"}))
    assert s.display_unit == "C" and s.summary == "Cloudy, 6°/0°"


async def test_fetch_request_parameters(net, cal_settings):
    async with httpx.AsyncClient(transport=net.transport) as client:
        data = await wx.fetch_forecast(client, cal_settings)
    assert data["current"]["temperature_2m"] == 4.2
    q = net.requests[0].url.params
    assert q["timezone"] == "America/New_York" and q["timeformat"] == "unixtime"
    assert "apparent_temperature" in q["current"] and "precipitation_probability" in q["hourly"]
    assert "precipitation_probability_max" in q["daily"]


async def test_fetch_errors():
    def bad(request):
        return httpx.Response(400, json={"error": True, "reason": "Cannot initialize"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
        with pytest.raises(wx.WeatherError, match="HTTP 400 Cannot initialize"):
            await wx.fetch_forecast(client, _settings())

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(wx.WeatherError, match="ConnectError"):
            await wx.fetch_forecast(client, _settings())


def _settings():
    from mirror_api.config import Settings

    return Settings(_env_file=None, timezone="America/New_York", weather_lat=1, weather_lon=2)
