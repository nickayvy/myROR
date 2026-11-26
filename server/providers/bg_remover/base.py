"""Background-removal protocol."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from PIL import Image


class BgRemovalError(RuntimeError):
    """The provider returned something unusable (not retryable)."""


@dataclass
class CutoutResult:
    png: bytes  # RGBA PNG, same size as the input image
    provider: str
    model: str
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BgRemover(Protocol):
    name: str

    async def remove(
        self,
        *,
        data: bytes,
        key: str | None = None,
        content_type: str = "image/jpeg",
        purpose: str = "bg_remove",
        job_id: UUID | None = None,
    ) -> CutoutResult:
        """`data` is the image; `key` its storage key (lets a hosted provider fetch it by a
        presigned link instead of receiving the bytes)."""
        ...


def normalise_rgba(png: bytes, size: tuple[int, int]) -> bytes:
    """Return an RGBA PNG at `size`. Raises BgRemovalError if the output has no alpha."""
    try:
        im = Image.open(io.BytesIO(png))
        im.load()
    except Exception as e:  # noqa: BLE001 - any decode failure is the provider's fault
        raise BgRemovalError(f"output is not an image: {e}") from e
    if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
        raise BgRemovalError(f"output has no alpha channel (mode {im.mode})")
    im = im.convert("RGBA")
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=False)
    return out.getvalue()
