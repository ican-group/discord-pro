"""chat_sessions (spec 008 Stage 4: P3, N1). Offline, synthetic keys."""
import unittest

from tests.unit import _support  # noqa: F401
import chat_sessions as cs

CODE_KEY = b"unit-test-code-hmac-key-not-real-abcdefghijklmnop"
KEY = cs.derive_key(CODE_KEY)
OTHER = cs.derive_key(b"another-unit-test-key-not-real-0123456789abcdefg")
NOW = 1_800_000_000


class SessionIdTests(unittest.TestCase):
    def test_format_and_uniqueness(self):
        ids = {cs.new_session_id() for _ in range(2000)}
        self.assertEqual(len(ids), 2000)
        for sid in list(ids)[:100]:
            self.assertTrue(cs.is_web_session_id(sid))
            self.assertEqual(len(sid), 36)

    def test_foreign_and_malformed_ids_rejected(self):
        for sid in ("discord_123456789012345678", "discord_1_abcdefgh", "telegram_1", "zendesk_1",
                    "web_short", "web_" + "a" * 31, "web_" + "a" * 33, "WEB_" + "a" * 32,
                    "web_" + "a" * 31 + "/", str(__import__("uuid").uuid4()), "", None, 123):
            with self.subTest(sid=sid):
                self.assertFalse(cs.is_web_session_id(sid))

    def test_derived_key_is_separate(self):
        self.assertEqual(len(KEY), 32)
        self.assertNotEqual(KEY, CODE_KEY)
        self.assertNotEqual(KEY, OTHER)
        with self.assertRaises(ValueError):
            cs.derive_key(b"")


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.sid = cs.new_session_id()
        self.tok = cs.issue_token(KEY, self.sid, "bot-a", now=NOW)

    def test_valid_token(self):
        self.assertTrue(cs.verify_token(KEY, self.tok, self.sid, "bot-a", now=NOW + 10))

    def test_bound_to_session_bot_and_key(self):
        self.assertFalse(cs.verify_token(KEY, self.tok, cs.new_session_id(), "bot-a", now=NOW))
        self.assertFalse(cs.verify_token(KEY, self.tok, self.sid, "bot-b", now=NOW))
        self.assertFalse(cs.verify_token(OTHER, self.tok, self.sid, "bot-a", now=NOW))

    def test_expiry(self):
        self.assertTrue(cs.verify_token(KEY, self.tok, self.sid, "bot-a", now=NOW + cs.TOKEN_TTL_SECONDS - 1))
        self.assertFalse(cs.verify_token(KEY, self.tok, self.sid, "bot-a", now=NOW + cs.TOKEN_TTL_SECONDS))

    def test_tampering_and_malformed_tokens(self):
        v, exp, sig = self.tok.split(".")
        forged_expiry = f"{v}.{int(exp) + 10**6}.{sig}"
        flipped = f"{v}.{exp}.{('A' if sig[0] != 'A' else 'B') + sig[1:]}"
        for bad in (forged_expiry, flipped, f"v2.{exp}.{sig}", f"{v}.{exp}", f"{v}.-1.{sig}", f"{v}.１２.{sig}",
                    "", None, 5, "x" * 500, self.tok + ".x"):
            with self.subTest(bad=bad):
                self.assertFalse(cs.verify_token(KEY, bad, self.sid, "bot-a", now=NOW))

    def test_non_web_session_never_verifies(self):
        sid = "discord_123456789012345678"
        tok = cs.issue_token(KEY, sid, "bot-a", now=NOW)  # even a correctly signed one
        self.assertFalse(cs.verify_token(KEY, tok, sid, "bot-a", now=NOW))


class ResumeTests(unittest.TestCase):
    def test_resume_with_valid_token(self):
        sid = cs.new_session_id()
        tok = cs.issue_token(KEY, sid, "bot-a", now=NOW)
        self.assertEqual(cs.resume_or_new(KEY, sid, tok, "bot-a", now=NOW + 1), (sid, tok, True))

    def test_client_supplied_id_without_valid_token_gets_a_new_session(self):
        victim = cs.new_session_id()
        victim_tok = cs.issue_token(KEY, victim, "bot-a", now=NOW)
        cases = [(victim, None, "bot-a"), (victim, "garbage", "bot-a"), (victim, victim_tok, "bot-b"),
                 ("discord_123456789012345678", victim_tok, "bot-a"), (None, victim_tok, "bot-a")]
        for sid, tok, bot in cases:
            with self.subTest(sid=sid, bot=bot):
                new_sid, new_tok, resumed = cs.resume_or_new(KEY, sid, tok, bot, now=NOW)
                self.assertFalse(resumed)
                self.assertNotEqual(new_sid, victim)
                self.assertTrue(cs.is_web_session_id(new_sid))
                self.assertTrue(cs.verify_token(KEY, new_tok, new_sid, bot, now=NOW))


if __name__ == "__main__":
    unittest.main()
