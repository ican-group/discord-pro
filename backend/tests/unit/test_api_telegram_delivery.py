"""Telegram durable outbound delivery (Phase 4D, T-A correction)."""
import asyncio
import unittest
from datetime import datetime, timedelta

from tests.unit._api_fixtures import HAVE_API
from tests.unit.test_api_telegram import TelegramBase


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class DeliveryTests(TelegramBase):
    async def worker(self, offset_seconds=0):
        from api.telegram import channel_for
        from runtime.worker import Worker
        now = (datetime.fromisoformat(self.w.S.clock()) + timedelta(seconds=offset_seconds)).isoformat()
        return await Worker(self.w.store, clock=lambda: now, worker_id=f"w{offset_seconds}",
                            telegram=channel_for(self.w.S)).run_once()

    async def deliveries(self):
        return await self.w.store.find("outbound_deliveries", {"instance_id": self.I})

    async def test_normal_reply_is_recorded_as_delivered_with_the_message_id(self):
        await self.hook(self.message("hi"))
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "delivered")
        self.assertTrue(d["parts"][0]["message_id"])
        self.assertEqual((d["enterprise_id"], d["organisation_id"], d["bot_id"]),
                         (self.w.eA.id, self.w.oA.id, self.w.bA.id))
        self.assertEqual(d["target"]["chat_id"], 555)

    async def test_crash_before_sending_is_sent_by_the_worker(self):
        from runtime.telegram_channel import TelegramChannel
        original = TelegramChannel.deliver

        async def crash(self, did):
            return None                                   # process dies before the first send
        TelegramChannel.deliver = crash
        try:
            await self.hook(self.message("hi"))
        finally:
            TelegramChannel.deliver = original
        self.assertEqual(self.sent(), [])
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "pending")
        report = await self.worker()
        self.assertEqual(report.deliveries["delivered"], 1)
        self.assertEqual(len(self.sent()), 1)
        await self.worker(600)
        self.assertEqual(len(self.sent()), 1)             # never sent twice

    async def test_crash_during_sending_becomes_uncertain_and_is_never_resent(self):
        from runtime import delivery as dlv

        async def dies(self, doc, part):
            raise asyncio.CancelledError()                # worker killed mid-call
        from runtime.telegram_channel import TelegramChannel
        original = TelegramChannel.send_part
        TelegramChannel.send_part = dies
        try:
            with self.assertRaises(asyncio.CancelledError):
                from api.telegram import channel_for
                ch = channel_for(self.w.S)
                await self.hook(self.message("hi"))
        except AssertionError:
            pass
        finally:
            TelegramChannel.send_part = original
        [d] = await self.deliveries()
        self.assertEqual((d["status"], d["parts"][0]["state"]), ("sending", "sending"))
        await self.worker()                               # lease still live: untouched
        self.assertEqual((await self.deliveries())[0]["status"], "sending")
        report = await self.worker(dlv.LEASE_SECONDS + 5)
        self.assertEqual(report.deliveries["uncertain"], 1)
        [d] = await self.deliveries()
        self.assertEqual((d["status"], d["parts"][0]["state"], d["last_error"]), ("uncertain", "uncertain", "worker_lost"))
        await self.worker(3600)
        self.assertEqual(self.sent(), [])                 # no blind retry
        r = await self.w.call("GET", f"/instances/{self.I}/deliveries?status=uncertain", "admin")
        self.assertEqual([x["id"] for x in r.json()["deliveries"]], [d["id"]])
        self.assertNotIn("Telegram answer", r.text)       # operator view without message text

    async def test_crash_after_confirmed_send_is_finalised_without_resending(self):
        from runtime import delivery as dlv
        await self.hook(self.message("hi"))
        [d] = await self.deliveries()
        # simulate: the part was confirmed but the worker died before finalising the record
        await self.w.store.update("outbound_deliveries", d["id"], {"status": "sending",
                                                                    "lease_until": "2020-01-01T00:00:00+00:00"})
        report = await self.worker()
        self.assertEqual(report.deliveries["resumed"], 1)
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "delivered")
        self.assertEqual(len(self.sent()), 1)

    async def test_network_timeout_is_uncertain_and_visible(self):
        import httpx
        def timeout(request):
            if request.url.path.endswith("/sendMessage"):
                raise httpx.ReadTimeout("timed out", request=request)
            return None
        self.tg.intercept = timeout
        await self.hook(self.message("hi"))
        [d] = await self.deliveries()
        self.assertEqual((d["status"], d["last_error"]), ("uncertain", "uncertain"))
        self.tg.intercept = None
        await self.worker(3600)
        self.assertEqual(self.sent(), [])
        self.assertEqual((await self.w.call("GET", "/platform/workers", "root")).json()["deliveries"]["uncertain"], 1)

    async def test_connection_refused_is_retried_safely(self):
        import httpx
        fails = {"n": 2}

        def refuse(request):
            if request.url.path.endswith("/sendMessage") and fails["n"] > 0:
                fails["n"] -= 1
                raise httpx.ConnectError("refused", request=request)
            return None
        self.tg.intercept = refuse
        await self.hook(self.message("hi"))
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "retry")
        await self.worker(10)
        await self.worker(100)
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "delivered")
        self.assertEqual(len(self.sent()), 1)

    async def test_long_rate_limit_becomes_a_scheduled_retry(self):
        self.tg.fail["sendMessage"] = [(429, {"ok": False, "parameters": {"retry_after": 300}})]
        await self.hook(self.message("hi"))
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "retry")
        await self.worker(60)
        self.assertEqual((await self.deliveries())[0]["status"], "retry")     # not due yet
        await self.worker(400)
        self.assertEqual((await self.deliveries())[0]["status"], "delivered")

    async def test_concurrent_workers_send_once(self):
        from runtime import delivery as dlv
        from runtime.telegram_channel import TelegramChannel
        original = TelegramChannel.deliver

        async def skip(self, did):
            return None
        TelegramChannel.deliver = skip
        try:
            await self.hook(self.message("hi"))
        finally:
            TelegramChannel.deliver = original
        [d] = await self.deliveries()
        from api.telegram import channel_for
        ch = channel_for(self.w.S)
        await asyncio.gather(*[dlv.run(self.w.store, d["id"], ch.send_part, now=self.w.S.clock(), worker=f"w{i}")
                               for i in range(5)])
        self.assertEqual(len(self.sent()), 1)

    async def test_duplicate_updates_never_duplicate_replies(self):
        upd = self.message("hi")
        for _ in range(4):
            await self.hook(upd)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(len(await self.deliveries()), 1)

    async def test_tenant_isolation(self):
        await self.hook(self.message("hi"))
        r = await self.w.call("GET", f"/instances/{self.I}/deliveries", "owner_b")
        self.assertEqual(r.status_code, 404)
        r = await self.w.call("GET", f"/instances/{self.I}/deliveries", "viewer")
        self.assertEqual(r.status_code, 403)
        [d] = await self.deliveries()
        from runtime.telegram_channel import TelegramChannel
        from api.telegram import channel_for
        forged = {**d, "enterprise_id": self.w.eB.id}
        result = await channel_for(self.w.S).send_part(forged, d["parts"][0])
        self.assertEqual(result.status, "not_sent_final")


    async def test_retention_only_for_terminal_records(self):
        from runtime import conversations as conv
        from runtime import delivery as dlv
        await self.hook(self.message("hi"))
        [d] = await self.deliveries()
        self.assertEqual(d["status"], "delivered")
        ttl = d["expires_at_dt"] - datetime.fromisoformat(d["updated_at"])
        self.assertEqual(ttl, timedelta(days=dlv.TERMINAL_RETENTION_DAYS))
        self.assertGreater(ttl, timedelta(hours=conv.REQUEST_TTL_HOURS))   # never shorter than dedupe
        import httpx

        def timeout(request):
            if request.url.path.endswith("/sendMessage"):
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.tg.intercept = timeout
        await self.hook(self.message("again"))
        self.tg.intercept = None
        unc = [x for x in await self.deliveries() if x["status"] == "uncertain"]
        self.assertEqual(len(unc), 1)
        self.assertNotIn("expires_at_dt", unc[0])                      # preserved until reviewed

    async def test_operator_resolution_is_tenant_scoped_and_never_resends(self):
        import httpx

        def timeout(request):
            if request.url.path.endswith("/sendMessage"):
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.tg.intercept = timeout
        await self.hook(self.message("hi"))
        self.tg.intercept = None
        [d] = await self.deliveries()
        path = f"/instances/{self.I}/deliveries/{d['id']}/resolve"
        self.assertEqual((await self.w.call("POST", path, "owner_b", {"note": "x"})).status_code, 404)
        self.assertEqual((await self.w.call("POST", path, "viewer", {"note": "x"})).status_code, 403)
        self.assertEqual((await self.w.call("POST", path, "admin", {"note": ""})).status_code, 422)
        wrong = f"/instances/{self.w.iA_web.id}/deliveries/{d['id']}/resolve"
        self.assertEqual((await self.w.call("POST", wrong, "admin", {"note": "x"})).status_code, 404)
        r = await self.w.call("POST", path, "admin", {"note": "Customer confirmed receipt."})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "resolved"))
        self.assertTrue(r.json()["expires_at"])
        self.assertEqual((await self.w.call("POST", path, "admin", {"note": "again"})).status_code, 409)
        await self.worker(7200)
        self.assertEqual(self.sent(), [])


if __name__ == "__main__":
    unittest.main()
