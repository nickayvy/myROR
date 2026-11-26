"""Closet taxonomy: the fixed value lists every item tag must come from (HANDOFF section 10).

Owned by the recommender; imported by the tagging pipeline. One wardrobe, any gender:
the lists cover menswear and womenswear basics and stay short on purpose. Adding a
value is safe; renaming or removing one needs a data migration for existing items.

For the tagger:

    from mirror_api.taxonomy import item_tags_schema, validate_tags, tagger_guide, TaxonomyError

    raw = await llm.complete_structured(..., json_schema=item_tags_schema(),
                                         system=... + tagger_guide())
    try:
        tags = validate_tags(raw)          # normalised dict, ready for the items columns
    except TaxonomyError as e:
        ...retry once, quoting e.errors...

`item_tags_schema()` is a flat schema in the subset structured outputs accept (enums,
anyOf with null, no numeric or length constraints). It cannot express "subcategory must
belong to category"; `validate_tags` enforces that and every other rule the schema
can't (unique seasons, at most 4 style tags, formality 1-5).

Derived attributes (warmth, sleeve length, layer role, light fabric, rain handling) are
functions of the stored tags, so they never go out of date and never need a column.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from mirror_api.contracts import OutfitSlot

# ---- value lists -------------------------------------------------------------------

CATEGORIES: tuple[str, ...] = ("top", "bottom", "one_piece", "outerwear", "shoes", "accessory")

SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "top": (
        "t_shirt", "long_sleeve_tee", "tank_top", "polo", "shirt", "short_sleeve_shirt",
        "blouse", "turtleneck", "sweater", "cardigan", "sweatshirt", "hoodie",
    ),
    "bottom": ("jeans", "trousers", "chinos", "shorts", "skirt", "leggings", "joggers"),
    "one_piece": ("dress", "jumpsuit", "romper", "overalls"),
    "outerwear": (
        "blazer", "denim_jacket", "leather_jacket", "jacket", "fleece", "vest",
        "raincoat", "windbreaker", "trench_coat", "coat", "puffer", "parka",
    ),
    "shoes": (
        "sneakers", "running_shoes", "loafers", "dress_shoes", "boots", "ankle_boots",
        "rain_boots", "heels", "flats", "sandals",
    ),
    "accessory": (
        "scarf", "hat", "cap", "beanie", "gloves", "belt", "tie", "bag", "sunglasses",
        "jewelry", "watch",
    ),
}

# Named colour buckets. Lab values are measured from pixels, not chosen by the model.
COLORS: tuple[str, ...] = (
    "black", "charcoal", "grey", "white", "cream", "beige", "tan", "brown", "navy", "blue",
    "light_blue", "teal", "green", "olive", "yellow", "orange", "red", "burgundy", "pink",
    "purple", "gold", "silver", "multicolor",
)
NEUTRAL_COLORS: frozenset[str] = frozenset(
    {"black", "charcoal", "grey", "white", "cream", "beige", "tan", "brown", "navy"}
)

PATTERNS: tuple[str, ...] = (
    "solid", "striped", "checked", "floral", "polka_dot", "graphic", "animal", "camo",
    "geometric", "colorblock", "textured", "other",
)

MATERIALS: tuple[str, ...] = (
    "cotton", "linen", "silk", "satin", "wool", "cashmere", "knit", "fleece", "denim",
    "corduroy", "leather", "suede", "canvas", "rubber", "nylon", "polyester", "down",
    "synthetic", "unknown",
)

SEASONS: tuple[str, ...] = ("spring", "summer", "fall", "winter")
# An empty season list, or all four, means "all season".

FITS: tuple[str, ...] = ("fitted", "slim", "regular", "relaxed", "oversized")

STYLE_TAGS: tuple[str, ...] = (
    "casual", "smart_casual", "business", "formal", "sporty", "streetwear", "minimalist",
    "classic", "preppy", "bohemian", "vintage", "edgy", "outdoor", "lounge",
)
MAX_STYLE_TAGS = 4

FORMALITY: dict[int, str] = {
    1: "lounge or athletic: sweats, gym wear, slides",
    2: "casual: tees, jeans, sneakers",
    3: "smart casual: collared or knit tops, chinos, clean leather shoes",
    4: "business: tailored trousers or skirts, blazers, dress shoes",
    5: "formal: suits, cocktail or evening wear",
}

# ---- slots ---------------------------------------------------------------------------

CATEGORY_SLOT: dict[str, OutfitSlot] = {
    "top": "top",
    "bottom": "bottom",
    "one_piece": "one_piece",
    "outerwear": "outer",
    "shoes": "shoes",
    "accessory": "accessory",
}
SLOTS: tuple[str, ...] = get_args(OutfitSlot)


def slot_for(category: str | None) -> OutfitSlot | None:
    return CATEGORY_SLOT.get(category or "")


# ---- derived attributes -----------------------------------------------------------------

Sleeve = Literal["none", "short", "long", "unknown"]
LayerRole = Literal["base", "mid"]

_SLEEVE: dict[str, Sleeve] = {
    "t_shirt": "short", "polo": "short", "short_sleeve_shirt": "short", "tank_top": "none",
    "long_sleeve_tee": "long", "shirt": "long", "turtleneck": "long", "sweater": "long",
    "cardigan": "long", "sweatshirt": "long", "hoodie": "long", "blouse": "unknown",
    "dress": "unknown", "jumpsuit": "unknown", "romper": "short", "overalls": "unknown",
}

# Tops worn over another top. A base and a mid top can share an outfit; two bases cannot.
MID_LAYER_TOPS: frozenset[str] = frozenset({"sweater", "cardigan", "sweatshirt", "hoodie"})

# Warmth 0 (barely there) .. 3 (winter weight), before material adjustment.
_BASE_WARMTH: dict[str, int] = {
    "tank_top": 0, "t_shirt": 1, "polo": 1, "short_sleeve_shirt": 1, "blouse": 1,
    "shirt": 1, "long_sleeve_tee": 1, "turtleneck": 2, "sweater": 2, "cardigan": 2,
    "sweatshirt": 2, "hoodie": 2,
    "shorts": 0, "skirt": 1, "leggings": 1, "jeans": 2, "trousers": 2, "chinos": 1,
    "joggers": 2,
    "dress": 1, "jumpsuit": 1, "romper": 0, "overalls": 2,
    "blazer": 1, "denim_jacket": 1, "windbreaker": 1, "vest": 1, "raincoat": 1,
    "leather_jacket": 2, "jacket": 2, "fleece": 2, "trench_coat": 2, "coat": 3,
    "puffer": 3, "parka": 3,
    "sandals": 0, "flats": 1, "heels": 1, "sneakers": 1, "running_shoes": 1, "loafers": 1,
    "dress_shoes": 1, "ankle_boots": 2, "boots": 2, "rain_boots": 2,
}
_WARM_MATERIALS = frozenset({"wool", "cashmere", "fleece", "down", "corduroy"})
_COOL_MATERIALS = frozenset({"linen", "silk", "satin"})
LIGHT_MATERIALS: frozenset[str] = frozenset({"linen", "cotton", "silk", "satin"})

# Shoes that suffer in rain (HANDOFF section 10). Extend in one place.
RAIN_UNSUITABLE_SHOE_MATERIALS: frozenset[str] = frozenset({"suede", "canvas"})
RAIN_FRIENDLY_SHOES: frozenset[str] = frozenset({"rain_boots", "boots", "ankle_boots"})
RAIN_LAYER_OUTERWEAR: frozenset[str] = frozenset(
    {"raincoat", "windbreaker", "trench_coat", "parka"}
)
RAIN_LAYER_MATERIALS: frozenset[str] = frozenset({"nylon", "rubber", "polyester"})
# Bare-skin items that are a poor choice when it's cold.
OPEN_ITEMS: frozenset[str] = frozenset({"shorts", "sandals", "tank_top", "romper"})
COLD_ACCESSORIES: frozenset[str] = frozenset({"scarf", "beanie", "gloves"})


def warmth(subcategory: str | None, material: str | None) -> int:
    """0..3. Unknown subcategories count as 1 (neutral)."""
    w = _BASE_WARMTH.get(subcategory or "", 1)
    if material in _WARM_MATERIALS:
        w += 1
    elif material in _COOL_MATERIALS:
        w -= 1
    return max(0, min(3, w))


def sleeve(subcategory: str | None) -> Sleeve:
    return _SLEEVE.get(subcategory or "", "unknown")


def layer_role(category: str | None, subcategory: str | None) -> LayerRole | None:
    if category != "top":
        return None
    return "mid" if subcategory in MID_LAYER_TOPS else "base"


def is_light_fabric(material: str | None, subcategory: str | None, material_warmth: int) -> bool:
    return material in LIGHT_MATERIALS and material_warmth <= 1


def rain_unsuitable(category: str | None, material: str | None, subcategory: str | None) -> bool:
    return category == "shoes" and (
        material in RAIN_UNSUITABLE_SHOE_MATERIALS or subcategory == "sandals"
    )


def is_rain_layer(category: str | None, subcategory: str | None, material: str | None) -> bool:
    if category != "outerwear":
        return False
    return subcategory in RAIN_LAYER_OUTERWEAR or (
        material in RAIN_LAYER_MATERIALS and subcategory in {"jacket", "puffer", "coat"}
    )


# ---- validation ---------------------------------------------------------------------------

TAG_FIELDS: tuple[str, ...] = (
    "category", "subcategory", "primary_color", "secondary_color", "pattern",
    "material_guess", "formality", "season", "fit", "style_tags",
)
REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"category", "subcategory", "primary_color", "pattern", "material_guess", "formality"}
)
NAME_MAX = 60


class TaxonomyError(ValueError):
    """One message per problem, each naming the field, the bad value and what is allowed."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _choices(values) -> str:
    return ", ".join(str(v) for v in values)


def validate_tags(
    tags: Any, *, allow_name: bool = True, extra_keys: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Check a tag dict against the taxonomy and return a normalised copy.

    Normalisation: strings are lower-cased and stripped, season and style_tags become
    sorted unique lists, missing optional fields become None or []. Raises TaxonomyError
    listing every problem (unknown keys, unknown values, missing required fields,
    subcategory not belonging to category).
    """
    if not isinstance(tags, dict):
        raise TaxonomyError([f"tags must be an object, got {type(tags).__name__}"])
    errors: list[str] = []
    allowed = set(TAG_FIELDS) | set(extra_keys) | ({"name"} if allow_name else set())
    unknown = sorted(set(tags) - allowed)
    if unknown:
        errors.append(f"unknown field(s) {unknown}; allowed: {_choices(sorted(allowed))}")
    missing = sorted(f for f in REQUIRED_FIELDS if tags.get(f) in (None, ""))
    if missing:
        errors.append(f"missing required field(s) {missing}")

    out: dict[str, Any] = {k: tags[k] for k in extra_keys if k in tags}

    def norm(v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    def one_of(field: str, values: tuple[str, ...], *, required: bool) -> None:
        v = norm(tags.get(field))
        if v in (None, ""):
            out[field] = None
            return
        if not isinstance(v, str) or v not in values:
            errors.append(f"{field} {tags.get(field)!r} is not allowed; expected one of: "
                          f"{_choices(values)}")
        out[field] = v

    one_of("category", CATEGORIES, required=True)
    cat = out.get("category")
    sub = norm(tags.get("subcategory"))
    if sub in (None, ""):
        out["subcategory"] = None
    elif cat in SUBCATEGORIES:
        if sub not in SUBCATEGORIES[cat]:
            errors.append(f"subcategory {tags.get('subcategory')!r} is not allowed for category "
                          f"{cat!r}; expected one of: {_choices(SUBCATEGORIES[cat])}")
        out["subcategory"] = sub
    else:
        out["subcategory"] = sub  # category already reported
    one_of("primary_color", COLORS, required=True)
    one_of("secondary_color", COLORS, required=False)
    one_of("pattern", PATTERNS, required=True)
    one_of("material_guess", MATERIALS, required=True)
    one_of("fit", FITS, required=False)

    f = tags.get("formality")
    if f is None:
        out["formality"] = None
    elif isinstance(f, bool) or not isinstance(f, int) or f not in FORMALITY:
        errors.append(f"formality {f!r} is not allowed; expected an integer 1-5 "
                      f"(1 lounge .. 5 formal)")
        out["formality"] = f
    else:
        out["formality"] = f

    def many_of(field: str, values: tuple[str, ...], max_items: int | None) -> None:
        v = tags.get(field)
        if v is None:
            out[field] = []
            return
        if not isinstance(v, list):
            errors.append(f"{field} must be a list, got {type(v).__name__}")
            out[field] = []
            return
        items = [norm(x) for x in v]
        bad = [x for x in v if not isinstance(x, str) or norm(x) not in values]
        if bad:
            errors.append(f"{field} has value(s) not allowed {bad}; expected any of: "
                          f"{_choices(values)}")
        if len(set(items)) != len(items):
            errors.append(f"{field} has duplicates: {v}")
        uniq = sorted({x for x in items if isinstance(x, str)}, key=lambda x: (
            values.index(x) if x in values else 99, x))
        if max_items is not None and len(uniq) > max_items:
            errors.append(f"{field} has {len(uniq)} values; at most {max_items} allowed")
        out[field] = uniq

    many_of("season", SEASONS, None)
    many_of("style_tags", STYLE_TAGS, MAX_STYLE_TAGS)

    if allow_name and "name" in tags:
        name = tags["name"]
        if not isinstance(name, str) or not name.strip():
            errors.append("name must be a non-empty string")
        elif len(name.strip()) > NAME_MAX:
            errors.append(f"name is {len(name.strip())} characters; at most {NAME_MAX}")
        out["name"] = name.strip() if isinstance(name, str) else name

    if errors:
        raise TaxonomyError(errors)
    return out


# ---- JSON schema for structured outputs -------------------------------------------------------


def _enum(values, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string", "enum": list(values)}
    if description:
        node["description"] = description
    return node


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [node, {"type": "null"}]}


def all_subcategories() -> list[str]:
    return [s for subs in SUBCATEGORIES.values() for s in subs]


def item_tags_schema(*, include_name: bool = True) -> dict[str, Any]:
    """JSON schema for one item's tags, in the subset structured outputs accept.

    Every field is required (nullable where optional), which is how structured outputs
    expresses optional fields. `validate_tags` is still the source of truth.
    """
    props: dict[str, Any] = {}
    if include_name:
        props["name"] = {
            "type": "string",
            "description": "Short plain name a person would use, e.g. 'navy crew-neck sweater'"
                           f" (at most {NAME_MAX} characters).",
        }
    props.update({
        "category": _enum(CATEGORIES),
        "subcategory": _enum(all_subcategories(), "Must belong to the chosen category."),
        "primary_color": _enum(COLORS),
        "secondary_color": _nullable(_enum(COLORS)),
        "pattern": _enum(PATTERNS),
        "material_guess": _enum(MATERIALS, "Best guess from appearance; 'unknown' if unsure."),
        "formality": {
            "type": "integer",
            "enum": list(FORMALITY),
            "description": "; ".join(f"{k} = {v}" for k, v in FORMALITY.items()),
        },
        "season": {
            "type": "array",
            "items": _enum(SEASONS),
            "description": "Seasons it suits. Empty list = all season.",
        },
        "fit": _nullable(_enum(FITS)),
        "style_tags": {
            "type": "array",
            "items": _enum(STYLE_TAGS),
            "description": f"Up to {MAX_STYLE_TAGS}, most fitting first.",
        },
    })
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def tagger_guide() -> str:
    """Compact text for the tagger's system prompt: categories with subcategories and the
    formality scale. About 350 tokens."""
    lines = ["Categories and their subcategories:"]
    for cat, subs in SUBCATEGORIES.items():
        lines.append(f"- {cat}: {', '.join(subs)}")
    lines.append("Formality: " + "; ".join(f"{k} {v}" for k, v in FORMALITY.items()))
    lines.append("Season: list the seasons the item suits; [] for all-season basics.")
    lines.append("Fit: null for shoes and accessories.")
    return "\n".join(lines)
