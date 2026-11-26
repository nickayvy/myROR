"""Manual QA over real HTTP against a running server (not collected by pytest).

    # in the api container: a second server whose LLM is the scripted fake that tags
    # synthetic garments (the stock LLM=fake answers {} to everything)
    python -m tests.pipeline.qa_http serve &          # 0.0.0.0:8001
    python -m tests.pipeline.qa_http drive http://<container-ip>:8001 /tmp/qa-out

`drive` pairs a phone (the code comes from 127.0.0.1, the phone calls from the container
IP, so it counts as LAN), uploads generated garment photos (one HEIC with GPS, one rotated
JPEG, one re-upload), works the inbox, the closet and a check-in, deletes a photo and checks
the bucket. Prints a transcript; saves a few cutouts to the output directory.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx


def serve() -> None:
    import uvicorn

    from mirror_api.app import create_app
    from mirror_api.config import Settings
    from providers.llm import FakeLLM
    from tests.pipeline import synth

    app = create_app(Settings(), {"llm": FakeLLM(default=synth.responder)})
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")


def say(*a) -> None:
    print(*a, flush=True)


async def wait_done(c: httpx.AsyncClient, ids: list[str], timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = [(await c.get(f"/api/photos/{i}")).json()["status"] for i in ids]
        if all(s in ("done", "failed") for s in st):
            say(f"  statuses: {st}")
            return
        await asyncio.sleep(0.3)
    raise TimeoutError("photos did not finish")


async def drive(base: str, out_dir: str) -> None:
    from mirror_api.config import Settings
    from storage import build_storage
    from tests.pipeline import synth

    out = Path(out_dir)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as mirror:
        code = (await mirror.post("/api/pair/code")).json()["code"]
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        r = await anon.post("/api/pair", json={"code": code, "name": "QA phone"})
        token = r.json()["token"]
        unauth = await anon.post("/api/items/upload", files={"file": ("a.jpg", b"x")})
        say(f"pair: {r.status_code}; unauthenticated upload -> {unauth.status_code}")
    c = httpx.AsyncClient(base_url=base, timeout=30, headers={"Authorization": f"Bearer {token}"})

    uploads = [
        ("red_tee.jpg", synth.garment_jpeg("red", "tee"), "image/jpeg"),
        ("navy_trousers.jpg", synth.garment_jpeg("navy", "trousers"), "image/jpeg"),
        ("green_tee.jpg", synth.garment_jpeg("green", "tee"), "image/jpeg"),
        ("yellow_trousers.jpg", synth.garment_jpeg("yellow", "trousers"), "image/jpeg"),
        ("black_tee.jpg", synth.garment_jpeg("black", "tee"), "image/jpeg"),
        ("teal_tee.jpg", synth.garment_jpeg("teal", "tee"), "image/jpeg"),
        ("purple_tee.heic", synth.heic_with_gps("purple", "tee"), "image/heic"),
        ("green_trousers_rotated.jpg", synth.rotated_jpeg("green", "trousers"), "image/jpeg"),
        ("orange_tee.jpg", synth.garment_jpeg("orange", "tee"), "image/jpeg"),
        ("red_tee_again_shifted.jpg", synth.garment_jpeg("red", "tee", offset=(60, -30),
                                                         scale=0.95), "image/jpeg"),
    ]
    ids: dict[str, str] = {}
    say("\n== upload 10 garment photos")
    for name, data, ctype in uploads[:-1]:
        r = await c.post("/api/items/upload", files={"file": (name, data, ctype)})
        ids[name] = r.json()["photo_id"]
        say(f"  {name:<28} {r.status_code} {r.json()['status']}")
    r = await c.post("/api/items/upload", files={"file": uploads[0][:2] + ("image/jpeg",)})
    say(f"  re-upload red_tee.jpg        {r.status_code} duplicate_upload="
        f"{r.json()['duplicate_upload']} same_id={r.json()['photo_id'] == ids['red_tee.jpg']}")
    await wait_done(c, list(ids.values()))

    inbox = (await c.get("/api/inbox")).json()["entries"]
    say(f"\n== inbox: {len(inbox)} entries")
    for e in inbox:
        t = e["proposed_tags"]
        say(f"  {e['id'][:8]} {t.get('primary_color'):<7} {t.get('subcategory'):<9} "
            f"tagging={e['tagging']} match={e['match'] and e['match']['kind']}")
    by_photo = {e["photo_id"]: e for e in inbox}
    # confirm one with an edit, bulk-confirm most, reject one
    first = by_photo[ids["red_tee.jpg"]]
    r = await c.post(f"/api/inbox/{first['id']}/confirm",
                     json={"name": "Red crew tee", "tags": {"fit": "relaxed"}})
    red_item = r.json()["item"]
    say(f"\nconfirm red tee with edits: {r.status_code} name={red_item['name']} "
        f"fit={red_item['tags']['fit']}")
    reject = by_photo[ids["orange_tee.jpg"]]
    rid = reject["id"]
    say(f"reject orange tee: {(await c.post(f'/api/inbox/{rid}/reject')).status_code}")
    rest = [by_photo[ids[n]]["id"] for n, _, _ in uploads[1:8]]
    r = (await c.post("/api/inbox/confirm", json={"entries": [{"id": i} for i in rest]})).json()
    say(f"bulk confirm 7: confirmed={len(r['confirmed'])} skipped={len(r['skipped'])} "
        f"failed={len(r['failed'])}")

    say("\n== duplicate: upload the red tee again, shifted")
    name, data, ctype = uploads[-1]
    r = await c.post("/api/items/upload", files={"file": (name, data, ctype)})
    dup_pid = r.json()["photo_id"]
    await wait_done(c, [dup_pid])
    dup = [e for e in (await c.get("/api/inbox")).json()["entries"] if e["photo_id"] == dup_pid][0]
    m = dup["match"]
    say(f"  match kind={m['kind']} score={m['score']:.3f} method={m['method']} "
        f"item={m['item']['name']}")
    r = await c.post(f"/api/inbox/{dup['id']}/merge", json={"item_id": m["item"]["id"]})
    say(f"  merge into existing: {r.status_code}; inbox now "
        f"{(await c.get('/api/inbox')).json()['count']}")

    say("\n== closet")
    closet = (await c.get("/api/items")).json()
    for it in closet["items"]:
        say(f"  {it['name']:<22} {it['tags']['category']:<7} {it['state']:<6} "
            f"cutout={'yes' if it['cutout_url'] else 'no'}")
    say(f"  total={closet['total']}; tops="
        f"{(await c.get('/api/items', params={'category': 'top'})).json()['total']}")
    navy = next(i for i in closet["items"] if i["tags"]["primary_color"] == "navy")
    r = await c.patch(f"/api/items/{navy['id']}", json={"name": "Navy wool trousers",
                                                        "formality": 4})
    say(f"patch navy: {r.status_code} name={r.json()['name']} formality="
        f"{r.json()['tags']['formality']}")
    r = await c.patch(f"/api/items/{navy['id']}", json={"subcategory": "hoodie"})
    say(f"patch invalid subcategory: {r.status_code} {r.json()['detail']['errors'][0][:70]}")
    r = await c.post(f"/api/items/{red_item['id']}/state", json={"state": "dirty"})
    say(f"mark red tee dirty: {r.status_code} state={r.json()['state']}")

    say("\n== check-in")
    r = await c.post("/api/checkins/upload", files={"file": ("me.jpg", synth.person_jpeg(
        "red", "navy"), "image/jpeg")})
    cid = r.json()["photo_id"]
    say(f"  upload: {r.status_code}")
    await wait_done(c, [cid])
    ck = (await c.get(f"/api/checkins/{cid}")).json()
    accept = []
    for g in ck["garments"]:
        mm = g["match"]
        say(f"  {g['label']:<16} seg={g['segmentation']} -> "
            f"{mm['item']['name'] if mm else None} ({mm and round(mm['score'], 3)}, "
            f"{mm and mm['kind']})")
        if mm:
            accept.append(mm["item"]["id"])
    r = await c.post(f"/api/checkins/{cid}/confirm", json={"item_ids": accept})
    say(f"  confirm: {r.status_code} {r.json()}")
    r = await c.post(f"/api/checkins/{cid}/confirm", json={"item_ids": accept})
    say(f"  confirm again: logged={r.json()['logged']} already={len(r.json()['already_logged'])}")
    worn = (await c.get(f"/api/items/{accept[0]}")).json()["last_worn_on"]
    say(f"  last_worn_on: {worn}")
    for g in ck["garments"][:2]:
        (out / f"checkin_{g['label'].replace(' ', '_')}.png").write_bytes(
            (await httpx.AsyncClient().get(g["cutout_url"])).content)

    # save a couple of onboarding cutouts for a look
    for it in closet["items"][:3]:
        if it["cutout_url"]:
            (out / f"item_{it['name'].replace(' ', '_')}.png").write_bytes(
                (await httpx.AsyncClient().get(it["cutout_url"])).content)

    say("\n== delete the HEIC photo (its item stays, without a cutout)")
    storage = build_storage(Settings())
    pid = ids["purple_tee.heic"]
    before = [k for k in await storage.list("") if pid in k]
    r = await c.delete(f"/api/photos/{pid}")
    rep = r.json()
    after = [k for k in await storage.list("") if pid in k]
    say(f"  objects before={len(before)} deleted={len(rep['objects_deleted'])} "
        f"left={len(after)} kept_items={len(rep['items_kept_without_cutout'])}")
    say("\n== delete the check-in photo")
    before = [k for k in await storage.list("") if cid in k]
    rep = (await c.delete(f"/api/photos/{cid}")).json()
    after = [k for k in await storage.list("") if cid in k]
    say(f"  objects before={len(before)} ({sorted({k.split('/')[0] for k in before})}) "
        f"left={len(after)}; wear kept: "
        f"{(await c.get(f'/api/items/{accept[0]}')).json()['last_worn_on']}")
    say("\n== delete every remaining photo with its items; the bucket must be empty")
    for p in (await c.get("/api/photos")).json()["photos"]:
        await c.delete(f"/api/photos/{p['photo_id']}", params={"delete_items": "true"})
    left = await storage.list("")
    say(f"  bucket objects left: {len(left)} {left[:3]}")
    every = {"state": ["clean", "dirty", "retired"]}
    say(f"  items left: {(await c.get('/api/items', params=every)).json()['total']}")
    await storage.close()
    await c.aclose()
    say(json.dumps({"ok": not left}))


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve()
    else:
        Path(sys.argv[3]).mkdir(parents=True, exist_ok=True)
        asyncio.run(drive(sys.argv[2], sys.argv[3]))
