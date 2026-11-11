# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in over HTTP, including the security properties (ADR 0011 §4 and §6)."""

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings

ORIGIN = "https://dread.test"
PASSWORD = "correct horse battery"
SAME_SITE = {"Origin": ORIGIN}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def setup_account(client: TestClient, **overrides: str) -> object:
    body = {
        "token": auth.create_setup_token(client.app.state.db),
        "username": "samus",
        "password": PASSWORD,
        "public_url": ORIGIN,
    } | overrides
    return client.post("/api/v1/setup", json=body, headers=SAME_SITE)


def protected_routes(client: TestClient) -> list[tuple[str, str]]:
    """Every API route except the deliberately public ones."""
    public = {("GET", "/api/v1/setup"), ("POST", "/api/v1/setup"), ("POST", "/api/v1/auth/login"),
              ("POST", "/api/v1/auth/passkey/options"), ("POST", "/api/v1/auth/passkey"),
              ("POST", "/api/v1/devices/pair"), ("POST", "/api/v1/invites/accept"),
              ("GET", "/api/v1/info")}  # fmt: skip
    routes = []
    for path, ops in client.app.openapi()["paths"].items():
        for method in ops:
            if path.startswith("/api/") and (method.upper(), path) not in public:
                routes.append((method.upper(), path))
    return routes


# ---- before setup ----------------------------------------------------------------------


def test_startup_logs_a_one_time_setup_link(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING)
    with TestClient(create_app(Settings(data_dir=tmp_path / "d", public_url=ORIGIN))):
        pass
    match = re.search(r"https://dread\.test/setup#token=(\S+)", caplog.text)
    assert match, caplog.text  # the token is in the fragment, never sent to the server


def test_before_setup_everything_but_setup_is_unavailable(client) -> None:
    assert client.get("/api/v1/setup").json() == {"setup_required": True}
    assert (
        client.post("/api/v1/auth/login", json={"username": "a", "password": "b"}).status_code
        == 503
    )
    assert client.post("/api/v1/auth/passkey/options").status_code == 503
    body = {"challenge_id": "x", "response": {}}
    assert client.post("/api/v1/auth/passkey", json=body).status_code == 503
    pairing = client.post("/api/v1/devices/pair", json={"code": "ABCD-EFGH"})
    assert pairing.status_code == 503
    invite = {"token": "x", "username": "a", "password": "b" * 12}
    assert client.post("/api/v1/invites/accept", json=invite).status_code == 503
    for method, path in protected_routes(client):
        response = client.request(method, path, headers=SAME_SITE)
        assert response.status_code == 503, (method, path)
        assert response.headers["content-type"] == "application/problem+json"


# ---- setup -------------------------------------------------------------------------------


def test_setup_signs_in_with_a_hardened_cookie(client) -> None:
    response = setup_account(client)
    assert response.status_code == 201, response.text
    assert response.json() == {"username": "samus"}
    cookie = response.headers["set-cookie"].lower()
    for flag in ("httponly", "samesite=lax", "secure", "path=/", "max-age=7776000"):
        assert flag in cookie, cookie
    assert client.get("/api/v1/auth/session").json() == {"username": "samus", "admin": True}
    assert client.get("/api/v1/setup").json() == {"setup_required": False}


def test_cookie_is_not_secure_on_plain_http(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "d")), base_url="http://lan.test") as c:
        token = auth.create_setup_token(c.app.state.db)
        response = c.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": PASSWORD,
                  "public_url": "http://lan.test"},
            headers={"Origin": "http://lan.test"},
        )  # fmt: skip
    assert response.status_code == 201
    assert "secure" not in response.headers["set-cookie"].lower()


def test_setup_with_wrong_token_is_refused(client) -> None:
    auth.create_setup_token(client.app.state.db)
    response = client.post(
        "/api/v1/setup",
        json={"token": "guess", "username": "samus", "password": PASSWORD, "public_url": ORIGIN},
        headers=SAME_SITE,
    )
    assert response.status_code == 403
    assert client.get("/api/v1/setup").json() == {"setup_required": True}


def test_setup_runs_only_once(client) -> None:
    assert setup_account(client).status_code == 201
    assert setup_account(client, username="ridley").status_code == 409


def test_setup_from_a_different_origin_is_refused(client) -> None:
    response = client.post(
        "/api/v1/setup",
        json={"token": auth.create_setup_token(client.app.state.db), "username": "samus",
              "password": PASSWORD, "public_url": ORIGIN},
        headers={"Origin": "https://evil.example"},
    )  # fmt: skip
    assert response.status_code == 403


def test_setup_rejects_a_weak_password(client) -> None:
    response = setup_account(client, password="short")
    assert response.status_code == 400
    assert "12" in response.json()["detail"]


def test_password_never_appears_in_logs(client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    setup_account(client)
    client.post("/api/v1/auth/login", json={"username": "samus", "password": PASSWORD})
    assert PASSWORD not in caplog.text


# ---- login, session, logout ------------------------------------------------------------------


@pytest.fixture
def account(client) -> TestClient:
    assert setup_account(client).status_code == 201
    client.cookies.clear()
    return client


def test_login_and_logout(account) -> None:
    response = account.post(
        "/api/v1/auth/login", json={"username": "Samus", "password": PASSWORD}, headers=SAME_SITE
    )
    assert response.status_code == 200
    assert account.get("/api/v1/auth/session").json() == {"username": "samus", "admin": True}

    stolen = account.cookies.get("dreadovania_session")
    assert account.post("/api/v1/auth/logout", headers=SAME_SITE).status_code == 204
    assert account.get("/api/v1/auth/session").status_code == 401
    # The old cookie is useless now: logout revoked it on the server, not just in the browser.
    account.cookies.set("dreadovania_session", stolen, domain="dread.test")
    assert account.get("/api/v1/auth/session").status_code == 401


def test_login_failures_are_generic(account) -> None:
    wrong = account.post("/api/v1/auth/login", json={"username": "samus", "password": "nope" * 4})
    nobody = account.post("/api/v1/auth/login", json={"username": "kraid", "password": "nope" * 4})
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()


def test_login_is_rate_limited(account) -> None:
    for _ in range(routes_limit()):
        account.post("/api/v1/auth/login", json={"username": "samus", "password": "wrong" * 3})
    response = account.post("/api/v1/auth/login", json={"username": "samus", "password": PASSWORD})
    assert response.status_code == 429  # even the right password waits
    assert int(response.headers["retry-after"]) > 0


def routes_limit() -> int:
    from dreadovania.routes_auth import LOGIN_LIMIT

    return LOGIN_LIMIT["per_client"]


def test_every_protected_route_needs_a_session(account) -> None:
    assert protected_routes(account)  # sanity: there is something to check
    for method, path in protected_routes(account):
        response = account.request(method, path, headers=SAME_SITE)
        assert response.status_code == 401, (method, path)


def test_bogus_session_cookie_is_refused(account) -> None:
    account.cookies.set("dreadovania_session", "forged", domain="dread.test")
    assert account.get("/api/v1/auth/session").status_code == 401


# ---- CSRF -------------------------------------------------------------------------------------


@pytest.fixture
def signed_in(client) -> TestClient:
    assert setup_account(client).status_code == 201
    return client


def test_cross_site_change_is_refused(signed_in) -> None:
    response = signed_in.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert signed_in.get("/api/v1/auth/session").status_code == 200  # still signed in


def test_a_refused_origin_says_which_address_to_open(signed_in) -> None:
    # The usual real-world cause: the server opened at its IP address while the
    # public URL chosen at setup is its hostname (or http vs https).
    response = signed_in.post("/logout", headers={"Origin": "http://192.168.1.20:8080"})
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "http://192.168.1.20:8080" in detail
    assert signed_in.base_url.scheme + "://" + signed_in.base_url.host in detail


def test_change_without_origin_is_refused(signed_in) -> None:
    assert signed_in.post("/api/v1/auth/logout").status_code == 403


def test_null_origin_is_refused(signed_in) -> None:
    assert signed_in.post("/api/v1/auth/logout", headers={"Origin": "null"}).status_code == 403


def test_login_from_another_site_is_refused(account) -> None:
    response = account.post(
        "/api/v1/auth/login",
        json={"username": "samus", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_configured_public_url_overrides_the_stored_one(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "d", public_url="https://configured.test")
    with TestClient(create_app(settings), base_url="https://configured.test") as c:
        token = auth.create_setup_token(c.app.state.db)
        c.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": PASSWORD,
                  "public_url": "https://configured.test"},
            headers={"Origin": "https://configured.test"},
        )  # fmt: skip
        assert c.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
        ok = c.post("/api/v1/auth/logout", headers={"Origin": "https://configured.test"})
        assert ok.status_code == 204
