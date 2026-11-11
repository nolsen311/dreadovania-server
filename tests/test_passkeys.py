# SPDX-License-Identifier: GPL-3.0-or-later
"""Passkeys: adding one, signing in with it, and everything that must be refused (ADR 0004).

A software authenticator (tests/soft_authenticator.py) stands in for the phone or
password manager, with real keys and signatures.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from soft_authenticator import SoftAuthenticator
from test_api import ORIGIN, H

from dreadovania import auth, passkeys
from dreadovania.app import create_app
from dreadovania.config import Settings


def make_client(tmp_path: Path, fake_runner, public_url: str = ORIGIN) -> TestClient:
    app = create_app(Settings(data_dir=tmp_path / "data"), runner=fake_runner)
    client = TestClient(app, base_url=public_url)
    client.__enter__()
    token = auth.create_setup_token(app.state.db)
    r = client.post(
        "/api/v1/setup",
        json={"token": token, "username": "samus", "password": "correct horse battery",
              "public_url": public_url},
        headers={"Origin": public_url},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return client


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    c = make_client(tmp_path, fake_runner)
    yield c
    c.__exit__(None, None, None)


def add_passkey(client: TestClient, device: SoftAuthenticator, name: str = "Phone") -> dict:
    start = client.post("/api/v1/passkeys/options", headers=H)
    assert start.status_code == 200, start.text
    body = start.json()
    r = client.post(
        "/api/v1/passkeys",
        json={"challenge_id": body["challenge_id"], "response": device.create(body["options"]),
              "name": name},
        headers=H,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


def sign_in(client: TestClient, device: SoftAuthenticator, **get_kwargs):
    start = client.post("/api/v1/auth/passkey/options", headers=H).json()
    response = device.get(start["options"], **get_kwargs)
    return client.post(
        "/api/v1/auth/passkey",
        json={"challenge_id": start["challenge_id"], "response": response},
        headers=H,
    )


def test_add_a_passkey_then_sign_in_with_it(client: TestClient) -> None:
    device = SoftAuthenticator(ORIGIN)
    added = add_passkey(client, device)
    assert (added["name"], added["rp_id"], added["last_used_at"]) == ("Phone", "dread.test", None)
    assert client.get("/api/v1/passkeys").json() == [added]

    client.cookies.clear()
    assert client.get("/api/v1/auth/session").status_code == 401
    r = sign_in(client, device)
    assert r.status_code == 200, r.text
    assert r.json() == {"username": "samus"}
    assert client.get("/api/v1/auth/session").json() == {"username": "samus", "admin": True}
    assert client.get("/api/v1/passkeys").json()[0]["last_used_at"]


def test_the_options_ask_for_a_discoverable_verified_passkey(client: TestClient) -> None:
    options = client.post("/api/v1/passkeys/options", headers=H).json()["options"]
    assert options["rp"] == {"id": "dread.test", "name": "Dreadovania"}
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    device = SoftAuthenticator(ORIGIN)
    add_passkey(client, device)
    options = client.post("/api/v1/passkeys/options", headers=H).json()["options"]
    assert [c["id"] for c in options["excludeCredentials"]] == [device.id]
    sign_in_options = client.post("/api/v1/auth/passkey/options", headers=H).json()["options"]
    assert sign_in_options["rpId"] == "dread.test"
    assert sign_in_options.get("allowCredentials", []) == []  # any passkey; no username


def test_a_challenge_is_used_once(client: TestClient) -> None:
    device = SoftAuthenticator(ORIGIN)
    add_passkey(client, device)
    client.cookies.clear()
    start = client.post("/api/v1/auth/passkey/options", headers=H).json()
    body = {"challenge_id": start["challenge_id"], "response": device.get(start["options"])}
    assert client.post("/api/v1/auth/passkey", json=body, headers=H).status_code == 200
    client.cookies.clear()
    replay = client.post("/api/v1/auth/passkey", json=body, headers=H)
    assert replay.status_code == 401
    assert "expired" in replay.json()["detail"]


def test_responses_for_another_site_are_refused(client: TestClient) -> None:
    device = SoftAuthenticator(ORIGIN)
    add_passkey(client, device)
    client.cookies.clear()
    assert sign_in(client, device, origin="https://evil.test").status_code == 401
    assert sign_in(client, device, rp_id="evil.test").status_code == 401
    assert sign_in(client, device).status_code == 200


def test_an_unknown_passkey_or_bad_signature_is_refused(client: TestClient) -> None:
    add_passkey(client, SoftAuthenticator(ORIGIN))
    client.cookies.clear()
    r = sign_in(client, SoftAuthenticator(ORIGIN))  # never added
    assert r.status_code == 401
    assert "isn't saved on this server" in r.json()["detail"]

    device = SoftAuthenticator(ORIGIN)
    client.post("/api/v1/auth/login", json={"username": "samus",
                "password": "correct horse battery"}, headers=H)  # fmt: skip
    add_passkey(client, device)
    client.cookies.clear()
    start = client.post("/api/v1/auth/passkey/options", headers=H).json()
    response = device.get(start["options"])
    response["response"]["signature"] = SoftAuthenticator(ORIGIN).get(start["options"])["response"][
        "signature"
    ]
    r = client.post(
        "/api/v1/auth/passkey",
        json={"challenge_id": start["challenge_id"], "response": response},
        headers=H,
    )
    assert r.status_code == 401


def test_the_device_must_verify_the_user(client: TestClient) -> None:
    start = client.post("/api/v1/passkeys/options", headers=H).json()
    r = client.post(
        "/api/v1/passkeys",
        json={"challenge_id": start["challenge_id"],
              "response": SoftAuthenticator(ORIGIN, verify_user=False).create(start["options"])},
        headers=H,
    )  # fmt: skip
    assert r.status_code == 400
    assert client.get("/api/v1/passkeys").json() == []


def test_removing_a_passkey(client: TestClient) -> None:
    device = SoftAuthenticator(ORIGIN)
    added = add_passkey(client, device)
    assert client.delete(f"/api/v1/passkeys/{added['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/passkeys/{added['id']}", headers=H).status_code == 404
    client.cookies.clear()
    assert sign_in(client, device).status_code == 401


def test_adding_needs_sign_in_and_the_right_origin(client: TestClient) -> None:
    assert client.post("/api/v1/passkeys/options").status_code == 403  # no Origin
    client.cookies.clear()
    assert client.post("/api/v1/passkeys/options", headers=H).status_code == 401
    r = client.post("/api/v1/auth/passkey/options", headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("public_url", "reason"),
    [("http://10.3.13.2:58080", "not an IP address"), ("http://dread.lan", "need HTTPS")],
)
def test_passkeys_need_https_at_a_hostname(
    tmp_path: Path, fake_runner, public_url: str, reason: str
) -> None:
    client = make_client(tmp_path, fake_runner, public_url)
    try:
        r = client.post("/api/v1/auth/passkey/options", headers={"Origin": public_url})
        assert r.status_code == 409
        assert reason in r.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_localhost_counts_as_secure() -> None:
    assert passkeys.party_for("http://localhost:8080").rp_id == "localhost"


def test_starting_a_sign_in_is_rate_limited(client: TestClient) -> None:
    for _ in range(60):
        assert client.post("/api/v1/auth/passkey/options", headers=H).status_code == 200
    r = client.post("/api/v1/auth/passkey/options", headers=H)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_the_pages(client: TestClient) -> None:
    device = SoftAuthenticator(ORIGIN)
    added = add_passkey(client, device, name="Nate's phone")
    page = client.get("/account").text
    assert "Nate&#39;s phone" in page
    assert 'id="passkey-add"' in page
    assert 'href="/account"' in client.get("/").text
    r = client.post(f"/account/passkeys/{added['id']}/delete", headers={**H, "HX-Request": "true"})
    assert r.status_code == 204
    assert client.get("/api/v1/passkeys").json() == []
    client.cookies.clear()
    login = client.get("/login").text
    assert "Sign in with a passkey" in login
    assert 'autocomplete="username webauthn"' in login


def test_the_pages_explain_when_passkeys_are_off(tmp_path: Path, fake_runner) -> None:
    client = make_client(tmp_path, fake_runner, "http://10.3.13.2:58080")
    try:
        assert "available on this server yet" in client.get("/account").text
        client.cookies.clear()
        login = client.get("/login").text
        assert "Sign in with a passkey" not in login
        assert "Passkey sign-in is off" in login
        assert 'autocomplete="username"' in login
    finally:
        client.__exit__(None, None, None)
