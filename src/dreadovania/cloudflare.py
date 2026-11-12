# SPDX-License-Identifier: GPL-3.0-or-later
"""Client certificates from Cloudflare's own CA, for a hostname Cloudflare proxies
(ADR 0023, decision 2).

On most plans Cloudflare's mTLS only accepts certificates from its managed CA. The
server makes the key and a certificate signing request (CSR), and Cloudflare signs
it through the API (``POST /zones/{zone}/client_certificates``); revoking is a
``DELETE`` on the same resource. The API token needs only "SSL and Certificates
Write" for the one zone, and is read from a secret file, never logged.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

API = "https://api.cloudflare.com/client/v4"
ZONE_LIMIT = 100  # active client certificates per zone, outside Enterprise

# (method, path, body) -> parsed JSON response; tests pass a fake.
Request = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


class CloudflareError(Exception):
    """The message is safe to show to the admin (it never contains the token)."""


@dataclass(frozen=True)
class Signed:
    id: str
    certificate_pem: str
    sha256: str
    expires_on: str


def _http(token: str) -> Request:
    def request(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 (fixed https API base)
            f"{API}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "User-Agent": "dreadovania-server"},
        )  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310
                return json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read(1024 * 1024))
            except ValueError:
                raise CloudflareError(f"Cloudflare answered HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise CloudflareError(f"couldn't reach Cloudflare's API ({exc})") from None

    return request


class CloudflareIssuer:
    def __init__(self, zone_id: str, token: str = "", request: Request | None = None) -> None:
        self.zone_id = zone_id
        self._request = request or _http(token)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self._request(method, f"/zones/{self.zone_id}/client_certificates{path}", body)
        if not answer.get("success"):
            errors = "; ".join(
                str(e.get("message", e)) for e in answer.get("errors") or [] if isinstance(e, dict)
            )
            raise CloudflareError(f"Cloudflare refused: {errors or 'no reason given'}")
        return answer

    def sign(self, csr_pem: str, validity_days: int) -> Signed:
        result = self._call("POST", "", {"csr": csr_pem, "validity_days": validity_days})["result"]
        try:
            return Signed(
                id=str(result["id"]),
                certificate_pem=str(result["certificate"]),
                sha256=str(result.get("fingerprint_sha256", "")).lower().replace(":", ""),
                expires_on=str(result.get("expires_on", "")),
            )
        except (KeyError, TypeError):
            raise CloudflareError("Cloudflare's answer had no certificate in it") from None

    def revoke(self, cert_id: str) -> None:
        self._call("DELETE", f"/{cert_id}")

    def find(self, sha256: str) -> str | None:
        """The ID of the zone's active client certificate with this fingerprint, if any."""
        wanted = sha256.lower().replace(":", "")
        page, pages = 1, 1
        while page <= pages and page <= 10:  # 100 per zone outside Enterprise: 2 pages
            answer = self._call("GET", f"?status=active&per_page=50&page={page}")
            for cert in answer.get("result") or []:
                if not isinstance(cert, dict):
                    continue
                found = str(cert.get("fingerprint_sha256", "")).lower().replace(":", "")
                if found == wanted and cert.get("id"):
                    return str(cert["id"])
            pages = int((answer.get("result_info") or {}).get("total_pages") or 1)
            page += 1
        return None

    def active_count(self) -> int:
        # per_page must be 5 to 50 (Cloudflare's API description).
        answer = self._call("GET", "?status=active&per_page=5")
        info = answer.get("result_info") or {}
        count = info.get("total_count", info.get("count"))
        if count is None:
            raise CloudflareError("Cloudflare didn't say how many certificates are active")
        return int(count)


def from_settings(zone_id: str | None, token_file: Any) -> CloudflareIssuer | None:
    """The issuer, when both settings are given. A missing or empty token file is a
    mistake worth stopping for, so it raises."""
    if not zone_id and token_file is None:
        return None
    if not zone_id or token_file is None:
        raise CloudflareError(
            "set both DREADOVANIA_CLOUDFLARE_ZONE_ID and DREADOVANIA_CLOUDFLARE_API_TOKEN_FILE,"
            " or neither"
        )
    try:
        # utf-8-sig drops the byte-order mark some editors (Notepad) put first.
        token = token_file.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise CloudflareError(
            f"the Cloudflare API token file can't be read ({exc.strerror})"
        ) from None
    except UnicodeDecodeError:
        raise CloudflareError("the Cloudflare API token file isn't text") from None
    if not token:
        raise CloudflareError("the Cloudflare API token file is empty")
    # A token goes in an HTTP header: only printable ASCII, no spaces. Say which
    # position is wrong, never the token itself.
    for position, char in enumerate(token, start=1):
        if not ("!" <= char <= "~"):
            raise CloudflareError(
                f"the Cloudflare API token file has a character that can't be in a token at"
                f" position {position} (U+{ord(char):04X}): save it with only the token on"
                " one line, as plain text"
            )
    return CloudflareIssuer(zone_id.strip(), token)
