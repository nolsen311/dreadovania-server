"""Runtime configuration, read from ``DREADOVANIA_*`` environment variables."""

import ipaddress
from pathlib import Path

from pydantic import AnyHttpUrl, Field, field_validator
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
    # Uploading a RomFS zip from the web UI (ADR 0013 §1). Chunks must fit through
    # any reverse proxy's request size limit (nginx defaults to 1 MB: raise it).
    max_romfs_upload_bytes: int = Field(default=20 * 1024**3, gt=0)
    max_romfs_files: int = Field(default=100_000, gt=0)
    romfs_upload_chunk_bytes: int = Field(default=8 * 1024**2, ge=64 * 1024, le=100 * 1024**2)

    # The address users will type, e.g. https://dread.home.example.com.
    # Passkeys are bound to this origin (ADR 0004/0005). Set by the setup wizard later.
    public_url: AnyHttpUrl | None = None

    # HTTPS (ADR 0005): mount the certificate files and point at them. Renewed
    # files are picked up without a restart. Without them the server speaks HTTP,
    # for use behind a TLS reverse proxy or on a LAN without passkeys.
    tls_cert_file: Path | None = None  # fullchain.pem
    tls_key_file: Path | None = None  # privkey.pem

    # Reverse proxies whose X-Forwarded-For/-Proto headers are believed, as
    # comma-separated addresses or networks (e.g. "172.18.0.0/16"). Empty trusts
    # none, so a client can't spoof its address or scheme.
    trusted_proxies: str = ""

    # Job runner (ADR 0011 §2).
    job_timeout_seconds: float = Field(default=600, gt=0)
    # Patching reads and rewrites several GB, so it gets longer (ADR 0013 §2).
    export_timeout_seconds: float = Field(default=1800, gt=0)
    max_queued_jobs_per_user: int = Field(default=5, ge=1)

    @property
    def romfs_path(self) -> Path:
        return self.romfs_dir if self.romfs_dir is not None else self.data_dir / "romfs"

    @field_validator("trusted_proxies")
    @classmethod
    def _check_proxies(cls, value: str) -> str:
        for entry in value.split(","):
            if entry.strip():
                try:
                    ipaddress.ip_network(entry.strip(), strict=False)
                except ValueError:
                    raise ValueError(
                        f"{entry.strip()!r} isn't an IP address or network, e.g. 172.18.0.0/16"
                    ) from None
        return value

    @property
    def trusted_proxy_list(self) -> list[str]:
        """Normalised for uvicorn, which ignores a network with host bits set."""
        out = []
        for entry in self.trusted_proxies.split(","):
            if entry.strip():
                net = ipaddress.ip_network(entry.strip(), strict=False)
                single = net.prefixlen == net.max_prefixlen
                out.append(str(net.network_address) if single else str(net))
        return out
