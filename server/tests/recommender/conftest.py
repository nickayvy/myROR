"""Recommender fixtures: a synthetic closet (pure items and seeded rows with generated
cutout PNGs), DayContext builders, a fake integrations.context, and a FakeLLM script
that answers like a well-behaved composer."""

from __future__ import annotations

import io
import re
import sys
import types
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from PIL import Image, ImageDraw

from mirror_api.contracts import CalendarEvent, DayContext, HourlyWeather, WeatherSnapshot
from recommender.closet import ClosetItem

DAY = date(2026, 11, 25)  # a Wednesday in fall
NS = uuid.UUID("7d7c1f0e-0000-4000-8000-000000000000")

# name, category, subcategory, primary, pattern, material, formality, season, extra
WARDROBE: list[tuple] = [
    ("white crew tee", "top", "t_shirt", "white", "solid", "cotton", 2, [], {}),
    ("black crew tee", "top", "t_shirt", "black", "solid", "cotton", 2, [], {}),
    ("striped breton tee", "top", "long_sleeve_tee", "navy", "striped", "cotton", 2, [], {}),
    ("light blue oxford shirt", "top", "shirt", "light_blue", "solid", "cotton", 3, [], {}),
    ("white dress shirt", "top", "shirt", "white", "solid", "cotton", 4, [], {}),
    ("linen camp shirt", "top", "short_sleeve_shirt", "beige", "solid", "linen", 2,
     ["spring", "summer"], {}),
    ("grey merino sweater", "top", "sweater", "grey", "solid", "wool", 3,
     ["fall", "winter"], {}),
    ("navy cardigan", "top", "cardigan", "navy", "solid", "knit", 3, [], {}),
    ("black hoodie", "top", "hoodie", "black", "solid", "cotton", 1, [], {}),
    ("silk blouse", "top", "blouse", "cream", "solid", "silk", 4, [], {}),
    ("dark wash jeans", "bottom", "jeans", "navy", "solid", "denim", 2, [], {}),
    ("black jeans", "bottom", "jeans", "black", "solid", "denim", 2, [], {}),
    ("khaki chinos", "bottom", "chinos", "beige", "solid", "cotton", 3, [], {}),
    ("charcoal wool trousers", "bottom", "trousers", "charcoal", "solid", "wool", 4,
     ["fall", "winter"], {}),
    ("denim shorts", "bottom", "shorts", "light_blue", "solid", "denim", 1,
     ["summer"], {}),
    ("black midi skirt", "bottom", "skirt", "black", "solid", "polyester", 3, [], {}),
    ("navy wrap dress", "one_piece", "dress", "navy", "floral", "polyester", 3,
     ["spring", "summer"], {}),
    ("black shift dress", "one_piece", "dress", "black", "solid", "wool", 4, [], {}),
    ("navy blazer", "outerwear", "blazer", "navy", "solid", "wool", 4, [], {}),
    ("denim jacket", "outerwear", "denim_jacket", "blue", "solid", "denim", 2,
     ["spring", "fall"], {}),
    ("camel wool coat", "outerwear", "coat", "tan", "solid", "wool", 4,
     ["fall", "winter"], {}),
    ("yellow raincoat", "outerwear", "raincoat", "yellow", "solid", "nylon", 2, [], {}),
    ("white canvas sneakers", "shoes", "sneakers", "white", "solid", "canvas", 2, [], {}),
    ("brown suede chelsea boots", "shoes", "ankle_boots", "brown", "solid", "suede", 3,
     ["fall", "winter"], {}),
    ("black leather boots", "shoes", "boots", "black", "solid", "leather", 3, [], {}),
    ("black oxfords", "shoes", "dress_shoes", "black", "solid", "leather", 4, [], {}),
    ("tan sandals", "shoes", "sandals", "tan", "solid", "leather", 1, ["summer"], {}),
    ("grey wool scarf", "accessory", "scarf", "grey", "solid", "wool", 3,
     ["fall", "winter"], {}),
    ("brown leather belt", "accessory", "belt", "brown", "solid", "leather", 3, [], {}),
    ("black beanie", "accessory", "beanie", "black", "solid", "knit", 1,
     ["fall", "winter"], {}),
]
assert len(WARDROBE) == 30


def item_id(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


def make_item(name: str, category: str, subcategory: str | None = None, color: str = "black",
              pattern: str = "solid", material: str = "cotton", formality: int | None = 2,
              season=(), **kw) -> ClosetItem:
    return ClosetItem(
        id=kw.pop("id", item_id(name)), name=name, category=category, subcategory=subcategory,
        primary_color=color, pattern=pattern, material=material, formality=formality,
        season=tuple(season), cutout_key=kw.pop("cutout_key", f"cutouts/{item_id(name)}.png"),
        updated_at=kw.pop("updated_at", datetime(2026, 11, 1, tzinfo=UTC)), **kw,
    )


def wardrobe_items(**overrides) -> list[ClosetItem]:
    """The 30-item closet. overrides: name -> dict of ClosetItem fields."""
    out = []
    for name, cat, sub, color, pat, mat, f, season, extra in WARDROBE:
        kw = {**extra, **overrides.get(name, {})}
        out.append(make_item(name, cat, sub, color, pat, mat, f, season, **kw))
    return out


TINY = ["white crew tee", "grey merino sweater", "dark wash jeans", "khaki chinos",
        "white canvas sneakers", "denim jacket"]


def tiny_items(**overrides) -> list[ClosetItem]:
    return [i for i in wardrobe_items(**overrides) if i.name in TINY]


def weather(temp_c: float, *, high_c: float | None = None, hourly: list[float] | None = None,
            condition: str = "clear", precip: float | None = 0.0,
            summary: str | None = None) -> WeatherSnapshot:
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    return WeatherSnapshot(
        fetched_at=now, temp_c=temp_c, feels_like_c=temp_c, high_c=high_c,
        precip_probability=precip, condition=condition, summary=summary,
        hourly=[HourlyWeather(time=now + timedelta(hours=i + 1), temp_c=t)
                for i, t in enumerate(hourly or [])],
    )


def day_ctx(temp_c: float | None = None, *, min_formality: int = 1, d: date = DAY,
            events: list[CalendarEvent] | None = None, **wkw) -> DayContext:
    return DayContext(
        date=d, weather=None if temp_c is None else weather(temp_c, **wkw),
        events=events or [], min_formality=min_formality,
    )


# ---- cutout PNGs -------------------------------------------------------------------------------

RGB = {
    "black": (30, 30, 32), "charcoal": (64, 64, 68), "grey": (140, 140, 145),
    "white": (250, 250, 250), "cream": (240, 230, 205), "beige": (215, 195, 160),
    "tan": (190, 150, 105), "brown": (110, 72, 45), "navy": (30, 45, 90),
    "blue": (50, 95, 170), "light_blue": (150, 185, 225), "yellow": (240, 200, 40),
    "green": (60, 130, 70), "red": (190, 40, 40),
}


def cutout_png(item: ClosetItem) -> bytes:
    """A flat garment silhouette on a transparent background."""
    w, h = 400, 500
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fill = RGB.get(item.primary_color or "", (120, 120, 120)) + (255,)
    edge = (90, 90, 90, 255)
    slot = item.slot
    if slot == "top":
        d.polygon([(110, 60), (290, 60), (380, 150), (330, 200), (300, 170), (300, 460),
                   (100, 460), (100, 170), (70, 200), (20, 150)], fill=fill, outline=edge)
    elif slot == "bottom":
        d.polygon([(110, 40), (290, 40), (320, 470), (215, 470), (200, 180), (185, 470),
                   (80, 470)], fill=fill, outline=edge)
    elif slot == "one_piece":
        d.polygon([(140, 30), (260, 30), (280, 160), (360, 480), (40, 480), (120, 160)],
                  fill=fill, outline=edge)
    elif slot == "outer":
        d.polygon([(100, 40), (300, 40), (390, 140), (360, 470), (40, 470), (10, 140)],
                  fill=fill, outline=edge)
        d.line([(200, 40), (200, 470)], fill=edge, width=4)
    elif slot == "shoes":
        d.rounded_rectangle((40, 300, 360, 420), radius=50, fill=fill, outline=edge)
        d.rectangle((40, 220, 170, 330), fill=fill, outline=edge)
    else:
        d.ellipse((80, 120, 320, 380), fill=fill, outline=edge, width=4)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


async def seed_items(db, storage, items: list[ClosetItem], *, cutouts: bool = True,
                     skip_cutout: set[str] = frozenset()) -> None:
    for it in items:
        key = it.cutout_key if (cutouts and it.name not in skip_cutout) else None
        await db.execute(
            "INSERT INTO items (id, name, category, subcategory, primary_color, pattern,"
            " material_guess, formality, season, fit, style_tags, cutout_key, state,"
            " last_worn_on, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (it.id, it.name, it.category, it.subcategory, it.primary_color, it.pattern,
             it.material, it.formality, list(it.season), it.fit, list(it.style_tags), key,
             it.state, it.last_worn_on, it.updated_at),
        )
        if key and storage is not None:
            await storage.put(key, cutout_png(it), "image/png")


@pytest.fixture
def closet() -> list[ClosetItem]:
    return wardrobe_items()


@pytest.fixture
async def seeded(db, storage) -> list[ClosetItem]:
    items = wardrobe_items()
    await seed_items(db, storage, items, skip_cutout={"brown leather belt"})
    return items


# ---- fake integrations.context -------------------------------------------------------------------


class FakeContext:
    def __init__(self) -> None:
        self.ctx_for: Callable[[date], DayContext] = lambda d: day_ctx(8.0, d=d)
        self.calls = 0
        self.error: Exception | None = None

    def set(self, **kw) -> None:
        self.ctx_for = lambda d: day_ctx(d=d, **kw)

    async def get_day_context(self, services, for_date: date) -> DayContext:
        self.calls += 1
        if self.error:
            raise self.error
        return self.ctx_for(for_date)


@pytest.fixture
def fake_context(monkeypatch) -> FakeContext:
    fc = FakeContext()
    mod = types.ModuleType("integrations.context")
    mod.get_day_context = fc.get_day_context
    monkeypatch.setitem(sys.modules, "integrations.context", mod)
    return fc


@pytest.fixture
def no_integrations(monkeypatch) -> None:
    """Make integrations.context and integrations.today look absent, whatever the other
    engineer has written so far."""
    monkeypatch.setitem(sys.modules, "integrations.context", None)
    monkeypatch.setitem(sys.modules, "integrations.today", None)


# ---- a well-behaved composer ---------------------------------------------------------------------

ROW = re.compile(r"^([TBDOSA]\d+) \| (.*)$", re.M)


def table_rows(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for code, rest in ROW.findall(text):
        out[code] = [c.strip() for c in rest.split("|")]
    return out


def good_answer(call) -> dict:
    """Pick 3 different valid outfits from the table in the prompt, like a sane model."""
    rows = table_rows(call.text)
    codes = list(rows)
    base_tops = [c for c in codes if c[0] == "T" and rows[c][8] == "base"]
    mids = [c for c in codes if c[0] == "T" and rows[c][8] == "mid"]
    tops = base_tops or mids
    bottoms = [c for c in codes if c[0] == "B"]
    ones = [c for c in codes if c[0] == "D"]
    shoes = [c for c in codes if c[0] == "S"]
    outers = [c for c in codes if c[0] == "O"]
    need_outer = "outer layer is required" in call.text
    m = re.search(r"Already suggested today \(choose different combinations\): (.*)", call.text)
    seen = [sorted(c.split("+")) for c in m.group(1).split(", ")] if m else []
    outfits = []
    k = 0
    while len(outfits) < 3 and k < 40:
        if tops and bottoms:
            items = [tops[k % len(tops)], bottoms[(k // max(len(tops), 1) + k) % len(bottoms)]]
        elif ones:
            items = [ones[k % len(ones)]]
        else:
            break
        if shoes:
            items.append(shoes[k % len(shoes)])
        if need_outer and outers:
            items.append(outers[k % len(outers)])
        if sorted(items) not in [sorted(o["items"]) for o in outfits] + seen:
            outfits.append({"items": items, "reason": f"Option {len(outfits) + 1}: easy pairing."})
        k += 1
    return {"outfits": outfits}
