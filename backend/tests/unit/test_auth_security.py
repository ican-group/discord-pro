"""Account-takeover primitives (spec 008 Stage 3: P2, N5, N6). Offline, synthetic values."""
import unittest
from datetime import datetime, timedelta, timezone

from tests.unit import _support  # noqa: F401
import auth_security as a
import password_policy as pp
import runtime_config as rc

KEY = b"unit-test-code-hmac-key-not-real-abcdefghijklmnop"
OTHER_KEY = b"another-unit-test-key-not-real-0123456789abcdefg"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def issued(purpose=a.PURPOSE_RESET, user_id="u-1", key=KEY, now=NOW):
    code, set_f, unset_f = a.issue_code(key, purpose, user_id, now=now)
    f = a.FIELDS_BY_PURPOSE[purpose]
    user = {"id": user_id, **set_f}
    return code, set_f, unset_f, user, f


class CodeIssueTests(unittest.TestCase):
    def test_code_format_and_randomness(self):
        codes = {a.generate_code() for _ in range(2000)}
        self.assertGreater(len(codes), 1900)
        for c in list(codes)[:200]:
            self.assertRegex(c, r"^[0-9]{6}$")

    def test_only_hash_id_expiry_stored(self):
        code, set_f, unset_f, _, f = issued()
        self.assertEqual(set(set_f), {f.hash, f.id, f.expiry})
        self.assertNotIn(code, repr(set_f.values()).replace(set_f[f.hash], ""))
        self.assertRegex(set_f[f.hash], r"^[0-9a-f]{64}$")
        self.assertEqual(unset_f, {f.legacy_plaintext: ""})
        self.assertEqual(datetime.fromisoformat(set_f[f.expiry]), NOW + a.CODE_TTL)

    def test_each_issuance_has_new_id_and_hash(self):
        _, s1, *_ = issued()
        _, s2, *_ = issued()
        f = a.RESET_FIELDS
        self.assertNotEqual(s1[f.id], s2[f.id])
        self.assertNotEqual(s1[f.hash], s2[f.hash])

    def test_missing_key_refused(self):
        with self.assertRaises(ValueError):
            a.hash_code(b"", "reset", "u", "c", "123456")


class CodeMatchTests(unittest.TestCase):
    def test_correct_code_matches(self):
        code, _, _, user, _ = issued()
        stored = a.stored_code(user, a.PURPOSE_RESET)
        self.assertTrue(a.code_matches(KEY, a.PURPOSE_RESET, "u-1", stored, code, now=NOW))
        self.assertTrue(a.code_matches(KEY, a.PURPOSE_RESET, "u-1", stored, f" {code} ", now=NOW))

    def test_wrong_and_malformed_codes(self):
        code, _, _, user, _ = issued()
        stored = a.stored_code(user, a.PURPOSE_RESET)
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        for bad in (wrong, "", "12345", "1234567", "abcdef", "١٢٣٤٥٦", None, 123456, code + "\n0"):
            with self.subTest(bad=bad):
                self.assertFalse(a.code_matches(KEY, a.PURPOSE_RESET, "u-1", stored, bad, now=NOW))

    def test_expired_code_never_matches(self):
        code, _, _, user, _ = issued()
        stored = a.stored_code(user, a.PURPOSE_RESET)
        self.assertTrue(a.code_matches(KEY, a.PURPOSE_RESET, "u-1", stored, code,
                                       now=NOW + a.CODE_TTL - timedelta(seconds=1)))
        for later in (a.CODE_TTL, a.CODE_TTL + timedelta(days=1)):
            self.assertFalse(a.code_matches(KEY, a.PURPOSE_RESET, "u-1", stored, code, now=NOW + later))

    def test_bound_to_purpose_user_key_and_issuance(self):
        code, _, _, user, f = issued()
        stored = a.stored_code(user, a.PURPOSE_RESET)
        self.assertFalse(a.code_matches(KEY, a.PURPOSE_VERIFY, "u-1", stored, code, now=NOW))
        self.assertFalse(a.code_matches(KEY, a.PURPOSE_RESET, "u-2", stored, code, now=NOW))
        self.assertFalse(a.code_matches(OTHER_KEY, a.PURPOSE_RESET, "u-1", stored, code, now=NOW))
        replaced = a.StoredCode("another-issuance", stored.code_hash, stored.expires_at)
        self.assertFalse(a.code_matches(KEY, a.PURPOSE_RESET, "u-1", replaced, code, now=NOW))

    def test_no_usable_code(self):
        for user in (None, {}, {"id": "u"}, {"id": "u", "reset_code": "123456"},          # legacy plaintext only
                     {"id": "u", "reset_code_hash": "x", "reset_code_id": "i"},              # no expiry
                     {"id": "u", "reset_code_hash": "x", "reset_code_id": "i", "reset_code_expires": "nope"}):
            with self.subTest(user=user):
                self.assertIsNone(a.stored_code(user, a.PURPOSE_RESET))

    def test_naive_and_datetime_expiry_accepted(self):
        user = {"id": "u", "reset_code_hash": "h", "reset_code_id": "i",
                "reset_code_expires": (NOW + a.CODE_TTL).replace(tzinfo=None).isoformat()}
        self.assertEqual(a.stored_code(user, a.PURPOSE_RESET).expires_at, NOW + a.CODE_TTL)
        user["reset_code_expires"] = NOW + a.CODE_TTL
        self.assertEqual(a.stored_code(user, a.PURPOSE_RESET).expires_at, NOW + a.CODE_TTL)

    def test_clear_fields_removes_current_and_legacy(self):
        f = a.RESET_FIELDS
        self.assertEqual(set(a.clear_code_fields(a.PURPOSE_RESET)), {f.hash, f.id, f.expiry, f.legacy_plaintext})

    def test_sensitive_fields_cover_code_material(self):
        for name in ("verification_code_hash", "verification_code_id", "reset_code_hash", "reset_code_id",
                     "verification_code", "reset_code", "hashed_password"):
            self.assertIn(name, a.SENSITIVE_USER_FIELDS)

    def test_uses_constant_time_compare(self):
        import inspect
        self.assertIn("hmac.compare_digest", inspect.getsource(a.code_matches))


class TokenVersionTests(unittest.TestCase):
    def test_legacy_token_without_tv_matches_version_zero(self):
        self.assertTrue(a.token_version_matches({"sub": "u"}, {"id": "u"}))
        self.assertTrue(a.token_version_matches({"sub": "u"}, {"id": "u", "token_version": 0}))

    def test_bumped_version_revokes_old_tokens(self):
        user = {"id": "u", "token_version": 3}
        self.assertTrue(a.token_version_matches({"tv": 3}, user))
        for tv in (0, 2, 4, -1):
            self.assertFalse(a.token_version_matches({"tv": tv}, user))
        self.assertFalse(a.token_version_matches({}, user))

    def test_malformed_claims_and_users_rejected(self):
        for tv in ("3", 3.0, True, None, [3]):
            with self.subTest(tv=tv):
                self.assertFalse(a.token_version_matches({"tv": tv}, {"token_version": 3}))
        self.assertFalse(a.token_version_matches({"tv": 0}, None))
        self.assertFalse(a.token_version_matches({"tv": 0}, {"token_version": "garbage"}))


class CodeKeyConfigTests(unittest.TestCase):
    def test_required_in_production(self):
        for env in ({}, {"CODE_HMAC_KEY": ""}, {"CODE_HMAC_KEY": "   "}):
            with self.subTest(env=env):
                with self.assertRaises(rc.ConfigError):
                    rc.get_code_hmac_key(env, rc.APP_ENV_PRODUCTION)

    def test_ephemeral_only_in_development(self):
        key, ephemeral = rc.get_code_hmac_key({}, rc.APP_ENV_DEVELOPMENT)
        self.assertTrue(ephemeral)
        self.assertEqual(len(key), 32)
        self.assertNotEqual(key, rc.get_code_hmac_key({}, rc.APP_ENV_DEVELOPMENT)[0])

    def test_length_and_separation(self):
        with self.assertRaises(rc.ConfigError):
            rc.get_code_hmac_key({"CODE_HMAC_KEY": "x" * 31}, rc.APP_ENV_PRODUCTION)
        same = "s" * 40
        with self.assertRaises(rc.ConfigError):
            rc.get_code_hmac_key({"CODE_HMAC_KEY": same}, rc.APP_ENV_PRODUCTION, jwt_secret=same)
        key, eph = rc.get_code_hmac_key({"CODE_HMAC_KEY": " " + KEY.decode() + "\n"}, rc.APP_ENV_PRODUCTION,
                                        jwt_secret="different-jwt-secret-value-000000")
        self.assertEqual((key, eph), (KEY, False))

    def test_error_never_contains_key(self):
        short = "secret-but-short"
        with self.assertRaises(rc.ConfigError) as cm:
            rc.get_code_hmac_key({"CODE_HMAC_KEY": short}, rc.APP_ENV_PRODUCTION)
        self.assertNotIn(short, str(cm.exception))


class UserPasswordPolicyTests(unittest.TestCase):
    EMAIL = "jane.doe@example.org"

    def test_accepts_reasonable_passwords(self):
        for pw in ("Vq3!tz-Kp9#mW2rL", "correct horse battery staple", "zq8tLm4vRw7nPk"):
            with self.subTest(pw=pw):
                self.assertEqual(pp.check_user_password(pw, self.EMAIL), [])
                self.assertIsNone(pp.user_password_error(pw, self.EMAIL))

    def test_rejects_weak_passwords(self):
        for pw in ("", "short", "x" * 12, "aaaaaabbbbbb", "Password1234!", "jane.doe-rocks-2026",
                   "Example2026!xyz", " leadingspace1Q", "trailingspace1Q ", "é" * 40):
            with self.subTest(pw=pw):
                self.assertTrue(pp.check_user_password(pw, self.EMAIL))

    def test_error_message_never_contains_password(self):
        pw = "Password1234!"
        msg = pp.user_password_error(pw, self.EMAIL)
        self.assertIsNotNone(msg)
        self.assertNotIn(pw, msg)

    def test_bootstrap_policy_unchanged_and_stricter(self):
        self.assertTrue(pp.check_password("zq8tLm4vRw7nPk", "admin@bitbotpro.io"))  # 14 chars < 16
        self.assertEqual(pp.MIN_LENGTH, 16)
        self.assertEqual(pp.USER_MIN_LENGTH, 12)


if __name__ == "__main__":
    unittest.main()
