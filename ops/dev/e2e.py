"""End-to-end journey against a running dev stack, black box over HTTP and WebSocket.

    # a fresh stack with the scripted LLM (so the LLM paths run, not only fallbacks)
    COMPOSE_PROJECT_NAME=myror-e PG_PORT=54325 MINIO_PORT=59005 API_PORT=8805 LLM=scripted \\
      docker compose -f ops/dev/docker-compose.yml up -d --build --wait
    docker compose -p myror-e -f ops/dev/docker-compose.yml exec -T api python -m db.migrate
    # run with the server's environment (httpx, websockets, Pillow, numpy, boto3)
    cd server && uv run python ../ops/dev/e2e.py http://127.0.0.1:8805 \\
        --compose-project myror-e --minio http://127.0.0.1:59005

Needs APP_ENV=dev and HARDWARE=mock on the server (the /api/dev/* controls drive the mock
camera, cover and presence sensors). Expects an empty database (a fresh stack).

--compose-project enables the steps that need Docker: stopping and restarting the
database, and calling /api/today as the mirror (from inside the api container, where
requests come from localhost). --minio lets presigned links (signed for the in-network
host minio:9000) be fetched from the host, and lists the bucket after a photo deletion.
Without them those checks are skipped and reported as such.

Every image is generated here (flat-colour shapes); no real photos. Exit code 0 when every
step passed.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np
from PIL import Image, ImageDraw

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
# Must match providers/llm/scripted.py (the scripted LLM tags by nearest colour).
PALETTE = {
    "red": (200, 30, 40), "navy": (25, 35, 90), "green": (40, 140, 60),
    "yellow": (230, 200, 40), "black": (20, 20, 20), "purple": (120, 50, 150),
    "orange": (235, 120, 30), "teal": (20, 130, 130),
}
SHEET, SKIN, WALL = (242, 240, 232), (205, 160, 130), (150, 160, 170)


# ---- images ----------------------------------------------------------------------------


def _jpeg(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def garment(color: str, kind: str, seed: int = 0, offset=(0, 0)) -> bytes:
    w, h = 1600, 1200
    im = Image.new("RGB", (w, h), SHEET)
    d = ImageDraw.Draw(im)
    cx, cy, s = w // 2 + offset[0], h // 2 + offset[1], min(w, h) / 1000

    def pts(rel):
        return [(cx + x * s, cy + y * s) for x, y in rel]

    if kind == "tee":
        d.polygon(pts([(-180, -300), (-60, -330), (60, -330), (180, -300), (360, -150),
                       (280, -60), (190, -130), (190, 320), (-190, 320), (-190, -130),
                       (-280, -60), (-360, -150)]), fill=PALETTE[color])
    else:  # trousers
        d.polygon(pts([(-160, -380), (160, -380), (190, 380), (40, 380), (0, -100),
                       (-40, 380), (-190, 380)]), fill=PALETTE[color])
    rng = np.random.default_rng(hash((color, kind, seed)) % 2**32)
    a = np.asarray(im, dtype=np.int16)
    a = np.clip(a + rng.integers(-3, 4, a.shape), 0, 255).astype(np.uint8)
    return _jpeg(Image.fromarray(a))


def person(top: str, bottom: str, outer: str | None = None) -> bytes:
    w, h = 900, 1200
    im = Image.new("RGB", (w, h), WALL)
    d = ImageDraw.Draw(im)
    d.ellipse([w * 0.40, h * 0.06, w * 0.60, h * 0.24], fill=SKIN)
    d.rectangle([w * 0.47, h * 0.23, w * 0.53, h * 0.28], fill=SKIN)
    if outer:
        d.rectangle([w * 0.20, h * 0.27, w * 0.80, h * 0.74], fill=PALETTE[outer])
        d.rectangle([w * 0.40, h * 0.28, w * 0.60, h * 0.72], fill=PALETTE[top])
    else:
        d.rectangle([w * 0.30, h * 0.28, w * 0.70, h * 0.72], fill=PALETTE[top])
    d.rectangle([w * 0.32, h * 0.72, w * 0.68, h], fill=PALETTE[bottom])
    return _jpeg(im)


# ---- harness ---------------------------------------------------------------------------


class StepFailed(AssertionError):
    pass


def check(cond: Any, msg: str) -> None:
    if not cond:
        raise StepFailed(msg)


class Events:
    """Collects every WebSocket message; waits look forward from a cursor."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.closed_code: int | None = None
        self._new = asyncio.Event()

    def add(self, msg: dict) -> None:
        self.items.append(msg)
        self._new.set()

    def mark(self) -> int:
        return len(self.items)

    async def wait(self, type_: str, pred: Callable[[dict], bool] = lambda d: True,
                   since: int = 0, timeout: float = 15.0) -> dict:
        end = time.monotonic() + timeout
        i = since
        while True:
            while i < len(self.items):
                m = self.items[i]
                i += 1
                if m.get("type") == type_ and pred(m.get("data") or {}):
                    return m.get("data") or {}
            left = end - time.monotonic()
            if left <= 0:
                seen = [m.get("type") for m in self.items[since:]][-15:]
                raise StepFailed(f"no {type_} event within {timeout}s (saw {seen})")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), left)
            except TimeoutError:
                pass

    def of(self, type_: str, since: int = 0) -> list[dict]:
        return [m.get("data") or {} for m in self.items[since:] if m.get("type") == type_]


class Journey:
    def __init__(self, args) -> None:
        self.base = args.base_url.rstrip("/")
        self.project = args.compose_project
        self.minio = args.minio
        self.results: list[tuple[str, str, float, str]] = []
        self.events = Events()
        self.anon = httpx.AsyncClient(base_url=self.base, timeout=30)
        self.phone: httpx.AsyncClient | None = None
        self.ctx: dict[str, Any] = {}
        self.db_stopped = False
        self._ws_task: asyncio.Task | None = None

    # -- helpers

    async def step(self, name: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            note = await fn() or ""
            dt = time.perf_counter() - t0
            self.results.append(("PASS", name, dt, note))
            print(f"PASS  {dt:6.2f}s  {name}" + (f"  -- {note}" if note else ""), flush=True)
        except Exception as e:
            dt = time.perf_counter() - t0
            self.results.append(("FAIL", name, dt, f"{type(e).__name__}: {e}"))
            print(f"FAIL  {dt:6.2f}s  {name}  -- {type(e).__name__}: {e}", flush=True)
            if not isinstance(e, StepFailed):
                traceback.print_exc()
            raise

    def skip(self, name: str, why: str) -> None:
        self.results.append(("SKIP", name, 0.0, why))
        print(f"SKIP           {name}  -- {why}", flush=True)

    async def ok(self, r: httpx.Response, *codes: int) -> Any:
        codes = codes or (200,)
        check(r.status_code in codes, f"{r.request.method} {r.request.url.path} -> "
                                      f"{r.status_code} {r.text[:300]}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    async def fetch_link(self, url: str) -> httpx.Response:
        """GET a presigned link. Links are signed for the in-network host (minio:9000);
        with --minio, connect to that address but keep the signed Host header."""
        if url.startswith("/"):
            return await self.anon.get(url)
        parts = urlsplit(url)
        if self.minio and parts.hostname == "minio":
            m = urlsplit(self.minio)
            target = urlunsplit(parts._replace(netloc=m.netloc, scheme=m.scheme))
            async with httpx.AsyncClient(timeout=15) as c:
                return await c.get(target, headers={"Host": parts.netloc})
        async with httpx.AsyncClient(timeout=15) as c:
            return await c.get(url)

    def compose(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-p", self.project, "-f", str(COMPOSE_FILE), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def mirror_get(self, path: str) -> tuple[int, float, Any]:
        """GET as the mirror: from inside the api container, so the peer is localhost."""
        code = ("import json,time,urllib.request;t=time.perf_counter();"
                f"r=urllib.request.urlopen('http://127.0.0.1:8000{path}',timeout=10);"
                "b=r.read();print(json.dumps([r.status,time.perf_counter()-t,"
                "json.loads(b)]))")
        p = self.compose("exec", "-T", "api", "python", "-c", code)
        check(p.returncode == 0, f"mirror request failed: {p.stderr[-300:]}")
        status, dt, body = json.loads(p.stdout.strip().splitlines()[-1])
        return status, dt, body

    async def _ws(self, token: str) -> None:
        import websockets

        url = self.base.replace("http", "ws", 1) + "/ws"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            try:
                async for raw in ws:
                    self.events.add(json.loads(raw))
            except websockets.ConnectionClosed:
                pass
            self.events.closed_code = ws.close_code

    async def wait_photos(self, ids: list[str], timeout: float = 90) -> list[dict]:
        end = time.monotonic() + timeout
        while True:
            rows = [await self.ok(await self.phone.get(f"/api/photos/{i}")) for i in ids]
            if all(r["status"] in ("done", "failed") for r in rows):
                return rows
            check(time.monotonic() < end, f"photos not done: {[r['status'] for r in rows]}")
            await asyncio.sleep(0.3)

    async def capture_state(self, state: str, since: int, timeout: float = 20,
                            pred: Callable[[dict], bool] = lambda d: True) -> dict:
        return await self.events.wait("capture.state",
                                      lambda d: d.get("state") == state and pred(d),
                                      since=since, timeout=timeout)

    # -- the journey

    async def run(self) -> int:
        try:
            await self.step("pair a phone (QR code -> token) and open the WebSocket", self.pair)
            await self.step("style quiz and profile (LLM-written)", self.style)
            await self.step("upload 12 generated garment photos; all processed", self.upload)
            await self.step("inbox lists them with LLM tags", self.inbox)
            await self.step("inbox: confirm one, merge a duplicate, reject one, bulk confirm",
                            self.resolve_inbox)
            await self.step("closet list and edit", self.closet)
            await self.step("morning precompute -> /api/today has outfits and a collage link",
                            self.precompute)
            await self.step("presence arrives -> presence event", self.presence)
            await self.step("outfit shown, thumbs up, reroll, mark worn", self.outfit_actions)
            await self.step("capture: cover wait, countdown, flash, review, retake",
                            self.capture_retake)
            await self.step("capture: half-closed cover rejected, retry, keep",
                            self.capture_reject_keep)
            await self.step("check-in matches closet items; unmatched garment to inbox",
                            self.checkin)
            await self.step("confirm check-in wears", self.confirm_wears)
            await self.step("shortcut token pushes reminders; refused elsewhere", self.shortcut)
            if self.project:
                await self.step("database stopped: phone and mirror still get /api/today fast",
                                self.db_down)
                await self.step("database restarted: first database-backed request is fast",
                                self.db_up)
            else:
                self.skip("database down/up", "needs --compose-project")
            await self.step("delete a photo; nothing left in the bucket", self.delete_photo)
            await self.step("revoke the phone: token and WebSocket refused", self.revoke)
        except Exception:  # noqa: BLE001 - reported by step()
            pass
        finally:
            if self.db_stopped:
                self.compose("start", "db")
            if self._ws_task:
                self._ws_task.cancel()
            await self.anon.aclose()
            if self.phone:
                await self.phone.aclose()
        failed = [r for r in self.results if r[0] == "FAIL"]
        print(f"\n{sum(r[0] == 'PASS' for r in self.results)} passed, {len(failed)} failed, "
              f"{sum(r[0] == 'SKIP' for r in self.results)} skipped")
        return 1 if failed else 0

    async def pair(self) -> str:
        code = await self.ok(await self.anon.post("/api/dev/pair-code"))
        check(code["pair_url"].endswith(f"/pair?code={code['code']}"), code["pair_url"])
        body = await self.ok(await self.anon.post(
            "/api/pair", json={"code": code["code"], "name": "e2e phone"}), 201)
        self.ctx["device_id"], token = body["device_id"], body["token"]
        self.phone = httpx.AsyncClient(base_url=self.base, timeout=30,
                                       headers={"Authorization": f"Bearer {token}"})
        check((await self.anon.get("/api/today")).status_code == 401, "no token -> 401")
        self._ws_task = asyncio.ensure_future(self._ws(token))
        await self.events.wait("hello", timeout=5)
        health = await self.ok(await self.phone.get("/api/health"))
        check(health["db"]["ok"] and health["storage"]["ok"], f"health {health}")
        check(health["hardware"] == "mock", "HARDWARE=mock needed")
        return f"device {body['device_id'][:8]}"

    async def style(self) -> str:
        quiz = (await self.ok(await self.phone.get("/api/style/quiz")))["questions"]
        answers: dict[str, Any] = {}
        for q in quiz:
            if q["kind"] == "single":
                answers[q["id"]] = q["options"][0]["value"]
            elif q["kind"] == "multi" and q["required"]:
                answers[q["id"]] = [q["options"][0]["value"]]
            elif q["kind"] == "scale":
                answers[q["id"]] = q["min"]
        answers["fit_notes"] = "Trousers are often a little long."
        out = await self.ok(await self.phone.post("/api/style/quiz",
                                                  json={"answers": answers}), 201)
        check(out["written_by"] == "llm", f"profile written by {out['written_by']}")
        await self.ok(await self.phone.put("/api/style", json={"fit_notes": "Prefer relaxed."}))
        cur = await self.ok(await self.phone.get("/api/style"))
        check(cur["fit_notes"] == "Prefer relaxed." and cur["text"], cur)
        return f"profile v{cur['version']} by llm"

    async def upload(self) -> str:
        plan = [("red", "tee"), ("navy", "trousers"), ("green", "tee"), ("yellow", "tee"),
                ("black", "trousers"), ("purple", "tee"), ("orange", "tee"),
                ("green", "trousers"), ("yellow", "trousers"), ("purple", "trousers"),
                ("red", "tee", 1)]  # the last: another photo of the red tee
        ids, kinds = [], {}
        for spec in plan:
            color, kind, *seed = spec
            data = garment(color, kind, seed[0] if seed else 0,
                           offset=(40, 25) if seed else (0, 0))
            r = await self.phone.post("/api/items/upload",
                                      files={"file": (f"{color}-{kind}.jpg", data, "image/jpeg")})
            body = await self.ok(r, 201)
            ids.append(body["photo_id"])
            kinds[body["photo_id"]] = (color, kind, bool(seed))
            if len(ids) == 1:
                again = await self.phone.post(
                    "/api/items/upload", files={"file": ("again.jpg", data, "image/jpeg")})
                b2 = await self.ok(again, 200)
                check(b2["duplicate_upload"] and b2["photo_id"] == ids[0], "re-upload")
        rows = await self.wait_photos(ids)
        check(all(r["status"] == "done" for r in rows), [r["error"] for r in rows])
        self.ctx["uploads"], self.ctx["kinds"] = ids, kinds
        return f"{len(ids) + 1} uploads (one repeat answered 200), {len(ids)} photos done"

    async def inbox(self) -> str:
        box = await self.ok(await self.phone.get("/api/inbox"))
        mine = [e for e in box["entries"] if e["photo_id"] in self.ctx["kinds"]]
        check(len(mine) == len(self.ctx["uploads"]), f"{len(mine)} entries")
        for e in mine:
            color, kind, _ = self.ctx["kinds"][e["photo_id"]]
            check(e["tagging"] == "ok", f"tagging {e['tagging']} {e['tag_errors']}")
            check(e["proposed_tags"]["primary_color"] == color, (color, e["proposed_tags"]))
            check(e["cutout_url"], "cutout link")
        r = await self.fetch_link(mine[0]["cutout_url"])
        if self.minio or "minio" not in mine[0]["cutout_url"]:
            check(r.status_code == 200 and r.content[:4] == b"\x89PNG", f"cutout {r.status_code}")
        self.ctx["entries"] = mine
        return f"{len(mine)} entries, all tagged by the LLM path"

    async def resolve_inbox(self) -> str:
        entries = self.ctx["entries"]
        by = {self.ctx["kinds"][e["photo_id"]]: e for e in entries}
        red = by[("red", "tee", False)]
        item = (await self.ok(await self.phone.post(f"/api/inbox/{red['id']}/confirm",
                                                    json={"name": "Red tee"}), 201))["item"]
        dup = by[("red", "tee", True)]
        merged = await self.ok(await self.phone.post(f"/api/inbox/{dup['id']}/merge",
                                                     json={"item_id": item["id"]}))
        check(merged["item"]["id"] == item["id"], "merge")
        rej = by[("purple", "trousers", False)]
        await self.ok(await self.phone.post(f"/api/inbox/{rej['id']}/reject"), 204)
        rest = [e for e in entries if e["id"] not in (red["id"], dup["id"], rej["id"])]
        bulk = await self.ok(await self.phone.post("/api/inbox/confirm", json={
            "entries": [{"id": e["id"]} for e in rest], "include_matches": True}))
        check(len(bulk["confirmed"]) == len(rest) and not bulk["failed"], bulk)
        left = (await self.ok(await self.phone.get("/api/inbox")))["count"]
        check(left == 0, f"{left} left in the inbox")
        items = {c["item"]["tags"]["primary_color"] + " " + c["item"]["tags"]["category"]:
                 c["item"] for c in bulk["confirmed"]}
        items["red top"] = item
        self.ctx["items"] = items
        return f"1 confirmed, 1 merged, 1 rejected, {len(rest)} bulk-confirmed"

    async def closet(self) -> str:
        lst = await self.ok(await self.phone.get("/api/items"))
        check(lst["total"] == 9, f"{lst['total']} items")
        it = self.ctx["items"]["green top"]
        out = await self.ok(await self.phone.patch(f"/api/items/{it['id']}",
                                                   json={"name": "Green weekend tee"}))
        check(out["name"] == "Green weekend tee", out)
        tops = await self.ok(await self.phone.get("/api/items", params={"category": "top"}))
        return f"{lst['total']} items, {tops['total']} tops; renamed one"

    async def precompute(self) -> str:
        mark = self.events.mark()
        await self.ok(await self.phone.post("/api/dev/daily/recommender.morning"))
        await self.ok(await self.phone.post("/api/dev/jobs/wait-idle", params={"timeout": 60}))
        await self.events.wait("outfits.updated", since=mark, timeout=30)
        today = await self.ok(await self.phone.get("/api/today"))
        check(today["outfits"], "no outfits on /api/today")
        check(all(o["source"] == "llm" for o in today["outfits"]),
              [o["source"] for o in today["outfits"]])
        check("stale" in today, "stale field")
        o = today["outfits"][0]
        check(o["collage_url"], "collage link")
        notes = []
        r = await self.fetch_link(o["collage_url"])
        if self.minio or "minio" not in o["collage_url"]:
            check(r.status_code == 200 and r.content[:2] == b"\xff\xd8",
                  f"collage {r.status_code}")
            notes.append(f"collage {len(r.content)} bytes")
        else:
            notes.append("collage download skipped (no --minio)")
        if today["weather"] is None:
            notes.append("weather not configured on this stack")
        else:
            check(today["weather"]["temp_c"] is not None, "weather")
            notes.append("weather present")
        self.ctx["outfits"] = today["outfits"]
        return f"{len(today['outfits'])} outfits by llm; " + "; ".join(notes)

    async def presence(self) -> str:
        mark = self.events.mark()
        await self.ok(await self.phone.post("/api/dev/hardware/presence", json={"present": True}))
        await self.events.wait("presence", lambda d: d.get("present") is True, since=mark,
                               timeout=5)
        return "presence {present: true} received"

    async def outfit_actions(self) -> str:
        first = self.ctx["outfits"][0]["id"]
        await self.ok(await self.phone.post(f"/api/outfits/{first}/shown"), 204)
        fb = await self.ok(await self.phone.post(f"/api/outfits/{first}/feedback",
                                                 json={"value": "up"}), 201)
        check(fb["value"] == 1, fb)
        rr = await self.ok(await self.phone.post("/api/outfits/reroll", json={}))
        new_ids = {o["id"] for o in rr["outfits"]}
        check(rr["outfits"] and first not in new_ids, "reroll returned the same outfit")
        check(rr["source"] == "llm", f"reroll source {rr['source']} ({rr['fallback_reason']})")
        pick = rr["outfits"][0]
        worn = await self.ok(await self.phone.post(f"/api/outfits/{pick['id']}/worn", json={}))
        check(worn["logged"], worn)
        self.ctx["worn_items"] = set(worn["logged"])
        return (f"reroll source={rr['source']}, worn {len(worn['logged'])} items of "
                f"outfit {pick['id'][:8]}")

    async def capture_retake(self) -> str:
        frame = person("red", "navy", outer="teal")
        await self.ok(await self.phone.post("/api/dev/hardware/camera/frame", content=frame,
                                            headers={"Content-Type": "image/jpeg"}))
        await self.ok(await self.phone.post("/api/dev/hardware/cover", json={"open": False}))
        mark = self.events.mark()
        start = await self.ok(await self.phone.post("/api/capture"))
        check(start["state"] == "cover_wait", start)
        cid = start["capture_id"]
        self.ctx["capture_id"] = cid
        check((await self.phone.post("/api/capture")).status_code == 409, "second capture")
        cur = await self.ok(await self.phone.get("/api/capture/current"))
        check(cur["state"] == "cover_wait" and cur["seconds_left"] > 50, cur)
        await self.ok(await self.phone.post("/api/dev/hardware/cover", json={"open": True}))
        await self.events.wait("cover", lambda d: d.get("open") is True, since=mark)
        review = await self.capture_state("review", mark)
        ticks = [d["seconds_left"] for d in self.events.of("capture.state", mark)
                 if d["state"] == "countdown"]
        check(ticks == [3, 2, 1], f"countdown ticks {ticks}")
        seq = [d["state"] for d in self.events.of("capture.state", mark)]
        check(seq[:2] == ["cover_wait", "preview"] and "flash" in seq
              and "quality_check" in seq, seq)
        img = await self.anon.get(review["review_url"])  # no token needed
        check(img.status_code == 200 and img.content[:2] == b"\xff\xd8", "review image")
        hw = await self.ok(await self.phone.get("/api/dev/hardware"))
        check({"level": 1.0, "full_duty": True} in hw["leds"]["history"], hw["leds"])
        check(hw["leds"]["level"] == 0.0, "LEDs left on")
        r = await self.ok(await self.phone.post(f"/api/capture/{cid}/retake"))
        check(r["state"] == "preview", r)
        self.ctx["retake_mark"] = self.events.mark()
        return f"states {' > '.join(dict.fromkeys(seq))}; retake -> preview"

    async def capture_reject_keep(self) -> str:
        cid, mark = self.ctx["capture_id"], self.ctx["retake_mark"]
        await self.ok(await self.phone.post("/api/dev/hardware/camera/frame",
                                            json={"kind": "half_covered"}))
        bad = await self.capture_state("quality_check", mark,
                                       pred=lambda d: "covered" in (d.get("message") or ""))
        await self.ok(await self.phone.post("/api/dev/hardware/camera/frame",
                                            json={"kind": "good"}))
        m2 = self.events.mark()
        await self.capture_state("review", m2)
        kept = await self.ok(await self.phone.post(f"/api/capture/{cid}/keep"))
        check(kept["state"] == "cover_reminder" and kept["photo_id"], kept)
        reminder = await self.capture_state("cover_reminder", m2)
        await self.ok(await self.phone.post("/api/dev/hardware/cover", json={"open": False}))
        await self.capture_state("idle", m2)
        check((await self.ok(await self.phone.get("/api/capture/current"))) == {"state": "idle"},
              "not idle")
        self.ctx["checkin_photo"] = kept["photo_id"]
        return (f"rejected: \"{bad['message']}\"; kept photo {kept['photo_id'][:8]}; "
                f"reminder \"{reminder['message']}\" until the cover closed")

    async def checkin(self) -> str:
        pid = self.ctx["checkin_photo"]
        end = time.monotonic() + 60
        while True:
            c = await self.ok(await self.phone.get(f"/api/checkins/{pid}"))
            if c["status"] in ("done", "failed"):
                break
            check(time.monotonic() < end, f"check-in {c['status']}")
            await asyncio.sleep(0.3)
        check(c["status"] == "done" and c["source"] == "mirror_checkin", c)
        by = {g["tags"]["primary_color"]: g for g in c["garments"]}
        check(set(by) == {"red", "navy", "teal"}, set(by))
        items = self.ctx["items"]
        check(by["red"]["match"] and by["red"]["match"]["item"]["id"] == items["red top"]["id"],
              "red tee not matched")
        check(by["navy"]["match"]["item"]["id"] == items["navy bottom"]["id"], "navy trousers")
        check(by["teal"]["match"] is None, "teal jacket should be unknown")
        r = await self.ok(await self.phone.post(
            f"/api/checkins/{pid}/garments/{by['teal']['detection_id']}/inbox"), 201)
        box = await self.ok(await self.phone.get("/api/inbox"))
        check([e["id"] for e in box["entries"]] == [r["inbox_id"]]
              and box["entries"][0]["origin"] == "checkin", box)
        self.ctx["checkin_items"] = [by["red"]["match"]["item"]["id"],
                                     by["navy"]["match"]["item"]["id"]]
        return "red tee and navy trousers matched; teal jacket sent to the inbox"

    async def confirm_wears(self) -> str:
        pid, ids = self.ctx["checkin_photo"], self.ctx["checkin_items"]
        out = await self.ok(await self.phone.post(f"/api/checkins/{pid}/confirm",
                                                  json={"item_ids": ids}))
        check(sorted(out["logged"] + out["already_logged"]) == sorted(ids), out)
        check(set(out["already_logged"]) == set(ids) & self.ctx["worn_items"], out)
        again = await self.ok(await self.phone.post(f"/api/checkins/{pid}/confirm",
                                                    json={"item_ids": ids}))
        check(again["logged"] == [], "confirm is idempotent")
        item = await self.ok(await self.phone.get(f"/api/items/{ids[0]}"))
        check(item["last_worn_on"] == out["worn_on"], item)
        return f"logged {len(out['logged'])}, already logged {len(out['already_logged'])}"

    async def shortcut(self) -> str:
        minted = await self.ok(await self.phone.post("/api/shortcut/tokens",
                                                     json={"name": "e2e shortcut"}), 201)
        async with httpx.AsyncClient(base_url=self.base, timeout=15, headers={
                "Authorization": f"Bearer {minted['token']}"}) as sc:
            push = await self.ok(await sc.post("/api/reminders", json={
                "list": "Errands", "reminders": [
                    {"id": "e2e-1", "title": "Pick up dry cleaning", "due": "2030-01-01"},
                    {"id": "e2e-2", "title": "Done already", "completed": True}]}))
            check(push["stored"] == 2 and push["open"] == 1, push)
            refused = {p: (await sc.get(p)).status_code for p in
                       ("/api/today", "/api/items", "/api/reminders", "/api/devices")}
            check(set(refused.values()) == {403}, refused)
        today = await self.ok(await self.phone.get("/api/today"))
        titles = [r["title"] for r in today["reminders"]]
        check(titles == ["Pick up dry cleaning"], titles)
        codes = sorted(set(refused.values()))
        return f"pushed 2 (1 open) and shown on /api/today; elsewhere {codes}"

    async def db_down(self) -> str:
        p = self.compose("stop", "db")
        check(p.returncode == 0, p.stderr)
        self.db_stopped = True
        # Make the next token check go to the (stopped) database, as it would once the
        # one-minute device cache runs out: it must fall back to the last known list.
        await self.ok(await self.phone.post("/api/dev/auth/expire-cache"))
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            r = await self.phone.get("/api/today")
            times.append(time.perf_counter() - t0)
            check(r.status_code == 200 and r.json()["outfits"], f"phone today {r.status_code}")
        check(max(times) < 2.5, f"phone /api/today took {times}")
        status, mdt, body = self.mirror_get("/api/today")
        check(status == 200 and body["outfits"], "mirror today")
        check(mdt < 1.0, f"mirror took {mdt:.2f}s")
        rem = await self.ok(await self.phone.get("/api/capture/current"))
        self.ctx["down_times"] = (times, mdt)
        return ("phone /api/today " + ", ".join(f"{t * 1000:.0f} ms" for t in times)
                + f"; mirror {mdt * 1000:.0f} ms; capture/current {rem['state']}")

    async def db_up(self) -> str:
        p = self.compose("start", "db")
        check(p.returncode == 0, p.stderr)
        end = time.monotonic() + 60
        while self.compose("exec", "-T", "db", "pg_isready", "-U", "mirror").returncode != 0:
            check(time.monotonic() < end, "database did not come back")
            await asyncio.sleep(0.5)
        self.db_stopped = False
        t0 = time.perf_counter()
        r = await self.phone.get("/api/items")
        first = time.perf_counter() - t0
        check(r.status_code == 200, f"items after restart {r.status_code} {r.text[:200]}")
        check(first < 3.0, f"first request took {first:.2f}s")
        t0 = time.perf_counter()
        await self.ok(await self.phone.get("/api/items"))
        second = time.perf_counter() - t0
        return f"first /api/items {first * 1000:.0f} ms, second {second * 1000:.0f} ms"

    async def delete_photo(self) -> str:
        item = self.ctx["items"]["orange top"]
        pid = item["source_photo_id"]
        detail = await self.ok(await self.phone.get(f"/api/photos/{pid}"))
        links = [detail["photo_url"]] + [d["cutout_url"] for d in detail["detections"]
                                         if d["cutout_url"]]
        out = await self.ok(await self.phone.delete(f"/api/photos/{pid}"))
        check(out["objects_deleted"] and item["id"] in out["items_kept_without_cutout"], out)
        check((await self.phone.get(f"/api/photos/{pid}")).status_code == 404, "row left")
        notes = [f"{len(out['objects_deleted'])} objects deleted"]
        if self.minio:
            for url in links:
                r = await self.fetch_link(url)
                check(r.status_code == 404, f"{urlsplit(url).path} still served ({r.status_code})")
            import boto3

            s3 = boto3.client("s3", endpoint_url=self.minio, aws_access_key_id="minioadmin",
                              aws_secret_access_key="minioadmin", region_name="us-east-1")
            left = []
            for prefix in (f"photos/{pid}/", f"cutouts/{pid}/", f"masks/{pid}/"):
                resp = s3.list_objects_v2(Bucket="mirror-images", Prefix=prefix)
                left += [o["Key"] for o in resp.get("Contents", [])]
            check(not left, f"left in bucket: {left}")
            notes.append("old links 404, bucket has no key for the photo")
        else:
            notes.append("bucket not checked (no --minio)")
        kept = await self.ok(await self.phone.get(f"/api/items/{item['id']}"))
        check(kept["cutout_url"] is None, "item kept without cutout")
        return "; ".join(notes)

    async def revoke(self) -> str:
        await self.ok(await self.phone.delete(f"/api/devices/{self.ctx['device_id']}"), 204)
        r = await self.phone.get("/api/today")
        check(r.status_code == 401, f"after revoke {r.status_code}")
        for _ in range(50):
            if self._ws_task.done():
                break
            await asyncio.sleep(0.1)
        check(self._ws_task.done() and self.events.closed_code == 4401,
              f"websocket close code {self.events.closed_code}")
        return "401 on the API; WebSocket closed with 4401"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base_url")
    ap.add_argument("--compose-project", help="docker compose project of the stack")
    ap.add_argument("--minio", help="host URL of the stack's MinIO, e.g. http://127.0.0.1:59005")
    return asyncio.run(Journey(ap.parse_args()).run())


if __name__ == "__main__":
    sys.exit(main())
