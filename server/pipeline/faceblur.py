"""Face-blur seam for mirror and full-body photos (HANDOFF A2: FACE_BLUR, off by default).

When FACE_BLUR is on, a FaceBlurrer runs before anything is stored or sent: its output
becomes photos.storage_key (the copy fal and Claude receive) and the original goes to
photos.original_key. No face detector is implemented yet: the default blurrer raises
FaceBlurUnavailable, and the check-in is refused with nothing stored and nothing sent.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class FaceBlurUnavailable(RuntimeError):
    pass


class NoFaceFound(RuntimeError):
    """A blurrer that cannot find a face must say so; the photo is then not sent."""


@runtime_checkable
class FaceBlurrer(Protocol):
    name: str

    async def blur(self, jpeg: bytes) -> bytes:
        """Return a JPEG with every face blurred. Raise NoFaceFound or FaceBlurUnavailable."""
        ...


class UnavailableFaceBlurrer:
    name = "unavailable"

    async def blur(self, jpeg: bytes) -> bytes:
        raise FaceBlurUnavailable(
            "FACE_BLUR is on but no face blurrer is installed; nothing was stored or sent")


def build_face_blurrer(settings) -> FaceBlurrer:
    return UnavailableFaceBlurrer()
