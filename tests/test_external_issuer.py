# SPDX-License-Identifier: GPL-3.0-or-later
"""Certificates from the owner's own CA (ADR 0023, the External issuer): only the
fingerprint is kept, whether the certificate or a .p12 is uploaded."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_user
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from test_api import H
from test_devices import as_device, pair, pairing_code
from test_remote_access import Peer, peer_client, set_peer

from dreadovania.client_certs import CertificateError, ClientCertificates
from dreadovania.db import Database


class OwnCA:
    """Stands in for the owner's step-ca or similar."""

    def __init__(self, name: str = "Owner's CA") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(UTC)
        self.cert = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self.key, hashes.SHA256())
        )  # fmt: skip

    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def client(self, *, days: int = 90, usage=ExtendedKeyUsageOID.CLIENT_AUTH, ca: bool = False):
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone")]))
            .issuer_name(self.cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )  # fmt: skip
        if usage is not None:
            builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        return key, builder.sign(self.key, hashes.SHA256())


def p12_of(key, cert, password: bytes = b"hunter22") -> bytes:
    return pkcs12.serialize_key_and_certificates(
        b"phone", key, cert, None, serialization.BestAvailableEncryption(password)
    )


@pytest.fixture
def certs(db: Database, tmp_path: Path) -> ClientCertificates:
    make_user(db, "alice")
    return ClientCertificates(db, tmp_path / "data")


def test_a_certificate_alone_is_recorded_by_fingerprint(certs) -> None:
    _key, cert = OwnCA().client()
    recorded = certs.record_external("alice", cert.public_bytes(serialization.Encoding.PEM),
                                     None, "  My   phone ")  # fmt: skip
    assert recorded["issuer"] == "external" and recorded["kind"] == "browser"
    assert recorded["name"] == "My phone"
    assert recorded["sha256"] == cert.fingerprint(hashes.SHA256()).hex()
    assert certs.is_valid(recorded["sha256"])
    der = cert.public_bytes(serialization.Encoding.DER)  # DER works too
    with pytest.raises(CertificateError, match="already recorded"):
        certs.record_external("alice", der, None, "again")


def test_a_p12_gives_its_certificate_and_nothing_else_is_kept(certs, db: Database) -> None:
    key, cert = OwnCA().client()
    data = p12_of(key, cert)
    with pytest.raises(CertificateError, match="password is wrong"):
        certs.record_external("alice", data, "wrong", "Phone")
    recorded = certs.record_external("alice", data, "hunter22", "Phone")
    assert recorded["sha256"] == cert.fingerprint(hashes.SHA256()).hex()
    with db.connect() as conn:
        row = " ".join(str(v) for v in dict(conn.execute(
            "SELECT * FROM client_certificates").fetchone()).values())  # fmt: skip
    assert "hunter22" not in row and "PRIVATE" not in row
    assert not list(certs.dir.glob("*")) if certs.dir.exists() else True  # no file written


def test_unsuitable_certificates_are_refused(certs) -> None:
    ca = OwnCA()
    for cert, reason in [
        (ca.client(days=-1)[1], "expired"),
        (ca.client(usage=ExtendedKeyUsageOID.SERVER_AUTH)[1], "client authentication"),
        (ca.client(ca=True)[1], "CA certificate"),
    ]:
        with pytest.raises(CertificateError, match=reason):
            certs.record_external("alice", cert.public_bytes(serialization.Encoding.PEM), None, "x")
    with pytest.raises(CertificateError, match="isn't a certificate"):
        certs.record_external("alice", b"not a certificate", None, "x")
    no_usage = ca.client(usage=None)[1]  # no EKU at all: any use, so accepted
    certs.record_external("alice", no_usage.public_bytes(serialization.Encoding.PEM), None, "x")


def test_with_the_owners_ca_set_only_its_certificates_are_accepted(certs) -> None:
    mine, other = OwnCA(), OwnCA("Someone else")
    with pytest.raises(CertificateError, match="isn't a CA"):
        certs.set_external_ca(mine.client()[1].public_bytes(serialization.Encoding.PEM))
    certs.set_external_ca(mine.pem())
    assert certs.external_ca().subject == mine.cert.subject
    pem = other.client()[1].public_bytes(serialization.Encoding.PEM)
    with pytest.raises(CertificateError, match="wasn't issued by your CA"):
        certs.record_external("alice", pem, None, "x")
    certs.record_external("alice", mine.client()[1].public_bytes(serialization.Encoding.PEM),
                          None, "ok")  # fmt: skip
    certs.set_external_ca(None)
    assert certs.external_ca() is None
    certs.record_external("alice", pem, None, "any CA again")


# ---- through the app -----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    yield from peer_client(tmp_path, fake_runner)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_uploading_through_the_api_and_the_proxy_check(client: TestClient) -> None:
    ca = OwnCA()
    key, cert = ca.client()
    r = client.post("/api/v1/certificates/external", headers=H,
                    json={"certificate": b64(p12_of(key, cert)), "password": "hunter22",
                          "name": "Phone"})  # fmt: skip
    assert r.status_code == 201, r.text
    assert "p12" not in r.json() and "password" not in r.json()
    sha256 = r.json()["sha256"]
    # Behind a proxy that checks against the owner's CA and passes the fingerprint on:
    proxy = Peer("172.18.0.3", None)
    remote = client.app.state.remote_access  # type: ignore[attr-defined]
    remote.trusted_proxies = ["172.18.0.0/16"]
    set_peer(client, proxy)
    header = {"x-ssl-client-sha256": sha256} | H
    on = {"enabled": True, "mode": "proxy", "check_forwarded": True}
    assert client.put("/api/v1/remote-access", headers=header, json=on).status_code == 200
    assert client.get("/api/v1/seeds", headers=header).status_code == 200
    cert_id = r.json()["id"]
    assert client.delete(f"/api/v1/certificates/{cert_id}", headers=header).status_code == 204
    assert client.get("/api/v1/seeds", headers=header).status_code == 403  # revoked here


def test_a_switch_with_an_external_certificate_isnt_renewed_by_the_server(client) -> None:
    paired = pair(client, pairing_code(client))
    with client.app.state.db.connect() as conn:  # type: ignore[attr-defined]
        conn.execute("DELETE FROM client_certificates")  # only the owner's own, below
    _key, cert = OwnCA().client()
    r = client.post("/api/v1/certificates/external", headers=H, json={
        "certificate": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "device_id": paired["device_id"], "name": "Switch"})  # fmt: skip
    assert r.status_code == 201 and r.json()["kind"] == "device", r.text
    client.cookies.clear()
    token = as_device(paired["token"])
    me = client.get("/api/v1/devices/me", headers=token).json()["certificate"]
    assert me["issuer"] == "external" and me["renew_after"] == me["not_after"]
    assert client.post("/api/v1/devices/me/certificate", headers=token).status_code == 409


def test_only_your_own_switches_and_only_the_admin_sets_the_ca(client) -> None:
    _key, cert = OwnCA().client()
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    r = client.post("/api/v1/certificates/external", headers=H,
                    json={"certificate": pem, "device_id": "not-mine"})  # fmt: skip
    assert r.status_code == 404
    r = client.put("/api/v1/remote-access/external-ca", headers=H,
                   json={"certificate": OwnCA().pem().decode()})  # fmt: skip
    assert r.status_code == 200 and r.json()["subject"].startswith("CN=")
    from test_accounts import as_member

    as_member(client)
    assert client.get("/api/v1/remote-access/external-ca", headers=H).status_code == 403
    assert client.delete("/api/v1/remote-access/external-ca", headers=H).status_code == 403


def test_the_pages(client: TestClient) -> None:
    assert "Add a certificate from your own CA" in client.get("/account").text
    client.post("/account/remote-access", data={"action": "choose", "mode": "proxy"}, headers=H)
    page = client.get("/account/remote-access").text
    assert "Your own CA (optional)" in page
    form = {"action": "external_ca", "ca": OwnCA("Home CA").pem().decode()}
    r = client.post("/account/remote-access", headers=H, follow_redirects=False, data=form)
    assert r.status_code == 303
    assert "CN=Home CA" in client.get("/account/remote-access").text
