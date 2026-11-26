"""Storage key layout for the closet pipeline. Everything derived from one photo lives under
that photo's id, so deleting a photo is three prefix sweeps.

  photos/<photo_id>/original.jpg        the photo as stored (EXIF stripped, downscaled,
                                        white-balanced for onboarding). photos.storage_key
                                        when FACE_BLUR is off; photos.original_key when on.
  photos/<photo_id>/blurred.jpg         FACE_BLUR=on only: the copy sent to AI (storage_key)
  cutouts/<photo_id>/<detection_id>.png background-removed / masked garment, cropped. Shared
                                        by the item confirmed from that detection.
  masks/<photo_id>/<detection_id>.png   check-in segmentation mask (mode L)

The `cutouts/` prefix is the one storage.cache keeps on the Pi's disk for the mirror.
"""

from __future__ import annotations

from uuid import UUID


def photo_original(photo_id: UUID | str) -> str:
    return f"photos/{photo_id}/original.jpg"


def photo_blurred(photo_id: UUID | str) -> str:
    return f"photos/{photo_id}/blurred.jpg"


def cutout(photo_id: UUID | str, detection_id: UUID | str) -> str:
    return f"cutouts/{photo_id}/{detection_id}.png"


def mask(photo_id: UUID | str, detection_id: UUID | str) -> str:
    return f"masks/{photo_id}/{detection_id}.png"


def photo_prefixes(photo_id: UUID | str) -> list[str]:
    return [f"photos/{photo_id}/", f"cutouts/{photo_id}/", f"masks/{photo_id}/"]


def belongs_to_photo(key: str | None, photo_id: UUID | str) -> bool:
    return bool(key) and any(key.startswith(p) for p in photo_prefixes(photo_id))
