"""Security regression tests for the Spec 012 API (Phase 3)."""
import json
import unittest

from tests.unit._api_fixtures import HAVE_API, World

UNKNOWN = "00000000-0000-4000-8000-000000000000"


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class SecurityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    async def test_foreign_and_unknown_ids_are_indistinguishable(self):
        pairs = [(f"/enterprises/{self.w.eA.id}", f"/enterprises/{UNKNOWN}"),
                 (f"/bots/{self.w.bA.id}/config", f"/bots/{UNKNOWN}/config"),
                 (f"/credentials/{self.w.cA_tg.id}", f"/credentials/{UNKNOWN}"),
                 (f"/instances/{self.w.iA_web.id}", "/instances/not-a-uuid")]
        for foreign, unknown in pairs:
            a = await self.w.call("GET", foreign, "owner_b")
            b = await self.w.call("GET", unknown, "owner_b")
            self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
            self.assertEqual(a.status_code, 404)

    async def test_operator_injection_is_rejected(self):
        bad_bodies = [
            ("PATCH", f"/bots/{self.w.bA.id}/config", {"changes": {"behaviour.language": {"$ne": None}}}),
            ("PATCH", f"/bots/{self.w.bA.id}/config", {"changes": {"$where": "1"}}),
            ("POST", f"/organisations/{self.w.oA.id}/members", {"user_id": {"$ne": None}, "role": "viewer"}),
            ("POST", "/invitations/accept", {"token": {"$gt": ""}}),
            ("PATCH", f"/instances/{self.w.iA_web.id}", {"channel": {"type": {"$ne": "webchat"}}}),
            ("PATCH", f"/organisations/{self.w.oA.id}", {"enterprise_id": self.w.eB.id}),
        ]
        for method, path, body in bad_bodies:
            with self.subTest(path=path, body=body):
                before = await self.w.snapshot()
                r = await self.w.call(method, path, "owner", body)
                self.assertIn(r.status_code, (404, 422))
                self.assertEqual(await self.w.snapshot(), before)

    async def test_client_cannot_choose_ids_ancestry_or_public_ids(self):
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                              {"name": "x", "channel": {"type": "webchat", "public_id": "pk_" + "A" * 30}})
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(r.json()["channel"]["public_id"], "pk_" + "A" * 30)
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                              {"name": "x", "channel": {"type": "discord", "guild_id": "999999999999999999"}})
        self.assertEqual(r.status_code, 422)                                     # guilds come from the install flow
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner",
                              {"name": "x", "enterprise_id": self.w.eB.id})
        self.assertEqual(r.status_code, 422)                                     # unknown field refused
        r = await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner", {"name": "x", "id": UNKNOWN})
        self.assertEqual(r.status_code, 422)

    async def test_errors_do_not_leak_internals(self):
        r = await self.w.call("POST", f"/bots/{self.w.bB.id}/instances", "owner",
                              {"name": "x", "channel": {"type": "telegram", "credential_id": self.w.cA_tg.id}})
        self.assertEqual(r.status_code, 404)
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                              {"name": "x", "channel": {"type": "discord"}})
        self.assertEqual(r.status_code, 201)
        conflicts = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                                      {"name": "y", "channel": {"type": "telegram", "credential_id": self.w.cA_tg.id}})
        self.assertEqual(conflicts.json()["detail"], "This channel binding is already in use.")
        for text in (r.text, conflicts.text):
            self.assertNotIn("Traceback", text)
            self.assertNotIn(self.w.iA_tg.id, text)                              # holder not disclosed

    async def test_audit_records_writes_without_secrets(self):
        await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "owner",
                          {"provider": "openai", "name": "k", "secret": "not-a-real-key-zzzzzzzzzzzz"})
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner", {"changes": {"behaviour.language": "de"}})
        events = (await self.w.call("GET", f"/enterprises/{self.w.eA.id}/audit", "owner")).json()["events"]
        actions = [e["action"] for e in events]
        self.assertIn("credential.create", actions)
        self.assertIn("config.update", actions)
        self.assertNotIn("not-a-real-key-zzzzzzzzzzzz", json.dumps(events))
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}/audit", "owner_b")
        self.assertEqual(r.status_code, 404)

    async def test_listing_never_shows_other_tenants(self):
        r = await self.w.call("GET", "/enterprises", "owner_b")
        self.assertEqual([e["id"] for e in r.json()["enterprises"]], [self.w.eB.id])
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}/organisations", "bot_editor")
        self.assertEqual([o["id"] for o in r.json()["organisations"]], [self.w.oA.id])   # navigation only
        r = await self.w.call("GET", f"/enterprises/{self.w.eA.id}", "bot_editor")
        self.assertEqual(r.status_code, 404)                                     # no role on the Enterprise
        r = await self.w.call("GET", f"/bots/{self.w.bA.id}", "bot_editor")
        self.assertEqual(r.json()["role"], "editor")


if __name__ == "__main__":
    unittest.main()
