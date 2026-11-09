"""Entry point: ``python -m dreadovania``."""

import logging
import ssl
import sys

import uvicorn

from dreadovania import tls
from dreadovania.app import create_app
from dreadovania.config import Settings

log = logging.getLogger("dreadovania")


def uvicorn_config(settings: Settings) -> uvicorn.Config:
    """The server's settings: HTTPS when certificate files are set (ADR 0005)."""
    pair = tls.check_pair(settings.tls_cert_file, settings.tls_key_file)
    ssl_factory = None
    if pair is not None:
        reloader = tls.CertificateReloader(*pair)

        def ssl_factory(_config: uvicorn.Config, _default: object) -> ssl.SSLContext:
            return reloader.context

    proxies = settings.trusted_proxy_list
    return uvicorn.Config(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Forwarded headers only from the proxies the user lists (ADR 0005).
        proxy_headers=bool(proxies),
        forwarded_allow_ips=proxies or [],
        ssl_context_factory=ssl_factory,
        server_header=False,
        date_header=False,
    )


def main() -> None:
    settings = Settings()
    try:
        config = uvicorn_config(settings)
    except tls.TlsError as exc:
        logging.basicConfig()
        log.error("HTTPS can't start: %s", exc)
        sys.exit(2)
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
