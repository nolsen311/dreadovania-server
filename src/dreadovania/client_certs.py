# SPDX-License-Identifier: GPL-3.0-or-later
"""Client certificates for secure remote access, from the built-in CA (ADR 0023).

The server keeps a small certificate authority in ``data/remote-access/``:
``ca.pem`` and its key, and ``crl.pem``, the revocation list. It issues one
certificate per Switch or browser, hands it out once as a ``.p12`` protected by a
one-time password, and keeps only the certificate's serial and fingerprint.
``ca.pem`` and ``crl.pem`` are what the server's own TLS check, or a proxy that
ends TLS (HAProxy's ``ca-file`` and ``crl-file``), verify clients against.

Nothing here enforces anything: that is the TLS layer's job (ADR 0023 PR 2).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from dreadovania.db import Database, utc_now
from dreadovania.jobs import new_id

if TYPE_CHECKING:
    from dreadovania.cloudflare import CloudflareIssuer

log = logging.getLogger(__name__)

Kind = Literal["device", "browser"]

CA_LIFETIME = timedelta(days=3650)
CRL_LIFETIME = timedelta(days=7)  # republished well before this, on every change
DEVICE_DAYS = 47  # renewed automatically by the app (ADR 0023 amendment)
BROWSER_DAYS = 365  # a browser can't renew by itself
MAX_NAME = 60
# Keys the Switch's TLS service is known to accept as a client identity.
KEY_BITS = 2048


class CertificateError(Exception):
    """The message is safe to show."""


@dataclass(frozen=True)
class Issued:
    """A new certificate, as handed out once."""

    certificate: dict[str, Any]
    p12: bytes
    password: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _write_private(path: Path, data: bytes) -> None:
    """Write a file only the server can read, atomically."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(tmp, path)


def _write_public(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


MAX_UPLOAD = 64 * 1024


def _load_certificate(data: bytes, password: str | None = None) -> x509.Certificate:
    """A certificate from PEM, DER or a .p12. A .p12's key is dropped at once."""
    if not data or len(data) > MAX_UPLOAD:
        raise CertificateError("upload a certificate (.pem, .crt, .cer) or a .p12 file")
    for load in (x509.load_pem_x509_certificate, x509.load_der_x509_certificate):
        try:
            return load(data)
        except ValueError:
            continue
    try:
        _key, cert, _extra = pkcs12.load_key_and_certificates(
            data, password.encode() if password else None
        )
    except ValueError:
        raise CertificateError(
            "that isn't a certificate, or the .p12's password is wrong"
        ) from None
    del _key, _extra  # never kept: only the certificate is recorded
    if cert is None:
        raise CertificateError("that .p12 has no certificate in it")
    return cert


class ClientCertificates:
    """The built-in CA and the record of every certificate it issued."""

    def __init__(
        self,
        db: Database,
        data_dir: Path,
        *,
        device_days: int = DEVICE_DAYS,
        browser_days: int = BROWSER_DAYS,
    ) -> None:
        self.db = db
        self.device_days, self.browser_days = device_days, browser_days
        # Set by the app when Cloudflare is configured (ADR 0023): signs while it's the
        # chosen check (``use_cloudflare``), otherwise the built-in CA does.
        self.cloudflare: CloudflareIssuer | None = None
        self.use_cloudflare: Callable[[], bool] = lambda: False
        self.dir = data_dir / "remote-access"
        self.ca_cert_path = self.dir / "ca.pem"
        self.ca_key_path = self.dir / "ca.key"
        self.crl_path = self.dir / "crl.pem"
        self._lock = threading.Lock()

    # ---- the CA ----------------------------------------------------------------------

    def _ca(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """The CA, made the first time it's needed."""
        with self._lock:
            if not self.ca_cert_path.is_file() or not self.ca_key_path.is_file():
                self._make_ca()
            cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
            key = serialization.load_pem_private_key(self.ca_key_path.read_bytes(), None)
        assert isinstance(key, rsa.RSAPrivateKey)  # noqa: S101 (we made it)
        return cert, key

    def _make_ca(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dreadovania"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"Dreadovania client CA {new_id()[:8]}"),
        ])  # fmt: skip
        now = _now()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + CA_LIFETIME)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .sign(key, hashes.SHA256())
        )
        _write_private(
            self.ca_key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        _write_public(self.ca_cert_path, cert.public_bytes(serialization.Encoding.PEM))

    def ca_pem(self) -> bytes:
        """The CA certificate, for a proxy's ``ca-file`` or the server's own check."""
        cert, _ = self._ca()
        return cert.public_bytes(serialization.Encoding.PEM)

    # ---- issuing ---------------------------------------------------------------------

    def current_issuer(self) -> str:
        """Who signs new certificates: Cloudflare when it's the chosen check, else us."""
        return "cloudflare" if self.cloudflare is not None and self.use_cloudflare() else "builtin"

    def issue(
        self,
        owner_id: str,
        kind: Kind,
        name: str,
        *,
        device_id: str | None = None,
        days: int | None = None,
    ) -> Issued:
        """A new certificate for a Switch (``device``) or a browser."""
        name = " ".join(name.split())[:MAX_NAME] or ("Switch" if kind == "device" else "Browser")
        if kind == "device" and not device_id:
            raise CertificateError("a Switch's certificate belongs to its paired device")
        days = days or (self.device_days if kind == "device" else self.browser_days)
        key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
        cert_id = new_id()
        subject = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dreadovania"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"{kind} {cert_id}"),
        ])  # fmt: skip
        issuer = self.current_issuer()
        issuer_id: str | None = None
        if issuer == "cloudflare":
            cert, chain, issuer_id = self._sign_at_cloudflare(key, subject, days)
        else:
            cert, chain = self._sign_here(key, subject, days)
        password = secrets.token_urlsafe(12)
        # The older PBE (SHA-1 and 3DES) is the one the Switch's TLS service and
        # older iOS import reliably. The file is handed out once, over TLS.
        encryption = (
            serialization.PrivateFormat.PKCS12.encryption_builder()
            .kdf_rounds(50000)
            .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
            .hmac_hash(hashes.SHA1())  # noqa: S303 (required by the PBE above)
            .build(password.encode())
        )
        p12 = pkcs12.serialize_key_and_certificates(
            f"Dreadovania {name}".encode(), key, cert, chain, encryption
        )
        record = {
            "id": cert_id, "owner_id": owner_id, "device_id": device_id, "kind": kind,
            "name": name, "issuer": issuer, "issuer_id": issuer_id,
            "serial": format(cert.serial_number, "x"),
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "not_before": _iso(cert.not_valid_before_utc),
            "not_after": _iso(cert.not_valid_after_utc),
            "created_at": utc_now(),
        }  # fmt: skip
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO client_certificates (id, owner_id, device_id, kind, name, issuer,"
                " issuer_id, serial, sha256, not_before, not_after, created_at)"
                " VALUES (:id, :owner_id, :device_id, :kind, :name, :issuer, :issuer_id,"
                " :serial, :sha256, :not_before, :not_after, :created_at)",
                record,
            )
        return Issued(self._public(record | {"revoked_at": None, "replaced_by": None}),
                      p12, password)  # fmt: skip

    def _sign_here(
        self, key: rsa.RSAPrivateKey, subject: x509.Name, days: int
    ) -> tuple[x509.Certificate, list[x509.Certificate]]:
        ca_cert, ca_key = self._ca()
        now = _now()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
            .sign(ca_key, hashes.SHA256())
        )
        return cert, [ca_cert]

    def _sign_at_cloudflare(
        self, key: rsa.RSAPrivateKey, subject: x509.Name, days: int
    ) -> tuple[x509.Certificate, list[x509.Certificate], str]:
        from dreadovania.cloudflare import ZONE_LIMIT, CloudflareError

        assert self.cloudflare is not None  # noqa: S101 (current_issuer checked)
        csr = (
            x509.CertificateSigningRequestBuilder().subject_name(subject)
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM).decode()
        )  # fmt: skip
        try:
            if self.cloudflare.active_count() >= ZONE_LIMIT:
                raise CertificateError(
                    f"Cloudflare already has {ZONE_LIMIT} active client certificates for this"
                    " zone, its limit: revoke some first"
                )
            signed = self.cloudflare.sign(csr, days)
            cert = x509.load_pem_x509_certificate(signed.certificate_pem.encode())
        except CloudflareError as exc:
            raise CertificateError(str(exc)) from None
        except ValueError:
            raise CertificateError("Cloudflare's certificate couldn't be read") from None
        if cert.public_key().public_numbers() != key.public_key().public_numbers():
            raise CertificateError("Cloudflare signed a different key than the one asked for")
        return cert, [], signed.id

    def sync_revocations(self) -> int:
        """Pass revocations on to Cloudflare. Returns how many it accepted.

        Revoking here, by renewal, or by removing a device or an account (a database
        trigger) marks the row; this sends each one on, and retries next time if
        Cloudflare couldn't be reached.
        """
        if self.cloudflare is None:
            return 0
        from dreadovania.cloudflare import CloudflareError

        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, issuer_id FROM client_certificates WHERE issuer = 'cloudflare'"
                " AND revoked_at IS NOT NULL AND issuer_revoked_at IS NULL"
                " AND issuer_id IS NOT NULL"
            ).fetchall()
        done = 0
        for row in rows:
            try:
                self.cloudflare.revoke(row["issuer_id"])
            except CloudflareError as exc:
                log.warning("Couldn't revoke a certificate at Cloudflare yet: %s", exc)
                continue
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE client_certificates SET issuer_revoked_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )
            done += 1
        return done

    # ---- the External issuer: the owner's own CA (ADR 0023) ------------------------------

    @property
    def external_ca_path(self) -> Path:
        return self.dir / "external-ca.pem"

    def external_ca(self) -> x509.Certificate | None:
        try:
            return x509.load_pem_x509_certificate(self.external_ca_path.read_bytes())
        except (OSError, ValueError):
            return None

    def set_external_ca(self, data: bytes | None) -> x509.Certificate | None:
        """The owner's CA, so certificates it didn't issue are refused. None removes it."""
        if data is None:
            self.external_ca_path.unlink(missing_ok=True)
            return None
        ca = _load_certificate(data)
        try:
            is_ca = ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        if not is_ca:
            raise CertificateError("that isn't a CA certificate (it has no CA basic constraint)")
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_public(self.external_ca_path, ca.public_bytes(serialization.Encoding.PEM))
        return ca

    def record_external(
        self,
        owner_id: str,
        data: bytes,
        password: str | None,
        name: str,
        *,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a certificate from the owner's own CA: only its fingerprint and dates.

        ``data`` is the certificate (PEM or DER) or a .p12. A .p12 is opened in memory
        for its certificate; the file, its private key and its password are not kept
        and never written anywhere. The owner installs the certificate themselves.
        """
        cert = _load_certificate(data, password)
        now = _now()
        if cert.not_valid_after_utc <= now:
            raise CertificateError("that certificate has expired")
        try:
            if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise CertificateError("that's a CA certificate, not a device's or browser's")
        except x509.ExtensionNotFound:
            pass
        try:
            usages = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if ExtendedKeyUsageOID.CLIENT_AUTH not in usages:
                raise CertificateError(
                    "that certificate isn't for client authentication (no clientAuth usage)"
                )
        except x509.ExtensionNotFound:
            pass  # no restriction: any use, client authentication included
        ca = self.external_ca()
        if ca is not None:
            try:
                cert.verify_directly_issued_by(ca)
            except (ValueError, TypeError, InvalidSignature):
                raise CertificateError("that certificate wasn't issued by your CA") from None
        kind: Kind = "device" if device_id else "browser"
        name = " ".join(name.split())[:MAX_NAME] or ("Switch" if device_id else "Browser")
        sha256 = cert.fingerprint(hashes.SHA256()).hex()
        record = {
            "id": new_id(), "owner_id": owner_id, "device_id": device_id, "kind": kind,
            "name": name, "issuer": "external", "issuer_id": None,
            "serial": format(cert.serial_number, "x"), "sha256": sha256,
            "not_before": _iso(cert.not_valid_before_utc),
            "not_after": _iso(cert.not_valid_after_utc), "created_at": utc_now(),
        }  # fmt: skip
        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM client_certificates WHERE sha256 = ?", (sha256,)
            ).fetchone():
                raise CertificateError("that certificate is already recorded")
            conn.execute(
                "INSERT INTO client_certificates (id, owner_id, device_id, kind, name, issuer,"
                " issuer_id, serial, sha256, not_before, not_after, created_at)"
                " VALUES (:id, :owner_id, :device_id, :kind, :name, :issuer, :issuer_id,"
                " :serial, :sha256, :not_before, :not_after, :created_at)",
                record,
            )
        return self._public(record | {"revoked_at": None, "replaced_by": None})

    # ---- the record ------------------------------------------------------------------

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "kind", "name", "issuer", "sha256", "not_before", "not_after",
                "created_at", "revoked_at", "device_id")  # fmt: skip
        return {k: row.get(k) for k in keys}

    def list(self, owner_id: str, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        """An account's certificates, newest first."""
        where = "owner_id = ?" + ("" if include_revoked else " AND revoked_at IS NULL")
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM client_certificates WHERE {where} ORDER BY created_at DESC",  # noqa: S608
                (owner_id,),
            ).fetchall()
        return [self._public(dict(r)) for r in rows]

    def current_for_device(self, owner_id: str, device_id: str) -> dict[str, Any] | None:
        """A paired Switch's newest live certificate, or None."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM client_certificates WHERE owner_id = ? AND device_id = ?"
                " AND revoked_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (owner_id, device_id),
            ).fetchone()
        return self._public(dict(row)) if row else None

    def without_browser_certificate(self, user_ids: list[str]) -> list[str]:
        """Which of these accounts have no live browser certificate."""
        with self.db.connect() as conn:
            have = {
                r["owner_id"]
                for r in conn.execute(
                    "SELECT DISTINCT owner_id FROM client_certificates"
                    " WHERE kind = 'browser' AND revoked_at IS NULL AND not_after > ?",
                    (_iso(_now()),),
                )
            }
        return [u for u in user_ids if u not in have]

    def renew_after(self, cert: dict[str, Any]) -> str:
        """When the app should ask for a replacement: once a third of the lifetime is left."""
        start, end = _parse(cert["not_before"]), _parse(cert["not_after"])
        return _iso(end - (end - start) / 3)

    def get(self, owner_id: str, cert_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM client_certificates WHERE id = ? AND owner_id = ?",
                (cert_id, owner_id),
            ).fetchone()
        return self._public(dict(row)) if row else None

    def by_fingerprint(self, sha256: str) -> dict[str, Any] | None:
        """A certificate by its SHA-256 fingerprint, valid or not."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM client_certificates WHERE sha256 = ?", (sha256.lower(),)
            ).fetchone()
        return dict(row) if row else None

    def is_valid(self, sha256: str) -> bool:
        """Issued here, not revoked, and not expired."""
        row = self.by_fingerprint(sha256)
        return row is not None and row["revoked_at"] is None and _parse(row["not_after"]) > _now()

    # ---- revoking and renewing --------------------------------------------------------

    def revoke(self, owner_id: str, cert_id: str) -> bool:
        """Revoke one of an account's certificates. Returns whether it was live."""
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE client_certificates SET revoked_at = ?"
                " WHERE id = ? AND owner_id = ? AND revoked_at IS NULL",
                (utc_now(), cert_id, owner_id),
            ).rowcount
        if changed:
            self.publish_crl()
            self.sync_revocations()
        return bool(changed)

    def renew(self, owner_id: str, cert_id: str) -> Issued:
        """A replacement for a live certificate, with the same owner, kind and name.

        The old one stays valid until ``confirm_renewal`` (the new one has been
        used), so a renewal that never arrives doesn't cut a device off.
        """
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM client_certificates WHERE id = ? AND owner_id = ?"
                " AND revoked_at IS NULL",
                (cert_id, owner_id),
            ).fetchone()
        if row is None:
            raise CertificateError("that certificate isn't live, so it can't be renewed")
        old = dict(row)
        days = max(1, round((_parse(old["not_after"]) - _parse(old["not_before"])).days))
        issued = self.issue(owner_id, old["kind"], old["name"],
                            device_id=old["device_id"], days=days)  # fmt: skip
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE client_certificates SET replaced_by = ? WHERE id = ?",
                (issued.certificate["id"], cert_id),
            )
        return issued

    def confirm_renewal(self, owner_id: str, new_id_: str) -> int:
        """The new certificate has been used: revoke the ones it replaced."""
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE client_certificates SET revoked_at = ?"
                " WHERE replaced_by = ? AND owner_id = ? AND revoked_at IS NULL",
                (utc_now(), new_id_, owner_id),
            ).rowcount
        if changed:
            self.publish_crl()
            self.sync_revocations()
        return changed

    def due_for_renewal(self, cert: dict[str, Any]) -> bool:
        """Once a third of the lifetime is left (ADR 0023 amendment)."""
        start, end = _parse(cert["not_before"]), _parse(cert["not_after"])
        return _now() >= end - (end - start) / 3

    # ---- the revocation list -----------------------------------------------------------

    def publish_crl(self) -> bytes:
        """Write ``crl.pem`` with every revoked certificate that hasn't expired yet."""
        ca_cert, ca_key = self._ca()
        now = _now()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT serial, revoked_at, not_after FROM client_certificates"
                " WHERE revoked_at IS NOT NULL AND issuer = 'builtin'"
            ).fetchall()
        builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(ca_cert.subject)
            .last_update(now - timedelta(minutes=5))
            .next_update(now + CRL_LIFETIME)
        )
        for row in rows:
            if _parse(row["not_after"]) < now:
                continue  # expired anyway: no longer needs listing
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder()
                .serial_number(int(row["serial"], 16))
                .revocation_date(_parse(row["revoked_at"]))
                .build()
            )
        crl = builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        with self._lock:
            _write_public(self.crl_path, crl)
        return crl

    def crl_pem(self) -> bytes:
        """The current revocation list, republished when it's missing, a day old, or
        older than a revocation (removing a device or an account revokes in the
        database, by trigger, so this is where those reach the list)."""
        with self.db.connect() as conn:
            latest = conn.execute(
                "SELECT max(revoked_at) FROM client_certificates WHERE issuer = 'builtin'"
            ).fetchone()[0]
        try:
            crl = x509.load_pem_x509_crl(self.crl_path.read_bytes())
            published = crl.last_update_utc + timedelta(minutes=5)  # last_update is set back
            fresh = published > _now() - timedelta(days=1) and (
                latest is None or _parse(latest) <= published
            )
        except (OSError, ValueError):
            fresh = False
        if not fresh:
            return self.publish_crl()
        return self.crl_path.read_bytes()
