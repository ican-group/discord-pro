"""In-process FastAPI tests for spec 004 (APP_MODE=api, KB 503, CORS, health,
Discord). Needs the backend dependencies (fastapi, motor, ...); skipped otherwise.
Never touches a real database: MONGO_URL points at a closed local port and
startup events are not run.
"""
import asyncio
import importlib.util
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.unit._support import BACKEND_DIR

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "motor", "httpx", "bcrypt", "jwt"))

TEST_ENV = {
    "APP_MODE": "api",
    "APP_ENV": "production",
    "CORS_ORIGINS": "https://bitbotpro.io",
    "MONGO_URL": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=300&connectTimeoutMS=300",
    "DB_NAME": "unit_test_never_used",
    "JWT_SECRET": "unit-test-jwt-secret-not-real-0123456789",
    "CODE_HMAC_KEY": "unit-test-code-hmac-key-not-real-abcdefghijklmnop",
    "PUBLIC_APP_URL": "https://bitbotpro.io",
    "PUBLIC_API_URL": "https://api.bitbotpro.io",
    "AUTH_RESPONSE_FLOOR_MS": "0",          # timing floor tested explicitly in AccountEnumerationTests
    "ENCRYPTION_KEY": "dW5pdC10ZXN0LWZlcm5ldC1rZXktbm90LXJlYWwtMDA=",  # synthetic Fernet key
}
# Synthetic password that satisfies the user password policy (spec 008, R-N6.1).
STRONG_PW = "Vq3!tz-Kp9#mW2rL"

server = None
client = None


def setUpModule():
    global server, client
    if not HAVE_DEPS:
        return
    for k in ("CLAUDE_API_KEY", "EMERGENT_LLM_KEY", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
    sys.modules.pop("server", None)
    import server as _server  # noqa: E402
    from fastapi.testclient import TestClient
    server = _server
    client = TestClient(server.app)  # not used as a context manager: startup does not run


def tearDownModule():
    """Close the Motor client that ``import server`` created (server.py's module-level
    ``mongo_client``). Runs once, after every test in this module. The app's own
    shutdown handler never runs here because TestClient is not used as a context
    manager. Safe when dependencies are missing or the import failed."""
    global server, client
    mongo_client = getattr(server, "mongo_client", None)
    if mongo_client is not None:
        mongo_client.close()
    server = None
    client = None


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class KnowledgeBaseDisabledTests(unittest.TestCase):
    KB_REQUESTS = [
        ("GET", "/api/knowledge/sources"),
        ("GET", "/api/knowledge/export"),
        ("POST", "/api/knowledge/import"),
        ("POST", "/api/knowledge/share"),
        ("POST", "/api/knowledge/sources/faq"),
        ("GET", "/api/knowledge/sources/abc/content"),
        ("GET", "/api/knowledge/sources/group/g1/content"),
        ("POST", "/api/knowledge/sources/url"),
        ("POST", "/api/knowledge/sources/url/rescrape"),
        ("POST", "/api/knowledge/sources/url/crawl"),
        ("POST", "/api/knowledge/sources/url/crawl-stream"),
        ("POST", "/api/knowledge/sources/bulk-rescrape"),
        ("POST", "/api/knowledge/sources/upload"),
        ("POST", "/api/knowledge/sources/bulk-delete"),
        ("DELETE", "/api/knowledge/sources/abc"),
        ("DELETE", "/api/knowledge/sources/group/g1"),
        ("PATCH", "/api/knowledge/sources/abc/toggle"),
        ("PATCH", "/api/knowledge/sources/group/g1/priority"),
        ("PUT", "/api/knowledge/sources/abc"),
        ("GET", "/api/knowledge/categories"),
        ("POST", "/api/knowledge/sources/github/scan"),
        ("POST", "/api/knowledge/sources/github/import-stream"),
        ("GET", "/api/knowledge/does-not-exist"),
    ]

    def test_every_kb_route_returns_503_without_auth_or_db(self):
        for method, path in self.KB_REQUESTS:
            with self.subTest(method=method, path=path):
                r = client.request(method, path)
                self.assertEqual(r.status_code, 503)
                self.assertIn("PostgreSQL/pgvector", r.json()["detail"])

    def test_registered_kb_routes_all_covered(self):
        kb_routes = [r.path for r in server.app.routes if "/knowledge" in getattr(r, "path", "")]
        self.assertTrue(kb_routes)
        for p in kb_routes:
            self.assertTrue(p.startswith("/api/knowledge"), p)

    def test_kb_503_keeps_cors_headers_for_allowed_origin(self):
        r = client.get("/api/knowledge/sources", headers={"Origin": "https://bitbotpro.io"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "https://bitbotpro.io")

    def test_search_knowledge_disabled(self):
        self.assertEqual(asyncio.run(server.search_knowledge("q", "inst")), "__NO_KB__")


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class UnaffectedRouteTests(unittest.TestCase):
    def test_root(self):
        r = client.get("/api/")
        self.assertEqual(r.status_code, 200)

    def test_auth_me_requires_auth(self):
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_health_reports_unavailable_without_db_and_leaks_nothing(self):
        r = client.get("/api/health")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"status": "unavailable"})

    def test_health_ok_when_ping_succeeds(self):
        async def ok(*a, **k):
            return {"ok": 1}
        with mock.patch.object(server.db, "command", ok):
            r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class CorsRuntimeTests(unittest.TestCase):
    def _preflight(self, origin):
        return client.options("/api/auth/login", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST"})

    def test_allowed_origin(self):
        r = self._preflight("https://bitbotpro.io")
        self.assertEqual(r.headers.get("access-control-allow-origin"), "https://bitbotpro.io")
        self.assertEqual(r.headers.get("access-control-allow-credentials"), "true")

    def test_disallowed_origins(self):
        for origin in ("https://evil.example", "http://bitbotpro.io", "https://www.bitbotpro.io",
                       "https://bitbotpro.io.evil.example", "null"):
            with self.subTest(origin=origin):
                self.assertIsNone(self._preflight(origin).headers.get("access-control-allow-origin"))


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class DiscordDisabledTests(unittest.TestCase):
    def test_choke_point_refuses(self):
        asyncio.run(server.start_shared_discord_bot("not-a-real-token"))
        self.assertIsNone(server._shared_discord_client)

    def test_restart_endpoint_409(self):
        server.app.dependency_overrides[server.get_instance_access] = lambda: "inst-1"
        try:
            r = client.post("/api/discord/restart")
        finally:
            server.app.dependency_overrides.clear()
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(server._shared_discord_task)

    def test_restart_endpoint_still_requires_auth(self):
        self.assertIn(client.post("/api/discord/restart").status_code, (401, 403))



@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class ProductionEmailEndpointTests(unittest.TestCase):
    """APP_ENV=production with no RESEND_API_KEY: generic 503, no state change.

    Motor returns a new collection object on every `db.users` access, so the
    collection *class* methods are patched (affects all collections in-test)."""

    def _patch_collection(self, **methods):
        from unittest.mock import AsyncMock
        from motor.motor_asyncio import AsyncIOMotorCollection
        mocks = {}
        for name, rv in methods.items():
            m = AsyncMock(return_value=rv)
            p = mock.patch.object(AsyncIOMotorCollection, name, m)
            p.start()
            self.addCleanup(p.stop)
            mocks[name] = m
        return mocks

    def test_email_endpoints_fail_safe_without_provider(self):
        m = self._patch_collection(find_one=None, insert_one=None, update_one=None, delete_one=None)
        with mock.patch.object(server.resend, "api_key", ""), mock.patch.object(server, "REGISTRATION_MODE", "open"):
            for path, body in (("/api/auth/register", {"email": "a@example.org", "password": STRONG_PW, "username": "u1"}),
                               ("/api/auth/resend-code", {"email": "a@example.org"}),
                               ("/api/auth/forgot-password", {"email": "a@example.org"})):
                with self.subTest(path=path):
                    r = client.post(path, json=body)
                    self.assertEqual(r.status_code, 503)
                    self.assertNotIn("sent", r.json()["detail"].lower())
        m["insert_one"].assert_not_called()
        m["update_one"].assert_not_called()
        m["delete_one"].assert_not_called()

    def setUp(self):
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()

    def test_register_rolls_back_when_delivery_fails(self):
        from datetime import datetime, timedelta, timezone
        def boom(params):
            raise RuntimeError("provider down")
        live = {"count": 1, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
        m = self._patch_collection(find_one=None, insert_one=None, delete_one=None,
                                   find_one_and_update=live)
        with mock.patch.object(server.resend, "api_key", "re_synthetic_key"), \
             mock.patch.object(server._email, "_send_fn", boom), \
             mock.patch.object(server, "REGISTRATION_MODE", "open"):
            r = client.post("/api/auth/register", json={"email": "a@example.org", "password": STRONG_PW, "username": "u1"})
        self.assertEqual(r.status_code, 503)
        m["insert_one"].assert_called_once()
        m["delete_one"].assert_called_once()


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class ImportTimeConfigTests(unittest.TestCase):
    def _import_with(self, **overrides):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **TEST_ENV, **overrides}
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run([sys.executable, "-c", "import server"], cwd=str(BACKEND_DIR),
                              env=env, capture_output=True, text=True, timeout=120)

    def test_valid_config_imports(self):
        self.assertEqual(self._import_with().returncode, 0)

    def test_invalid_configs_refuse_to_start(self):
        cases = [
            {"CORS_ORIGINS": "*"},
            {"CORS_ORIGINS": None},
            {"CORS_ORIGINS": "http://localhost:3000"},
            {"APP_MODE": "bogus"},
            {"APP_ENV": "staging"},
            {"CODE_HMAC_KEY": None},                         # required in production (fail closed)
            {"CODE_HMAC_KEY": "too-short"},
            {"CODE_HMAC_KEY": TEST_ENV["JWT_SECRET"]},       # must differ from JWT_SECRET
            {"PUBLIC_APP_URL": None},                        # required in production
            {"PUBLIC_API_URL": None},
            {"PUBLIC_APP_URL": "http://bitbotpro.io"},       # https only
            {"PUBLIC_API_URL": "https://api.bitbotpro.io/api"},  # origin only, no path
            {"REGISTRATION_MODE": "open"},                   # invite-only launch (spec 008, P8)
            {"REGISTRATION_MODE": "bogus"},
            {"TRUSTED_PROXY_IPS": "*"},                      # exact proxy addresses only (R-IP.2)
            {"TRUSTED_PROXY_IPS": "10.250.250.0/29"},
            {"TRUSTED_PROXY_IPS": "cloudflared"},
        ]
        for case in cases:
            with self.subTest(case=case):
                r = self._import_with(**case)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("ConfigError", r.stderr)
                self.assertNotIn(TEST_ENV["JWT_SECRET"], r.stderr)
                self.assertNotIn(TEST_ENV["CODE_HMAC_KEY"], r.stderr)


# ── Spec 008 Stage 2 ─────────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class Stage2AppTests(unittest.TestCase):
    """Docs disabled, suggestion route removed, uniform constant-time webhook checks."""

    @classmethod
    def setUpClass(cls):
        # server.py injects the Telegram/Zendesk route dependencies (including the
        # database handle) in its startup event, which these in-process tests do
        # not run. Apply the same injection directly: both functions only assign
        # module globals (no I/O, no index creation, no Discord).
        server._init_telegram()
        server._init_zendesk()

    def test_docs_and_schema_not_served_in_production(self):
        self.assertIsNone(server.app.docs_url)
        self.assertIsNone(server.app.redoc_url)
        self.assertIsNone(server.app.openapi_url)
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)

    def test_zendesk_suggestion_route_removed(self):
        paths = {getattr(r, "path", "") for r in server.app.routes}
        self.assertFalse(any(p.startswith("/api/zendesk/suggestion") for p in paths))
        self.assertEqual(client.get("/api/zendesk/suggestion/inst-1/12345").status_code, 404)

    def _post_webhooks(self, find_one_result, secret):
        from unittest.mock import AsyncMock
        from motor.motor_asyncio import AsyncIOMotorCollection
        results = {}
        with mock.patch.object(AsyncIOMotorCollection, "find_one", AsyncMock(return_value=find_one_result)):
            for name, base in (("telegram", "/api/telegram/webhook"), ("zendesk", "/api/zendesk/webhook")):
                r = client.post(f"{base}/inst-1/{secret}", json={"message": {"text": "x"}, "ticket_id": "1"})
                results[name] = (r.status_code, r.json())
        return results

    def test_unknown_instance_and_wrong_secret_are_indistinguishable(self):
        cfg = {"instance_id": "inst-1", "webhook_secret": "right-secret-value-0123456789", "is_active": False}
        unknown = self._post_webhooks(None, "right-secret-value-0123456789")
        wrong = self._post_webhooks(cfg, "wrong-secret-value-0123456789")
        # A config without a stored secret must never accept any presented secret.
        empty_stored = self._post_webhooks({**cfg, "webhook_secret": ""}, "any-presented-secret-0123")
        for name in ("telegram", "zendesk"):
            with self.subTest(webhook=name):
                self.assertEqual(unknown[name], (404, {"detail": "Not found"}))
                self.assertEqual(wrong[name], unknown[name])
                self.assertEqual(empty_stored[name], unknown[name])

    def test_correct_secret_still_accepted(self):
        cfg = {"instance_id": "inst-1", "webhook_secret": "right-secret-value-0123456789", "is_active": False}
        ok = self._post_webhooks(cfg, "right-secret-value-0123456789")
        for name in ("telegram", "zendesk"):
            with self.subTest(webhook=name):
                self.assertEqual(ok[name], (200, {"ok": True, "skipped": "inactive"}))

    def test_valid_secret_reaches_payload_processing_for_active_config(self):
        """An active config with the right secret gets past authentication and into
        payload validation (no LLM or provider call is reached)."""
        from unittest.mock import AsyncMock
        from motor.motor_asyncio import AsyncIOMotorCollection
        cfg = {"instance_id": "inst-1", "webhook_secret": "right-secret-value-0123456789", "is_active": True}
        with mock.patch.object(AsyncIOMotorCollection, "find_one", AsyncMock(return_value=cfg)):
            r = client.post("/api/zendesk/webhook/inst-1/right-secret-value-0123456789", json={})
        self.assertEqual((r.status_code, r.json()), (400, {"detail": "ticket_id required"}))

    def test_docs_still_available_in_development(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **TEST_ENV, "APP_ENV": "development"}
        code = ("import server, sys; a = server.app; "
                "sys.exit(0 if (a.docs_url, a.redoc_url, a.openapi_url) == ('/docs', '/redoc', '/openapi.json') else 3)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_DIR), env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class SidebarKeyHashingTests(unittest.TestCase):
    """Zendesk sidebar keys stored hashed and verified by hash (spec 008, R-P4.3).
    Calls the route functions directly against the in-memory FakeDB."""

    def setUp(self):
        from unittest.mock import AsyncMock
        from tests.unit._support import FakeDB
        import zendesk_routes as zr
        self.zr = zr
        self.db = FakeDB()
        self.db.zendesk_config.docs = [
            {"instance_id": "inst-1", "subdomain": "acme", "oauth_access_token": "synthetic-oauth-token-1"},
            {"instance_id": "inst-2", "subdomain": "other", "oauth_access_token": "synthetic-oauth-token-2"},
        ]
        self.current_instance = "inst-1"

        async def access(_request):
            return self.current_instance

        p1 = mock.patch.object(zr, "_db", self.db)
        p2 = mock.patch.object(zr, "_get_instance_access", AsyncMock(side_effect=access))
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def _req(self, key=None):
        from types import SimpleNamespace
        return SimpleNamespace(headers={} if key is None else {"X-Sidebar-Key": key})

    def _auth(self, key):
        return asyncio.run(self.zr._authenticate_sidebar(self._req(key)))

    def _auth_status(self, key):
        from fastapi import HTTPException
        try:
            self._auth(key)
            return 200
        except HTTPException as e:
            return e.status_code

    def _doc(self, inst):
        return next(d for d in self.db.zendesk_config.docs if d["instance_id"] == inst)

    def test_first_get_returns_key_once_and_stores_only_hash(self):
        r1 = asyncio.run(self.zr.get_sidebar_key(self._req()))
        key = r1["sidebar_api_key"]
        self.assertTrue(key.startswith("zsk_"))
        self.assertFalse(r1["key_hidden"])
        doc = self._doc("inst-1")
        self.assertNotIn("sidebar_api_key", doc)
        self.assertNotIn(key, repr(doc))
        self.assertRegex(doc["sidebar_api_key_hash"], r"^[0-9a-f]{64}$")
        r2 = asyncio.run(self.zr.get_sidebar_key(self._req()))
        self.assertTrue(r2["key_hidden"])
        self.assertNotIn(key, repr(r2))
        self.assertEqual(r2["key_hint"], "zsk_…" + key[-4:])

    def test_authentication_by_hash(self):
        key = asyncio.run(self.zr.get_sidebar_key(self._req()))["sidebar_api_key"]
        instance_id, cfg = self._auth(key)
        self.assertEqual(instance_id, "inst-1")
        self.assertEqual(self._auth_status(f"  {key}  "), 200)
        masked = asyncio.run(self.zr.get_sidebar_key(self._req()))["sidebar_api_key"]
        for bad in (masked, key + "x", key[:-1], "zsk_" + "A" * 43, "not-a-key", "", None):
            with self.subTest(bad=bad):
                self.assertEqual(self._auth_status(bad), 401)

    def test_regenerate_invalidates_old_key(self):
        old = asyncio.run(self.zr.get_sidebar_key(self._req()))["sidebar_api_key"]
        new = asyncio.run(self.zr.regenerate_sidebar_key(self._req()))["sidebar_api_key"]
        self.assertNotEqual(old, new)
        self.assertEqual(self._auth_status(old), 401)
        self.assertEqual(self._auth(new)[0], "inst-1")
        self.assertNotIn(new, repr(self._doc("inst-1")))

    def test_keys_are_per_instance(self):
        k1 = asyncio.run(self.zr.get_sidebar_key(self._req()))["sidebar_api_key"]
        self.current_instance = "inst-2"
        k2 = asyncio.run(self.zr.get_sidebar_key(self._req()))["sidebar_api_key"]
        self.assertEqual(self._auth(k1)[0], "inst-1")
        self.assertEqual(self._auth(k2)[0], "inst-2")

    def test_legacy_plaintext_key_never_used_for_auth_and_is_migrated(self):
        legacy = "zsk_" + "L" * 43
        self._doc("inst-1")["sidebar_api_key"] = legacy
        self.assertEqual(self._auth_status(legacy), 401)  # fail closed until migrated
        r = asyncio.run(self.zr.get_sidebar_key(self._req()))
        self.assertEqual(r["sidebar_api_key"], legacy)    # shown one last time
        doc = self._doc("inst-1")
        self.assertNotIn("sidebar_api_key", doc)
        self.assertEqual(self._auth(legacy)[0], "inst-1")


# ── Spec 008 Stage 3: account-takeover protection ───────────────────────────

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class AccountTakeoverFlowTests(unittest.TestCase):
    """End-to-end auth flows against an in-memory Motor stand-in (no MongoDB).

    Email delivery is stubbed and the emailed code is captured from the stub.
    """

    EMAIL = "victim@example.org"
    OLD_PW = "Vq3!tz-Kp9#mW2rL"
    NEW_PW = "Hn7$wq-Lr4@zX8cB"

    def setUp(self):
        from unittest.mock import AsyncMock
        from tests.unit._fake_motor import patch_motor
        import bcrypt
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()
        self.store = patch_motor(self)
        self.sent = []

        async def capture(to, code, *a, **k):
            self.sent.append((to, code))

        for name in ("send_verification_email", "send_reset_email"):
            p = mock.patch.object(server, name, AsyncMock(side_effect=capture))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(server, "_require_email_delivery", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self.user_id = "user-victim"
        self.store.col("users").append({
            "id": self.user_id, "email": self.EMAIL, "username": "victim", "role": "user",
            "is_verified": True, "token_version": 0,
            "hashed_password": bcrypt.hashpw(self.OLD_PW.encode(), bcrypt.gensalt(4)).decode(),
        })

    # helpers
    def user(self):
        return next(u for u in self.store.col("users") if u["id"] == self.user_id)

    def request_reset_code(self):
        r = client.post("/api/auth/forgot-password", json={"email": self.EMAIL})
        self.assertEqual(r.status_code, 200, r.text)
        return self.sent[-1][1]

    def reset(self, code, pw=None):
        return client.post("/api/auth/reset-password",
                           json={"email": self.EMAIL, "code": code, "new_password": pw or self.NEW_PW})

    def wrong(self, code):
        return f"{(int(code) + 1) % 1000000:06d}"

    # ── P2: hashed codes, single use, expiry, brute force
    def test_reset_code_stored_only_as_hash(self):
        code = self.request_reset_code()
        u = self.user()
        self.assertNotIn("reset_code", u)
        self.assertRegex(u["reset_code_hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn(code, u["reset_code_hash"])
        self.assertNotIn(code, repr({k: v for k, v in u.items() if k != "reset_code_hash"}))

    def test_correct_code_resets_once_and_revokes_sessions(self):
        old_token = server.create_access_token(self.user_id, self.EMAIL, "user", 0)
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code, 200)
        code = self.request_reset_code()
        r = self.reset(code)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.user()["token_version"], 1)
        for field in ("reset_code_hash", "reset_code_id", "reset_code_expires"):
            self.assertNotIn(field, self.user())
        # Reuse of the same code fails with the generic error.
        again = self.reset(code, "Zz9!another-Pass")
        self.assertEqual((again.status_code, again.json()["detail"]), (400, server._auth.INVALID_CODE_DETAIL))
        # Old session revoked; login with the new password issues a working token.
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code, 401)
        login = client.post("/api/auth/login", json={"email_or_username": self.EMAIL, "password": self.NEW_PW})
        self.assertEqual(login.status_code, 200, login.text)
        new_token = login.json()["token"]
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"}).status_code, 200)

    def test_brute_force_invalidates_code_after_five_failures(self):
        code = self.request_reset_code()
        for _ in range(5):
            r = self.reset(self.wrong(code))
            self.assertEqual((r.status_code, r.json()["detail"]), (400, server._auth.INVALID_CODE_DETAIL))
        self.assertNotIn("reset_code_hash", self.user())          # invalidated
        r = self.reset(code)                                       # correct code no longer works
        self.assertEqual((r.status_code, r.json()["detail"]), (400, server._auth.INVALID_CODE_DETAIL))
        self.assertEqual(self.user().get("token_version"), 0)     # password unchanged

    def test_locked_code_rejected_even_before_deletion(self):
        code = self.request_reset_code()
        # Simulate a lost race: the failure counter is at the limit but the code still exists.
        u = self.user()
        key = server._auth.attempt_key(self.user_id, u["reset_code_id"])
        from datetime import datetime, timedelta, timezone
        self.store.col("security_counters").append({
            "_id": f"reset_code:{key}", "count": 5, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)})
        r = self.reset(code)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.user().get("token_version"), 0)

    def test_new_code_replaces_old_and_resets_counter(self):
        first = self.request_reset_code()
        for _ in range(4):
            self.reset(self.wrong(first))
        second = self.request_reset_code()
        self.assertEqual(self.reset(first).status_code, 400)       # replaced
        self.assertEqual(self.reset(second).status_code, 200)      # fresh attempt budget

    def test_expired_code_rejected(self):
        code = self.request_reset_code()
        self.user()["reset_code_expires"] = "2000-01-01T00:00:00+00:00"
        self.assertEqual(self.reset(code).status_code, 400)

    def test_unknown_account_and_wrong_code_indistinguishable(self):
        code = self.request_reset_code()
        a = self.reset(self.wrong(code))
        b = client.post("/api/auth/reset-password",
                        json={"email": "nobody@example.org", "code": code, "new_password": self.NEW_PW})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))

    def test_legacy_plaintext_code_never_accepted(self):
        self.user()["reset_code"] = "123456"
        self.user()["reset_code_expires"] = "2999-01-01T00:00:00+00:00"
        self.assertEqual(self.reset("123456").status_code, 400)

    def test_weak_new_password_rejected_without_consuming_code(self):
        code = self.request_reset_code()
        r = self.reset(code, "password")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Password does not meet the requirements", r.json()["detail"])
        self.assertIn("reset_code_hash", self.user())
        self.assertEqual(self.reset(code).status_code, 200)

    def test_issuance_capped_per_address(self):
        for _ in range(3):
            self.request_reset_code()
        self.assertEqual(len(self.sent), 3)
        r = client.post("/api/auth/forgot-password", json={"email": self.EMAIL})
        self.assertEqual(r.status_code, 200)                       # same generic response
        self.assertEqual(len(self.sent), 3)                        # but nothing sent

    # ── email verification codes
    def test_verification_brute_force_and_success(self):
        self.store.col("users").clear()
        p = mock.patch.object(server, "REGISTRATION_MODE", "open")
        p.start()
        self.addCleanup(p.stop)
        r = client.post("/api/auth/register", json={"email": "new@example.org", "password": self.OLD_PW,
                                                    "username": "newbie"})
        self.assertEqual(r.status_code, 200, r.text)
        code = self.sent[-1][1]
        u = next(x for x in self.store.col("users") if x["email"] == "new@example.org")
        self.assertNotIn("verification_code", u)
        for _ in range(5):
            bad = client.post("/api/auth/verify", json={"email": "new@example.org", "code": self.wrong(code)})
            self.assertEqual(bad.status_code, 400)
        self.assertEqual(client.post("/api/auth/verify", json={"email": "new@example.org", "code": code}).status_code, 400)
        # A fresh code (resend) verifies.
        client.post("/api/auth/resend-code", json={"email": "new@example.org"})
        fresh = self.sent[-1][1]
        ok = client.post("/api/auth/verify", json={"email": "new@example.org", "code": fresh})
        self.assertEqual(ok.status_code, 200, ok.text)
        u = next(x for x in self.store.col("users") if x["email"] == "new@example.org")
        self.assertTrue(u["is_verified"])
        self.assertNotIn("verification_code_hash", u)

    def test_register_rejects_weak_password(self):
        p = mock.patch.object(server, "REGISTRATION_MODE", "open")
        p.start()
        self.addCleanup(p.stop)
        r = client.post("/api/auth/register", json={"email": "w@example.org", "password": "x" * 12, "username": "w"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.sent, [])

    # ── login lockout (per account) and per-IP limits
    def test_login_lockout_after_five_failures_uniform_for_unknown_accounts(self):
        results = {}
        for ident in (self.EMAIL, "ghost@example.org"):
            for _ in range(5):
                r = client.post("/api/auth/login", json={"email_or_username": ident, "password": "Wrong-pass-123!"})
                self.assertEqual(r.status_code, 401)
            for limiter in server._IP_LIMITERS.values():
                limiter.clear()  # isolate the per-account lock from the per-IP limit
            results[ident] = client.post("/api/auth/login",
                                         json={"email_or_username": ident, "password": self.OLD_PW})
        a, b = results[self.EMAIL], results["ghost@example.org"]
        self.assertEqual(a.status_code, 429)                      # even the correct password
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        self.assertIn("retry-after", a.headers)

    def test_successful_reset_lifts_login_lock(self):
        for _ in range(5):
            client.post("/api/auth/login", json={"email_or_username": self.EMAIL, "password": "Wrong-pass-123!"})
        code = self.request_reset_code()
        self.assertEqual(self.reset(code).status_code, 200)
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()
        r = client.post("/api/auth/login", json={"email_or_username": self.EMAIL, "password": self.NEW_PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_per_ip_limit_on_login(self):
        codes = [client.post("/api/auth/login", json={"email_or_username": f"u{i}@example.org",
                                                       "password": "Wrong-pass-123!"}).status_code
                 for i in range(11)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)

    def test_per_ip_limit_on_code_issuance(self):
        codes = [client.post("/api/auth/forgot-password", json={"email": f"x{i}@example.org"}).status_code
                 for i in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])

    def test_spoofed_forwarding_headers_do_not_bypass_ip_limit(self):
        codes = []
        for i in range(11):
            r = client.post("/api/auth/login", json={"email_or_username": f"s{i}@example.org", "password": "Wrong-1!xyzab"},
                            headers={"X-Forwarded-For": f"203.0.113.{i}", "CF-Connecting-IP": f"198.51.100.{i}"})
            codes.append(r.status_code)
        self.assertEqual(codes[10], 429)

    # ── N5: change password revokes other sessions
    def test_change_password_revokes_old_tokens_and_returns_new_one(self):
        old_token = server.create_access_token(self.user_id, self.EMAIL, "user", 0)
        r = client.post("/api/auth/change-password", headers={"Authorization": f"Bearer {old_token}"},
                        json={"current_password": self.OLD_PW, "new_password": self.NEW_PW})
        self.assertEqual(r.status_code, 200, r.text)
        new_token = r.json()["token"]
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code, 401)
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"}).status_code, 200)

    def test_change_password_enforces_policy(self):
        token = server.create_access_token(self.user_id, self.EMAIL, "user", 0)
        r = client.post("/api/auth/change-password", headers={"Authorization": f"Bearer {token}"},
                        json={"current_password": self.OLD_PW, "new_password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.user()["token_version"], 0)

    def test_token_for_deleted_user_rejected(self):
        token = server.create_access_token("no-such-user", "x@example.org", "user", 0)
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_me_never_returns_code_material(self):
        self.request_reset_code()
        token = server.create_access_token(self.user_id, self.EMAIL, "user", 0)
        body = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        for field in server._auth.SENSITIVE_USER_FIELDS:
            self.assertNotIn(field, body)

    # ── admin password reset authorization (cross-tenant takeover)
    def _may(self, actor, target):
        return asyncio.run(server._may_admin_reset_password(actor, target))

    def test_admin_reset_authorization(self):
        members = self.store.col("workspace_members")
        members += [
            {"workspace_id": "ws-a", "user_id": "owner-a", "role": "owner"},
            {"workspace_id": "ws-a", "user_id": "admin-a", "role": "admin"},
            {"workspace_id": "ws-a", "user_id": "member-a", "role": "member"},
            {"workspace_id": "ws-b", "user_id": "owner-b", "role": "owner"},
            {"workspace_id": "ws-b", "user_id": "member-b", "role": "member"},
        ]
        def u(uid, role="user"):
            return {"id": uid, "role": role}
        self.assertTrue(self._may(u("owner-a"), u("member-a")))
        self.assertTrue(self._may(u("owner-a"), u("admin-a")))
        self.assertTrue(self._may(u("admin-a"), u("member-a")))
        self.assertFalse(self._may(u("admin-a"), u("owner-a")))        # never the owner
        self.assertFalse(self._may(u("member-a"), u("admin-a")))       # members cannot
        self.assertFalse(self._may(u("owner-b"), u("member-a")))       # other tenant
        self.assertFalse(self._may(u("owner-a"), u("member-b")))
        self.assertFalse(self._may(u("owner-a"), u("root", "superadmin")))
        self.assertFalse(self._may(u("owner-a"), {"id": "p", "role": "user", "platform_role": "superadmin"}))
        self.assertTrue(self._may(u("root", "superadmin"), u("member-b")))


# ── Spec 008 Stage 3b (N8): authorization scoping ─────────────────────────────

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class AuthorizationScopingTests(unittest.TestCase):
    """`require_admin_or_ws_admin` admits every verified user (each owns a
    workspace); these tests prove each endpoint applies its own scoping."""

    def setUp(self):
        from unittest.mock import AsyncMock
        from tests.unit._fake_motor import patch_motor
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()
        self.store = st = patch_motor(self)
        for name in ("send_invitation_email",):
            p = mock.patch.object(server, name, AsyncMock(return_value=None))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(server, "_require_email_delivery", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        users = {"root": "superadmin", "owner-a": "user", "admin-a": "user", "member-a": "user",
                 "owner-b": "user", "member-b": "user", "multi": "user", "loner": "user"}
        for uid, role in users.items():
            st.col("users").append({"id": uid, "email": f"{uid}@example.org", "username": uid, "role": role,
                                    "is_verified": True, "token_version": 0, "hashed_password": "x",
                                    "org_ids": ["org-a", "org-b"] if uid == "member-a" else [],
                                    "bot_ids": ["bot-b"] if uid == "member-a" else []})
        st.col("workspaces").extend([{"id": "ws-a", "owner_id": "owner-a"}, {"id": "ws-b", "owner_id": "owner-b"}])
        st.col("workspace_members").extend([
            {"workspace_id": "ws-a", "user_id": "owner-a", "role": "owner"},
            {"workspace_id": "ws-a", "user_id": "admin-a", "role": "admin"},
            {"workspace_id": "ws-a", "user_id": "member-a", "role": "member"},
            {"workspace_id": "ws-a", "user_id": "multi", "role": "member"},
            {"workspace_id": "ws-b", "user_id": "owner-b", "role": "owner"},
            {"workspace_id": "ws-b", "user_id": "member-b", "role": "member"},
            {"workspace_id": "ws-b", "user_id": "multi", "role": "member"},
        ])
        st.col("organisations").extend([{"id": "org-a", "name": "Org A", "workspace_id": "ws-a", "member_ids": []},
                                        {"id": "org-b", "name": "Org B", "workspace_id": "ws-b", "member_ids": []}])
        st.col("bots").extend([{"id": "bot-a", "name": "Bot A", "org_id": "org-a"},
                               {"id": "bot-b", "name": "Bot B", "org_id": "org-b"}])
        st.col("api_keys").extend([{"id": "key-a", "instance_id": "bot-a", "is_active": True},
                                   {"id": "key-b", "instance_id": "bot-b", "is_active": True}])
        st.col("invitations").extend([
            {"token": "tok-a", "email": "x@example.org", "workspace_id": "ws-a", "status": "pending", "created_at": "2026-11-17T00:00:00+00:00"},
            {"token": "tok-b", "email": "y@example.org", "workspace_id": "ws-b", "status": "pending", "created_at": "2026-11-17T00:00:00+00:00"},
        ])
        self.secret = "platform-discord-client-secret-SYNTH"
        st.col("discord_app_config").append({"_id": "discord_app", "client_id": "1234567890",
                                             "client_secret": self.secret, "bot_token": "platform-bot-token-SYNTH",
                                             "redirect_uri": "https://api.example.org/api/discord/callback"})
        st.col("discord_config").append({"instance_id": "bot-b", "guild_id": "g-b", "guild_name": "Tenant B guild"})

    def h(self, uid):
        role = "superadmin" if uid == "root" else "user"
        return {"Authorization": f"Bearer {server.create_access_token(uid, f'{uid}@example.org', role, 0)}"}

    def doc(self, col, **flt):
        return next((d for d in self.store.col(col) if all(d.get(k) == v for k, v in flt.items())), None)

    # ── platform credentials and destructive operations
    def test_platform_discord_operations_are_superadmin_only(self):
        for uid in ("owner-a", "admin-a", "loner"):
            with self.subTest(uid=uid):
                self.assertIn(client.put("/api/discord/app-config", headers=self.h(uid),
                                         json={"client_secret": "attacker"}).status_code, (401, 403))
                self.assertIn(client.delete("/api/discord/app-config", headers=self.h(uid),
                                            params={"clear_connections": "true"}).status_code, (401, 403))
                self.assertIn(client.get("/api/discord/app-config/connections", headers=self.h(uid)).status_code, (401, 403))
                self.assertIn(client.post("/api/discord/purge-all-connections", headers=self.h(uid)).status_code, (401, 403))
        self.assertEqual(self.doc("discord_app_config", _id="discord_app")["client_secret"], self.secret)
        self.assertEqual(self.doc("discord_config", instance_id="bot-b")["guild_id"], "g-b")
        # Superadmin keeps the platform functionality.
        self.assertEqual(client.get("/api/discord/app-config/connections", headers=self.h("root")).status_code, 200)
        self.assertEqual(client.put("/api/discord/app-config", headers=self.h("root"),
                                    json={"redirect_uri": "https://api.example.org/api/discord/callback"}).status_code, 200)
        self.assertEqual(client.post("/api/discord/purge-all-connections", headers=self.h("root")).status_code, 200)

    def test_app_config_read_never_reveals_secret_fragments_to_non_superadmins(self):
        r = client.get("/api/discord/app-config", headers=self.h("owner-a"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["has_client_secret"] and body["has_bot_token"])
        for fragment in (self.secret, self.secret[-4:], "SYNTH"):
            self.assertNotIn(fragment, r.text)
        root = client.get("/api/discord/app-config", headers=self.h("root")).json()
        self.assertEqual(root["client_secret"], "***" + self.secret[-4:])

    # ── invitations
    def invite(self, uid, **body):
        payload = {"email": body.pop("email"), "workspace_role": "member", **body}
        return client.post("/api/admin/users/invite", headers=self.h(uid), json=payload)

    def test_invitations_cannot_target_foreign_workspaces_or_resources(self):
        cases = [
            ("owner-b", dict(email="i1@example.org", workspace_id="ws-a"), 403),
            ("owner-a", dict(email="i2@example.org"), 400),                                        # workspace required
            ("owner-a", dict(email="i3@example.org", workspace_id="ws-a", org_ids=["org-b"]), 400),
            ("owner-a", dict(email="i4@example.org", workspace_id="ws-a", bot_ids=["bot-b"]), 400),
            ("owner-a", dict(email="i5@example.org", workspace_id="ws-a", org_ids=["no-such-org"]), 400),
            ("owner-a", dict(email="i6@example.org", workspace_id="ws-a", bot_ids=["no-such-bot"]), 400),
            ("owner-a", dict(email="i7@example.org", workspace_id="no-such-ws"), 403),
            ("owner-a", dict(email="i8@example.org", workspace_id="ws-a", instance_id="legacy-1"), 403),
            ("member-a", dict(email="i9@example.org", workspace_id="ws-a"), 403),                  # not an admin
        ]
        for uid, body, expected in cases:
            with self.subTest(uid=uid, body=body):
                self.assertEqual(self.invite(uid, **body).status_code, expected)
        self.assertEqual(len(self.store.col("invitations")), 2)                                    # nothing created

    def test_invitations_cannot_escalate_roles(self):
        self.assertEqual(self.invite("admin-a", email="e1@example.org", workspace_id="ws-a",
                                     workspace_role="admin").status_code, 403)                     # admins cannot mint admins
        self.assertEqual(self.invite("owner-a", email="e2@example.org", workspace_id="ws-a",
                                     workspace_role="owner").status_code, 400)
        r = self.invite("owner-a", email="e3@example.org", workspace_id="ws-a", role="superadmin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.doc("invitations", email="e3@example.org")["role"], "user")         # platform role never granted

    def test_authorized_invitations_still_work(self):
        r = self.invite("owner-a", email="ok1@example.org", workspace_id="ws-a", workspace_role="admin",
                        org_ids=["org-a"], bot_ids=["bot-a"])
        self.assertEqual(r.status_code, 200, r.text)
        inv = self.doc("invitations", email="ok1@example.org")
        self.assertEqual((inv["workspace_id"], inv["workspace_role"], inv["org_ids"], inv["bot_ids"]),
                         ("ws-a", "admin", ["org-a"], ["bot-a"]))
        self.assertEqual(self.invite("admin-a", email="ok2@example.org", workspace_id="ws-a").status_code, 200)
        self.assertEqual(self.invite("root", email="ok3@example.org", workspace_id="ws-b",
                                     org_ids=["org-b"]).status_code, 200)

    def test_invitation_listing_and_revocation_scoped(self):
        listed = client.get("/api/admin/users/invitations", headers=self.h("owner-a")).json()["invitations"]
        self.assertEqual({i["token"] for i in listed}, {"tok-a"})
        self.assertEqual(client.delete("/api/admin/users/invitations/tok-a", headers=self.h("owner-b")).status_code, 404)
        self.assertEqual(self.doc("invitations", token="tok-a")["status"], "pending")
        self.assertEqual(client.delete("/api/admin/users/invitations/tok-a", headers=self.h("owner-a")).status_code, 200)
        every = client.get("/api/admin/users/invitations", headers=self.h("root")).json()["invitations"]
        self.assertEqual({i["token"] for i in every}, {"tok-a", "tok-b"})

    # ── API keys
    def test_api_keys_scoped_to_the_callers_bots(self):
        self.assertEqual(client.post("/api/admin/api-keys", headers=self.h("owner-b"),
                                     json={"instance_id": "bot-a"}).status_code, 403)
        self.assertEqual(client.post("/api/admin/api-keys", headers=self.h("owner-a"),
                                     json={"instance_id": "no-such-bot"}).status_code, 404)
        for method, path in (("delete", "/api/admin/api-keys/key-a"), ("patch", "/api/admin/api-keys/key-a/toggle"),
                             ("delete", "/api/admin/api-keys/no-such-key")):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(client, method)(path, headers=self.h("owner-b")).status_code, 404)
        key_a = self.doc("api_keys", id="key-a")
        self.assertTrue(key_a is not None and key_a["is_active"])
        created = client.post("/api/admin/api-keys", headers=self.h("owner-a"), json={"instance_id": "bot-a"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(client.patch("/api/admin/api-keys/key-a/toggle", headers=self.h("owner-a")).json(),
                         {"is_active": False})
        self.assertEqual(client.delete("/api/admin/api-keys/key-b", headers=self.h("root")).status_code, 200)

    # ── user management (reset / delete / role)
    def reset(self, actor, target):
        return client.post(f"/api/admin/users/{target}/reset-password", headers=self.h(actor),
                           json={"new_password": "Hn7$wq-Lr4@zX8cB"})

    def test_password_reset_and_delete_scoping(self):
        self.assertEqual(self.reset("owner-b", "member-a").status_code, 403)   # other tenant
        self.assertEqual(self.reset("owner-a", "multi").status_code, 403)      # also belongs to ws-b
        self.assertEqual(self.reset("owner-a", "loner").status_code, 403)      # no relationship
        self.assertEqual(self.reset("admin-a", "owner-a").status_code, 403)    # never the owner
        self.assertEqual(self.reset("owner-a", "root").status_code, 403)       # never a superadmin
        self.assertEqual(self.reset("owner-a", "no-such-user").status_code, 404)
        self.assertEqual(self.doc("users", id="member-a")["token_version"], 0)
        self.assertEqual(self.reset("owner-a", "member-a").status_code, 200)   # legitimate
        self.assertEqual(self.doc("users", id="member-a")["token_version"], 1)
        self.assertEqual(self.reset("root", "member-b").status_code, 200)
        self.assertEqual(client.delete("/api/admin/users/member-a", headers=self.h("owner-b")).status_code, 403)
        self.assertEqual(client.delete("/api/admin/users/multi", headers=self.h("owner-a")).status_code, 403)
        self.assertEqual(client.delete("/api/admin/users/member-a", headers=self.h("owner-a")).status_code, 200)
        self.assertIsNone(self.doc("users", id="member-a"))

    def test_role_change_cannot_pull_foreign_users_into_a_workspace(self):
        r = client.patch("/api/admin/users/member-b/role", headers=self.h("owner-a"),
                         json={"role": "admin", "workspace_id": "ws-a"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.doc("workspace_members", workspace_id="ws-a", user_id="member-b"))
        self.assertEqual(self.reset("owner-a", "member-b").status_code, 403)   # takeover chain blocked

    def test_only_owner_manages_admins_and_platform_roles(self):
        self.assertEqual(client.patch("/api/admin/users/member-a/role", headers=self.h("admin-a"),
                                      json={"role": "admin", "workspace_id": "ws-a"}).status_code, 403)
        self.assertEqual(client.patch("/api/admin/users/member-a/role", headers=self.h("owner-a"),
                                      json={"role": "superadmin"}).status_code, 403)
        ok = client.patch("/api/admin/users/member-a/role", headers=self.h("owner-a"),
                          json={"role": "admin", "workspace_id": "ws-a"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self.doc("workspace_members", workspace_id="ws-a", user_id="member-a")["role"], "admin")
        # Superadmin platform role changes revoke the target's sessions.
        self.assertEqual(client.patch("/api/admin/users/member-b/role", headers=self.h("root"),
                                      json={"role": "superadmin"}).status_code, 200)
        self.assertEqual(self.doc("users", id="member-b")["token_version"], 1)

    def test_user_listing_never_resolves_other_tenants_names(self):
        users = client.get("/api/admin/users", headers=self.h("owner-a")).json()
        member = next(u for u in users if u["id"] == "member-a")
        self.assertIn({"id": "org-a", "name": "Org A"}, member["assigned_orgs"])
        self.assertIn({"id": "org-b", "name": "org-b"}, member["assigned_orgs"])       # bare ID only
        self.assertEqual(member["assigned_bots"], [{"id": "bot-b", "name": "bot-b"}])
        self.assertNotIn("Org B", json_dumps(users))
        self.assertNotIn("member-b", {u["id"] for u in users})


# ── Spec 008 Stage 4: public chat sessions ───────────────────────────────────

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class ChatSessionSecurityTests(unittest.TestCase):
    """Public /chat endpoints against the in-memory Motor stand-in. The knowledge
    search is stubbed to "no knowledge base" so no LLM is ever called; any LLM
    call fails the test."""

    def setUp(self):
        from unittest.mock import AsyncMock
        from tests.unit._fake_motor import patch_motor
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()
        self.store = st = patch_motor(self)
        self.search = AsyncMock(return_value="__NO_KB__")
        self.llm = AsyncMock(side_effect=AssertionError("LLM must not be called"))
        for name, value in (("search_knowledge", self.search), ("call_claude", self.llm)):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        st.col("bots").extend([{"id": "bot-a", "name": "A", "org_id": "org-a"},
                               {"id": "bot-b", "name": "B", "org_id": "org-b"},
                               {"id": "bot-v2", "name": "V2", "org_id": "org-a", "legacy_instance_id": "legacy-1"}])
        st.col("conversations").append({"session_id": "discord_123456789012345678", "instance_id": "bot-a",
                                        "platform": "discord", "messages": [{"role": "user", "content": "private"}]})

    def send(self, **body):
        body.setdefault("message", "hello")
        return client.post("/api/chat/send", json=body)

    def convs(self, **flt):
        return [c for c in self.store.col("conversations") if all(c.get(k) == v for k, v in flt.items())]

    def history(self, sid, instance_id="bot-a", token=None, **kw):
        headers = {"X-Chat-Session-Token": token} if token else {}
        params = {"instance_id": instance_id} if instance_id else {}
        params.update(kw)
        return client.get(f"/api/chat/history/{sid}", headers=headers, params=params)

    # ── session issue and continuity
    def test_new_session_is_server_issued_with_token(self):
        r = self.send(instance_id="bot-a")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(server._chat.is_web_session_id(body["session_id"]))
        self.assertTrue(body["session_token"].startswith("v1."))
        self.assertEqual(len(self.convs(session_id=body["session_id"], instance_id="bot-a", platform="web")), 1)

    def test_continuity_with_valid_token(self):
        first = self.send(instance_id="bot-a").json()
        second = self.send(instance_id="bot-a", session_id=first["session_id"],
                           session_token=first["session_token"]).json()
        self.assertEqual(second["session_id"], first["session_id"])
        conv = self.convs(session_id=first["session_id"])
        self.assertEqual(len(conv), 1)
        self.assertEqual(len(conv[0]["messages"]), 4)

    # ── hijacking
    def test_client_supplied_session_id_is_not_proof_of_ownership(self):
        victim = self.send(instance_id="bot-a").json()
        other = self.send(instance_id="bot-a").json()
        for token in (None, "garbage", "v1.9999999999.AAAA", other["session_token"]):
            with self.subTest(token=token):
                r = self.send(instance_id="bot-a", session_id=victim["session_id"], session_token=token,
                              message="injected")
                self.assertNotEqual(r.json()["session_id"], victim["session_id"])
        victim_conv = self.convs(session_id=victim["session_id"])[0]
        self.assertEqual(len(victim_conv["messages"]), 2)
        self.assertNotIn("injected", repr(victim_conv))

    def test_discord_session_ids_cannot_be_written_or_read(self):
        r = self.send(instance_id="bot-a", session_id="discord_123456789012345678", message="injected")
        self.assertNotEqual(r.json()["session_id"], "discord_123456789012345678")
        discord = self.convs(session_id="discord_123456789012345678")
        self.assertEqual(len(discord), 1)
        self.assertEqual(discord[0]["messages"], [{"role": "user", "content": "private"}])
        self.assertEqual(self.history("discord_123456789012345678").status_code, 404)

    def test_token_from_one_bot_cannot_be_used_with_another(self):
        a = self.send(instance_id="bot-a").json()
        r = self.send(instance_id="bot-b", session_id=a["session_id"], session_token=a["session_token"],
                      message="cross-tenant")
        self.assertNotEqual(r.json()["session_id"], a["session_id"])
        self.assertEqual(len(self.convs(session_id=a["session_id"])[0]["messages"]), 2)
        self.assertEqual(self.history(a["session_id"], "bot-b", a["session_token"]).status_code, 404)

    # ── history
    def test_history_requires_token_for_same_bot(self):
        a = self.send(instance_id="bot-a", message="my secret question").json()
        ok = self.history(a["session_id"], "bot-a", a["session_token"])
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(set(ok.json()), {"session_id", "messages"})
        self.assertEqual(ok.json()["messages"][0]["content"], "my secret question")
        other = self.send(instance_id="bot-a").json()
        failures = [
            self.history(a["session_id"], "bot-a", None),
            self.history(a["session_id"], "bot-a", "garbage"),
            self.history(a["session_id"], "bot-a", other["session_token"]),
            self.history(a["session_id"], None, a["session_token"]),
            self.history(a["session_id"], "bot-a", None, session_token=a["session_token"]),   # never via URL
            self.history("web_" + "x" * 32, "bot-a", a["session_token"]),
        ]
        for r in failures:
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "Not found"}))

    # ── bot resolution
    def test_no_fallback_to_first_bot(self):
        for body in ({}, {"instance_id": None}, {"instance_id": "default"}, {"instance_id": "no-such-bot"},
                     {"instance_id": ""}, {"instance_id": "x" * 200}):
            with self.subTest(body=body):
                r = self.send(**body)
                self.assertEqual((r.status_code, r.json()), (404, {"detail": "Bot not found"}))
        self.search.assert_not_called()
        self.assertEqual(self.convs(platform="web"), [])

    def test_legacy_instance_id_still_resolves(self):
        self.assertEqual(self.send(instance_id="legacy-1").status_code, 200)

    # ── limits before expensive work
    def test_message_size_limits(self):
        self.assertEqual(self.send(instance_id="bot-a", message="x" * (server.MAX_CHAT_MESSAGE_CHARS + 1)).status_code, 413)
        self.assertEqual(self.send(instance_id="bot-a", message="   ").status_code, 400)
        self.search.assert_not_called()

    def test_per_ip_rate_limit_before_any_work(self):
        codes = [self.send(instance_id="bot-a").status_code for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)
        self.assertEqual(self.search.await_count, 20)

    def test_spoofed_headers_do_not_bypass_chat_limit(self):
        codes = [client.post("/api/chat/send", json={"message": "hi", "instance_id": "bot-a"},
                             headers={"X-Forwarded-For": f"203.0.113.{i}", "CF-Connecting-IP": f"198.51.100.{i}"}).status_code
                 for i in range(21)]
        self.assertEqual(codes[20], 429)

    def test_per_bot_daily_ceiling(self):
        with mock.patch.object(server, "CHAT_DAILY_LIMIT_PER_BOT", 2):
            codes = [self.send(instance_id="bot-a").status_code for _ in range(3)]
            other = self.send(instance_id="bot-b").status_code
        self.assertEqual(codes, [200, 200, 503])
        self.assertEqual(other, 200)
        self.assertEqual(self.search.await_count, 3)

    # ── streaming
    def test_stream_issues_session_token_and_rejects_hijack(self):
        victim = self.send(instance_id="bot-a").json()
        r = client.post("/api/chat/stream", json={"message": "hi", "instance_id": "bot-a",
                                                  "session_id": victim["session_id"]})
        self.assertEqual(r.status_code, 200)
        import json as _j
        first = r.text.split("\n\n")[0]
        self.assertTrue(first.startswith("event: session"))
        data = _j.loads(first.split("data: ", 1)[1])
        self.assertNotEqual(data["session_id"], victim["session_id"])
        self.assertTrue(server._chat.verify_token(server._CHAT_SESSION_KEY, data["session_token"],
                                                  data["session_id"], "bot-a"))
        self.assertEqual(len(self.convs(session_id=victim["session_id"])[0]["messages"]), 2)
        self.assertEqual(client.post("/api/chat/stream", json={"message": "hi"}).status_code, 404)

    # ── storage isolation and concurrency
    def test_save_conversation_is_scoped_by_instance_and_concurrency_safe(self):
        async def burst():
            await asyncio.gather(*(server.save_conversation("web_" + "s" * 32, f"m{i}", "r", platform="web",
                                                            instance_id="bot-a") for i in range(25)))
            await server.save_conversation("web_" + "s" * 32, "other", "r", platform="web", instance_id="bot-b")
        asyncio.run(burst())
        a = self.convs(session_id="web_" + "s" * 32, instance_id="bot-a")
        b = self.convs(session_id="web_" + "s" * 32, instance_id="bot-b")
        self.assertEqual((len(a), len(b)), (1, 1))
        self.assertEqual(len(a[0]["messages"]), 50)
        self.assertNotIn("other", repr(a[0]))

    def test_chat_content_never_logged(self):
        import logging
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Grab()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            self.send(instance_id="bot-a", message="TOP-SECRET-USER-CONTENT")
        finally:
            root.removeHandler(handler)
        self.assertFalse(any("TOP-SECRET-USER-CONTENT" in m for m in records))


# ── Spec 008 Stage 9b: public origins, embed widget, Zendesk sidebar app ───

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class PublicOriginAndEmbedTests(unittest.TestCase):
    APP = "https://bitbotpro.io"
    API = "https://api.bitbotpro.io"

    def setUp(self):
        from tests.unit._fake_motor import patch_motor
        self.store = patch_motor(self)

    def embed(self, **params):
        r = client.get("/api/embed.js", params=params)
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_embed_script_uses_app_origin_for_widget_and_api_origin_for_api(self):
        js = self.embed(instance="bot-a")
        self.assertIn(f'var APP_ORIGIN = "{self.APP}";', js)
        self.assertIn(f'var API_ORIGIN = "{self.API}";', js)
        self.assertIn('APP_ORIGIN + "/widget?instance="', js)
        self.assertIn('API_ORIGIN + "/api/embed/config/"', js)
        self.assertIn('API_ORIGIN + "/api/bot-logo/"', js)
        self.assertNotIn("testserver", js)                     # never the request host

    def test_embed_script_query_values_cannot_inject_code(self):
        payload = '"; alert(document.cookie); var x="</script><script>alert(1)</script>'
        js = self.embed(instance=payload, mode=payload)
        import json as _j
        import re as _re
        m = _re.search(r'var INSTANCE = attr\("instance", (".*?(?<!\\)")\);', js)
        self.assertIsNotNone(m)
        self.assertEqual(_j.loads(m.group(1)), payload)          # stays one string literal
        self.assertIsNone(_re.search(r'(?<!\\)"; alert\(document\.cookie\)', js))
        self.assertNotIn("</script>", js)
        self.assertIn('var MODE = attr("mode", "popup");', js)   # unknown mode coerced
        # A newline must not be able to end a JS comment and start code.
        js2 = self.embed(instance="x\nalert(document.domain)//")
        self.assertNotIn("\nalert(document.domain)", js2)
        self.assertEqual(js2.count("alert(document.domain)"), 1)  # only inside the INSTANCE literal

    def test_embed_iframes_are_sandboxed(self):
        js = self.embed(instance="bot-a")
        self.assertIn('"allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"', js)
        self.assertIn("var harden = function(el)", js)
        self.assertEqual(js.count("harden("), 3)               # popup, inline and fullpage iframes
        self.assertIn('getElementById("bitbotpro-container")', js)

    def test_embed_config_public_fields_and_anonymous_cors(self):
        self.store.col("embed_configs").append({
            "instance_id": "bot-a", "custom_css_url": "http://evil.example/x.css", "theme": {"--bb-accent": "#fff"},
            "welcome_message": "Hi", "default_open": True, "updated_at": "internal", "updated_by": "admin@x"})
        r = client.get("/api/embed/config/bot-a", headers={"Origin": "https://customer.example"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertEqual(set(r.json()), {"instance_id", "custom_css_url", "theme", "welcome_message", "default_open"})
        self.assertEqual(r.json()["custom_css_url"], "")         # non-https dropped
        self.assertEqual(client.get("/api/embed/config/unknown").json()["welcome_message"], "")

    def test_invitation_links_use_the_frontend_origin(self):
        from unittest.mock import AsyncMock
        sent = AsyncMock()
        with mock.patch.object(server, "_send_or_dev_fallback", sent):
            asyncio.run(server.send_invitation_email("x@example.org", "tok123", "member", "Owner"))
        args = sent.await_args.args
        self.assertIn(f"{self.APP}/invite/tok123", repr(args))
        self.assertNotIn(f"{self.API}/invite", repr(args))

    def test_discord_oauth_error_redirects_to_frontend_with_encoded_reason(self):
        r = client.get("/api/discord/callback", params={"error": "access_denied&evil=1", "error_description": "x"},
                       follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        loc = r.headers["location"]
        self.assertTrue(loc.startswith(f"{self.APP}/admin/discord?"), loc)
        self.assertIn("reason=access_denied%26evil%3D1", loc)
        self.assertNotIn("&evil=1", loc)

    def test_zendesk_urls_split_between_api_and_app(self):
        import zendesk_routes as zr
        self.assertEqual(zr._get_base_url(), self.API)
        self.assertEqual(zr._get_app_url(), self.APP)

    def test_sidebar_app_manifest_whitelists_api_domain(self):
        import io, json, zipfile
        from unittest.mock import AsyncMock
        import zendesk_routes as zr
        with mock.patch.object(zr, "_get_instance_access", AsyncMock(return_value="inst-1")):
            resp = asyncio.run(zr.download_sidebar_app(None))
        body = resp.body if hasattr(resp, "body") else b"".join(asyncio.run(_collect(resp.body_iterator)))
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            main_js = zf.read("assets/main.js").decode()
        self.assertEqual(manifest["domainWhitelist"], ["api.bitbotpro.io"])
        params = {p["name"]: p for p in manifest["parameters"]}
        self.assertEqual(params["bitbot_url"]["default"], self.API)
        self.assertTrue(params["sidebar_api_key"]["secure"])
        self.assertIn("client.request(", main_js)
        self.assertIn('"{{setting.sidebar_api_key}}"', main_js)
        self.assertNotIn("fetch(", main_js)


async def _collect(iterator):
    return [chunk async for chunk in iterator]


# ── Spec 008 Stages 5–8 ──────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class Stages5to8Tests(unittest.TestCase):
    """Enumeration (5), OAuth state (6), webhooks/public surface (7), invite-only (8)."""

    @classmethod
    def setUpClass(cls):
        server._init_telegram()
        server._init_zendesk()

    def setUp(self):
        from unittest.mock import AsyncMock
        from tests.unit._fake_motor import patch_motor
        import webhook_auth
        import bcrypt
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()
        webhook_auth.WEBHOOK_LIMITER.clear()
        self.store = st = patch_motor(self)
        self.sent = []

        async def capture(*args, **kwargs):
            self.sent.append(args)

        for name, value in (("send_verification_email", AsyncMock(side_effect=capture)),
                            ("send_reset_email", AsyncMock(side_effect=capture)),
                            ("_require_email_delivery", lambda: None),
                            ("EMAIL_RETRY_DELAYS_SECONDS", (0,))):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        pw = bcrypt.hashpw(b"Vq3!tz-Kp9#mW2rL", bcrypt.gensalt(4)).decode()
        st.col("users").extend([
            {"id": "root", "email": "root@example.org", "role": "superadmin", "is_verified": True, "token_version": 0, "hashed_password": pw},
            {"id": "owner-a", "email": "owner-a@example.org", "role": "user", "is_verified": True, "token_version": 0, "hashed_password": pw},
            {"id": "verified", "email": "verified@example.org", "role": "user", "is_verified": True, "token_version": 0, "hashed_password": pw},
            {"id": "pending", "email": "pending@example.org", "role": "user", "is_verified": False, "token_version": 0, "hashed_password": pw},
        ])
        st.col("workspaces").extend([{"id": "ws-a", "name": "A"}, {"id": "ws-b", "name": "B"}])
        st.col("workspace_members").append({"workspace_id": "ws-a", "user_id": "owner-a", "role": "owner"})
        st.col("organisations").extend([{"id": "org-a", "workspace_id": "ws-a", "member_ids": []},
                                        {"id": "org-b", "workspace_id": "ws-b", "member_ids": []}])
        st.col("bots").extend([{"id": "bot-a", "name": "A", "org_id": "org-a", "persona": "SECRET SYSTEM PROMPT"},
                               {"id": "bot-b", "name": "B", "org_id": "org-b"}])
        st.col("discord_app_config").append({"_id": "discord_app", "client_id": "1234567890",
                                             "client_secret": "s", "redirect_uri": "https://api.bitbotpro.io/api/discord/callback"})

    def h(self, uid, role="user"):
        return {"Authorization": f"Bearer {server.create_access_token(uid, f'{uid}@example.org', role, 0)}"}

    def user(self, uid):
        return next((u for u in self.store.col("users") if u["id"] == uid), None)

    # ── Stage 8: invite-only registration
    def test_registration_is_invite_only_and_uniform(self):
        self.assertEqual(client.get("/api/auth/registration-mode").json(), {"mode": "invite_only"})
        bodies = [client.post("/api/auth/register", json={"email": e, "password": "Vq3!tz-Kp9#mW2rL", "username": u})
                  for e, u in (("new@example.org", "newbie"), ("verified@example.org", "dupe"))]
        for r in bodies:
            self.assertEqual((r.status_code, r.json()), (403, {"detail": server.REGISTRATION_CLOSED_DETAIL}))
        self.assertEqual(len(self.store.col("users")), 4)
        self.assertEqual(self.sent, [])

    def test_open_mode_register_does_not_reveal_existing_email(self):
        with mock.patch.object(server, "REGISTRATION_MODE", "open"):
            a = client.post("/api/auth/register", json={"email": "verified@example.org", "password": "Hn7$wq-Lr4@zX8cB", "username": "x1"})
            b = client.post("/api/auth/register", json={"email": "brand-new@example.org", "password": "Hn7$wq-Lr4@zX8cB", "username": "x2"})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))

    def seed_invite(self, token="T" * 40, **extra):
        from datetime import datetime, timedelta, timezone
        doc = {"token": token, "email": "invitee@example.org", "role": "superadmin", "status": "pending",
               "workspace_id": "ws-a", "workspace_role": "member", "org_ids": ["org-a", "org-b"],
               "bot_ids": ["bot-a", "bot-b"], "instance_id": "legacy-1", "invited_by": "owner-a",
               "created_at": "2026-11-17T00:00:00+00:00",
               "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), **extra}
        self.store.col("invitations").append(doc)
        return doc

    def accept(self, token="T" * 40, password="Hn7$wq-Lr4@zX8cB", username="invitee"):
        return client.post(f"/api/invite/{token}/accept", json={"username": username, "password": password})

    def test_invitation_accepted_once_with_revalidated_grants(self):
        self.seed_invite()
        self.store.col("bot_instances").append({"id": "legacy-1", "assigned_user_ids": []})
        r = self.accept()
        self.assertEqual(r.status_code, 200, r.text)
        u = next(x for x in self.store.col("users") if x["email"] == "invitee@example.org")
        self.assertEqual(u["role"], "user")                       # forged platform role ignored
        self.assertEqual((u["org_ids"], u["bot_ids"]), (["org-a"], ["bot-a"]))   # foreign grants dropped
        self.assertEqual(self.store.col("bot_instances")[0]["assigned_user_ids"], [])  # non-superadmin inviter
        inv = self.store.col("invitations")[0]
        self.assertEqual(inv["status"], "accepted")
        again = self.accept(username="someone-else")
        self.assertEqual((again.status_code, again.json()["detail"]), (404, server.INVITATION_INVALID_DETAIL))
        self.assertEqual(client.get(f"/api/invite/{'T' * 40}").status_code, 404)

    def test_unusable_invitations_are_indistinguishable(self):
        from datetime import datetime, timedelta, timezone
        self.seed_invite(token="E" * 40, expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        self.seed_invite(token="R" * 40, status="revoked")
        self.seed_invite(token="A" * 40, status="accepted")
        answers = {client.get(f"/api/invite/{t}").text for t in ("E" * 40, "R" * 40, "A" * 40, "N" * 40, "short")}
        self.assertEqual(len(answers), 1)

    def test_invalid_input_does_not_consume_the_invitation(self):
        self.seed_invite()
        self.assertEqual(self.accept(password="password").status_code, 400)
        self.assertEqual(self.store.col("invitations")[0]["status"], "pending")
        self.assertEqual(self.accept().status_code, 200)

    def test_failed_account_creation_rolls_back_the_claim(self):
        from unittest.mock import AsyncMock
        self.seed_invite()
        with mock.patch.object(server, "_create_invited_account", AsyncMock(side_effect=RuntimeError("db down"))):
            with self.assertRaises(RuntimeError):
                self.accept()
        self.assertEqual(self.store.col("invitations")[0]["status"], "pending")
        self.assertIsNone(next((x for x in self.store.col("users") if x["email"] == "invitee@example.org"), None))

    # ── Stage 5: enumeration
    def test_forgot_and_resend_answers_are_identical(self):
        for path in ("/api/auth/forgot-password", "/api/auth/resend-code"):
            for limiter in server._IP_LIMITERS.values():
                limiter.clear()   # isolate from the per-IP issuance limit (tested elsewhere)
            answers = {client.post(path, json={"email": e}).text
                       for e in ("verified@example.org", "pending@example.org", "nobody@example.org")}
            with self.subTest(path=path):
                self.assertEqual(len(answers), 1)

    def test_provider_failure_never_changes_the_response(self):
        from unittest.mock import AsyncMock
        with mock.patch.object(server, "send_reset_email", AsyncMock(side_effect=RuntimeError("provider down"))):
            failing = client.post("/api/auth/forgot-password", json={"email": "verified@example.org"})
        unknown = client.post("/api/auth/forgot-password", json={"email": "nobody@example.org"})
        self.assertEqual((failing.status_code, failing.text), (unknown.status_code, unknown.text))
        self.assertNotIn("reset_code_hash", self.user("verified"))   # undeliverable code rolled back

    def test_verify_does_not_reveal_verified_state(self):
        answers = {client.post("/api/auth/verify", json={"email": e, "code": "123456"}).text
                   for e in ("verified@example.org", "pending@example.org", "nobody@example.org")}
        self.assertEqual(len(answers), 1)

    def test_response_time_floor(self):
        import time
        with mock.patch.object(server, "AUTH_RESPONSE_FLOOR_SECONDS", 0.25):
            t0 = time.monotonic()
            client.post("/api/auth/forgot-password", json={"email": "nobody@example.org"})
            self.assertGreaterEqual(time.monotonic() - t0, 0.25)

    # ── Stage 6: Discord OAuth state
    def oauth_state(self, path="/api/discord/oauth-url", uid="root", role="superadmin"):
        import urllib.parse as up
        r = client.get(path, headers={**self.h(uid, role), "X-Instance-ID": "bot-a"})
        self.assertEqual(r.status_code, 200, r.text)
        return up.parse_qs(up.urlparse(r.json()["url"]).query)["state"][0]

    def callback(self, state, cookie_uid=None):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import httpx
        headers = {}
        if cookie_uid:
            headers["Cookie"] = f"bf_access_token={server.create_access_token(cookie_uid, 'x@example.org', 'superadmin', 0)}"
        failing_exchange = AsyncMock(return_value=SimpleNamespace(status_code=400, text="invalid_grant", json=lambda: {}))
        with mock.patch.object(httpx.AsyncClient, "post", failing_exchange):
            r = client.get("/api/discord/callback", params={"code": "c", "state": state}, headers=headers,
                           follow_redirects=False)
        return r.headers.get("location", ""), failing_exchange.await_count

    def test_state_is_random_hashed_bound_and_single_use(self):
        state = self.oauth_state()
        self.assertGreaterEqual(len(state), 40)
        self.assertNotIn("bot-a", state)
        rec = self.store.col("oauth_states")[0]
        self.assertEqual((rec["provider"], rec["mode"], rec["user_id"], rec["instance_id"]),
                         ("discord", "install", "root", "bot-a"))
        self.assertNotEqual(rec["_id"], state)                    # only a hash is stored
        loc, exchanges = self.callback(state, cookie_uid="root")
        self.assertIn("token_exchange_failed", loc)               # accepted, then reached Discord
        self.assertEqual(exchanges, 1)
        loc2, exchanges2 = self.callback(state, cookie_uid="root")
        self.assertIn("reason=invalid_state", loc2)               # replay rejected before any exchange
        self.assertEqual(exchanges2, 0)

    def test_forged_legacy_and_expired_states_rejected_before_any_exchange(self):
        from datetime import datetime, timedelta, timezone
        import oauth_state
        expired = oauth_state.new_state()
        self.store.col("oauth_states").append(oauth_state.state_record(
            expired, provider="discord", mode="share", user_id="root", instance_id="bot-a",
            now=datetime.now(timezone.utc) - timedelta(days=30)))
        for state in ("bot-a", "share:bot-a", oauth_state.new_state(), expired, "", "x" * 300):
            with self.subTest(state=state[:20]):
                loc, exchanges = self.callback(state)
                self.assertTrue(loc.startswith("https://bitbotpro.io/admin/discord?"), loc)
                self.assertIn("reason=invalid_state", loc)
                self.assertEqual(exchanges, 0)

    def test_install_state_requires_the_initiating_session(self):
        loc, n = self.callback(self.oauth_state(), cookie_uid=None)
        self.assertIn("reason=session_mismatch", loc)
        loc, n = self.callback(self.oauth_state(), cookie_uid="owner-a")
        self.assertIn("reason=session_mismatch", loc)
        self.assertEqual(n, 0)

    def test_share_state_works_without_a_session(self):
        loc, n = self.callback(self.oauth_state("/api/discord/share-oauth-url"))
        self.assertIn("token_exchange_failed", loc)
        self.assertEqual(n, 1)

    def test_invite_email_only_for_our_discord_authorize_links(self):
        with mock.patch.object(server.resend, "api_key", "re_synthetic"):
            for url in ("https://evil.example/phish", "https://discord.com/oauth2/authorize?client_id=999",
                        "http://discord.com/oauth2/authorize?client_id=1234567890",
                        "https://discord.com.evil.example/oauth2/authorize?client_id=1234567890"):
                r = client.post("/api/discord/send-invite-email", headers={**self.h("root", "superadmin"), "X-Instance-ID": "bot-a"},
                                json={"to_email": "x@example.org", "invite_url": url})
                with self.subTest(url=url):
                    self.assertEqual(r.status_code, 400)

    # ── Stage 7: webhooks and public surface
    def test_telegram_requires_secret_header_when_registered(self):
        ws = "W" * 43
        self.store.col("telegram_config").append({"instance_id": "inst-t", "webhook_secret": ws,
                                                  "secret_token_registered": True, "is_active": False})
        url = f"/api/telegram/webhook/inst-t/{ws}"
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            r = client.post(url, json={}, headers=headers)
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "Not found"}))
        ok = client.post(url, json={}, headers={"X-Telegram-Bot-Api-Secret-Token": ws})
        self.assertEqual(ok.json(), {"ok": True, "skipped": "inactive"})

    def zendesk(self, raw, ts=None, sig=None, secret="Z" * 40, ws="S" * 43):
        from datetime import datetime, timezone
        import webhook_auth
        ts = ts or datetime.now(timezone.utc).isoformat()
        sig = sig if sig is not None else webhook_auth.zendesk_signature(secret, ts, raw)
        return client.post(f"/api/zendesk/webhook/inst-z/{ws}", content=raw,
                           headers={"Content-Type": "application/json",
                                    webhook_auth.ZENDESK_TIMESTAMP_HEADER: ts, webhook_auth.ZENDESK_SIGNATURE_HEADER: sig})

    def test_zendesk_signature_required_when_configured(self):
        from datetime import datetime, timedelta, timezone
        import api_integrations
        import zendesk_routes as zr
        self.store.col("zendesk_config").append({"instance_id": "inst-z", "webhook_secret": "S" * 43, "is_active": False,
                                                 zr.SIGNING_SECRET_FIELD: api_integrations.encrypt_value("Z" * 40)})
        raw = b'{"ticket_id": "1"}'
        self.assertEqual(self.zendesk(raw).json(), {"ok": True, "skipped": "inactive"})
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        import webhook_auth
        ts = datetime.now(timezone.utc).isoformat()
        signed_for_original = webhook_auth.zendesk_signature("Z" * 40, ts, raw)
        tampered = self.zendesk(raw + b" ", ts=ts, sig=signed_for_original)        # body changed after signing
        for r in (self.zendesk(raw, sig="AAAA"), self.zendesk(raw, secret="other-secret-value-000000"),
                  self.zendesk(raw, ts=stale), tampered):
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "Not found"}))
        import zendesk_routes  # noqa: F401  (stored secret encrypted, never plaintext)
        self.assertNotIn("Z" * 40, repr(self.store.col("zendesk_config")))

    def test_webhook_body_limit_and_invalid_json(self):
        self.store.col("zendesk_config").append({"instance_id": "inst-z", "webhook_secret": "S" * 43, "is_active": True})
        big = client.post(f"/api/zendesk/webhook/inst-z/{'S' * 43}", content=b"x" * (300 * 1024),
                          headers={"Content-Type": "application/json"})
        self.assertEqual(big.status_code, 413)
        bad = client.post(f"/api/zendesk/webhook/inst-z/{'S' * 43}", content=b"{not json",
                          headers={"Content-Type": "application/json"})
        self.assertEqual(bad.status_code, 400)

    def test_webhook_rate_limit_per_instance(self):
        codes = [client.post(f"/api/zendesk/webhook/inst-x/wrong-{i}", json={}).status_code for i in range(121)]
        self.assertEqual(codes[:120], [404] * 120)
        self.assertEqual(codes[120], 429)

    def test_bot_info_publishes_only_the_name(self):
        r = client.get("/api/bot-info/bot-a")
        self.assertEqual(set(r.json()), {"name"})
        self.assertNotIn("SECRET SYSTEM PROMPT", r.text)


# ── Spec 008 Stage 10: security headers and trusted proxy ────────────────────

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class Stage10EdgeTests(unittest.TestCase):
    def test_security_headers_on_real_responses(self):
        for path, corp in (("/api/health", "same-site"), ("/api/embed.js?instance=bot-a", "cross-origin"),
                           ("/api/knowledge/sources", "same-site"), ("/api/no-such-route", "same-site")):
            with self.subTest(path=path):
                r = client.get(path)
                self.assertEqual(r.headers["x-content-type-options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
                self.assertEqual(r.headers["cross-origin-resource-policy"], corp)
                self.assertIn("max-age=31536000", r.headers["strict-transport-security"])   # production

    def test_cors_preflight_keeps_cors_and_gets_security_headers(self):
        r = client.options("/api/auth/login", headers={"Origin": "https://bitbotpro.io",
                                                       "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "content-type,x-chat-session-token"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["access-control-allow-origin"], "https://bitbotpro.io")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_account_responses_are_not_cacheable(self):
        self.assertEqual(client.get("/api/auth/registration-mode").headers["cache-control"], "no-store")

    def _login_codes(self, peer, n=11):
        import httpx
        from tests.unit._fake_motor import patch_motor
        patch_motor(self)
        for limiter in server._IP_LIMITERS.values():
            limiter.clear()

        async def go():
            transport = httpx.ASGITransport(app=server.app, client=(peer, 41000))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                out = []
                for i in range(n):
                    r = await c.post("/api/auth/login", json={"email_or_username": f"u{i}@example.org", "password": "Wrong-pass-123!"},
                                     headers={"CF-Connecting-IP": f"203.0.113.{i + 1}", "X-Forwarded-For": f"198.51.100.{i + 1}"})
                    out.append(r.status_code)
                return out
        return asyncio.run(go())

    def test_client_ip_header_honoured_only_from_the_configured_edge(self):
        import client_ip
        # Selected edge: Caddy VPS over WireGuard (10.50.0.1); alternative: cloudflared (10.250.250.2).
        for trusted, untrusted in (("10.50.0.1", "10.50.0.11"), ("10.250.250.2", "10.250.250.4")):
            with self.subTest(edge=trusted):
                original = client_ip.get_resolver()
                client_ip.configure(client_ip.ClientIPResolver(client_ip.parse_trusted_proxies(trusted)))
                try:
                    via_edge = self._login_codes(trusted)      # 11 distinct real clients behind the edge
                    spoofed = self._login_codes(untrusted)     # e.g. a Gridscan WireGuard peer forging headers
                finally:
                    client_ip.configure(original)
                self.assertEqual(via_edge, [401] * 11)          # keyed per client header: no shared bucket
                self.assertEqual(spoofed[:10], [401] * 10)
                self.assertEqual(spoofed[10], 429)              # headers ignored: one bucket for the peer

    def test_production_process_configures_resolver_from_env(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **TEST_ENV, "TRUSTED_PROXY_IPS": "10.250.250.2"}
        code = ("import server, client_ip, sys; t = {str(a) for a in client_ip.get_resolver().trusted_proxies}; "
                "sys.exit(0 if t == {'10.250.250.2'} else 3)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertEqual(server._client_ip.get_resolver().trusted_proxies, frozenset())   # TEST_ENV trusts nothing


def json_dumps(obj):
    import json
    return json.dumps(obj)


if __name__ == "__main__":
    unittest.main()
