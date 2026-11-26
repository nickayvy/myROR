"""fal client with the mandatory privacy options (HANDOFF A7). See client.py."""

from __future__ import annotations

from providers.fal.client import (
    FAL_RUN_BASE,
    FalClient,
    FalError,
    FalNotConfigured,
    FalRequestError,
    FalResponseError,
    FalUnavailable,
    data_uri,
    decode_data_uri,
)
from providers.fal.pricing import FAL_PRICES, fal_price

__all__ = [
    "FAL_PRICES",
    "FAL_RUN_BASE",
    "FalClient",
    "FalError",
    "FalNotConfigured",
    "FalRequestError",
    "FalResponseError",
    "FalUnavailable",
    "build_fal",
    "data_uri",
    "decode_data_uri",
    "fal_price",
]


def build_fal(settings, spend, storage, **kw) -> FalClient:
    return FalClient(
        settings.fal_key,
        spend,
        storage=storage,
        input_mode=settings.fal_input_mode,
        presign_ttl_s=settings.fal_presign_ttl_s,
        timeout_s=settings.fal_timeout_s,
        **kw,
    )
