"""Pairing, tokens, revoke, and the localhost-only guard."""

from __future__ import annotations

import psycopg

from mirror_api.auth import PairingCodes, hash_token
from tests.conftest import LAN_CLIENT, http_client


async def test_pair_code_is_localhost_only(client, remote):
    r = await remote.post("/api/pair/code")
    assert r.status_code == 403
    r = await client.post("/api/pair/code")
    assert r.status_code == 200
    body = r.json()
    assert len(body["code"]) == 8
    assert body["pair_url"].endswith(f"/pair?code={body['code']}")
    assert body["qr_png"].startswith("data:image/png;base64,")


async def test_pair_url_uses_given_base(client):
    r = await client.post("/api/pair/code", json={"base_url": "http://192.168.1.20:8000/"})
    assert r.json()["pair_url"].startswith("http://192.168.1.20:8000/pair?code=")


async def test_pair_flow_stores_only_hash(client, remote, settings):
    code = (await client.post("/api/pair/code")).json()["code"]
    r = await remote.post("/api/pair", json={"code": code.lower()[:4] + "-" + code[4:],
                                             "name": "Phone"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert len(token) >= 40
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute("SELECT token_hash, kind FROM devices").fetchall()
    assert rows == [(hash_token(token), "phone")]
    assert token not in rows[0][0]


async def test_code_is_single_use(client, remote):
    code = (await client.post("/api/pair/code")).json()["code"]
    assert (await remote.post("/api/pair", json={"code": code})).status_code == 201
    assert (await remote.post("/api/pair", json={"code": code})).status_code == 400


async def test_new_code_replaces_old(client, remote):
    old = (await client.post("/api/pair/code")).json()["code"]
    new = (await client.post("/api/pair/code")).json()["code"]
    assert (await remote.post("/api/pair", json={"code": old})).status_code == 400
    assert (await remote.post("/api/pair", json={"code": new})).status_code == 201


async def test_wrong_code_rejected(client, remote):
    await client.post("/api/pair/code")
    r = await remote.post("/api/pair", json={"code": "ZZZZZZZZ"})
    assert r.status_code == 400


def test_code_expiry_and_lockout():
    now = [0.0]
    codes = PairingCodes(ttl_s=300, max_failures=3, clock=lambda: now[0])
    code, _ = codes.issue()
    now[0] = 301
    assert not codes.redeem(code)  # expired

    code, _ = codes.issue()
    for _ in range(3):
        assert not codes.redeem("AAAAAAAA" if code != "AAAAAAAA" else "BBBBBBBB")
    assert not codes.redeem(code)  # burned after 3 wrong guesses


async def test_authed_route_requires_token(remote, authed):
    assert (await remote.get("/api/devices")).status_code == 401
    r = await authed.get("/api/devices")
    assert r.status_code == 200
    devices = r.json()
    assert len(devices) == 1 and "token_hash" not in devices[0]


async def test_bad_token_rejected_even_from_localhost(app):
    async with http_client(app, ("127.0.0.1", 1), token="nope") as c:
        assert (await c.get("/api/devices")).status_code == 401


async def test_localhost_is_trusted_as_mirror(client):
    assert (await client.get("/api/devices")).status_code == 200


async def test_revoke(app, authed, paired):
    device_id, token = paired
    r = await authed.delete(f"/api/devices/{device_id}")
    assert r.status_code == 204
    # Rejected immediately: revoke clears the auth cache.
    assert (await authed.get("/api/devices")).status_code == 401
    async with http_client(app, LAN_CLIENT, token=token) as c:
        assert (await c.get("/api/settings")).status_code == 401
    assert (await authed.delete(f"/api/devices/{device_id}")).status_code == 401


async def test_revoke_unknown_is_404(client):
    r = await client.delete("/api/devices/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
