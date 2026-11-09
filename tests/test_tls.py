# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTPS from mounted certificate files, hot reload, proxies and hostnames (ADR 0005).

Certificates are generated per test with ``cryptography`` (already installed as a
dependency of the server's own dependencies) for the made-up name dread.test.
"""

import datetime
import os
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api import ORIGIN, H

from dreadovania import auth, healthcheck, tls
from dreadovania.__main__ import uvicorn_config
from dreadovania.app import create_app
from dreadovania.config import Settings

HOST = "dread.test"


def make_pair(folder: Path, serial: int) -> tuple[Path, Path]:
    """A self-signed certificate for dread.test, written like certbot's files."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = folder / "fullchain.pem", folder / "privkey.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def renew(cert: Path, key: Path, serial: int, tmp: Path) -> None:
    """Replace the files the way renewal tools do: write elsewhere, then rename."""
    new_cert, new_key = make_pair(tmp / f"renewed-{serial}", serial)
    os.replace(new_key, key)
    os.replace(new_cert, cert)


def client_context(*certs: Path) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    for cert in certs:
        context.load_verify_locations(cadata=cert.read_text())
    return context


def serial_seen(port: int, context: ssl.SSLContext) -> int:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=HOST) as conn,
    ):
        der = conn.getpeercert(binary_form=True)
    return x509.load_der_x509_certificate(der).serial_number


@pytest.fixture
def tls_listener(tmp_path: Path) -> Iterator[dict]:
    """A TLS socket served with a CertificateReloader's context."""
    cert, key = make_pair(tmp_path / "live", serial=1)
    reloader = tls.CertificateReloader(cert, key, interval=0)
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            try:
                with reloader.context.wrap_socket(conn, server_side=True) as tls_conn:
                    tls_conn.recv(1)  # until the client closes
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"port": server.getsockname()[1], "cert": cert, "key": key, "reloader": reloader}
    stop.set()
    thread.join(timeout=5)
    server.close()


# ---- loading and reloading -------------------------------------------------------------------


def test_a_renewed_certificate_is_used_without_a_restart(
    tls_listener: dict, tmp_path: Path
) -> None:
    port, cert, key = tls_listener["port"], tls_listener["cert"], tls_listener["key"]
    assert serial_seen(port, client_context(cert)) == 1
    renew(cert, key, serial=2, tmp=tmp_path)
    assert serial_seen(port, client_context(cert)) == 2  # trusting only the new one


def test_a_broken_renewal_keeps_the_working_certificate(
    tls_listener: dict, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    port, cert, key = tls_listener["port"], tls_listener["cert"], tls_listener["key"]
    trusted = client_context(cert)
    # Only the certificate has been replaced so far: it doesn't match the key.
    other_cert, _ = make_pair(tmp_path / "other", serial=3)
    os.replace(other_cert, cert)
    assert serial_seen(port, trusted) == 1
    assert "Keeping the current certificate" in caplog.text
    # Then the key arrives, and the pair works.
    renew(cert, key, serial=4, tmp=tmp_path)
    assert serial_seen(port, client_context(cert)) == 4


def test_changes_are_checked_at_most_once_per_interval(tmp_path: Path) -> None:
    cert, key = make_pair(tmp_path / "live", serial=1)
    reloader = tls.CertificateReloader(cert, key, interval=3600)
    renew(cert, key, serial=2, tmp=tmp_path)
    assert reloader.check() is False  # too soon
    assert reloader.check(force=True) is True
    assert reloader.check(force=True) is False  # nothing new


def test_unusable_files_stop_the_server_with_a_reason(tmp_path: Path) -> None:
    cert, key = make_pair(tmp_path / "a", serial=1)
    _, other_key = make_pair(tmp_path / "b", serial=2)
    with pytest.raises(tls.TlsError, match="doesn't exist"):
        tls.build_context(tmp_path / "missing.pem", key)
    with pytest.raises(tls.TlsError, match="can't be used together"):
        tls.build_context(cert, other_key)
    with pytest.raises(tls.TlsError, match="both"):
        tls.check_pair(cert, None)
    assert tls.check_pair(None, None) is None


# ---- the server -------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_server_speaks_https_and_the_health_check_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert, key = make_pair(tmp_path / "certs", serial=7)
    port = free_port()
    for name, value in {
        "DREADOVANIA_DATA_DIR": str(tmp_path / "data"),
        "DREADOVANIA_HOST": "127.0.0.1",
        "DREADOVANIA_PORT": str(port),
        "DREADOVANIA_TLS_CERT_FILE": str(cert),
        "DREADOVANIA_TLS_KEY_FILE": str(key),
    }.items():
        monkeypatch.setenv(name, value)
    server = uvicorn.Server(uvicorn_config(Settings()))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        assert serial_seen(port, client_context(cert)) == 7
        assert healthcheck.main() == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert healthcheck.main() == 1  # stopped


def test_plain_http_without_certificates(tmp_path: Path) -> None:
    config = uvicorn_config(Settings(data_dir=tmp_path))
    assert not config.is_ssl
    assert config.proxy_headers is False


def test_forwarded_headers_only_from_listed_proxies(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, trusted_proxies=" 172.18.0.5/16, 10.0.0.2 ,fd00::/8")
    assert settings.trusted_proxy_list == ["172.18.0.0/16", "10.0.0.2", "fd00::/8"]
    config = uvicorn_config(settings)
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == ["172.18.0.0/16", "10.0.0.2", "fd00::/8"]
    with pytest.raises(ValidationError, match="isn't an IP address or network"):
        Settings(data_dir=tmp_path, trusted_proxies="*")


# ---- hostname, not address --------------------------------------------------------------------


def set_up(app, public_url: str) -> None:
    with TestClient(app, base_url=ORIGIN) as c:
        token = auth.create_setup_token(app.state.db)
        r = c.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": "correct horse battery",
                  "public_url": public_url},
            headers={"Origin": auth.origin_of(public_url)},
        )  # fmt: skip
        assert r.status_code == 201, r.text


def test_an_ip_address_redirects_to_the_hostname(tmp_path: Path, fake_runner) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"), runner=fake_runner)
    set_up(app, ORIGIN)
    with TestClient(app, base_url="http://10.3.13.2:58080", follow_redirects=False) as c:
        r = c.get("/seeds/abc?x=1")
        assert (r.status_code, r.headers["location"]) == (308, f"{ORIGIN}/seeds/abc?x=1")
        assert r.headers["x-frame-options"] == "DENY"  # security headers still apply
        assert c.get("/healthz").status_code == 200  # Docker's check, by address
        assert c.post("/api/v1/login", json={}, headers=H).status_code != 308
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as c:
        assert c.get("/login").status_code == 200


def test_no_redirect_when_the_server_is_used_by_address(tmp_path: Path, fake_runner) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"), runner=fake_runner)
    set_up(app, "http://10.3.13.2:58080")
    with TestClient(app, base_url="http://10.3.13.2:58080", follow_redirects=False) as c:
        assert c.get("/login").status_code == 200


def test_the_public_url_setting_moves_an_existing_server_to_its_hostname(
    tmp_path: Path, fake_runner
) -> None:
    # Set up by address; later given a hostname with DREADOVANIA_PUBLIC_URL.
    data = tmp_path / "data"
    set_up(create_app(Settings(data_dir=data), runner=fake_runner), "http://10.3.13.2:58080")
    app = create_app(Settings(data_dir=data, public_url=ORIGIN), runner=fake_runner)
    with TestClient(app, base_url="http://10.3.13.2:58080", follow_redirects=False) as c:
        assert c.get("/").headers["location"] == f"{ORIGIN}/"
