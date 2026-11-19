"""Router factory for the Spec 012 API.

:func:`build_router` returns an ``APIRouter`` with every Phase 3 endpoint.
:func:`create_app` wraps it in a standalone FastAPI app for tests and local
development only. **Nothing in the production application imports this
package; it is mounted only at the Phase 7 cutover.**
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from api import (auth, config, credentials, deletion, discord, hierarchy, integrations, knowledge, lifecycle,
                 members, runtime_routes, telegram, usage, users, webchat)
from api.common import Services, make_principal_dependency


def build_router(services: Services) -> APIRouter:
    router = APIRouter()
    principal = make_principal_dependency(services)
    for module in (auth, hierarchy, config, lifecycle, members, credentials, deletion, runtime_routes, webchat, telegram,
                   discord, integrations, knowledge, usage, users):
        module.register(router, services, principal)
    return router


MAX_BODY_BYTES = 1024 * 1024                      # Phase 4F security review: every route
MAX_UPLOAD_BODY_BYTES = 3 * 1024 * 1024           # knowledge uploads (the route itself enforces 2 MiB)


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Bounded request bodies for every route (Content-Length *and* streamed or
    chunked bodies). Before Phase 4F only webhooks and uploads were bounded."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        limit = MAX_UPLOAD_BODY_BYTES if "/knowledge/sources/" in path and "/documents/" in path else MAX_BODY_BYTES
        headers = dict(scope.get("headers") or [])
        try:
            if int(headers.get(b"content-length", b"0") or 0) > limit:
                return await self._reject(send)
        except ValueError:
            return await self._reject(send, 400, b'{"detail":"Bad request."}')
        # read the whole body (bounded) BEFORE the app runs, then replay it: the
        # limit holds for streamed/chunked bodies too, whatever the framework does
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                messages.append(message)
                break
            size += len(message.get("body") or b"")
            if size > limit:
                return await self._reject(send)
            messages.append(message)
            if not message.get("more_body"):
                break
        replay = iter(messages)

        async def replayed():
            try:
                return next(replay)
            except StopIteration:
                return await receive()
        await self.app(scope, replayed, send)

    @staticmethod
    async def _reject(send, status=413, body=b'{"detail":"Request body too large."}'):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Baseline headers on every API response unless the route set its own
    (the Webchat widget sets its CSP frame-ancestors; assets set caching)."""
    DEFAULTS = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                (b"cache-control", b"no-store"), (b"x-frame-options", b"DENY"))

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers") or []}
                extra = [(k, v) for k, v in self.DEFAULTS if k not in present and
                         not (k == b"x-frame-options" and b"content-security-policy" in present)]
                message = {**message, "headers": list(message.get("headers") or []) + extra}
            await send(message)
        await self.app(scope, receive, with_headers)


def route_table(app) -> list:
    """Every HTTP route of ``app`` with its full path, methods and dependant (Phase 6B).

    FastAPI ≥ 0.13x keeps included routers lazily (``app.routes`` holds ``_IncludedRouter``
    objects whose ``effective_route_contexts`` carry the real routes); older versions
    flattened them into ``APIRoute`` objects. Route inventories (permission matrix, CSRF
    sweep, public-route review, frontend contract) must use this helper, never
    ``app.routes`` directly, or they silently see nothing."""
    out = []

    def walk(routes):
        for r in routes:
            ctx = getattr(r, "effective_route_contexts", None)
            if ctx is not None:
                walk(ctx() if callable(ctx) else ctx)
            elif getattr(r, "path", None) is not None and getattr(r, "methods", None):
                out.append(r)
    walk(app.routes)
    return out


def create_app(services: Services, *, prefix: str = "/api") -> FastAPI:
    app = FastAPI(title="BitBotPro API (Spec 012)", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(build_router(services), prefix=prefix)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodyLimitMiddleware)
    origins = [o for o in (services.settings.admin_origins or ()) if isinstance(o, str)]
    if origins:
        if any(o == "*" or not o.startswith("https://") or o.rstrip("/") != o for o in origins):
            raise ValueError("admin_origins must be exact https origins without a trailing slash")
        from starlette.middleware.cors import CORSMiddleware
        # outermost: preflights are answered before body limits; credentials only for the listed origins
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", "X-Requested-With"], max_age=600)
    app.state.spec012 = services
    return app
