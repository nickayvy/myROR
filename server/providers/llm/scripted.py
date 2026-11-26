"""LLM=scripted: a deterministic stand-in for Claude, so dev stacks and ops/dev/e2e.py
exercise the LLM code paths (the stock LLM=fake answers {} and only reaches fallbacks).

It understands synthetic images only: flat-colour garments on a white sheet and crude
"person" photos, in the colours of PALETTE (ops/dev/e2e.py draws them). Per purpose:

- tag_item:            colour = nearest palette colour of the foreground; tall shape ->
                       trousers, otherwise a t-shirt
- checkin_vision:      one garment per palette colour region, with its pixel box
- compose_outfits:     up to 3 different top+bottom(+shoes, +outer when required) picks
                       from the candidate codes in the schema
- event_formality:     4 for interviews/clients/presentations, 3 for meetings, else 2
- style_profile_init / style_weekly_update: short fixed text
Never calls the network. Costs nothing (no spend is recorded).
"""

from __future__ import annotations

import base64
import io
import itertools
import json
import re
from typing import Any

import numpy as np
from PIL import Image

from providers.llm.fake import FakeCall, FakeLLM

PALETTE: dict[str, tuple[int, int, int]] = {
    "red": (200, 30, 40),
    "navy": (25, 35, 90),
    "green": (40, 140, 60),
    "yellow": (230, 200, 40),
    "black": (20, 20, 20),
    "purple": (120, 50, 150),
    "orange": (235, 120, 30),
    "teal": (20, 130, 130),
}

TAGS_BY_KIND = {
    "tee": {"category": "top", "subcategory": "t_shirt", "pattern": "solid",
            "material_guess": "cotton", "formality": 2, "season": ["spring", "summer"],
            "fit": "regular", "style_tags": ["casual"]},
    "trousers": {"category": "bottom", "subcategory": "trousers", "pattern": "solid",
                 "material_guess": "wool", "formality": 3, "season": [], "fit": "slim",
                 "style_tags": ["smart_casual"]},
    "jacket": {"category": "outerwear", "subcategory": "jacket", "pattern": "solid",
               "material_guess": "cotton", "formality": 3, "season": ["fall"],
               "fit": "regular", "style_tags": ["casual"]},
}


def _image(call: FakeCall) -> Image.Image | None:
    for b in call.content:
        if b.get("type") == "image":
            raw = base64.b64decode(b["source"]["data"])
            return Image.open(io.BytesIO(raw)).convert("RGB")
    return None


def _nearest(rgb) -> str:
    return min(PALETTE, key=lambda k: float(np.linalg.norm(np.subtract(PALETTE[k], rgb))))


def _tags(color: str, kind: str) -> dict[str, Any]:
    return {"name": f"{color} {kind}", "primary_color": color, "secondary_color": None,
            **TAGS_BY_KIND[kind]}


def tag_item(call: FakeCall) -> dict[str, Any]:
    im = _image(call)
    if im is None:
        return {"tags": _tags("black", "tee"), "confidence": "low", "single_item": False}
    a = np.asarray(im, dtype=np.int16)
    fg = (a.min(axis=2) < 200) | ((a.max(axis=2) - a.min(axis=2)) > 40)
    if fg.sum() < 50:
        return {"tags": _tags("black", "tee"), "confidence": "low", "single_item": False}
    ys, xs = np.nonzero(fg)
    tall = (ys.max() - ys.min()) > 1.3 * (xs.max() - xs.min())
    color = _nearest(np.median(a[fg], axis=0))
    return {"tags": _tags(color, "trousers" if tall else "tee"), "confidence": "high",
            "single_item": True}


def checkin_vision(call: FakeCall) -> dict[str, Any]:
    im = _image(call)
    if im is None:
        return {"image_usable": False, "garments": []}
    a = np.asarray(im, dtype=np.float32)
    h, w = a.shape[:2]
    garments = []
    for name, rgb in PALETTE.items():
        m = np.linalg.norm(a - np.array(rgb), axis=2) < 40
        if m.sum() < 0.01 * m.size:
            continue
        ys, xs = np.nonzero(m)
        box = {"x_min": int(xs.min()), "y_min": int(ys.min()), "x_max": int(xs.max()) + 1,
               "y_max": int(ys.max()) + 1}
        bottom = box["y_min"] > 0.6 * h
        wide = (box["x_max"] - box["x_min"]) > 0.48 * w
        kind = "trousers" if bottom else "jacket" if wide else "tee"
        garments.append({"tags": _tags(name, kind), "box": box,
                         "visibility": "partly_visible" if bottom else "mostly_visible"})
    return {"image_usable": bool(garments), "garments": garments}


def _codes(schema: dict[str, Any]) -> list[str]:
    try:
        return list(schema["properties"]["outfits"]["items"]["properties"]["items"]["items"]
                    ["enum"])
    except (KeyError, TypeError):
        return []


def compose_outfits(call: FakeCall) -> dict[str, Any]:
    codes = _codes(call.json_schema)
    by = {p: [c for c in codes if c.startswith(p)] for p in "TBDOSA"}
    outer = "outer layer is required" in call.text
    bases = [[t, b] for t, b in itertools.product(by["T"], by["B"])] + [[d] for d in by["D"]]
    # Combinations listed as already suggested (rerolls) are skipped, like the model would.
    seen = re.search(r"Already suggested today[^:]*: (.*)", call.text)
    avoid = [set(c.split("+")) for c in seen.group(1).split(", ")] if seen else []
    bases = [b for b in bases if not any(set(b) <= a for a in avoid)]
    outfits = []
    for i, base in enumerate(bases[:3]):
        items = list(base)
        if by["S"]:
            items.append(by["S"][i % len(by["S"])])
        if outer and by["O"]:
            items.append(by["O"][i % len(by["O"])])
        outfits.append({"items": items, "reason": "Scripted pick: an easy, matching outfit."})
    return {"outfits": outfits}


def event_formality(call: FakeCall) -> dict[str, Any]:
    try:
        titles = json.loads(call.text)
    except ValueError:
        titles = []
    ratings = []
    for t in titles:
        title = str(t.get("title", "")).lower()
        f = 4 if re.search(r"interview|client|presentation|investor", title) else \
            3 if re.search(r"meeting|standup|class", title) else 2
        ratings.append({"i": t.get("i", 0), "formality": f})
    return {"ratings": ratings}


def responder(call: FakeCall) -> dict[str, Any]:
    if call.purpose == "tag_item":
        return tag_item(call)
    if call.purpose == "checkin_vision":
        return checkin_vision(call)
    if call.purpose == "compose_outfits":
        return compose_outfits(call)
    if call.purpose == "event_formality":
        return event_formality(call)
    if call.purpose == "style_profile_init":
        return {"profile": "Scripted profile: clean, casual combinations in solid colours."}
    if call.purpose == "style_weekly_update":
        return {"observations": ["Scripted note: liked the casual outfits this week."]}
    return {}


def build_scripted_llm(spend: Any = None) -> FakeLLM:
    return FakeLLM(default=responder, spend=spend, name="scripted")
