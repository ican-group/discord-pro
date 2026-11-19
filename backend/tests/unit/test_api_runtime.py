"""Runtime configuration resolution and worker status APIs (Phase 4A)."""
import json
import unittest

from tests.unit._api_fixtures import ANTHROPIC_KEY, HAVE_API, World


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class RuntimeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    async def test_runtime_config_preview(self):
        w = self.w
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        r = await w.call("GET", f"/instances/{w.iA_web.id}/runtime-config", "viewer")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["ancestry"], {"enterprise_id": w.eA.id, "organisation_id": w.oA.id, "bot_id": w.bA.id,
                                            "bot_instance_id": w.iA_web.id})
        self.assertEqual(body["ai"]["credential"]["id"], w.cA_ai.id)
        self.assertFalse(body["ai"]["provider_available"])                 # no provider adapters in 4A
        self.assertEqual(body["knowledge"]["mode"], "general")
        self.assertNotIn(ANTHROPIC_KEY, r.text)
        self.assertNotIn("secret_enc", r.text)
        await w.call("PATCH", f"/enterprises/{w.eA.id}/policy", "owner",
                     {"changes": {"knowledge.minimum_mode": "strict_knowledge_only"}})
        body = (await w.call("GET", f"/instances/{w.iA_web.id}/runtime-config", "viewer")).json()
        self.assertTrue(body["knowledge"]["strict"])
        self.assertTrue(body["knowledge"]["retrieval_engine"])              # Phase 4K: the store-backed engine
        await w.call("POST", f"/organisations/{w.oA.id}/suspend", "owner", {})
        body = (await w.call("GET", f"/instances/{w.iA_web.id}/runtime-config", "viewer")).json()
        self.assertEqual((body["available"], body["unavailable_reason"]), (False, "organisation suspended"))

    async def test_worker_status_is_superadmin_only(self):
        r = await self.w.call("GET", "/platform/workers", "root")
        self.assertEqual(r.status_code, 200)
        self.assertIn("deletion_jobs", r.json())
        for who in ("owner", "admin"):
            self.assertEqual((await self.w.call("GET", "/platform/workers", who)).status_code, 404)


if __name__ == "__main__":
    unittest.main()
