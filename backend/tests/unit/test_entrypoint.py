"""docker-entrypoint.sh secret loading (spec 004, C9, R8). Runs the script with /bin/sh."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit._support import BACKEND_DIR

ENTRYPOINT = BACKEND_DIR / "docker-entrypoint.sh"
SECRET = "unit-test-secret-value-0123456789abcdef"  # synthetic, not a real credential

# Prints only lengths/flags, never the values themselves.
PROBE = ('printf "len=%s file_set=%s jwt=%s\\n" "${#MONGO_URL}" "${MONGO_URL_FILE:+yes}" '
         '"${JWT_SECRET:+yes}"')


class EntrypointTests(unittest.TestCase):
    def _secret_file(self, content):
        d = tempfile.mkdtemp()
        p = Path(d) / "secret"
        p.write_text(content, encoding="utf-8")
        self.addCleanup(lambda: (p.unlink(missing_ok=True), os.rmdir(d)))
        return str(p)

    def _run(self, env_extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(env_extra)
        return subprocess.run(["/bin/sh", str(ENTRYPOINT), "/bin/sh", "-c", PROBE],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_loads_secret_from_file_and_unsets_file_var(self):
        r = self._run({"MONGO_URL_FILE": self._secret_file(SECRET + "\n")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"len={len(SECRET)} file_set= jwt=", r.stdout)
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_unset_file_vars_are_ignored(self):
        r = self._run({})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("len=0", r.stdout)

    def test_conflict_between_value_and_file(self):
        r = self._run({"MONGO_URL": "x", "MONGO_URL_FILE": self._secret_file(SECRET)})
        self.assertEqual(r.returncode, 64)
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_unreadable_file(self):
        r = self._run({"JWT_SECRET_FILE": "/nonexistent/secret"})
        self.assertEqual(r.returncode, 66)
        self.assertIn("JWT_SECRET_FILE", r.stderr)

    def test_empty_file(self):
        r = self._run({"ENCRYPTION_KEY_FILE": self._secret_file("\n")})
        self.assertEqual(r.returncode, 65)

    def test_code_hmac_key_loaded_from_file(self):
        """spec 008 Stage 3: CODE_HMAC_KEY is on the allowlist; value never echoed."""
        r = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), "/bin/sh", "-c",
             'printf "len=%s file=%s" "${#CODE_HMAC_KEY}" "${CODE_HMAC_KEY_FILE:+set}"'],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "CODE_HMAC_KEY_FILE": self._secret_file(SECRET + "\n")},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"len={len(SECRET)} file=")
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_code_hmac_key_empty_file_rejected(self):
        r = self._run({"CODE_HMAC_KEY_FILE": self._secret_file("\n")})
        self.assertEqual(r.returncode, 65)
        self.assertIn("CODE_HMAC_KEY_FILE", r.stderr)

    def test_optional_resend_api_key_loaded_from_file(self):
        r = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), "/bin/sh", "-c",
             'printf "len=%s file=%s" "${#RESEND_API_KEY}" "${RESEND_API_KEY_FILE:+set}"'],
            env={"PATH": "/usr/bin:/bin", "RESEND_API_KEY_FILE": self._secret_file(SECRET + "\n")},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"len={len(SECRET)} file=")
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_resend_api_key_absent_is_fine(self):
        r = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), "/bin/sh", "-c", 'printf "len=%s" "${#RESEND_API_KEY}"'],
            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "len=0")

    def test_admin_password_is_not_loaded_into_env(self):
        # The bootstrap reads its password file directly; it never becomes an env var.
        r = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), "/bin/sh", "-c", 'printf "%s" "${ADMIN_PASSWORD:+set}"'],
            env={"PATH": "/usr/bin:/bin", "ADMIN_PASSWORD_FILE": self._secret_file(SECRET)},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout, "")

    def test_script_has_no_tracing_and_is_executable(self):
        text = ENTRYPOINT.read_text()
        self.assertNotRegex(text, r"(?m)^\s*set\s+-[a-z]*x")
        self.assertTrue(os.access(ENTRYPOINT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
