"""Fixtures for pipeline tests: an app with fake bg remover, segmenter and embedder (selected
through settings, the way production selects the real ones) and a FakeLLM that tags
synthetic garments and finds garments in synthetic person photos."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from providers.llm import FakeLLM
from tests.conftest import LAN_CLIENT, LOCAL_CLIENT, http_client
from tests.pipeline import synth


@pytest.fixture
def pipeline_settings(settings):
    return settings.model_copy(update={"bg_remover": "fake", "segmenter": "fake",
                                       "embedder": "fake"})


@pytest.fixture
def smart_llm() -> FakeLLM:
    return FakeLLM(default=synth.responder)


@pytest.fixture
async def papp(make_app, pipeline_settings, smart_llm) -> FastAPI:
    return await make_app(settings_=pipeline_settings, llm=smart_llm)


@pytest.fixture
def services(papp):
    return papp.state.services


@pytest.fixture
async def phone(papp) -> AsyncIterator[httpx.AsyncClient]:
    async with http_client(papp, LOCAL_CLIENT) as mirror:
        code = (await mirror.post("/api/pair/code")).json()["code"]
    async with http_client(papp, LAN_CLIENT) as anon:
        r = await anon.post("/api/pair", json={"code": code, "name": "Test phone"})
        assert r.status_code == 201, r.text
        token = r.json()["token"]
    async with http_client(papp, LAN_CLIENT, token=token) as c:
        yield c


async def upload(client, data: bytes, name: str = "g.jpg", ctype: str = "image/jpeg",
                 path: str = "/api/items/upload"):
    return await client.post(path, files={"file": (name, data, ctype)})


async def settle(services, timeout: float = 30.0) -> None:
    """Wait until no job is running or ready to run. JobQueue.wait_idle alone can return
    early: a wake() that lands while a dispatch pass is finishing is followed by
    `_idle.set()`, so we also check the jobs table (tests only; production never polls)."""
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while True:
        await services.jobs.wait_idle(max(0.1, deadline - time.monotonic()))
        busy = await services.db.fetchval(
            "SELECT count(*) FROM jobs WHERE status = 'running'"
            " OR (status = 'queued' AND run_after <= now())")
        if not busy:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"{busy} job(s) still busy")
        await asyncio.sleep(0.05)
