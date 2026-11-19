"""Phase 6B: production wiring of the Spec 012 API and worker (``api/production.py``,
``server.py``, ``worker.py``, ``healthcheck.py``). Replaces the legacy server suites
(``test_server_app.py``, ``test_server_static.py``, ``test_public_routes.py``)
for the properties that still apply; see ``security-traceability.md``."""
import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.unit._support import BACKEND_DIR

try:
    from tests.unit._api_fixtures import FERNET_KEY, HAVE_API
except Exception:            # pragma: no cover
    HAVE_API, FERNET_KEY = False, ""

JWT = "unit-test-jwt-secret-not-real-0123456789abcdef"
MONGO = "mongodb://appuser:Sup3rS3cretMongoPw@127.0.0.1:1/?serverSelectionTimeoutMS=200"


def env(**over):
    base = {"APP_MODE": "api", "APP_ENV": "production", "MONGO_URL": MONGO, "DB_NAME": "bitbotpro_v12",
            "JWT_SECRET": JWT, "ENCRYPTION_KEY": FERNET_KEY, "PUBLIC_API_URL": "https://api.bitbotpro.io",
            "PUBLIC_APP_URL": "https://bitbotpro.io", "CORS_ORIGINS": "https://bitbotpro.io",
            "TRUSTED_PROXIES": "10.50.0.1"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


_SAVED_RESOLVER = []


def setUpModule():
    try:
        import client_ip
        _SAVED_RESOLVER.append(client_ip.get_resolver())
    except Exception:          # pragma: no cover
        pass


def tearDownModule():
    """create_api_app configures the process-wide client-IP resolver; restore it for other suites."""
    if _SAVED_RESOLVER:
        import client_ip
        client_ip.configure(_SAVED_RESOLVER[0])


class FakeDB:
    def __init__(self, ok=True):
        self.ok = ok

    async def command(self, name):
        if not self.ok:
            raise RuntimeError("down")
        return {"ok": 1}


class FakeClient:
    def __init__(self, ok=True):
        self.db = FakeDB(ok)
        self.closed = False

    def __getitem__(self, name):
        return self.db

    def close(self):
        self.closed = True


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ConfigTests(unittest.TestCase):
    def cfg(self, **over):
        from api.production import ProductionConfig
        return ProductionConfig.from_environment(env(**over))

    def test_a_valid_environment(self):
        c = self.cfg()
        self.assertEqual((c.app_mode, c.app_env, c.db_name), ("api", "production", "bitbotpro_v12"))
        self.assertEqual(c.allowed_hosts, ("api.bitbotpro.io", "127.0.0.1", "localhost"))
        s = c.settings()
        self.assertTrue(s.cookie_secure)
        self.assertEqual(s.reset_url_base, "https://bitbotpro.io/app/reset-password#token=")
        self.assertEqual(s.invitation_url_base, "https://bitbotpro.io/app/invitations#token=")
        self.assertEqual((s.oauth_result_origin, s.public_api_origin), ("https://bitbotpro.io", "https://api.bitbotpro.io"))
        self.assertEqual(s.admin_origins, ("https://bitbotpro.io",))

    def test_missing_or_invalid_values_are_refused_without_echoing_them(self):
        from api.production import ProductionConfigError
        cases = {
            "APP_MODE": dict(APP_MODE=None), "APP_MODE ": dict(APP_MODE="all"), "APP_ENV": dict(APP_ENV="staging"),
            "MONGO_URL": dict(MONGO_URL=None), "DB_NAME": dict(DB_NAME=None), "DB_NAME ": dict(DB_NAME="bad name!"),
            "JWT_SECRET": dict(JWT_SECRET="short"),
            "ENCRYPTION": dict(ENCRYPTION_KEY=None),
            "PUBLIC_API_URL": dict(PUBLIC_API_URL=None), "PUBLIC_APP_URL": dict(PUBLIC_APP_URL="http://bitbotpro.io"),
            "PUBLIC_API_URL ": dict(PUBLIC_API_URL="https://api.bitbotpro.io/path"),
            "PUBLIC_APP_URL ": dict(PUBLIC_APP_URL="https://user:pw@bitbotpro.io"),
            "CORS_ORIGINS": dict(CORS_ORIGINS="*"), "CORS_ORIGINS ": dict(CORS_ORIGINS="http://x.example"),
            "ALLOWED_HOSTS": dict(ALLOWED_HOSTS="*"),
            "FROM_EMAIL": dict(RESEND_API_KEY="re_" + "s" * 30),
            "DISCORD_GATEWAY_URL": dict(DISCORD_GATEWAY_URL="ws://gateway.example"),
            "WORKER_INTERVAL_SECONDS": dict(WORKER_INTERVAL_SECONDS="0"),
        }
        for name, over in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ProductionConfigError) as cm:
                    self.cfg(**over)
                msg = str(cm.exception)
                self.assertIn(name.strip().split("_")[0], msg)
                for secret in (JWT, FERNET_KEY, "Sup3rS3cretMongoPw", "re_" + "s" * 30, "pw@"):
                    self.assertNotIn(secret, msg)

    def test_development_allows_http_origins_only_there(self):
        c = self.cfg(APP_ENV="development", PUBLIC_APP_URL="http://localhost:3000",
                     PUBLIC_API_URL="http://localhost:8000", CORS_ORIGINS="")
        self.assertFalse(c.settings().cookie_secure)

    def test_repr_never_contains_secrets(self):
        text = repr(self.cfg(RESEND_API_KEY="re_" + "q" * 30, FROM_EMAIL="noreply@bitbotpro.io"))
        for secret in (JWT, FERNET_KEY, "Sup3rS3cretMongoPw", "re_" + "q" * 30):
            self.assertNotIn(secret, text)

    def test_key_ring_configuration(self):
        from cryptography.fernet import Fernet
        k2 = Fernet.generate_key().decode()
        c = self.cfg(ENCRYPTION_KEY=None, ENCRYPTION_KEYS=f"k1={FERNET_KEY}\nk2={k2}", ENCRYPTION_ACTIVE_KEY_ID="k2")
        self.assertEqual((c.key_ring.active, c.key_ring.key_ids), ("k2", ("k1", "k2")))


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ApiAppTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import client_ip
        from api.production import ProductionConfig, create_api_app
        self.addCleanup(client_ip.configure, client_ip.get_resolver())    # never leak the trusted proxies
        self.prepared = []

        async def fake_prepare(db):
            self.prepared.append(db)

        p = mock.patch("persistence.mongo.prepare_database", fake_prepare)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = ProductionConfig.from_environment(env())
        self.client_obj = FakeClient()
        self.app = create_api_app(self.cfg, client=self.client_obj)
        self.c = TestClient(self.app, base_url="https://api.bitbotpro.io")
        self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)

    def test_health_and_readiness(self):
        r = self.c.get("/api/health")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(self.c.get("/api/ready").status_code, 200)
        self.assertEqual(len(self.prepared), 1)                      # database prepared before serving
        self.client_obj.db.ok = False
        self.assertEqual(self.c.get("/api/ready").json(), {"status": "unavailable"})

    def test_host_header_allowlist(self):
        self.assertEqual(self.c.get("/api/health", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.c.get("/api/health", headers={"Host": "api.bitbotpro.io.evil.example"}).status_code, 400)
        for host in ("api.bitbotpro.io", "127.0.0.1:8000", "localhost"):
            self.assertEqual(self.c.get("/api/health", headers={"Host": host}).status_code, 200, host)

    def test_cors_only_for_the_admin_origin(self):
        ok = self.c.options("/api/auth/session", headers={"Origin": "https://bitbotpro.io",
                                                          "Access-Control-Request-Method": "GET"})
        self.assertEqual(ok.headers.get("access-control-allow-origin"), "https://bitbotpro.io")
        self.assertEqual(ok.headers.get("access-control-allow-credentials"), "true")
        bad = self.c.options("/api/auth/session", headers={"Origin": "https://evil.example",
                                                           "Access-Control-Request-Method": "GET"})
        self.assertIsNone(bad.headers.get("access-control-allow-origin"))

    def test_security_headers_and_no_api_documentation(self):
        r = self.c.get("/api/health")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertNotIn("server", {k.lower() for k in r.headers})
        for path in ("/docs", "/redoc", "/openapi.json", "/api/docs", "/api/openapi.json", "/"):
            self.assertEqual(self.c.get(path).status_code, 404, path)

    def test_unauthenticated_requests_are_refused_without_detail(self):
        r = self.c.get("/api/enterprises")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("Traceback", r.text)

    def test_client_ip_trusts_only_the_configured_edge(self):
        import client_ip
        trusted = {str(ip) for ip in client_ip.get_resolver().trusted_proxies}
        self.assertEqual(trusted, {"10.50.0.1"})
        self.assertEqual(client_ip.get_resolver().resolve("10.50.0.1", {"CF-Connecting-IP": "198.51.100.7"}),
                         "198.51.100.7")
        self.assertEqual(client_ip.get_resolver().resolve("203.0.113.9", {"CF-Connecting-IP": "198.51.100.7"}),
                         "203.0.113.9")

    def test_real_adapters_and_no_test_doubles(self):
        S = self.app.state.services
        from runtime.providers import AnthropicProvider, OpenAIProvider
        self.assertIsInstance(S.providers["anthropic"], AnthropicProvider)
        self.assertIsInstance(S.providers["openai"], OpenAIProvider)
        self.assertEqual((S.telegram_transport, S.discord_transport, S.zendesk_transport, S.zendesk_resolver),
                         (None, None, None, None))
        self.assertEqual(type(S.mailer).__name__, "ResendMailer")
        self.assertEqual(S.box.ring.active, "k1")

    def test_wrong_mode_is_refused(self):
        from api.production import ProductionConfig, ProductionConfigError, create_api_app, run_worker
        with self.assertRaises(ProductionConfigError):
            create_api_app(ProductionConfig.from_environment(env(APP_MODE="worker")), client=FakeClient())
        with self.assertRaises(ProductionConfigError):
            asyncio.run(run_worker(self.cfg, client=FakeClient()))


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class LogRedactionInstalledTests(unittest.TestCase):
    def test_access_log_handlers_are_wrapped_at_startup(self):
        import log_redaction
        from fastapi.testclient import TestClient
        from api.production import ProductionConfig, create_api_app
        handler = logging.StreamHandler()
        access = logging.getLogger("uvicorn.access")
        access.addHandler(handler)                 # uvicorn configures its handlers before the lifespan runs
        self.addCleanup(access.removeHandler, handler)

        async def fake_prepare(db):
            return None

        with mock.patch("persistence.mongo.prepare_database", fake_prepare):
            app = create_api_app(ProductionConfig.from_environment(env()), client=FakeClient())
            with TestClient(app, base_url="https://api.bitbotpro.io"):
                pass
        self.assertIsInstance(handler.formatter, log_redaction.RedactingFormatter)
        line = handler.formatter.format(logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '"POST /api/hooks/telegram/i-1/PathSecret0123456789abcd HTTP/1.1" 200',
            None, None))
        self.assertNotIn("PathSecret0123456789abcd", line)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class MailerTests(unittest.IsolatedAsyncioTestCase):
    def mailer(self, key="re_" + "k" * 30):
        from api.production import ProductionConfig, ResendMailer
        sent = []
        cfg = ProductionConfig.from_environment(env(RESEND_API_KEY=key or None, FROM_EMAIL="noreply@bitbotpro.io"
                                                    if key else None))
        return ResendMailer(cfg, send_fn=sent.append), sent

    async def test_messages_are_escaped_and_never_logged(self):
        m, sent = self.mailer()
        with self.assertLogs("email_delivery", level="INFO") as logs:
            await m.send_password_reset("u@example.org", 'https://bitbotpro.io/app/reset-password#token=abc"><script>')
            await m.send_discord_invite("u@example.org", "https://api.bitbotpro.io/api/public/discord/install/TOKEN123",
                                        organisation_name="<b>Org</b>", instance_name="I & co", expires_at="2026-12-01")
        self.assertEqual([s["to"] for s in sent], [["u@example.org"], ["u@example.org"]])
        self.assertNotIn("<script>", sent[0]["html"])
        self.assertIn("&lt;b&gt;Org&lt;/b&gt;", sent[1]["html"])
        self.assertEqual(sent[0]["from"], "noreply@bitbotpro.io")
        joined = "\n".join(logs.output)
        self.assertNotIn("token=abc", joined)
        self.assertNotIn("TOKEN123", joined)

    async def test_without_a_key_nothing_is_sent_and_callers_see_a_failure(self):
        import email_delivery
        m, sent = self.mailer(key="")
        with self.assertRaises(email_delivery.EmailDeliveryError):
            await m.send_invitation("u@example.org", subject="s", text="t", html="<p>h</p>")
        self.assertEqual(sent, [])


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class WorkerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_worker_runs_every_adapter_with_the_real_gateway_and_stops_cleanly(self):
        from api.production import ProductionConfig, run_worker, worker_healthy
        from runtime.discord_gateway_ws import DiscordGatewayConnection
        seen = {}

        async def fake_prepare(db):
            seen["prepared"] = True

        async def fake_run_forever(self, stop, *, interval_seconds):
            seen.update(worker=self, interval=interval_seconds)
            await asyncio.sleep(0.05)

        shutdowns = []

        async def fake_shutdown(self):
            shutdowns.append(True)

        hb = tempfile.NamedTemporaryFile(delete=False)
        hb.close()
        os.unlink(hb.name)
        client = FakeClient()
        with mock.patch("persistence.mongo.prepare_database", fake_prepare), \
                mock.patch("runtime.worker.Worker.run_forever", fake_run_forever), \
                mock.patch("runtime.discord_gateway.GatewayManager.shutdown", fake_shutdown):
            cfg = ProductionConfig.from_environment(env(APP_MODE="worker", WORKER_ID="w-1", WORKER_INTERVAL_SECONDS="7"))
            await run_worker(cfg, client=client, heartbeat_file=hb.name)
        w = seen["worker"]
        self.assertTrue(seen["prepared"])
        self.assertEqual((w.worker_id, seen["interval"]), ("w-1", 7.0))
        for adapter in (w.telegram, w.discord, w.gateway, w.zendesk):
            self.assertIsNotNone(adapter)
        conn = w.gateway.factory({"application_id": "123456789012345678"})
        self.assertIsInstance(conn, DiscordGatewayConnection)       # the real protocol adapter, not a mock
        self.assertEqual(conn.gateway_url, "wss://gateway.discord.gg")
        self.assertTrue(any(k for k in w.executors))
        self.assertEqual(shutdowns, [True])                           # the gateway is closed on stop
        self.assertTrue(client.closed)
        self.assertTrue(worker_healthy(hb.name))
        os.utime(hb.name)
        Path(hb.name).write_text(str(int(time.time()) - 600))
        self.assertFalse(worker_healthy(hb.name))
        os.unlink(hb.name)
        self.assertFalse(worker_healthy(hb.name))


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ProcessTests(unittest.TestCase):
    def run_py(self, code, extra_env):
        # a clean environment: other suites set variables in os.environ (e.g. DB_NAME) that must not leak in
        e = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
        e.update(extra_env)
        return subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_DIR), env=e, capture_output=True,
                              text=True, timeout=60)

    def test_server_refuses_to_start_with_a_bad_configuration_and_never_echoes_secrets(self):
        r = self.run_py("import server", env(JWT_SECRET=None))
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn("JWT_SECRET", r.stderr)
        for secret in ("Sup3rS3cretMongoPw", FERNET_KEY):
            self.assertNotIn(secret, r.stdout + r.stderr)

    def test_server_builds_the_app_without_contacting_anything(self):
        r = self.run_py("import server; from api.app import route_table; print(sorted(r.path for r in route_table(server.app) if r.path.startswith('/api/h')))",
                        env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/api/health", r.stdout)

    def test_worker_entrypoint_usage_and_configuration_errors(self):
        r = self.run_py("import worker, sys; sys.exit(worker.main(['--bogus']))", env(APP_MODE="worker"))
        self.assertEqual(r.returncode, 64)
        r = self.run_py("import worker, sys; sys.exit(worker.main([]))", env(APP_MODE="worker", DB_NAME=None))
        self.assertEqual(r.returncode, 78)
        self.assertIn("DB_NAME", r.stderr)
        r = self.run_py("import worker, sys; sys.exit(worker.main(['--healthcheck']))", env(APP_MODE="worker"))
        self.assertEqual(r.returncode, 1)                             # no heartbeat yet

    def test_healthcheck_script_follows_the_mode(self):
        r = self.run_py("import healthcheck, sys; sys.exit(healthcheck.main())", {"APP_MODE": "api"})
        self.assertEqual(r.returncode, 1)                             # nothing listening on 8000 here


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class PublicRouteInventoryTests(unittest.TestCase):
    """Every route reachable without a session is reviewed (spec 008 R-P7.5, ported)."""
    REVIEWED = {
        ("GET", "/api/health"), ("GET", "/api/ready"),
        ("POST", "/api/auth/login"), ("POST", "/api/auth/forgot-password"), ("POST", "/api/auth/reset-password"),
        ("POST", "/api/invitations/register"),                         # 6C B-ACCT (token = credential)
        ("GET", "/api/embed.js"), ("GET", "/api/public/widget.js"), ("GET", "/api/public/widget.css"),
        ("GET", "/api/public/widget/{pid}"), ("GET", "/api/public/instances/{pid}"),
        ("GET", "/api/public/instances/{pid}/logo"), ("POST", "/api/public/instances/{pid}/sessions"),
        ("POST", "/api/public/instances/{pid}/chat"), ("POST", "/api/public/instances/{pid}/chat/stream"),
        ("GET", "/api/public/instances/{pid}/sessions/{sid}/history"),
        ("OPTIONS", "/api/public/instances/{pid}"), ("OPTIONS", "/api/public/instances/{pid}/{rest:path}"),
        ("GET", "/api/public/discord/install/{token}"),
        ("GET", "/api/hooks/discord/oauth/callback"), ("GET", "/api/hooks/zendesk/oauth/callback"),
        ("POST", "/api/hooks/telegram/{instance_id}/{secret}"), ("POST", "/api/hooks/zendesk/{integration_id}/{secret}"),
        ("POST", "/api/hooks/zendesk/sidebar/generate"),
    }

    def test_the_route_table_sees_every_route(self):
        """Guard: a framework change must never make route inventories silently empty."""
        from api.app import route_table
        from api.production import ProductionConfig, create_api_app
        app = create_api_app(ProductionConfig.from_environment(env()), client=FakeClient())
        self.assertGreaterEqual(len(route_table(app)), 150)

    def test_public_routes_match_the_reviewed_inventory(self):
        from api.production import ProductionConfig, create_api_app

        def deps(d):
            out = set()
            for x in d.dependencies:
                out.add(getattr(x.call, "__name__", ""))
                out |= deps(x)
            return out

        app = create_api_app(ProductionConfig.from_environment(env()), client=FakeClient())
        from api.app import route_table
        public = {(sorted(r.methods)[0], r.path) for r in route_table(app)
                  if hasattr(r, "dependant") and "principal" not in deps(r.dependant)}
        self.assertEqual(public, self.REVIEWED)


class StaticTests(unittest.TestCase):
    SHIPPED = ["server.py", "worker.py", "healthcheck.py"] + [str(p.relative_to(BACKEND_DIR)) for d in
                                                              ("api", "core", "persistence", "runtime")
                                                              for p in (BACKEND_DIR / d).glob("*.py")]

    def test_no_emergent_or_legacy_discord_client_in_shipped_code(self):
        for name in self.SHIPPED:
            text = (BACKEND_DIR / name).read_text(encoding="utf-8")
            for needle in ("emergentintegrations", "EMERGENT_LLM_KEY", "import discord\n", "discord.Client(",
                           "workspace_id"):
                self.assertFalse(needle in text, f"{name}: {needle}")
            # no tenant header is ever read (spec §1.6); docstrings may name them as ignored
            self.assertFalse(re.search(r"(?i)headers(\.get\(|\[)\s*[\"']x-(instance|org|workspace|enterprise)-id", text),
                             name)

    def test_the_gateway_is_only_built_inside_the_worker(self):
        text = (BACKEND_DIR / "api" / "production.py").read_text(encoding="utf-8")
        api_part = text[text.index("def create_api_app"):text.index("async def run_worker")]
        self.assertNotIn("GatewayManager", api_part)
        self.assertNotIn("connection_factory", api_part)
        for name in self.SHIPPED:
            if name in ("api/production.py",) or name.startswith("runtime/"):
                continue
            self.assertNotIn("discord_gateway_ws", (BACKEND_DIR / name).read_text(encoding="utf-8"), name)

    def test_no_secret_is_logged_by_the_wiring(self):
        text = (BACKEND_DIR / "api" / "production.py").read_text(encoding="utf-8")
        for call in re.findall(r"log\.\w+\(([^)]*)\)", text):
            for field in ("mongo_url", "jwt_secret", "resend_api_key", "key_ring)", "encryption_key"):
                self.assertNotIn(field, call)


if __name__ == "__main__":
    unittest.main()
