"""Runtime configuration, read from ``DREADOVANIA_*`` environment variables."""

from pathlib import Path

from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DREADOVANIA_")

    # Listening socket. Inside the container we bind all interfaces; the port is
    # unprivileged so the server never needs root or CAP_NET_BIND_SERVICE.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080

    # Writable state (seeds, database). The only writable mount in the container.
    data_dir: Path = Path("/data")

    # The address users will type, e.g. https://dread.home.example.com.
    # Passkeys are bound to this origin (ADR 0004/0005). Set by the setup wizard later.
    public_url: AnyHttpUrl | None = None
