"""Lifecycle through the API (spec 012 §7, D-Q2)."""
import unittest

from tests.unit._api_fixtures import HAVE_API, World


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class LifecycleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    async def op(self, path, op, who="owner"):
        return await self.w.call("POST", f"{path}/{op}", who, {"reason": "test"})

    async def available(self, path):
        return (await self.w.call("GET", path, "owner")).json()["available"]

    async def test_parent_suspension_disables_descendants_and_resume_keeps_child_state(self):
        O, B, I = f"/organisations/{self.w.oA.id}", f"/bots/{self.w.bA.id}", f"/instances/{self.w.iA_web.id}"
        self.assertEqual((await self.op(B, "suspend", "admin")).status_code, 200)       # child's own suspension
        self.assertEqual((await self.op(O, "suspend")).status_code, 200)
        r = await self.w.call("GET", I, "owner")
        self.assertEqual(r.json()["unavailable_reason"], "organisation suspended")
        self.assertEqual((await self.op(O, "resume")).status_code, 200)
        self.assertFalse(await self.available(I))                                       # bot still suspended
        self.assertEqual((await self.w.call("GET", B, "owner")).json()["lifecycle"]["state"], "suspended")
        self.assertEqual((await self.op(B, "resume", "admin")).status_code, 200)
        self.assertTrue(await self.available(I))

    async def test_suspension_authority(self):
        B = f"/bots/{self.w.bA.id}"
        self.assertEqual((await self.op(B, "suspend", "root")).status_code, 200)        # platform enforcement
        self.assertEqual((await self.op(B, "resume", "owner")).status_code, 409)
        self.assertEqual((await self.op(B, "resume", "admin")).status_code, 409)
        self.assertEqual((await self.op(B, "resume", "root")).status_code, 200)
        self.assertEqual((await self.op(B, "suspend", "owner")).status_code, 200)       # enterprise authority
        self.assertEqual((await self.op(B, "resume", "admin")).status_code, 409)
        self.assertEqual((await self.op(B, "resume", "owner")).status_code, 200)

    async def test_archived_objects_are_read_only_and_reversible(self):
        B = f"/bots/{self.w.bA.id}"
        self.assertEqual((await self.op(B, "archive", "admin")).status_code, 200)
        r = await self.w.call("PATCH", f"{B}/config", "owner", {"changes": {"behaviour.language": "de"}})
        self.assertEqual(r.status_code, 403)
        r = await self.w.call("POST", f"{B}/instances", "owner", {"name": "x", "channel": {"type": "webchat"}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual((await self.op(B, "unarchive", "admin")).status_code, 200)
        r = await self.w.call("PATCH", f"{B}/config", "owner", {"changes": {"behaviour.language": "de"}})
        self.assertEqual(r.status_code, 200)

    async def test_invalid_transitions(self):
        B = f"/bots/{self.w.bA.id}"
        self.assertEqual((await self.op(B, "resume")).status_code, 409)
        self.assertEqual((await self.op(B, "unarchive")).status_code, 409)
        r = await self.w.call("POST", f"{B}/suspend", "owner", {"expected_version": 999})
        self.assertEqual(r.status_code, 409)                                            # stale version


if __name__ == "__main__":
    unittest.main()
