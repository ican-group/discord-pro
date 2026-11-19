"""Authentication and sessions (spec 012 §9; Phase 4A)."""
import asyncio
import json
import time
import unittest

from tests.unit._api_fixtures import HAVE_API, JWT_SECRET, World

CSRF = {"X-Requested-With": "BitBotPro"}
GOOD_PASSWORD = "Correct-Horse-Battery-91"
NEW_PASSWORD = "Another-Quiet-Lantern-73"


class RecordingMailer:
    def __init__(self):
        self.sent = []

    async def send_password_reset(self, email, reset_url):
        self.sent.append((email, reset_url))


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class AuthBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from api import passwords
        from core.identity import USERS
        self.w = await World().build()
        self.w.S.settings.cookie_secure = False
        self.mailer = RecordingMailer()
        self.w.S.mailer = self.mailer
        for key in ("viewer", "admin"):
            await self.w.store.update(USERS, self.w.users[key].id, {"password_hash": passwords.hash_password(GOOD_PASSWORD)})

    async def raw(self, method, path, json_body=None, headers=None, cookies=None):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test",
                                     cookies=cookies) as c:
            return await c.request(method, "/api" + path, json=json_body, headers=headers or {})

    async def login(self, email="viewer@example.test", password=GOOD_PASSWORD, headers=CSRF):
        return await self.raw("POST", "/auth/login", {"email": email, "password": password}, headers)

    def cookie_of(self, response):
        header = response.headers.get("set-cookie", "")
        return header.split(";", 1)[0].split("=", 1)[1] if "bbp_session=" in header else None


class SessionTokenTests(AuthBase):
    def token(self, **claims):
        import jwt
        u = self.w.users["viewer"]
        now = int(time.time())
        payload = {"sub": u.id, "tv": u.token_version, "typ": "s012", "sid": self.sid, "iat": now, "exp": now + 600,
                   **claims}
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from core.identity import create_session
        self.sid = (await create_session(self.w.store, self.w.users["viewer"], now=self.w.S.clock(),
                                         ttl_seconds=600))["id"]

    async def get(self, token):
        return await self.raw("GET", f"/enterprises/{self.w.eA.id}",
                              headers={"Authorization": "Bearer " + token} if token else {})

    async def test_valid_session(self):
        r = await self.get(self.token())
        self.assertEqual((r.status_code, r.json()["role"]), (200, "viewer"))

    async def test_invalid_sessions_are_rejected(self):
        import jwt
        now = int(time.time())
        u = self.w.users["viewer"]
        bad = {
            "missing": None, "garbage": "not-a-jwt",
            "wrong_secret": jwt.encode({"sub": u.id, "tv": 0, "typ": "s012", "sid": self.sid, "iat": now,
                                        "exp": now + 60}, "another-secret-that-is-long-enough-000", algorithm="HS256"),
            "expired": self.token(iat=now - 100, exp=now - 10),
            "legacy_token_without_type": self.token(typ=None),
            "no_session_id": self.token(sid=None),
            "unknown_session": self.token(sid="00000000-0000-4000-8000-000000000000"),
            "wrong_version": self.token(tv=5),
            "boolean_version": self.token(tv=False),
            "unknown_user": self.token(sub="00000000-0000-4000-8000-000000000000"),
            "none_alg": jwt.encode({"sub": u.id, "tv": 0, "typ": "s012", "sid": self.sid, "iat": now, "exp": now + 60},
                                   None, algorithm="none"),
        }
        for name, value in bad.items():
            with self.subTest(name):
                r = await self.get(value)
                self.assertEqual((r.status_code, r.json()["detail"]), (401, "Authentication required."))

    async def test_session_of_another_user_is_rejected(self):
        from core.identity import create_session
        other = (await create_session(self.w.store, self.w.users["admin"], now=self.w.S.clock(), ttl_seconds=600))["id"]
        self.assertEqual((await self.get(self.token(sid=other))).status_code, 401)

    async def test_disabled_or_deleted_users_lose_access(self):
        from core.identity import USERS
        token = self.token()
        await self.w.store.update(USERS, self.w.users["viewer"].id, {"status": "disabled"})
        self.assertEqual((await self.get(token)).status_code, 401)
        await self.w.store.delete(USERS, self.w.users["viewer"].id)
        self.assertEqual((await self.get(token)).status_code, 401)

    async def test_superadmin_status_comes_from_the_database(self):
        from core.identity import USERS
        h = self.w.headers("outsider")
        self.assertEqual((await self.w.call("GET", f"/enterprises/{self.w.eA.id}", headers=h)).status_code, 404)
        await self.w.store.update(USERS, self.w.users["outsider"].id, {"is_platform_superadmin": True})
        self.assertEqual((await self.w.call("GET", f"/enterprises/{self.w.eA.id}", headers=h)).status_code, 200)

    async def test_tenant_headers_never_grant_access(self):
        h = {**self.w.headers("owner_b"), "X-Instance-ID": self.w.iA_web.id, "X-Org-ID": self.w.oA.id,
             "X-Workspace-ID": self.w.eA.id, "X-Enterprise-ID": self.w.eA.id}
        for path in (f"/enterprises/{self.w.eA.id}", f"/organisations/{self.w.oA.id}", f"/bots/{self.w.bA.id}",
                     f"/instances/{self.w.iA_web.id}/config", f"/credentials/{self.w.cA_tg.id}"):
            with self.subTest(path):
                self.assertEqual((await self.w.call("GET", path, headers=h)).status_code, 404)


class LoginLogoutTests(AuthBase):
    async def test_login_sets_a_secure_cookie_and_logout_revokes_only_that_session(self):
        self.w.S.settings.cookie_secure = True
        r = await self.login()
        self.assertEqual(r.status_code, 200, r.text)
        header = r.headers["set-cookie"].lower()
        for attr in ("httponly", "secure", "samesite=strict", "path=/api", "max-age="):
            self.assertIn(attr, header)
        self.assertNotIn("password", r.text)
        first = self.cookie_of(r)
        second = self.cookie_of(await self.login())
        me = await self.raw("GET", "/auth/session", cookies={"bbp_session": first})
        self.assertEqual(me.json()["user"]["email"], "viewer@example.test")
        r = await self.raw("POST", "/auth/logout", headers=CSRF, cookies={"bbp_session": first})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": first})).status_code, 401)
        self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": second})).status_code, 200)

    async def test_logout_all_ends_every_session(self):
        a = self.cookie_of(await self.login())
        b = self.cookie_of(await self.login())
        r = await self.raw("POST", "/auth/logout-all", headers=CSRF, cookies={"bbp_session": a})
        self.assertEqual(r.json()["sessions_ended"], 3)          # two logins + the fixture's session
        for c in (a, b):
            self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": c})).status_code, 401)
        r = await self.w.call("GET", "/auth/session", "viewer")              # bearer session ended too
        self.assertEqual(r.status_code, 401)

    async def test_failed_logins_do_not_reveal_accounts(self):
        wrong = await self.login(password="Wrong-Password-1234")
        unknown = await self.login(email="nobody@example.test")
        malformed = await self.login(email="not-an-email")
        for r in (wrong, unknown, malformed):
            self.assertEqual((r.status_code, r.json()["detail"]), (401, "Invalid email or password."))
            self.assertNotIn("set-cookie", r.headers)

    async def test_disabled_users_cannot_log_in(self):
        from core.identity import USERS
        await self.w.store.update(USERS, self.w.users["viewer"].id, {"status": "disabled"})
        self.assertEqual((await self.login()).status_code, 401)

    async def test_csrf_header_required_for_cookie_flows(self):
        r = await self.login(headers={})
        self.assertEqual(r.status_code, 403)
        cookie = self.cookie_of(await self.login())
        for path in ("/auth/logout", "/auth/logout-all", f"/enterprises/{self.w.eA.id}/organisations"):
            r = await self.raw("POST", path, {"name": "x"} if "organisations" in path else None,
                               cookies={"bbp_session": cookie})
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": cookie})).status_code, 200)

    async def test_login_throttling_per_account_and_ip(self):
        from api.auth import LOGIN_ACCOUNT
        for _ in range(LOGIN_ACCOUNT[0]):
            self.assertEqual((await self.login(password="Wrong-Password-1234")).status_code, 401)
        r = await self.login()                            # correct password, but the account is locked
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["retry-after"]), 0)
        self.assertEqual((await self.login(email="admin@example.test")).status_code, 200)   # other account fine
        stored = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn("viewer@example.test", stored.split('"auth_throttle"')[-1][:5000])  # keys are hashed

    async def test_ip_throttle(self):
        from api.auth import LOGIN_IP
        for i in range(LOGIN_IP[0]):
            await self.login(email=f"spray{i}@example.test", password="Wrong-Password-1234")
        self.assertEqual((await self.login()).status_code, 429)

    async def test_concurrent_failures_never_exceed_the_limit(self):
        from api.auth import LOGIN_ACCOUNT
        results = await asyncio.gather(*[self.login(password="Wrong-Password-1234") for _ in range(12)])
        self.assertEqual(sum(1 for r in results if r.status_code == 401), 12 - sum(
            1 for r in results if r.status_code == 429))
        doc = [d for d in (await self.w.store.find("auth_throttle", {})) if d["id"].startswith("login-acct:")][0]
        self.assertLessEqual(doc["count"], LOGIN_ACCOUNT[0])


class PasswordTests(AuthBase):
    async def test_change_password_ends_other_sessions_and_keeps_this_client(self):
        a = self.cookie_of(await self.login())
        b = self.cookie_of(await self.login())
        r = await self.raw("POST", "/auth/change-password", {"current_password": "Wrong-Password-1234",
                                                              "new_password": NEW_PASSWORD}, CSRF, {"bbp_session": a})
        self.assertEqual(r.status_code, 403)
        r = await self.raw("POST", "/auth/change-password", {"current_password": GOOD_PASSWORD,
                                                              "new_password": "short"}, CSRF, {"bbp_session": a})
        self.assertEqual(r.status_code, 422)
        r = await self.raw("POST", "/auth/change-password", {"current_password": GOOD_PASSWORD,
                                                              "new_password": NEW_PASSWORD}, CSRF, {"bbp_session": a})
        self.assertEqual(r.status_code, 200, r.text)
        fresh = self.cookie_of(r)
        for c in (a, b):
            self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": c})).status_code, 401)
        self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": fresh})).status_code, 200)
        self.assertEqual((await self.login()).status_code, 401)
        self.assertEqual((await self.login(password=NEW_PASSWORD)).status_code, 200)
        stored = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(NEW_PASSWORD, stored)

    async def test_password_reset_flow(self):
        unknown = await self.raw("POST", "/auth/forgot-password", {"email": "nobody@example.test"}, CSRF)
        known = await self.raw("POST", "/auth/forgot-password", {"email": "viewer@example.test"}, CSRF)
        self.assertEqual((unknown.status_code, unknown.json()), (known.status_code, known.json()))   # no oracle
        self.assertEqual(len(self.mailer.sent), 1)
        token = self.mailer.sent[0][1].split("token=", 1)[1]
        stored = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(token, stored)                                   # only the hash is stored
        session = self.cookie_of(await self.login())
        r = await self.raw("POST", "/auth/reset-password", {"token": token, "new_password": "weak"}, CSRF)
        self.assertEqual(r.status_code, 422)                              # policy failure does not spend the token
        results = await asyncio.gather(*[self.raw("POST", "/auth/reset-password",
                                                  {"token": token, "new_password": NEW_PASSWORD}, CSRF)
                                         for _ in range(4)])
        self.assertEqual(sorted(r.status_code for r in results), [200, 400, 400, 400])   # single use
        self.assertEqual((await self.raw("GET", "/auth/session", cookies={"bbp_session": session})).status_code, 401)
        self.assertEqual((await self.login(password=NEW_PASSWORD)).status_code, 200)

    async def test_reset_tokens_expire(self):
        await self.raw("POST", "/auth/forgot-password", {"email": "viewer@example.test"}, CSRF)
        token = self.mailer.sent[0][1].split("token=", 1)[1]
        doc = (await self.w.store.find("password_resets", {}))[0]
        await self.w.store.update("password_resets", doc["id"], {"expires_at": "2020-01-01T00:00:00+00:00"})
        r = await self.raw("POST", "/auth/reset-password", {"token": token, "new_password": NEW_PASSWORD}, CSRF)
        self.assertEqual(r.status_code, 400)

    async def test_reset_answers_are_identical_even_when_e_mail_delivery_fails(self):
        """Phase 6B security review: a failing mail provider must not turn an existing account into
        a 500 (oracle), and the provider call must not run before the answer is computed."""
        import time as _t
        from api.auth import AUTH_RESPONSE_FLOOR_MS

        class Failing:
            calls = 0

            async def send_password_reset(self, email, url):
                Failing.calls += 1
                raise RuntimeError("provider down: " + url)       # the link must never reach a log

        self.w.S.mailer = Failing()
        with self.assertLogs("bitbotpro.auth", level="WARNING") as logs:
            t0 = _t.monotonic()
            known = await self.raw("POST", "/auth/forgot-password", {"email": "viewer@example.test"}, CSRF)
            t_known = _t.monotonic() - t0
            t0 = _t.monotonic()
            unknown = await self.raw("POST", "/auth/forgot-password", {"email": "nobody@example.test"}, CSRF)
            t_unknown = _t.monotonic() - t0
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(known.status_code, 202)
        self.assertEqual(Failing.calls, 1)                               # attempted once, in the background
        self.assertNotIn("token=", "\n".join(logs.output))
        for t in (t_known, t_unknown):
            self.assertGreaterEqual(t, AUTH_RESPONSE_FLOOR_MS / 1000 - 0.01)   # response floor

    async def test_reset_requests_are_throttled(self):
        from api.auth import RESET_ACCOUNT
        for _ in range(RESET_ACCOUNT[0]):
            await self.raw("POST", "/auth/forgot-password", {"email": "viewer@example.test"}, CSRF)
        r = await self.raw("POST", "/auth/forgot-password", {"email": "viewer@example.test"}, CSRF)
        self.assertEqual(r.status_code, 429)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_safeguards(self):
        from api.bootstrap import run
        from core.hierarchy import InMemoryStore
        from core.identity import IdentityError, get_user
        store = InMemoryStore()
        with self.assertRaises(ValueError):
            await run(store, email="root@example.test", name="R", password="short", now="2026-11-18T00:00:00+00:00")
        results = await asyncio.gather(*[run(store, email=f"root{i}@example.test", name="R",
                                             password="Granite-Orchard-Lighthouse-4821",
                                             now="2026-11-18T00:00:00+00:00") for i in range(3)],
                                       return_exceptions=True)
        created = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(created), 1)                                 # concurrent bootstraps: exactly one
        self.assertTrue(all(isinstance(r, IdentityError) for r in results if isinstance(r, BaseException)))
        user = await get_user(store, created[0].id)
        self.assertTrue(user.is_platform_superadmin and user.email_verified)
        self.assertEqual((await store.get("platform_state", "superadmins"))["count"], 1)
        with self.assertRaises(IdentityError):
            await run(store, email="again@example.test", name="R", password="Granite-Orchard-Lighthouse-4821",
                      now="2026-11-18T00:00:00+00:00")
        self.assertNotIn("Granite-Orchard-Lighthouse-4821", json.dumps(store.collections, default=str))


if __name__ == "__main__":
    unittest.main()
