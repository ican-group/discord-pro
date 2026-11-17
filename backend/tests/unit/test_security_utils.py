"""security_utils (spec 008 Stage 2): constant-time secret checks, sidebar-key hashing.
All values are synthetic."""
import unittest

from tests.unit import _support  # noqa: F401
import security_utils as su

SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"


class SecretMatchesTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(su.secret_matches(SECRET, SECRET))

    def test_mismatches(self):
        for provided in ("", "x", SECRET[:-1], SECRET + "x", SECRET.upper(), " " + SECRET, None, 123):
            with self.subTest(provided=provided):
                self.assertFalse(su.secret_matches(SECRET, provided))

    def test_missing_stored_secret_never_matches(self):
        for expected in (None, "", 0, b"bytes"):
            for provided in ("", su._DUMMY_SECRET, "anything"):
                with self.subTest(expected=expected, provided=provided):
                    self.assertFalse(su.secret_matches(expected, provided))

    def test_dummy_value_is_not_a_backdoor(self):
        self.assertFalse(su.secret_matches(None, su._DUMMY_SECRET))
        self.assertFalse(su.secret_matches("", su._DUMMY_SECRET))

    def test_oversized_input_rejected(self):
        long = "a" * (su.MAX_SECRET_LENGTH + 10)
        self.assertFalse(su.secret_matches(long, long))

    def test_unicode_safe(self):
        self.assertTrue(su.secret_matches("päss-🔑", "päss-🔑"))
        self.assertFalse(su.secret_matches("päss-🔑", "pass-🔑"))

    def test_uses_compare_digest(self):
        import inspect
        self.assertIn("hmac.compare_digest", inspect.getsource(su.secret_matches))


class SidebarKeyTests(unittest.TestCase):
    def test_new_key_format_and_uniqueness(self):
        keys = {su.new_sidebar_key() for _ in range(50)}
        self.assertEqual(len(keys), 50)
        for k in keys:
            self.assertTrue(k.startswith("zsk_"))
            self.assertGreaterEqual(len(k), 4 + 43)

    def test_stored_fields_contain_no_plaintext(self):
        key = su.new_sidebar_key()
        fields = su.sidebar_key_fields(key)
        self.assertEqual(set(fields), {su.SIDEBAR_KEY_HASH_FIELD, su.SIDEBAR_KEY_HINT_FIELD})
        self.assertNotIn(key, repr(fields))
        self.assertNotIn(key[4:20], repr(fields))
        self.assertRegex(fields[su.SIDEBAR_KEY_HASH_FIELD], r"^[0-9a-f]{64}$")
        self.assertEqual(fields[su.SIDEBAR_KEY_HINT_FIELD], "zsk_…" + key[-4:])

    def test_hash_deterministic_and_namespaced(self):
        import hashlib
        key = "zsk_" + "A" * 43
        self.assertEqual(su.hash_sidebar_key(key), su.hash_sidebar_key(key))
        self.assertNotEqual(su.hash_sidebar_key(key), hashlib.sha256(key.encode()).hexdigest())

    def test_candidate_hash(self):
        key = su.new_sidebar_key()
        self.assertEqual(su.sidebar_key_candidate_hash(f"  {key} "), su.hash_sidebar_key(key))
        for bad in (None, "", "   ", "abc", "zsk_" + "x" * 200, key.replace("zsk_", "zsk-")):
            with self.subTest(bad=bad):
                self.assertIsNone(su.sidebar_key_candidate_hash(bad))

    def test_masked_value_is_not_a_usable_key(self):
        key = su.new_sidebar_key()
        masked = su.masked_sidebar_key(su.sidebar_key_hint(key))
        self.assertNotIn(key, masked)
        self.assertNotEqual(su.sidebar_key_candidate_hash(masked), su.hash_sidebar_key(key))
        self.assertIn("hidden", masked)


if __name__ == "__main__":
    unittest.main()
