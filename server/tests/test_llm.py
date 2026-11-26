"""ClaudeLLM against a mocked HTTP transport (no network, no key), and FakeLLM."""

from __future__ import annotations

import json
from decimal import Decimal
from zoneinfo import ZoneInfo

import anthropic
import httpx2
import pytest

from mirror_api.spend import SpendCapExceeded, SpendLedger
from providers.llm import (
    ClaudeLLM,
    FakeLLM,
    LLMNotConfigured,
    LLMRefusal,
    LLMRequestError,
    LLMSchemaError,
    LLMTruncated,
    image_block,
    text_block,
)
from providers.llm.base import api_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["top", "bottom", "shoes"]},
        "formality": {"type": "integer", "minimum": 1, "maximum": 5},
        "pattern": {"type": "string", "maxLength": 20},
    },
    "required": ["category", "formality", "pattern"],
}
GOOD = {"category": "top", "formality": 2, "pattern": "solid"}


def message(payload, *, stop="end_turn", model="claude-haiku-5-5", usage=None, details=None):
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": payload if isinstance(payload, str)
                     else json.dumps(payload)}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 200,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    if details:
        body["stop_details"] = details
    return body


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "body": json.loads(request.content)})
        status, body = self.responses.pop(0)
        return httpx2.Response(status, json=body, headers={"request-id": "req_test"})


@pytest.fixture
def spend(db):
    return SpendLedger(db, ledger="dev", live_cap_usd=5, dev_cap_usd=10,
                       tz=ZoneInfo("America/New_York"))


def claude(spend, transport):
    client = anthropic.AsyncAnthropic(
        api_key="test-key-not-real",
        base_url="http://anthropic.invalid",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(transport)),
    )
    return ClaudeLLM(api_key="test-key-not-real", spend=spend, client=client)


async def call(llm, **kw):
    args = dict(model="claude-haiku-5-5", system="You tag garments.", purpose="tag_item",
                effort="low", json_schema=SCHEMA,
                content=[image_block(b"\xff\xd8fake", "image/jpeg"), text_block("Tag it.")])
    args.update(kw)
    return await llm.complete_structured(**args)


async def spend_rows(db):
    return await db.fetch("SELECT * FROM api_spend ORDER BY id")


async def test_request_shape_and_spend(db, spend):
    t = Transport((200, message(GOOD)))
    assert await call(claude(spend, t)) == GOOD
    req = t.requests[0]
    body = req["body"]
    assert req["url"].endswith("/v1/messages")
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    props = fmt["schema"]["properties"]
    assert "minimum" not in props["formality"] and "maxLength" not in props["pattern"]
    assert "pattern" in props  # a field named like a keyword survives
    for banned in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert banned not in body
    assert body["messages"][0]["content"][0]["type"] == "image"
    rows = await spend_rows(db)
    assert len(rows) == 1
    r = rows[0]
    assert (r["provider"], r["model"], r["purpose"], r["ledger"]) == (
        "anthropic", "claude-haiku-5-5", "tag_item", "dev")
    # 1000 * 0.10/M + 200 * 0.50/M = 0.0002
    assert r["est_usd"] == Decimal("0.000200")
    assert r["units"]["input_tokens"] == 1000 and r["units"]["request_id"] == "req_test"


async def test_schema_failure_retries_once(db, spend):
    bad = {"category": "hat", "formality": 2, "pattern": "x"}
    t = Transport((200, message(bad)), (200, message(GOOD)))
    assert await call(claude(spend, t)) == GOOD
    assert len(t.requests) == 2 and len(await spend_rows(db)) == 2


async def test_local_constraints_still_enforced(db, spend):
    out_of_range = {"category": "top", "formality": 9, "pattern": "x"}
    t = Transport((200, message(out_of_range)), (200, message("not json")))
    with pytest.raises(LLMSchemaError):
        await call(claude(spend, t))
    assert len(await spend_rows(db)) == 2  # both attempts were paid for and logged


async def test_refusal(db, spend):
    t = Transport((200, message("", stop="refusal",
                                details={"type": "refusal", "category": "bio",
                                         "explanation": "no"})))
    with pytest.raises(LLMRefusal) as ei:
        await call(claude(spend, t))
    assert ei.value.category == "bio"
    assert len(await spend_rows(db)) == 1  # refusals are still logged


async def test_truncation(db, spend):
    t = Transport((200, message('{"category": "to', stop="max_tokens")))
    with pytest.raises(LLMTruncated):
        await call(claude(spend, t))
    assert len(t.requests) == 1


async def test_blocked_spend_makes_no_call(db, spend):
    await spend.record(provider="p", model="m", purpose="x", est_usd=10)
    t = Transport()
    with pytest.raises(SpendCapExceeded):
        await call(claude(spend, t))
    assert t.requests == []


async def test_api_error_mapped(db, spend):
    t = Transport((400, {"type": "error", "error": {"type": "invalid_request_error",
                                                    "message": "bad"}}))
    with pytest.raises(LLMRequestError):
        await call(claude(spend, t))


async def test_unconfigured(spend):
    llm = ClaudeLLM(api_key="", spend=spend)
    assert not llm.configured
    with pytest.raises(LLMNotConfigured):
        await call(llm)


def test_default_base_url_ignores_environment(monkeypatch, spend):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://should-not-be-used.invalid")
    llm = ClaudeLLM(api_key="k", spend=spend)
    assert str(llm._client.base_url).startswith("https://api.anthropic.com")


def test_api_schema_nested():
    s = api_schema({"type": "object", "properties": {"items": {"type": "array", "minItems": 1,
                    "items": {"type": "object", "properties": {"id": {"type": "string"}}}}}})
    assert s["properties"]["items"]["items"]["additionalProperties"] is False
    assert "minItems" not in s["properties"]["items"]


async def test_fake_llm_scripts_and_records(db, spend):
    llm = FakeLLM([GOOD, LLMRefusal("cyber", "x")], spend=spend, cost_usd=Decimal("0.01"))
    assert await call(llm) == GOOD
    with pytest.raises(LLMRefusal):
        await call(llm)
    assert len(llm.calls) == 2 and llm.calls[0].image_count == 1
    assert "Tag it." in llm.calls[0].text
    assert len(await spend_rows(db)) == 2
    llm.push({"category": "nope"})
    with pytest.raises(LLMSchemaError):
        await call(llm)
    with pytest.raises(AssertionError):
        await call(llm)  # script exhausted
