"""Resolve the ways a file can reach this server into raw bytes.

    path            local file, only under READER_ALLOWED_ROOTS          (agent shares our disk)
    url / file      server pulls over HTTPS; `file` is ChatGPT's file param (pull)
    upload_id       client pushed bytes to a signed upload URL           (push)
    content_base64  bytes inline in the tool call; small files only
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx2

from .config import Settings

PATH_EXTENSIONS = {".pdf", ".docx", ".csv", ".tsv", ".txt", ".md", ".markdown", ".json"}

# httpx logs full request URLs at INFO; signed download URLs are credentials, so keep them out of logs.
for _name in ("httpx2", "httpcore2"):
    logging.getLogger(_name).setLevel(logging.WARNING)
MAX_REDIRECTS = 3
UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")


class SourceError(ValueError):
    """The file could not be obtained from the given source."""


@dataclass
class Incoming:
    data: bytes
    filename: str
    source: str


def safe_filename(name: str | None, fallback: str = "document") -> str:
    name = Path(unquote(name or "")).name
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name).strip(". ")[:200]
    return name or fallback


# --------------------------------------------------------------------------- path


def read_path(raw: str, settings: Settings) -> Incoming:
    if not settings.allowed_roots:
        raise SourceError("Local paths are disabled on this server. Use url, file, upload_id or content_base64.")
    try:
        real = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise SourceError(f"File not found: {raw}") from None

    root = next((r for r in settings.allowed_roots if real.is_relative_to(r)), None)
    if root is None:
        raise SourceError("Path is outside the folders this server may read (READER_ALLOWED_ROOTS).")
    if any(part.startswith(".") for part in real.relative_to(root).parts):
        raise SourceError("Hidden files and folders are not readable.")
    if real.suffix.lower() not in PATH_EXTENSIONS:
        raise SourceError(f"Unsupported extension. Supported: {', '.join(sorted(PATH_EXTENSIONS))}")
    if not real.is_file():
        raise SourceError("Path is not a regular file.")
    if real.stat().st_size > settings.max_file_bytes:
        raise SourceError(_too_big(settings))
    return Incoming(real.read_bytes(), real.name, "path")


# --------------------------------------------------------------------------- url


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve_public(host: str, port: int) -> str:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise SourceError(f"Could not resolve host: {host}") from None
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public(ipaddress.ip_address(a.split("%")[0])) for a in addresses):
        raise SourceError("URL points to a private or internal address, which is not allowed.")
    ipv4 = sorted(a for a in addresses if ":" not in a)
    return ipv4[0] if ipv4 else sorted(addresses)[0]


def _filename_from(response: httpx2.Response, url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.IGNORECASE)
    return safe_filename(match.group(1) if match else urlsplit(url).path)


async def fetch_url(url: str, settings: Settings, filename: str | None = None, source: str = "url") -> Incoming:
    """Download over HTTPS with SSRF protection.

    Every hop (including redirects) must resolve only to public IPs, and the connection is
    pinned to the IP we checked (with SNI/Host set to the real hostname) so DNS can't be
    swapped between the check and the request.
    """
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(30.0), follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise SourceError("Only https:// URLs are allowed.")
            if parts.username or parts.password:
                raise SourceError("URLs with embedded credentials are not allowed.")
            host, port = parts.hostname, parts.port or 443
            ip = await _resolve_public(host, port)
            pinned = parts._replace(netloc=f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}").geturl()
            headers = {"Host": parts.netloc, "Accept": "*/*", "User-Agent": "reader-mcp"}
            try:
                async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get("location", ""))
                        continue
                    if resp.status_code != 200:
                        raise SourceError(f"Download failed with HTTP {resp.status_code}.")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > settings.max_file_bytes:
                        raise SourceError(_too_big(settings))
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > settings.max_file_bytes:
                            raise SourceError(_too_big(settings))
                        chunks.append(chunk)
                    name = safe_filename(filename) if filename else _filename_from(resp, url)
                    return Incoming(b"".join(chunks), name, source)
            except httpx2.HTTPError as exc:
                # Never echo the URL back: signed download links are bearer credentials.
                raise SourceError(f"Download failed ({type(exc).__name__}).") from None
    raise SourceError("Too many redirects.")


# --------------------------------------------------------------------------- upload / base64


def read_upload(upload_id: str, settings: Settings) -> Incoming:
    if not UPLOAD_ID.match(upload_id):
        raise SourceError("Invalid upload_id.")
    folder = settings.data_dir / "uploads"
    blob, meta = folder / f"{upload_id}.bin", folder / f"{upload_id}.json"
    if not blob.exists():
        raise SourceError("No completed upload with this id. Upload the file to the upload_url first.")
    filename = json.loads(meta.read_text()).get("filename", "document") if meta.exists() else "document"
    data = blob.read_bytes()
    blob.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return Incoming(data, filename, "upload")


def decode_base64(content: str, filename: str | None, settings: Settings) -> Incoming:
    content = "".join(content.split())
    if content.startswith("data:") and "," in content:
        content = content.split(",", 1)[1]
    if len(content) > settings.max_file_bytes * 4 // 3 + 4:
        raise SourceError(_too_big(settings))
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise SourceError("content_base64 is not valid base64.") from None
    return Incoming(data, safe_filename(filename), "base64")


def _too_big(settings: Settings) -> str:
    return f"File is larger than the {settings.max_file_bytes // (1024 * 1024)} MB limit (READER_MAX_FILE_MB)."
