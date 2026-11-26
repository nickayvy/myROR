"""Taxonomy: value lists, slot map, tag validation, and the structured-output schema."""

from __future__ import annotations

from typing import get_args

import jsonschema
import pytest

from mirror_api import taxonomy as tx
from mirror_api.contracts import OutfitSlot
from providers.llm import FakeLLM
from providers.llm.base import api_schema

GOOD = {
    "name": "Navy crew-neck sweater", "category": "top", "subcategory": "sweater",
    "primary_color": "navy", "secondary_color": None, "pattern": "solid",
    "material_guess": "wool", "formality": 3, "season": ["winter", "fall"], "fit": "regular",
    "style_tags": ["classic", "casual"],
}


def test_slot_map_covers_contract_slots():
    assert set(tx.CATEGORY_SLOT) == set(tx.CATEGORIES)
    assert set(tx.CATEGORY_SLOT.values()) == set(get_args(OutfitSlot))
    assert set(tx.SUBCATEGORIES) == set(tx.CATEGORIES)


def test_subcategories_unique_across_categories():
    subs = tx.all_subcategories()
    assert len(subs) == len(set(subs))


def test_every_subcategory_has_a_warmth_and_tops_have_sleeves():
    for cat, subs in tx.SUBCATEGORIES.items():
        for s in subs:
            if cat != "accessory":
                assert s in tx._BASE_WARMTH, s
            if cat == "top":
                assert s in tx._SLEEVE, s


def test_formality_has_five_levels_with_meanings():
    assert sorted(tx.FORMALITY) == [1, 2, 3, 4, 5]
    assert all(tx.FORMALITY[k] for k in tx.FORMALITY)


def test_validate_good_tags_normalises():
    out = tx.validate_tags({**GOOD, "category": " Top ", "primary_color": "NAVY"})
    assert out["category"] == "top" and out["primary_color"] == "navy"
    assert out["season"] == ["fall", "winter"]  # taxonomy order, unique
    assert out["style_tags"] == ["casual", "classic"]
    assert out["name"] == "Navy crew-neck sweater"


@pytest.mark.parametrize("patch, needle", [
    ({"category": "dresses"}, "category 'dresses' is not allowed"),
    ({"subcategory": "kilt"}, "subcategory 'kilt' is not allowed for category 'top'"),
    ({"subcategory": "jeans"}, "subcategory 'jeans' is not allowed for category 'top'"),
    ({"primary_color": "chartreuse"}, "primary_color 'chartreuse' is not allowed"),
    ({"pattern": "paisley"}, "pattern 'paisley' is not allowed"),
    ({"material_guess": "vibranium"}, "material_guess 'vibranium'"),
    ({"formality": 6}, "formality 6 is not allowed"),
    ({"formality": "3"}, "formality '3' is not allowed"),
    ({"formality": True}, "formality True is not allowed"),
    ({"season": ["summer", "monsoon"]}, "season has value(s) not allowed ['monsoon']"),
    ({"season": ["summer", "summer"]}, "season has duplicates"),
    ({"season": "summer"}, "season must be a list"),
    ({"style_tags": ["casual", "classic", "edgy", "preppy", "vintage"]}, "at most 4"),
    ({"fit": "baggy"}, "fit 'baggy' is not allowed"),
    ({"colour": "navy"}, "unknown field(s) ['colour']"),
    ({"category": None}, "missing required field(s) ['category']"),
    ({"name": "x" * 61}, "at most 60"),
])
def test_validate_rejects_precisely(patch, needle):
    with pytest.raises(tx.TaxonomyError) as e:
        tx.validate_tags({**GOOD, **patch})
    assert any(needle in m for m in e.value.errors), e.value.errors


def test_validate_reports_every_problem_at_once():
    with pytest.raises(tx.TaxonomyError) as e:
        tx.validate_tags({**GOOD, "pattern": "paisley", "fit": "baggy", "formality": 0})
    assert len(e.value.errors) == 3


def test_validate_non_dict():
    with pytest.raises(tx.TaxonomyError):
        tx.validate_tags(["top"])


def test_optional_fields_default():
    out = tx.validate_tags({k: GOOD[k] for k in tx.REQUIRED_FIELDS})
    assert out["secondary_color"] is None and out["fit"] is None
    assert out["season"] == [] and out["style_tags"] == []


def test_schema_is_valid_and_structured_output_safe():
    schema = tx.item_tags_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    sent = api_schema(schema)
    unsupported = {"minimum", "maximum", "minItems", "maxItems", "uniqueItems", "minLength",
                   "maxLength", "pattern", "multipleOf"}

    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "properties":
                    for name, sub in v.items():
                        walk(sub, f"{path}.{name}")
                    continue
                assert k not in unsupported, f"{path}: {k}"
                walk(v, path)
        elif isinstance(node, list):
            for v in node:
                walk(v, path)

    walk(schema)
    assert sent == schema or walk(sent) is None
    assert sent["additionalProperties"] is False
    assert set(sent["required"]) == set(sent["properties"])
    jsonschema.validate(GOOD, schema)


async def test_schema_round_trips_through_fake_llm():
    llm = FakeLLM([GOOD])
    out = await llm.complete_structured(
        model="m", system="s", content=[], json_schema=tx.item_tags_schema(), purpose="t",
        effort="low")
    assert tx.validate_tags(out)["subcategory"] == "sweater"


def test_schema_without_name():
    assert "name" not in tx.item_tags_schema(include_name=False)["properties"]


@pytest.mark.parametrize("sub, mat, expected", [
    ("tank_top", "linen", 0), ("t_shirt", "cotton", 1), ("sweater", "wool", 3),
    ("puffer", "down", 3), ("shirt", "linen", 0), ("mystery", None, 1),
])
def test_warmth(sub, mat, expected):
    assert tx.warmth(sub, mat) == expected


def test_derived_rain_and_layers():
    assert tx.rain_unsuitable("shoes", "suede", "ankle_boots")
    assert tx.rain_unsuitable("shoes", "canvas", "sneakers")
    assert not tx.rain_unsuitable("shoes", "leather", "boots")
    assert tx.is_rain_layer("outerwear", "raincoat", "polyester")
    assert tx.is_rain_layer("outerwear", "jacket", "nylon")
    assert not tx.is_rain_layer("outerwear", "blazer", "wool")
    assert tx.layer_role("top", "cardigan") == "mid"
    assert tx.layer_role("top", "t_shirt") == "base"
    assert tx.layer_role("bottom", "jeans") is None


def test_tagger_guide_is_compact():
    g = tx.tagger_guide()
    assert "outerwear:" in g and "5 formal" in g
    assert len(g) < 2000
