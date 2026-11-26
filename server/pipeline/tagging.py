"""Tag one garment cutout with Claude (settings.llm_tag_model) against the taxonomy.

Image: the cutout composited on white, at most 448 px on the long edge, JPEG q85, so at most
~268 image tokens (Claude bills roughly width*height/750). Prompt: ~180 tokens of
instructions + ~350 of taxonomy guide; the output schema adds a few hundred more. Estimate
about 1,100-1,300 input and 120-250 output tokens per item at effort "low": roughly $0.0002
per item on Haiku 5.5 ($0.10/$0.50 per MTok); a retry doubles that for the item.

Flow: one call; validate with mirror_api.taxonomy.validate_tags; on errors one retry that
quotes the errors; if still invalid, keep the fields that passed. Refusals and failures are
not retried and the item still reaches the inbox, marked for manual tagging.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mirror_api.taxonomy import (
    NAME_MAX,
    TAG_FIELDS,
    TaxonomyError,
    item_tags_schema,
    tagger_guide,
    validate_tags,
)
from providers.llm import (
    LLMNotConfigured,
    LLMRefusal,
    LLMRequestError,
    LLMSchemaError,
    LLMTruncated,
    image_block,
    text_block,
)

log = logging.getLogger(__name__)

CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}
LOW_CONFIDENCE = 0.5

SYSTEM = f"""You tag clothing for a personal wardrobe app. The image shows one item the owner \
photographed on a white sheet, with the background removed.

Describe only the item itself: its kind, colours, pattern, likely material, formality, the \
seasons it suits, its fit and style. Choose every value from the allowed lists. The name is a \
short plain description a person would use, such as "navy crew-neck sweater" (at most \
{NAME_MAX} characters); never include brand names you cannot read on the item.

Set confidence to "low" when the image is unclear, the item is partly cut off, or you are \
guessing the category. Set single_item to false if the image shows no clothing item or more \
than one; still give your best tags for the most prominent item.

{tagger_guide()}"""


def tag_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tags": item_tags_schema(include_name=True),
            "confidence": {"type": "string", "enum": list(CONFIDENCE)},
            "single_item": {"type": "boolean"},
        },
        "required": ["tags", "confidence", "single_item"],
        "additionalProperties": False,
    }


@dataclass
class TagOutcome:
    status: str  # ok | partial | refused | failed
    tags: dict[str, Any] = field(default_factory=dict)  # validated, or the fields that passed
    confidence: float | None = None
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    single_item: bool | None = None

    @property
    def needs_review(self) -> bool:
        return self.status != "ok" or (self.confidence or 0) < LOW_CONFIDENCE \
            or self.single_item is False

    def notes(self, model: str) -> dict[str, Any]:
        return {"status": self.status, "errors": self.errors[:10], "attempts": self.attempts,
                "model": model, "single_item": self.single_item,
                "needs_review": self.needs_review}


def partial_tags(raw: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """The fields of `raw` no error mentions, normalised the way validate_tags would."""
    bad = {e.split()[0] for e in errors}
    if any(e.startswith("unknown field") for e in errors):
        bad.add("unknown")
    keep = {k: v for k, v in raw.items() if k in (*TAG_FIELDS, "name") and k not in bad}
    out: dict[str, Any] = {}
    for k, v in keep.items():
        if isinstance(v, str):
            out[k] = v.strip().lower() if k != "name" else v.strip()[:NAME_MAX]
        else:
            out[k] = v
    if "subcategory" in out and "category" not in out:
        out.pop("subcategory")  # cannot vouch for it without its category
    return out


async def tag_image(llm: Any, model: str, jpeg: bytes, *, job_id: UUID | None = None,
                    purpose: str = "tag_item") -> TagOutcome:
    schema = tag_schema()
    content = [image_block(jpeg, "image/jpeg"), text_block("Tag this item.")]
    outcome = TagOutcome(status="failed")
    raw: dict[str, Any] | None = None
    for attempt in (1, 2):
        outcome.attempts = attempt
        try:
            result = await llm.complete_structured(
                model=model, system=SYSTEM, content=content, json_schema=schema,
                purpose=purpose, effort="low", max_tokens=2048, job_id=job_id,
            )
        except LLMRefusal as e:
            log.info("tagger refused: %s", e)
            return TagOutcome("refused", partial_tags(raw or {}, outcome.errors),
                              errors=[str(e)], attempts=attempt)
        except (LLMSchemaError, LLMTruncated, LLMRequestError, LLMNotConfigured) as e:
            log.warning("tagging failed: %s", e)
            return TagOutcome("failed", partial_tags(raw or {}, outcome.errors),
                              errors=[f"{type(e).__name__}: {e}"], attempts=attempt)
        raw = result.get("tags") if isinstance(result.get("tags"), dict) else {}
        outcome.confidence = CONFIDENCE.get(result.get("confidence"), None)
        outcome.single_item = result.get("single_item")
        try:
            outcome.tags = validate_tags(raw)
            outcome.status = "ok"
            outcome.errors = []
            return outcome
        except TaxonomyError as e:
            outcome.errors = e.errors
            if attempt == 1:
                content = [
                    image_block(jpeg, "image/jpeg"),
                    text_block("Tag this item."),
                    text_block("Your previous answer was rejected:\n"
                               + json.dumps(raw, sort_keys=True)
                               + "\nProblems:\n- " + "\n- ".join(e.errors)
                               + "\nAnswer again using only allowed values."),
                ]
    outcome.status = "partial"
    outcome.tags = partial_tags(raw or {}, outcome.errors)
    return outcome
