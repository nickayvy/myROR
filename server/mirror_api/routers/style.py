"""Style profile: GET/PUT /api/style, GET /api/style/history, GET/POST /api/style/quiz.

PUT body {"text"?, "fit_notes"?}: a new version (source 'user'); omitted fields carry over.
POST /api/style/quiz {"answers": {...}}: writes the first profile (source 'quiz'). Fit
notes come only from what the user typed and are stored verbatim.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mirror_api.deps import ServicesDep
from recommender import style

router = APIRouter(prefix="/api/style", tags=["style"])


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=style.TEXT_MAX)
    fit_notes: str | None = Field(default=None, max_length=style.FIT_NOTES_MAX)


class QuizBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, Any]


def _empty() -> dict[str, Any]:
    return style.StyleProfile(None, "", None, {}, "none").as_dict()


@router.get("")
async def get_style(services: ServicesDep) -> dict[str, Any]:
    p = await style.current_profile(services.db)
    return p.as_dict() if p else _empty()


@router.put("")
async def put_style(body: ProfileBody, services: ServicesDep) -> dict[str, Any]:
    if body.text is None and body.fit_notes is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give text and/or fit_notes")
    cur = await style.current_profile(services.db)
    text = body.text if body.text is not None else (cur.text if cur else "")
    fit = body.fit_notes if body.fit_notes is not None else (cur.fit_notes if cur else None)
    new = await style.save_profile(services.db, text=text.strip(),
                                   fit_notes=(fit or "").strip() or None,
                                   quiz=cur.quiz if cur else {}, source="user")
    await services.hub.broadcast("style.updated", {"version": new.version, "source": "user"})
    return new.as_dict()


@router.get("/history")
async def history(services: ServicesDep) -> list[dict[str, Any]]:
    return [p.as_dict() for p in await style.profile_history(services.db)]


@router.get("/quiz")
async def quiz() -> dict[str, Any]:
    return {"questions": style.QUIZ}


@router.post("/quiz", status_code=status.HTTP_201_CREATED)
async def submit_quiz(body: QuizBody, services: ServicesDep) -> dict[str, Any]:
    try:
        answers = style.validate_answers(body.answers)
    except style.QuizError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.errors) from None
    text, used_llm = await style.profile_from_quiz(
        services.llm, services.settings.llm_outfit_model, answers)
    new = await style.save_profile(services.db, text=text, fit_notes=answers.get("fit_notes"),
                                   quiz=answers, source="quiz")
    await services.hub.broadcast("style.updated", {"version": new.version, "source": "quiz"})
    return {**new.as_dict(), "written_by": "llm" if used_llm else "template"}
