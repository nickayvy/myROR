"""The process-wide service container, stored on `app.state.services`.

Routes get pieces through the providers in mirror_api.deps; jobs get them through
JobContext. Tests replace pieces by passing `overrides` to create_app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db import Database
    from hardware import Hardware
    from mirror_api.auth import Auth
    from mirror_api.config import Settings
    from mirror_api.jobs import DailyScheduler, JobQueue
    from mirror_api.spend import SpendLedger
    from mirror_api.ws import Hub
    from providers.llm import LLM
    from storage import Storage


@dataclass
class Services:
    settings: Settings
    db: Database
    storage: Storage | None
    spend: SpendLedger
    llm: LLM
    hub: Hub
    hardware: Hardware
    auth: Auth
    jobs: JobQueue = field(init=False)
    scheduler: DailyScheduler = field(init=False)
    # Closet-pipeline providers, built lazily from settings on first use by
    # pipeline.wiring.pipeline_providers(). Override them through create_app(overrides=...)
    # (or assign after startup in tests). Typed Any so this module does not import
    # provider packages.
    bg_remover: Any = None
    segmenter: Any = None
    embedder: Any = None
    face_blurrer: Any = None
    fal: Any = None

    # Per-process state owned by feature packages (created on first use by their own
    # accessors: integrations.today.get_state, recommender.service._state,
    # capture.get_machine, mirror_api.presence.get_watcher).
    integrations: Any = None
    recommender: Any = None
    capture: Any = None
    presence: Any = None

    # Names accepted in create_app(overrides=...)
    OVERRIDABLE = ("db", "storage", "spend", "llm", "hub", "hardware", "auth",
                   "bg_remover", "segmenter", "embedder", "face_blurrer", "fal")
    # Overridable names that are plain optional fields (None = build lazily)
    PROVIDER_FIELDS = ("bg_remover", "segmenter", "embedder", "face_blurrer", "fal")
