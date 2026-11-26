"""S3-compatible storage (Neon Object Storage in production, MinIO in dev).

boto3 is synchronous, so every network call runs in a worker thread.
Path-style addressing and SigV4, as Neon Object Storage requires.
"""

from __future__ import annotations

import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from storage.base import StorageError, StorageNotFound, check_key

_MISSING = {"404", "NoSuchKey", "NotFound"}


class S3Storage:
    backend = "s3"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        check_key(key)
        extra = {"ContentType": content_type} if content_type else {}
        await self._call(self._client.put_object, Bucket=self.bucket, Key=key, Body=data, **extra)

    async def get(self, key: str) -> bytes:
        check_key(key)

        def _get() -> bytes:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read()

        try:
            return await asyncio.to_thread(_get)
        except ClientError as e:
            if _code(e) in _MISSING:
                raise StorageNotFound(key) from None
            raise StorageError(f"get {key}: {_code(e)}") from e

    async def delete(self, key: str) -> None:
        check_key(key)
        await self._call(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        check_key(key)
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if _code(e) in _MISSING:
                return False
            raise StorageError(f"head {key}: {_code(e)}") from e

    async def presign_get(self, key: str, expires_in: int = 900) -> str:
        check_key(key)
        # Signing is local; no network call.
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
        )

    async def list(self, prefix: str = "") -> list[str]:
        def _list() -> list[str]:
            keys: list[str] = []
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            )
            for page in pages:
                keys.extend(o["Key"] for o in page.get("Contents", []))
            return sorted(keys)

        try:
            return await asyncio.to_thread(_list)
        except ClientError as e:
            raise StorageError(f"list {prefix}: {_code(e)}") from e

    async def ping(self) -> None:
        """Cheap reachability check for /api/health."""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
        except ClientError as e:
            raise StorageError(f"bucket: {_code(e)}") from e

    async def close(self) -> None:
        self._client.close()

    async def _call(self, fn, **kwargs):
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except ClientError as e:
            raise StorageError(f"{fn.__name__} {kwargs.get('Key', '')}: {_code(e)}") from e


def _code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", "unknown"))
