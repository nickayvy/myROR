"""LLM providers: `LLM` protocol, `ClaudeLLM`, `FakeLLM`.

    from providers.llm import text_block, image_block
    result = await llm.complete_structured(
        model=settings.llm_tag_model, system=SYSTEM, purpose="tag_item", effort="low",
        content=[image_block(png, "image/png"), text_block("Tag this garment.")],
        json_schema=TAG_SCHEMA,
    )
"""

from __future__ import annotations

from providers.llm.base import (
    LLM,
    ContentBlock,
    Effort,
    LLMError,
    LLMNotConfigured,
    LLMRefusal,
    LLMRequestError,
    LLMSchemaError,
    LLMTruncated,
    LLMUnavailable,
    image_block,
    text_block,
)
from providers.llm.claude import ClaudeLLM
from providers.llm.fake import FakeCall, FakeLLM

__all__ = [
    "LLM",
    "ClaudeLLM",
    "ContentBlock",
    "Effort",
    "FakeCall",
    "FakeLLM",
    "LLMError",
    "LLMNotConfigured",
    "LLMRefusal",
    "LLMRequestError",
    "LLMSchemaError",
    "LLMTruncated",
    "LLMUnavailable",
    "build_llm",
    "image_block",
    "text_block",
]


def build_llm(settings, spend) -> LLM:
    if settings.llm == "fake":
        return FakeLLM(default=lambda call: {}, validate_responses=False, spend=spend)
    if settings.llm == "scripted":
        from providers.llm.scripted import build_scripted_llm

        return build_scripted_llm(spend)
    # Without a key this is an unconfigured ClaudeLLM that raises LLMNotConfigured on use.
    return ClaudeLLM(api_key=settings.anthropic_api_key, spend=spend)
