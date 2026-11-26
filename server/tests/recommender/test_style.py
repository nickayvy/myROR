"""Style profile: API, quiz, LLM step and template fallback, weekly update."""

from __future__ import annotations

from datetime import timedelta

import pytest

from providers.llm import LLMUnavailable
from recommender import service, store, style
from tests.conftest import LAN_CLIENT, http_client
from tests.recommender.conftest import DAY, good_answer

ANSWERS = {
    "style_words": ["minimalist", "classic"], "usual_formality": "3",
    "favourite_colors": ["navy", "grey"], "avoid_colors": ["orange"],
    "patterns": "mostly_solid", "fit_preference": "relaxed", "layering": "practical",
    "comfort_vs_polish": 3, "fit_notes": "Sleeves are often too long on me.",
}


@pytest.fixture
async def api(make_app, fake_llm, paired, db):
    app = await make_app(start_workers=False)
    async with http_client(app, LAN_CLIENT, token=paired[1]) as c:
        c.services = app.state.services
        yield c


def test_quiz_definition():
    assert 8 <= len(style.QUIZ) <= 12
    ids = [q["id"] for q in style.QUIZ]
    assert len(ids) == len(set(ids)) and "fit_notes" in ids


@pytest.mark.parametrize("bad, needle", [
    ({**ANSWERS, "style_words": ["goth"]}, "style_words"),
    ({**ANSWERS, "style_words": ["casual", "classic", "edgy", "preppy"]}, "at most 3"),
    ({**ANSWERS, "comfort_vs_polish": 9}, "1-5"),
    ({**ANSWERS, "usual_formality": "7"}, "not an option"),
    ({**ANSWERS, "weight_kg": 80}, "unknown question"),
    ({k: v for k, v in ANSWERS.items() if k != "patterns"}, "patterns is required"),
])
def test_validate_answers(bad, needle):
    with pytest.raises(style.QuizError) as e:
        style.validate_answers(bad)
    assert any(needle in m for m in e.value.errors)


async def test_get_empty_and_put_versions(api):
    r = (await api.get("/api/style")).json()
    assert r["version"] is None and r["text"] == ""
    r = await api.put("/api/style", json={"text": "I like navy.", "fit_notes": "Petite fit."})
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["source"] == "user"
    r = await api.put("/api/style", json={"text": "I like navy and grey."})
    assert r.json()["fit_notes"] == "Petite fit."  # carried over
    hist = (await api.get("/api/style/history")).json()
    assert [h["version"] for h in hist] == [2, 1]
    assert (await api.put("/api/style", json={})).status_code == 422


async def test_quiz_with_llm(api, fake_llm):
    fake_llm.push({"profile": "You like calm, classic outfits in navy and grey."})
    r = await api.post("/api/style/quiz", json={"answers": ANSWERS})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["written_by"] == "llm" and body["source"] == "quiz"
    assert body["fit_notes"] == "Sleeves are often too long on me."  # verbatim
    assert body["quiz"]["style_words"] == ["minimalist", "classic"]
    call = fake_llm.calls[-1]
    assert "too long on me" not in call.text  # fit notes never sent for profile writing
    assert call.image_count == 0 and "body" in call.system
    assert (await api.get("/api/style/quiz")).json()["questions"][0]["id"] == "style_words"


async def test_quiz_llm_failure_uses_template(api, fake_llm):
    fake_llm.push(LLMUnavailable("down"))
    body = (await api.post("/api/style/quiz", json={"answers": ANSWERS})).json()
    assert body["written_by"] == "template"
    assert "minimalist" in body["text"] and "navy" in body["text"]


async def test_quiz_llm_body_talk_rejected(api, fake_llm):
    fake_llm.push({"profile": "Dark colours will make your body look slimmer."})
    body = (await api.post("/api/style/quiz", json={"answers": ANSWERS})).json()
    assert body["written_by"] == "template"


async def test_quiz_bad_answers_422(api):
    r = await api.post("/api/style/quiz", json={"answers": {"style_words": ["goth"]}})
    assert r.status_code == 422


async def _activity(services, seeded_day):
    await service.precompute(services, seeded_day)
    outs = await store.current_outfits(services.db, seeded_day)
    await store.add_feedback(services.db, outs[0].id, 1, "great")
    await store.add_feedback(services.db, outs[1].id, -1, "too dark")
    await store.log_wear(services.db, [i.item_id for i in outs[0].items], seeded_day,
                         source="outfit", outfit_id=outs[0].id)


async def test_weekly_update_keeps_user_words(make_app, seeded, fake_context, fake_llm):
    fake_llm.default = good_answer
    app = await make_app(start_workers=False)
    services = app.state.services
    user_text = "I like navy and grey. Never yellow."
    await style.save_profile(services.db, text=user_text, fit_notes="Long arms.", quiz={},
                             source="user")
    await _activity(services, DAY)
    fake_llm.push({"observations": ["Reaches for jeans with boots.", "Liked the oxford shirt."]})
    new = await style.weekly_update(services, DAY)
    assert new.source == "weekly_update" and new.fit_notes == "Long arms."
    assert new.text.startswith(user_text)
    assert style.LEARNED_MARKER in new.text and "- Reaches for jeans with boots." in new.text
    prompt = fake_llm.calls[-1].text
    assert "liked:" in prompt and "disliked:" in prompt and "too dark" in prompt
    assert "Long arms" not in prompt
    # the user edits; next week only the learned section is replaced
    edited = new.text.replace("Never yellow.", "Never yellow or orange.")
    await style.save_profile(services.db, text=edited, fit_notes="Long arms.", quiz={},
                             source="user")
    await services.db.execute(
        "UPDATE style_profile SET updated_at = updated_at - interval '8 days'"
        " WHERE source = 'weekly_update'")
    fake_llm.push({"observations": ["Prefers lighter tops lately."]})
    newer = await style.weekly_update(services, DAY + timedelta(days=7))
    user_part, learned = style.split_learned(newer.text)
    assert user_part == "I like navy and grey. Never yellow or orange."
    assert learned == "- Prefers lighter tops lately."


async def test_weekly_update_skips(make_app, seeded, fake_context, fake_llm):
    fake_llm.default = good_answer
    app = await make_app(start_workers=False)
    services = app.state.services
    assert await style.weekly_update(services, DAY) is None  # no signals
    await _activity(services, DAY)
    calls = len(fake_llm.calls)
    await services.db.execute(
        "INSERT INTO api_spend (ledger, provider, model, purpose, est_usd)"
        " VALUES ('dev','t','m','seed', 8.5)")
    assert await style.weekly_update(services, DAY) is None  # degraded: save budget
    assert len(fake_llm.calls) == calls
    await services.db.execute("DELETE FROM api_spend")
    fake_llm.push({"observations": ["Weight: looks slimmer in black."]})  # body talk dropped
    assert await style.weekly_update(services, DAY) is None
    fake_llm.push({"observations": ["Likes boots."]})
    assert await style.weekly_update(services, DAY) is not None
    assert await style.weekly_update(services, DAY) is None  # once a week
