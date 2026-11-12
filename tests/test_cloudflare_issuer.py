# SPDX-License-Identifier: GPL-3.0-or-later
"""Client certificates from Cloudflare's CA (ADR 0023), against a fake Cloudflare API."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import make_user
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from test_api import ORIGIN, H
from test_devices import as_device, pair, pairing_code
from test_remote_access import PeerApp

from dreadovania import auth, cloudflare
from dreadovania.app import create_app
from dreadovania.client_certs import CertificateError, ClientCertificates
from dreadovania.cloudflare import CloudflareError, CloudflareIssuer
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.remote_access import RemoteAccess


class FakeCloudflare:
    """Signs CSRs with its own CA, as Cloudflare's managed CA would, and records calls."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake Cloudflare CA")])
        self.certs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.extra_active = 0
        self.sign_other_key = False

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append((method, path))
        if self.down:
            raise CloudflareError("couldn't reach Cloudflare's API (test)")
        assert path.startswith("/zones/zone-1/client_certificates")
        rest = path.removeprefix("/zones/zone-1/client_certificates")
        if method == "GET":
            query = dict(p.split("=") for p in rest.lstrip("?").split("&") if p)
            per_page, page = int(query.get("per_page", 20)), int(query.get("page", 1))
            if not 5 <= per_page <= 50:  # as Cloudflare's API description says
                return {"success": False, "errors": [{"message": "per_page out of range"}]}
            active = [{"id": i, "fingerprint_sha256": c["sha256"], "status": c["status"]}
                      for i, c in self.certs.items() if c["status"] == "active"]  # fmt: skip
            pages = max(1, -(-len(active) // per_page))
            return {"success": True, "errors": [], "messages": [],
                    "result": active[(page - 1) * per_page : page * per_page],
                    "result_info": {"total_count": len(active) + self.extra_active,
                                    "total_pages": pages}}  # fmt: skip
        if method == "DELETE":
            cert_id = rest.lstrip("/")
            self.certs[cert_id]["status"] = "pending_revocation"
            return {"success": True, "errors": [], "messages": [], "result": {"id": cert_id}}
        assert method == "POST" and body is not None
        csr = x509.load_pem_x509_csr(body["csr"].encode())
        public = ec.generate_private_key(ec.SECP256R1()).public_key() if self.sign_other_key \
            else csr.public_key()  # fmt: skip
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.name)
            .public_key(public).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=body["validity_days"]))
            .sign(self.key, hashes.SHA256())
        )  # fmt: skip
        cert_id = f"cf-{len(self.certs) + 1}"
        self.certs[cert_id] = {
            "status": "active",
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
        }
        return {"success": True, "errors": [], "messages": [], "result": {
            "id": cert_id, "status": "active",
            "certificate": cert.public_bytes(serialization.Encoding.PEM).decode(),
            "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "expires_on": cert.not_valid_after_utc.isoformat(),
        }}  # fmt: skip

    def made_in_dashboard(self):
        """A certificate created in Cloudflare's dashboard, not through the server."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dash")]))
            .issuer_name(self.name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=365))
            .sign(self.key, hashes.SHA256())
        )  # fmt: skip
        cert_id = f"cf-{len(self.certs) + 1}"
        self.certs[cert_id] = {
            "status": "active",
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
        }
        return key, cert, cert_id


@pytest.fixture
def fake() -> FakeCloudflare:
    return FakeCloudflare()


@pytest.fixture
def certs(db: Database, tmp_path: Path, fake: FakeCloudflare) -> ClientCertificates:
    make_user(db, "alice")
    c = ClientCertificates(db, tmp_path / "data")
    c.cloudflare = CloudflareIssuer("zone-1", request=fake)
    c.use_cloudflare = lambda: True
    return c


def load(issued):
    return pkcs12.load_key_and_certificates(issued.p12, issued.password.encode())


# ---- the API client ------------------------------------------------------------------------


def test_cloudflare_errors_are_explained(fake: FakeCloudflare) -> None:
    answer = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    refused = CloudflareIssuer("zone-1", request=lambda m, p, b: answer)
    with pytest.raises(CloudflareError, match="Authentication error"):
        refused.active_count()
    fake.down = True
    with pytest.raises(CloudflareError, match="couldn't reach"):
        CloudflareIssuer("zone-1", request=fake).sign("csr", 47)


def test_settings_need_both_the_zone_and_a_token_file(tmp_path: Path) -> None:
    assert cloudflare.from_settings(None, None) is None
    with pytest.raises(CloudflareError, match="both"):
        cloudflare.from_settings("zone-1", None)
    with pytest.raises(CloudflareError, match="can't be read"):
        cloudflare.from_settings("zone-1", tmp_path / "missing")
    (tmp_path / "empty").write_text("  \n")
    with pytest.raises(CloudflareError, match="empty"):
        cloudflare.from_settings("zone-1", tmp_path / "empty")
    (tmp_path / "token").write_text("secret-token\n")
    assert cloudflare.from_settings(" zone-1 ", tmp_path / "token").zone_id == "zone-1"


def test_the_token_file_may_start_with_a_byte_order_mark(tmp_path: Path, monkeypatch) -> None:
    """Seen live: Notepad saved the token with a BOM, which then broke the header."""
    seen: list[str] = []
    monkeypatch.setattr(cloudflare, "_http", lambda token: seen.append(token) or None)
    (tmp_path / "token").write_bytes(b"\xef\xbb\xbfsecret-token\r\n")
    cloudflare.from_settings("zone-1", tmp_path / "token")
    assert seen == ["secret-token"]


def test_a_token_that_cant_go_in_a_header_is_refused_without_showing_it(tmp_path: Path) -> None:
    for content, position in [("secret token", 7), ("secret\u200btoken", 7), ("tökén", 2)]:
        (tmp_path / "token").write_text(content, encoding="utf-8")
        with pytest.raises(CloudflareError, match=f"at position {position} ") as raised:
            cloudflare.from_settings("zone-1", tmp_path / "token")
        assert "secret" not in str(raised.value) and "tök" not in str(raised.value)
    (tmp_path / "token").write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(CloudflareError, match="isn't text"):
        cloudflare.from_settings("zone-1", tmp_path / "token")


# ---- issuing and revoking ------------------------------------------------------------------


def test_cloudflare_signs_while_it_is_the_chosen_check(certs, fake) -> None:
    issued = certs.issue("alice", "browser", "Phone")
    key, cert, chain = load(issued)
    assert cert.issuer == fake.name and chain == []  # Cloudflare's CA, not ours
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert issued.certificate["issuer"] == "cloudflare"
    assert certs.is_valid(cert.fingerprint(hashes.SHA256()).hex())
    with certs.db.connect() as conn:
        assert conn.execute("SELECT issuer_id FROM client_certificates").fetchone()[0] == "cf-1"
    certs.use_cloudflare = lambda: False  # back to the server's own check
    _key, own, _chain = load(certs.issue("alice", "browser", "Laptop"))
    assert own.issuer != fake.name


def test_the_zone_limit_is_kept(certs, fake) -> None:
    fake.extra_active = 100
    with pytest.raises(CertificateError, match="100 active"):
        certs.issue("alice", "browser", "Phone")
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_a_certificate_for_another_key_is_refused(certs, fake) -> None:
    fake.sign_other_key = True
    with pytest.raises(CertificateError, match="different key"):
        certs.issue("alice", "browser", "Phone")


def test_revoking_reaches_cloudflare(certs, fake) -> None:
    issued = certs.issue("alice", "browser", "Phone")
    assert certs.revoke("alice", issued.certificate["id"])
    assert fake.certs["cf-1"]["status"] == "pending_revocation"
    assert certs.sync_revocations() == 0  # nothing left to pass on


def test_removals_and_outages_are_passed_on_by_the_sweep(certs, fake, db: Database) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO devices (id, owner_id, name, token_hash, created_at)"
                     " VALUES ('dev-1', 'alice', 'Switch', 'x', '2026-01-01')")  # fmt: skip
    certs.issue("alice", "device", "Switch", device_id="dev-1")
    fake.down = True
    with db.connect() as conn:
        conn.execute("DELETE FROM devices WHERE id = 'dev-1'")  # revoked by trigger
    assert certs.sync_revocations() == 0  # Cloudflare unreachable: kept for later
    fake.down = False
    assert certs.sync_revocations() == 1
    assert fake.certs["cf-1"]["status"] == "pending_revocation"


# ---- through the app -----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner, fake: FakeCloudflare) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path / "data", trusted_proxies="172.18.0.0/16")
    app = create_app(settings, runner=fake_runner,
                     cloudflare_issuer=CloudflareIssuer("zone-1", request=fake))  # fmt: skip
    wrapped = PeerApp(app)
    with TestClient(wrapped, base_url=ORIGIN) as c:
        token = auth.create_setup_token(app.state.db)
        r = c.post("/api/v1/setup", headers=H, json={
            "token": token, "username": "samus", "password": "correct horse battery",
            "public_url": ORIGIN})  # fmt: skip
        assert r.status_code == 201
        app.state.remote_access.cache_seconds = 0
        yield c


def test_choosing_cloudflare_has_switches_renew_and_lets_it_be_turned_on(client, fake) -> None:
    paired = pair(client, pairing_code(client))  # signed by the built-in CA
    token = as_device(paired["token"])
    admin = client.cookies["dreadovania_session"]
    client.cookies.clear()
    me = client.get("/api/v1/devices/me", headers=token).json()
    assert me["certificate"]["issuer"] == "builtin"
    assert me["certificate"]["renew_after"] > datetime.now(UTC).isoformat()
    client.cookies.set("dreadovania_session", admin, domain="dread.test")
    choose = {"enabled": False, "mode": "cloudflare", "check_forwarded": False}
    r = client.put("/api/v1/remote-access", headers=H, json=choose)
    assert r.json()["issuer"] == "cloudflare" and r.json()["cloudflare_configured"]
    client.cookies.clear()
    me = client.get("/api/v1/devices/me", headers=token).json()
    assert me["certificate"]["renew_after"] <= datetime.now(UTC).isoformat()  # renew now
    renewed = client.post("/api/v1/devices/me/certificate", headers=token).json()
    _key, cert, _ = pkcs12.load_key_and_certificates(
        base64.b64decode(renewed["p12"]), renewed["password"].encode()
    )
    assert cert.issuer == fake.name
    client.cookies.set("dreadovania_session", admin, domain="dread.test")
    on = choose | {"enabled": True, "confirmed": True}
    r = client.put("/api/v1/remote-access", headers=H, json=on)
    assert r.status_code == 200 and r.json()["enabled"], r.text


def test_without_cloudflare_settings_it_still_cant_be_turned_on(db, tmp_path) -> None:
    make_user(db, "alice")
    certs = ClientCertificates(db, tmp_path / "data")
    ra = RemoteAccess(db, certs, serves_tls=False, trusted_proxies=[], cache_seconds=0)
    ra.choose("alice", "cloudflare", check_forwarded=False)
    with pytest.raises(Exception, match="DREADOVANIA_CLOUDFLARE_ZONE_ID"):
        ra.turn_on("alice", None, confirmed=True)


# ---- certificates made in Cloudflare's dashboard -------------------------------------------


def test_one_made_in_the_dashboard_is_recorded_as_cloudflares(certs, fake) -> None:
    for _ in range(55):  # more than one page of the zone's certificates
        fake.made_in_dashboard()
    key, cert, cert_id = fake.made_in_dashboard()
    p12 = pkcs12.serialize_key_and_certificates(
        b"x", key, cert, None, serialization.BestAvailableEncryption(b"pw")
    )
    recorded = certs.record_external("alice", p12, "pw", "Laptop")
    assert recorded["issuer"] == "cloudflare"
    with certs.db.connect() as conn:
        assert conn.execute("SELECT issuer_id FROM client_certificates").fetchone()[0] == cert_id
    certs.revoke("alice", recorded["id"])  # revoking here revokes it at Cloudflare too
    assert fake.certs[cert_id]["status"] == "pending_revocation"


def test_with_cloudflare_checking_only_its_own_are_accepted(certs, fake) -> None:
    from test_external_issuer import OwnCA

    pem = OwnCA().client()[1].public_bytes(serialization.Encoding.PEM)
    with pytest.raises(CertificateError, match="isn't one of this zone's"):
        certs.record_external("alice", pem, None, "From my CA")
    certs.use_cloudflare = lambda: False  # a proxy checks instead: the owner's CA is fine
    assert certs.record_external("alice", pem, None, "From my CA")["issuer"] == "external"
    fake.down = True
    with pytest.raises(CertificateError, match="couldn't check it with Cloudflare"):
        certs.record_external("alice", OwnCA().client()[1].public_bytes(
            serialization.Encoding.PEM), None, "x")  # fmt: skip
