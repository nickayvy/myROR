"""Serve the built phone app (phone/dist, PHONE_DIST_DIR) at / with a single-page-app
fallback: a GET for a path that is not a file in the build returns index.html, so
client-side routes such as /pair?code=... and /closet work on a reload.

Never shadows the API: /api/..., /ws, /docs, /redoc and /openapi.json are not served
from here (an unknown /api path still answers the API's 404). The app shell needs no
token (the phone loads it before pairing); every API call it makes still does.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

RESERVED = ("api", "ws", "docs", "redoc", "openapi.json")


def is_reserved(path: str) -> bool:
    first = path.lstrip("/").split("/", 1)[0]
    return first in RESERVED


def mount_phone_app(app: FastAPI, dist: Path) -> Path | None:
    """Add the catch-all route if `dist` holds a build. Returns the directory served."""
    index = dist / "index.html"
    if not index.is_file():
        log.info("phone app not served: %s has no index.html", dist)
        return None
    root = dist.resolve()

    async def phone_app(request: Request, path: str = "") -> FileResponse:
        if is_reserved(path):
            raise HTTPException(404, "Not Found")
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                # Hashed build assets can be cached; the shell must not be.
                cache = ("public, max-age=31536000, immutable"
                         if candidate.parent.name == "assets" else "no-cache")
                return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    # Registered last, after every router, so explicit routes always win.
    app.add_api_route("/", phone_app, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/{path:path}", phone_app, methods=["GET", "HEAD"],
                      include_in_schema=False)
    log.info("serving the phone app from %s", root)
    return root
