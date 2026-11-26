from datetime import UTC, datetime

from integrations.formality import FormalityCache, day_formality, tag_titles
from mirror_api.contracts import CalendarEvent
from mirror_api.spend import SpendCapExceeded, SpendStatus
from providers.llm import LLMRefusal


def ev(title, f=None, location=None):
    return CalendarEvent(uid=title, source="google", title=title, location=location,
                         start=datetime(2026, 11, 25, 15, tzinfo=UTC), formality=f)


def rate_all(value):
    def respond(call):
        import json

        items = json.loads(call.text)
        return {"ratings": [{"i": it["i"], "formality": value} for it in items]}

    return respond


async def test_tags_once_and_caches_by_hash(fake_services, tmp_path):
    cache = FormalityCache(tmp_path / "f.json")
    llm = fake_services.llm
    llm.push(rate_all(4))
    titles = ["Client presentation", "client  PRESENTATION", "Board meeting"]
    assert await tag_titles(fake_services, cache, titles) == 2
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call.model == fake_services.settings.llm_tag_model and call.purpose == "event_formality"
    assert "Board meeting" in call.text and "presentation" in call.text.lower()
    # Persistent and keyed by hash: a new cache object reads it, no titles on disk.
    assert b"Board" not in (tmp_path / "f.json").read_bytes()
    again = FormalityCache(tmp_path / "f.json")
    assert again.get("CLIENT presentation") == 4
    assert await tag_titles(fake_services, again, titles) == 0
    assert len(llm.calls) == 1  # paid once


async def test_only_titles_reach_the_model(fake_services, tmp_path):
    from integrations.today import IntegrationsState

    fake_services.llm.push(rate_all(3))
    state = IntegrationsState(fake_services)
    await state.tag_events([ev("Dentist", location="12 Secret Street")])
    assert "Secret Street" not in fake_services.llm.calls[0].text


async def test_failures_leave_formality_unknown(fake_services, tmp_path):
    cache = FormalityCache(tmp_path / "f.json")
    status = SpendStatus(ledger="dev", cap_usd=1, spent_usd=1, percent=100, mode="blocked",
                         period_start=None, period_end=None)
    fake_services.llm.push(LLMRefusal("other", "no"), SpendCapExceeded("event_formality", status),
                           {"ratings": [{"i": 0, "formality": 9}]})
    for _ in range(3):
        assert await tag_titles(fake_services, cache, ["Gala"]) == 0
    events = cache.apply([ev("Gala")])
    assert events[0].formality is None and day_formality(events) == 1


async def test_unconfigured_llm_is_not_called(fake_services, tmp_path):
    class Off:
        configured = False

        async def complete_structured(self, **kw):
            raise AssertionError("called")

    fake_services.llm = Off()
    assert await tag_titles(fake_services, FormalityCache(tmp_path / "f.json"), ["Gala"]) == 0


def test_day_formality():
    assert day_formality([]) == 1
    assert day_formality([ev("a", 2), ev("b", None), ev("c", 4)]) == 4
