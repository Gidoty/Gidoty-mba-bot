"""Two small browser-facing guards (docs/security/browser_guards.md), both OFF by default.

CSRF_ORIGIN_CHECK: the session cookie is SameSite=None (the frontend is on another site), so a page on any other site can make the browser send it. CORS stops that page READING answers, and a JSON body forces
a preflight, but a request with no body or a text/plain body is a "simple" request that is sent anyway. This middleware refuses an unsafe request (POST, PUT, PATCH, DELETE) that comes from another site:
  * an Origin header that is not the frontend's own origin (or is "null") is refused with 403;
  * no Origin, but Sec-Fetch-Site: cross-site, is refused;
  * no Origin and no Sec-Fetch-Site (a command-line client, a server) passes: such a client cannot carry a victim's cookie;
  * the three provider callbacks (billing webhook, NOWPayments IPN, Telegram webhook) are server-to-server and never checked here (they have their own signature or secret).
SECURITY_HEADERS: adds nosniff, no-referrer, frame denial, a deny-all content policy and no-store caching to API answers (the interactive docs, when enabled, keep their own policy), and HSTS on https requests."""
from __future__ import annotations

from starlette.responses import JSONResponse

from .ratelimit import EXEMPT_PATHS

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
REFUSED = "this request came from another site and was refused"


def _norm(origin: str) -> str:
    return (origin or "").strip().rstrip("/").lower()


class OriginCheckMiddleware:
    def __init__(self, app, *, allowed_origin: str):
        self.app, self.allowed = app, _norm(allowed_origin)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in UNSAFE or scope["path"] in EXEMPT_PATHS:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        if origin is not None:
            bad = _norm(origin) != self.allowed
        else:
            bad = headers.get("sec-fetch-site", "").strip().lower() == "cross-site"
        if bad:
            return await JSONResponse({"detail": REFUSED}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        is_doc = path in DOC_PATHS or path.startswith("/docs/")
        https = scope.get("scheme") == "https"

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                have = {k.decode("latin-1").lower() for k, _ in headers}
                add = {"x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY"}
                if not is_doc:
                    add["content-security-policy"] = "default-src 'none'; frame-ancestors 'none'"
                if path.startswith("/api/"):
                    add["cache-control"] = "no-store"
                if https:
                    add["strict-transport-security"] = "max-age=15552000; includeSubDomains"
                for k, v in add.items():
                    if k not in have:
                        headers.append((k.encode(), v.encode()))
            await send(message)

        return await self.app(scope, receive, wrapped)
