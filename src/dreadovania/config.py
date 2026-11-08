"""Runtime configuration, read from ``DREADOVANIA_*`` environment variables."""

from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DREADOVANIA_")

    # Listening socket. Inside the container we bind all interfaces; the port is
    # unprivileged so the server never needs root or CAP_NET_BIND_SERVICE.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080

    # Writable state (seeds, database). The only writable mount in the container.
    data_dir: Path = Path("/data")

    # The user's extracted RomFS (ADR 0013). Defaults to <data_dir>/romfs; usually a
    # read-only mount. Never served, listed or logged.
    romfs_dir: Path | None = None

    # The address users will type, e.g. https://dread.home.example.com.
    # Passkeys are bound to this origin (ADR 0004/0005). Set by the setup wizard later.
    public_url: AnyHttpUrl | None = None

    # Job runner (ADR 0011 §2).
    job_timeout_seconds: float = Field(default=600, gt=0)
    max_queued_jobs_per_user: int = Field(default=5, ge=1)

    @property
    def romfs_path(self) -> Path:
        return self.romfs_dir if self.romfs_dir is not None else self.data_dir / "romfs"
