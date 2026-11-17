"""Frontend/backend password-policy parity (spec 008, R-N6.1, Stage 9b).

The frontend mirrors `check_user_password` in frontend/src/utils/passwordPolicy.js
and is tested against vectors generated from this backend. This host-only test
fails if the backend policy changes without the frontend copy and vectors being
updated. Skipped inside the Docker build (frontend/ is outside the context).
"""
import json
import re
import unittest

from tests.unit._support import BACKEND_DIR
import password_policy as pp

FRONTEND = BACKEND_DIR.parent / "frontend" / "src" / "utils"
POLICY_JS = FRONTEND / "passwordPolicy.js"
VECTORS = FRONTEND / "testing" / "passwordPolicyVectors.json"


@unittest.skipUnless(POLICY_JS.is_file() and VECTORS.is_file(), "frontend sources not available (Docker build context)")
class PasswordPolicyParityTests(unittest.TestCase):
    def test_frontend_policy_matches_backend(self):
        js = POLICY_JS.read_text(encoding="utf-8")
        consts = {name: int(m.group(1)) for name in ("USER_MIN_LENGTH", "MAX_BYTES", "USER_MIN_DISTINCT")
                  for m in [re.search(rf"export const {name} = (\d+);", js)] if m}
        self.assertEqual(consts, {"USER_MIN_LENGTH": pp.USER_MIN_LENGTH, "MAX_BYTES": pp.MAX_LENGTH,
                                  "USER_MIN_DISTINCT": pp.USER_MIN_DISTINCT})
        deny_src = re.search(r"export const DENYLIST = \[([\s\S]*?)\];", js).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', deny_src)), pp.DENYLIST)
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
        self.assertGreaterEqual(len(vectors), 15)
        for v in vectors:
            with self.subTest(password=v["password"][:20]):
                self.assertEqual(pp.check_user_password(v["password"], v["email"]), v["problems"])
                self.assertEqual(pp.user_password_error(v["password"], v["email"]), v["error"])


if __name__ == "__main__":
    unittest.main()
