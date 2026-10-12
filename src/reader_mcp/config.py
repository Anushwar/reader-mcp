"""Runtime settings. Every value is optional and read from READER_* environment variables."""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def _env(name: str) -> str | None:
    value = os.environ.get(f"READER_{name}", "").strip()
    return value or None


def _stored_secret(data_dir: Path) -> str:
    """Load the upload-signing key from <data_dir>/upload_secret, creating it on first run.

    Keeping it on disk means upload links (and uploads in progress) survive restarts. It sits
    next to the documents it protects, in a folder only the owner can open, so anyone able to
    read it could already read the documents. The file is created atomically with 0600
    permissions (two servers starting together still agree on one key) and never logged.
    """
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = data_dir / "upload_secret"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(3):
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            fd_tmp, tmp = tempfile.mkstemp(dir=data_dir)  # mkstemp creates files with 0600
            with os.fdopen(fd_tmp, "w") as out:
                out.write(secrets.token_hex(32))
            try:
                os.link(tmp, path)  # atomic "create if absent"
            except FileExistsError:
                pass  # another process won the race; use its key
            finally:
                os.unlink(tmp)
            continue
        except OSError as exc:  # e.g. the path is a symlink (O_NOFOLLOW)
            sys.exit(f"reader: cannot read {path}: {exc.strerror}")
        with os.fdopen(fd) as file:
            if hasattr(os, "fchmod") and os.fstat(file.fileno()).st_mode & 0o077:
                os.fchmod(file.fileno(), 0o600)  # tighten a key file that was made readable by others
            secret = file.read().strip()
        if len(secret) >= 32:
            return secret
        sys.exit(f"reader: {path} is too short to be a signing key; delete it to generate a new one.")
    sys.exit(f"reader: could not create {path}")


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

        default_host = "localhost" if host in LOCAL_HOSTS else host
        public_url = (_env("PUBLIC_URL") or f"http://{default_host}:{port}").rstrip("/")
        if urlsplit(public_url).scheme not in ("http", "https"):
            sys.exit("reader: READER_PUBLIC_URL must start with http:// or https://")

        data_dir = Path(_env("DATA_DIR") or Path.home() / ".reader-mcp").expanduser().resolve()
        secret = _env("UPLOAD_SECRET")
        if secret is None:
            # Upload links only exist in HTTP mode; stdio never signs anything.
            secret = _stored_secret(data_dir) if http else secrets.token_hex(32)

        return cls(
            data_dir=data_dir,
            allowed_roots=roots,
            max_file_bytes=int(_number("MAX_FILE_MB", 2048) * 1024 * 1024),
            extract_timeout=_number("EXTRACT_TIMEOUT", 600),
            retention_hours=_number("RETENTION_HOURS", 24),
            public_url=public_url,
            upload_secret=secret.encode(),
            upload_ttl=int(_number("UPLOAD_TTL", 3600)),
            auth_token=_env("AUTH_TOKEN"),
        )
