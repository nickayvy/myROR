"""The "today" payload: weather, events, reminders, outfits, kept in memory and on disk.

Design (HANDOFF A3, A4): the database is remote and must be allowed to scale to zero,
and the mirror must show the day within 2 seconds even with the internet down. So:

- The assembled TodayPayload lives in memory and in `<cache_dir>/integrations/today.json`
  (atomic write), next to the last good forecast, the raw ICS of each feed and the
  reminders and outfits last seen. It is loaded from disk at startup with no network.
- `GET /api/today` serves the in-memory payload and never touches the database. The
  very first request of a process that has nothing cached starts one background
  bootstrap (reminders, outfits, spend mode from the database).
- In-process timers (one asyncio task, a tick every minute) refresh the weather every
  30 minutes and the calendars every 15 minutes over HTTP, and roll the payload over
  at local midnight. **Timers never query the database.**
- Database reads (outfits, reminders on first boot, spend mode) and formality tagging
  happen only on triggers: `refresh_if_stale(services)` (the presence sensor and
  `POST /api/today/refresh` from the phone), `invalidate_outfits(services)` (the
  recommender, after storing outfits), a reminders push, and `get_day_context` (the
  recommender). Each is rate-limited, so presence events do not keep Neon awake.
- `today.updated` is broadcast on the hub (data: the payload) whenever the payload
  changes, ignoring `generated_at`.

Public functions: get_state, refresh_if_stale, invalidate_outfits, current_payload,
reminders_changed, start, stop.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from integrations import weather as wx
from integrations.calendars import CalendarStore, feed_urls
from integrations.files import atomic_write_json, read_json
from integrations.formality import FormalityCache, tag_titles, title_hash
from integrations.reminders import load_all, visible
from mirror_api.contracts import (
    CalendarEvent,
    OutfitSuggestion,
    Reminder,
    SpendMode,
    SyncTimes,
    TodayPayload,
    WeatherSnapshot,
)

if TYPE_CHECKING:
    from mirror_api.services import Services

log = logging.getLogger(__name__)

TICK_S = 60.0
WEATHER_TTL = timedelta(minutes=30)
CALENDAR_TTL = timedelta(minutes=15)
FAILURE_RETRY = timedelta(minutes=5)
# Rate limits for database-touching work on presence/phone triggers.
OUTFITS_RECHECK_S = 10 * 60  # while today's outfits are missing
SPEND_RECHECK_S = 60 * 60
TAG_RETRY_S = 60 * 60  # a title the LLM could not rate is retried after this
UPCOMING_DAYS = 7  # titles tagged ahead of time, and the ICS window (plus a margin)
OUTFITS_TIMEOUT_S = 20.0
DB_TIMEOUT_S = 10.0
STATE_ATTR = "integrations"  # the Services field that holds IntegrationsState
CACHE_VERSION = 1
COLLAGE_LINK_TTL_S = 3600

OutfitsProvider = Callable[["Services", date], Awaitable[list[OutfitSuggestion] | None]]


def utcnow() -> datetime:
    return datetime.now(UTC)


async def recommender_outfits(services: Services, for_date: date) -> list[OutfitSuggestion] | None:
    """The fixed seam to the recommender. None means "the recommender is not there"."""
    try:
        from recommender.today import get_outfits
    except ImportError:
        return None
    return await get_outfits(services, for_date)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


class IntegrationsState:
    def __init__(
        self,
        services: Services,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utcnow,
        outfits_provider: OutfitsProvider = recommender_outfits,
    ) -> None:
        self.services = services
        self.settings = services.settings
        self.clock = clock
        self.outfits_provider = outfits_provider
        self.dir = self.settings.cache_dir / "integrations"
        self.calendars = CalendarStore(self.dir / "calendars", self.settings)
        self.formality = FormalityCache(self.dir / "formality.json")
        self.transport = transport

        self.forecast: dict[str, Any] | None = None
        self.forecast_at: datetime | None = None
        self.weather_error: str | None = None
        self.reminders: list[Reminder] = []
        self.reminders_at: datetime | None = None
        self.reminders_loaded = False  # true once read from the DB or pushed
        self.outfits: dict[str, list[OutfitSuggestion]] = {}  # iso date -> outfits
        self.outfits_at: datetime | None = None
        self.spend_mode: SpendMode = "normal"
        self.spend_known = False
        self.db_error: str | None = None  # last database read failed (class name)
        self.payload: TodayPayload | None = None

        self.loaded = False
        self._client: httpx.AsyncClient | None = None
        self._net_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._next_weather: datetime | None = None
        self._next_calendars: datetime | None = None
        self._outfits_checked: dict[str, float] = {}
        self._spend_checked: float | None = None
        self._tag_tried: dict[str, float] = {}
        self._bootstrapped = False
        self._task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    # ---- time

    def now(self) -> datetime:
        return self.clock()

    def today(self) -> date:
        return self.now().astimezone(self.settings.tz).date()

    # ---- disk

    @property
    def cache_file(self):
        return self.dir / "today.json"

    def load(self) -> None:
        """Restore everything from disk. No network, no database."""
        self.loaded = True
        self.calendars.load()
        data = read_json(self.cache_file)
        if not isinstance(data, dict) or data.get("v") != CACHE_VERSION:
            return
        try:
            self.forecast = data.get("forecast")
            self.forecast_at = _dt(data.get("forecast_at"))
            self.reminders = [Reminder(**r) for r in data.get("reminders", [])]
            self.reminders_at = _dt(data.get("reminders_at"))
            self.reminders_loaded = bool(data.get("reminders_loaded"))
            self.outfits = {
                d: [OutfitSuggestion(**o) for o in lst]
                for d, lst in (data.get("outfits") or {}).items()
            }
            self.outfits_at = _dt(data.get("outfits_at"))
            self.spend_mode = data.get("spend_mode") or "normal"
            self.spend_known = bool(data.get("spend_known"))
            if data.get("payload"):
                p = TodayPayload(**data["payload"])
                if p.date == self.today():
                    self.payload = p
        except Exception as e:  # noqa: BLE001 - a bad cache must never stop the mirror
            log.warning("ignoring today cache (%s)", type(e).__name__)
        if not wx.configured(self.settings):
            self.forecast = None
        self._bootstrapped = self.reminders_loaded and self.spend_known

    def save(self) -> None:
        today = self.today().isoformat()
        self.outfits = {d: v for d, v in self.outfits.items() if d >= today}
        try:
            atomic_write_json(self.cache_file, {
                "v": CACHE_VERSION,
                "payload": self.payload.model_dump(mode="json") if self.payload else None,
                "forecast": self.forecast,
                "forecast_at": _iso(self.forecast_at),
                "reminders": [r.model_dump(mode="json") for r in self.reminders],
                "reminders_at": _iso(self.reminders_at),
                "reminders_loaded": self.reminders_loaded,
                "outfits": {d: [o.model_dump(mode="json") for o in v]
                            for d, v in self.outfits.items()},
                "outfits_at": _iso(self.outfits_at),
                "spend_mode": self.spend_mode,
                "spend_known": self.spend_known,
            })
        except OSError as e:
            log.warning("could not write today cache (%s)", type(e).__name__)

    # ---- lifecycle

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self.transport, headers={"User-Agent": "mirror-api/0.1"}
            )
        return self._client

    async def start(self, *, timers: bool = True) -> None:
        if not self.loaded:
            self.load()
        if self.payload is None or self.payload.date != self.today():
            self.payload = self.build()
            self.save()
        # The presence sensor is handled by mirror_api.presence, which calls
        # refresh_if_stale on an arrival when something is stale.
        if timers and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="integrations-timers")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._background) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._background.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def spawn(self, coro) -> asyncio.Task:
        """A background task owned by this state (cancelled by stop())."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("integrations timer tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> TodayPayload:
        """Timer work: HTTP refreshes when due, midnight rollover. No database."""
        await self.refresh_network()
        return await self.publish()

    # ---- network (weather, calendars)

    def weather_fresh(self, ttl: timedelta = WEATHER_TTL) -> bool:
        if not wx.configured(self.settings):
            return True
        return self.forecast_at is not None and self.now() - self.forecast_at < ttl

    def calendars_fresh(self, ttl: timedelta = CALENDAR_TTL) -> bool:
        if not feed_urls(self.settings):
            return True
        st = self.calendars.feeds
        if set(st) != set(self.calendars.configured_sources()):
            return False
        return all(
            fs.last_attempt is not None and self.now() - fs.last_attempt < ttl
            for fs in st.values()
        )

    async def refresh_network(self, force: bool = False) -> bool:
        """Fetch weather and calendars if due. Returns True if anything changed."""
        async with self._net_lock:
            if not self.loaded:
                self.load()
            now = self.now()
            changed = False
            if wx.configured(self.settings) and (
                force or (not self.weather_fresh()
                          and (self._next_weather is None or now >= self._next_weather))
            ):
                try:
                    self.forecast = await wx.fetch_forecast(self.client, self.settings)
                    self.forecast_at, self.weather_error = now, None
                    self._next_weather = now + WEATHER_TTL
                    changed = True
                except wx.WeatherError as e:
                    self.weather_error = str(e)
                    self._next_weather = now + FAILURE_RETRY
                    log.warning("weather fetch failed (%s); keeping last forecast", e)
            elif not wx.configured(self.settings) and self.forecast is not None:
                self.forecast = self.forecast_at = None
                changed = True
            cal_due = self._next_calendars is None or now >= self._next_calendars
            if force or (not self.calendars_fresh() and cal_due):
                changed |= await self.calendars.refresh(self.client, now)
                failed = any(fs.last_error for fs in self.calendars.feeds.values())
                self._next_calendars = now + (FAILURE_RETRY if failed else CALENDAR_TTL)
            return changed

    # ---- database-touching work (triggered, rate-limited)

    async def refresh_db_parts(self, force: bool = False) -> None:
        async with self._db_lock:
            await self._load_reminders(force)
            await self._load_spend(force)
            await self._load_outfits(self.today(), force)
            self._bootstrapped = True
        await self.tag_upcoming()

    async def _load_reminders(self, force: bool) -> None:
        db = self.services.db
        if (self.reminders_loaded and not force) or not db.configured:
            return
        try:
            reminders, synced = await asyncio.wait_for(load_all(db), DB_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - DB down: keep the cache
            log.info("reminders not loaded (%s)", type(e).__name__)
            self.db_error = type(e).__name__
            return
        self.db_error = None
        self.reminders = reminders
        times = [_dt(v) for v in synced.values()]
        self.reminders_at = max((t for t in times if t), default=self.reminders_at)
        self.reminders_loaded = True

    async def _load_spend(self, force: bool) -> None:
        mono = _time.monotonic()
        if not force and self._spend_checked is not None and (
            mono - self._spend_checked < SPEND_RECHECK_S
        ):
            return
        if not self.services.db.configured:
            return
        self._spend_checked = mono
        try:
            st = await asyncio.wait_for(self.services.spend.status(), DB_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            log.info("spend mode not refreshed (%s)", type(e).__name__)
            self.db_error = type(e).__name__
            return
        self.db_error = None
        self.spend_mode, self.spend_known = st.mode, True

    async def _load_outfits(self, day: date, force: bool) -> None:
        key = day.isoformat()
        mono = _time.monotonic()
        last = self._outfits_checked.get(key)
        if not force and (self.outfits.get(key) or (
            last is not None and mono - last < OUTFITS_RECHECK_S
        )):
            return
        self._outfits_checked[key] = mono
        try:
            got = await asyncio.wait_for(
                self.outfits_provider(self.services, day), OUTFITS_TIMEOUT_S
            )
        except Exception as e:  # noqa: BLE001 - recommender failure = no new outfits
            log.warning("outfits unavailable (%s); keeping cached", type(e).__name__)
            self.db_error = type(e).__name__
            return
        outfits = []
        for o in got or []:
            try:
                o = o if isinstance(o, OutfitSuggestion) else OutfitSuggestion.model_validate(o)
            except Exception:  # noqa: BLE001
                continue
            if o.for_date == day:
                outfits.append(o)
        outfits.sort(key=lambda o: o.rank)
        self.outfits[key] = outfits
        if got is not None:
            self.outfits_at = self.now()

    async def tag_events(self, events: list[CalendarEvent]) -> None:
        """One LLM call for titles never rated, skipping titles tried within the hour."""
        mono = _time.monotonic()
        titles = []
        for t in self.formality.missing([e.title for e in events]):
            tried = self._tag_tried.get(title_hash(t))
            if tried is None or mono - tried >= TAG_RETRY_S:
                titles.append(t)
        if not titles or not getattr(self.services.llm, "configured", False):
            return
        for t in titles:
            self._tag_tried[title_hash(t)] = mono
        n = await tag_titles(self.services, self.formality, titles)
        if n:
            log.info("rated formality of %d event title(s)", n)

    async def tag_upcoming(self) -> None:
        day = self.today()
        await self.tag_events(
            self.calendars.events_between(day, day + timedelta(days=UPCOMING_DAYS))
        )

    # ---- assembly

    def weather_for(self, day: date) -> WeatherSnapshot | None:
        return wx.snapshot_for(self.forecast, day, self.now(), self.settings, self.forecast_at)

    def events_for(self, day: date) -> list[CalendarEvent]:
        return self.formality.apply(self.calendars.events_for_day(day))

    def build(self) -> TodayPayload:
        day = self.today()
        return TodayPayload(
            date=day,
            generated_at=self.now(),
            weather=self.weather_for(day),
            events=self.events_for(day),
            reminders=visible(self.reminders),
            outfits=self.outfits.get(day.isoformat(), []),
            last_sync=SyncTimes(
                weather=self.forecast_at,
                calendars=self.calendars.last_sync(),
                reminders=self.reminders_at,
                outfits=self.outfits_at,
            ),
            spend_mode=self.spend_mode,
            stale=self.stale_parts(),
        )

    def stale_parts(self) -> list[str]:
        """What the payload shows from an old copy: "weather" (forecast older than an
        hour), "calendars" (a feed failing or older than its limit), "database" (the last
        read of reminders, outfits or spend mode failed)."""
        out = []
        if wx.configured(self.settings) and not self.weather_fresh(2 * WEATHER_TTL):
            out.append("weather")
        if self.calendars.stale_sources(self.now()):
            out.append("calendars")
        if self.db_error and self.services.db.configured:
            out.append("database")
        return out

    @staticmethod
    def _comparable(p: TodayPayload | None) -> dict | None:
        return None if p is None else p.model_dump(mode="json", exclude={"generated_at"})

    async def publish(self) -> TodayPayload:
        """Rebuild; if anything but generated_at changed, store, save and broadcast."""
        new = self.build()
        if self._comparable(new) == self._comparable(self.payload):
            return self.payload  # type: ignore[return-value]
        self.payload = new
        self.save()
        await self._broadcast(new)
        return new

    async def _broadcast(self, payload: TodayPayload) -> None:
        hub = getattr(self.services, "hub", None)
        if hub is not None:
            linked = await with_links(self.services, payload)
            await hub.broadcast("today.updated", linked.model_dump(mode="json"))

    def current(self) -> TodayPayload:
        """For GET /api/today: memory only, rebuilt from caches after midnight."""
        if not self.loaded:
            self.load()
        if self.payload is None or self.payload.date != self.today():
            self.payload = self.build()
            self.save()
            self.spawn(self._broadcast(self.payload))
        if not self._bootstrapped and self.services.db.configured:
            self._bootstrapped = True  # once per process
            self.spawn(self.refresh())
        return self.payload

    async def refresh(self, force: bool = False) -> TodayPayload:
        await self.refresh_network(force)
        await self.refresh_db_parts(force)
        return await self.publish()

    def status(self) -> dict[str, Any]:
        now = self.now()
        return {
            "date": self.today().isoformat(),
            "weather": {
                "configured": wx.configured(self.settings),
                "last_success": _iso(self.forecast_at),
                "last_error": self.weather_error,
                "stale": wx.configured(self.settings) and not self.weather_fresh(2 * WEATHER_TTL),
            },
            "calendars": self.calendars.status(now),
            "reminders": {"last_push": _iso(self.reminders_at), "count": len(self.reminders)},
            "outfits": {"last_sync": _iso(self.outfits_at),
                        "today": len(self.outfits.get(self.today().isoformat(), []))},
            "formality": {"cached_titles": len(self.formality),
                          "llm_configured": bool(getattr(self.services.llm, "configured", False))},
            "spend_mode": self.spend_mode,
        }


# ---- module-level API ------------------------------------------------------------------


async def with_links(services: Services, payload: TodayPayload) -> TodayPayload:
    """The payload with short-lived collage links. Presigning is a local signature (no
    network, works offline); links are never stored in the cache because they expire."""
    storage = getattr(services, "storage", None)
    if storage is None or not any(o.collage_key for o in payload.outfits):
        return payload
    outfits = []
    for o in payload.outfits:
        url = None
        if o.collage_key:
            try:
                url = await storage.presign_get(o.collage_key, expires_in=COLLAGE_LINK_TTL_S)
            except Exception as e:  # noqa: BLE001 - a missing link is not worth a failure
                log.info("collage link not made (%s)", type(e).__name__)
        outfits.append(o.model_copy(update={"collage_url": url}))
    return payload.model_copy(update={"outfits": outfits})


def get_state(services: Services) -> IntegrationsState:
    state = getattr(services, STATE_ATTR, None)
    if state is None:
        state = IntegrationsState(services)
        setattr(services, STATE_ATTR, state)
    return state


async def start(services: Services, *, timers: bool = True) -> IntegrationsState:
    state = get_state(services)
    await state.start(timers=timers)
    return state


async def stop(services: Services) -> None:
    state = getattr(services, STATE_ATTR, None)
    if state is not None:
        await state.stop()


def current_payload(services: Services) -> TodayPayload:
    return get_state(services).current()


async def refresh_if_stale(services: Services, *, force: bool = False) -> TodayPayload:
    """For the presence sensor and the phone. HTTP refreshes when due; database reads
    only when their rate limits allow; broadcasts today.updated on change. Never raises
    for a missing or failing integration."""
    return await get_state(services).refresh(force)


async def invalidate_outfits(services: Services, for_date: date | None = None) -> None:
    """Call after storing new outfits. Re-reads them through recommender.today.get_outfits
    (and the spend mode, since outfits usually cost money) and broadcasts the change."""
    state = get_state(services)
    day = for_date or state.today()
    async with state._db_lock:
        await state._load_outfits(day, force=True)
        await state._load_spend(force=True)
    await state.publish()


async def reminders_changed(
    services: Services, reminders: list[Reminder], synced_at: datetime
) -> TodayPayload:
    state = get_state(services)
    state.reminders = reminders
    state.reminders_at = synced_at
    state.reminders_loaded = True
    return await state.publish()

