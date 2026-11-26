"""Filesystem storage under LOCAL_DATA_DIR/store. Used when STORAGE_BACKEND=local.

presign_get returns a relative URL served by mirror_api/routers/files.py, signed
with a per-process HMAC key (links stop working when the server restarts).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from storage.base import StorageNotFound, check_key


class LocalStorage:
    backend = "local"

    def __init__(self, root: Path, *, signing_key: bytes | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._signing_key = signing_key or secrets.token_bytes(32)

    def _path(self, key: str) -> Path:
        p = (self.root / check_key(key)).resolve()
        if self.root not in p.parents:
            raise ValueError(f"invalid storage key: {key!r}")
        return p

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise StorageNotFound(key) from None

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def presign_get(self, key: str, expires_in: int = 900) -> str:
        check_key(key)
        exp = int(time.time()) + expires_in
        return f"/api/files/{quote(key)}?exp={exp}&sig={self._sign(key, exp)}"

    def verify(self, key: str, exp: int, sig: str) -> bool:
        if exp < time.time():
            return False
        return hmac.compare_digest(self._sign(key, exp), sig)

    async def list(self, prefix: str = "") -> list[str]:
        def _list() -> list[str]:
            out = []
            for p in self.root.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    key = p.relative_to(self.root).as_posix()
                    if key.startswith(prefix):
                        out.append(key)
            return sorted(out)

        return await asyncio.to_thread(_list)

    async def ping(self) -> None:
        if not self.root.is_dir():
            raise StorageNotFound(str(self.root))

    async def close(self) -> None:
        return None

    def _sign(self, key: str, exp: int) -> str:
        msg = f"{key}\n{exp}".encode()
        return hmac.new(self._signing_key, msg, hashlib.sha256).hexdigest()
