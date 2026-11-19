"""Phase 4F security validation of every mounted route of the new API."""
import json
import unittest

from tests.unit._api_fixtures import (ANTHROPIC_KEY, DISCORD_SECRET_A, DISCORD_TOKEN_A, DISCORD_TOKEN_B, HAVE_API,
                                      TELEGRAM_TOKEN_A, TELEGRAM_TOKEN_B, ZD_API_TOKEN, World)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class SecurityValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    def session_routes(self):
        """(method, path) of every mutating route that requires an account session."""
        out = []
        from api.app import route_table
        for r in route_table(self.w.app):
            dep = getattr(r, "dependant", None)
            if dep is None or not r.path.startswith("/api"):
                continue
            uses_session = any(getattr(d.call, "__name__", "") == "principal" for d in dep.dependencies)
            for m in r.methods or ():
                if m in ("POST", "PUT", "PATCH", "DELETE") and uses_session:
                    out.append((m, r.path))
        return out

    async def raw(self, method, path, *, cookie=None, headers=None, content=None):
        import httpx
        h = dict(headers or {})
        cookies = {"bbp_session": cookie} if cookie else None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="https://test",
                                     cookies=cookies) as c:
            return await c.request(method, path, headers=h, content=content)

    async def test_every_cookie_authenticated_mutation_requires_the_csrf_header(self):
        token = self.w._tokens["owner"]
        routes = self.session_routes()
        self.assertGreater(len(routes), 80)
        fill = {"{enterprise_id}": self.w.eA.id, "{organisation_id}": self.w.oA.id, "{bot_id}": self.w.bA.id,
                "{instance_id}": self.w.iA_web.id, "{owner_type:ownertype}": "enterprises",
                "{type_:objtype}": "bots", "{type_:policytype}": "enterprises", "{type_:scopetype}": "bots",
                "{op:gateway_op}": "start", "{scope_type:scopetype}": "bots", "{owner_type}": "enterprises"}
        missing = []
        for method, path in routes:
            p = path
            for k, v in fill.items():
                p = p.replace(k, v)
            import re
            p = re.sub(r"\{[^}]+\}", "8f7e6d5c-4b3a-4291-8a7b-6c5d4e3f2a1b", p)
            r = await self.raw(method, p, cookie=token, content=b"{}", headers={"Content-Type": "application/json"})
            if not (r.status_code == 403 and "Missing request header" in r.text):
                missing.append((method, path, r.status_code))
        self.assertEqual(missing, [], f"cookie-authenticated mutations without the CSRF header must be refused: "
                                      f"{missing}")

    async def test_bodies_are_bounded_everywhere(self):
        h = {"Authorization": "Bearer " + self.w._tokens["owner"], "Content-Type": "application/json"}
        big = json.dumps({"changes": {"behaviour.persona": "x" * (2 * 1024 * 1024)}}).encode()
        r = await self.raw("PATCH", f"/api/bots/{self.w.bA.id}/config", headers=h, content=big)
        self.assertEqual(r.status_code, 413)

        async def chunks():
            for _ in range(40):
                yield b"x" * 65536                                        # 2.5 MiB, no Content-Length
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="https://test") as c:
            r = await c.patch(f"/api/bots/{self.w.bA.id}/config", headers=h, content=chunks())
        self.assertEqual(r.status_code, 413)

    async def test_security_headers_and_widget_framing(self):
        r = await self.w.call("GET", f"/bots/{self.w.bA.id}", "viewer")
        for k, v in (("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer"),
                     ("cache-control", "no-store"), ("x-frame-options", "DENY")):
            self.assertEqual(r.headers.get(k), v, k)
        r = await self.w.call("GET", f"/public/widget/{self.w.iA_web.channel.public_id}")
        self.assertIn("frame-ancestors", r.headers.get("content-security-policy", ""))
        self.assertIsNone(r.headers.get("x-frame-options"))                # the widget may be embedded

    async def test_upload_and_outbound_rate_limits(self):
        import httpx
        src = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/knowledge/sources", "admin",
                                 {"name": "S"})).json()["id"]
        h = {**self.w.headers("admin"), "Content-Type": "text/plain", "X-Requested-With": "BitBotPro"}
        codes = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            for i in range(125):
                r = await c.put(f"/api/knowledge/sources/{src}/documents/d{i}.txt", content=b"Policy text.",
                                headers=h)
                codes.append(r.status_code)
        self.assertIn(429, codes)

    async def test_no_plaintext_secret_anywhere_in_the_database(self):
        dump = json.dumps(await self.w.snapshot(), default=str)
        for secret in (ANTHROPIC_KEY, TELEGRAM_TOKEN_A, TELEGRAM_TOKEN_B, DISCORD_TOKEN_A, DISCORD_TOKEN_B,
                       DISCORD_SECRET_A, ZD_API_TOKEN):
            self.assertNotIn(secret, dump)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class AdminOriginCorsTests(unittest.IsolatedAsyncioTestCase):
    """Phase 5: the admin app's exact origin may call the API with the session
    cookie; nothing else gets CORS headers; misconfiguration fails at start."""

    async def asyncSetUp(self):
        self.w = await World().build()

    def app(self, origins):
        from api.app import create_app
        self.w.S.settings.admin_origins = origins
        return create_app(self.w.S)

    async def req(self, app, method, path, headers):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.request(method, "/api" + path, headers=headers)

    async def test_listed_origin_only(self):
        app = self.app(("https://bitbotpro.io",))
        pre = {"Origin": "https://bitbotpro.io", "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "content-type,x-requested-with"}
        r = await self.req(app, "OPTIONS", "/auth/login", pre)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["access-control-allow-origin"], "https://bitbotpro.io")
        self.assertEqual(r.headers["access-control-allow-credentials"], "true")
        r = await self.req(app, "OPTIONS", "/auth/login", {**pre, "Origin": "https://evil.example"})
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), "https://evil.example")
        r = await self.req(app, "GET", "/auth/session", {"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        r = await self.req(app, "GET", "/auth/session", {"Origin": "https://bitbotpro.io"})
        self.assertEqual(r.status_code, 401)                                    # CORS never grants authority
        self.assertEqual(r.headers["access-control-allow-origin"], "https://bitbotpro.io")

    async def test_default_has_no_cors_and_wildcards_are_refused(self):
        r = await self.req(self.app(()), "GET", "/auth/session", {"Origin": "https://bitbotpro.io"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        for bad in (("*",), ("http://bitbotpro.io",), ("https://bitbotpro.io/",)):
            with self.assertRaises(ValueError):
                self.app(bad)
