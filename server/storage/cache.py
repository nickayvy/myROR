"""Read-through disk cache for hot keys (cutouts and collages for the mirror).

Only keys under `prefixes` are cached; everything else passes straight through.
Writes go to both the backend and the cache. Eviction is by oldest access time
once the cache exceeds `max_bytes`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from pathlib import Path

from storage.base import Storage, check_key

DEFAULT_PREFIXES = ("cutouts/", "collages/")


class CachedStorage:
    def __init__(
        self,
        inner: Storage,
        cache_dir: Path,
        *,
        prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.inner = inner
        self.backend = inner.backend
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prefixes = prefixes
        self.max_bytes = max_bytes
        self.hits = 0
        self.misses = 0

    def _cacheable(self, key: str) -> bool:
        return key.startswith(self.prefixes)

    def _path(self, key: str) -> Path:
        h = hashlib.sha256(check_key(key).encode()).hexdigest()
        return self.dir / h[:2] / h

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await self.inner.put(key, data, content_type)
        if self._cacheable(key):
            await asyncio.to_thread(self._store, key, data)

    async def get(self, key: str) -> bytes:
        if not self._cacheable(key):
            return await self.inner.get(key)
        path = self._path(key)
        try:
            data = await asyncio.to_thread(path.read_bytes)
            self.hits += 1
            os.utime(path)  # LRU by access
            return data
        except FileNotFoundError:
            pass
        self.misses += 1
        data = await self.inner.get(key)
        await asyncio.to_thread(self._store, key, data)
        return data

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)
        if self._cacheable(key):
            self._path(key).unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        if self._cacheable(key) and self._path(key).is_file():
            return True
        return await self.inner.exists(key)

    async def presign_get(self, key: str, expires_in: int = 900) -> str:
        return await self.inner.presign_get(key, expires_in)

    async def list(self, prefix: str = "") -> list[str]:
        return await self.inner.list(prefix)

    async def ping(self) -> None:
        ping = getattr(self.inner, "ping", None)
        if ping is not None:
            await ping()

    async def close(self) -> None:
        await self.inner.close()

    def _store(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._evict()

    def _evict(self) -> None:
        files = [p for p in self.dir.rglob("*") if p.is_file() and not p.name.startswith(".")]
        total = sum(p.stat().st_size for p in files)
        if total <= self.max_bytes:
            return
        for p in sorted(files, key=lambda f: f.stat().st_mtime):
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
            if total <= self.max_bytes:
                break
