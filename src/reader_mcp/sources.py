"""Resolve the ways a file can reach this server into a file on disk.

    path            local file, only under READER_ALLOWED_ROOTS          (agent shares our disk)
    url / file      server pulls over HTTPS; `file` is ChatGPT's file param (pull)
    upload_id       client pushed bytes to a signed upload URL           (push)
    content_base64  bytes inline in the tool call; small files only

Every source is streamed into a temporary file while it is hashed, so large documents
never have to fit in memory.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx2

from .config import Settings

PATH_EXTENSIONS = {".pdf", ".docx", ".csv", ".tsv", ".txt", ".md", ".markdown", ".json"}
BASE64_MAX_BYTES = 10 * 1024 * 1024  # base64 travels through the model's context; keep it small
MAX_REDIRECTS = 5
CHUNK_BYTES = 1024 * 1024
UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")
WEB_PAGE = ("This link returned a web page, not a document. Use a direct download link "
            "(e.g. a presigned S3 URL or the file's own download link).")
APP_DATA_DIRS = ("Library", "AppData")  # macOS / Windows per-user app settings, keys and tokens
IPV6_EMBEDDING_IPV4 = tuple(ipaddress.ip_network(n) for n in
                            ("64:ff9b::/96", "64:ff9b:1::/48", "::ffff:0:0:0/96", "::/96"))

# httpx logs full request URLs at INFO; signed download URLs are credentials, so keep them out of logs.
for _name in ("httpx2", "httpcore2"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class SourceError(ValueError):
    """The file could not be obtained from the given source."""


@dataclass
class Incoming:
    path: Path  # temporary file; the store moves it into place
    filename: str
    source: str
    sha256: str
    size: int


def safe_filename(name: str | None, fallback: str = "document") -> str:
    name = Path(unquote(name or "")).name
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name).strip(". ")[:200]
    return name or fallback


class Spool:
    """Write chunks to a temp file in <data_dir>/tmp, hashing and enforcing the size limit as they arrive."""

    def __init__(self, settings: Settings, limit: int | None = None):
        folder = settings.data_dir / "tmp"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=folder)
        self.path, self.file = Path(name), os.fdopen(fd, "wb")
        self.hash, self.size = hashlib.sha256(), 0
        self.limit = limit or settings.max_file_bytes

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            raise SourceError(_too_big(self.limit))
        self.hash.update(chunk)
        self.file.write(chunk)

    def finish(self, filename: str, source: str) -> Incoming:
        self.file.close()
        if self.size == 0:
            self.path.unlink(missing_ok=True)
            raise SourceError("The file is empty.")
        return Incoming(self.path, filename, source, self.hash.hexdigest(), self.size)

    def __enter__(self) -> Spool:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        if exc_type is not None:
            self.file.close()
            self.path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- path


def read_path(raw: str, settings: Settings) -> Incoming:
    if not settings.allowed_roots:
        raise SourceError("Local paths are disabled on this server. Use url, file, upload_id or content_base64.")
    if os.name == "nt" and raw.replace("/", "\\").startswith("\\\\"):
        # Resolving a network path would contact that machine (and can leak credentials) before any check.
        raise SourceError("Network paths are not readable.")
    try:
        real = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise SourceError(f"File not found: {raw}") from None

    root = next((r for r in settings.allowed_roots if real.is_relative_to(r)), None)
    if root is None:
        raise SourceError("Path is outside the folders this server may read (READER_ALLOWED_ROOTS).")
    if any(part.startswith(".") for part in real.relative_to(root).parts):
        raise SourceError("Hidden files and folders are not readable.")
    for private in APP_DATA_DIRS:
        folder = Path.home() / private
        # Apps keep settings and credentials here; readable only if a root was explicitly set inside it.
        if real.is_relative_to(folder) and not root.is_relative_to(folder):
            raise SourceError(f"Files under ~/{private} (app settings and credentials) are not readable.")
    if real.suffix.lower() not in PATH_EXTENSIONS:
        raise SourceError(f"Unsupported extension. Supported: {', '.join(sorted(PATH_EXTENSIONS))}")
    if not real.is_file():
        raise SourceError("Path is not a regular file.")
    with Spool(settings) as spool, real.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
            spool.write(chunk)
        return spool.finish(real.name, "path")


# --------------------------------------------------------------------------- url


def direct_download_url(url: str) -> str:
    """Rewrite common share links (which open a viewer page) into direct-download links."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "drive.google.com":
        match = re.search(r"/file/d/([\w-]+)", parts.path)
        file_id = match.group(1) if match else parse_qs(parts.query).get("id", [""])[0]
        if file_id:
            return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    if host == "docs.google.com":
        match = re.match(r"/document/d/([\w-]+)", parts.path)
        if match:
            return f"https://docs.google.com/document/d/{match.group(1)}/export?format=docx"
    if host in ("dropbox.com", "www.dropbox.com"):
        query = parse_qs(parts.query)
        query["dl"] = ["1"]
        return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
    return url


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local:
            return False
        # IPv6 forms that carry an IPv4 address (mapped, 6to4, Teredo, NAT64, SIIT, IPv4-compatible)
        # are judged by that IPv4 address, so they can't be used to reach internal IPv4 hosts.
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and any(ip in net for net in IPV6_EMBEDDING_IPV4):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return _is_public(embedded)
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
    """Stream a download to disk over HTTPS, with SSRF protection.

    Every hop (including redirects) must resolve only to public IPs, and the connection is
    pinned to the IP we checked (with SNI/Host set to the real hostname) so DNS can't be
    swapped between the check and the request.
    """
    url = direct_download_url(url)
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
            # identity: ask for the bytes as stored, so a tiny compressed response cannot expand on our side
            headers = {"Host": parts.netloc, "Accept": "*/*", "Accept-Encoding": "identity", "User-Agent": "reader-mcp"}
            try:
                async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get("location", ""))
                        continue
                    if resp.status_code != 200:
                        raise SourceError(f"Download failed with HTTP {resp.status_code}.")
                    if resp.headers.get("content-type", "").lower().startswith(("text/html", "application/xhtml")):
                        raise SourceError(WEB_PAGE)
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > settings.max_file_bytes:
                        raise SourceError(_too_big(settings.max_file_bytes))
                    with Spool(settings) as spool:
                        async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                            spool.write(chunk)
                        name = safe_filename(filename) if filename else _filename_from(resp, url)
                        return spool.finish(name, source)
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
    try:
        os.utime(blob)  # mark it fresh first, so the periodic cleanup can't remove it mid-ingest
    except FileNotFoundError:
        raise SourceError("No completed upload with this id. Upload the file first; "
                          "uploads that wait too long before ingestion are cleaned up.") from None
    filename = json.loads(meta.read_text()).get("filename", "document") if meta.exists() else "document"
    digest = hashlib.sha256()
    with blob.open("rb") as data:
        for chunk in iter(lambda: data.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    # Mark the link as spent before the file moves away, so it can't accept a second upload.
    (folder / f"{upload_id}.used").touch()
    meta.unlink(missing_ok=True)
    return Incoming(blob, filename, "upload", digest.hexdigest(), blob.stat().st_size)


def decode_base64(content: str, filename: str | None, settings: Settings) -> Incoming:
    content = "".join(content.split())
    if content.startswith("data:") and "," in content:
        content = content.split(",", 1)[1]
    limit = min(settings.max_file_bytes, BASE64_MAX_BYTES)
    if len(content) > limit * 4 // 3 + 4:
        raise SourceError(_too_big(limit) + " Use url or create_upload_url for larger files.")
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise SourceError("content_base64 is not valid base64.") from None
    with Spool(settings, limit) as spool:
        spool.write(data)
        return spool.finish(safe_filename(filename), "base64")


def _too_big(limit: int) -> str:
    return f"File is larger than the {limit // (1024 * 1024)} MB limit (READER_MAX_FILE_MB)."
