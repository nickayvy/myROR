"""Spend ledger and cap (HANDOFF U31, section 15).

- `live` ledger: current calendar month in the configured timezone, against LIVE_CAP_USD.
- `dev` ledger: all time, against DEV_CAP_USD.
- mode: normal < 80% <= degraded < 100% <= blocked.

Every paid call does `await spend.check(purpose)` before calling out, and
`await spend.record(...)` before returning its result. In degraded mode callers
should prefer cached results (e.g. rerolls reuse outfits); in blocked mode
`check` raises SpendCapExceeded and callers fall back (rules-only outfits,
queued check-ins).

Known limit: check-then-call is not atomic, so two concurrent calls at 99% can
both proceed. With one user and a couple of calls a day that overshoot is cents.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from db import Database
from mirror_api.contracts import SpendMode

Ledger = Literal["live", "dev"]
DEGRADED_AT = 80.0
BLOCKED_AT = 100.0


class SpendCapExceeded(RuntimeError):
    def __init__(self, purpose: str, status: SpendStatus) -> None:
        super().__init__(
            f"spend cap reached for {status.ledger} ledger "
            f"(${status.spent_usd:.2f} of ${status.cap_usd:.2f}); refused: {purpose}"
        )
        self.purpose = purpose
        self.status = status


class SpendStatus(BaseModel):
    ledger: Ledger
    cap_usd: float
    spent_usd: float
    percent: float
    mode: SpendMode
    period_start: datetime | None  # None for the all-time dev ledger
    period_end: datetime | None


def mode_for(percent: float) -> SpendMode:
    if percent >= BLOCKED_AT:
        return "blocked"
    if percent >= DEGRADED_AT:
        return "degraded"
    return "normal"


def month_bounds(now: datetime, tz) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month containing `now`, in `tz`, as aware datetimes."""
    local = now.astimezone(tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    # replace() keeps the old UTC offset; re-resolve so DST months are exact.
    start = datetime(start.year, start.month, 1, tzinfo=tz)
    end = datetime(end.year, end.month, 1, tzinfo=tz)
    return start, end


class SpendLedger:
    def __init__(
        self,
        db: Database,
        *,
        ledger: Ledger,
        live_cap_usd: float,
        dev_cap_usd: float,
        tz,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.ledger: Ledger = ledger
        self.caps = {"live": live_cap_usd, "dev": dev_cap_usd}
        self.tz = tz
        self.clock = clock

    @classmethod
    def from_settings(cls, db: Database, settings, **kw: Any) -> SpendLedger:
        return cls(
            db,
            ledger=settings.spend_ledger,
            live_cap_usd=settings.live_cap_usd,
            dev_cap_usd=settings.dev_cap_usd,
            tz=settings.tz,
            **kw,
        )

    async def status(self, ledger: Ledger | None = None) -> SpendStatus:
        ledger = ledger or self.ledger
        cap = self.caps[ledger]
        if ledger == "live":
            start, end = month_bounds(self.clock(), self.tz)
            spent = await self.db.fetchval(
                "SELECT coalesce(sum(est_usd), 0) FROM api_spend"
                " WHERE ledger = 'live' AND at >= %s AND at < %s",
                (start, end),
            )
        else:
            start = end = None
            spent = await self.db.fetchval(
                "SELECT coalesce(sum(est_usd), 0) FROM api_spend WHERE ledger = 'dev'"
            )
        spent_f = float(spent)
        percent = 100.0 if cap <= 0 else spent_f / cap * 100.0
        return SpendStatus(
            ledger=ledger,
            cap_usd=cap,
            spent_usd=round(spent_f, 6),
            percent=round(percent, 2),
            mode=mode_for(percent),
            period_start=start,
            period_end=end,
        )

    async def check(self, purpose: str) -> SpendStatus:
        """Raise SpendCapExceeded when blocked; otherwise return the status."""
        st = await self.status()
        if st.mode == "blocked":
            raise SpendCapExceeded(purpose, st)
        return st

    async def record(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        est_usd: Decimal | float,
        units: dict[str, Any] | None = None,
        job_id: UUID | None = None,
    ) -> None:
        """Write one ledger row. Call this before handing a paid result to anyone."""
        await self.db.execute(
            "INSERT INTO api_spend (at, ledger, provider, model, purpose, units, est_usd, job_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self.clock(),
                self.ledger,
                provider,
                model,
                purpose,
                Jsonb(units or {}),
                Decimal(str(est_usd)),
                job_id,
            ),
        )
