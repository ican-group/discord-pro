"""Security response headers for the API (spec 008 Stage 10). Pure ASGI, stdlib only.

Applied to every HTTP response (including CORS preflights, 503s, redirects and
streamed SSE) without buffering bodies. The API serves JSON, one JavaScript
loader (embed.js) and logo images, never HTML documents, so the policy is
deny-by-default:

* CSP `default-src 'none'; frame-ancestors 'none'` + `X-Frame-Options: DENY`:
  nothing the API returns may be framed or render active content.
* `Cross-Origin-Resource-Policy: same-site`, except `cross-origin` for the
  resources customer websites load directly (embed.js, bot logos, embed config).
* `Cache-Control: no-store` for account, chat, invitation and admin responses.
* HSTS (production only; the public edge is HTTPS via Cloudflare).
Existing headers set by a route are never overridden.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

CSP = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
HSTS = b"max-age=31536000; includeSubDomains"
BASE_HEADERS: Tuple[Tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", CSP),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)
# Loaded by customer websites (script tag, <img>, cross-origin fetch).
CROSS_ORIGIN_EXACT = ("/api/embed.js",)
CROSS_ORIGIN_PREFIXES = ("/api/bot-logo/", "/api/embed/config/")
NO_STORE_PREFIXES = ("/api/auth/", "/api/chat/", "/api/invite/", "/api/admin/", "/api/v2/")


def headers_for(path: str, hsts: bool) -> List[Tuple[bytes, bytes]]:
    out = list(BASE_HEADERS)
    cross = path in CROSS_ORIGIN_EXACT or path.startswith(CROSS_ORIGIN_PREFIXES)
    corp = b"cross-origin" if cross else b"same-site"
    out.append((b"cross-origin-resource-policy", corp))
    if path.startswith(NO_STORE_PREFIXES):
        out.append((b"cache-control", b"no-store"))
    if hsts:
        out.append((b"strict-transport-security", HSTS))
    return out


def _merge(existing: Iterable[Tuple[bytes, bytes]], extra: Iterable[Tuple[bytes, bytes]]):
    headers = list(existing)
    present = {k.lower() for k, _ in headers}
    headers.extend((k, v) for k, v in extra if k not in present)
    return headers


class SecurityHeadersMiddleware:
    def __init__(self, app, hsts: bool = False):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        extra = headers_for(scope.get("path", ""), self.hsts)

        async def send_with_headers(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = _merge(message.get("headers") or [], extra)
            await send(message)

        await self.app(scope, receive, send_with_headers)
