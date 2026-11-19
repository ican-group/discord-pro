"""Phase 6A (H): Organisation deletion racing live activity.

Finding (4F load test): requests already in flight when an Organisation was
deleted wrote runtime records after the purge (removed later by the grace
re-purge). These tests prove that such records are inert — never readable,
usable or delivered — and that lifecycle checks stop new activity at the
moment of confirmation (generation, webhooks, outbound delivery, Zendesk).
"""
import asyncio
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta

from tests.unit._api_fixtures import HAVE_API
from tests.unit.test_api_webchat import FakeProvider, WebchatBase

RUNTIME = ("conversations", "chat_sessions", "chat_requests", "usage_events", "usage_ledger", "outbound_deliveries",
           "channel_state", "suggestions", "zendesk_events")


class GateProvider(FakeProvider):
    """Blocks inside generate() until released, so deletion can happen mid-generation."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def generate(self, request):
        self.entered.set()
        await self.release.wait()
        return await super().generate(request)


@contextmanager
def background_job_suppressed():
    """Confirmation runs the immediate steps; the rest normally follows in a
    background task. Suppressing it freezes the 'during deletion' window."""
    import api.deletion as ad
    real = ad.dl.run_job

    async def only_immediate(store, job_id, *, clock, only=None, **kw):
        if only is None:
            return None
        return await real(store, job_id, clock=clock, only=only, **kw)
    ad.dl.run_job = only_immediate
    try:
        yield real
    finally:
        ad.dl.run_job = real


async def delete_org(w, org_id, name="Org A", who="owner"):
    pv = (await w.call("POST", f"/organisations/{org_id}/deletion-preview", who)).json()
    return await w.call("POST", f"/organisations/{org_id}/delete", who,
                        {"preview_token": pv["preview_token"], "confirm_name": name})


async def leftovers(w, org_id):
    out = {}
    for col in RUNTIME:
        n = await w.store.count(col, {"organisation_id": org_id})
        if n:
            out[col] = n
    return out


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class WebchatDeletionRaceTests(WebchatBase):
    async def test_generation_in_flight_is_never_delivered_and_its_records_are_inert(self):
        gate = GateProvider(text="Secret answer after deletion.")
        self.w.S.providers = {"anthropic": gate}
        s = await self.new_session()
        task = asyncio.create_task(self.chat("hello", session=s))
        await asyncio.wait_for(gate.entered.wait(), 5)
        with background_job_suppressed() as real_run:
            r = await delete_org(self.w, self.w.oA.id)
            self.assertEqual(r.status_code, 202, r.text)
            job_id = r.json()["id"]
            gate.release.set()
            resp = await task
            self.assertEqual(resp.status_code, 404)                                  # nothing delivered
            self.assertNotIn("Secret answer", resp.text)
            convs = await self.w.store.find("conversations", {"instance_id": self.w.iA_web.id})
            self.assertNotIn("Secret answer", json.dumps(convs))                    # nothing stored as a reply
            ledger = await self.w.store.find("usage_ledger", {"organisation_id": self.w.oA.id})
            self.assertEqual([d["state"] for d in ledger], ["settled"])             # the provider ran: billed once
            await self.assert_inert(s)
            await real_run(self.w.store, job_id, clock=self.w.S.clock)
        await self.assert_inert(s)
        # the Discord leave-guild action has no adapter here: record it as done so the job can finish
        from core import deletion as dl
        job = await self.w.store.get("deletion_jobs", job_id)
        acts = [{**x, "state": "succeeded"} for x in job["external_actions"]]
        await self.w.store.update("deletion_jobs", job_id, {"external_actions": acts}, expect_version=job["version"])
        job = await dl.run_job(self.w.store, job_id, clock=self.w.S.clock)
        self.assertEqual(job["status"], "completed")
        # grace re-purge removes whatever an in-flight request wrote after the purge
        later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(seconds=dl.REPURGE_DELAY_SECONDS + 1)).isoformat()
        await self.w.store.insert("conversations", {"id": "late-write", "enterprise_id": self.w.eA.id,
                                                    "organisation_id": self.w.oA.id, "bot_id": self.w.bA.id,
                                                    "instance_id": self.w.iA_web.id, "messages": [], "version": 1})
        self.assertEqual(await dl.repurge_due(self.w.store, now=later), 1)
        self.assertEqual(await leftovers(self.w, self.w.oA.id), {})
        self.assertEqual(await dl.repurge_due(self.w.store, now=later), 0)          # idempotent
        self.assertTrue(await self.w.store.find("audit_events", {"action": "deletion.confirm"}))   # audit kept

    async def assert_inert(self, session):
        P = self.P
        self.assertEqual((await self.chat("again", session=session)).status_code, 404)
        self.assertEqual((await self.pub("POST", P + "/sessions")).status_code, 404)
        self.assertEqual((await self.pub("GET", f"{P}/sessions/{session[0]}/history",
                                         headers={"X-Chat-Session-Token": session[1]})).status_code, 404)
        for path in (f"/instances/{self.w.iA_web.id}/conversations", f"/instances/{self.w.iA_web.id}/usage",
                     f"/organisations/{self.w.oA.id}/usage", f"/bots/{self.w.bA.id}/usage"):
            self.assertEqual((await self.w.call("GET", path, "owner")).status_code, 404, path)
        self.assertEqual((await self.w.call("POST", f"/instances/{self.w.iA_web.id}/preview-session", "owner")).status_code, 404)

    async def test_suspension_mid_generation_also_stops_delivery(self):
        gate = GateProvider(text="Answer.")
        self.w.S.providers = {"anthropic": gate}
        task = asyncio.create_task(self.chat("hello"))
        await asyncio.wait_for(gate.entered.wait(), 5)
        await self.w.call("POST", f"/organisations/{self.w.oA.id}/suspend", "owner", {"reason": "x"})
        gate.release.set()
        self.assertEqual((await task).status_code, 404)

    async def test_concurrent_confirmations_converge_on_one_job(self):
        with background_job_suppressed():
            p1 = (await self.w.call("POST", f"/organisations/{self.w.oA.id}/deletion-preview", "owner")).json()
            p2 = (await self.w.call("POST", f"/organisations/{self.w.oA.id}/deletion-preview", "owner")).json()
            rs = await asyncio.gather(*[self.w.call("POST", f"/organisations/{self.w.oA.id}/delete", "owner",
                                                    {"preview_token": p["preview_token"], "confirm_name": "Org A"})
                                        for p in (p1, p2)])
        self.assertEqual(sorted(r.status_code for r in rs)[0], 202)
        self.assertEqual(sum(r.status_code == 202 for r in rs), 1)
        self.assertEqual(await self.w.store.count("deletion_jobs", {"target_id": self.w.oA.id}), 1)

    async def test_worker_crash_resumes_and_other_tenants_are_untouched(self):
        from runtime.worker import Worker
        from core import deletion as dl
        other = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner", {"name": "Org A2"})).json()
        for ent, org in ((self.w.eA.id, other["id"]), (self.w.eB.id, self.w.oB.id)):
            await self.w.store.insert("conversations", {"id": f"keep-{org}", "enterprise_id": ent, "organisation_id": org,
                                                        "bot_id": "b", "instance_id": "i", "messages": [], "version": 1})
        await self.chat("hello")
        with background_job_suppressed():
            job_id = (await delete_org(self.w, self.w.oA.id)).json()["id"]        # "crash": background never ran
        job = await self.w.store.get("deletion_jobs", job_id)
        self.assertNotEqual(job["status"], "completed")
        later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(minutes=10)).isoformat()
        await Worker(self.w.store, clock=lambda: later, worker_id="w2").run_once()
        job = await self.w.store.get("deletion_jobs", job_id)
        self.assertIn(job["status"], ("completed", "awaiting_external_cleanup"))
        self.assertEqual(await leftovers(self.w, self.w.oA.id), {})
        for org in (other["id"], self.w.oB.id):
            self.assertTrue(await self.w.store.get("conversations", f"keep-{org}"))
        self.assertTrue(await self.w.store.get("organisations", other["id"]))


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class TelegramDeletionRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.unit.test_api_telegram import TelegramBase
        self.base = TelegramBase()
        await TelegramBase.asyncSetUp(self.base)
        self.b = self.base

    async def asyncTearDown(self):
        from tests.unit.test_api_telegram import TelegramBase
        await TelegramBase.asyncTearDown(self.b)

    async def test_webhook_processing_during_deletion_sends_nothing(self):
        gate = GateProvider(text="Telegram answer.")
        self.b.w.S.providers = {"anthropic": gate}
        task = asyncio.create_task(self.b.hook(self.b.message("hi")))
        await asyncio.wait_for(gate.entered.wait(), 5)
        with background_job_suppressed():
            r = await delete_org(self.b.w, self.b.w.oA.id)
            self.assertEqual(r.status_code, 202, r.text)
            gate.release.set()
            await task
            self.assertEqual(self.b.sent(), [])
            r = await self.b.hook(self.b.message("again"))
            self.assertIn(r.status_code, (200, 404))                                 # acknowledged or unknown
            self.assertEqual(self.b.sent(), [])

    async def test_queued_outbound_delivery_is_not_sent_after_confirmation(self):
        from runtime.telegram_channel import TelegramChannel
        from runtime.worker import Worker
        from api.telegram import channel_for
        original = TelegramChannel.deliver

        async def crash(self, did):
            return None
        TelegramChannel.deliver = crash
        try:
            await self.b.hook(self.b.message("hi"))
        finally:
            TelegramChannel.deliver = original
        [d] = await self.b.w.store.find("outbound_deliveries", {"instance_id": self.b.I})
        self.assertEqual(d["status"], "pending")
        with background_job_suppressed():
            await delete_org(self.b.w, self.b.w.oA.id)
            await Worker(self.b.w.store, clock=self.b.w.S.clock, worker_id="w", telegram=channel_for(self.b.w.S)).run_once()
        self.assertEqual(self.b.sent(), [])
        d = await self.b.w.store.get("outbound_deliveries", d["id"])
        self.assertTrue(d is None or d["status"] != "delivered")


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ZendeskDeletionRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.unit.test_api_zendesk_lifecycle import LifecycleBase
        self.b = LifecycleBase()
        await LifecycleBase.asyncSetUp(self.b)

    async def test_no_zendesk_request_starts_once_the_organisation_is_being_deleted(self):
        await self.b.settings(reply_mode="auto_reply")
        path = await self.b.hook_url(None)
        before = len(self.b.zd.calls)
        with background_job_suppressed():
            r = await delete_org(self.b.w, self.b.w.oA.id)
            self.assertEqual(r.status_code, 202, r.text)
            await self.b.webhook(path=path)
            await self.b.svc(5).maintain(limit=10)
        posted = [c for c in self.b.zd.calls[before:] if c[1] in ("PUT", "POST") and "/tickets/" in c[2]]
        self.assertEqual(posted, [])
        ok, reason = await self.b.svc()._lease(self.b.Z, "probe")
        self.assertEqual((ok, reason), (False, "integration_unavailable"))


if __name__ == "__main__":
    unittest.main()
