# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure remote access (ADR 0023, part 2): the setting, the checks, the TLS layer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from conftest import make_user
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi.testclient import TestClient
from test_accounts import as_member, invite
from test_api import ORIGIN, H, use_session
from test_tls import HOST, client_context, free_port, make_pair

from dreadovania import __main__ as entry
from dreadovania import auth, remote_access, tls
from dreadovania.app import create_app
from dreadovania.client_certs import ClientCertificates
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.remote_access import Peer, RemoteAccess, RemoteAccessError

# ---- the front door and the headers -------------------------------------------------------


def test_the_front_door_is_pairing_invites_and_their_files() -> None:
    for method, path in [("POST", "/api/v1/devices/pair"), ("GET", "/invite"),
                         ("POST", "/invite"), ("POST", "/api/v1/invites/accept"),
                         ("GET", "/static/app.css"), ("GET", "/healthz")]:  # fmt: skip
        assert remote_access.is_front_door(method, path), (method, path)
    for method, path in [("GET", "/"), ("GET", "/setup"), ("POST", "/api/v1/setup"),
                         ("GET", "/api/v1/devices/pair"), ("POST", "/static/x"),
                         ("GET", "/api/v1/seeds"), ("GET", "/invites")]:  # fmt: skip
        assert not remote_access.is_front_door(method, path), (method, path)
    listed = remote_access.front_door_list()
    assert "POST /api/v1/devices/pair" in listed and "GET /static/*" in listed


def test_the_cloudflare_rule_blocks_everything_but_the_front_door() -> None:
    rule = remote_access.cloudflare_expression()
    assert rule.startswith("(not cf.tls_client_auth.cert_verified")
    assert '"/api/v1/devices/pair"' in rule and '"/invite"' in rule
    assert 'starts_with(http.request.uri.path, "/static/")' in rule


def test_forwarded_certificate_headers() -> None:
    der = b"\x30\x03\x02\x01\x01"
    fp = hashlib.sha256(der).hexdigest()
    pretty = ":".join(fp[i : i + 2] for i in range(0, 64, 2)).upper()
    assert remote_access.forwarded_fingerprint({"x-ssl-client-sha256": pretty}) == fp
    b64 = base64.b64encode(der).decode()
    assert remote_access.forwarded_fingerprint({"client-cert": f":{b64}:"}) == fp
    for bad in [{"x-ssl-client-sha256": "abc"}, {"client-cert": b64},
                {"client-cert": ":!!:"}, {"client-cert": "::"}, {}]:  # fmt: skip
        assert remote_access.forwarded_fingerprint(bad) is None


def test_only_listed_proxies_are_trusted() -> None:
    nets = ["172.18.0.0/16", "10.0.0.2"]
    assert remote_access.is_trusted("172.18.4.5", nets)
    assert remote_access.is_trusted("10.0.0.2", nets)
    assert not remote_access.is_trusted("10.0.0.3", nets)
    assert not remote_access.is_trusted("testclient", nets)
    assert not remote_access.is_trusted("172.18.4.5", [])


# ---- the setting ---------------------------------------------------------------------------


@pytest.fixture
def certs(db: Database, tmp_path: Path) -> ClientCertificates:
    make_user(db, "alice")
    make_user(db, "bob", role="member")
    return ClientCertificates(db, tmp_path / "data")


def remote(db, certs, *, serves_tls=True, proxies=("172.18.0.0/16",)) -> RemoteAccess:
    return RemoteAccess(
        db, certs, serves_tls=serves_tls, trusted_proxies=list(proxies), cache_seconds=0
    )


def der_of(issued) -> bytes:
    _key, cert, _ = pkcs12.load_key_and_certificates(issued.p12, issued.password.encode())
    return cert.public_bytes(serialization.Encoding.DER)


def test_off_by_default_and_admin_only(db, certs) -> None:
    ra = remote(db, certs)
    assert ra.state() == remote_access.State()
    assert ra.check("GET", "/", None, {}) is None
    with pytest.raises(Exception, match="admin"):
        ra.choose("bob", "proxy")
    with pytest.raises(RemoteAccessError, match="doesn't serve HTTPS"):
        remote(db, certs, serves_tls=False).choose("alice", "server")


def test_the_server_checks_only_once_this_browser_has_a_certificate(db, certs) -> None:
    ra = remote(db, certs)
    ra.choose("alice", "server")
    assert not ra.state().enabled
    with pytest.raises(RemoteAccessError, match="didn't present a certificate"):
        ra.turn_on("alice", None, confirmed=True)  # confirming isn't enough here
    bobs = certs.issue("bob", "browser", "Bob's phone")
    with pytest.raises(RemoteAccessError, match="isn't one of yours"):
        ra.turn_on("alice", certs.by_fingerprint(bobs.certificate["sha256"]), confirmed=False)
    mine = certs.issue("alice", "browser", "Laptop")
    peer = Peer("203.0.113.9", der_of(mine))
    assert ra.turn_on("alice", ra.certificate_of(peer, {}), confirmed=False).enabled
    with pytest.raises(RemoteAccessError, match="turn secure remote access off"):
        ra.choose("alice", "proxy")

    assert ra.check("GET", "/", peer, {}) is None
    assert ra.check("GET", "/", Peer("203.0.113.9", None), {}).status == 403
    assert ra.check("GET", "/", None, {}).status == 403
    assert ra.check("POST", "/api/v1/devices/pair", None, {}) is None  # the front door
    certs.revoke("alice", mine.certificate["id"])  # at once, even on an open connection
    assert ra.check("GET", "/", peer, {}).status == 403
    assert not ra.turn_off(None).enabled  # the command line, for a locked-out owner


def test_the_front_door_port_serves_only_the_front_door(db, certs) -> None:
    ra = remote(db, certs)
    door = Peer("203.0.113.9", None, front_door=True)
    assert ra.check("POST", "/api/v1/devices/pair", door, {}) is None
    assert ra.check("GET", "/static/app.css", door, {}) is None
    for path in ("/", "/setup", "/api/v1/seeds", "/account"):
        assert ra.check("GET", path, door, {}).status == 404  # on or off


def test_behind_a_proxy_the_forwarded_certificate_is_checked(db, certs) -> None:
    ra = remote(db, certs)
    ra.choose("alice", "proxy", check_forwarded=True)
    mine = certs.issue("alice", "browser", "Laptop")
    header = {"x-ssl-client-sha256": mine.certificate["sha256"]}
    proxy, stranger = Peer("172.18.0.3", None), Peer("198.51.100.7", None)
    assert ra.certificate_of(stranger, header) is None  # only from trusted proxies
    with pytest.raises(RemoteAccessError):
        ra.turn_on("alice", ra.certificate_of(stranger, header), confirmed=True)
    ra.turn_on("alice", ra.certificate_of(proxy, header), confirmed=False)
    assert ra.check("GET", "/", proxy, header) is None
    assert ra.check("GET", "/", proxy, {}).status == 403
    assert ra.check("GET", "/", stranger, header).status == 403
    elsewhere = Database(certs.dir.parent.parent / "other.db")
    elsewhere.migrate()
    make_user(elsewhere, "alice")
    other = ClientCertificates(elsewhere, certs.dir.parent.parent / "other").issue(
        "alice", "browser", "Another server's"
    )
    refused = ra.check("GET", "/", proxy, {"x-ssl-client-sha256": other.certificate["sha256"]})
    assert refused is not None and refused.status == 403


def test_when_only_the_proxy_checks_the_admin_confirms(db, certs) -> None:
    ra = remote(db, certs)
    ra.choose("alice", "proxy", check_forwarded=False)
    with pytest.raises(RemoteAccessError, match="confirm"):
        ra.turn_on("alice", None, confirmed=False)
    assert ra.turn_on("alice", None, confirmed=True).enabled
    assert ra.check("GET", "/", None, {}) is None  # the proxy is trusted to have checked


def test_cloudflare_can_be_chosen_but_not_turned_on_yet(db, certs) -> None:
    ra = remote(db, certs)
    ra.choose("alice", "cloudflare", check_forwarded=False)
    with pytest.raises(RemoteAccessError, match="Cloudflare"):
        ra.turn_on("alice", None, confirmed=True)


def test_what_the_main_port_asks_of_clients(db, certs) -> None:
    ra = remote(db, certs)
    ra.choose("alice", "server")
    assert ra.verify_mode() == ssl.CERT_NONE  # no CA yet: nothing to check against
    mine = certs.issue("alice", "browser", "Laptop")
    assert ra.verify_mode() == ssl.CERT_OPTIONAL  # so the admin can check theirs first
    ra.turn_on("alice", ra.certificate_of(Peer("1.2.3.4", der_of(mine)), {}), confirmed=False)
    assert ra.verify_mode() == ssl.CERT_REQUIRED
    ra.turn_off("alice")
    ra.choose("alice", "proxy")
    assert ra.verify_mode() == ssl.CERT_NONE  # the proxy's job


def test_expiry_warning() -> None:
    soon = {"not_after": (datetime.now(UTC) + timedelta(days=12, hours=1)).isoformat()}
    later = {"not_after": (datetime.now(UTC) + timedelta(days=200)).isoformat()}
    assert remote_access.expiring_soon(soon) == 12
    assert remote_access.expiring_soon(later) is None
    assert remote_access.expiring_soon(None) is None


# ---- through the app ---------------------------------------------------------------------


class PeerApp:
    """Stands in for ``connection.py``: puts a chosen peer in each request's scope."""

    def __init__(self, app: Any) -> None:
        self.app, self.state, self.peer = app, app.state, None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and self.peer is not None:
            scope[remote_access.PEER_KEY] = self.peer
        await self.app(scope, receive, send)


def peer_client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    """A set-up server whose requests carry the peer set with ``set_peer``."""
    settings = Settings(
        data_dir=tmp_path / "data", tls_cert_file=tmp_path / "unused.pem",
        tls_key_file=tmp_path / "unused.key",
    )  # fmt: skip
    wrapped = PeerApp(create_app(settings, runner=fake_runner))
    with TestClient(wrapped, base_url=ORIGIN) as c:
        token = auth.create_setup_token(wrapped.state.db)
        r = c.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": "correct horse battery",
                  "public_url": ORIGIN},
            headers=H,
        )  # fmt: skip
        assert r.status_code == 201
        wrapped.state.remote_access.cache_seconds = 0
        yield c


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    yield from peer_client(tmp_path, fake_runner)


def set_peer(client: TestClient, peer: Peer | None) -> None:
    client.app.peer = peer  # type: ignore[attr-defined]


def browser_certificate(client: TestClient, name: str = "Laptop") -> bytes:
    body = client.post("/api/v1/certificates", json={"name": name}, headers=H).json()
    _key, cert, _ = pkcs12.load_key_and_certificates(
        base64.b64decode(body["p12"]), body["password"].encode()
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_turning_it_on_through_the_api(client: TestClient) -> None:
    status = client.get("/api/v1/remote-access", headers=H).json()
    assert status["enabled"] is False and status["mode"] is None and status["serves_tls"]
    assert status["front_door_url"] == "https://dread.test:8081"
    r = client.put("/api/v1/remote-access", json={"enabled": True, "mode": "server"}, headers=H)
    assert r.status_code == 409 and "didn't present a certificate" in r.json()["detail"]
    der = browser_certificate(client)
    set_peer(client, Peer("203.0.113.9", der))
    presented = client.get("/api/v1/certificates/presented", headers=H).json()
    assert presented["certificate"]["name"] == "Laptop" and presented["expires_in_days"] is None
    r = client.put("/api/v1/remote-access", json={"enabled": True, "mode": "server"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] and r.json()["presented"]["name"] == "Laptop"

    assert client.get("/api/v1/seeds", headers=H).status_code == 200
    set_peer(client, Peer("203.0.113.9", None))  # a connection without one
    r = client.get("/api/v1/seeds", headers=H)
    assert r.status_code == 403 and r.json()["title"] == "Certificate required"
    page = client.get("/")
    assert page.status_code == 403 and "Certificate required" in page.text
    assert client.post("/api/v1/devices/pair", json={"code": "x", "name": "y"}).status_code == 400
    set_peer(client, Peer("203.0.113.9", der))
    r = client.put("/api/v1/remote-access", json={"enabled": False, "mode": "server"}, headers=H)
    assert r.json()["enabled"] is False


def test_only_the_admin_sees_the_setting(client: TestClient) -> None:
    assert client.get("/api/v1/remote-access/ca.pem", headers=H).text.startswith(
        "-----BEGIN CERTIFICATE-----"
    )
    assert "X509 CRL" in client.get("/api/v1/remote-access/crl.pem", headers=H).text
    as_member(client)
    for path in ("/api/v1/remote-access", "/api/v1/remote-access/ca.pem"):
        assert client.get(path, headers=H).status_code == 403
    r = client.put("/api/v1/remote-access", json={"enabled": False, "mode": "proxy"}, headers=H)
    assert r.status_code == 403
    assert client.get("/account/remote-access").status_code == 403


def test_the_front_door_port(client: TestClient) -> None:
    created = invite(client)
    assert created["url"].startswith(f"{ORIGIN}/invite#token=")  # off: the usual address
    der = browser_certificate(client)
    set_peer(client, Peer("203.0.113.9", der))
    client.put("/api/v1/remote-access", json={"enabled": True, "mode": "server"}, headers=H)
    created = invite(client)
    assert created["url"].startswith("https://dread.test:8081/invite#token=")
    admin_cookie = client.cookies["dreadovania_session"]

    set_peer(client, Peer("198.51.100.4", None, front_door=True))
    client.cookies.clear()
    assert client.get("/").status_code == 404
    assert client.get("/api/v1/seeds").status_code == 404
    assert client.get("/setup").status_code == 404
    assert client.get("/static/app.css").status_code == 200
    page = client.get(f"/invite#token={created['token']}")
    assert page.status_code == 200
    door = {"Origin": "https://dread.test:8081"}
    r = client.post(
        "/api/v1/invites/accept",
        json={"token": created["token"], "username": "ridley", "password": "pirates pirates"},
        headers=door,
    )
    assert r.status_code == 201, r.text
    use_session(client, admin_cookie)


def test_the_account_page_hands_out_a_certificate_once(client: TestClient) -> None:
    page = client.get("/account")
    assert "Add a certificate for this browser or phone" in page.text
    assert "Set up secure remote access" in page.text
    r = client.post("/account/certificates", data={"name": "My phone"}, headers=H)
    assert r.status_code == 200 and "Download the certificate" in r.text
    link = r.text.split('href="/account/certificates/download/')[1].split('"')[0]
    password = r.text.split('aria-label="Certificate password">')[1].split("<")[0]
    download = client.get(f"/account/certificates/download/{link}")
    assert download.headers["content-type"] == "application/x-pkcs12"
    assert 'filename="dreadovania-My-phone.p12"' in download.headers["content-disposition"]
    key, cert, _ = pkcs12.load_key_and_certificates(download.content, password.encode())
    assert key is not None and cert is not None
    assert client.get(f"/account/certificates/download/{link}").status_code == 404  # once
    listed = client.get("/api/v1/certificates", headers=H).json()
    assert [c["name"] for c in listed] == ["My phone"]
    r = client.post(f"/account/certificates/{listed[0]['id']}/revoke", headers=H,
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    assert client.get("/api/v1/certificates", headers=H).json() == []


def test_someone_elses_download_link_doesnt_work(client: TestClient) -> None:
    r = client.post("/account/certificates", data={"name": "Mine"}, headers=H)
    link = r.text.split('href="/account/certificates/download/')[1].split('"')[0]
    as_member(client)
    assert client.get(f"/account/certificates/download/{link}").status_code == 404


def test_a_banner_when_this_browsers_certificate_expires_soon(client: TestClient) -> None:
    der = browser_certificate(client)
    set_peer(client, Peer("203.0.113.9", der))
    assert "certificate expires" not in client.get("/").text
    soon = (datetime.now(UTC) + timedelta(days=9, hours=2)).isoformat()
    with client.app.state.db.connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE client_certificates SET not_after = ?", (soon,))
    assert "This browser's certificate expires in 9 days" in client.get("/").text
    presented = client.get("/api/v1/certificates/presented", headers=H).json()
    assert presented["expires_in_days"] == 9


def test_the_settings_page(client: TestClient) -> None:
    page = client.get("/account/remote-access")
    assert page.status_code == 200 and "Who checks certificates" in page.text
    r = client.post("/account/remote-access", data={"action": "choose", "mode": "proxy"},
                    headers=H, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    page = client.get("/account/remote-access").text
    assert "Download ca.pem" in page and "POST /api/v1/devices/pair" in page
    r = client.post("/account/remote-access", data={"action": "on"}, headers=H)
    assert "confirm" in r.text  # check_forwarded is off (unchecked), so it needs confirming
    client.post("/account/remote-access", data={"action": "choose", "mode": "cloudflare"},
                headers=H)  # fmt: skip
    assert "cf.tls_client_auth.cert_verified" in client.get("/account/remote-access").text


# ---- the TLS layer -----------------------------------------------------------------------


def write_client_pem(path: Path, issued) -> Path:
    key, cert, _ = pkcs12.load_key_and_certificates(issued.p12, issued.password.encode())
    path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    )  # fmt: skip
    return path


def handshake(context: ssl.SSLContext, server_cert: Path, client_pem: Path | None) -> bool:
    """Whether a client (with or without a certificate) gets a byte through."""
    client = client_context(server_cert)
    if client_pem is not None:
        client.load_cert_chain(client_pem)
    a, b = socket.socketpair()
    ok: dict[str, bool] = {}

    def serve() -> None:
        try:
            with context.wrap_socket(a, server_side=True) as s:
                ok["got"] = s.recv(1) == b"x"
        except (ssl.SSLError, OSError):
            ok["got"] = False

    t = threading.Thread(target=serve)
    t.start()
    try:
        with client.wrap_socket(b, server_hostname=HOST) as c:
            c.sendall(b"x")
    except (ssl.SSLError, OSError):
        pass
    t.join(5)
    return ok.get("got", False)


def test_the_listening_contexts_follow_the_setting(db, certs, tmp_path: Path) -> None:
    cert, key = make_pair(tmp_path / "live", serial=1)
    ra = remote(db, certs)
    auth_files = tls.ClientAuth(ca=certs.ca_cert_path, crl=certs.crl_path,
                                verify_mode=ra.verify_mode, refresh=certs.crl_pem)  # fmt: skip
    reloader = tls.CertificateReloader(cert, key, interval=0, client_auth=auth_files)
    assert reloader.context.verify_mode == ssl.CERT_NONE  # as before this feature
    assert handshake(reloader.context, cert, None)

    mine = certs.issue("alice", "browser", "Laptop")
    lost = certs.issue("alice", "browser", "Lost phone")
    good, bad = (
        write_client_pem(tmp_path / "good.pem", mine),
        write_client_pem(tmp_path / "bad.pem", lost),
    )
    ra.choose("alice", "server")
    reloader.check(force=True)
    assert reloader.context.verify_mode == ssl.CERT_OPTIONAL
    assert handshake(reloader.context, cert, None) and handshake(reloader.context, cert, good)

    ra.turn_on("alice", certs.by_fingerprint(mine.certificate["sha256"]), confirmed=False)
    certs.revoke("alice", lost.certificate["id"])
    reloader.check(force=True)
    assert reloader.context.verify_mode == ssl.CERT_REQUIRED
    assert handshake(reloader.context, cert, good)
    assert not handshake(reloader.context, cert, None)
    assert not handshake(reloader.context, cert, bad)  # revoked: in crl.pem
    stranger = ClientCertificates(db, tmp_path / "elsewhere").issue("alice", "browser", "x")
    assert not handshake(reloader.context, cert, write_client_pem(tmp_path / "s.pem", stranger))
    # The front door never asks.
    assert handshake(reloader.front_door_context, cert, None)
    assert handshake(reloader.front_door_context, cert, bad)


# ---- end to end, with uvicorn ------------------------------------------------------------


def https_status(port: int, path: str, server_cert: Path, client_pem: Path | None) -> int | None:
    """The HTTP status of a GET, or None when the TLS layer refuses the connection."""
    context = client_context(server_cert)
    if client_pem is not None:
        context.load_cert_chain(client_pem)
    try:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
            context.wrap_socket(raw, server_hostname=HOST) as conn,
        ):
            conn.sendall(
                f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode()
            )
            data = b""
            while chunk := conn.recv(4096):
                data += chunk
    except (ssl.SSLError, OSError):
        return None
    if not data:
        return None
    return int(data.split(b" ", 2)[1])


def test_both_ports_with_uvicorn(tmp_path: Path, fake_runner, monkeypatch) -> None:
    cert, key = make_pair(tmp_path / "certs", serial=3)
    data = tmp_path / "data"
    db = Database(data / "dreadovania.db")
    db.migrate()
    make_user(db, "alice")
    with db.connect() as conn:
        conn.execute("UPDATE users SET username = 'samus' WHERE id = 'alice'")
    certs = ClientCertificates(db, data)
    mine = certs.issue("alice", "browser", "Laptop")
    ra = remote(db, certs)
    ra.choose("alice", "server")
    ra.turn_on("alice", certs.by_fingerprint(mine.certificate["sha256"]), confirmed=False)
    good = write_client_pem(tmp_path / "good.pem", mine)

    port, door = free_port(), free_port()
    settings = Settings(data_dir=data, host="127.0.0.1", port=port, front_door_port=door,
                        tls_cert_file=cert, tls_key_file=key)  # fmt: skip
    monkeypatch.setattr(entry, "create_app", lambda s: create_app(s, runner=fake_runner))
    main, front = entry.uvicorn_configs(settings)
    assert front is not None
    server, front_door = uvicorn.Server(main), entry._FrontDoorServer(front)

    async def both() -> None:
        await asyncio.gather(server.serve(), front_door.serve())

    thread = threading.Thread(target=asyncio.run, args=(both(),), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while https_status(door, "/healthz", cert, None) != 200 and time.monotonic() < deadline:
            time.sleep(0.1)
        # The main port: TLS refuses a client without a certificate.
        assert https_status(port, "/healthz", cert, None) is None
        # With one, the app sees it: sign-in is what's missing, not the certificate.
        assert https_status(port, "/api/v1/seeds", cert, good) == 401
        # The front door: no certificate needed, only for its paths.
        assert https_status(door, "/healthz", cert, None) == 200
        assert https_status(door, "/api/v1/info", cert, None) == 404
        assert https_status(door, "/api/v1/seeds", cert, None) == 404
        # Revoking reaches an open server within the reload interval.
        certs.revoke("alice", mine.certificate["id"])
        time.sleep(tls.CHECK_INTERVAL + 0.5)
        https_status(port, "/healthz", cert, good)  # this hello reloads the files
        assert https_status(port, "/api/v1/seeds", cert, good) is None
    finally:
        server.should_exit = front_door.should_exit = True
        thread.join(timeout=10)


def test_the_banner_gets_a_replacement_and_using_it_revokes_the_old_one(client) -> None:
    der = browser_certificate(client, "Phone")
    set_peer(client, Peer("203.0.113.9", der))
    now = datetime.now(UTC)
    aged = ((now - timedelta(days=362)).isoformat(), (now + timedelta(days=3)).isoformat())
    with client.app.state.db.connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE client_certificates SET not_before = ?, not_after = ?", aged)
    assert "Get its replacement" in client.get("/").text
    r = client.post("/account/certificates/replace", headers=H)
    assert "It replaces “Phone”" in r.text
    link = r.text.split('href="/account/certificates/download/')[1].split('"')[0]
    password = r.text.split('aria-label="Certificate password">')[1].split("<")[0]
    p12 = client.get(f"/account/certificates/download/{link}").content
    _key, new, _ = pkcs12.load_key_and_certificates(p12, password.encode())
    names = [c["name"] for c in client.get("/api/v1/certificates", headers=H).json()]
    assert names == ["Phone", "Phone"]  # the old one still works meanwhile
    set_peer(client, Peer("203.0.113.9", new.public_bytes(serialization.Encoding.DER)))
    assert "Get its replacement" not in client.get("/").text
    listed = client.get("/api/v1/certificates", headers=H).json()
    assert (
        len(listed) == 1
        and listed[0]["sha256"]
        == hashlib.sha256(new.public_bytes(serialization.Encoding.DER)).hexdigest()
    )


def test_certificate_lifetimes_are_configurable(tmp_path: Path, fake_runner) -> None:
    settings = Settings(data_dir=tmp_path / "data", device_certificate_days=30,
                        browser_certificate_days=90)  # fmt: skip
    app = create_app(settings, runner=fake_runner)
    with TestClient(app, base_url=ORIGIN):
        make_user(app.state.db, "alice")
        certs = app.state.client_certs
        browser = certs.issue("alice", "browser", "x").certificate
        device = certs.issue("alice", "device", "y", device_id="d").certificate

    def days(c):
        return (
            datetime.fromisoformat(c["not_after"]) - datetime.fromisoformat(c["not_before"])
        ).days

    assert (days(browser), days(device)) == (90, 30)
