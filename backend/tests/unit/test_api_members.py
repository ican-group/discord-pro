"""Memberships and invitations through the API (spec 012 §9)."""
import asyncio
import json
import unittest

from tests.unit._api_fixtures import HAVE_API, World


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class MemberApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()
        self.E = self.w.eA.id

    def uid(self, k):
        return self.w.users[k].id

    async def test_grant_limits(self):
        O = f"/organisations/{self.w.oA.id}/members"
        r = await self.w.call("POST", O, "admin", {"user_id": self.uid("member2"), "role": "owner"})
        self.assertEqual(r.status_code, 403)                          # owner only at Enterprise level / by owner
        r = await self.w.call("POST", O, "admin", {"user_id": self.uid("outsider"), "role": "viewer"})
        self.assertEqual(r.status_code, 403)                          # must already belong to the Enterprise
        r = await self.w.call("POST", O, "admin", {"user_id": self.uid("member2"), "role": "admin"})
        self.assertEqual(r.status_code, 201)
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/members/{self.uid('viewer')}", "admin",
                              {"role": "owner"})
        self.assertEqual(r.status_code, 403)                          # above own role
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/members/{self.uid('editor')}", "editor",
                              {"role": "admin"})
        self.assertEqual(r.status_code, 403)                          # editors cannot manage members
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/members/{self.uid('admin')}", "admin",
                              {"role": "owner"})
        self.assertEqual(r.status_code, 403)                          # no self-escalation

    async def test_org_admin_delegation_policy(self):
        O = f"/organisations/{self.w.oA.id}/members"
        r = await self.w.call("POST", O, "org_admin", {"user_id": self.uid("member2"), "role": "viewer"})
        self.assertEqual(r.status_code, 201)
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/policy", "owner",
                              {"changes": {"delegation.org_admins_manage_members": False}})
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.w.call("PATCH", f"{O}/{self.uid('member2')}", "org_admin", {"role": "editor"})
        self.assertEqual(r.status_code, 403)

    async def test_last_owner_cannot_leave_or_be_demoted(self):
        me = self.uid("owner")
        r = await self.w.call("DELETE", f"/enterprises/{self.E}/members/{me}", "owner")
        self.assertEqual(r.status_code, 403)
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/members/{me}", "owner", {"role": "admin"})
        self.assertEqual(r.status_code, 403)
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/members/{self.uid('admin')}", "owner",
                              {"role": "owner"})
        self.assertEqual(r.status_code, 200)
        r = await self.w.call("DELETE", f"/enterprises/{self.E}/members/{me}", "owner")
        self.assertEqual(r.status_code, 200)

    async def test_concurrent_owner_removals_through_the_api(self):
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/members/{self.uid('admin')}", "owner", {"role": "owner"})
        self.assertEqual(r.status_code, 200)
        await self.w.refresh_user("admin")
        await self.w.refresh_user("owner")
        results = await asyncio.gather(
            self.w.call("DELETE", f"/enterprises/{self.E}/members/{self.uid('admin')}", "owner"),
            self.w.call("DELETE", f"/enterprises/{self.E}/members/{self.uid('owner')}", "admin"))
        self.assertEqual(sorted(r.status_code for r in results), [200, 403])
        from core import memberships as ms
        self.assertEqual(len((await ms.load(self.w.store, self.E)).owners()), 1)

    async def test_enterprise_removal_cascades_lower_memberships(self):
        await self.w.call("POST", f"/bots/{self.w.bA.id}/members", "owner",
                          {"user_id": self.uid("viewer"), "role": "editor"})
        r = await self.w.call("DELETE", f"/enterprises/{self.E}/members/{self.uid('viewer')}", "owner")
        self.assertEqual(r.status_code, 200)
        r = await self.w.call("GET", f"/bots/{self.w.bA.id}", "viewer")
        self.assertEqual(r.status_code, 401)                              # removal ends the member's sessions
        await self.w.refresh_user("viewer")
        r = await self.w.call("GET", f"/bots/{self.w.bA.id}", "viewer")
        self.assertEqual(r.status_code, 404)                              # bot grant removed with the membership
        self.assertEqual(await self.w.store.count("memberships", {"user_id": self.uid("viewer")}), 0)

    async def test_invitation_flow(self):
        r = await self.w.call("POST", f"/enterprises/{self.E}/invitations", "admin",
                              {"email": "Outsider@Example.Test", "role": "editor", "delivery": "link"})
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["token"]
        self.assertNotIn(token, str(await self.w.snapshot()))           # only the hash is stored
        r = await self.w.call("POST", "/invitations/lookup", "outsider", {"token": token})
        self.assertEqual((r.status_code, r.json()["role"]), (200, "editor"))
        r = await self.w.call("POST", "/invitations/lookup", "member2", {"token": token})
        self.assertEqual(r.status_code, 404)                              # X14: only the verified addressee
        r = await self.w.call("POST", "/invitations/accept", "member2", {"token": token})
        self.assertEqual(r.status_code, 404)                              # wrong addressee
        results = await asyncio.gather(*[self.w.call("POST", "/invitations/accept", "outsider", {"token": token})
                                         for _ in range(5)])
        self.assertEqual(sorted(x.status_code for x in results), [200, 404, 404, 404, 404])   # single use
        r = await self.w.call("GET", f"/enterprises/{self.E}", "outsider")
        self.assertEqual(r.json()["role"], "editor")

    async def test_invitation_authority_is_revalidated_on_accept(self):
        r = await self.w.call("POST", f"/enterprises/{self.E}/invitations", "admin",
                              {"email": "outsider@example.test", "role": "admin", "delivery": "link"})
        token = r.json()["token"]
        await self.w.call("PATCH", f"/enterprises/{self.E}/members/{self.uid('admin')}", "owner", {"role": "viewer"})
        r = await self.w.call("POST", "/invitations/accept", "outsider", {"token": token})
        self.assertEqual(r.status_code, 403)
        r = await self.w.call("GET", f"/enterprises/{self.E}", "outsider")
        self.assertEqual(r.status_code, 404)

    async def test_invitation_limits_and_unverified_email(self):
        r = await self.w.call("POST", f"/enterprises/{self.E}/invitations", "admin",
                              {"email": "x@example.test", "role": "owner"})
        self.assertEqual(r.status_code, 403)
        r = await self.w.call("POST", f"/enterprises/{self.E}/invitations", "admin",
                              {"email": "x@example.test", "role": "viewer", "scope_type": "organisations",
                               "scope_id": self.w.oB.id})
        self.assertEqual(r.status_code, 404)                              # foreign scope
        from core.identity import USERS
        await self.w.store.update(USERS, self.uid("outsider"), {"email_verified": False})
        r = await self.w.call("POST", f"/enterprises/{self.E}/invitations", "admin",
                              {"email": "outsider@example.test", "role": "viewer", "delivery": "link"})
        r = await self.w.call("POST", "/invitations/accept", "outsider", {"token": r.json()["token"]})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class InvitationSecurityTests(unittest.IsolatedAsyncioTestCase):
    """X14 and the invitation guarantees."""

    async def asyncSetUp(self):
        self.w = await World().build()
        self.E = self.w.eA.id

    async def invite(self, email="outsider@example.test", role="viewer"):
        r = await self.w.call("POST", f"/enterprises/{self.E}/invitations", "admin", {"email": email, "role": role, "delivery": "link"})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    async def test_token_returned_once_and_never_listed_logged_or_audited(self):
        created = await self.invite()
        token = created["token"]
        self.assertGreaterEqual(len(token), 43)                           # 256-bit urlsafe token
        listed = await self.w.call("GET", f"/enterprises/{self.E}/invitations", "admin")
        events = await self.w.call("GET", f"/enterprises/{self.E}/audit", "owner")
        snapshot = json.dumps(await self.w.snapshot(), default=str)
        for text in (listed.text, events.text, snapshot):
            self.assertNotIn(token, text)
        self.assertNotIn("token_hash", listed.text)
        self.assertEqual(listed.json()["invitations"][0]["status"], "pending")

    async def test_expired_invitations_are_not_shown_as_pending_and_cannot_be_used(self):
        created = await self.invite()
        inv_id = created["invitation"]["id"]
        doc = await self.w.store.get("invitations", inv_id)
        await self.w.store.update("invitations", inv_id, {"expires_at": "2020-01-01T00:00:00+00:00"},
                                  expect_version=doc["version"])
        listed = (await self.w.call("GET", f"/enterprises/{self.E}/invitations", "admin")).json()["invitations"]
        self.assertEqual(listed, [])
        listed = (await self.w.call("GET", f"/enterprises/{self.E}/invitations?include_expired=true",
                                    "admin")).json()["invitations"]
        self.assertEqual([i["status"] for i in listed], ["expired"])
        for path in ("/invitations/lookup", "/invitations/accept"):
            r = await self.w.call("POST", path, "outsider", {"token": created["token"]})
            self.assertEqual(r.status_code, 404)

    async def test_lookup_requires_the_verified_addressee(self):
        from core.identity import USERS
        created = await self.invite()
        for who in ("member2", "owner_b", "root"):
            r = await self.w.call("POST", "/invitations/lookup", who, {"token": created["token"]})
            self.assertEqual(r.status_code, 404)
        await self.w.store.update(USERS, self.w.users["outsider"].id, {"email_verified": False})
        r = await self.w.call("POST", "/invitations/lookup", "outsider", {"token": created["token"]})
        self.assertEqual(r.status_code, 404)
        r = await self.w.call("POST", "/invitations/lookup", None, {"token": created["token"]})
        self.assertEqual(r.status_code, 401)

    async def test_tenant_scope_and_authorised_revocation(self):
        created = await self.invite()
        inv = created["invitation"]["id"]
        for who in ("owner_b", "editor", "viewer"):
            before = await self.w.snapshot()
            r = await self.w.call("DELETE", f"/invitations/{inv}", who)
            self.assertIn(r.status_code, (403, 404))
            self.assertEqual(await self.w.snapshot(), before)
        r = await self.w.call("GET", f"/enterprises/{self.w.eB.id}/invitations", "admin")
        self.assertEqual(r.status_code, 404)
        self.assertEqual((await self.w.call("DELETE", f"/invitations/{inv}", "admin")).status_code, 200)
        r = await self.w.call("POST", "/invitations/accept", "outsider", {"token": created["token"]})
        self.assertEqual(r.status_code, 404)                              # revoked

    async def test_concurrent_acceptance_and_revocation(self):
        created = await self.invite()
        results = await asyncio.gather(
            self.w.call("POST", "/invitations/accept", "outsider", {"token": created["token"]}),
            self.w.call("DELETE", f"/invitations/{created['invitation']['id']}", "admin"),
            self.w.call("POST", "/invitations/accept", "outsider", {"token": created["token"]}))
        accepted = [r for r in (results[0], results[2]) if r.status_code == 200]
        self.assertLessEqual(len(accepted), 1)
        doc = await self.w.store.get("invitations", created["invitation"]["id"])
        member = await self.w.store.get("memberships", f"{self.E}:{self.w.users['outsider'].id}")
        self.assertEqual(doc["status"] == "accepted", member is not None)   # consistent outcome


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class MembershipRevocationApiTests(unittest.IsolatedAsyncioTestCase):
    """X4 through the API: concurrent revocation of the granting admin."""

    async def asyncSetUp(self):
        self.w = await World().build()
        self.E = self.w.eA.id

    async def test_grant_racing_demotion_of_the_admin(self):
        outcomes = set()
        for i in range(8):
            w = await World().build()
            E, admin, target = w.eA.id, w.users["admin"].id, w.users["member2"].id
            grant = w.call("PATCH", f"/enterprises/{E}/members/{target}", "admin", {"role": "admin"})
            demote = w.call("PATCH", f"/enterprises/{E}/members/{admin}", "owner", {"role": "viewer"})
            if i % 2:
                d, g = await asyncio.gather(demote, grant)
            else:
                g, d = await asyncio.gather(grant, demote)
            self.assertEqual(d.status_code, 200)
            guard = await w.store.get("membership_guards", E)
            order = [r for r in guard["recent"] if r["outcome"] == "applied"]
            target_doc = await w.store.get("memberships", f"{E}:{target}")
            if g.status_code == 200:
                outcomes.add("granted")
                # the grant took effect before the demotion
                self.assertEqual(target_doc["role"], "admin")
                self.assertLess(order[-2]["seq"], order[-1]["seq"])
            else:
                outcomes.add("refused")
                self.assertIn(g.status_code, (401, 403))
                self.assertEqual(target_doc["role"], "viewer")
        self.assertTrue(outcomes)

    async def test_delegation_policy_change_is_serialised_with_membership_changes(self):
        r = await self.w.call("PATCH", f"/enterprises/{self.E}/policy", "owner",
                              {"changes": {"delegation.org_admins_manage_members": False}})
        self.assertEqual(r.status_code, 200, r.text)
        guard = await self.w.store.get("membership_guards", self.E)
        self.assertTrue(any(x["outcome"] == "applied" for x in guard["recent"]))
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/members", "org_admin",
                              {"user_id": self.w.users["member2"].id, "role": "viewer"})
        self.assertEqual(r.status_code, 403)
