"""HTTP upload handling shared by /api/items/upload and /api/checkins/upload."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, UploadFile, status

from pipeline.faceblur import FaceBlurUnavailable, NoFaceFound
from pipeline.imaging import ImageRejected
from pipeline.ingest import ingest_photo

ACCEPTED_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/heic", "image/heif",
    "image/heic-sequence", "image/heif-sequence", "image/webp", "application/octet-stream",
    "",
}
CHUNK = 1024 * 1024


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if ctype not in ACCEPTED_CONTENT_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            {"code": "unsupported_type",
                             "message": f"unsupported content type {ctype!r}"})
    buf = bytearray()
    while chunk := await file.read(CHUNK):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(413,
                                {"code": "too_large",
                                 "message": f"file is larger than {max_bytes // CHUNK} MB"})
    if not buf:
        raise HTTPException(422, {"code": "empty", "message": "empty file"})
    return bytes(buf)


def photo_status(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "photo_id": str(row["id"]),
        "source": row["source"],
        "status": row["pipeline_status"],
        "step": row["pipeline_step"],
        "error": row["pipeline_error"],
        "width": row["width"],
        "height": row["height"],
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


async def handle_upload(services: Any, file: UploadFile, source: str,
                        response) -> dict[str, Any]:
    max_bytes = int(services.settings.upload_max_mb * CHUNK)
    data = await read_limited(file, max_bytes)
    try:
        result = await ingest_photo(services, data, source=source)
    except ImageRejected as e:
        code = 413 if e.code == "too_large" else 415 if e.code == "unsupported_type" else 422
        raise HTTPException(code, {"code": e.code, "message": str(e)}) from None
    except (FaceBlurUnavailable, NoFaceFound) as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            {"code": "face_blur_unavailable", "message": str(e)}) from None
    if result.photo["source"] != source:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"code": "already_uploaded_elsewhere",
                             "message": f"this exact file was already uploaded as "
                                        f"{result.photo['source']}",
                             "photo_id": str(result.photo["id"])})
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return {**photo_status(result.photo), "duplicate_upload": not result.created}
