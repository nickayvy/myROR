"""iCal feeds (Google, Outlook, iCloud) -> CalendarEvent.

    store = CalendarStore(cache_dir / "calendars", settings)
    store.load()                                   # last good data from disk, no network
    await store.refresh(client)                    # fetch every configured feed
    store.events_for_day(day)                      # merged, sorted, deduplicated
    store.events_between(start_day, end_day)       # [start_day, end_day)

Rules:
- `webcal://` is fetched as `https://`.
- A feed that fails keeps its last good data and reports itself stale; the others are
  unaffected. The raw ICS of each feed's last good fetch is kept on disk (mode 0600),
  so the mirror shows events after a reboot with no internet.
- Recurrences are expanded with recurring-ical-events (RRULE, RDATE, EXDATE and
  moved instances). Floating times are read in TIMEZONE. All-day events are dates in
  TIMEZONE; their `start` is local midnight and `end` the exclusive end date.
- The same meeting in two feeds (same normalised title and start) appears once.
  Feed priority when merging: google, outlook, icloud.
- Feed URLs are secrets. They are never logged, never written to disk (only a hash, to
  notice when one changes) and never returned. Errors are recorded as a short class
  ("HTTP 404", "ConnectTimeout", "not an iCalendar file").
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from integrations.files import atomic_write_bytes, atomic_write_json, read_json
from mirror_api.config import Settings
from mirror_api.contracts import CalendarEvent

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO. Calendar URLs are secrets, so keep the
# client libraries at WARNING whatever the root log level is.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

SOURCES = ("google", "outlook", "icloud")  # also the merge priority
MAX_FEED_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT_S = 20.0
STALE_AFTER = timedelta(hours=2)


def feed_urls(settings: Settings) -> dict[str, str]:
    """Configured feeds only, with webcal:// rewritten. Never log the values."""
    raw = {
        "google": settings.cal_google_ics_url,
        "outlook": settings.cal_outlook_ics_url,
        "icloud": settings.cal_icloud_ics_url,
    }
    return {k: normalise_url(v) for k, v in raw.items() if v and v.strip()}


def normalise_url(url: str) -> str:
    url = url.strip()
    low = url.lower()
    if low.startswith("webcals://"):
        return "https://" + url[len("webcals://"):]
    if low.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def url_fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


_WS = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Key for de-duplication and formality caching: NFKC, casefold, collapsed spaces."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", title)).strip().casefold()


# ---- per-feed state --------------------------------------------------------------------


@dataclass
class FeedState:
    source: str
    url_hash: str | None = None
    calendar: icalendar.Calendar | None = None
    last_success: datetime | None = None  # last fetch that returned usable data
    last_attempt: datetime | None = None
    last_error: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    def status(self, now: datetime) -> dict[str, Any]:
        stale = self.last_success is None or self.last_error is not None or (
            now - self.last_success > STALE_AFTER
        )
        return {
            "has_data": self.calendar is not None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "last_error": self.last_error,
            "stale": stale,
        }


class FeedError(RuntimeError):
    pass


def parse_ics(data: bytes) -> icalendar.Calendar:
    head = data.lstrip()[:64].upper()
    if not head.startswith(b"BEGIN:VCALENDAR") and not head.startswith(b"\xef\xbb\xbfBEGIN"):
        raise FeedError("not an iCalendar file")
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception:  # noqa: BLE001 - any parser failure means "bad feed"
        raise FeedError("unparseable iCalendar") from None
    if isinstance(cal, list):  # several VCALENDARs concatenated: take the first
        cal = cal[0]
    return cal


class CalendarStore:
    def __init__(self, directory: Path, settings: Settings) -> None:
        self.dir = directory
        self.settings = settings
        self.feeds: dict[str, FeedState] = {}
        self.version = 0  # bumps whenever any feed's data changes
        self._memo: dict[tuple[int, date, date], list[CalendarEvent]] = {}

    @property
    def tz(self) -> ZoneInfo:
        return self.settings.tz

    def configured_sources(self) -> list[str]:
        return [s for s in SOURCES if s in feed_urls(self.settings)]

    # -- disk

    def _ics_path(self, source: str) -> Path:
        return self.dir / f"{source}.ics"

    def _meta_path(self, source: str) -> Path:
        return self.dir / f"{source}.json"

    def load(self) -> None:
        """Restore last good data for configured feeds; forget feeds whose URL changed
        or was removed."""
        urls = feed_urls(self.settings)
        for source in SOURCES:
            url = urls.get(source)
            if url is None:
                self._forget(source)
                continue
            meta = read_json(self._meta_path(source)) or {}
            fs = FeedState(source, url_fingerprint(url))
            if meta.get("url_hash") == fs.url_hash:
                try:
                    fs.calendar = parse_ics(self._ics_path(source).read_bytes())
                    fs.last_success = _dt(meta.get("last_success"))
                    fs.etag = meta.get("etag")
                    fs.last_modified = meta.get("last_modified")
                except (OSError, FeedError):
                    fs.calendar = None
            else:
                self._forget(source)
            self.feeds[source] = fs
        self._changed()

    def _forget(self, source: str) -> None:
        self.feeds.pop(source, None)
        for p in (self._ics_path(source), self._meta_path(source)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def _save(self, fs: FeedState, raw: bytes | None) -> None:
        if raw is not None:
            atomic_write_bytes(self._ics_path(fs.source), raw, mode=0o600)
        atomic_write_json(self._meta_path(fs.source), {
            "url_hash": fs.url_hash,
            "last_success": fs.last_success.isoformat() if fs.last_success else None,
            "etag": fs.etag,
            "last_modified": fs.last_modified,
        }, mode=0o600)

    # -- network

    async def refresh(self, client: httpx.AsyncClient, now: datetime | None = None) -> bool:
        """Fetch every configured feed. Returns True if any feed's data changed.
        Never raises for a feed failure."""
        now = now or datetime.now(UTC)
        urls = feed_urls(self.settings)
        changed = False
        for source in SOURCES:
            if source not in urls and source in self.feeds:
                self._forget(source)
                changed = True
        for source, url in urls.items():
            fs = self.feeds.get(source)
            if fs is None or fs.url_hash != url_fingerprint(url):
                self._forget(source)
                fs = self.feeds[source] = FeedState(source, url_fingerprint(url))
                changed = True
            changed |= await self._fetch(client, fs, url, now)
        if changed:
            self._changed()
        return changed

    async def _fetch(
        self, client: httpx.AsyncClient, fs: FeedState, url: str, now: datetime
    ) -> bool:
        fs.last_attempt = now
        headers = {"Accept": "text/calendar, */*;q=0.5"}
        if fs.calendar is not None:
            if fs.etag:
                headers["If-None-Match"] = fs.etag
            if fs.last_modified:
                headers["If-Modified-Since"] = fs.last_modified
        try:
            r = await client.get(url, headers=headers, timeout=FETCH_TIMEOUT_S,
                                 follow_redirects=True)
            if r.status_code == 304 and fs.calendar is not None:
                fs.last_success, fs.last_error = now, None
                self._save(fs, None)
                return False
            if r.status_code != 200:
                raise FeedError(f"HTTP {r.status_code}")
            raw = r.content
            if len(raw) > MAX_FEED_BYTES:
                raise FeedError("feed too large")
            cal = parse_ics(raw)
        except FeedError as e:
            fs.last_error = str(e)
        except httpx.HTTPError as e:
            # The exception text can contain the URL; record the class only.
            fs.last_error = type(e).__name__
        except Exception as e:  # noqa: BLE001 - one feed must not break the others
            fs.last_error = type(e).__name__
        else:
            fs.calendar = cal
            fs.last_success, fs.last_error = now, None
            fs.etag = r.headers.get("etag")
            fs.last_modified = r.headers.get("last-modified")
            self._save(fs, raw)
            return True
        log.warning("calendar %s: fetch failed (%s); keeping last good data", fs.source,
                    fs.last_error)
        return False

    def status(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        now = now or datetime.now(UTC)
        out = {}
        for source in SOURCES:
            fs = self.feeds.get(source)
            if fs is None:
                out[source] = {"configured": False}
            else:
                out[source] = {"configured": True, **fs.status(now)}
        return out

    def last_sync(self) -> datetime | None:
        """Oldest last-success among configured feeds: the data is at least this fresh.
        None while any configured feed has never synced."""
        times = [fs.last_success for fs in self.feeds.values()]
        if not times or any(t is None for t in times):
            return None
        return min(times)

    def stale_sources(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        return [s for s, st in self.status(now).items() if st.get("configured") and st["stale"]]

    # -- expansion

    def _changed(self) -> None:
        self.version += 1
        self._memo.clear()

    def events_for_day(self, day: date) -> list[CalendarEvent]:
        return self.events_between(day, day + timedelta(days=1))

    def events_between(self, start_day: date, end_day: date) -> list[CalendarEvent]:
        """Events overlapping local days [start_day, end_day), merged across feeds."""
        key = (self.version, start_day, end_day)
        if key in self._memo:
            return self._memo[key]
        tz = self.tz
        lo = datetime.combine(start_day, time(), tz)
        hi = datetime.combine(end_day, time(), tz)
        found: list[CalendarEvent] = []
        for source in SOURCES:
            fs = self.feeds.get(source)
            if fs is None or fs.calendar is None:
                continue
            found.extend(expand(fs.calendar, source, start_day, end_day, tz))
        merged = [e for e in merge(found) if _overlaps(e, lo, hi, start_day, end_day)]
        merged.sort(key=sort_key)
        if len(self._memo) > 32:
            self._memo.clear()
        self._memo[key] = merged
        return merged


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def sort_key(e: CalendarEvent) -> tuple:
    return (not e.all_day, e.start, normalise_title(e.title))


def _overlaps(e: CalendarEvent, lo: datetime, hi: datetime, d0: date, d1: date) -> bool:
    if e.all_day:
        first = e.start.date()
        last = e.end.date() if e.end else first + timedelta(days=1)
        return first < d1 and last > d0
    if e.end is None or e.end <= e.start:
        return lo <= e.start < hi
    return e.start < hi and e.end > lo


def _as_local(value: date | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # floating time: the mirror's own timezone
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    return datetime.combine(value, time(), tz)


def expand(
    cal: icalendar.Calendar, source: str, start_day: date, end_day: date, tz: ZoneInfo
) -> list[CalendarEvent]:
    """Every instance overlapping [start_day, end_day) in tz (with a day of margin;
    callers filter precisely)."""
    lo = datetime.combine(start_day - timedelta(days=1), time(), tz)
    hi = datetime.combine(end_day + timedelta(days=1), time(), tz)
    try:
        instances = recurring_ical_events.of(cal, skip_bad_series=True).between(lo, hi)
    except Exception as e:  # noqa: BLE001 - a broken feed yields no events, not a crash
        log.warning("calendar %s: expansion failed (%s)", source, type(e).__name__)
        return []
    out = []
    for comp in instances:
        try:
            ev = to_event(comp, source, tz)
        except Exception as e:  # noqa: BLE001 - skip one bad event
            log.info("calendar %s: skipping an event (%s)", source, type(e).__name__)
            continue
        if ev is not None:
            out.append(ev)
    return out


def to_event(comp: Any, source: str, tz: ZoneInfo) -> CalendarEvent | None:
    if str(comp.get("STATUS", "")).upper() == "CANCELLED":
        return None
    if "DTSTART" not in comp:
        return None
    raw_start = comp.decoded("DTSTART")
    all_day = not isinstance(raw_start, datetime)
    start = _as_local(raw_start, tz)
    end: datetime | None = None
    if "DTEND" in comp:
        end = _as_local(comp.decoded("DTEND"), tz)
    elif "DURATION" in comp:
        end = start + comp.decoded("DURATION")
    if all_day and (end is None or end <= start):
        end = start + timedelta(days=1)
    if all_day and end is not None:  # keep all-day ends on local midnight across DST
        end = datetime.combine(end.date(), time(), tz)
    title = str(comp.get("SUMMARY", "") or "").strip() or "(No title)"
    location = str(comp.get("LOCATION", "") or "").strip() or None
    uid = str(comp.get("UID", "") or "").strip()
    rid = comp.get("RECURRENCE-ID")
    rid_s = rid.dt.isoformat() if rid is not None and hasattr(rid, "dt") else start.isoformat()
    if not uid:
        uid = hashlib.sha256(f"{title}|{start.isoformat()}".encode()).hexdigest()[:24]
    return CalendarEvent(
        uid=f"{uid}/{rid_s}", source=source, title=title[:300], start=start, end=end,
        all_day=all_day, location=location[:300] if location else None,
    )


def dedupe_key(e: CalendarEvent) -> tuple:
    when = e.start.date().isoformat() if e.all_day else e.start.astimezone(UTC).isoformat()
    return (normalise_title(e.title), e.all_day, when)


def merge(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """Drop duplicates (same normalised title and start), keeping the copy from the
    highest-priority feed and borrowing a location from a duplicate if it lacks one."""
    rank = {s: i for i, s in enumerate(SOURCES)}
    best: dict[tuple, CalendarEvent] = {}
    for e in sorted(events, key=lambda e: rank.get(e.source, 99)):
        k = dedupe_key(e)
        kept = best.get(k)
        if kept is None:
            best[k] = e
        elif kept.location is None and e.location:
            best[k] = kept.model_copy(update={"location": e.location})
    return list(best.values())


# ---- merged feed for MagicMirror ------------------------------------------------------


def to_ics(events: list[CalendarEvent], now: datetime | None = None) -> bytes:
    """One VCALENDAR of expanded instances (no RRULEs), unique UIDs, UTC times."""
    now = now or datetime.now(UTC)
    cal = icalendar.Calendar()
    cal.add("prodid", "-//MY-ror//merged calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Mirror")
    for e in events:
        ve = icalendar.Event()
        digest = hashlib.sha256(f"{e.source}|{e.uid}".encode()).hexdigest()[:32]
        ve.add("uid", f"{digest}@my-ror.local")
        ve.add("summary", e.title)
        ve.add("dtstamp", now.astimezone(UTC))
        if e.all_day:
            ve.add("dtstart", e.start.date())
            ve.add("dtend", (e.end or e.start + timedelta(days=1)).date())
        else:
            ve.add("dtstart", e.start.astimezone(UTC))
            if e.end is not None:
                ve.add("dtend", e.end.astimezone(UTC))
        if e.location:
            ve.add("location", e.location)
        cal.add_component(ve)
    return cal.to_ical()
