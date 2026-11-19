"""Webchat billing idempotency (4C Part C).

Failures injected between generation, result persistence, usage settlement
and conversation storage; crash simulations; concurrent duplicates; worker
repair. In every case a client message is generated (billed) at most once."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.unit._api_fixtures import ANTHROPIC_KEY, HAVE_API
from tests.unit.test_api_webchat import WebchatBase

CMID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class BillingIdempotencyTests(WebchatBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session = await self.new_session()
        self.rid = f"{self.session[0]}:{CMID}"

    async def send(self, stream=False):
        return await self.chat("What are your hours?", session=self.session, cmid=CMID, stream=stream)

    async def billing_state(self):
        ledger = await self.w.store.get("usage_ledger", self.rid)
        events = await self.w.store.find("usage_events", {"kind": "llm_call"})
        counter = await self.w.store.get("usage_counters", f"{self.w.eA.id}:" + self.w.S.clock()[:7])
        conv = await self.w.store.get("conversations", self.session[0])
        return (ledger and ledger["state"], len(events), counter and counter["count"],
                len((conv or {}).get("messages") or []))

    async def test_happy_path_bills_exactly_once_and_replays(self):
        first = await self.send()
        self.assertEqual(first.status_code, 200)
        for stream in (False, True, False):
            again = await self.send(stream=stream)
            self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(await self.billing_state(), ("settled", 1, 1, 2))
        ledger = await self.w.store.get("usage_ledger", self.rid)
        self.assertEqual((ledger["enterprise_id"], ledger["instance_id"]), (self.w.eA.id, self.w.iA_web.id))
        self.assertNotIn(ANTHROPIC_KEY, json.dumps(ledger, default=str) +
                         json.dumps(await self.w.store.find("usage_events", {}), default=str))

    async def test_failure_between_generation_and_persistence_is_never_regenerated(self):
        from runtime import conversations as conv
        original = conv.mark_generated

        async def broken(*a, **kw):
            raise RuntimeError("storage down")
        conv.mark_generated = broken
        try:
            r = await self.send()
        finally:
            conv.mark_generated = original
        self.assertEqual(r.status_code, 409)                     # uncertain: send as a new message
        for _ in range(3):
            self.assertEqual((await self.send()).status_code, 409)
        self.assertEqual(len(self.provider.calls), 1)            # never regenerated
        ledger, events, counter, msgs = await self.billing_state()
        self.assertEqual((ledger, events, counter), ("reserved", 0, 1))   # counted once, not settled twice
        # a new message (new id) works normally
        ok = await self.chat("again", session=self.session)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(self.provider.calls), 2)

    async def test_failure_between_persistence_and_settlement_is_completed_on_retry(self):
        from runtime.usage import StoreUsageTracker
        original = StoreUsageTracker.settle

        async def broken(self, *a, **kw):
            raise RuntimeError("settlement store down")
        StoreUsageTracker.settle = broken
        try:
            r = await self.send()
        finally:
            StoreUsageTracker.settle = original
        self.assertEqual(r.status_code, 503)                     # the answer is stored; retry completes it
        rec = await self.w.store.get("chat_requests", self.rid)
        self.assertEqual(rec["status"], "generated")
        again = await self.send()
        self.assertEqual((again.status_code, again.json()["replayed"]), (200, True))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(await self.billing_state(), ("settled", 1, 1, 2))

    async def test_failure_between_settlement_and_conversation_is_completed_without_duplicates(self):
        from runtime import conversations as conv
        original = conv.append_turn
        calls = {"n": 0}

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("conversation store down")
            return await original(*a, **kw)
        conv.append_turn = flaky
        try:
            r = await self.send()
            self.assertEqual(r.status_code, 503)
            again = await self.send(stream=True)
            self.assertEqual(again.status_code, 200)
            self.assertIn("Hello from the bot.", again.text)
            third = await self.send()
            self.assertEqual(third.status_code, 200)
        finally:
            conv.append_turn = original
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(await self.billing_state(), ("settled", 1, 1, 2))

    async def test_crash_during_provider_call_is_uncertain_not_regenerated(self):
        from runtime import conversations as conv
        from core.hierarchy import Level
        chain = await self.w.S.hierarchy.load_chain(Level.BOT_INSTANCE, self.w.iA_web.id)
        # state left by a process that died after reserving and calling the provider
        await conv.begin_request(self.w.store, chain, self.session[0], CMID, now="2026-01-01T00:00:00+00:00")
        from runtime.usage import StoreUsageTracker
        await StoreUsageTracker(self.w.store).reserve_ai_message(chain, now=self.w.S.clock(), key=self.rid)
        r = await self.send()                                     # lease long expired
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual((await self.w.store.get("chat_requests", self.rid))["status"], "uncertain")

    async def test_crash_before_reservation_is_safely_retried(self):
        from runtime import conversations as conv
        from core.hierarchy import Level
        chain = await self.w.S.hierarchy.load_chain(Level.BOT_INSTANCE, self.w.iA_web.id)
        await conv.begin_request(self.w.store, chain, self.session[0], CMID, now="2026-01-01T00:00:00+00:00")
        busy = await conv.begin_request(self.w.store, chain, self.session[0], CMID, now="2026-01-01T00:00:30+00:00")
        self.assertEqual(busy[0], "busy")                          # live lease respected
        r = await self.send()                                       # stale and nothing reserved: re-run once
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(await self.billing_state(), ("settled", 1, 1, 2))

    async def test_concurrent_duplicates_generate_once(self):
        self.provider.delay = 0.05
        results = await asyncio.gather(*[self.send(stream=bool(i % 2)) for i in range(6)])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(all(r.status_code in (200, 409) for r in results))
        self.assertEqual((await self.billing_state())[:3], ("settled", 1, 1))

    async def test_refused_and_limited_requests_are_not_billed(self):
        await self.strict(allowed_sources=[])
        self.assertEqual((await self.send()).status_code, 200)       # strict refusal before any AI call
        self.assertIsNone(await self.w.store.get("usage_ledger", self.rid))
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.minimum_mode": "general"}})
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 0}})
        r = await self.chat("q", session=self.session)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(await self.w.store.count("usage_events", {"kind": "llm_call"}), 0)
        self.assertEqual(await self.w.store.count("usage_ledger", {"state": ["reserved", "settled"]}), 0)

    async def test_worker_repairs_crashed_requests_and_counters(self):
        from runtime import conversations as conv
        from runtime.usage import StoreUsageTracker
        from runtime.worker import Worker
        from core.hierarchy import Level
        chain = await self.w.S.hierarchy.load_chain(Level.BOT_INSTANCE, self.w.iA_web.id)
        usage = StoreUsageTracker(self.w.store)
        past = "2026-01-01T00:00:00+00:00"
        # (a) generated but never completed
        original = usage.settle
        from runtime.usage import StoreUsageTracker as T
        orig_settle = T.settle

        async def broken(self, *a, **kw):
            raise RuntimeError("down")
        T.settle = broken
        try:
            await self.send()
        finally:
            T.settle = orig_settle
        await self.w.store.update("chat_requests", self.rid, {"lease_until": past})
        # (b) processing, nothing reserved; (c) processing with a reservation
        cm_b, cm_c = "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d", "2c3d4e5f-6071-4b8c-9d0e-1f2a3b4c5d6e"
        await conv.begin_request(self.w.store, chain, self.session[0], cm_b, now=past)
        await conv.begin_request(self.w.store, chain, self.session[0], cm_c, now=past)
        await usage.reserve_ai_message(chain, now=past.replace("2026-01", self.w.S.clock()[:7]),
                                       key=f"{self.session[0]}:{cm_c}")
        await self.w.store.update("usage_ledger", f"{self.session[0]}:{cm_c}", {"updated_at": past})
        stale_counter = f"{self.w.eA.id}:" + self.w.S.clock()[:7]
        doc = await self.w.store.get("usage_counters", stale_counter)
        await self.w.store.update("usage_counters", stale_counter, {"count": doc["count"] + 5})   # leaked count
        report = await Worker(self.w.store, clock=self.w.S.clock, worker_id="repair").run_once()
        self.assertEqual(report.requests_repaired, {"completed": 1, "removed": 1, "uncertain": 1})
        self.assertEqual(report.usage_reconciled["uncertain"], 1)
        self.assertEqual((await self.w.store.get("chat_requests", self.rid))["status"], "done")
        self.assertIsNone(await self.w.store.get("chat_requests", f"{self.session[0]}:{cm_b}"))
        self.assertEqual((await self.w.store.get("chat_requests", f"{self.session[0]}:{cm_c}"))["status"], "uncertain")
        counter = await self.w.store.get("usage_counters", stale_counter)
        self.assertEqual(counter["count"], 2)                      # one settled + one uncertain; leak removed
        self.assertEqual(await self.w.store.count("usage_events", {"kind": "llm_call"}), 1)

    async def test_usage_is_never_credited_to_another_tenant(self):
        await self.send()
        other = await self.chat("hi", path=f"/public/instances/{self.w.iB_web.channel.public_id}")
        self.assertEqual(other.status_code, 200)                    # B has no AI credential: fallback, unbilled
        for ledger in await self.w.store.find("usage_ledger", {}):
            self.assertEqual(ledger["enterprise_id"], self.w.eA.id)
        self.assertIsNone(await self.w.store.get("usage_counters", f"{self.w.eB.id}:" + self.w.S.clock()[:7]))


if __name__ == "__main__":
    unittest.main()
