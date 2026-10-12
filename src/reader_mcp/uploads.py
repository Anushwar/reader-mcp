"""Signed upload links: the "push" path for clients that hold a file the server can't reach.

create_upload_url returns  <PUBLIC_URL>/upload/<token>
  PUT  the whole file (e.g. `curl -T file.pdf <url>` from an agent's sandbox), or
  PUT  resumable pieces with `Content-Range: bytes <start>-<end>/<total>`
  GET  ?status  -> how many bytes have arrived, so an interrupted upload can resume
  GET  a browser page that uploads in resumable pieces (large files, phones, flaky networks)

The token is HMAC-signed and single-use. It must be first used within READER_UPLOAD_TTL
seconds; an upload that has started may continue as long as pieces keep arriving.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import Settings
from .sources import safe_filename

MAX_PIECE_BYTES = 64 * 1024 * 1024  # stays under the ~100 MB per-request cap of common proxies
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",  # the URL itself is the credential
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
_locks: dict[str, asyncio.Lock] = {}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _reply(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=SECURITY_HEADERS)


def create_upload(settings: Settings, filename: str) -> dict[str, Any]:
    upload_id = secrets.token_hex(16)
    expires = int(time.time()) + settings.upload_ttl
    payload = _b64(json.dumps({"u": upload_id, "f": safe_filename(filename), "e": expires}).encode())
    signature = _b64(hmac.new(settings.upload_secret, payload.encode(), hashlib.sha256).digest())
    url = f"{settings.public_url}/upload/{payload}.{signature}"
    return {
        "upload_id": upload_id,
        "upload_url": url,
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
        "max_bytes": settings.max_file_bytes,
        "how_to_upload": {
            "agent_sandbox": f"curl -sS -T '<path to the file>' '{url}'",
            "human": "Open upload_url in a browser and choose the file. Large files upload in resumable pieces.",
            "resumable": "PUT pieces with 'Content-Range: bytes <start>-<end>/<total>'; GET <upload_url>?status "
                         "returns the bytes received so far.",
        },
        "next_step": f"After the upload succeeds, call ingest_document(upload_id='{upload_id}').",
    }


def verify_token(settings: Settings, token: str) -> dict[str, Any] | None:
    """Return the token's claims if the signature is valid (expiry is checked by the caller)."""
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(settings.upload_secret, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        return json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None


async def handle_upload(request: Request, settings: Settings) -> Response:
    claims = verify_token(settings, request.path_params["token"])
    if claims is None:
        return _reply({"error": "This upload link is not valid. Ask for a new one."}, 403)

    folder = settings.data_dir / "uploads"
    upload_id = claims["u"]
    blob, partial, state, used = (folder / f"{upload_id}{ext}" for ext in (".bin", ".part", ".json", ".used"))
    done = lambda: blob.exists() or used.exists()  # noqa: E731  (.used: the upload was already ingested)
    if claims.get("e", 0) < time.time() and not (partial.exists() or done()):  # expiry only blocks new uploads
        return _reply({"error": "Upload link has expired. Ask for a new one."}, 403)

    if request.method == "GET":
        if "status" in request.query_params:
            received = blob.stat().st_size if blob.exists() else partial.stat().st_size if partial.exists() else 0
            return _reply({"received": received, "complete": done()})
        page = UPLOAD_PAGE.replace("{{filename}}", html.escape(claims["f"]))
        headers = {**SECURITY_HEADERS, "Content-Security-Policy":
                   "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"}
        return HTMLResponse(page, headers=headers)

    async with _locks.setdefault(upload_id, asyncio.Lock()):
        # Checked under the lock: a request queued behind a finishing upload must see it as used.
        if done():
            response = _reply({"error": "This upload link was already used.", "complete": True}, 409)
        elif request.headers.get("content-range"):
            response = await _receive_piece(request, settings, claims, request.headers["content-range"],
                                            blob, partial, state)
        else:
            response = await _receive_whole(request, settings, claims, blob, partial, state)
    if done():
        _locks.pop(upload_id, None)  # finished for good; any later request is refused by done()
    return response


async def _receive_whole(request: Request, settings: Settings, claims: dict[str, Any],
                         blob: Path, partial: Path, state: Path) -> Response:
    if partial.exists():
        if state.exists():
            return _reply({"error": "A resumable upload is already in progress for this link."}, 409)
        partial.unlink()  # left over from an interrupted whole-file upload (e.g. a server restart): start over
    size = 0
    try:
        with partial.open("xb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise OverflowError
                out.write(chunk)
    except (OverflowError, ClientDisconnect, OSError) as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, OverflowError):
            return _reply({"error": f"File exceeds {settings.max_file_bytes} bytes."}, 413)
        if isinstance(exc, OSError):
            return _reply({"error": "The server could not store the upload."}, 507)
        return _reply({"error": "Upload interrupted."}, 400)
    if size == 0:
        partial.unlink(missing_ok=True)
        return _reply({"error": "Empty upload."}, 400)
    return _finish(claims, blob, partial, state, size)


async def _receive_piece(request: Request, settings: Settings, claims: dict[str, Any], content_range: str,
                         blob: Path, partial: Path, state: Path) -> Response:
    match = CONTENT_RANGE.fullmatch(content_range.strip())
    if not match:
        return _reply({"error": "Content-Range must look like 'bytes <start>-<end>/<total>'."}, 400)
    start, end, total = map(int, match.groups())
    if end < start or end >= total:
        return _reply({"error": "Invalid Content-Range."}, 400)
    if total > settings.max_file_bytes:
        return _reply({"error": f"File exceeds {settings.max_file_bytes} bytes."}, 413)
    if end - start + 1 > MAX_PIECE_BYTES:
        return _reply({"error": f"Pieces must be at most {MAX_PIECE_BYTES} bytes."}, 413)

    received = partial.stat().st_size if partial.exists() else 0
    known = json.loads(state.read_text()) if state.exists() else {}
    if known.get("total", total) != total:
        return _reply({"error": "Total size changed between pieces.", "received": received}, 409)
    if start != received:  # out of order, or a retry of a piece that already arrived
        return _reply({"error": "Resume from the received offset.", "received": received}, 409)

    expected, written = end - start + 1, 0
    try:
        with partial.open("ab") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > expected:
                    raise ValueError
                out.write(chunk)
        if written != expected:
            raise ValueError
    except (ValueError, ClientDisconnect, OSError) as exc:
        os.truncate(partial, received)  # drop the broken piece; the client resumes from `received`
        if isinstance(exc, OSError):
            return _reply({"error": "The server could not store the upload.", "received": received}, 507)
        return _reply({"error": "Piece did not match its Content-Range.", "received": received}, 400)

    received += written
    state.write_text(json.dumps({"filename": claims["f"], "total": total}))  # also keeps the upload "active"
    if received == total:
        return _finish(claims, blob, partial, state, received)
    return _reply({"status": "partial", "received": received, "total": total})


def _finish(claims: dict[str, Any], blob: Path, partial: Path, state: Path, size: int) -> Response:
    state.write_text(json.dumps({"filename": claims["f"]}))
    partial.replace(blob)
    return _reply({"status": "uploaded", "upload_id": claims["u"], "bytes": size,
                   "next_step": f"Call ingest_document(upload_id='{claims['u']}')."})


UPLOAD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload to reader</title>
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fafafa; --muted:#666; --card:#fff; --line:#ddd; --bar:#2f6fde; --ok:#127a3a; --err:#b42318; }
  @media (prefers-color-scheme: dark) { :root { --fg:#eee; --bg:#141414; --muted:#999; --card:#1e1e1e; --line:#333; --bar:#6c9cf0; --ok:#4ade80; --err:#f87171; } }
  body { margin:0; min-height:100vh; display:grid; place-items:center; font:16px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--fg); }
  main { width:min(420px, calc(100% - 32px)); background:var(--card); border:1px solid var(--line); border-radius:12px; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; } p { margin:0 0 16px; color:var(--muted); }
  input { width:100%; margin-bottom:12px; } button { width:100%; padding:10px; border-radius:8px; border:0; font:inherit; cursor:pointer; }
  progress { width:100%; height:8px; margin-top:12px; accent-color:var(--bar); }
  #status { margin-top:8px; min-height:1.5em; } .ok { color:var(--ok); } .err { color:var(--err); }
</style></head>
<body><main>
  <h1>Upload a document</h1>
  <p>Requested file: <strong>{{filename}}</strong></p>
  <input id="file" type="file"><button id="go">Upload</button>
  <progress id="bar" value="0" max="1" hidden></progress><div id="status"></div>
</main>
<script>
  // Uploads in resumable pieces: only one piece is in memory at a time, failed pieces are
  // retried, and reopening this link with the same file continues where it stopped.
  const PIECE = 8 * 1024 * 1024;
  const target = location.pathname;
  const pause = (ms) => new Promise((done) => setTimeout(done, ms));

  // The link itself is unusable (invalid, expired, file too large): retrying won't help.
  class LinkError extends Error {}

  async function received() {
    const res = await fetch(target + "?status=1");
    const body = await res.json();
    if (!res.ok) throw new LinkError(body.error || res.statusText);
    return body.received || 0;
  }

  async function sendPiece(file, start) {
    const end = Math.min(start + PIECE, file.size);
    for (let attempt = 0; ; attempt++) {
      let res, body;
      try {
        res = await fetch(target, { method: "PUT", body: file.slice(start, end),
          headers: { "Content-Range": `bytes ${start}-${end - 1}/${file.size}` } });
        body = await res.json();
      } catch (error) {
        // A server that rejects a piece early can look like a dropped connection, so ask
        // whether the link still works before treating this as a network blip.
        try { await received(); } catch (check) { if (check instanceof LinkError) throw check; }
        if (attempt >= 5) throw new Error("Connection keeps dropping. Tap Upload again to resume.");
        await pause(1000 * 2 ** attempt);
        continue;
      }
      if (res.ok) return body.received ?? end;
      if (body.complete) return file.size;
      if (res.status === 409 && body.received !== undefined) return body.received;  // resume where the server is
      if (res.status === 400 && body.received !== undefined && attempt < 5) { await pause(1000); continue; }
      throw new LinkError(body.error || res.statusText);  // permanent: expired link, file too large, ...
    }
  }

  async function upload(file, onProgress) {
    let offset = await received();
    while (offset < file.size) {
      offset = await sendPiece(file, offset);
      onProgress(offset / file.size);
    }
  }

  const status = document.getElementById("status");
  const bar = document.getElementById("bar");
  document.getElementById("go").onclick = async () => {
    const file = document.getElementById("file").files[0];
    if (!file) { status.textContent = "Choose a file first."; return; }
    status.className = ""; bar.hidden = false;
    try {
      await upload(file, (done) => { bar.value = done; status.textContent = `Uploading… ${Math.floor(done * 100)}%`; });
      status.className = "ok"; status.textContent = "Uploaded. Go back to your chat and say it's done.";
    } catch (error) {
      status.className = "err";
      status.textContent = error instanceof LinkError ? error.message : "Upload paused: " + error.message;
    }
  };
</script></body></html>
"""
