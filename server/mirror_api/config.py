"""Settings, read from the repo-root `.env` (names exactly as `.env.example`).

Nothing here raises at import time. An empty value means "not configured": the
feature reports itself as unavailable in /api/health instead of crashing.
"""

from __future__ import annotations

import re
from datetime import time
from functools import cached_property
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
ENV_FILE = REPO_ROOT / ".env"

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


def parse_hhmm(value: str) -> time:
    """'06:00' or '06:00:30' -> time. Raises ValueError on anything else."""
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"expected HH:MM or HH:MM:SS, got {value!r}")
    return time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["dev", "prod"] = "dev"
    spend_ledger: Literal["dev", "live"] = "dev"
    live_cap_usd: float = 5.00
    dev_cap_usd: float = 10.00

    database_url: str = ""

    storage_backend: Literal["s3", "local"] = "s3"
    storage_endpoint: str = ""
    storage_region: str = "us-east-2"
    storage_bucket: str = "mirror-images"
    storage_access_key_id: str = ""
    storage_secret_access_key: str = ""
    local_data_dir: Path = Path("./data")

    fal_key: str = ""
    anthropic_api_key: str = ""
    # fake: answers {} to everything (fallback paths only). scripted: a deterministic fake
    # that tags synthetic garments, finds them in mirror photos, composes outfits and rates
    # events (providers/llm/scripted.py), so dev and e2e runs exercise the LLM paths.
    llm: Literal["claude", "fake", "scripted"] = "claude"
    llm_tag_model: str = "claude-haiku-5-5"
    llm_outfit_model: str = "claude-sonnet-5-5"
    bg_remover: Literal["fal", "fake"] = "fal"
    segmenter: Literal["fal", "fake"] = "fal"
    embedder: Literal["fashionclip", "fake"] = "fashionclip"

    # Closet pipeline (pipeline/, providers/fal|bg_remover|segmenter|embedder).
    # FAL_INPUT_MODE=presign sends fal a short-lived GET link to the private bucket;
    # data_uri sends the image inline as base64. Neither uploads to fal's CDN.
    fal_input_mode: Literal["presign", "data_uri"] = "presign"
    fal_bg_endpoint: str = "fal-ai/birefnet/v2"
    fal_bg_model: str = "General Use (Light)"  # BiRefNet only
    fal_bg_resolution: str = "1024x1024"  # BiRefNet only
    fal_seg_endpoint: str = "fal-ai/sam-3/image"
    llm_checkin_model: str = "claude-haiku-5-5"
    embedder_model: str = "hf-hub:Marqo/marqo-fashionCLIP"

    timezone: str = ""
    weather_lat: float | None = None
    weather_lon: float | None = None
    temp_unit: Literal["F", "C"] = "F"
    precompute_time: str = "06:00"

    cal_google_ics_url: str = ""
    cal_outlook_ics_url: str = ""
    cal_icloud_ics_url: str = ""

    hardware: Literal["mock", "pi"] = "mock"

    # FACE_BLUR=on|off (default off, U37). On: photos.storage_key is the blurred copy
    # and photos.original_key the original. Off: storage_key is the original,
    # face_blurred=false, original_key null.
    face_blur: bool = False

    # Not in .env.example on purpose: internal knobs with safe defaults.
    db_pool_max_size: int = Field(default=5, ge=1)
    db_pool_max_idle_s: float = Field(default=60.0, gt=0)
    # Neon is remote and scales to zero: bound every wait. connect: libpq connect_timeout;
    # acquire: how long a caller waits for a pooled connection; reconnect: how long the
    # pool keeps retrying a failed connect before giving up (a short value means the first
    # request after an outage connects at once instead of waiting out a backoff).
    db_connect_timeout_s: int = Field(default=10, ge=1)
    db_acquire_timeout_s: float = Field(default=10.0, gt=0)
    db_reconnect_timeout_s: float = Field(default=2.0, gt=0)
    # Token checks: how long a request may wait for the database, and how long to serve
    # the last known device list without retrying after the database failed.
    auth_db_timeout_s: float = Field(default=2.0, gt=0)
    auth_retry_after_s: float = Field(default=30.0, ge=0)
    # Built phone app (React + Vite). Served at / when the directory exists. Relative
    # paths are resolved against the repository root.
    phone_dist_dir: Path = Path("phone/dist")
    # Presence sensor debounce: an arrival must hold this long, a departure longer.
    presence_arrive_debounce_s: float = Field(default=0.5, ge=0)
    presence_leave_debounce_s: float = Field(default=10.0, ge=0)
    job_concurrency: int = Field(default=2, ge=1)
    fal_timeout_s: float = Field(default=60.0, gt=0)
    fal_seg_timeout_s: float = Field(default=120.0, gt=0)  # SAM 3 cold start was 21 s
    fal_presign_ttl_s: int = Field(default=300, ge=30, le=3600)
    upload_max_mb: float = Field(default=25.0, gt=0)

    @field_validator("weather_lat", "weather_lon", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v == "" else v

    @field_validator("precompute_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    # ---- derived -------------------------------------------------------------

    @cached_property
    def tz(self) -> ZoneInfo:
        """Configured timezone; UTC when TIMEZONE is empty or unknown (health says so)."""
        try:
            return ZoneInfo(self.timezone) if self.timezone else ZoneInfo("UTC")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @property
    def timezone_configured(self) -> bool:
        if not self.timezone:
            return False
        try:
            ZoneInfo(self.timezone)
            return True
        except (ZoneInfoNotFoundError, ValueError):
            return False

    @property
    def data_dir(self) -> Path:
        p = self.local_data_dir
        return p if p.is_absolute() else (SERVER_DIR / p).resolve()

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def phone_dist(self) -> Path:
        p = self.phone_dist_dir
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def dev_controls(self) -> bool:
        """Dev-only routes that drive the mock hardware from outside the process."""
        return self.app_env == "dev" and self.hardware == "mock"

    @property
    def db_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def storage_configured(self) -> bool:
        if self.storage_backend == "local":
            return True
        return bool(
            self.storage_endpoint
            and self.storage_bucket
            and self.storage_access_key_id
            and self.storage_secret_access_key
        )

    @property
    def spend_cap_usd(self) -> float:
        return self.live_cap_usd if self.spend_ledger == "live" else self.dev_cap_usd

    def providers_configured(self) -> dict[str, bool | dict[str, bool]]:
        """Booleans only. Never put key material in here."""
        return {
            "llm": self.llm in ("fake", "scripted") or bool(self.anthropic_api_key),
            "fal": bool(self.fal_key),
            "weather": self.weather_lat is not None and self.weather_lon is not None,
            "timezone": self.timezone_configured,
            "calendars": {
                "google": bool(self.cal_google_ics_url),
                "outlook": bool(self.cal_outlook_ics_url),
                "icloud": bool(self.cal_icloud_ics_url),
            },
        }


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
