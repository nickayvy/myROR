import logging
from datetime import UTC, date, datetime, timedelta

import httpx
import icalendar
import pytest

from integrations.calendars import (
    CalendarStore,
    feed_urls,
    normalise_title,
    normalise_url,
    parse_ics,
    to_ics,
)

from .conftest import MORNING, fixture_bytes

DAY = date(2026, 11, 25)


async def refreshed(settings, net, now=MORNING) -> CalendarStore:
    store = CalendarStore(settings.cache_dir / "cal", settings)
    store.load()
    async with httpx.AsyncClient(transport=net.transport) as client:
        await store.refresh(client, now)
    return store


def test_webcal_and_titles(cal_settings):
    assert normalise_url("webcal://x.example/a.ics") == "https://x.example/a.ics"
    assert normalise_url(" WEBCALS://x.example/a ") == "https://x.example/a"
    assert normalise_url("https://x.example/a") == "https://x.example/a"
    assert set(feed_urls(cal_settings)) == {"google", "outlook", "icloud"}
    assert all(u.startswith("https://") for u in feed_urls(cal_settings).values())
    assert normalise_title("  Client   PRESENTATION ") == "client presentation"


async def test_requests_use_https(cal_settings, net):
    await refreshed(cal_settings, net)
    cal_requests = [r for r in net.requests if r.url.host == "calendar.example"]
    assert len(cal_requests) == 3 and all(r.url.scheme == "https" for r in cal_requests)


async def test_day_expansion_merge_and_timezones(cal_settings, net):
    store = await refreshed(cal_settings, net)
    events = store.events_for_day(DAY)
    got = [(e.title, e.start.strftime("%H:%M"), e.source) for e in events]
    assert got == [
        ("Water the plants", "08:30", "google"),      # floating time, read as New York
        ("Quarterly review", "10:00", "outlook"),     # 16:00 W. Europe -> 10:00 New York
        ("Team standup (moved)", "11:00", "google"),  # overridden recurrence instance
        ("Client presentation", "14:00", "google"),   # in all three feeds, shown once
        ("Gym", "18:00", "icloud"),                   # UTC + DURATION
        ("Late call", "23:00", "outlook"),            # 05:00 next day in Europe
    ]
    pres = next(e for e in events if e.title == "Client presentation")
    assert pres.location == "Conference room B"  # borrowed from the Outlook duplicate
    assert all(str(e.start.tzinfo) == "America/New_York" for e in events)
    assert len({e.uid for e in events}) == len(events)
    assert "Cancelled thing" not in [e.title for e in events]


async def test_recurrence_exdate_and_all_day(cal_settings, net):
    store = await refreshed(cal_settings, net)
    titles = lambda d: [e.title for e in store.events_for_day(d)]  # noqa: E731
    assert "Team standup" in titles(date(2026, 11, 11))
    assert "Team standup" not in titles(date(2026, 11, 18))  # EXDATE
    dec2 = [e for e in store.events_for_day(date(2026, 12, 2)) if e.title == "Team standup"]
    assert dec2 and dec2[0].start.strftime("%H:%M") == "09:30"
    assert dec2[0].start.utcoffset() == timedelta(hours=-5)
    thu = store.events_for_day(date(2026, 11, 26))
    assert [e.title for e in thu] == ["Thanksgiving"]  # the 23:00 call does not spill over
    th = thu[0]
    assert th.all_day and th.start.hour == 0 and th.end.date() == date(2026, 11, 27)
    assert "Thanksgiving" not in titles(DAY)
    week = store.events_between(DAY, DAY + timedelta(days=7))
    assert [e.title for e in week].count("Thanksgiving") == 1


async def test_failed_feed_keeps_last_good_and_others_work(cal_settings, net, caplog):
    store = await refreshed(cal_settings, net)
    before = len(store.events_for_day(DAY))
    net.fail.add("outlook-secret")
    net.responses["icloud-secret"] = fixture_bytes("icloud.ics").replace(b"Gym", b"Yoga")
    later = MORNING + timedelta(hours=3)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=net.transport) as client:
            assert await store.refresh(client, later)
    titles = [e.title for e in store.events_for_day(DAY)]
    assert "Quarterly review" in titles and "Yoga" in titles and len(titles) == before
    st = store.status(later)
    assert st["outlook"]["stale"] and st["outlook"]["last_error"] == "ConnectError"
    assert not st["google"]["stale"] and not st["icloud"]["stale"]
    assert store.stale_sources(later) == ["outlook"]
    assert store.last_sync() == MORNING  # oldest good sync among feeds
    for secret in ("secret-abc", "secret-def", "secret-ghi", "calendar.example"):
        assert secret not in caplog.text
        assert secret not in str(st)


async def test_bad_content_is_a_failure(cal_settings, net):
    net.responses["google-secret"] = b"<html>Sign in</html>"
    store = await refreshed(cal_settings, net)
    assert store.status()["google"]["last_error"] == "not an iCalendar file"
    with pytest.raises(Exception, match="not an iCalendar"):
        parse_ics(b"nope")


async def test_survives_restart_offline_and_forgets_changed_url(cal_settings, net):
    store = await refreshed(cal_settings, net)
    expected = store.events_for_day(DAY)
    files = sorted(p.name for p in store.dir.iterdir())
    assert files == ["google.ics", "google.json", "icloud.ics", "icloud.json",
                     "outlook.ics", "outlook.json"]
    for p in store.dir.iterdir():
        assert (p.stat().st_mode & 0o077) == 0
        assert b"secret" not in p.read_bytes()  # no URL on disk
    again = CalendarStore(store.dir, cal_settings)
    again.load()
    assert again.events_for_day(DAY) == expected

    changed = cal_settings.model_copy(update={"cal_outlook_ics_url": "https://other.example/x"})
    third = CalendarStore(store.dir, changed)
    third.load()
    assert "Quarterly review" not in [e.title for e in third.events_for_day(DAY)]
    removed = cal_settings.model_copy(update={"cal_icloud_ics_url": ""})
    fourth = CalendarStore(store.dir, removed)
    fourth.load()
    assert "Gym" not in [e.title for e in fourth.events_for_day(DAY)]
    assert not (store.dir / "icloud.ics").exists()


async def test_not_modified(cal_settings, net):
    def etagged(request):
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=fixture_bytes("google.ics"), headers={"etag": '"v1"'})

    net.responses["google-secret"] = etagged
    store = await refreshed(cal_settings, net)
    async with httpx.AsyncClient(transport=net.transport) as client:
        later = MORNING + timedelta(minutes=20)
        await store.refresh(client, later)
    assert store.feeds["google"].last_success == later
    assert "Team standup (moved)" in [e.title for e in store.events_for_day(DAY)]


async def test_merged_ics(cal_settings, net):
    store = await refreshed(cal_settings, net)
    events = store.events_between(DAY - timedelta(days=1), DAY + timedelta(days=14))
    out = to_ics(events, datetime(2026, 11, 25, tzinfo=UTC))
    cal = icalendar.Calendar.from_ical(out)
    vevents = list(cal.walk("VEVENT"))
    assert len(vevents) == len(events)
    assert len({str(v["UID"]) for v in vevents}) == len(vevents)
    assert [str(v["SUMMARY"]) for v in vevents].count("Client presentation") == 1
    th = next(v for v in vevents if str(v["SUMMARY"]) == "Thanksgiving")
    assert th.decoded("DTSTART") == date(2026, 11, 26)
    assert b"RRULE" not in out
    assert b"secret" not in out
