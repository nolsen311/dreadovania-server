# SPDX-License-Identifier: GPL-3.0-or-later
"""Client certificates from the built-in CA (ADR 0023): issue, revoke, renew, CRL."""

import base64
import socket
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_user
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from test_accounts import as_member
from test_api import H, _client, use_session
from test_devices import as_device, pair, pairing_code

from dreadovania import client_certs
from dreadovania.client_certs import CertificateError, ClientCertificates
from dreadovania.db import Database


@pytest.fixture
def certs(db: Database, tmp_path: Path) -> ClientCertificates:
    make_user(db, "alice")
    make_user(db, "bob", role="member")
    return ClientCertificates(db, tmp_path / "data")


def load(issued: client_certs.Issued):
    key, cert, extra = pkcs12.load_key_and_certificates(issued.p12, issued.password.encode())
    return key, cert, extra


# ---- issuing -------------------------------------------------------------------------------------


def test_a_browser_certificate_is_a_client_certificate_from_the_ca(certs) -> None:
    issued = certs.issue("alice", "browser", "  My   phone ")
    key, cert, extra = load(issued)
    ca = x509.load_pem_x509_certificate(certs.ca_pem())
    assert isinstance(key, rsa.RSAPrivateKey) and key.key_size == 2048
    cert.verify_directly_issued_by(ca)
    assert extra == [ca]  # the chain goes with it
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert timedelta(days=365) <= lifetime < timedelta(days=366)
    assert issued.certificate["name"] == "My phone"
    assert issued.certificate["sha256"] == cert.fingerprint(hashes.SHA256()).hex()
    assert certs.is_valid(issued.certificate["sha256"])


def test_a_switch_certificate_lasts_47_days_and_needs_its_device(certs) -> None:
    _, cert, _ = load(certs.issue("alice", "device", "Switch", device_id="dev-1"))
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert timedelta(days=47) <= lifetime < timedelta(days=48)
    with pytest.raises(CertificateError):
        certs.issue("alice", "device", "Switch")


def test_only_the_certificate_is_kept(certs, db: Database) -> None:
    issued = certs.issue("alice", "browser", "Laptop")
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM client_certificates").fetchone())
    stored = " ".join(str(v) for v in row.values())
    assert issued.password not in stored
    assert "PRIVATE" not in stored and set(row) >= {"serial", "sha256"}
    assert (certs.dir / "ca.key").stat().st_mode & 0o077 == 0  # the CA key is private
    assert not list(certs.dir.glob("*.p12"))


def test_the_ca_is_made_once(certs, db: Database, tmp_path: Path) -> None:
    first = certs.ca_pem()
    assert ClientCertificates(db, tmp_path / "data").ca_pem() == first


# ---- revoking ------------------------------------------------------------------------------------


def crl_serials(certs: ClientCertificates) -> set[int]:
    crl = x509.load_pem_x509_crl(certs.crl_pem())
    ca = x509.load_pem_x509_certificate(certs.ca_pem())
    assert crl.is_signature_valid(ca.public_key())
    return {r.serial_number for r in crl}


def test_revoking_lists_it_and_only_its_owner_can(certs) -> None:
    issued = certs.issue("alice", "browser", "Laptop")
    _, cert, _ = load(issued)
    assert cert.serial_number not in crl_serials(certs)
    assert not certs.revoke("bob", issued.certificate["id"])  # someone else's
    assert certs.revoke("alice", issued.certificate["id"])
    assert not certs.revoke("alice", issued.certificate["id"])  # once
    assert cert.serial_number in crl_serials(certs)
    assert not certs.is_valid(issued.certificate["sha256"])
    assert certs.list("alice") == []
    assert len(certs.list("alice", include_revoked=True)) == 1


def test_removing_a_device_or_an_account_revokes_its_certificates(certs, db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO devices (id, owner_id, name, token_hash, created_at)"
            " VALUES ('dev-1', 'alice', 'Switch', 'x', '2026-01-01')"
        )
    switch = load(certs.issue("alice", "device", "Switch", device_id="dev-1"))[1]
    browser = load(certs.issue("bob", "browser", "Phone"))[1]
    certs.crl_pem()  # published before the removals
    with db.connect() as conn:
        conn.execute("DELETE FROM devices WHERE id = 'dev-1'")
        conn.execute("DELETE FROM users WHERE id = 'bob'")
    assert {switch.serial_number, browser.serial_number} <= crl_serials(certs)


def test_expired_certificates_drop_off_the_list(certs, db: Database) -> None:
    issued = certs.issue("alice", "browser", "Old")
    serial = load(issued)[1].serial_number
    certs.revoke("alice", issued.certificate["id"])
    assert serial in crl_serials(certs)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE client_certificates SET not_after = ?", (past,))
    certs.publish_crl()
    assert serial not in crl_serials(certs)  # expired anyway: no longer needs listing


# ---- renewing ------------------------------------------------------------------------------------


def test_renewal_keeps_the_old_one_until_the_new_one_is_used(certs) -> None:
    old = certs.issue("alice", "device", "Switch", device_id="dev-1")
    new = certs.renew("alice", old.certificate["id"])
    assert new.certificate["kind"] == "device" and new.certificate["device_id"] == "dev-1"
    assert certs.is_valid(old.certificate["sha256"]) and certs.is_valid(new.certificate["sha256"])
    assert certs.confirm_renewal("bob", new.certificate["id"]) == 0  # not bob's
    assert certs.confirm_renewal("alice", new.certificate["id"]) == 1
    assert not certs.is_valid(old.certificate["sha256"])
    assert certs.is_valid(new.certificate["sha256"])
    with pytest.raises(CertificateError):
        certs.renew("alice", old.certificate["id"])  # revoked now


def test_renewal_is_due_with_a_third_left(certs) -> None:
    cert = certs.issue("alice", "device", "Switch", device_id="dev-1").certificate
    assert not certs.due_for_renewal(cert)
    start = datetime.now(UTC) - timedelta(days=32)
    aged = cert | {"not_before": start.isoformat(),
                   "not_after": (start + timedelta(days=47)).isoformat()}  # fmt: skip
    assert certs.due_for_renewal(aged)


# ---- a real TLS handshake, as the server's own check or HAProxy does it ------------------


def _server_context(tmp_path: Path, certs: ClientCertificates) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    (tmp_path / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "server.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))  # fmt: skip
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=certs.ca_pem().decode())
    certs.crl_pem()  # published to crl.pem, which is what a server or proxy loads
    ctx.load_verify_locations(cafile=str(certs.crl_path))
    ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    return ctx


def _handshake(server_ctx: ssl.SSLContext, issued, tmp_path: Path) -> bool:
    key, cert, _ = load(issued)
    (tmp_path / "client.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE  # this test is about the client's certificate
    client_ctx.load_cert_chain(tmp_path / "client.pem")
    a, b = socket.socketpair()
    result: dict[str, bool] = {}

    def serve() -> None:
        try:
            with server_ctx.wrap_socket(a, server_side=True) as s:
                s.recv(1)
                result["ok"] = True
        except (ssl.SSLError, OSError):
            result["ok"] = False

    t = threading.Thread(target=serve)
    t.start()
    try:
        with client_ctx.wrap_socket(b) as c:
            c.sendall(b"x")
    except (ssl.SSLError, OSError):
        pass
    t.join(5)
    return result.get("ok", False)


def test_openssl_accepts_a_live_certificate_and_refuses_a_revoked_one(certs, tmp_path) -> None:
    good = certs.issue("alice", "browser", "Laptop")
    revoked = certs.issue("alice", "browser", "Lost phone")
    certs.revoke("alice", revoked.certificate["id"])
    ctx = _server_context(tmp_path, certs)
    assert _handshake(ctx, good, tmp_path)
    assert not _handshake(ctx, revoked, tmp_path)


# ---- the API ---------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    yield from _client(tmp_path, fake_runner)


def test_a_browser_certificate_from_the_api(client: TestClient) -> None:
    r = client.post("/api/v1/certificates", json={"name": "My phone"}, headers=H)
    assert r.status_code == 201, r.text
    body = r.json()
    p12 = base64.b64decode(body["p12"])
    _key, cert, _ = pkcs12.load_key_and_certificates(p12, body["password"].encode())
    assert body["certificate"]["sha256"] == cert.fingerprint(hashes.SHA256()).hex()
    listed = client.get("/api/v1/certificates", headers=H).json()
    assert [c["id"] for c in listed] == [body["certificate"]["id"]]
    assert "p12" not in listed[0] and "password" not in listed[0]  # shown once only
    cert_id = body["certificate"]["id"]
    assert client.delete(f"/api/v1/certificates/{cert_id}", headers=H).status_code == 204
    assert client.get("/api/v1/certificates", headers=H).json() == []
    assert client.delete(f"/api/v1/certificates/{cert_id}", headers=H).status_code == 404


def test_certificates_are_per_account_and_not_for_devices(client: TestClient) -> None:
    mine = client.post("/api/v1/certificates", json={}, headers=H).json()["certificate"]["id"]
    device = pair(client, pairing_code(client))
    admin_cookie = as_member(client)
    assert client.get("/api/v1/certificates", headers=H).json() == []
    assert client.delete(f"/api/v1/certificates/{mine}", headers=H).status_code == 404
    use_session(client, admin_cookie)
    client.cookies.clear()
    r = client.get("/api/v1/certificates", headers=as_device(device["token"]))
    assert r.status_code == 403
