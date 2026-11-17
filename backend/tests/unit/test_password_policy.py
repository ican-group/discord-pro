"""Strong-password policy for the superadmin bootstrap (spec 004, C6)."""
import unittest

from tests.unit import _support  # noqa: F401
from password_policy import check_password

EMAIL = "admin@bitbotpro.io"
# Synthetic test value only (not a real credential).
STRONG = "Tq7#vLm2!pZx9&Rw"


class PasswordPolicyTests(unittest.TestCase):
    def test_strong_password_accepted(self):
        self.assertEqual(check_password(STRONG, EMAIL), [])

    def test_long_passphrase_without_symbols_accepted(self):
        self.assertEqual(check_password("correct horse battery staple zebra", EMAIL), [])

    def test_too_short(self):
        self.assertTrue(check_password("Tq7#vLm2!pZ", EMAIL))

    def test_known_defaults_rejected(self):
        for pw in ("ChangeMe123!ChangeMe123!", "SuperPassword#2026xyz", "Admin!Strong#9876xq",
                   "MyBitBotPro#2026!!xq", "Bridgebot#2026!!xqz"):
            with self.subTest(pw=pw):
                self.assertTrue(check_password(pw, "ops@example.org"))

    def test_email_parts_rejected(self):
        self.assertTrue(check_password("Zq9!nevillefXy7#Lp", "nevillef@example.org"))
        self.assertTrue(check_password("Zq9!exampleXy7#Lpk", "ops@example.org"))

    def test_character_classes(self):
        self.assertTrue(check_password("tq7vlm2pzx9rwkjd", EMAIL))  # 16 chars, 2 classes

    def test_whitespace_edges_rejected(self):
        self.assertTrue(check_password(" " + STRONG, EMAIL))

    def test_low_distinct_characters(self):
        self.assertTrue(check_password("Aa1!Aa1!Aa1!Aa1!Aa1!", EMAIL))

    def test_bcrypt_byte_limit(self):
        self.assertTrue(check_password(STRONG * 5, EMAIL))

    def test_empty(self):
        self.assertTrue(check_password("", EMAIL))

    def test_reasons_never_contain_password(self):
        for pw in ("short", "ChangeMe123!ChangeMe123!", "tq7vlm2pzx9rwkjd"):
            for reason in check_password(pw, EMAIL):
                self.assertNotIn(pw, reason)


if __name__ == "__main__":
    unittest.main()
