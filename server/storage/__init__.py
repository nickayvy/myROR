"""Image storage behind one protocol: S3-compatible (prod) or local disk, plus a read cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from storage.base import Storage, StorageError, StorageNotFound
from storage.cache import CachedStorage
from storage.local import LocalStorage
from storage.s3 import S3Storage

if TYPE_CHECKING:
    from mirror_api.config import Settings

__all__ = [
    "CachedStorage",
    "LocalStorage",
    "S3Storage",
    "Storage",
    "StorageError",
    "StorageNotFound",
    "build_storage",
]


class StorageNotConfigured(StorageError):
    pass


def build_storage(settings: Settings) -> Storage | None:
    """The app's storage: S3 wrapped in the read cache, or local disk. None if unconfigured."""
    if settings.storage_backend == "local":
        return LocalStorage(settings.data_dir / "store")
    if not settings.storage_configured:
        return None
    s3 = S3Storage(
        endpoint=settings.storage_endpoint,
        bucket=settings.storage_bucket,
        access_key_id=settings.storage_access_key_id,
        secret_access_key=settings.storage_secret_access_key,
        region=settings.storage_region,
    )
    return CachedStorage(s3, settings.cache_dir / "storage")
