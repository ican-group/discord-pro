"""security_headers (spec 008 Stage 10). Offline: drives the ASGI middleware directly."""
import asyncio
import unittest

from tests.unit import _support  # noqa: F401
import security_headers as sh


def run_app(path, route_headers=(), hsts=True, kind="http"):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": list(route_headers)})
        await send({"type": "http.response.body", "body": b"chunk-1", "more_body": True})
        await send({"type": "http.response.body", "body": b"chunk-2"})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    mw = sh.SecurityHeadersMiddleware(app, hsts=hsts)
    asyncio.run(mw({"type": kind, "path": path}, receive, send))
    start = sent[0]
    return {k.decode(): v.decode() for k, v in start.get("headers", [])}, sent


class SecurityHeaderTests(unittest.TestCase):
    def test_baseline_headers_on_api_responses(self):
        h, _ = run_app("/api/health")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertEqual(h["cross-origin-resource-policy"], "same-site")
        self.assertEqual(h["strict-transport-security"], "max-age=31536000; includeSubDomains")
        self.assertNotIn("cache-control", h)

    def test_customer_loaded_resources_are_cross_origin(self):
        for path in ("/api/embed.js", "/api/bot-logo/bot-a", "/api/embed/config/bot-a"):
            with self.subTest(path=path):
                self.assertEqual(run_app(path)[0]["cross-origin-resource-policy"], "cross-origin")
        self.assertEqual(run_app("/api/embed.jsx")[0]["cross-origin-resource-policy"], "same-site")

    def test_sensitive_responses_not_cached(self):
        for path in ("/api/auth/login", "/api/chat/history/x", "/api/invite/t", "/api/admin/users", "/api/v2/orgs"):
            with self.subTest(path=path):
                self.assertEqual(run_app(path)[0]["cache-control"], "no-store")

    def test_route_headers_win_and_are_not_duplicated(self):
        h, sent = run_app("/api/chat/stream", route_headers=[(b"cache-control", b"no-cache"),
                                                             (b"content-type", b"text/event-stream")])
        self.assertEqual(h["cache-control"], "no-cache")
        names = [k for k, _ in sent[0]["headers"]]
        self.assertEqual(len(names), len(set(names)))

    def test_streaming_body_untouched(self):
        _, sent = run_app("/api/chat/stream")
        self.assertEqual([m.get("body") for m in sent[1:]], [b"chunk-1", b"chunk-2"])
        self.assertTrue(sent[1]["more_body"])

    def test_hsts_only_when_enabled(self):
        self.assertNotIn("strict-transport-security", run_app("/api/health", hsts=False)[0])

    def test_non_http_scopes_pass_through(self):
        async def app(scope, receive, send):
            await send({"type": "lifespan.startup.complete"})
        out = []

        async def send(m):
            out.append(m)
        asyncio.run(sh.SecurityHeadersMiddleware(app)({"type": "lifespan"}, None, send))
        self.assertEqual(out, [{"type": "lifespan.startup.complete"}])


if __name__ == "__main__":
    unittest.main()
