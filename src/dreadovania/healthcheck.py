# SPDX-License-Identifier: GPL-3.0-or-later
"""Docker's health check: ``python -m dreadovania.healthcheck``.

It asks the server in the same container for /healthz over loopback, using
HTTPS when certificate files are configured. The certificate is issued for the
hostname, not 127.0.0.1, so this one check skips certificate verification: it
sends nothing secret and never leaves the container. Everything else verifies.

With secure remote access on (ADR 0023), the main port asks for a client
certificate, so the check asks the front-door port first, which never does.
"""

import ssl
import sys
import urllib.request

from dreadovania.config import Settings


def main() -> int:
    settings = Settings()
    https = settings.tls_cert_file is not None and settings.tls_key_file is not None
    ports = [settings.port]
    if https and settings.front_door_port:
        ports.insert(0, settings.front_door_port)
    context = None
    if https:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # loopback only; see the module docstring
    for port in ports:
        url = f"{'https' if https else 'http'}://127.0.0.1:{port}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=3, context=context) as response:  # noqa: S310 (fixed loopback URL)
                if response.status == 200:
                    return 0
        except OSError:
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
