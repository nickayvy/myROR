"""LLM protocol: one method that returns JSON matching a schema.

Content blocks use the Anthropic Messages shape, built with `text_block` and
`image_block`, so a FakeLLM sees exactly what Claude would.
"""

from __future__ import annotations

import base64
import copy
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

import jsonschema

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ContentBlock = dict[str, Any]
ImageMediaType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


class LLMError(RuntimeError):
    """Base class. Callers that only need "it failed" catch this."""


class LLMNotConfigured(LLMError):
    """No API key (or LLM disabled)."""


class LLMUnavailable(LLMError):
    """Network, rate limit, overload or 5xx after the SDK's own retries. Retryable."""


class LLMRequestError(LLMError):
    """The API rejected the request (4xx). A bug, not retryable."""


class LLMRefusal(LLMError):
    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"model refused (category={category}): {explanation or ''}".strip())
        self.category = category
        self.explanation = explanation


class LLMTruncated(LLMError):
    """stop_reason == max_tokens: the JSON is incomplete. Raise max_tokens."""


class LLMSchemaError(LLMError):
    """The response did not validate against the schema, twice."""


@runtime_checkable
class LLM(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

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
        """Return a dict that validates against json_schema.

        Raises SpendCapExceeded (before any call), LLMRefusal, LLMTruncated,
        LLMSchemaError, LLMUnavailable, LLMRequestError, LLMNotConfigured.
        `purpose` is a short label stored in the spend ledger, e.g. "tag_item".
        """
        ...


def text_block(text: str) -> ContentBlock:
    return {"type": "text", "text": text}


def image_block(data: bytes, media_type: ImageMediaType = "image/jpeg") -> ContentBlock:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


# Structured outputs reject these keywords; we strip them from what we send and
# enforce them locally with jsonschema instead.
_UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "pattern",
}


def api_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of `schema` safe to send: unsupported keywords removed, and every object
    gets additionalProperties: false (required by structured outputs)."""

    def walk(node: Any) -> Any:
        # `node` is a schema. Keys of name->schema maps (properties, $defs) are
        # field names, not keywords, so a field called "pattern" survives.
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _NAME_MAPS and isinstance(v, dict):
                out[k] = {name: walk(sub) for name, sub in v.items()}
            elif k in _UNSUPPORTED:
                continue
            elif isinstance(v, dict | list) and k not in ("enum", "const", "required"):
                out[k] = walk(v)
            else:
                out[k] = v
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
        return out

    return walk(copy.deepcopy(schema))


_NAME_MAPS = {"properties", "$defs", "definitions"}


def validate(instance: Any, schema: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if instance does not match the full schema."""
    jsonschema.validate(instance, schema, cls=jsonschema.Draft202012Validator)
