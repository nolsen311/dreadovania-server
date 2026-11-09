# SPDX-License-Identifier: GPL-3.0-or-later
"""Docker's health check: ``python -m dreadovania.healthcheck``.

It asks the server in the same container for /healthz over loopback, using
HTTPS when certificate files are configured. The certificate is issued for the
hostname, not 127.0.0.1, so this one check skips certificate verification: it
sends nothing secret and never leaves the container. Everything else verifies.
"""

import ssl
import sys
import urllib.request

from dreadovania.config import Settings


def main() -> int:
    settings = Settings()
    https = settings.tls_cert_file is not None and settings.tls_key_file is not None
    url = f"{'https' if https else 'http'}://127.0.0.1:{settings.port}/healthz"
    context = None
    if https:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # loopback only; see the module docstring
    try:
        with urllib.request.urlopen(url, timeout=3, context=context) as response:  # noqa: S310 (fixed loopback URL)
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
