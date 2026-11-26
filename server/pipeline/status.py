"""Per-photo progress: photos.pipeline_status/step/error plus a WebSocket event.

Event `pipeline.photo` (audience phone): {"photo_id", "source", "status", "step", "error"}.
Statuses: queued -> processing -> done | failed. Steps (onboarding): remove_background,
tag, embed, match. Steps (check-in): analyse, segment, embed, match. `waiting_for_budget`
when the spend cap deferred the job.
Also `inbox.updated` {"photo_id"} when an onboarding photo reaches the inbox, and
`checkin.ready` {"photo_id"} when check-in proposals are ready.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)


async def set_status(services: Any, photo_id: UUID, status: str, step: str | None = None,
                     error: str | None = None) -> None:
    row = await services.db.fetchrow(
        "UPDATE photos SET pipeline_status = %s, pipeline_step = %s, pipeline_error = %s"
        " WHERE id = %s RETURNING source",
        (status, step, error, photo_id),
    )
    if row is None:
        return
    await services.hub.broadcast(
        "pipeline.photo",
        {"photo_id": str(photo_id), "source": row["source"], "status": status, "step": step,
         "error": error},
        audience="phone",
    )
