"""Storage round trips on MinIO and local disk, plus the read cache."""

from __future__ import annotations

import httpx
import pytest

from storage import CachedStorage, StorageNotFound


@pytest.fixture(params=["s3", "local"])
def any_storage(request, storage, local_storage):
    return storage if request.param == "s3" else local_storage


async def test_round_trip(any_storage):
    st = any_storage
    await st.put("cutouts/a.png", b"\x89PNG-a", "image/png")
    await st.put("cutouts/b.png", b"b")
    await st.put("photos/c.jpg", b"c")
    assert await st.get("cutouts/a.png") == b"\x89PNG-a"
    assert await st.exists("cutouts/a.png")
    assert not await st.exists("cutouts/zzz.png")
    assert await st.list("cutouts/") == ["cutouts/a.png", "cutouts/b.png"]
    assert len(await st.list()) == 3
    await st.delete("cutouts/a.png")
    await st.delete("cutouts/a.png")  # idempotent
    with pytest.raises(StorageNotFound):
        await st.get("cutouts/a.png")


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "../x"])
async def test_rejects_bad_keys(local_storage, key):
    with pytest.raises(ValueError):
        await local_storage.put(key, b"x")


async def test_s3_presigned_url_works_and_bucket_is_private(storage, settings):
    await storage.put("photos/p.jpg", b"secret-bytes", "image/jpeg")
    url = await storage.presign_get("photos/p.jpg", expires_in=60)
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
        assert r.status_code == 200 and r.content == b"secret-bytes"
        anon = await c.get(f"{settings.storage_endpoint}/{settings.storage_bucket}/photos/p.jpg")
        assert anon.status_code == 403


async def test_local_presign_served_by_files_route(make_app, local_storage, tmp_path):
    app = await make_app(storage=local_storage, start_workers=False)
    await local_storage.put("collages/x.jpg", b"jpeg")
    url = await local_storage.presign_get("collages/x.jpg")
    from tests.conftest import LAN_CLIENT, http_client
    async with http_client(app, LAN_CLIENT) as c:
        r = await c.get(url)
        assert r.status_code == 200 and r.content == b"jpeg"
        assert (await c.get(url.replace("sig=", "sig=0"))).status_code == 403


class CountingStorage:
    def __init__(self, inner):
        self.inner, self.backend, self.gets = inner, inner.backend, 0

    async def get(self, key):
        self.gets += 1
        return await self.inner.get(key)

    def __getattr__(self, name):
        return getattr(self.inner, name)


async def test_cache_reads_through_once(storage, tmp_path):
    counting = CountingStorage(storage)
    cached = CachedStorage(counting, tmp_path / "cache", max_bytes=10_000)
    await storage.put("cutouts/x.png", b"x" * 100)
    assert await cached.get("cutouts/x.png") == b"x" * 100
    assert await cached.get("cutouts/x.png") == b"x" * 100
    assert counting.gets == 1 and cached.hits == 1
    await storage.put("photos/y.jpg", b"y")
    await cached.get("photos/y.jpg")
    await cached.get("photos/y.jpg")
    assert counting.gets == 3  # not a cached prefix
    await cached.delete("cutouts/x.png")
    with pytest.raises(StorageNotFound):
        await cached.get("cutouts/x.png")


async def test_cache_evicts_oldest(storage, tmp_path):
    cached = CachedStorage(storage, tmp_path / "cache", max_bytes=250)
    for i in range(4):
        await cached.put(f"cutouts/{i}.png", bytes(100))
    files = [p for p in (tmp_path / "cache").rglob("*") if p.is_file()]
    assert sum(p.stat().st_size for p in files) <= 250
