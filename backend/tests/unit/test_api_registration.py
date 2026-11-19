"""Phase 6C B-ACCT: account creation by invitation (new users) and acceptance (existing users)."""
import asyncio
import json
import unittest

from tests.unit._api_fixtures import HAVE_API, World
from tests.unit.test_api_phase6a import CapturingMailer

CSRF = {"X-Requested-With": "BitBotPro"}
GOOD = "Orchid-Lantern-Harbour-2026"


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()
        self.mail = CapturingMailer()
        self.w.S.mailer = self.mail

    async def invite(self, email="new.person@example.test", who="owner", role="editor", **extra):
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/invitations", who,
                              {"email": email, "role": role, **extra})
        self.assertEqual(r.status_code, 201, r.text)
        if extra.get("delivery") == "link":
            return r.json()["token"]
        return self.mail.sent[-1]["text"].split("#token=")[1].split()[0]

    async def register(self, token, name="New Person", password=GOOD, headers=CSRF, **extra):
        return await self.w.call("POST", "/invitations/register", None,
                                 {"token": token, "name": name, "password": password, **extra}, headers=headers)

    async def users_named(self, email):
        return await self.w.store.find("users", {"email": email})

    async def get_as(self, response, path):
        import httpx
        cookie = response.cookies.get("bbp_session")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            return await c.get("/api" + path, cookies={"bbp_session": cookie})

    async def test_a_new_user_creates_an_account_joins_and_sees_only_their_enterprise(self):
        token = await self.invite()
        r = await self.register(token)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual((body["enterprise_id"], body["scope_type"]), (self.w.eA.id, "enterprise"))
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        [u] = await self.users_named("new.person@example.test")
        self.assertTrue(u["email_verified"])
        self.assertFalse(u["is_platform_superadmin"])
        self.assertNotEqual(u["password_hash"], GOOD)
        session = await self.get_as(r, "/auth/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual((await self.get_as(r, f"/enterprises/{self.w.eA.id}")).status_code, 200)
        self.assertEqual((await self.get_as(r, f"/enterprises/{self.w.eB.id}")).status_code, 404)   # other tenant
        ents = (await self.get_as(r, "/enterprises")).json()
        self.assertEqual([e["id"] for e in ents["enterprises"]], [self.w.eA.id])
        inv = (await self.w.store.find("invitations", {"email": "new.person@example.test"}))[0]
        self.assertEqual((inv["status"], inv["accepted_by"]), ("accepted", u["id"]))
        actions = [a["action"] for a in await self.w.store.find("audit_events", {"target_id": u["id"]})]
        self.assertIn("user.register_by_invitation", actions)
        # the account then signs in normally
        login = await self.w.call("POST", "/auth/login", None, {"email": "new.person@example.test", "password": GOOD},
                                  headers=CSRF)
        self.assertEqual(login.status_code, 200, login.text)
        # the token was single use
        self.assertEqual((await self.register(token)).status_code, 404)
        dump = json.dumps(await self.w.snapshot(), default=str)
        self.assertNotIn(token, dump)
        self.assertNotIn(GOOD, dump)

    async def test_the_role_is_the_invited_one_and_cannot_be_raised_by_the_request(self):
        token = await self.invite(role="viewer")
        r = await self.register(token, role="owner")
        self.assertEqual(r.status_code, 422)                              # unknown fields are refused
        self.assertEqual(await self.users_named("new.person@example.test"), [])
        r = await self.register(token)
        self.assertEqual(r.status_code, 201)
        [u] = await self.users_named("new.person@example.test")
        m = (await self.w.store.find("memberships", {"user_id": u["id"]}))[0]
        self.assertIn("viewer", json.dumps(m))
        self.assertNotIn('"owner"', json.dumps(m))

    async def test_an_organisation_scoped_invitation_grants_only_that_organisation(self):
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/invitations", "owner",
                              {"email": "org.person@example.test", "role": "admin", "scope_type": "organisations",
                               "scope_id": self.w.oA.id})
        self.assertEqual(r.status_code, 201, r.text)
        token = self.mail.sent[-1]["text"].split("#token=")[1].split()[0]
        r = await self.register(token)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["scope_type"], "organisation")
        self.assertEqual((await self.get_as(r, f"/organisations/{self.w.oA.id}")).status_code, 200)
        self.assertEqual((await self.get_as(r, f"/enterprises/{self.w.eA.id}")).status_code, 404)  # org-level only
        self.assertEqual((await self.get_as(r, f"/organisations/{self.w.oB.id}")).status_code, 404)

    async def test_an_existing_account_must_sign_in_and_accept(self):
        token = await self.invite(email="viewer_b@example.test")              # address of an existing account?
        existing = await self.w.store.find("users", {"email": "viewer_b@example.test"})
        if not existing:
            from core.identity import create_user
            await create_user(self.w.store, email="viewer_b@example.test", name="Existing", now=self.w.S.clock(),
                              email_verified=True)
        before = await self.w.store.count("users", {})
        r = await self.register(token)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Sign in", r.json()["detail"])
        self.assertEqual(await self.w.store.count("users", {}), before)       # no duplicate identity
        inv = (await self.w.store.find("invitations", {"email": "viewer_b@example.test"}))[0]
        self.assertEqual(inv["status"], "pending")
        # the existing user signs in and accepts the same invitation
        from api.common import issue_session
        from core.identity import find_user_by_email
        u = await find_user_by_email(self.w.store, "viewer_b@example.test")
        self.w._tokens["existing"] = await issue_session(self.w.S, u)
        r = await self.w.call("POST", "/invitations/accept", "existing", {"token": token})
        self.assertEqual(r.status_code, 200, r.text)

    async def test_link_invitations_cannot_create_accounts(self):
        token = await self.invite(delivery="link")
        r = await self.register(token)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(await self.users_named("new.person@example.test"), [])
        inv = (await self.w.store.find("invitations", {"email": "new.person@example.test"}))[0]
        self.assertEqual(inv["status"], "pending")

    async def test_unusable_invitations_are_indistinguishable_and_create_nothing(self):
        expired = await self.invite(email="expired@example.test")
        inv = (await self.w.store.find("invitations", {"email": "expired@example.test"}))[0]
        await self.w.store.update("invitations", inv["id"], {"expires_at": "2020-01-01T00:00:00+00:00"})
        revoked = await self.invite(email="revoked@example.test")
        inv = (await self.w.store.find("invitations", {"email": "revoked@example.test"}))[0]
        self.assertEqual((await self.w.call("DELETE", f"/invitations/{inv['id']}", "owner")).status_code, 200)
        answers = set()
        for t in (expired, revoked, "x" * 43, "short"):
            r = await self.register(t)
            answers.add((r.status_code, r.text))
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers.pop()[0], 404)
        for e in ("expired@example.test", "revoked@example.test"):
            self.assertEqual(await self.users_named(e), [])

    async def test_password_policy_name_and_csrf_are_enforced_without_spending_the_invitation(self):
        token = await self.invite()
        self.assertEqual((await self.register(token, password="short")).status_code, 422)
        self.assertEqual((await self.register(token, password="new.person@example.test1A")).status_code, 422)
        self.assertEqual((await self.register(token, name="   ")).status_code, 422)
        self.assertEqual((await self.register(token, headers={})).status_code, 403)          # CSRF header
        self.assertEqual(await self.users_named("new.person@example.test"), [])
        self.assertEqual((await self.register(token)).status_code, 201)                     # still usable

    async def test_concurrent_registrations_create_exactly_one_identity(self):
        token = await self.invite()
        results = await asyncio.gather(*[self.register(token) for _ in range(5)])
        codes = sorted(r.status_code for r in results)
        self.assertEqual(codes.count(201), 1, codes)
        self.assertTrue(all(c in (201, 404, 409) for c in codes), codes)
        self.assertEqual(len(await self.users_named("new.person@example.test")), 1)

    async def test_a_revoked_inviter_authority_leaves_no_account_behind(self):
        token = await self.invite(who="admin")
        admin_id = self.w.users["admin"].id
        r = await self.w.call("DELETE", f"/enterprises/{self.w.eA.id}/members/{admin_id}", "owner")
        self.assertIn(r.status_code, (200, 204), r.text)
        r = await self.register(token)
        self.assertIn(r.status_code, (403, 404), r.text)
        self.assertEqual(await self.users_named("new.person@example.test"), [])
        inv = (await self.w.store.find("invitations", {"email": "new.person@example.test"}))[0]
        self.assertEqual(inv["status"], "pending")

    async def test_attempts_are_rate_limited_per_invitation(self):
        from api.members import REGISTER_TOKEN
        token = await self.invite()
        for _ in range(REGISTER_TOKEN[0]):
            await self.register(token, password="short")
        r = await self.register(token)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)


if __name__ == "__main__":
    unittest.main()
