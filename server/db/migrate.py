"""Apply db/migrations/*.sql in order. `python -m db.migrate [--dry-run]`.

Each file runs in its own transaction and its stem (e.g. `0001_init`) is recorded
in schema_migrations. Files already recorded are skipped, so running this against
production, where 0001 is recorded, is a no-op until a new file is added.

Rules for new migrations: never edit an applied file; add `NNNN_description.sql`
with the next number. A file may contain several statements (no parameters).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg

log = logging.getLogger("db.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Same DDL as in 0001, so the runner can read the table before 0001 has run.
_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""
# Arbitrary constant: serialises concurrent runners. Transaction-scoped, so it
# works through a transaction-mode pooler.
_LOCK_KEY = 0x4D59524F52  # "MYROR"


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    files = sorted(migrations_dir.glob("*.sql"))
    stems = [f.stem for f in files]
    if len(set(stems)) != len(stems):
        raise RuntimeError("duplicate migration names")
    return files


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.transaction():
        conn.execute(_BOOTSTRAP)
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def apply_migrations(
    database_url: str, migrations_dir: Path = MIGRATIONS_DIR, *, dry_run: bool = False
) -> list[str]:
    """Apply pending migrations; return the versions applied (or pending, if dry_run)."""
    files = discover(migrations_dir)
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True, prepare_threshold=None) as conn:
        done = applied_versions(conn)
        pending = [f for f in files if f.stem not in done]
        if dry_run:
            return [f.stem for f in pending]
        for f in pending:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                # Re-check under the lock in case another runner got here first.
                row = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (f.stem,)
                ).fetchone()
                if row:
                    continue
                log.info("applying %s", f.name)
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
                applied.append(f.stem)
    return applied


def main(argv: list[str] | None = None) -> int:
    from mirror_api.config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list pending migrations only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    url = Settings().database_url
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    result = apply_migrations(url, dry_run=args.dry_run)
    verb = "pending" if args.dry_run else "applied"
    print(f"{verb}: {', '.join(result) if result else 'nothing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
