"""APP_MODE, APP_ENV, CORS validation and KB path matching (spec 004 C2, C8, R5)."""
import unittest

from tests.unit import _support  # noqa: F401  (sys.path setup)
import runtime_config as rc


class AppModeTests(unittest.TestCase):
    def test_default_is_all(self):
        self.assertEqual(rc.get_app_mode({}), "all")

    def test_api_mode(self):
        self.assertEqual(rc.get_app_mode({"APP_MODE": "api"}), "api")
        self.assertEqual(rc.get_app_mode({"APP_MODE": " API "}), "api")

    def test_invalid_mode_rejected(self):
        for bad in ("worker", "bogus", "api,all", "0"):
            with self.assertRaises(rc.ConfigError):
                rc.get_app_mode({"APP_MODE": bad})

    def test_discord_disallowed_only_in_api_mode(self):
        self.assertFalse(rc.discord_allowed("api"))
        self.assertTrue(rc.discord_allowed("all"))

    def test_kb_disabled_only_in_api_mode(self):
        self.assertTrue(rc.kb_disabled("api"))
        self.assertFalse(rc.kb_disabled("all"))


class AppEnvTests(unittest.TestCase):
    def test_default_is_production(self):
        self.assertEqual(rc.get_app_env({}), "production")

    def test_development(self):
        self.assertEqual(rc.get_app_env({"APP_ENV": "development"}), "development")

    def test_invalid_env_rejected(self):
        for bad in ("prod", "dev", "staging", "test"):
            with self.assertRaises(rc.ConfigError):
                rc.get_app_env({"APP_ENV": bad})


class CorsTests(unittest.TestCase):
    def test_production_value(self):
        self.assertEqual(rc.parse_cors_origins("https://bitbotpro.io", "production"),
                         ["https://bitbotpro.io"])

    def test_multiple_and_dedup(self):
        self.assertEqual(
            rc.parse_cors_origins("https://bitbotpro.io, https://app.example.com:8443,https://bitbotpro.io",
                                  "production"),
            ["https://bitbotpro.io", "https://app.example.com:8443"])

    def test_required(self):
        for raw in (None, "", "   "):
            with self.assertRaises(rc.ConfigError):
                rc.parse_cors_origins(raw, "production")

    def test_rejects_wildcards_and_malformed(self):
        bad = [
            "*", "https://*.bitbotpro.io", "https://bitbotpro.io,*", "null",
            "https://bitbotpro.io/", "https://bitbotpro.io/path", "bitbotpro.io",
            "HTTPS://BITBOTPRO.IO", "https://bitbotpro.io,", ",https://bitbotpro.io",
            "https://user@bitbotpro.io", "https://bitbotpro.io?x=1", "https://bitbotpro.io:99999",
            "ftp://bitbotpro.io", "https://", "https://-bad.example",
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(rc.ConfigError):
                rc.parse_cors_origins(raw, "production")

    def test_plain_http_rejected_in_production(self):
        for raw in ("http://bitbotpro.io", "http://localhost:3000", "http://127.0.0.1:3000"):
            with self.subTest(raw=raw), self.assertRaises(rc.ConfigError):
                rc.parse_cors_origins(raw, "production")

    def test_localhost_allowed_only_in_development(self):
        self.assertEqual(rc.parse_cors_origins("http://localhost:3000", "development"),
                         ["http://localhost:3000"])
        self.assertEqual(rc.parse_cors_origins("http://127.0.0.1", "development"),
                         ["http://127.0.0.1"])

    def test_non_localhost_http_rejected_even_in_development(self):
        for raw in ("http://bitbotpro.io", "http://192.168.0.32:3000", "http://localhost.evil.com"):
            with self.subTest(raw=raw), self.assertRaises(rc.ConfigError):
                rc.parse_cors_origins(raw, "development")


class KbPathTests(unittest.TestCase):
    def test_kb_paths(self):
        for p in ("/api/knowledge", "/api/knowledge/sources", "/api/knowledge/sources/abc/content",
                  "/api/knowledge/sources/url/crawl-stream", "/api/knowledge/import",
                  "/api/knowledge/sources/github/import-stream", "/api/knowledge/"):
            with self.subTest(p=p):
                self.assertTrue(rc.is_kb_path(p))

    def test_non_kb_paths(self):
        for p in ("/api/", "/api/health", "/api/auth/login", "/api/auth/me", "/api/bots",
                  "/api/knowledgebase", "/api/knowledge-x", "/knowledge", "/api/v2/knowledge",
                  "/api/admin/settings", "/api/workspaces"):
            with self.subTest(p=p):
                self.assertFalse(rc.is_kb_path(p))

    def test_message_mentions_postgres_migration(self):
        self.assertIn("PostgreSQL/pgvector", rc.KB_UNAVAILABLE_DETAIL)


class EmergentStorageTests(unittest.TestCase):
    def test_only_with_key(self):
        self.assertFalse(rc.emergent_storage_configured({}))
        self.assertFalse(rc.emergent_storage_configured({"CLAUDE_API_KEY": "  "}))
        self.assertTrue(rc.emergent_storage_configured({"EMERGENT_LLM_KEY": "x"}))


if __name__ == "__main__":
    unittest.main()
