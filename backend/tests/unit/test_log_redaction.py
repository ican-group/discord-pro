"""Defence-in-depth log redaction (spec 004, R8). All values are synthetic."""
import io
import logging
import unittest

from tests.unit import _support  # noqa: F401
import log_redaction as lr

TG_TOKEN = "123456789:AAH4synthetic_telegram_bot_token_value_x"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGljX3NpZ25hdHVyZQ"


class RedactTests(unittest.TestCase):
    CASES = [
        ('127.0.0.1:5 - "POST /api/zendesk/webhook/inst-1/s3cr3tWebhookValue HTTP/1.1" 200',
         "s3cr3tWebhookValue"),
        ('"POST /api/telegram/webhook/inst-1/tgWebhookSecret123 HTTP/1.1" 200', "tgWebhookSecret123"),
        ('"GET /api/invite/Zx9_invite_token_value/accept HTTP/1.1" 200', "Zx9_invite_token_value"),
        ('"GET /api/discord/callback?code=oauthCode123&state=st4te&guild_id=9 HTTP/1.1" 307',
         "oauthCode123"),
        ('"GET /api/discord/callback?code=oauthCode123&state=st4te HTTP/1.1" 307', "st4te"),
        (f"POST https://api.telegram.org/bot{TG_TOKEN}/sendMessage failed", TG_TOKEN.split(":")[1]),
        ("Authorization: Bearer abcdefghijklmnop123456", "abcdefghijklmnop123456"),
        (f"token={JWT}", JWT),
        (f"decoded {JWT} ok", JWT),
        ("mongodb://bitbotpro_app:0123456789abcdef@bitbotpro-mongo:27017/bitbotpro", "0123456789abcdef"),
        ("postgresql://bitbotpro_app:pw0123456789@bitbotpro-postgres:5432/x", "pw0123456789"),
        ("key sk-ant-api03-SYNTHETICsyntheticKEY0123", "SYNTHETICsyntheticKEY0123"),
        ("resend key re_SYNTHETIC0123456789abc", "SYNTHETIC0123456789abc"),
    ]

    def test_secrets_redacted(self):
        for line, secret in self.CASES:
            with self.subTest(line=line):
                out = lr.redact(line)
                self.assertNotIn(secret, out)
                self.assertIn(lr.REDACTED, out)

    def test_ordinary_lines_unchanged(self):
        for line in ('127.0.0.1:5 - "GET /api/health HTTP/1.1" 200',
                     '"GET /api/knowledge/sources HTTP/1.1" 503',
                     "Startup mode: APP_MODE=api APP_ENV=production discord=disabled mongo_kb=disabled",
                     "Email sent (kind=verification) to user@example.org",
                     '"GET /api/bots?limit=10&page=2 HTTP/1.1" 200'):
            with self.subTest(line=line):
                self.assertEqual(lr.redact(line), line)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.logger = logging.getLogger("test.redaction.install")
        self.logger.propagate = False
        self.logger.handlers = []
        h = logging.StreamHandler(self.stream)
        h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger.addHandler(h)
        self.logger.setLevel(logging.INFO)

    def test_install_is_idempotent_and_redacts_messages_args_and_tracebacks(self):
        self.assertEqual(lr.install(["test.redaction.install"]), 1)
        self.assertEqual(lr.install(["test.redaction.install"]), 0)
        # uvicorn-style access record with args
        self.logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "POST",
                         "/api/zendesk/webhook/i1/s3cr3tWebhookValue", "1.1", 200)
        try:
            raise RuntimeError(f"failed calling https://api.telegram.org/bot{TG_TOKEN}/getMe")
        except RuntimeError:
            self.logger.exception("telegram call failed")
        out = self.stream.getvalue()
        self.assertNotIn("s3cr3tWebhookValue", out)
        self.assertNotIn(TG_TOKEN.split(":")[1], out)
        self.assertIn("INFO", out)
        self.assertIn("Traceback", out)


class SanitizeForLogTests(unittest.TestCase):
    """Log-injection sanitising (spec 008, R-L1.1). Not yet applied to call sites."""

    def test_plain_text_unchanged(self):
        self.assertEqual(lr.sanitize_for_log("inst-123 abc"), "inst-123 abc")

    def test_crlf_cannot_forge_a_second_log_line(self):
        payload = "inst-1\r\n2026-11-17 00:00:00,000 - server - INFO - admin logged in"
        stream = io.StringIO()
        logger = logging.getLogger("test.sanitize.crlf")
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.warning("instance header: %s", lr.sanitize_for_log(payload))
        finally:
            logger.removeHandler(handler)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("\\r\\n", lines[0])

    def test_all_control_and_separator_characters_escaped(self):
        chars = [chr(c) for c in range(0x00, 0x20)] + ["\x7f", "\x85", "\x9b", "\u2028", "\u2029",
                                                      "\u202e", "\u2066", "\u200f", "\ufeff"]
        out = lr.sanitize_for_log("".join(chars), max_len=1000)
        for ch in chars:
            self.assertNotIn(ch, out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.isprintable())

    def test_ansi_escape_neutralised(self):
        out = lr.sanitize_for_log("\x1b[2J\x1b[31mFAKE")
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b[2J", out)

    def test_backslash_escaped_so_escapes_cannot_be_forged(self):
        self.assertEqual(lr.sanitize_for_log("a\\nb"), "a\\\\nb")
        self.assertNotEqual(lr.sanitize_for_log("a\\nb"), lr.sanitize_for_log("a\nb"))

    def test_length_capped_and_marked(self):
        out = lr.sanitize_for_log("x" * 10_000, max_len=50)
        self.assertEqual(len(out), 50)
        self.assertTrue(out.endswith("...[truncated]"))

    def test_cap_holds_when_escapes_expand(self):
        for max_len in (20, 21, 37, 200):
            with self.subTest(max_len=max_len):
                out = lr.sanitize_for_log("\u202e" * 500, max_len=max_len)
                self.assertLessEqual(len(out), max_len)
                body = out[: -len("...[truncated]")]
                self.assertEqual(len(body) % 6, 0)  # never splits a \uXXXX escape

    def test_exact_fit_not_truncated(self):
        self.assertEqual(lr.sanitize_for_log("y" * 30, max_len=30), "y" * 30)
        self.assertEqual(lr.sanitize_for_log("a\nb", max_len=30), "a\\nb")

    def test_non_string_values(self):
        self.assertEqual(lr.sanitize_for_log(None), "None")
        self.assertEqual(lr.sanitize_for_log(42), "42")
        self.assertEqual(lr.sanitize_for_log(b"a\nb"), "b'a\\\\nb'")  # repr backslash is escaped too

    def test_unicode_text_preserved(self):
        self.assertEqual(lr.sanitize_for_log("Zürich – 東京"), "Zürich – 東京")

    def test_max_len_too_small_rejected(self):
        with self.assertRaises(ValueError):
            lr.sanitize_for_log("x", max_len=5)

    def test_existing_redaction_behaviour_unchanged(self):
        self.assertEqual(lr.redact("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
