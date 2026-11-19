"""Permanent deletion through the API (spec 012 §8, D-Q3)."""
import asyncio
import unittest

from tests.unit._api_fixtures import HAVE_API, World


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class DeletionApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()

    async def preview(self, path, who="owner"):
        r = await self.w.call("POST", f"{path}/deletion-preview", who)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def test_bot_deletion_end_to_end(self):
        B = f"/bots/{self.w.bA.id}"
        pv = await self.preview(B, "admin")
        self.assertEqual(pv["counts"]["bot_instances"], 3)
        self.assertIn("telegram_delete_webhook", [a["action"] for a in pv["external_actions"]])
        r = await self.w.call("POST", f"{B}/delete", "admin", {"preview_token": pv["preview_token"],
                                                               "confirm_name": "wrong"})
        self.assertEqual(r.status_code, 409)
        pv = await self.preview(B, "admin")
        r = await self.w.call("POST", f"{B}/delete", "admin", {"preview_token": pv["preview_token"],
                                                               "confirm_name": self.w.bA.name})
        self.assertEqual(r.status_code, 202, r.text)
        job_id = r.json()["id"]
        self.assertEqual((await self.w.call("GET", B, "owner")).status_code, 404)        # immediate
        self.assertEqual((await self.w.call("GET", f"/instances/{self.w.iA_web.id}", "owner")).status_code, 404)
        r = await self.w.call("GET", f"/deletion-jobs/{job_id}", "admin")                 # requester
        job = r.json()
        # X12: data removed, but mandatory Telegram/Discord cleanup has no adapter yet
        self.assertEqual(job["status"], "awaiting_external_cleanup")
        self.assertEqual(job["steps_done"][-1], "target")
        mandatory = {a["action"] for a in job["external_actions"] if a["mandatory"]}
        self.assertEqual(mandatory, {"telegram_delete_webhook", "discord_routing_removal"})
        self.assertEqual(await self.w.store.count("bot_instances", {"bot_id": self.w.bA.id}), 0)
        self.assertEqual(await self.w.store.count("bot_instances", {"bot_id": self.w.bB.id}), 1)
        # the Telegram bot and webchat public id are free again
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/bots", "owner", {"name": "New"})
        r = await self.w.call("POST", f"/bots/{r.json()['id']}/instances", "owner",
                              {"name": "TG", "channel": {"type": "telegram", "credential_id": self.w.cA_tg.id}})
        self.assertEqual(r.status_code, 201, r.text)

    async def test_token_is_bound_to_actor_and_target_and_single_use(self):
        B = f"/bots/{self.w.bA.id}"
        pv = await self.preview(B, "admin")
        body = {"preview_token": pv["preview_token"], "confirm_name": self.w.bA.name}
        r = await self.w.call("POST", f"{B}/delete", "owner", body)                       # other actor
        self.assertEqual(r.status_code, 409)
        pv = await self.preview(B, "admin")
        body = {"preview_token": pv["preview_token"], "confirm_name": self.w.oA.name}
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/delete", "owner", body)   # other target
        self.assertEqual(r.status_code, 409)
        pv = await self.preview(B, "admin")
        body = {"preview_token": pv["preview_token"], "confirm_name": self.w.bA.name}
        results = await asyncio.gather(*[self.w.call("POST", f"{B}/delete", "admin", body) for _ in range(4)])
        self.assertEqual(sorted(r.status_code for r in results).count(202), 1)

    async def test_changed_data_invalidates_the_preview(self):
        O = f"/organisations/{self.w.oA.id}"
        pv = await self.preview(O, "owner")
        await self.w.call("POST", f"{O}/bots", "admin", {"name": "Late"})
        r = await self.w.call("POST", f"{O}/delete", "owner", {"preview_token": pv["preview_token"],
                                                               "confirm_name": self.w.oA.name})
        self.assertEqual(r.status_code, 409)
        self.assertEqual((await self.w.call("GET", O, "owner")).status_code, 200)

    async def test_enterprise_deletion_removes_only_that_enterprise(self):
        E = f"/enterprises/{self.w.eA.id}"
        self.assertEqual((await self.w.call("POST", f"{E}/deletion-preview", "admin")).status_code, 403)
        pv = await self.preview(E, "owner")
        r = await self.w.call("POST", f"{E}/delete", "owner", {"preview_token": pv["preview_token"],
                                                               "confirm_name": self.w.eA.name})
        self.assertEqual(r.status_code, 202, r.text)
        store = self.w.store
        for col in ("organisations", "bots", "bot_instances", "invitations", "memberships"):
            self.assertEqual(await store.count(col, {"enterprise_id": self.w.eA.id}), 0, col)
        # X12: only the Telegram credential that the pending webhook cleanup needs is kept (encrypted)
        kept = await store.find("credentials", {"enterprise_id": self.w.eA.id})
        self.assertEqual([c["id"] for c in kept], [self.w.cA_tg.id])
        self.assertIsNone(await store.get("enterprises", self.w.eA.id))
        self.assertIsNone(await store.get("membership_guards", self.w.eA.id))
        self.assertIsNotNone(await store.get("enterprises", self.w.eB.id))
        self.assertEqual(await store.count("bot_instances", {"enterprise_id": self.w.eB.id}), 1)
        self.assertEqual(await store.count("credentials", {"enterprise_id": self.w.eB.id}), 1)
        job_id = r.json()["id"]
        r = await self.w.call("GET", f"/deletion-jobs/{job_id}", "owner")
        self.assertEqual(r.json()["status"], "awaiting_external_cleanup")
        # operator establishes every outstanding action as not required -> job completes, credential released
        for a in r.json()["external_actions"]:
            if a["state"] not in ("succeeded", "not_required"):
                rr = await self.w.call("POST", f"/deletion-jobs/{job_id}/actions/{a['id']}/resolve", "root",
                                       {"reason": "test environment: resource never existed"})
                self.assertEqual(rr.status_code, 200, rr.text)
        r = await self.w.call("GET", f"/deletion-jobs/{job_id}", "owner")
        self.assertEqual(r.json()["status"], "completed")
        self.assertEqual(await store.count("credentials", {"enterprise_id": self.w.eA.id}), 0)
        events = (await self.w.call("GET", "/platform/audit", "root")).json()["events"]
        self.assertIn("deletion.confirm", [e["action"] for e in events])

    async def test_user_self_deletion(self):
        pv = (await self.w.call("POST", "/users/me/deletion-preview", "viewer")).json()
        self.assertEqual(pv["blocked"], [])
        r = await self.w.call("POST", "/users/me/delete", "viewer", {"preview_token": pv["preview_token"],
                                                                     "confirm_email": "viewer@example.test"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["status"], "completed")
        self.assertEqual((await self.w.call("GET", "/users/me", "viewer")).status_code, 401)   # session ended
        from core import memberships as ms
        self.assertFalse((await ms.load(self.w.store, self.w.eA.id)).for_user(self.w.users["viewer"].id))

    async def test_last_owner_and_last_superadmin_cannot_delete_themselves(self):
        for who, reason in (("owner", "last_owner"), ("root", "last_superadmin")):
            pv = (await self.w.call("POST", "/users/me/deletion-preview", who)).json()
            self.assertIn(reason, [b["reason"] for b in pv["blocked"]])
            r = await self.w.call("POST", "/users/me/delete", who, {"preview_token": pv["preview_token"],
                                                                    "confirm_email": f"{who}@example.test"})
            self.assertEqual(r.status_code, 409)
            self.assertEqual((await self.w.call("GET", "/users/me", who)).status_code, 200)

    async def test_superadmin_demotion_safeguard(self):
        root = self.w.users["root"].id
        r = await self.w.call("POST", f"/platform/users/{root}/superadmin", "root", {"grant": False})
        self.assertEqual(r.status_code, 403)
        other = self.w.users["outsider"].id
        r = await self.w.call("POST", f"/platform/users/{other}/superadmin", "root", {"grant": True})
        self.assertEqual(r.status_code, 200)
        r = await self.w.call("POST", f"/platform/users/{root}/superadmin", "root", {"grant": False})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ExternalCleanupApiTests(unittest.IsolatedAsyncioTestCase):
    """X12 through the API: credential holds and operator controls."""

    async def asyncSetUp(self):
        self.w = await World().build()

    async def delete_bot(self):
        B = f"/bots/{self.w.bA.id}"
        pv = (await self.w.call("POST", f"{B}/deletion-preview", "owner")).json()
        r = await self.w.call("POST", f"{B}/delete", "owner", {"preview_token": pv["preview_token"],
                                                               "confirm_name": self.w.bA.name})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["id"]

    async def test_credential_needed_for_cleanup_cannot_be_revoked_or_deleted(self):
        job_id = await self.delete_bot()
        C = f"/credentials/{self.w.cA_tg.id}"
        for method, path in (("POST", f"{C}/revoke"), ("DELETE", C)):
            before = await self.w.snapshot()
            r = await self.w.call(method, path, "owner")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(await self.w.snapshot(), before)
        action = f"telegram_delete_webhook:{self.w.iA_tg.id}"
        r = await self.w.call("POST", f"/deletion-jobs/{job_id}/actions/{action}/resolve", "root",
                              {"reason": "webhook removed manually"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await self.w.call("POST", f"{C}/revoke", "owner")).status_code, 200)

    async def test_operator_controls_are_superadmin_only_and_audited(self):
        job_id = await self.delete_bot()
        action = f"telegram_delete_webhook:{self.w.iA_tg.id}"
        for who in ("owner", "admin", "owner_b"):
            r = await self.w.call("POST", f"/deletion-jobs/{job_id}/actions/{action}/resolve", who, {"reason": "x"})
            self.assertEqual(r.status_code, 404)
        r = await self.w.call("POST", f"/deletion-jobs/{job_id}/actions/{action}/resolve", "root", {"reason": ""})
        self.assertEqual(r.status_code, 409)
        r = await self.w.call("POST", f"/deletion-jobs/{job_id}/actions/{action}/resolve", "root",
                              {"reason": "confirmed with operator"})
        tg = next(a for a in r.json()["external_actions"] if a["id"] == action)
        self.assertEqual((tg["state"], tg["resolution"]["by"]), ("not_required", self.w.users["root"].id))
        events = (await self.w.call("GET", "/platform/audit", "root")).json()["events"]
        self.assertIn("deletion.cleanup_resolved_by_operator", [e["action"] for e in events])
        self.assertEqual(r.json()["status"], "awaiting_external_cleanup")       # Discord actions still open
