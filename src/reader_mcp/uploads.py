"""Signed upload links: the "push" path for clients that hold a file the server can't reach.

create_upload_url returns  <PUBLIC_URL>/upload/<token>
  PUT  the raw bytes (e.g. `curl -T file.pdf <url>` from an agent's sandbox)
  GET  a minimal browser page for humans to pick the file
The token is HMAC-signed, expires after READER_UPLOAD_TTL seconds and can be used once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import Settings
from .sources import safe_filename

SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",  # the URL itself is the credential
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


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
            "human": "Open upload_url in a browser and choose the file.",
        },
        "next_step": f"After the upload succeeds, call ingest_document(upload_id='{upload_id}').",
    }


def verify_token(settings: Settings, token: str) -> dict[str, Any] | None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(settings.upload_secret, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if claims.get("e", 0) < time.time():
        return None
    return claims


async def handle_upload(request: Request, settings: Settings) -> Response:
    claims = verify_token(settings, request.path_params["token"])
    if claims is None:
        return JSONResponse({"error": "Upload link is invalid or expired."}, status_code=403, headers=SECURITY_HEADERS)

    if request.method == "GET":
        page = UPLOAD_PAGE.replace("{{filename}}", html.escape(claims["f"]))
        headers = {**SECURITY_HEADERS, "Content-Security-Policy":
                   "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"}
        return HTMLResponse(page, headers=headers)

    folder = settings.data_dir / "uploads"
    blob, partial = folder / f"{claims['u']}.bin", folder / f"{claims['u']}.part"
    if blob.exists() or partial.exists():
        return JSONResponse({"error": "This upload link was already used."}, status_code=409, headers=SECURITY_HEADERS)

    size = 0
    try:
        with partial.open("xb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise OverflowError
                out.write(chunk)
    except OverflowError:
        partial.unlink(missing_ok=True)
        return JSONResponse({"error": f"File exceeds {settings.max_file_bytes} bytes."}, status_code=413,
                            headers=SECURITY_HEADERS)
    except FileExistsError:
        return JSONResponse({"error": "This upload link was already used."}, status_code=409, headers=SECURITY_HEADERS)
    if size == 0:
        partial.unlink(missing_ok=True)
        return JSONResponse({"error": "Empty upload."}, status_code=400, headers=SECURITY_HEADERS)

    (folder / f"{claims['u']}.json").write_text(json.dumps({"filename": claims["f"]}))
    partial.replace(blob)
    return JSONResponse({"status": "uploaded", "upload_id": claims["u"], "bytes": size,
                         "next_step": f"Call ingest_document(upload_id='{claims['u']}')."},
                        headers=SECURITY_HEADERS)


UPLOAD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload to reader</title>
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fafafa; --muted:#666; --card:#fff; --line:#ddd; --ok:#127a3a; --err:#b42318; }
  @media (prefers-color-scheme: dark) { :root { --fg:#eee; --bg:#141414; --muted:#999; --card:#1e1e1e; --line:#333; --ok:#4ade80; --err:#f87171; } }
  body { margin:0; min-height:100vh; display:grid; place-items:center; font:16px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--fg); }
  main { width:min(420px, calc(100% - 32px)); background:var(--card); border:1px solid var(--line); border-radius:12px; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; } p { margin:0 0 16px; color:var(--muted); }
  input { width:100%; margin-bottom:12px; } button { width:100%; padding:10px; border-radius:8px; border:0; font:inherit; cursor:pointer; }
  #status { margin-top:12px; min-height:1.5em; } .ok { color:var(--ok); } .err { color:var(--err); }
</style></head>
<body><main>
  <h1>Upload a document</h1>
  <p>Requested file: <strong>{{filename}}</strong></p>
  <input id="file" type="file"><button id="go">Upload</button><div id="status"></div>
</main>
<script>
  const status = document.getElementById("status");
  document.getElementById("go").onclick = async () => {
    const file = document.getElementById("file").files[0];
    if (!file) { status.textContent = "Choose a file first."; return; }
    status.className = ""; status.textContent = "Uploading…";
    try {
      const res = await fetch(location.href, { method: "PUT", body: file });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || res.statusText);
      status.className = "ok"; status.textContent = "Uploaded. Go back to your chat and say it's done.";
    } catch (e) { status.className = "err"; status.textContent = "Upload failed: " + e.message; }
  };
</script></body></html>
"""
