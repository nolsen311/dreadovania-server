# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure remote access, part 3 (ADR 0023): certificates from pairing and invites,
and renewing a Switch's certificate."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi.testclient import TestClient
from test_accounts import invite, join
from test_api import ORIGIN, H, use_session
from test_devices import as_device, pair, pairing_code
from test_remote_access import Peer, browser_certificate, peer_client, set_peer


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    yield from peer_client(tmp_path, fake_runner)


def open_p12(p12_b64: str, password: str):
    key, cert, _ = pkcs12.load_key_and_certificates(base64.b64decode(p12_b64), password.encode())
    assert key is not None and cert is not None
    return cert


def der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def turn_on(client: TestClient) -> bytes:
    """Secure remote access on, checked by the server; returns the admin's browser cert."""
    admin = browser_certificate(client)
    set_peer(client, Peer("203.0.113.9", admin))
    r = client.put("/api/v1/remote-access", json={"enabled": True, "mode": "server"}, headers=H)
    assert r.status_code == 200, r.text
    return admin


# ---- pairing -------------------------------------------------------------------------------


def user_id(client: TestClient, username: str) -> str:
    with client.app.state.db.connect() as conn:  # type: ignore[attr-defined]
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def test_pairing_hands_the_switch_its_certificate(client: TestClient) -> None:
    paired = pair(client, pairing_code(client))
    cert = paired["certificate"]
    x509 = open_p12(cert["p12"], cert["password"])
    assert x509.fingerprint(hashes.SHA256()).hex() == cert["sha256"]
    assert paired["server_url"] == ORIGIN
    start, end = x509.not_valid_before_utc, x509.not_valid_after_utc
    renew = datetime.fromisoformat(cert["renew_after"])
    assert abs((end - renew) - (end - start) / 3) < timedelta(minutes=10)  # a third left
    certs = client.app.state.client_certs  # type: ignore[attr-defined]
    mine = [c for c in certs.list(user_id(client, "samus")) if c["kind"] == "device"]
    assert [c["device_id"] for c in mine] == [paired["device_id"]]


def test_the_switch_renews_and_using_the_new_one_revokes_the_old(client: TestClient) -> None:
    paired = pair(client, pairing_code(client))
    old = open_p12(paired["certificate"]["p12"], paired["certificate"]["password"])
    token = as_device(paired["token"])
    client.cookies.clear()  # the app has only its token
    set_peer(client, Peer("198.51.100.20", der(old)))
    r = client.post("/api/v1/devices/me/certificate", headers=token)
    assert r.status_code == 200, r.text
    new = open_p12(r.json()["p12"], r.json()["password"])
    certs = client.app.state.client_certs  # type: ignore[attr-defined]
    old_fp, new_fp = paired["certificate"]["sha256"], r.json()["sha256"]
    assert certs.is_valid(old_fp) and certs.is_valid(new_fp)  # no gap while it installs
    assert client.get("/api/v1/devices/me", headers=token).status_code == 200  # still the old
    assert certs.is_valid(old_fp)
    set_peer(client, Peer("198.51.100.20", der(new)))  # now it connects with the new one
    assert client.get("/api/v1/devices/me", headers=token).status_code == 200
    assert not certs.is_valid(old_fp) and certs.is_valid(new_fp)


def test_a_switch_paired_before_certificates_gets_its_first_one(client: TestClient) -> None:
    paired = pair(client, pairing_code(client))
    with client.app.state.db.connect() as conn:  # type: ignore[attr-defined]
        conn.execute("DELETE FROM client_certificates")  # as if paired before part 3
    client.cookies.clear()
    r = client.post("/api/v1/devices/me/certificate", headers=as_device(paired["token"]))
    assert r.status_code == 200, r.text
    certs = client.app.state.client_certs  # type: ignore[attr-defined]
    assert certs.by_fingerprint(r.json()["sha256"])["device_id"] == paired["device_id"]


def test_browsers_cant_ask_for_a_switch_certificate(client: TestClient) -> None:
    r = client.post("/api/v1/devices/me/certificate", headers=H)
    assert r.status_code in (401, 403)


def test_unpairing_revokes_the_switch_certificate(client: TestClient) -> None:
    paired = pair(client, pairing_code(client))
    assert client.delete(f"/api/v1/devices/{paired['device_id']}", headers=H).status_code == 204
    certs = client.app.state.client_certs  # type: ignore[attr-defined]
    assert not certs.is_valid(paired["certificate"]["sha256"])


# ---- invites -------------------------------------------------------------------------------


def test_an_invite_hands_out_no_certificate_while_it_is_off(client: TestClient) -> None:
    created = invite(client)
    admin_cookie = client.cookies["dreadovania_session"]
    client.cookies.clear()
    r = join(client, created["token"])
    assert r.status_code == 201 and r.json()["certificate"] is None
    use_session(client, admin_cookie)


def test_an_invite_hands_out_a_browser_certificate_while_it_is_on(client: TestClient) -> None:
    turn_on(client)
    created = invite(client)
    admin_cookie = client.cookies["dreadovania_session"]
    set_peer(client, Peer("198.51.100.4", None, front_door=True))
    client.cookies.clear()
    r = client.post(
        "/api/v1/invites/accept", headers={"Origin": "https://dread.test:8081"},
        json={"token": created["token"], "username": "ridley", "password": "pirates pirates"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    body = r.json()["certificate"]
    open_p12(body["p12"], body["password"])
    certs = client.app.state.client_certs  # type: ignore[attr-defined]
    issued = certs.by_fingerprint(body["certificate"]["sha256"])
    assert issued["owner_id"] == user_id(client, "ridley") and issued["kind"] == "browser"
    use_session(client, admin_cookie)


def test_joining_on_the_front_door_page_gives_a_one_time_download(client: TestClient) -> None:
    turn_on(client)
    created = invite(client)
    set_peer(client, Peer("198.51.100.4", None, front_door=True))
    client.cookies.clear()
    door = {"Origin": "https://dread.test:8081"}
    r = client.post(
        "/invite", data={"token": created["token"], "username": "kraid",
                         "password": "pirates pirates"},
        headers=door, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303, r.text
    welcome = r.headers["location"]
    assert welcome.startswith("/invite/welcome/")
    page = client.get(welcome)  # on the front door: no certificate yet
    assert page.status_code == 200 and "Download your certificate" in page.text
    password = page.text.split('aria-label="Certificate password">')[1].split("<")[0]
    assert client.get(welcome).status_code == 404  # the password is shown once
    link = welcome.replace("/invite/welcome/", "/invite/certificate/")
    p12 = client.get(link)
    assert p12.headers["content-type"] == "application/x-pkcs12"
    pkcs12.load_key_and_certificates(p12.content, password.encode())
    assert client.get(link).status_code == 404  # and downloaded once
    assert client.get("/").status_code == 404  # the rest stays shut on the front door


def test_the_settings_page_warns_about_accounts_without_a_certificate(client) -> None:
    created = invite(client)
    admin_cookie = client.cookies["dreadovania_session"]
    client.cookies.clear()
    assert join(client, created["token"]).status_code == 201
    use_session(client, admin_cookie)
    r = client.post("/account/remote-access", data={"action": "choose", "mode": "proxy"},
                    headers=H)  # fmt: skip
    assert r.status_code == 200, r.text
    page = client.get("/account/remote-access").text
    assert "Without a browser certificate yet:" in page and "ridley" in page and "samus" in page
    browser_certificate(client)
    page = client.get("/account/remote-access").text
    assert (
        "ridley" in page
        and "samus" not in page.split("Without a browser certificate yet:")[1].split("</p>")[0]
    )
