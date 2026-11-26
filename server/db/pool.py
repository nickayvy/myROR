"""Async Postgres access for Neon.

Neon specifics:
- DATABASE_URL points at a pgbouncer pooler in transaction mode, so server-side
  prepared statements are disabled (prepare_threshold=None).
- The free plan scales compute to zero when idle. The pool keeps min_size=0 and
  closes idle connections after `max_idle` seconds, and nothing in this module
  touches the database unless a caller asks it to. The pool's own maintenance is
  local timers only (no health-check queries).
"""

from __future__ import annotations

import logging
import select
import time
import weakref
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from pgvector.psycopg.vector import register_vector_info
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types import TypeInfo
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

Params = Sequence[Any] | dict[str, Any] | None
CHECK_AFTER_IDLE_S = 30.0


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL is empty."""


class Database:
    """Thin wrapper over an AsyncConnectionPool with dict rows.

    Use the helpers for one-shot statements, or `async with db.connection() as conn`
    / `async with db.transaction() as conn` for several statements.
    Placeholders are psycopg style: %s or %(name)s.

    Embeddings: pass a numpy float32 array (or pgvector.Vector) for vector(512)
    columns. Pass plain lists for real[] columns (Lab colours): numpy arrays are
    always dumped as `vector`.
    """

    def __init__(
        self,
        url: str,
        *,
        max_size: int = 5,
        max_idle: float = 60.0,
        connect_timeout_s: int = 10,
        acquire_timeout_s: float = 10.0,
        reconnect_timeout_s: float = 2.0,
    ) -> None:
        self.url = url
        self._max_size = max_size
        self._max_idle = max_idle
        # Every wait is bounded (Neon is remote and scales to zero). A short reconnect
        # timeout makes the pool give up on a failed connect quickly, so the next caller
        # starts a fresh attempt instead of waiting out psycopg_pool's growing backoff
        # (that backoff is what made the first request after an outage take ~20 s).
        self._connect_timeout = connect_timeout_s
        self._acquire_timeout = acquire_timeout_s
        self._reconnect_timeout = reconnect_timeout_s
        self._pool: AsyncConnectionPool | None = None
        self._vector_info: TypeInfo | None = None
        # Callbacks to run when a checked-out connection is returned (see after_release).
        self._after_release: dict[int, list[Callable[[], None]]] = {}
        # Connections opened before the vector type existed (see _configure).
        self._unadapted: weakref.WeakSet[AsyncConnection] = weakref.WeakSet()
        # When each pooled connection was last returned (see _check).
        self._released_at: weakref.WeakKeyDictionary[AsyncConnection, float] = \
            weakref.WeakKeyDictionary()
        # Counts checkouts; tests use it to prove nothing touches the DB while idle.
        self.checkouts = 0

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def open(self) -> None:
        if not self.configured or self._pool is not None:
            return
        self._pool = AsyncConnectionPool(
            self.url,
            min_size=0,
            max_size=self._max_size,
            max_idle=self._max_idle,
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row,
                    "connect_timeout": self._connect_timeout},
            configure=self._configure,
            check=self._check,
            open=False,
            name="mirror",
            timeout=self._acquire_timeout,
            reconnect_timeout=self._reconnect_timeout,
        )
        # wait=False: with min_size=0 there is nothing to wait for, and opening must
        # not contact the database (the mirror must boot while offline).
        await self._pool.open(wait=False)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _configure(self, conn: AsyncConnection) -> None:
        # Fetch the vector OID once per process, not once per connection.
        if self._vector_info is None:
            info = await TypeInfo.fetch(conn, "vector")
            if info is None:
                # Before migrations (a fresh stack): retried at checkout until it exists.
                if not self._unadapted:
                    log.info("pgvector extension not installed yet; vector columns unadapted")
                self._unadapted.add(conn)
                return
            self._vector_info = info
        register_vector_info(conn, self._vector_info)
        self._unadapted.discard(conn)

    async def _check(self, conn: AsyncConnection) -> None:
        """At checkout. A connection the server closed is caught by a socket check that
        costs nothing. One idle for CHECK_AFTER_IDLE_S or more (it may have died silently:
        network change, NAT timeout) also gets an empty query. Either failure makes the pool
        discard it and connect afresh. Runs only on checkout, never on a timer."""
        # Free check first: an idle connection has nothing to read (we never LISTEN), so a
        # readable socket means the server sent a termination notice or closed it (database
        # restarted, backend terminated).
        try:
            readable, _, _ = select.select([conn.fileno()], [], [], 0)
        except (OSError, ValueError):
            readable = [True]
        if readable:
            # Close it first: the pool only discards a connection that is not usable;
            # returned in a good state it would be handed out again.
            await conn.close()
            raise psycopg.OperationalError("connection closed by the server while idle")
        last = self._released_at.get(conn)
        if last is not None and time.monotonic() - last < CHECK_AFTER_IDLE_S:
            return
        await AsyncConnectionPool.check_connection(conn)

    def _require_pool(self) -> AsyncConnectionPool:
        if not self.configured:
            raise DatabaseNotConfigured("DATABASE_URL is not set")
        if self._pool is None:
            raise RuntimeError("Database.open() has not been called")
        return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Autocommit connection: each statement commits on its own."""
        pool = self._require_pool()
        self.checkouts += 1
        async with pool.connection() as conn:
            if conn in self._unadapted:
                await self._configure(conn)
            self._after_release[id(conn)] = []
            try:
                yield conn
            finally:
                callbacks = self._after_release.pop(id(conn), [])
                self._released_at[conn] = time.monotonic()
        for fn in callbacks:
            try:
                fn()
            except Exception:  # noqa: BLE001 - a callback must not mask the caller's result
                log.exception("after_release callback failed")

    def after_release(self, conn: AsyncConnection, fn: Callable[[], None]) -> None:
        """Run `fn` once `conn` (checked out from this Database) is returned, i.e. after
        the transaction around it has committed or rolled back. Used to wake the job worker
        only once an enqueued row is visible. For a connection this Database did not hand
        out, `fn` runs at once."""
        pending = self._after_release.get(id(conn))
        if pending is None:
            fn()
        else:
            pending.append(fn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        """Connection inside one transaction; commits on exit, rolls back on error."""
        async with self.connection() as conn, conn.transaction():
            yield conn

    async def execute(self, sql: str, params: Params = None) -> int:
        async with self.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def fetch(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def fetchrow(self, sql: str, params: Params = None) -> dict[str, Any] | None:
        async with self.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def fetchval(self, sql: str, params: Params = None) -> Any:
        row = await self.fetchrow(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))
