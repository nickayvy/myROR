"""Shared data contracts. Integrations produce these, the recommender consumes them,
and /api/today serialises TodayPayload for the mirror and the phone.

Conventions: temperatures are stored in Celsius (`*_c`); conversion happens only at
display time via `display_unit`. Datetimes are timezone-aware. Probabilities are 0..1.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TempUnit = Literal["F", "C"]
SpendMode = Literal["normal", "degraded", "blocked"]
Formality = Annotated[int, Field(ge=1, le=5)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]

WeatherCondition = Literal[
    "clear",
    "partly_cloudy",
    "cloudy",
    "fog",
    "drizzle",
    "rain",
    "snow",
    "sleet",
    "thunderstorm",
    "unknown",
]

# Where an item sits in an outfit. Extend here (one place) if the taxonomy needs more.
OutfitSlot = Literal["top", "bottom", "one_piece", "outer", "shoes", "accessory"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


class HourlyWeather(_Model):
    time: AwareDatetime
    temp_c: float
    precip_probability: Probability | None = None
    condition: WeatherCondition = "unknown"


class WeatherSnapshot(_Model):
    fetched_at: AwareDatetime
    temp_c: float
    feels_like_c: float | None = None
    high_c: float | None = None
    low_c: float | None = None
    precip_probability: Probability | None = None  # max chance of precipitation today
    precip_mm: float | None = Field(default=None, ge=0)  # expected total today
    condition: WeatherCondition = "unknown"
    hourly: list[HourlyWeather] = Field(default_factory=list)  # rest of today, coarse
    summary: str | None = None  # one line for the mirror, e.g. "Rain after 3 pm"
    display_unit: TempUnit = "F"

    def display(self, celsius: float | None) -> int | None:
        """A temperature in the display unit, rounded for the screen."""
        if celsius is None:
            return None
        return round(c_to_f(celsius) if self.display_unit == "F" else celsius)

    @property
    def is_wet(self) -> bool:
        return self.condition in ("drizzle", "rain", "sleet", "snow", "thunderstorm") or (
            self.precip_probability is not None and self.precip_probability >= 0.5
        )


class CalendarEvent(_Model):
    uid: str
    source: Literal["google", "outlook", "icloud"]
    title: str
    start: AwareDatetime
    end: AwareDatetime | None = None
    all_day: bool = False
    location: str | None = None
    formality: Formality | None = None  # set by the formality tagger; None = unknown


class Reminder(_Model):
    id: str
    title: str
    due: AwareDatetime | None = None
    list: str | None = None
    completed: bool = False


class DayContext(_Model):
    """Everything the recommender needs to know about the day, apart from the closet."""

    date: date
    weather: WeatherSnapshot | None = None
    events: list[CalendarEvent] = Field(default_factory=list)
    min_formality: Formality = 1  # highest event formality; 1 when no events


class OutfitItem(_Model):
    item_id: UUID
    slot: OutfitSlot


class OutfitSuggestion(_Model):
    id: UUID | None = None  # outfits.id once stored
    for_date: date
    rank: int = Field(default=1, ge=1)
    items: list[OutfitItem] = Field(min_length=1)
    reasoning: str
    source: Literal["llm", "rules"]
    collage_key: str | None = None
    # Short-lived presigned link to the collage, added when served (GET /api/today and
    # today.updated); never stored.
    collage_url: str | None = None


class SyncTimes(_Model):
    weather: AwareDatetime | None = None
    calendars: AwareDatetime | None = None
    reminders: AwareDatetime | None = None
    outfits: AwareDatetime | None = None


class TodayPayload(_Model):
    date: date
    generated_at: AwareDatetime
    weather: WeatherSnapshot | None = None
    events: list[CalendarEvent] = Field(default_factory=list)
    reminders: list[Reminder] = Field(default_factory=list)
    outfits: list[OutfitSuggestion] = Field(default_factory=list)
    last_sync: SyncTimes = Field(default_factory=SyncTimes)
    spend_mode: SpendMode = "normal"
    # Parts shown from an old copy: any of "weather", "calendars", "database".
    stale: list[Literal["weather", "calendars", "database"]] = Field(default_factory=list)


class WsEvent(_Model):
    """Envelope for every WebSocket message: {"type": "...", "data": {...}, "at": ...}."""

    type: str
    data: dict | list | str | int | float | bool | None = None
    at: datetime
