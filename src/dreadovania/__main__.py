"""Entry point: ``python -m dreadovania``."""

import uvicorn

from dreadovania.app import create_app
from dreadovania.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        # Forwarded headers are only trusted from an explicit allowlist (ADR 0005),
        # which arrives with the TLS work. Until then, trust none.
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
