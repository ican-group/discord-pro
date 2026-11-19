"""F-A at the API: every throttle consumer still enforces its limit with the
atomic sliding-window counters, per key (IP, account, session, instance,
integration, user) and per tenant."""
import unittest

from tests.unit._api_fixtures import HAVE_API, World


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class RateLimitSweep(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    async def post(self, path, body, *, ip="203.0.113.9", headers=None):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app, client=(ip, 5000)),
                                     base_url="http://test") as c:
            return await c.post("/api" + path, json=body, headers={"X-Requested-With": "BitBotPro", **(headers or {})})

    async def until_429(self, make, attempts):
        codes = [(await make(i)).status_code for i in range(attempts)]
        return codes, codes.index(429) if 429 in codes else None

    async def test_login_account_and_ip_limits(self):
        from api.auth import LOGIN_ACCOUNT, LOGIN_IP
        codes, first = await self.until_429(
            lambda i: self.post("/auth/login", {"email": "owner@example.test", "password": "wrong-password-1"},
                                ip=f"198.51.100.{i + 1}"), LOGIN_ACCOUNT[0] + 3)
        self.assertEqual(first, LOGIN_ACCOUNT[0])                               # per account, across IPs
        codes, first = await self.until_429(
            lambda i: self.post("/auth/login", {"email": f"nobody{i}@example.test", "password": "wrong-password-1"}),
            LOGIN_IP[0] + 3)
        self.assertEqual(first, LOGIN_IP[0])                                    # per IP, across accounts
        r = await self.post("/auth/login", {"email": "admin@example.test", "password": "wrong-password-1"},
                            ip="192.0.2.77")
        self.assertNotEqual(r.status_code, 429)                                 # another IP and account: unaffected

    async def test_password_reset_limits(self):
        from api.auth import RESET_ACCOUNT, RESET_IP
        codes, first = await self.until_429(
            lambda i: self.post("/auth/forgot-password", {"email": "viewer@example.test"}, ip=f"198.51.100.{i + 1}"),
            RESET_ACCOUNT[0] + 3)
        self.assertTrue(first is None or first >= RESET_ACCOUNT[0])             # account limit (non-disclosing)
        codes, first = await self.until_429(
            lambda i: self.post("/auth/forgot-password", {"email": f"x{i}@example.test"}), RESET_IP[0] + 3)
        self.assertEqual(first, RESET_IP[0])

    async def test_webchat_ip_session_and_instance_limits(self):
        from api.webchat import LIMIT_CHAT_IP, LIMIT_PER_SESSION, LIMIT_SESSION_CREATE_IP
        P = f"/public/instances/{self.w.iA_web.channel.public_id}"
        h = {"Origin": "https://shop.example"}
        codes, first = await self.until_429(lambda i: self.post(P + "/sessions", {}, headers=h),
                                            LIMIT_SESSION_CREATE_IP[0] + 3)
        self.assertEqual(first, LIMIT_SESSION_CREATE_IP[0])
        s = (await self.post(P + "/sessions", {}, ip="192.0.2.1", headers=h)).json()
        codes, first = await self.until_429(
            lambda i: self.post(P + "/chat", {"message": "hi", "session_id": s["session_id"]}, ip=f"192.0.2.{i + 2}",
                                headers={**h, "X-Chat-Session-Token": s["session_token"]}), LIMIT_PER_SESSION[0] + 3)
        self.assertEqual(first, LIMIT_PER_SESSION[0])                           # per session, across IPs
        codes, first = await self.until_429(lambda i: self.post(P + "/chat", {"message": f"q{i}"}, ip="192.0.2.200",
                                                                headers=h), LIMIT_CHAT_IP[0] + 3)
        self.assertEqual(first, LIMIT_CHAT_IP[0])

    async def test_end_user_limit_from_configuration(self):
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"limits.end_user_msgs_per_minute": 3}})
        from runtime.usage import StoreUsageTracker
        chain = await self.w.S.hierarchy.load_chain(__import__("core.hierarchy").hierarchy.Level.BOT_INSTANCE,
                                                    self.w.iA_web.id)
        u = StoreUsageTracker(self.w.store)
        res = [await u.allow_end_user(chain, "end-user-1", 3, now=self.w.S.clock()) for _ in range(5)]
        self.assertEqual(res, [True, True, True, False, False])
        self.assertTrue(await u.allow_end_user(chain, "end-user-2", 3, now=self.w.S.clock()))

    async def test_webhook_and_admin_limits_are_per_integration_and_per_user(self):
        from api.telegram import HOOK_RATE
        from core import throttle
        key_a = throttle.throttle_key("tg-hook", self.w.iA_tg.id)
        res = [await throttle.hit(self.w.store, key_a, limit=HOOK_RATE[0], window_seconds=HOOK_RATE[1],
                                  now=self.w.S.clock()) for _ in range(HOOK_RATE[0] + 5)]
        self.assertEqual(sum(d.allowed for d in res), HOOK_RATE[0])
        other = await throttle.hit(self.w.store, throttle.throttle_key("tg-hook", self.w.iA_web.id),
                                   limit=HOOK_RATE[0], window_seconds=HOOK_RATE[1], now=self.w.S.clock())
        self.assertTrue(other.allowed)                                          # another instance: unaffected
        for scope in ("zd-admin-outbound", "kb-upload", "dc-invite"):
            k1, k2 = throttle.throttle_key(scope, self.w.users["admin"].id), throttle.throttle_key(scope, self.w.users["owner_b"].id)
            r1 = [await throttle.hit(self.w.store, k1, limit=3, window_seconds=3600, now=self.w.S.clock()) for _ in range(5)]
            r2 = await throttle.hit(self.w.store, k2, limit=3, window_seconds=3600, now=self.w.S.clock())
            self.assertEqual((sum(d.allowed for d in r1), r2.allowed), (3, True), scope)


if __name__ == "__main__":
    unittest.main()
