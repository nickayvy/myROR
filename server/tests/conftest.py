"""Shared fixtures. Other test modules (yours too) can use:

  settings          Settings for the test stack (no .env read, LLM=fake, HARDWARE=mock)
  db                open Database on a clean test database (all tables truncated)
  storage           S3Storage on MinIO, test bucket emptied per test
  local_storage     LocalStorage under tmp_path
  fake_llm          FakeLLM (script it: fake_llm.push({...}))
  hardware          MockHardware
  make_app          async factory: await make_app(**overrides) -> started FastAPI app
  app               started app wired to db/storage/fake_llm/hardware
  client            httpx client calling from 127.0.0.1 (the mirror)
  remote            httpx client calling from a LAN address, no token
  paired            (device_id, token) for a freshly paired phone
  authed            LAN client with the paired phone's token

Safety: tests refuse to run unless the database host is a local container.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import boto3
import httpx
import psycopg
import pytest
from botocore.config import Config
from fastapi import FastAPI

from db import Database
from db.migrate import apply_migrations
from hardware import MockHardware, build_mock_hardware
from mirror_api.app import create_app
from mirror_api.config import Settings
from providers.llm import FakeLLM
from storage import LocalStorage, S3Storage

for _var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(_var, None)

LOCAL_HOSTS = {"db", "localhost", "127.0.0.1", "postgres"}
TEST_BUCKET = os.environ.get("TEST_STORAGE_BUCKET", "mirror-images-test")
LAN_CLIENT = ("192.168.1.50", 50123)
LOCAL_CLIENT = ("127.0.0.1", 50124)


def _base_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.exit("DATABASE_URL is not set; run tests through ops/dev/qa.sh", returncode=2)
    host = urlsplit(url).hostname or ""
    if host not in LOCAL_HOSTS:
        pytest.exit(f"refusing to run tests against non-local database host {host!r}",
                    returncode=2)
    return url


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.fixture(scope="session")
def database_url() -> str:
    """A separate `mirror_test` database, created and migrated once per session."""
    base = _base_url()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = 'mirror_test'").fetchone()
        if not exists:
            conn.execute("CREATE DATABASE mirror_test")
    url = _with_db(base, "mirror_test")
    apply_migrations(url)
    return url


@pytest.fixture(scope="session")
def admin_url() -> str:
    """URL of the maintenance database, for tests that create scratch databases."""
    return _with_db(_base_url(), "postgres")


@pytest.fixture
def settings(database_url: str, tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="dev",
        spend_ledger="dev",
        database_url=database_url,
        storage_backend="s3",
        storage_endpoint=os.environ.get("STORAGE_ENDPOINT", "http://minio:9000"),
        storage_bucket=TEST_BUCKET,
        storage_access_key_id=os.environ.get("STORAGE_ACCESS_KEY_ID", "minioadmin"),
        storage_secret_access_key=os.environ.get("STORAGE_SECRET_ACCESS_KEY", "minioadmin"),
        storage_region="us-east-1",
        local_data_dir=tmp_path / "data",
        llm="fake",
        anthropic_api_key="",
        timezone="America/New_York",
        hardware="mock",
    )


@pytest.fixture
def clean_db(database_url: str) -> None:
    """Truncate every table except schema_migrations (tables added by later
    migrations are included automatically)."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename <> 'schema_migrations'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")


@pytest.fixture
async def db(settings: Settings, clean_db) -> AsyncIterator[Database]:
    d = Database(settings.database_url, max_size=4, max_idle=5)
    await d.open()
    try:
        yield d
    finally:
        await d.close()


@pytest.fixture
async def storage(settings: Settings) -> AsyncIterator[S3Storage]:
    s3 = boto3.resource(
        "s3",
        endpoint_url=settings.storage_endpoint,
        aws_access_key_id=settings.storage_access_key_id,
        aws_secret_access_key=settings.storage_secret_access_key,
        region_name=settings.storage_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    bucket = s3.Bucket(settings.storage_bucket)
    if bucket.creation_date is None:
        bucket.create()
    bucket.objects.all().delete()
    st = S3Storage(
        endpoint=settings.storage_endpoint,
        bucket=settings.storage_bucket,
        access_key_id=settings.storage_access_key_id,
        secret_access_key=settings.storage_secret_access_key,
        region=settings.storage_region,
    )
    yield st
    await st.close()


@pytest.fixture
def local_storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def hardware() -> MockHardware:
    return build_mock_hardware()


@pytest.fixture
async def make_app(
    settings: Settings, db: Database, storage: S3Storage, fake_llm: FakeLLM,
    hardware: MockHardware,
) -> AsyncIterator[Callable[..., Awaitable[FastAPI]]]:
    """`app = await make_app(llm=other, start_workers=False)`; started, stopped at teardown."""
    stack = []

    async def _make(*, start_workers: bool = True, settings_: Settings | None = None,
                    **overrides) -> FastAPI:
        base = {"db": db, "storage": storage, "llm": fake_llm, "hardware": hardware}
        base.update(overrides)
        app = create_app(settings_ or settings, base, start_workers=start_workers)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        stack.append(ctx)
        return app

    yield _make
    for ctx in reversed(stack):
        await ctx.__aexit__(None, None, None)


@pytest.fixture
async def app(make_app) -> FastAPI:
    return await make_app()


def http_client(app: FastAPI, peer: tuple[str, int], token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://mirror.test",
        headers=headers,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with http_client(app, LOCAL_CLIENT) as c:
        yield c


@pytest.fixture
async def remote(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with http_client(app, LAN_CLIENT) as c:
        yield c


@pytest.fixture
async def paired(client: httpx.AsyncClient, remote: httpx.AsyncClient) -> tuple[str, str]:
    code = (await client.post("/api/pair/code")).json()["code"]
    r = await remote.post("/api/pair", json={"code": code, "name": "Test phone"})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["device_id"], body["token"]


@pytest.fixture
async def authed(app: FastAPI, paired) -> AsyncIterator[httpx.AsyncClient]:
    async with http_client(app, LAN_CLIENT, token=paired[1]) as c:
        yield c
