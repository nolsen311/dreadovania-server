# SPDX-License-Identifier: GPL-3.0-or-later
"""Keeping the Switch app up to date (ADR 0021).

The server finds the latest release of the app on GitHub, downloads its ``.nro``
once, and offers it only after checking its SHA-256 and its build-provenance
attestation: a Sigstore bundle that must be signed, for that file's exact
digest, by the app's ``release.yml`` on the release's tag. This is the check
``gh attestation verify`` does. Sigstore's trust root is the one built into the
``sigstore`` library (its offline mode), so the server contacts only GitHub.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

APP_REPO = "nolsen311/dreadovania-nx"
ASSET = "dreadovania.nro"
# The oldest app this server works with. Raise it when the API changes in a way
# older apps can't handle; they are then told to update first (ADR 0021 §4).
MINIMUM_APP_VERSION = "0.2.0"
CHECK_EVERY = 6 * 60 * 60  # seconds between release checks
MAX_NRO_BYTES = 64 * 1024 * 1024
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

Fetch = Callable[[str], bytes]
# (bundle JSON, sha256 hex, tag) -> raises UpdateProblem if it doesn't verify.
Verify = Callable[[str, str, str], None]


class UpdateProblem(Exception):
    """Why no update is offered. The message is safe to show."""


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


@dataclass(frozen=True)
class AppRelease:
    version: str
    size: int
    sha256: str
    path: Path


def _fetch(url: str, limit: int = MAX_NRO_BYTES) -> bytes:
    request = urllib.request.Request(  # noqa: S310 (only https URLs from GitHub's API)
        url,
        headers={"User-Agent": "dreadovania-server", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateProblem("the download was larger than expected")
    return data


def verify_attestation(bundle_json: str, sha256: str, tag: str, repo: str = APP_REPO) -> None:
    """Check a build-provenance bundle: signed by the release workflow, for this digest."""
    from sigstore.errors import Error as SigstoreError
    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    identity = policy.Identity(
        identity=f"https://github.com/{repo}/.github/workflows/release.yml@refs/tags/{tag}",
        issuer=OIDC_ISSUER,
    )
    try:
        bundle = Bundle.from_json(bundle_json)
        _type, payload = Verifier.production(offline=True).verify_dsse(bundle, identity)
        statement = json.loads(payload)
    except (SigstoreError, ValueError, KeyError) as exc:
        raise UpdateProblem(f"the release's signature didn't verify: {exc}") from None
    digests = {s.get("digest", {}).get("sha256") for s in statement.get("subject", [])}
    if sha256 not in digests:
        raise UpdateProblem("the release's signature is for a different file")


class AppUpdates:
    """The latest verified app release, checked at most every CHECK_EVERY seconds."""

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = True,
        repo: str = APP_REPO,
        fetch: Fetch = _fetch,
        verify: Verify | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dir = data_dir / "app"
        self.enabled = enabled
        self.repo = repo
        self.fetch = fetch
        self.verify = verify or (lambda b, s, t: verify_attestation(b, s, t, repo))
        self.clock = clock
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._result: AppRelease | UpdateProblem | None = None

    def latest(self) -> AppRelease:
        """The latest verified release, or UpdateProblem saying why there's none."""
        if not self.enabled:
            raise UpdateProblem("app updates are turned off on this server")
        with self._lock:
            now = self.clock()
            if self._checked_at is None or now - self._checked_at >= CHECK_EVERY:
                try:
                    self._result = self._check()
                except UpdateProblem as exc:
                    log.warning("No app update offered: %s", exc)
                    self._result = exc
                except OSError as exc:  # network, disk
                    log.warning("Couldn't check for app updates: %s", exc)
                    self._result = UpdateProblem("the server couldn't reach GitHub")
                self._checked_at = now
            if isinstance(self._result, UpdateProblem):
                raise self._result
            assert self._result is not None  # noqa: S101 (set just above)
            return self._result

    def _check(self) -> AppRelease:
        api = f"https://api.github.com/repos/{self.repo}"
        try:
            release = json.loads(self.fetch(f"{api}/releases/latest"))
        except (ValueError, UnicodeDecodeError):
            raise UpdateProblem("GitHub's answer wasn't readable") from None
        tag = str(release.get("tag_name", ""))
        if parse_version(tag) is None:
            raise UpdateProblem(f"the latest release ({tag or 'none'}) isn't a version number")
        version = tag.removeprefix("v")
        folder = self.dir / version
        done = folder / ASSET
        if done.is_file() and (folder / "verified").is_file():
            digest = (folder / "verified").read_text(encoding="utf-8").strip()
            return AppRelease(version, done.stat().st_size, digest, done)

        assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", [])}
        if not assets.get(ASSET) or not assets.get(f"{ASSET}.sha256"):
            raise UpdateProblem(f"release {tag} has no {ASSET} and checksum")
        downloads = f"https://github.com/{self.repo}/releases/download/"
        if not all(str(assets[n]).startswith(downloads) for n in (ASSET, f"{ASSET}.sha256")):
            raise UpdateProblem(f"release {tag}'s files aren't GitHub release downloads")
        expected = self.fetch(assets[f"{ASSET}.sha256"]).decode("ascii", "replace").split()[0]
        data = self.fetch(assets[ASSET])
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected.lower():
            raise UpdateProblem(f"release {tag}'s {ASSET} doesn't match its checksum")
        try:
            attestations = json.loads(self.fetch(f"{api}/attestations/sha256:{digest}"))
            bundles = [a["bundle"] for a in attestations.get("attestations", [])]
        except (ValueError, KeyError, TypeError):
            raise UpdateProblem(f"release {tag} has no readable attestation") from None
        if not bundles:
            raise UpdateProblem(f"release {tag} has no attestation")
        problems = []
        for bundle in bundles:
            try:
                self.verify(json.dumps(bundle), digest, tag)
                break
            except UpdateProblem as exc:
                problems.append(str(exc))
        else:
            raise UpdateProblem(problems[0])

        # Verified: keep it, and remove older versions.
        if self.dir.exists():
            for old in self.dir.iterdir():
                if old.name != version:
                    shutil.rmtree(old, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f"{ASSET}.partial"
        partial.write_bytes(data)
        partial.replace(done)
        (folder / "verified").write_text(digest, encoding="utf-8")
        log.info("App %s is verified and offered to paired Switches", version)
        return AppRelease(version, len(data), digest, done)
