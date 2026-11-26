"""GET /api/files/{key}: serves presigned links for STORAGE_BACKEND=local only.
(With S3, presign_get returns a URL on the bucket and this route is unused.)"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Response, status

from mirror_api.deps import ServicesDep
from storage import LocalStorage, StorageNotFound

AUTH = "none"  # the signature is the credential
router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/{key:path}")
async def get_file(key: str, exp: int, sig: str, services: ServicesDep) -> Response:
    storage = services.storage
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        if not storage.verify(key, exp, sig):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "link expired or invalid")
        data = await storage.get(key)
    except (StorageNotFound, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    media = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return Response(data, media_type=media, headers={"Cache-Control": "private, max-age=300"})
