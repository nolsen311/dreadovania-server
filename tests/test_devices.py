# SPDX-License-Identifier: GPL-3.0-or-later
"""Pairing the Switch app and what its device token may do (ADR 0004, ADR 0017)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import ORIGIN, H
from test_passkeys import make_client

from dreadovania import devices


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    c = make_client(tmp_path, fake_runner)
    yield c
    c.__exit__(None, None, None)


def pairing_code(client: TestClient) -> str:
    r = client.post("/api/v1/devices/pairing-code", headers=H)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def pair(client: TestClient, code: str, name: str = "Nate's OG Switch") -> dict:
    # The Switch app sends no Origin; the route is public.
    r = client.post("/api/v1/devices/pair", json={"code": code, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def as_device(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def device(client: TestClient) -> dict:
    return pair(client, pairing_code(client))


def test_pairing_gives_a_working_token(client: TestClient, device: dict) -> None:
    assert device["name"] == "Nate's OG Switch"
    assert device["token"].startswith("dvt_")
    client.cookies.clear()  # from here on, only the token
    me = client.get("/api/v1/devices/me", headers=as_device(device["token"]))
    assert me.status_code == 200, me.text
    assert me.json()["id"] == device["device_id"]
    assert me.json()["last_ip"] == "testclient"
    assert client.get("/api/v1/seeds", headers=as_device(device["token"])).json() == []
    presets = client.get("/api/v1/presets", headers=as_device(device["token"]))
    assert presets.status_code == 200


def test_the_device_can_generate_and_follow_a_seed(client: TestClient, device: dict) -> None:
    client.cookies.clear()
    headers = as_device(device["token"])
    r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=headers)
    assert r.status_code == 202, r.text
    job = r.json()
    seed_id = job["seed_id"]
    poll = client.get(f"/api/v1/jobs/{job['job_id']}", headers=headers)
    assert poll.status_code == 200
    assert client.get(f"/api/v1/seeds/{seed_id}", headers=headers).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/switches"),
        ("GET", "/api/v1/romfs"),
        ("GET", "/api/v1/devices"),
        ("POST", "/api/v1/devices/pairing-code"),
        ("POST", "/api/v1/passkeys/options"),
        ("GET", "/api/v1/auth/session"),
        ("POST", "/api/v1/presets"),
        ("DELETE", "/api/v1/presets/starter_preset"),
        ("POST", "/api/v1/seeds/purge"),
        ("DELETE", "/api/v1/devices/00000000-0000-4000-8000-000000000000"),
    ],
)
def test_the_token_reaches_nothing_else(
    client: TestClient, device: dict, method: str, path: str
) -> None:
    client.cookies.clear()
    r = client.request(method, path, headers=as_device(device["token"]))
    assert r.status_code == 403, (method, path, r.text)
    assert "web app" in r.json()["detail"]


def test_every_device_route_exists() -> None:
    from dreadovania.app import create_app

    paths = {(m.upper(), p) for p, ops in create_app().openapi()["paths"].items() for m in ops}
    assert paths >= devices.DEVICE_ROUTES  # a typo would silently lock the app out


def test_unknown_or_malformed_tokens_are_refused(client: TestClient) -> None:
    client.cookies.clear()
    for header in ("Bearer dvt_nope", "Bearer nope", "Basic abc", "Bearer"):
        r = client.get("/api/v1/seeds", headers={"Authorization": header})
        assert r.status_code == 401, header
        assert r.headers["www-authenticate"] == "Bearer"


def test_a_token_wins_over_a_session_cookie(client: TestClient) -> None:
    # With a cookie the browser is signed in; a wrong token still fails.
    assert client.get("/api/v1/switches").status_code == 200
    r = client.get("/api/v1/switches", headers={"Authorization": "Bearer dvt_nope"})
    assert r.status_code == 401


def test_codes_work_once_and_forgive_formatting(client: TestClient) -> None:
    code = pairing_code(client)
    assert len(code) == 9 and code[4] == "-"
    pair(client, code.replace("-", "").lower())  # typed without the dash, in lower case
    r = client.post("/api/v1/devices/pair", json={"code": code})
    assert r.status_code == 400
    assert "wrong or has expired" in r.json()["detail"]


def test_a_new_code_replaces_the_old(client: TestClient) -> None:
    old = pairing_code(client)
    new = pairing_code(client)
    assert client.post("/api/v1/devices/pair", json={"code": old}).status_code == 400
    pair(client, new)


def test_expired_and_malformed_codes(client: TestClient) -> None:
    code = pairing_code(client)
    with client.app.state.db.connect() as conn:
        conn.execute("UPDATE pairing_codes SET expires_at = '2000-01-01T00:00:00+00:00'")
    r = client.post("/api/v1/devices/pair", json={"code": code})
    assert r.status_code == 400
    assert "expired" in r.json()["detail"]
    r = client.post("/api/v1/devices/pair", json={"code": "0000-0000"})  # 0 isn't used
    assert "isn't a pairing code" in r.json()["detail"]


def test_guessing_codes_is_rate_limited(client: TestClient) -> None:
    from dreadovania.routes_auth import PAIR_LIMIT

    for _ in range(PAIR_LIMIT["per_client"]):
        client.post("/api/v1/devices/pair", json={"code": "AAAA-AAAA"})
    code = pairing_code(client)
    r = client.post("/api/v1/devices/pair", json={"code": code})
    assert r.status_code == 429  # even the right code waits


def test_revoking_a_device(client: TestClient, device: dict) -> None:
    listed = client.get("/api/v1/devices").json()
    assert [d["id"] for d in listed] == [device["device_id"]]
    assert "token" not in listed[0]
    r = client.delete(f"/api/v1/devices/{device['device_id']}", headers=H)
    assert r.status_code == 204
    assert client.get("/api/v1/seeds", headers=as_device(device["token"])).status_code == 401


def test_a_device_can_unpair_itself(client: TestClient, device: dict) -> None:
    client.cookies.clear()
    headers = as_device(device["token"])
    assert client.delete("/api/v1/devices/me", headers=headers).status_code == 204
    assert client.get("/api/v1/devices/me", headers=headers).status_code == 401


def test_devices_me_is_only_for_devices(client: TestClient) -> None:
    assert client.get("/api/v1/devices/me").status_code == 404


def test_only_hashes_are_stored(client: TestClient, device: dict) -> None:
    with client.app.state.db.connect() as conn:
        dump = "\n".join(conn.iterdump())
    assert device["token"] not in dump
    assert device["token"][len(devices.TOKEN_PREFIX) :] not in dump


def test_the_account_page(client: TestClient, device: dict) -> None:
    page = client.get("/account").text
    assert "Pair a Switch" in page
    assert "Nate&#39;s OG Switch" in page
    code_html = client.post("/account/devices/code", headers={**H, "HX-Request": "true"}).text
    assert 'class="pairing-code"' in code_html
    assert ORIGIN in code_html
    r = client.post(
        f"/account/devices/{device['device_id']}/delete", headers={**H, "HX-Request": "true"}
    )
    assert r.status_code == 204
    assert client.get("/api/v1/devices").json() == []
