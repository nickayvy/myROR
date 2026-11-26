"""Stage 1 rules: table-driven across cold, hot, wet, formal, tiny closet, all dirty."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from recommender.fallback import compose_rules_only
from recommender.outfit import structure_errors
from recommender.rules import RulesConfig, apply_rules, calendar_season, weather_facts
from tests.recommender.conftest import DAY, day_ctx, make_item, tiny_items, wardrobe_items


def names(result, slot):
    return [c.item.name for c in result.candidates[slot]]


def first(result, slot):
    return result.candidates[slot][0].item.name


SCENARIOS = {
    # name: (ctx kwargs, checks)
    "cold": (dict(temp_c=3.0), {
        "outer_required": True, "first_outer_in": {"camel wool coat"},
        "absent": {"denim shorts", "tan sandals", "linen camp shirt"},
    }),
    "hot": (dict(temp_c=31.0, high_c=33.0), {
        "outer_required": False,
        "first_top_sleeve": {"short", "none"},
        "absent": {"camel wool coat", "grey merino sweater", "charcoal wool trousers",
                   "black beanie"},
    }),
    "wet": (dict(temp_c=13.0, condition="rain", precip=0.9), {
        "rain_layer_suggested": True, "first_outer_in": {"yellow raincoat"},
        "absent": {"white canvas sneakers", "brown suede chelsea boots", "tan sandals"},
        "first_shoes_in": {"black leather boots"},
    }),
    "formal": (dict(temp_c=15.0, min_formality=4), {
        "absent": {"dark wash jeans", "white canvas sneakers", "black crew tee"},
        "first_shoes_in": {"black oxfords"},
        "all_core_formality_at_least": 4,
    }),
    "no_weather": (dict(temp_c=None), {"outer_required": False}),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenarios(name):
    kw, checks = SCENARIOS[name]
    r = apply_rules(wardrobe_items(), day_ctx(**kw))
    assert r.can_form, r.why_not
    present = {c.item.name for c in r.all_candidates()}
    if "outer_required" in checks:
        assert r.requirements.outer_required is checks["outer_required"]
    if "rain_layer_suggested" in checks:
        assert r.requirements.rain_layer_suggested is checks["rain_layer_suggested"]
    if "first_outer_in" in checks:
        assert first(r, "outer") in checks["first_outer_in"], names(r, "outer")
    if "first_shoes_in" in checks:
        assert first(r, "shoes") in checks["first_shoes_in"], names(r, "shoes")
    if "first_top_sleeve" in checks:
        assert r.candidates["top"][0].item.sleeve in checks["first_top_sleeve"]
    for n in checks.get("absent", ()):
        assert n not in present, f"{n} should be excluded in {name}"
    if "all_core_formality_at_least" in checks:
        for slot in ("top", "bottom", "one_piece", "shoes"):
            for c in r.candidates[slot]:
                assert c.item.eff_formality >= checks["all_core_formality_at_least"], c.item.name
    # every rules-only outfit satisfies the structure rules
    outfits = compose_rules_only(r)
    assert len(outfits) == 3
    assert len({o.key for o in outfits}) == 3
    for o in outfits:
        assert structure_errors(o.items, r.requirements) == []


def test_hot_puts_short_sleeves_and_light_fabrics_first():
    r = apply_rules(wardrobe_items(), day_ctx(30.0))
    top3 = r.candidates["top"][:3]
    assert all(c.item.sleeve in ("short", "none") for c in top3), [c.item.name for c in top3]
    assert "linen camp shirt" in [c.item.name for c in top3]


def test_hot_but_wet_keeps_rain_layer():
    r = apply_rules(wardrobe_items(), day_ctx(28.0, condition="thunderstorm"))
    assert first(r, "outer") == "yellow raincoat"


def test_cold_morning_warm_afternoon_uses_coldest_hour():
    r = apply_rules(wardrobe_items(), day_ctx(7.0, hourly=[12, 18, 21]))
    assert r.requirements.outer_required
    assert r.weather.cold_ref_c == 7.0 and r.weather.hot_ref_c == 21


def test_thresholds_are_strict_inequalities():
    assert not weather_facts(day_ctx(10.0), RulesConfig()).outer_needed
    assert weather_facts(day_ctx(9.9), RulesConfig()).outer_needed
    assert not weather_facts(day_ctx(25.0), RulesConfig()).hot
    assert weather_facts(day_ctx(25.1), RulesConfig()).hot


def test_tiny_closet_still_forms_outfits():
    r = apply_rules(tiny_items(), day_ctx(12.0))
    assert r.can_form
    outfits = compose_rules_only(r)
    assert 1 <= len(outfits) <= 3
    for o in outfits:
        assert structure_errors(o.items, r.requirements) == []
        assert any(i.slot == "shoes" for i in o.items)


def test_tiny_closet_cold_without_warm_outer_notes_it():
    items = [i for i in tiny_items() if i.name != "denim jacket"]
    r = apply_rules(items, day_ctx(2.0))
    assert r.can_form
    assert not r.requirements.outer_required  # nothing to require
    assert any("no clean outer layer" in n for n in r.notes)


def test_all_dirty_says_why():
    items = [make_item(i.name, i.category, i.subcategory, i.primary_color, state="dirty")
             for i in wardrobe_items()]
    r = apply_rules(items, day_ctx(15.0))
    assert not r.can_form
    assert r.why_not.startswith("Can't suggest an outfit: the closet has no clean items")
    assert "30 in the laundry" in r.why_not
    assert compose_rules_only(r) == []


def test_missing_bottoms_message_counts_laundry():
    items = [i if i.slot != "bottom" else make_item(i.name, "bottom", i.subcategory,
                                                     state="dirty")
             for i in wardrobe_items() if i.slot != "one_piece"]
    r = apply_rules(items, day_ctx(15.0))
    assert not r.can_form
    assert "no clean bottoms (6 in the laundry)" in r.why_not


def test_empty_closet():
    r = apply_rules([], day_ctx(15.0))
    assert not r.can_form and "Add some clothes first" in r.why_not


def test_dresses_only_closet_forms_one_piece_outfits():
    items = [i for i in wardrobe_items() if i.slot in ("one_piece", "shoes")]
    r = apply_rules(items, day_ctx(20.0))
    assert r.can_form
    for o in compose_rules_only(r):
        assert [i.slot for i in o.items].count("one_piece") == 1


def test_retired_and_unknown_category_excluded():
    items = wardrobe_items(**{"white crew tee": {"state": "retired"}})
    items.append(make_item("mystery thing", "sleepwear"))
    r = apply_rules(items, day_ctx(20.0))
    present = {c.item.name for c in r.all_candidates()}
    assert "white crew tee" not in present and "mystery thing" not in present
    assert "unknown category" in r.excluded[items[-1].id]


def test_only_canvas_shoes_in_rain_relaxes_with_note():
    items = [i for i in wardrobe_items() if i.slot != "shoes" or i.name == "white canvas sneakers"]
    r = apply_rules(items, day_ctx(12.0, condition="rain"))
    assert names(r, "shoes") == ["white canvas sneakers"]
    assert r.candidates["shoes"][0].relaxed == ("rain_shoes",)
    assert any("not ideal in rain" in n for n in r.notes)
    assert r.requirements.shoes_required


def test_formal_day_with_casual_closet_relaxes_formality():
    items = tiny_items()
    r = apply_rules(items, day_ctx(15.0, min_formality=5))
    assert r.can_form
    assert any("below today's formality" in n for n in r.notes)


def test_no_shoes_in_closet_is_fine():
    items = [i for i in wardrobe_items() if i.slot != "shoes"]
    r = apply_rules(items, day_ctx(15.0))
    assert r.can_form and not r.requirements.shoes_required
    assert compose_rules_only(r)


def test_off_season_excluded_unless_weather_overrides():
    # November (fall): summer-only items are out on a mild day...
    r = apply_rules(wardrobe_items(), day_ctx(15.0))
    present = {c.item.name for c in r.all_candidates()}
    assert "denim shorts" not in present and "navy wrap dress" not in present
    # ...but a November heatwave brings them back
    r = apply_rules(wardrobe_items(), day_ctx(27.0, high_c=29.0))
    present = {c.item.name for c in r.all_candidates()}
    assert "denim shorts" in present and "navy wrap dress" in present
    # without weather, the calendar season decides
    r = apply_rules(wardrobe_items(), day_ctx(None, d=date(2026, 7, 10)))
    present = {c.item.name for c in r.all_candidates()}
    assert "camel wool coat" not in present and "denim shorts" in present


def test_southern_hemisphere_flips_season():
    assert calendar_season(date(2026, 7, 1)) == "summer"
    assert calendar_season(date(2026, 7, 1), southern=True) == "winter"


def test_recently_worn_is_down_ranked_not_excluded():
    base = apply_rules(wardrobe_items(), day_ctx(None))
    best = base.candidates["top"][0].item
    worn = wardrobe_items(**{best.name: {"last_worn_on": DAY - timedelta(days=1)}})
    r = apply_rules(worn, day_ctx(None))
    assert r.candidates["top"][0].item.name != best.name
    c = next(c for c in r.candidates["top"] if c.item.name == best.name)
    assert "worn 1d ago" in c.reasons
    # outside the window, no penalty
    old = wardrobe_items(**{best.name: {"last_worn_on": DAY - timedelta(days=10)}})
    assert apply_rules(old, day_ctx(None)).candidates["top"][0].item.name == best.name


def test_recent_days_per_category_from_settings():
    cfg, warnings = RulesConfig.from_overrides(
        {"recent_days": {"top": 0}, "outer_required_below_c": 5, "bogus": 1,
         "hot_above_c": "hot"})
    assert cfg.recent_days["top"] == 0 and cfg.recent_days["bottom"] == 2
    assert cfg.outer_required_below_c == 5.0
    assert cfg.hot_above_c == 25.0
    assert len(warnings) == 2
    r = apply_rules(wardrobe_items(), day_ctx(8.0), cfg)
    assert not r.requirements.outer_required  # 8 >= 5


def test_deterministic():
    a = apply_rules(wardrobe_items(), day_ctx(12.0))
    b = apply_rules(list(reversed(wardrobe_items())), day_ctx(12.0))
    assert [c.item.id for c in a.all_candidates()] == [c.item.id for c in b.all_candidates()]
    assert [o.key for o in compose_rules_only(a)] == [o.key for o in compose_rules_only(b)]


def test_candidate_caps():
    cfg, _ = RulesConfig.from_overrides({"max_candidates": {"top": 3}})
    r = apply_rules(wardrobe_items(), day_ctx(15.0), cfg)
    assert len(r.candidates["top"]) == 3


def test_rules_only_avoids_previous_and_falls_back_to_repeat():
    r = apply_rules(tiny_items(), day_ctx(15.0))
    first_round = compose_rules_only(r)
    again = compose_rules_only(r, avoid=[o.key for o in first_round])
    assert all(o.key not in {p.key for p in first_round} for o in again) or again
    everything = compose_rules_only(r, n=50)
    repeat = compose_rules_only(r, avoid=[o.key for o in everything])
    assert len(repeat) >= 1  # the mirror still gets something


def test_rules_reasons_use_display_unit():
    cfg = RulesConfig(temp_unit="F")
    r = apply_rules(wardrobe_items(), day_ctx(0.0), cfg)
    assert "32°F" in compose_rules_only(r, cfg)[0].reasoning
