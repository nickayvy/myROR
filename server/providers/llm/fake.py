"""Scripted LLM for tests and offline development (LLM=fake)."""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import jsonschema

from providers.llm.base import ContentBlock, Effort, LLMSchemaError, validate

Script = dict[str, Any] | Exception | Callable[["FakeCall"], dict[str, Any]]


@dataclass
class FakeCall:
    model: str
    system: str
    content: list[ContentBlock]
    json_schema: dict[str, Any]
    purpose: str
    effort: Effort
    max_tokens: int
    job_id: UUID | None = None

    @property
    def text(self) -> str:
        """All text blocks joined, for easy assertions on prompts."""
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text")

    @property
    def image_count(self) -> int:
        return sum(1 for b in self.content if b.get("type") == "image")


@dataclass
class FakeLLM:
    """Returns scripted responses in order and records every call.

        llm = FakeLLM([{"category": "top"}, LLMRefusal("cyber", "no")])
        llm.push({"category": "shoes"})
        llm.default = lambda call: {...}   # used when the script is empty

    A script entry may be a dict (returned), an Exception (raised) or a callable
    taking the FakeCall. Responses are validated against the schema (like the real
    client) unless validate_responses=False. If `spend` is set, the fake honours
    the cap and records `cost_usd` per call, so spend-mode behaviour is testable.
    """

    script: Iterable[Script] = ()
    default: Script | None = None
    spend: Any = None
    cost_usd: Decimal = Decimal("0")
    validate_responses: bool = True
    name: str = "fake"
    calls: list[FakeCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue: deque[Script] = deque(self.script)

    @property
    def configured(self) -> bool:
        return True

    def push(self, *items: Script) -> None:
        self._queue.extend(items)

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
        if self.spend is not None:
            await self.spend.check(purpose)
        call = FakeCall(
            model, system, copy.deepcopy(content), json_schema, purpose, effort, max_tokens, job_id
        )
        self.calls.append(call)
        if self._queue:
            item = self._queue.popleft()
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError(f"FakeLLM has no scripted response for purpose={purpose!r}")
        if self.spend is not None and self.cost_usd:
            await self.spend.record(
                provider="fake", model=model, purpose=purpose, est_usd=self.cost_usd, job_id=job_id
            )
        if isinstance(item, Exception):
            raise item
        result = item(call) if callable(item) else copy.deepcopy(item)
        if self.validate_responses:
            try:
                validate(result, json_schema)
            except jsonschema.ValidationError as e:
                raise LLMSchemaError(f"scripted response fails schema: {e.message}") from e
        return result
