"""Phase 6A launch-critical gaps: usage (F1), user discovery (F2), invitation
e-mail (F3), credential tests (F4), webchat preview accounting (F5)."""
import json
import unittest

from tests.unit._api_fixtures import ANTHROPIC_KEY, HAVE_API, ZD_API_TOKEN, World
from tests.unit.test_api_webchat import ORIGIN, FakeProvider, WebchatBase

CSRF = {"X-Requested-With": "BitBotPro"}


class CapturingMailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_invitation(self, email, *, subject, text, html):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append({"to": email, "subject": subject, "text": text, "html": html})

    async def send_password_reset(self, email, reset_url):
        return None


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class UsageTests(WebchatBase):
    async def bill(self, chain, n, labels=None, state="settled"):
        from runtime.usage import StoreUsageTracker
        t = StoreUsageTracker(self.w.store)
        for _ in range(n):
            r = await t.reserve_ai_message(chain, now=self.w.S.clock(), labels=labels)
            if state == "settled":
                await t.settle(r, chain, now=self.w.S.clock())
            elif state == "uncertain":
                await t.mark_uncertain(r.key, now=self.w.S.clock())

    async def chains(self):
        from core.hierarchy import Level
        H = self.w.S.hierarchy
        return (await H.load_chain(Level.BOT_INSTANCE, self.w.iA_web.id),
                await H.load_chain(Level.BOT_INSTANCE, self.w.iA_tg.id),
                await H.load_chain(Level.BOT_INSTANCE, self.w.iB_web.id))

    async def test_counts_by_level_state_and_label_with_tenant_isolation(self):
        web, tg, other = await self.chains()
        await self.bill(web, 3)
        await self.bill(web, 2, labels={"preview": True})
        await self.bill(tg, 1, state="uncertain")
        await self.bill(other, 7)                                            # Enterprise B: never visible to A
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 100}})
        ent = (await self.w.call("GET", f"/enterprises/{self.w.eA.id}/usage", "owner")).json()
        self.assertEqual((ent["ai_messages"]["settled"], ent["ai_messages"]["uncertain"], ent["preview"]["settled"]), (5, 1, 2))
        self.assertEqual(ent["quotas"][0], {"scope": "enterprise", "id": self.w.eA.id, "limit": 100, "used": 6})
        inst = (await self.w.call("GET", f"/instances/{self.w.iA_web.id}/usage", "viewer")).json()
        self.assertEqual((inst["ai_messages"]["settled"], inst["ai_messages"]["uncertain"]), (5, 0))
        self.assertIn("webchat_daily_limit", inst)
        bot = (await self.w.call("GET", f"/bots/{self.w.bA.id}/usage", "editor")).json()
        self.assertEqual(bot["ai_messages"]["settled"], 5)
        for who, path in (("owner_b", f"/enterprises/{self.w.eA.id}/usage"), ("outsider", f"/bots/{self.w.bA.id}/usage"),
                          ("owner", f"/instances/{self.w.iB_web.id}/usage")):
            self.assertEqual((await self.w.call("GET", path, who)).status_code, 404, (who, path))
        self.assertEqual((await self.w.call("GET", f"/enterprises/{self.w.eA.id}/usage?period=2026-13", "owner")).status_code, 422)
        # tenant headers grant nothing
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}/usage", "owner_b",
                              headers={"X-Enterprise-ID": self.w.eA.id, "X-Instance-ID": self.w.iA_web.id})
        self.assertEqual(r.status_code, 404)

    async def test_enterprise_counter_hidden_from_organisation_members(self):
        web, _, _ = await self.chains()
        await self.bill(web, 2)
        org = (await self.w.call("GET", f"/organisations/{self.w.oA.id}/usage", "org_admin")).json()
        self.assertEqual([q["scope"] for q in org["quotas"]], ["organisation"])
        own = (await self.w.call("GET", f"/organisations/{self.w.oA.id}/usage", "owner")).json()
        self.assertEqual([q["scope"] for q in own["quotas"]], ["enterprise", "organisation"])

    async def test_integration_usage_is_attributed(self):
        web, _, _ = await self.chains()
        z = (await self.w.call("POST", f"/bots/{self.w.bA.id}/integrations", "admin", {"type": "zendesk", "name": "Z"})).json()
        from runtime.pipeline import _IntegrationUsage
        from runtime.usage import StoreUsageTracker
        from core.hierarchy import Level
        bchain = await self.w.S.hierarchy.load_chain(Level.BOT, self.w.bA.id)
        t = _IntegrationUsage(StoreUsageTracker(self.w.store), z["id"])
        r = await t.reserve_ai_message(bchain, now=self.w.S.clock(), key="zd:e1")
        await t.settle(r, bchain, now=self.w.S.clock())
        u = (await self.w.call("GET", f"/integrations/{z['id']}/usage", "viewer")).json()
        self.assertEqual(u["ai_messages"]["settled"], 1)
        ev = await self.w.store.find("usage_events", {"kind": "llm_call"})
        self.assertEqual(ev[0]["details"]["integration_id"], z["id"])
        self.assertEqual((await self.w.call("GET", f"/integrations/{z['id']}/usage", "owner_b")).status_code, 404)

    async def test_unknown_labels_are_refused(self):
        web, _, _ = await self.chains()
        from runtime.usage import StoreUsageTracker
        with self.assertRaises(ValueError):
            await StoreUsageTracker(self.w.store).reserve_ai_message(web, now=self.w.S.clock(), labels={"free": True})


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class PreviewAccountingTests(WebchatBase):
    async def preview(self, who="admin", headers=CSRF):
        return await self.w.call("POST", f"/instances/{self.w.iA_web.id}/preview-session", who, headers=headers)

    async def test_preview_session_is_server_side_counted_and_labelled(self):
        r = await self.preview()
        self.assertEqual(r.status_code, 201, r.text)
        url = r.json()["preview_url"]
        self.assertTrue(url.startswith(f"https://api.bitbotpro.io/api/public/widget/{self.pid}?mode=fullpage#preview="))
        sid, token = url.split("#preview=")[1].split(".", 1)
        doc = await self.w.store.get("chat_sessions", sid)
        self.assertEqual((doc["preview"], doc["preview_by"]), (True, self.w.users["admin"].id))
        self.assertNotIn(token, json.dumps(doc, default=str))                      # only the hash is stored
        self.assertEqual((await self.chat("Hi", session=(sid, token))).status_code, 200)
        self.assertEqual((await self.chat("Hi again")).status_code, 200)            # live traffic
        ledger = {d.get("preview", False) for d in await self.w.store.find("usage_ledger", {"state": "settled"})}
        self.assertEqual(ledger, {True, False})
        u = (await self.w.call("GET", f"/instances/{self.w.iA_web.id}/usage", "admin")).json()
        self.assertEqual((u["ai_messages"]["settled"], u["preview"]["settled"]), (2, 1))   # preview is counted too

    async def test_forged_preview_flags_do_nothing(self):
        s = await self.new_session()
        r = await self.pub("POST", self.P + "/chat?preview=1", {"message": "Hi", "session_id": s[0], "preview": True},
                           {"X-Chat-Session-Token": s[1]})
        self.assertEqual(r.status_code, 422)                                       # unknown body field refused
        r = await self.pub("POST", self.P + "/chat?preview=1", {"message": "Hi", "session_id": s[0]},
                           {"X-Chat-Session-Token": s[1]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([d.get("preview") for d in await self.w.store.find("usage_ledger", {})], [None])

    async def test_preview_cannot_bypass_quota(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 1}})
        sid, token = (await self.preview()).json()["preview_url"].split("#preview=")[1].split(".", 1)
        self.assertEqual((await self.chat("one", session=(sid, token))).status_code, 200)
        self.assertEqual((await self.chat("two", session=(sid, token))).status_code, 429)

    async def test_preview_requires_authority_csrf_and_availability(self):
        self.assertEqual((await self.preview(who="viewer")).status_code, 403)
        self.assertEqual((await self.preview(who="owner_b")).status_code, 404)
        await self.w.call("POST", f"/bots/{self.w.bA.id}/suspend", "owner", {"reason": "x"})
        self.assertEqual((await self.preview()).status_code, 409)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class UserDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    async def test_platform_listing_is_superadmin_only_bounded_and_paginated(self):
        for who in ("owner", "admin", "outsider"):
            self.assertEqual((await self.w.call("GET", "/platform/users", who)).status_code, 404)
        r = (await self.w.call("GET", "/platform/users?limit=4", "root")).json()
        self.assertEqual(len(r["users"]), 4)
        r2 = (await self.w.call("GET", f"/platform/users?limit=4&after={r['next']}", "root")).json()
        emails = [u["email"] for u in r["users"] + r2["users"]]
        self.assertEqual(emails, sorted(emails))
        self.assertEqual(len(set(emails)), len(emails))
        r = (await self.w.call("GET", "/platform/users?q=OWN", "root")).json()
        self.assertEqual(sorted(u["email"] for u in r["users"]), ["owner@example.test", "owner_b@example.test"])
        self.assertEqual((await self.w.call("GET", "/platform/users?limit=500", "root")).status_code, 422)
        self.assertNotIn("password", json.dumps(r))
        self.assertEqual((await self.w.call("GET", "/platform/users?q=.*", "root")).json()["users"], [])  # never a regex

    async def test_member_candidates_are_enterprise_members_only(self):
        r = await self.w.call("GET", f"/organisations/{self.w.oA.id}/member-candidates?q=out", "owner")
        self.assertEqual(r.json()["candidates"], [])                                  # outsider is not a member
        r = await self.w.call("GET", f"/bots/{self.w.bA.id}/member-candidates?q=mem", "owner")
        self.assertEqual([c["email"] for c in r.json()["candidates"]], ["member2@example.test"])
        r = await self.w.call("GET", f"/organisations/{self.w.oA.id}/member-candidates?q=own", "owner")
        self.assertNotIn("owner_b@example.test", json.dumps(r.json()))                # other Enterprise
        self.assertEqual((await self.w.call("GET", f"/organisations/{self.w.oA.id}/member-candidates?q=ow", "owner")).status_code, 422)
        self.assertEqual((await self.w.call("GET", f"/organisations/{self.w.oA.id}/member-candidates?q=own", "viewer")).status_code, 403)
        self.assertEqual((await self.w.call("GET", f"/organisations/{self.w.oA.id}/member-candidates?q=own", "owner_b")).status_code, 404)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class InvitationEmailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()
        self.mail = CapturingMailer()
        self.w.S.mailer = self.mail

    async def invite(self, email="new.person@example.test", who="owner", **extra):
        return await self.w.call("POST", f"/enterprises/{self.w.eA.id}/invitations", who,
                                 {"email": email, "role": "viewer", **extra})

    async def test_email_carries_the_only_copy_of_the_link(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}", "root", {"name": "Ent <script>alert(1)</script>"})
        r = await self.invite("New.Person@Example.test")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["email_sent"], True)
        self.assertNotIn("token", r.json())
        [m] = self.mail.sent
        self.assertEqual(m["to"], "new.person@example.test")
        token = m["text"].split("#token=")[1].split()[0]
        self.assertTrue(m["text"].count("https://bitbotpro.io/app/invitations#token="), 1)
        self.assertNotIn("<script>", m["html"])
        self.assertIn("&lt;script&gt;", m["html"])
        snapshot = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(token, snapshot)                                           # stored hashed; not in audit
        # only the verified addressee can use it, once
        from core.identity import create_user
        u = await create_user(self.w.store, email="new.person@example.test", name="New", now=self.w.S.clock(), email_verified=True)
        from api.common import issue_session
        self.w._tokens["new"] = await issue_session(self.w.S, u)
        self.assertEqual((await self.w.call("POST", "/invitations/accept", "viewer", {"token": token})).status_code, 404)
        self.assertEqual((await self.w.call("POST", "/invitations/accept", "new", {"token": token})).status_code, 200)
        self.assertEqual((await self.w.call("POST", "/invitations/accept", "new", {"token": token})).status_code, 404)

    async def test_link_delivery_returns_the_token_once_and_sends_nothing(self):
        r = await self.invite(delivery="link")
        self.assertTrue(r.json()["token"])
        self.assertEqual(self.mail.sent, [])
        self.assertEqual((await self.invite(delivery="sms")).status_code, 422)

    async def test_rate_limits_and_mail_failure(self):
        codes = [(await self.invite("same@example.test")).status_code for _ in range(7)]
        self.assertEqual(codes.count(429), 2)                                       # 5 per recipient per day
        self.w.S.mailer = CapturingMailer(fail=True)
        r = await self.invite("other@example.test")
        self.assertEqual((r.status_code, r.json()["email_sent"]), (201, False))   # invitation exists; resend later

    async def test_authority_unchanged(self):
        self.assertEqual((await self.invite(who="viewer")).status_code, 403)
        self.assertEqual((await self.invite(who="owner_b")).status_code, 404)
        self.assertEqual(self.mail.sent, [])


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class CredentialTestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from runtime.providers import build_providers
        self.w = await World().build()
        self.calls, self.status = [], 200

        def handler(request):
            self.calls.append((request.method, str(request.url), dict(request.headers)))
            return httpx.Response(self.status, json={"data": []})
        self.w.S.providers = build_providers(transport=httpx.MockTransport(handler))

    async def test_ai_key_check_is_read_only_on_the_fixed_host(self):
        r = await self.w.call("POST", f"/credentials/{self.w.cA_ai.id}/test", "owner")
        self.assertEqual(r.json(), {"ok": True, "category": None})
        [(method, url, headers)] = self.calls
        self.assertEqual((method, url), ("GET", "https://api.anthropic.com/v1/models"))
        self.assertEqual(headers["x-api-key"], ANTHROPIC_KEY)
        self.status = 401
        r = await self.w.call("POST", f"/credentials/{self.w.cA_ai.id}/test", "owner")
        self.assertEqual(r.json(), {"ok": False, "category": "unauthorized"})
        self.assertNotIn(ANTHROPIC_KEY, r.text)
        audit = await self.w.store.find("audit_events", {"action": "credential.test"})
        self.assertEqual(len(audit), 2)
        self.assertNotIn(ANTHROPIC_KEY, json.dumps(audit))

    async def test_only_managers_of_the_owner_may_test(self):
        for who, code in (("viewer", 403), ("org_admin", 404), ("owner_b", 404), ("outsider", 404)):
            r = await self.w.call("POST", f"/credentials/{self.w.cA_ai.id}/test", who)
            self.assertIn(r.status_code, (403, 404), who)
        self.assertEqual(self.calls, [])

    async def test_zendesk_token_check_goes_through_the_guarded_client(self):
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                              {"provider": "zendesk_api_token", "name": "ZD", "secret": ZD_API_TOKEN,
                               "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})
        self.assertEqual(r.status_code, 201, r.text)
        r = await self.w.call("POST", f"/credentials/{r.json()['id']}/test", "admin")
        self.assertEqual(r.json()["ok"], True)
        self.assertTrue(all("acme.zendesk.com" in c[0] for c in self.w.zendesk.calls if c[2] == "/api/v2/users/me.json"))

    async def test_rate_limited(self):
        codes = [(await self.w.call("POST", f"/credentials/{self.w.cA_ai.id}/test", "owner")).status_code for _ in range(32)]
        self.assertEqual(codes.count(429), 2)


if __name__ == "__main__":
    unittest.main()


class WidgetPreviewScriptTests(unittest.TestCase):
    def test_widget_takes_a_preview_session_only_from_a_strict_fragment_and_clears_it(self):
        from api.webchat_assets import WIDGET_JS
        self.assertIn('#preview=([0-9a-f-]{36})\\.([A-Za-z0-9_-]{20,200})$', WIDGET_JS)
        self.assertIn("history.replaceState(null", WIDGET_JS)
        self.assertNotIn("innerHTML", WIDGET_JS)
