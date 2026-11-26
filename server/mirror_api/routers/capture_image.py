"""GET /api/capture/{id}/image?t=<token>: the frame under review, as JPEG.

No device token (an <img> on the mirror or phone cannot send one): the link carries a
random token, exists only while that capture is in `review`, and the frame lives in
memory only. Not cached by the browser.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from capture import get_machine
from mirror_api.deps import ServicesDep

AUTH = "none"
router = APIRouter(prefix="/api/capture", tags=["capture"])


@router.get("/{capture_id}/image")
async def review_image(capture_id: str, t: str, services: ServicesDep) -> Response:
    frame = get_machine(services).image(capture_id, t)
    if frame is None:
        raise HTTPException(404, "no such image (the review has ended)")
    return Response(frame, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
