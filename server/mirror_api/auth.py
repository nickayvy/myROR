"""Device pairing and tokens (HANDOFF sections 11 and 14).

Flow: the mirror (localhost) asks for a one-time code and shows it as a QR code;
the phone scans it and POSTs the code to /api/pair, receiving a long random token.
Only sha256(token) is stored. Every other route needs `Authorization: Bearer <token>`,
except requests from the Pi itself (the mirror), which are trusted as `mirror`.

Codes live in memory only: 8 characters, 5 minutes, single use, one active at a
time; 10 wrong guesses burn the active code.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from db import Database

log = logging.getLogger(__name__)

DeviceKind = Literal["phone", "mirror", "shortcut"]
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CODE_LEN = 8


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class Device(BaseModel):
    id: UUID
    name: str
    kind: DeviceKind
    paired_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class Principal:
    """Who is calling. kind='mirror' with device=None means a localhost request."""

    kind: Literal["device", "mirror"]
    device: Device | None = None

    @property
    def device_id(self) -> UUID | None:
        return self.device.id if self.device else None


class PairingError(Exception):
    pass


@dataclass
class _Code:
    value: str
    expires_at: float


class PairingCodes:
    def __init__(
        self,
        ttl_s: float = 300.0,
        max_failures: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_failures = max_failures
        self.clock = clock
        self._active: _Code | None = None
        self._failures = 0

    def issue(self) -> tuple[str, float]:
        """New code; any previous code stops working. Returns (code, ttl seconds)."""
        value = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        self._active = _Code(value, self.clock() + self.ttl_s)
        self._failures = 0
        return value, self.ttl_s

    def redeem(self, code: str) -> bool:
        active = self._active
        given = normalize_code(code)
        if active is None or self.clock() > active.expires_at:
            self._active = None
            return False
        if hmac.compare_digest(active.value.encode(), given.encode()):
            self._active = None  # single use
            return True
        self._failures += 1
        if self._failures >= self.max_failures:
            self._active = None
        return False


class AuthUnavailable(RuntimeError):
    """The device list is unknown: the database is unreachable and no local copy exists."""


DEVICE_COLUMNS = "id, name, kind, paired_at, last_seen_at, revoked_at"


class Auth:
    """Device tokens.

    The unrevoked devices (token hashes only) are held in memory and re-read from the
    database at most every `cache_ttl_s` while requests arrive. Each re-read waits at most
    `db_timeout_s`. If it fails, the last known list is used and the database is not tried
    again for `retry_after_s`, so with the database down a request never blocks for more
    than `db_timeout_s`, and usually not at all. The list is also written to
    `snapshot_path` (mode 0600; hashes, ids, names and kinds, never tokens) so it survives
    a restart during an outage. Pairing, minting and revoking update it immediately;
    a revocation made directly in the database takes effect within `cache_ttl_s` once the
    database is reachable.
    """

    def __init__(
        self,
        db: Database,
        *,
        codes: PairingCodes | None = None,
        cache_ttl_s: float = 60.0,
        seen_every_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        snapshot_path: Path | None = None,
        db_timeout_s: float = 2.0,
        retry_after_s: float = 30.0,
    ) -> None:
        self.db = db
        self.codes = codes or PairingCodes()
        self.cache_ttl_s = cache_ttl_s
        self.seen_every_s = seen_every_s
        self.clock = clock
        self.snapshot_path = snapshot_path
        self.db_timeout_s = db_timeout_s
        self.retry_after_s = retry_after_s
        self._devices: dict[str, Device] | None = None  # token hash -> unrevoked device
        self._loaded_at: float | None = None
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()
        self._seen: dict[UUID, float] = {}
        self._background: set[asyncio.Task] = set()

    @classmethod
    def from_settings(cls, db: Database, settings) -> Auth:
        return cls(db, snapshot_path=settings.cache_dir / "auth" / "devices.json",
                   db_timeout_s=settings.auth_db_timeout_s,
                   retry_after_s=settings.auth_retry_after_s)

    @property
    def degraded(self) -> bool:
        """True while serving the last known device list because the database failed."""
        return self._failed_at is not None

    async def pair(self, code: str, name: str, kind: DeviceKind = "phone") -> tuple[Device, str]:
        if not self.codes.redeem(code):
            raise PairingError("invalid or expired pairing code")
        return await self._insert(name, kind)

    async def issue_token(self, name: str, kind: DeviceKind) -> tuple[Device, str]:
        """A new device and token without a pairing code. Callers must already be
        authorised (e.g. a paired phone minting a token for the Reminders Shortcut)."""
        return await self._insert(name, kind)

    async def _insert(self, name: str, kind: DeviceKind) -> tuple[Device, str]:
        token = secrets.token_urlsafe(32)
        digest = hash_token(token)
        row = await self.db.fetchrow(
            "INSERT INTO devices (name, kind, token_hash) VALUES (%s, %s, %s)"
            f" RETURNING {DEVICE_COLUMNS}",
            (name, kind, digest),
        )
        device = Device(**row)
        if self._devices is not None:
            self._devices[digest] = device
            self._save_snapshot()
        return device, token

    async def authenticate(self, token: str) -> Device | None:
        """The active device for this token, or None. Raises AuthUnavailable only when the
        database is unreachable and there is no last known device list at all."""
        if not token:
            return None
        digest = hash_token(token)
        devices = await self._device_list()
        device = None
        for h, d in devices.items():
            if hmac.compare_digest(h, digest):
                device = d
                break
        if device is None:
            return None
        self._touch(device.id)
        return device

    async def _device_list(self) -> dict[str, Device]:
        now = self.clock()
        if self._fresh(now):
            return self._devices  # type: ignore[return-value]
        if self._backing_off(now):
            return self._devices  # type: ignore[return-value]
        async with self._lock:
            now = self.clock()
            if self._fresh(now) or self._backing_off(now):
                return self._devices  # type: ignore[return-value]
            try:
                rows = await asyncio.wait_for(
                    self.db.fetch(f"SELECT {DEVICE_COLUMNS}, token_hash FROM devices"
                                  " WHERE revoked_at IS NULL"),
                    self.db_timeout_s)
            except Exception as e:  # noqa: BLE001 - unreachable, timed out, not configured
                self._failed_at = self.clock()
                if self._devices is None:
                    self._devices = self._load_snapshot()
                log.warning("device list not refreshed (%s); using the last known list (%s)",
                            type(e).__name__, "none" if self._devices is None
                            else f"{len(self._devices)} device(s)")
                if self._devices is None:
                    raise AuthUnavailable("database unreachable and no device list") from e
                return self._devices
            self._devices = {r.pop("token_hash"): Device(**r) for r in rows}
            self._loaded_at, self._failed_at = self.clock(), None
            self._save_snapshot()
            return self._devices

    def _fresh(self, now: float) -> bool:
        return (self._devices is not None and self._loaded_at is not None
                and now - self._loaded_at < self.cache_ttl_s)

    def _backing_off(self, now: float) -> bool:
        return (self._devices is not None and self._failed_at is not None
                and now - self._failed_at < self.retry_after_s)

    # -- local copy

    def _load_snapshot(self) -> dict[str, Device] | None:
        if self.snapshot_path is None:
            return None
        try:
            data = json.loads(self.snapshot_path.read_text())
            return {d.pop("token_hash"): Device(**d) for d in data["devices"]}
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001 - a bad file is the same as no file
            log.warning("ignoring device snapshot (%s)", type(e).__name__)
            return None

    def _save_snapshot(self) -> None:
        if self.snapshot_path is None or self._devices is None:
            return
        body = {"v": 1, "devices": [{**d.model_dump(mode="json"), "token_hash": h}
                                    for h, d in self._devices.items()]}
        path = self.snapshot_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(body, f)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("could not write device snapshot (%s)", type(e).__name__)

    # -- last seen (best effort, never on the request path)

    def _touch(self, device_id: UUID) -> None:
        now = self.clock()
        last = self._seen.get(device_id)
        if last is not None and now - last < self.seen_every_s:
            return
        if self._failed_at is not None:
            return  # the database is down; try again on a later request
        self._seen[device_id] = now

        async def write() -> None:
            try:
                await asyncio.wait_for(self.db.execute(
                    "UPDATE devices SET last_seen_at = now() WHERE id = %s", (device_id,)),
                    self.db_timeout_s * 5)
            except Exception as e:  # noqa: BLE001
                self._seen.pop(device_id, None)
                log.info("last_seen not recorded (%s)", type(e).__name__)

        try:
            task = asyncio.get_running_loop().create_task(write())
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close(self) -> None:
        for t in list(self._background):
            t.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    async def list_devices(self, include_revoked: bool = False) -> list[Device]:
        sql = f"SELECT {DEVICE_COLUMNS} FROM devices"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        rows = await self.db.fetch(sql + " ORDER BY paired_at")
        return [Device(**r) for r in rows]

    async def revoke(self, device_id: UUID) -> bool:
        n = await self.db.execute(
            "UPDATE devices SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
            (device_id,),
        )
        if self._devices is not None:
            self._devices = {h: d for h, d in self._devices.items() if d.id != device_id}
            self._save_snapshot()
        self._seen.pop(device_id, None)
        return n > 0
