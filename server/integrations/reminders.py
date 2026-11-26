"""Reminders pushed by the iPhone Shortcut (contract and build steps in README.md).

    lists, rows, unparsed = normalise(body, tz)   # tolerant parsing, raises PayloadError
    await replace_lists(db, lists, rows)          # one transaction per POST
    reminders, synced = await load_all(db)

Body: {"list": "Groceries", "reminders": [{"id", "title", "due", "completed"}, ...]}
or a JSON array of such objects, or {"lists": [...]}. The stored set for every list
named is replaced, so a list sent with no reminders is cleared.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from db import Database
from mirror_api.contracts import Reminder

MAX_LISTS = 20
MAX_REMINDERS = 1000
MAX_TITLE = 500
MAX_LIST_NAME = 100
DATE_ONLY_DUE = time(23, 59)  # a date-only due date means "by the end of that day"


class PayloadError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    id: str
    title: str
    due: datetime | None
    list: str
    completed: bool


# ---- tolerant field access ------------------------------------------------------------

_KEY_ALIASES = {
    "id": ("id", "identifier", "uuid", "creationdate", "created"),
    "title": ("title", "name", "text"),
    "due": ("due", "duedate", "due_date", "date"),
    "completed": ("completed", "iscompleted", "is_completed", "done"),
    "list": ("list", "listname", "list_name", "name"),
    "reminders": ("reminders", "items", "tasks"),
}


def _norm_key(k: str) -> str:
    return re.sub(r"[\s_-]", "", str(k)).casefold()


def _get(obj: dict[str, Any], field: str) -> Any:
    keys = {_norm_key(k): v for k, v in obj.items()}
    for alias in _KEY_ALIASES[field]:
        v = keys.get(_norm_key(alias))
        if v is not None:
            return v
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise PayloadError("reminders must be an array") from None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise PayloadError("reminders must be an array")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().casefold() in ("true", "yes", "1", "y", "done", "completed")


# ---- due dates ------------------------------------------------------------------------

_SPACES = re.compile(r"[   \s]+")
_FORMATS_DT = (
    "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d, %Y %H:%M", "%B %d, %Y %H:%M",
    "%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p", "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p",
    "%m/%d/%y, %H:%M", "%m/%d/%Y, %H:%M", "%d %b %Y %H:%M", "%d %B %Y %H:%M",
    "%Y-%m-%d %I:%M %p", "%Y/%m/%d %H:%M", "%A, %B %d, %Y %I:%M %p", "%a, %b %d, %Y %I:%M %p",
)
_FORMATS_D = (
    "%b %d, %Y", "%B %d, %Y", "%m/%d/%y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d",
    "%A, %B %d, %Y", "%a, %b %d, %Y",
)


def parse_due(
    value: Any, tz: ZoneInfo, now: datetime | None = None
) -> tuple[datetime | None, bool]:
    """(aware datetime or None, ok). ok=False means a value was given but not understood;
    the reminder is kept without a due date."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int | float):
        secs = value / 1000 if value > 1e11 else value  # milliseconds or seconds
        if 1e9 <= secs <= 1e10:
            return datetime.fromtimestamp(secs, UTC), True
        return None, False
    if not isinstance(value, str):
        return None, False
    s = _SPACES.sub(" ", value).strip()
    s = re.sub(r"\s+at\s+", " ", s, flags=re.IGNORECASE)
    try:
        iso = s.replace(" ", "T", 1) if re.match(r"^\d{4}-\d\d-\d\d \d", s) else s
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is not None:
        if len(s) == 10 and "-" in s:  # date only
            return datetime.combine(parsed.date(), DATE_ONLY_DUE, tz), True
        return (parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed), True
    low = s.casefold()
    now = (now or datetime.now(UTC)).astimezone(tz)
    for word, delta in (("today", 0), ("tomorrow", 1), ("yesterday", -1)):
        if low.startswith(word):
            day = now.date() + timedelta(days=delta)
            rest = s[len(word):].strip(" ,")
            if not rest:
                return datetime.combine(day, DATE_ONLY_DUE, tz), True
            for fmt in ("%I:%M %p", "%H:%M", "%I %p"):
                try:
                    t = datetime.strptime(rest.upper(), fmt).time()
                    return datetime.combine(day, t, tz), True
                except ValueError:
                    continue
            return None, False
    upper = re.sub(r"\b(am|pm)\b", lambda m: m.group(1).upper(), s, flags=re.IGNORECASE)
    for fmt in _FORMATS_DT:
        try:
            return datetime.strptime(upper, fmt).replace(tzinfo=tz), True
        except ValueError:
            continue
    for fmt in _FORMATS_D:
        try:
            d: date = datetime.strptime(upper, fmt).date()
            return datetime.combine(d, DATE_ONLY_DUE, tz), True
        except ValueError:
            continue
    return None, False


# ---- payload --------------------------------------------------------------------------


def stored_id(list_name: str, given: str) -> str:
    """Primary key: namespaced by list so two lists can never collide."""
    return hashlib.sha256(f"{list_name}\0{given}".encode()).hexdigest()[:24]


def normalise(
    body: Any, tz: ZoneInfo, now: datetime | None = None
) -> tuple[list[str], list[Row], int]:
    """Returns (list names, rows, count of due dates that could not be parsed)."""
    nested = isinstance(body, dict) and isinstance(body.get("lists"), list)
    if nested and _get(body, "list") is None:
        groups = body["lists"]
    elif isinstance(body, list):
        groups = body
    elif isinstance(body, dict):
        groups = [body]
    else:
        raise PayloadError("expected a JSON object with 'list' and 'reminders'")
    if not groups or len(groups) > MAX_LISTS:
        raise PayloadError(f"send between 1 and {MAX_LISTS} lists")

    lists: list[str] = []
    rows: dict[str, Row] = {}
    unparsed = 0
    for g in groups:
        if not isinstance(g, dict):
            raise PayloadError("each list must be a JSON object")
        name = _get(g, "list")
        if not isinstance(name, str) or not name.strip():
            raise PayloadError("'list' (the Reminders list name) is required")
        name = name.strip()[:MAX_LIST_NAME]
        if name not in lists:
            lists.append(name)
        for item in _as_list(_get(g, "reminders")):
            if isinstance(item, str):
                item = {"title": item}
            if not isinstance(item, dict):
                continue
            title = _get(item, "title")
            if title is None or not str(title).strip():
                continue
            title = str(title).strip()[:MAX_TITLE]
            raw_due = _get(item, "due")
            due, ok = parse_due(raw_due, tz, now)
            unparsed += 0 if ok else 1
            given = _get(item, "id")
            given = str(given).strip() if given is not None and str(given).strip() else (
                f"{title}|{raw_due or ''}"
            )
            rid = stored_id(name, given)
            n = 2
            while rid in rows:  # two reminders sharing an id in one push: keep both
                rid = stored_id(name, f"{given}#{n}")
                n += 1
            rows[rid] = Row(rid, title, due, name, _truthy(_get(item, "completed") or False))
            if len(rows) > MAX_REMINDERS:
                raise PayloadError(f"at most {MAX_REMINDERS} reminders per push")
    return lists, list(rows.values()), unparsed


async def replace_lists(db: Database, lists: list[str], rows: list[Row]) -> datetime:
    synced_at = datetime.now(UTC)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM reminders WHERE list = ANY(%s)", (lists,))
        for r in rows:
            await conn.execute(
                "INSERT INTO reminders (id, title, due, list, completed, synced_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, due = EXCLUDED.due,"
                " list = EXCLUDED.list, completed = EXCLUDED.completed,"
                " synced_at = EXCLUDED.synced_at",
                (r.id, r.title, r.due, r.list, r.completed, synced_at),
            )
    return synced_at


async def load_all(db: Database) -> tuple[list[Reminder], dict[str, str]]:
    rows = await db.fetch(
        "SELECT id, title, due, list, completed FROM reminders"
        " ORDER BY completed, due NULLS LAST, title"
    )
    synced = await db.fetch(
        "SELECT list, max(synced_at) AS at FROM reminders GROUP BY list ORDER BY list"
    )
    return [Reminder(**r) for r in rows], {r["list"]: r["at"].isoformat() for r in synced}


def visible(reminders: list[Reminder], limit: int = 50) -> list[Reminder]:
    """What the mirror shows: open reminders, soonest due first."""
    far = datetime.max.replace(tzinfo=UTC)
    open_ = [r for r in reminders if not r.completed]
    open_.sort(key=lambda r: (r.due is None, r.due or far, r.title.casefold()))
    return open_[:limit]
