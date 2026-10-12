"""Runtime settings. Every value is optional and read from READER_* environment variables."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def _env(name: str) -> str | None:
    value = os.environ.get(f"READER_{name}", "").strip()
    return value or None


def _number(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"reader: READER_{name} must be a number, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    allowed_roots: tuple[Path, ...]
    max_file_bytes: int
    extract_timeout: float
    retention_hours: float
    public_url: str
    upload_secret: bytes
    upload_ttl: int
    auth_token: str | None

    @classmethod
    def from_env(cls, *, http: bool = False, host: str = "127.0.0.1", port: int = 8000) -> Settings:
        roots_env = _env("ALLOWED_ROOTS")
        if roots_env is not None:
            roots = tuple(Path(p).expanduser().resolve() for p in roots_env.split(os.pathsep) if p.strip())
        else:
            # Local (stdio) clients may read files under $HOME by default; remote (HTTP)
            # clients must not read the server's disk unless explicitly allowed.
            roots = () if http else (Path.home().resolve(),)

        secret = _env("UPLOAD_SECRET")
        if http and secret is None:
            print("reader: READER_UPLOAD_SECRET not set; upload links stop working on restart.", file=sys.stderr)

        default_host = "localhost" if host in LOCAL_HOSTS else host
        public_url = (_env("PUBLIC_URL") or f"http://{default_host}:{port}").rstrip("/")
        if urlsplit(public_url).scheme not in ("http", "https"):
            sys.exit("reader: READER_PUBLIC_URL must start with http:// or https://")

        return cls(
            data_dir=Path(_env("DATA_DIR") or Path.home() / ".reader-mcp").expanduser().resolve(),
            allowed_roots=roots,
            max_file_bytes=int(_number("MAX_FILE_MB", 2048) * 1024 * 1024),
            extract_timeout=_number("EXTRACT_TIMEOUT", 600),
            retention_hours=_number("RETENTION_HOURS", 24),
            public_url=public_url,
            upload_secret=(secret or secrets.token_hex(32)).encode(),
            upload_ttl=int(_number("UPLOAD_TTL", 3600)),
            auth_token=_env("AUTH_TOKEN"),
        )
