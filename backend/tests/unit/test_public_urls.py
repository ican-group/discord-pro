"""PUBLIC_APP_URL / PUBLIC_API_URL validation (spec 008, R-P6.1).

Stage 1: validation only. Not read by any route, not set in compose.
"""
import unittest

from tests.unit._support import BACKEND_DIR
import runtime_config as rc

PROD, DEV = rc.APP_ENV_PRODUCTION, rc.APP_ENV_DEVELOPMENT


class PublicUrlTests(unittest.TestCase):
    def test_valid_production_origins(self):
        for value in ("https://bitbotpro.io", "https://api.bitbotpro.io", "https://api.bitbotpro.io:8443"):
            with self.subTest(value=value):
                self.assertEqual(rc.parse_public_url("PUBLIC_APP_URL", value, PROD), value)

    def test_surrounding_whitespace_trimmed(self):
        self.assertEqual(rc.parse_public_url("PUBLIC_APP_URL", "  https://bitbotpro.io \n", PROD),
                         "https://bitbotpro.io")

    def test_rejected_in_production(self):
        bad = [
            "http://bitbotpro.io",                 # not https
            "http://localhost:3000",               # dev-only
            "http://127.0.0.1:8000",               # dev-only
            "https://bitbotpro.io/",               # trailing slash
            "https://bitbotpro.io/app",            # path
            "https://bitbotpro.io?x=1",            # query
            "https://bitbotpro.io#frag",           # fragment
            "https://user:pw@bitbotpro.io",        # credentials
            "https://user@bitbotpro.io",
            "https://BitBotPro.io",                # not lowercase
            "https://*.bitbotpro.io",              # wildcard
            "https://bitbotpro.io:0",              # bad port
            "https://bitbotpro.io:70000",
            "bitbotpro.io",                        # no scheme
            "//bitbotpro.io",
            "ftp://bitbotpro.io",
            "javascript:alert(1)",
            "https://",
            "https://-bad.example",
            "https://bitbotpro.io https://evil.example",
            "https://bitbotpro.io,https://evil.example",
            "https://bitbotpro.io\r\nX-Injected: 1",
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(rc.ConfigError):
                    rc.parse_public_url("PUBLIC_APP_URL", value, PROD)

    def test_unset_is_none_unless_required(self):
        for raw in (None, "", "   "):
            self.assertIsNone(rc.parse_public_url("PUBLIC_API_URL", raw, PROD))
            with self.assertRaises(rc.ConfigError):
                rc.parse_public_url("PUBLIC_API_URL", raw, PROD, required=True)

    def test_development_allows_local_http_only(self):
        self.assertEqual(rc.parse_public_url("PUBLIC_APP_URL", "http://localhost:3000", DEV), "http://localhost:3000")
        self.assertEqual(rc.parse_public_url("PUBLIC_API_URL", "http://127.0.0.1:8000", DEV), "http://127.0.0.1:8000")
        for value in ("http://bitbotpro.io", "http://192.168.1.10:8000", "http://localhost.evil.example"):
            with self.subTest(value=value):
                with self.assertRaises(rc.ConfigError):
                    rc.parse_public_url("PUBLIC_APP_URL", value, DEV)

    def test_error_names_the_variable(self):
        with self.assertRaises(rc.ConfigError) as cm:
            rc.parse_public_url("PUBLIC_API_URL", "http://api.bitbotpro.io", PROD)
        self.assertIn("PUBLIC_API_URL", str(cm.exception))

    def test_get_public_urls(self):
        env = {"PUBLIC_APP_URL": "https://bitbotpro.io", "PUBLIC_API_URL": "https://api.bitbotpro.io"}
        self.assertEqual(rc.get_public_urls(env, PROD), ("https://bitbotpro.io", "https://api.bitbotpro.io"))
        self.assertEqual(rc.get_public_urls({}, PROD), (None, None))
        with self.assertRaises(rc.ConfigError):
            rc.get_public_urls({}, PROD, required=True)
        with self.assertRaises(rc.ConfigError):
            rc.get_public_urls({"PUBLIC_APP_URL": "http://bitbotpro.io"}, PROD)

    def test_cors_error_message_unchanged(self):
        with self.assertRaises(rc.ConfigError) as cm:
            rc.parse_cors_origins("https://bitbotpro.io:0", PROD)
        self.assertIn("CORS origin has an invalid port", str(cm.exception))


class ActivatedUsageTests(unittest.TestCase):
    """Stage 9b: links/redirects/webhooks use PUBLIC_APP_URL / PUBLIC_API_URL only."""

    def test_backend_never_reads_frontend_env_or_legacy_url(self):
        for path in sorted(BACKEND_DIR.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("REACT_APP_BACKEND_URL", src)
                self.assertNotIn('"frontend", ".env"', src)

    def test_server_requires_public_urls_in_production(self):
        """Spec 012 (Phase 6B): the production wiring refuses to start without both public URLs
        (behaviour: tests/unit/test_production_app.py::ConfigTests)."""
        src = (BACKEND_DIR / "api" / "production.py").read_text(encoding="utf-8")
        self.assertIn('_origin("PUBLIC_API_URL", env.get("PUBLIC_API_URL"), app_env=app_env, required=True)', src)
        self.assertIn('_origin("PUBLIC_APP_URL", env.get("PUBLIC_APP_URL"), app_env=app_env, required=True)', src)

    def test_public_url_accessor(self):
        self.assertEqual(rc.public_url("PUBLIC_APP_URL", {"PUBLIC_APP_URL": " https://bitbotpro.io/ "}),
                         "https://bitbotpro.io")
        self.assertEqual(rc.public_url("PUBLIC_API_URL", {}), "")

if __name__ == "__main__":
    unittest.main()
