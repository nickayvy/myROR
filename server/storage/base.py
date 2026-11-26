"""Storage protocol. Keys are '/'-separated paths such as 'cutouts/<item_id>.png'.

Suggested key prefixes (keep them stable; the cache wrapper keys off them):
  photos/<photo_id>.jpg      original or face-blurred upload
  cutouts/<id>.png           background-removed garment
  masks/<detection_id>.png   segmentation mask
  collages/<date>/<outfit_id>.jpg
  calibration/<id>.png
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    pass


class StorageNotFound(StorageError, KeyError):
    """The key does not exist."""


@runtime_checkable
class Storage(Protocol):
    backend: str

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    async def get(self, key: str) -> bytes:
        """Raise StorageNotFound if missing."""
        ...

    async def delete(self, key: str) -> None:
        """Idempotent: deleting a missing key is not an error."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def presign_get(self, key: str, expires_in: int = 900) -> str:
        """A time-limited URL a phone can GET directly."""
        ...

    async def list(self, prefix: str = "") -> list[str]:
        """All keys starting with prefix, sorted."""
        ...

    async def close(self) -> None: ...


def check_key(key: str) -> str:
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError(f"invalid storage key: {key!r}")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"invalid storage key: {key!r}")
    return key
