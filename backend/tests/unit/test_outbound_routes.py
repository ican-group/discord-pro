"""Spec 010 — Ollama SSRF and Zendesk token-exposure regression tests (in-process).

Every outbound HTTP call is intercepted (httpx MockTransport / fake aiohttp
session) and recorded, so the tests prove *where* requests would go and that
blocked requests never leave the process. No real network or database.
"""
import asyncio
import copy
import importlib.util
import os
import sys
import unittest
from unittest import mock

from tests.unit._support import BACKEND_DIR  # noqa: F401

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "motor", "httpx", "bcrypt", "jwt", "jose", "aiohttp"))

TEST_ENV = {
    "APP_MODE": "api", "APP_ENV": "production", "CORS_ORIGINS": "https://bitbotpro.io",
    "MONGO_URL": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=300&connectTimeoutMS=300",
    "DB_NAME": "unit_test_never_used", "JWT_SECRET": "unit-test-jwt-secret-not-real-0123456789",
    "CODE_HMAC_KEY": "unit-test-code-hmac-key-not-real-abcdefghijklmnop",
    "PUBLIC_APP_URL": "https://bitbotpro.io", "PUBLIC_API_URL": "https://api.bitbotpro.io",
    "AUTH_RESPONSE_FLOOR_MS": "0", "ENCRYPTION_KEY": "dW5pdC10ZXN0LWZlcm5ldC1rZXktbm90LXJlYWwtMDA=",
}
ALLOW = {"OLLAMA_ALLOWED_BASE_URLS": "https://ollama.example.com"}
INTERNAL_SECRET = "INTERNAL-SERVICE-CONTENT-must-not-leak"
FAKE_ZD_TOKEN = "UNITTEST-zendesk-oauth-token-000000000000"
ORG = "org-a"

server = None
client = None


def setUpModule():
    global server, client
    if not HAVE_DEPS:
        return
    for k in ("CLAUDE_API_KEY", "EMERGENT_LLM_KEY", "ADMIN_EMAIL", "ADMIN_PASSWORD",
              "OLLAMA_ALLOWED_BASE_URLS", "OLLAMA_BASE_URL", "OLLAMA_ALLOW_PRIVATE_NETWORKS"):
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
    sys.modules.pop("server", None)
    import server as _server  # noqa: E402
    from fastapi.testclient import TestClient
    server = _server
    client = TestClient(server.app)


def tearDownModule():
    global server, client
    mc = getattr(server, "mongo_client", None)
    if mc is not None:
        mc.close()
    server = client = None


def _seed(store):
    store.col("users").append({"id": "alice", "email": "alice@example.org", "username": "alice", "role": "user",
                               "is_verified": True, "token_version": 0, "hashed_password": "x"})
    store.col("organisations").append({"id": ORG, "name": "A", "workspace_id": None, "owner_id": "alice",
                                       "member_ids": ["alice"], "member_roles": {"alice": "admin"}})
    store.col("bots").append({"id": "bot-a", "org_id": ORG, "name": "A", "llm_config": {"provider": "anthropic"}})


class _Base(unittest.TestCase):
    def setUp(self):
        from tests.unit._fake_motor import patch_motor
        self.store = patch_motor(self)
        _seed(self.store)
        self.h = {"Authorization": f"Bearer {server.create_access_token('alice', 'alice@example.org', 'user', 0)}",
                  "X-Org-ID": ORG}
        self.requests = []    # every outbound httpx request made through the Ollama client

    def env(self, extra):
        p = mock.patch.dict(os.environ, extra)
        p.start()
        self.addCleanup(p.stop)

    def resolve_to(self, answers):
        import outbound_guard
        calls = []

        async def fake(host, port):
            calls.append(host)
            return list(answers)
        p = mock.patch.object(outbound_guard, "_system_resolve", fake)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def ollama_upstream(self, handler):
        import httpx

        def record(request):
            self.requests.append(request)
            return handler(request)

        def factory(timeout=120.0):
            return httpx.AsyncClient(transport=httpx.MockTransport(record), follow_redirects=False, trust_env=False)
        p = mock.patch.object(server, "_ollama_http_client", factory)
        p.start()
        self.addCleanup(p.stop)

    def no_ollama_http(self):
        def boom(*a, **k):
            raise AssertionError("an outbound Ollama request was attempted")
        p = mock.patch.object(server, "_ollama_http_client", boom)
        p.start()
        self.addCleanup(p.stop)

    def snapshot(self):
        return copy.deepcopy(self.store.cols)


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class OllamaConfigEndpointTests(_Base):
    URL = f"/api/v2/orgs/{ORG}/bots/bot-a/llm-config"
    INTERNAL = ["http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434", "http://0.0.0.0:11434",
                "http://169.254.169.254/latest/meta-data", "http://169.254.169.254", "http://bitbotpro-mongo:27017",
                "http://10.50.0.1", "http://192.168.0.32:8000", "http://[fd00:ec2::254]",
                "https://ollama.example.com@10.0.0.1", "https://ollama.example.com.evil.example",
                "file:///etc/passwd", "gopher://127.0.0.1:6379/_x", "https://ollama.example.com/api/../x"]

    def put(self, url):
        return client.put(self.URL, json={"provider": "ollama", "ollama_base_url": url}, headers=self.h)

    def test_ollama_disabled_by_default_in_production(self):
        for url in ["https://ollama.example.com"] + self.INTERNAL:
            with self.subTest(url=url):
                before = self.snapshot()
                r = self.put(url)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.put("").status_code, 400)
        r = client.get(self.URL, headers=self.h)
        self.assertFalse(r.json()["ollama_enabled"])

    def test_only_allowlisted_origin_is_accepted(self):
        self.env(ALLOW)
        for url in self.INTERNAL:
            with self.subTest(url=url):
                before = self.snapshot()
                self.assertEqual(self.put(url).status_code, 400)
                self.assertEqual(self.snapshot(), before)
        r = self.put("https://OLLAMA.example.com/")
        self.assertEqual(r.status_code, 200, r.text)
        stored = next(b for b in self.store.col("bots") if b["id"] == "bot-a")["llm_config"]
        self.assertEqual(stored["ollama_base_url"], "https://ollama.example.com")
        g = client.get(self.URL, headers=self.h).json()
        self.assertTrue(g["ollama_enabled"])
        self.assertTrue(g["llm_configured"])

    def test_other_providers_unaffected(self):
        r = client.put(self.URL, json={"provider": "anthropic", "anthropic_api_key": "sk-ant-UNITTEST-000000"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)

    def test_llm_test_endpoint_never_echoes_raw_exceptions(self):
        import bot_routes

        async def boom(*a, **k):
            raise RuntimeError(INTERNAL_SECRET)

        async def cfg(*a, **k):
            return {}
        with mock.patch.dict(bot_routes._llm_refs, {"call_claude": boom, "resolve_bot_config": cfg}):
            r = client.post(f"/api/v2/orgs/{ORG}/bots/bot-a/llm-config/test", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["success"])
        self.assertNotIn(INTERNAL_SECRET, r.text)


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class OllamaCallTests(_Base):
    def call(self, base_url="https://ollama.example.com"):
        return asyncio.run(server._call_ollama("s", "hi", "sys", "bot-a", "llama3.1", base_url))

    def assert_blocked(self, base_url="https://ollama.example.com"):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.call(base_url)
        self.assertEqual(ctx.exception.status_code, 400)
        return ctx.exception

    def test_disabled_without_allowlist_and_no_request(self):
        self.no_ollama_http()
        for url in ("https://ollama.example.com", "", "http://localhost:11434"):
            with self.subTest(url=url):
                self.assert_blocked(url)

    def test_stored_internal_urls_are_blocked_before_any_request(self):
        self.env(ALLOW)
        self.no_ollama_http()
        for url in ("http://127.0.0.1:11434", "http://bitbotpro-mongo:27017", "http://169.254.169.254"):
            with self.subTest(url=url):
                self.assert_blocked(url)

    def test_allowlisted_host_resolving_internally_is_blocked(self):
        self.env(ALLOW)
        self.no_ollama_http()
        for answers in (["127.0.0.1"], ["10.0.0.7"], ["169.254.169.254"], ["203.0.114.7", "172.20.0.2"], ["::1"]):
            with self.subTest(answers=answers):
                self.resolve_to(answers)
                self.assert_blocked()

    def test_request_is_pinned_to_the_checked_address(self):
        import httpx
        self.env(ALLOW)
        calls = self.resolve_to(["203.0.114.7"])
        self.ollama_upstream(lambda req: httpx.Response(200, json={"message": {"content": "pong"}}))
        text, _, _ = self.call()
        self.assertEqual(text, "pong")
        self.assertEqual(len(self.requests), 1)
        req = self.requests[0]
        self.assertEqual((req.url.scheme, req.url.host, req.url.path), ("https", "203.0.114.7", "/api/chat"))
        self.assertIn(req.url.port, (None, 443))   # httpx normalises the default port away
        self.assertEqual(req.headers["host"], "ollama.example.com")
        self.assertEqual(req.extensions.get("sni_hostname"), "ollama.example.com")
        self.assertEqual(calls, ["ollama.example.com"])   # resolved exactly once

    def test_redirects_are_not_followed(self):
        import httpx
        from fastapi import HTTPException
        self.env(ALLOW)
        self.resolve_to(["203.0.114.7"])
        self.ollama_upstream(lambda req: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}))
        with self.assertRaises(HTTPException) as ctx:
            self.call()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("169.254", ctx.exception.detail)

    def test_upstream_error_content_is_never_returned(self):
        import httpx
        from fastapi import HTTPException
        self.env(ALLOW)
        self.resolve_to(["203.0.114.7"])
        for resp in (httpx.Response(500, text=INTERNAL_SECRET), httpx.Response(404, text=INTERNAL_SECRET),
                     httpx.Response(200, text=INTERNAL_SECRET), httpx.Response(200, json=[INTERNAL_SECRET])):
            with self.subTest(status=resp.status_code):
                self.ollama_upstream(lambda req, resp=resp: resp)
                with self.assertRaises(HTTPException) as ctx:
                    self.call()
                self.assertNotIn(INTERNAL_SECRET, ctx.exception.detail)

    def test_network_error_details_are_not_returned(self):
        import httpx
        from fastapi import HTTPException
        self.env(ALLOW)
        self.resolve_to(["203.0.114.7"])

        def fail(req):
            raise httpx.ConnectError(f"cannot connect: {INTERNAL_SECRET}")
        self.ollama_upstream(fail)
        with self.assertRaises(HTTPException) as ctx:
            self.call()
        self.assertEqual(ctx.exception.detail, "Could not reach the Ollama server.")

    def stream(self, base_url="https://ollama.example.com"):
        async def go():
            gen, _ = await server._stream_ollama("s", "hi", "sys", "bot-a", "llama3.1", base_url)
            return "".join([c async for c in gen])
        return asyncio.run(go())

    def test_stream_is_checked_before_streaming(self):
        from fastapi import HTTPException
        self.env(ALLOW)
        self.no_ollama_http()
        self.resolve_to(["10.0.0.7"])
        with self.assertRaises(HTTPException) as ctx:
            self.stream()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_stream_errors_do_not_leak_content(self):
        import httpx
        self.env(ALLOW)
        self.resolve_to(["203.0.114.7"])
        self.ollama_upstream(lambda req: httpx.Response(500, text=INTERNAL_SECRET))
        out = self.stream()
        self.assertNotIn(INTERNAL_SECRET, out)
        self.assertIn("HTTP 500", out)

        def fail(req):
            raise httpx.ConnectError(INTERNAL_SECRET)
        self.ollama_upstream(fail)
        out = self.stream()
        self.assertNotIn(INTERNAL_SECRET, out)

    def test_stream_redirect_is_not_followed(self):
        import httpx
        self.env(ALLOW)
        self.resolve_to(["203.0.114.7"])
        self.ollama_upstream(lambda req: httpx.Response(307, headers={"Location": "http://127.0.0.1:27017/"}))
        out = self.stream()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("HTTP 307", out)


class _FakeAiohttp:
    """Records aiohttp GETs made by the account test endpoint."""
    calls = []

    class _Resp:
        status = 200

        async def json(self):
            return {"user": {"name": "n", "email": "e", "role": "admin"}}

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class ClientSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, **kw):
            _FakeAiohttp.calls.append((url, kw))
            return _FakeAiohttp._Resp()


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class ZendeskAccountTests(_Base):
    BAD = ["evil.example#", "evil.example#.zendesk.com", "acme.zendesk.com.evil.example", "evil.example/x",
           "acme:8443", "user@evil.example", "http://acme.zendesk.com", "acme.zendesk.com/../x",
           "acme%2eevil", "ac me", "аcme", "-acme"]

    def setUp(self):
        super().setUp()
        _FakeAiohttp.calls = []
        p = mock.patch("aiohttp.ClientSession", _FakeAiohttp.ClientSession)
        p.start()
        self.addCleanup(p.stop)
        self.base = f"/api/v2/orgs/{ORG}/accounts"

    def test_create_rejects_malicious_subdomains(self):
        for sub in self.BAD:
            with self.subTest(sub=sub):
                before = self.snapshot()
                r = client.post(self.base, headers=self.h, json={
                    "provider": "zendesk", "name": "Z", "credentials": {"subdomain": sub, "oauth_access_token": FAKE_ZD_TOKEN}})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(self.snapshot(), before)

    def test_create_normalises_valid_subdomain(self):
        r = client.post(self.base, headers=self.h, json={
            "provider": "zendesk", "name": "Z", "credentials": {"subdomain": "https://Acme.zendesk.com/"}})
        self.assertEqual(r.status_code, 200, r.text)
        acc = next(a for a in self.store.col("connected_accounts") if a["id"] == r.json()["id"])
        self.assertEqual(acc["credentials"]["subdomain"], "acme")

    def test_update_rejects_malicious_subdomain(self):
        self.store.col("connected_accounts").append({"id": "z1", "org_id": ORG, "provider": "zendesk", "name": "Z",
                                                     "status": "connected", "credentials": {"subdomain": "acme"}})
        for sub in self.BAD:
            with self.subTest(sub=sub):
                before = self.snapshot()
                r = client.put(f"{self.base}/z1", headers=self.h, json={"credentials": {"subdomain": sub}})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(self.snapshot(), before)

    def test_stored_malicious_subdomain_never_receives_the_token(self):
        for sub in ("evil.example#", "evil.example/", "acme.zendesk.com.evil.example"):
            with self.subTest(sub=sub):
                self.store.col("connected_accounts")[:] = [{
                    "id": "z1", "org_id": ORG, "provider": "zendesk", "name": "Z", "status": "connected",
                    "credentials": {"subdomain": sub, "auth_type": "oauth", "oauth_access_token": FAKE_ZD_TOKEN}}]
                r = client.post(f"{self.base}/z1/test", headers=self.h)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(_FakeAiohttp.calls, [])

    def test_valid_subdomain_targets_zendesk_without_redirects(self):
        self.store.col("connected_accounts").append({
            "id": "z1", "org_id": ORG, "provider": "zendesk", "name": "Z", "status": "connected",
            "credentials": {"subdomain": "acme", "auth_type": "oauth", "oauth_access_token": FAKE_ZD_TOKEN}})
        r = client.post(f"{self.base}/z1/test", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(_FakeAiohttp.calls), 1)
        url, kw = _FakeAiohttp.calls[0]
        self.assertEqual(url, "https://acme.zendesk.com/api/v2/users/me.json")
        self.assertIs(kw.get("allow_redirects"), False)
        self.assertNotIn(FAKE_ZD_TOKEN, r.text)


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class ZendeskRoutesHelperTests(unittest.TestCase):
    def setUp(self):
        import zendesk_routes
        self.z = zendesk_routes
        self.constructed = []
        test = self

        class NoClient:
            def __init__(self, *a, **k):
                test.constructed.append(k)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, *a, **k):
                raise AssertionError("request sent")

            async def post(self, *a, **k):
                raise AssertionError("request sent")
        p = mock.patch.object(zendesk_routes.httpx, "AsyncClient", NoClient)
        p.start()
        self.addCleanup(p.stop)

    def test_normalize_subdomain_rejects_with_400(self):
        from fastapi import HTTPException
        self.assertEqual(self.z._normalize_subdomain("https://midnight-gd.zendesk.com/"), "midnight-gd")
        for sub in ("evil.example#", "evil.example/", "a.b", ""):
            with self.subTest(sub=sub):
                with self.assertRaises(HTTPException) as ctx:
                    self.z._normalize_subdomain(sub)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_api_request_with_malicious_stored_subdomain_sends_nothing(self):
        from fastapi import HTTPException
        for sub in ("evil.example#", "evil.example/x?", "acme.zendesk.com.evil.example", "", None):
            with self.subTest(sub=sub):
                cfg = {"subdomain": sub, "auth_type": "api_token", "email": "a@example.org", "api_token": FAKE_ZD_TOKEN}
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(self.z._zendesk_request(cfg, "GET", "/users/me.json"))
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.constructed, [])

    def test_zd_url_builds_only_zendesk_hosts(self):
        self.assertEqual(self.z._zd_url("acme", "/oauth/tokens"), "https://acme.zendesk.com/oauth/tokens")


if __name__ == "__main__":
    unittest.main()
