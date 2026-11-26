"""Migration runner: fresh apply, no-op re-run, production-style no-op, ordering."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from db.migrate import MIGRATIONS_DIR, apply_migrations, discover

ALL = [p.stem for p in discover()]


@pytest.fixture
def scratch_url(admin_url):
    name = f"mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def versions(url):
    with psycopg.connect(url) as conn:
        return [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]


def test_all_files_discovered_in_order():
    assert ALL == sorted(ALL) and ALL[0] == "0001_init" and len(ALL) >= 1
    assert all(p.parent == MIGRATIONS_DIR for p in discover())


def test_fresh_database_applies_everything_once(scratch_url):
    assert apply_migrations(scratch_url) == ALL
    assert versions(scratch_url) == ALL
    with psycopg.connect(scratch_url) as conn:
        n = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
        ext = conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
    assert n >= 15 and ext
    assert apply_migrations(scratch_url) == []  # re-run is a no-op


def test_production_state_is_noop(scratch_url):
    """Production has every current file recorded: the runner must do nothing."""
    apply_migrations(scratch_url)
    assert apply_migrations(scratch_url, dry_run=True) == []
    assert apply_migrations(scratch_url) == []


def test_new_file_applies_in_order(scratch_url, tmp_path):
    for p in discover():
        (tmp_path / p.name).write_text(p.read_text())
    apply_migrations(scratch_url, tmp_path)
    (tmp_path / "9998_b.sql").write_text("CREATE TABLE zz_b (id int);")
    (tmp_path / "9997_a.sql").write_text(
        "CREATE TABLE zz_a (id int); INSERT INTO zz_a VALUES (1);")
    assert apply_migrations(scratch_url, tmp_path, dry_run=True) == ["9997_a", "9998_b"]
    assert apply_migrations(scratch_url, tmp_path) == ["9997_a", "9998_b"]
    assert versions(scratch_url)[-2:] == ["9997_a", "9998_b"]


def test_failed_migration_rolls_back(scratch_url, tmp_path):
    for p in discover():
        (tmp_path / p.name).write_text(p.read_text())
    (tmp_path / "9999_bad.sql").write_text("CREATE TABLE zz_c (id int); SELECT nope();")
    with pytest.raises(psycopg.Error):
        apply_migrations(scratch_url, tmp_path)
    assert "9999_bad" not in versions(scratch_url)
    with psycopg.connect(scratch_url) as conn:
        assert conn.execute("SELECT to_regclass('zz_c')").fetchone()[0] is None
