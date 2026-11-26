"""Thin async client for fal's synchronous run endpoint (HANDOFF A7).

Every request:
- goes to POST https://fal.run/<endpoint-id> (never fal's storage/upload API);
- sends `Authorization: Key <FAL_KEY>` and `X-Fal-Store-IO: 0`;
- sets `sync_mode: true` so outputs come back as data URIs and stay out of request history;
- passes the image as a short-lived presigned GET link to our private bucket (default) or,
  with FAL_INPUT_MODE=data_uri, inline as a base64 data URI.

Spend: `spend.check` before the call, `spend.record` before returning the result. A request
that fails is not recorded (fal may still bill it; we cannot see that).
Retry: one retry on transient errors (connect/read timeouts, 429, 5xx).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from providers.fal.pricing import fal_price

log = logging.getLogger(__name__)

FAL_RUN_BASE = "https://fal.run"
# Hosts a non-data-URI output may be fetched from (only if fal ignored sync_mode).
OUTPUT_HOST_SUFFIXES = (".fal.media", ".fal.run", ".fal.ai")
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_OUTPUT_BYTES = 40 * 1024 * 1024


class FalError(RuntimeError):
    """Base class."""


class FalNotConfigured(FalError):
    """FAL_KEY is empty."""


class FalUnavailable(FalError):
    """Network, timeout, 429 or 5xx after one retry. Retryable later."""


class FalRequestError(FalError):
    """fal rejected the request (4xx other than 408/425/429). A bug or bad input; not retryable."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"fal {status}: {detail}")
        self.status = status
        self.detail = detail


class FalResponseError(FalError):
    """The response did not have the expected shape."""


def data_uri(data: bytes, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def decode_data_uri(uri: str) -> tuple[bytes, str]:
    head, sep, payload = uri.partition(",")
    if not sep or not head.startswith("data:"):
        raise FalResponseError("not a data URI")
    meta = head[5:]
    content_type = meta.split(";")[0] or "application/octet-stream"
    try:
        if ";base64" in meta:
            return base64.b64decode(payload, validate=False), content_type
        from urllib.parse import unquote_to_bytes

        return unquote_to_bytes(payload), content_type
    except (binascii.Error, ValueError) as e:
        raise FalResponseError(f"bad data URI: {e}") from e


class FalClient:
    def __init__(
        self,
        key: str,
        spend: Any,
        *,
        storage: Any = None,
        input_mode: str = "presign",
        presign_ttl_s: int = 300,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = FAL_RUN_BASE,
        retry_delay_s: float = 1.0,
    ) -> None:
        self.key = key
        self.spend = spend
        self.storage = storage
        self.input_mode = input_mode
        self.presign_ttl_s = presign_ttl_s
        self.timeout_s = timeout_s
        self.base_url = base_url.rstrip("/")
        self.retry_delay_s = retry_delay_s
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout_s,
                                       follow_redirects=False)
        self.last_output_was_data_uri: bool | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def close(self) -> None:
        await self._http.aclose()

    # ---- inputs -------------------------------------------------------------------

    async def image_input(self, *, data: bytes | None, key: str | None,
                          content_type: str = "image/jpeg") -> str:
        """What to put in `image_url`: a presigned link when we have a stored key and
        FAL_INPUT_MODE=presign, otherwise a data URI of `data`."""
        if self.input_mode == "presign" and key and self.storage is not None:
            return await self.storage.presign_get(key, expires_in=self.presign_ttl_s)
        if data is None:
            if key is None or self.storage is None:
                raise FalError("no image bytes and no stored key to send")
            data = await self.storage.get(key)
        return data_uri(data, content_type)

    # ---- call ---------------------------------------------------------------------

    async def run(
        self,
        endpoint: str,
        arguments: dict[str, Any],
        *,
        purpose: str,
        timeout_s: float | None = None,
        job_id: UUID | None = None,
        units: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.key:
            raise FalNotConfigured("FAL_KEY is not set")
        if "/" not in endpoint or endpoint.startswith(("http:", "https:", "/")):
            raise FalError(f"bad fal endpoint id {endpoint!r}")
        body = dict(arguments)
        body["sync_mode"] = True  # always; never let a caller turn it off
        headers = {
            "Authorization": f"Key {self.key}",
            "X-Fal-Store-IO": "0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self.base_url}/{endpoint}"
        await self.spend.check(purpose)
        timeout = httpx.Timeout(timeout_s or self.timeout_s, connect=10.0)
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = await self._http.post(url, json=body, headers=headers, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = FalUnavailable(f"{endpoint}: {type(e).__name__}: {e}")
            else:
                if resp.status_code in TRANSIENT_STATUS:
                    last = FalUnavailable(f"{endpoint}: HTTP {resp.status_code}")
                elif resp.status_code >= 400:
                    raise FalRequestError(resp.status_code, _detail(resp))
                else:
                    try:
                        result = resp.json()
                    except ValueError as e:
                        raise FalResponseError(f"{endpoint}: response is not JSON") from e
                    if not isinstance(result, dict):
                        raise FalResponseError(f"{endpoint}: response is not an object")
                    await self.spend.record(
                        provider="fal", model=endpoint, purpose=purpose,
                        est_usd=fal_price(endpoint),
                        units={"requests": 1, "attempt": attempt, **(units or {})},
                        job_id=job_id,
                    )
                    return result
            if attempt == 1:
                log.warning("fal %s transient failure, retrying once: %s", endpoint, last)
                await asyncio.sleep(self.retry_delay_s)
        assert last is not None
        raise last

    # ---- outputs ------------------------------------------------------------------

    async def fetch_output(self, url: str) -> bytes:
        """Bytes of an output file. With sync_mode this is a data URI. If fal ignored
        sync_mode and returned a hosted URL, fetch it only from fal's own media hosts."""
        if url.startswith("data:"):
            self.last_output_was_data_uri = True
            return decode_data_uri(url)[0]
        self.last_output_was_data_uri = False
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (
            host.endswith(OUTPUT_HOST_SUFFIXES) or host == "fal.media"
        ):
            raise FalResponseError(f"refusing to fetch output from unexpected host {host!r}")
        log.warning("fal returned a hosted output URL despite sync_mode; the output is "
                    "stored on fal's CDN (%s)", host)
        try:
            resp = await self._http.get(url, timeout=httpx.Timeout(30.0, connect=10.0))
        except httpx.HTTPError as e:
            raise FalUnavailable(f"fetch output: {e}") from e
        if resp.status_code != 200:
            raise FalUnavailable(f"fetch output: HTTP {resp.status_code}")
        if len(resp.content) > MAX_OUTPUT_BYTES:
            raise FalResponseError("output too large")
        return resp.content


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return str(detail)[:500]
