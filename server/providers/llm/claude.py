"""Claude on the official `anthropic` SDK (1.x), structured outputs only.

Request shape, per the claude-api skill for the Claude 5.5 family:
- JSON comes back through `output_config.format` (json_schema). Forced tool_choice
  returns 400 on Sonnet 5.5 / Opus 5.5, so it is not used.
- `thinking` is omitted: all three 5.5 models run adaptive thinking by default and
  reject `{"type": "disabled"}` (Opus 5.5, Sonnet 5.5) or budgets. Depth and cost are
  set with `output_config.effort`, always explicit (defaults differ per model).
- No sampling parameters (temperature/top_p/top_k return 400 on these models).
- No prefill.
- Server-side refusal fallbacks are NOT enabled: a fallback model bills at its own
  rates per `usage.iterations`, which this ledger does not yet itemise. A refusal
  surfaces as LLMRefusal and callers fall back to rules (HANDOFF section 10).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import anthropic
import jsonschema

from mirror_api.pricing import claude_cost
from providers.llm.base import (
    ContentBlock,
    Effort,
    LLMNotConfigured,
    LLMRefusal,
    LLMRequestError,
    LLMSchemaError,
    LLMTruncated,
    LLMUnavailable,
    api_schema,
    validate,
)

log = logging.getLogger(__name__)

# Pinned so a shell-exported ANTHROPIC_BASE_URL is never inherited.
DEFAULT_BASE_URL = "https://api.anthropic.com"


class ClaudeLLM:
    name = "claude"

    def __init__(
        self,
        *,
        api_key: str,
        spend,
        client: anthropic.AsyncAnthropic | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self.spend = spend
        if client is None and api_key:
            client = anthropic.AsyncAnthropic(
                api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
            )
        self._client = client

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def complete_structured(
        self,
        *,
        model: str,
        system: str,
        content: list[ContentBlock],
        json_schema: dict[str, Any],
        purpose: str,
        effort: Effort,
        max_tokens: int = 4096,
        job_id: UUID | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            raise LLMNotConfigured("ANTHROPIC_API_KEY is not set")
        sent_schema = api_schema(json_schema)
        last_error: Exception | None = None
        for attempt in (1, 2):  # one retry on a schema failure, nothing else
            await self.spend.check(purpose)
            message = await self._create(model, system, content, sent_schema, effort, max_tokens)
            await self._record(message, model, purpose, job_id, attempt)
            self._raise_for_stop(message)
            try:
                data = _parse_json(message)
                validate(data, json_schema)
                return data
            except (ValueError, jsonschema.ValidationError) as e:
                last_error = e
                log.warning("claude %s: schema failure on attempt %d: %s", purpose, attempt, e)
        raise LLMSchemaError(f"{purpose}: response failed schema twice: {last_error}")

    async def _create(self, model, system, content, schema, effort, max_tokens):
        try:
            return await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as e:
            raise LLMUnavailable(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409, 529):
                raise LLMUnavailable(f"{e.status_code}: {e.message}") from e
            raise LLMRequestError(f"{e.status_code}: {e.message}") from e

    async def _record(self, message, requested_model, purpose, job_id, attempt) -> None:
        u = message.usage
        units = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "stop_reason": message.stop_reason,
            "attempt": attempt,
            "request_id": getattr(message, "_request_id", None),
        }
        model = message.model or requested_model
        cost = claude_cost(
            model,
            input_tokens=units["input_tokens"],
            output_tokens=units["output_tokens"],
            cache_read_input_tokens=units["cache_read_input_tokens"],
            cache_creation_input_tokens=units["cache_creation_input_tokens"],
        )
        await self.spend.record(
            provider="anthropic", model=model, purpose=purpose, est_usd=cost, units=units,
            job_id=job_id,
        )

    @staticmethod
    def _raise_for_stop(message) -> None:
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusal(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )
        if message.stop_reason == "max_tokens":
            raise LLMTruncated("response hit max_tokens; JSON is incomplete")


def _parse_json(message) -> Any:
    texts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    if not texts:
        raise ValueError("no text block in response")
    return json.loads(texts[0])
