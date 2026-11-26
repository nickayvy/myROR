"""HTTP routes against the real test database: reminders, shortcut tokens, today,
calendar.ics."""

import time
from datetime import UTC, datetime

import pytest

from integrations import today as today_mod
from integrations.reminders import normalise, parse_due
from mirror_api.config import Settings
from tests.conftest import LAN_CLIENT, LOCAL_CLIENT, http_client

NY = Settings(_env_file=None, timezone="America/New_York").tz
NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)

SHORTCUT_BODY = {
    "list": "Errands",
    "reminders": [
        {"id": "2026-11-20T09:15:02-05:00", "title": "Pick up dry cleaning",
         "due": "2026-11-25T17:00:00-05:00", "completed": False},
        {"id": "2026-11-21T10:00:00-05:00", "title": "Buy stamps", "due": "Nov 26, 2026 at 9:00 AM",
         "completed": "No"},
        {"id": "2026-11-22T10:00:00-05:00", "title": "Return library book", "due": "",
         "completed": False},
        {"id": "2026-11-23T10:00:00-05:00", "title": "Old thing", "due": "sometime soon",
         "completed": "Yes"},
    ],
}


@pytest.mark.parametrize(("raw", "expected"), [
    ("2026-11-25T17:00:00-05:00", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("2026-11-25T22:00:00Z", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("2026-11-25 17:00", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("2026-11-25", datetime(2026, 11, 26, 4, 59, tzinfo=UTC)),  # end of local day
    ("Nov 25, 2026 at 5:00 PM", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("November 25, 2026 at 5:00 PM", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("11/25/26, 5:00 PM", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("Wednesday, November 25, 2026 at 5:00 PM", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("Today at 5:00 PM", datetime(2026, 11, 25, 22, tzinfo=UTC)),
    ("Tomorrow", datetime(2026, 11, 27, 4, 59, tzinfo=UTC)),
    (1795644000, datetime.fromtimestamp(1795644000, UTC)),
])
def test_parse_due_formats(raw, expected):
    got, ok = parse_due(raw, NY, NOW)
    assert ok and got == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_due_empty(raw):
    assert parse_due(raw, NY, NOW) == (None, True)


@pytest.mark.parametrize("raw", ["sometime soon", "32/13/2026", True, 12])
def test_parse_due_garbage(raw):
    assert parse_due(raw, NY, NOW) == (None, False)


def test_normalise_shapes():
    lists, rows, bad = normalise(SHORTCUT_BODY, NY, NOW)
    assert lists == ["Errands"] and len(rows) == 4 and bad == 1
    assert [r.completed for r in rows] == [False, False, False, True]
    # Shortcut-style keys, a stringified array, a missing id, several lists at once.
    body = [{"List": "A", "Reminders": '[{"Title": "x", "Due Date": "2026-11-25"}]'},
            {"list": "B"}]
    lists, rows, bad = normalise(body, NY, NOW)
    assert lists == ["A", "B"] and len(rows) == 1 and rows[0].title == "x" and bad == 0
    # The same id in two lists does not collide.
    _, r1, _ = normalise({"list": "A", "reminders": [{"id": "1", "title": "t"}]}, NY, NOW)
    _, r2, _ = normalise({"list": "B", "reminders": [{"id": "1", "title": "t"}]}, NY, NOW)
    assert r1[0].id != r2[0].id


async def test_shortcut_token_and_reminders_flow(app, authed, remote, client, db):
    # The phone mints a token for the Shortcut.
    r = await authed.post("/api/shortcut/tokens", json={"name": "Reminders Shortcut"})
    assert r.status_code == 201, r.text
    sc = r.json()
    async with http_client(app, LAN_CLIENT, token=sc["token"]) as shortcut:
        r = await shortcut.post("/api/reminders", json=SHORTCUT_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stored"] == 4 and body["open"] == 3 and body["due_unparsed"] == 1
        # A shortcut token cannot mint more tokens.
        assert (await shortcut.post("/api/shortcut/tokens")).status_code == 403

        today = (await client.get("/api/today")).json()
        assert [x["title"] for x in today["reminders"]] == [
            "Pick up dry cleaning", "Buy stamps", "Return library book"]
        assert today["last_sync"]["reminders"] is not None

        # Replace semantics: the next push for that list replaces it.
        r = await shortcut.post("/api/reminders", json={"list": "Errands", "reminders": [
            {"id": "n1", "title": "Only this", "due": None, "completed": False}]})
        assert r.status_code == 200
        other = {"list": "Groceries", "reminders": [{"title": "Milk"}]}
        assert (await shortcut.post("/api/reminders", json=other)).status_code == 200
        got = (await authed.get("/api/reminders")).json()
        assert sorted(x["title"] for x in got["reminders"]) == ["Milk", "Only this"]
        assert set(got["lists"]) == {"Errands", "Groceries"}
        # An empty push clears just that list.
        assert (await shortcut.post("/api/reminders", json={"list": "Errands"})).status_code == 200
        got = (await authed.get("/api/reminders")).json()
        assert [x["title"] for x in got["reminders"]] == ["Milk"]

        bad = await shortcut.post("/api/reminders", json={"reminders": []})
        assert bad.status_code == 422

    listed = (await authed.get("/api/shortcut/tokens")).json()
    assert [d["id"] for d in listed] == [sc["device_id"]] and "token" not in listed[0]
    assert (await authed.delete(f"/api/shortcut/tokens/{sc['device_id']}")).status_code == 204
    async with http_client(app, LAN_CLIENT, token=sc["token"]) as shortcut:
        assert (await shortcut.post("/api/reminders", json=SHORTCUT_BODY)).status_code == 401
    # Revoking a phone through this route is refused.
    phone_id = (await authed.get("/api/devices")).json()[0]["id"]
    assert (await authed.delete(f"/api/shortcut/tokens/{phone_id}")).status_code == 404
    assert (await remote.post("/api/shortcut/tokens")).status_code == 401


async def test_get_today_serves_from_memory(app, client, authed, db):
    services = app.state.services
    state = today_mod.get_state(services)
    await state.refresh()  # bootstrap once (reads reminders and spend mode)
    before = db.checkouts
    t0 = time.perf_counter()
    for _ in range(20):
        r = await client.get("/api/today")
        assert r.status_code == 200
    per_call = (time.perf_counter() - t0) / 20
    assert db.checkouts == before
    assert per_call < 0.05
    assert (await authed.get("/api/today")).status_code == 200
    assert (await authed.get("/api/today/status")).json()["calendars"]["google"] == {
        "configured": False}
    r = await authed.post("/api/today/refresh")
    assert r.status_code == 200 and r.json()["spend_mode"] == "normal"


async def test_calendar_ics_is_local_only(app, client, authed):
    r = await client.get("/api/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert r.content.startswith(b"BEGIN:VCALENDAR")
    assert (await authed.get("/api/calendar.ics")).status_code == 403


async def test_today_answers_with_database_down(make_app, settings, net):
    """The database URL points nowhere: /api/today still answers from the cache."""
    good = await make_app(start_workers=False)
    state = today_mod.get_state(good.state.services)
    state.reminders_loaded = state.spend_known = True
    await state.publish()

    from db import Database

    dead = Database("postgresql://mirror:mirror@127.0.0.1:1/none", max_size=1, max_idle=1)
    app = await make_app(start_workers=False, db=dead)
    async with http_client(app, LOCAL_CLIENT) as c:
        t0 = time.perf_counter()
        r = await c.get("/api/today")
        assert r.status_code == 200 and time.perf_counter() - t0 < 0.5
        assert (await c.get("/api/reminders")).json()["cached"] is True
    await dead.close()
