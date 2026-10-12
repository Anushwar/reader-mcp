"""Command line entry point: `reader` (stdio) or `reader --http`."""

from __future__ import annotations

import argparse
import hmac
import sys
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .server import build_server

HOSTED_CLIENT_ORIGINS = ["https://chatgpt.com", "https://claude.ai"]


class BearerAuth:
    """Require `Authorization: Bearer <READER_AUTH_TOKEN>` on the MCP endpoint (upload links carry their own signature)."""

    def __init__(self, app: ASGIApp, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Keep DNS-rebinding protection on, but accept the public hostname too."""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", *HOSTED_CLIENT_ORIGINS]
    public = urlsplit(settings.public_url)
    if public.hostname not in ("localhost", "127.0.0.1", "::1"):
        hosts += [public.netloc, f"{public.hostname}:*"]
        origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reader", description="MCP server that reads PDF, DOCX, CSV and text files.")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP at /mcp instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--version", action="version", version=f"reader-mcp {__version__}")
    args = parser.parse_args(argv)

    settings = Settings.from_env(http=args.http, host=args.host, port=args.port)
    server = build_server(settings, http=args.http)
    if not args.http:
        server.run("stdio")
        return

    import uvicorn

    app: ASGIApp = server.streamable_http_app(
        stateless_http=True, transport_security=_transport_security(settings), host=args.host,
        max_request_body_size=16 * 1024 * 1024,  # room for base64 files up to the 10 MB cap
    )
    if settings.auth_token:
        app = BearerAuth(app, settings.auth_token)
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        print("reader: listening beyond localhost without READER_AUTH_TOKEN.", file=sys.stderr)
    print(f"reader: MCP endpoint http://{args.host}:{args.port}/mcp  (public: {settings.public_url}/mcp)", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
