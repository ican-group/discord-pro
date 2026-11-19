"""Phase 4F load and concurrency tests on an isolated standalone MongoDB.

Runs only with MONGO_TEST_URL (disposable server) and LOAD_TEST=1. Writes
measured latencies and error counts to LOAD_TEST_REPORT. All external
services are simulated."""
import asyncio
import json
import os
import statistics
import time
import unittest
import uuid

from tests.unit import _support  # noqa: F401
from tests.unit._api_fixtures import HAVE_API

MONGO_TEST_URL = os.environ.get("MONGO_TEST_URL", "")
SAFE = bool(MONGO_TEST_URL) and "bitbotpro-mongo" not in MONGO_TEST_URL and os.environ.get("LOAD_TEST") == "1"
REPORT = {}


def summary(latencies, errors=0, **extra):
    lat = sorted(latencies)
    q = (lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))] * 1000, 1)) if lat else (lambda p: None)
    return {"requests": len(lat), "errors": errors, "p50_ms": q(0.5), "p95_ms": q(0.95),
            "max_ms": round(lat[-1] * 1000, 1) if lat else None, **extra}


async def timed(coro):
    t = time.perf_counter()
    try:
        r = await coro
        return time.perf_counter() - t, r, None
    except Exception as exc:                                   # counted, never hidden
        return time.perf_counter() - t, None, type(exc).__name__


@unittest.skipUnless(SAFE and HAVE_API, "set MONGO_TEST_URL (disposable) and LOAD_TEST=1")
class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        from persistence.mongo import MongoStore, prepare_database
        from tests.unit._api_fixtures import World
        from tests.unit.test_api_webchat import FakeProvider
        asyncio.get_running_loop().slow_callback_duration = 10
        self.client = AsyncIOMotorClient(MONGO_TEST_URL, serverSelectionTimeoutMS=10000, maxPoolSize=200)
        self.db_name = f"bbp_itest_load_{uuid.uuid4().hex[:8]}"
        self.db = self.client[self.db_name]
        await prepare_database(self.db)
        self.store = MongoStore(self.db)
        self.w = await World(self.store).build()
        self.provider = FakeProvider(text="Load answer.")
        self.w.S.providers = {"anthropic": self.provider}
        r = await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                              {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        assert r.status_code == 200, r.text

    async def asyncTearDown(self):
        await self.client.drop_database(self.db_name)
        self.client.close()
        path = os.environ.get("LOAD_TEST_REPORT")
        if path:
            existing = json.load(open(path)) if os.path.exists(path) else {}
            existing.update(REPORT)
            json.dump(existing, open(path, "w"), indent=2)

    def http(self, ip):
        import httpx
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app, client=(ip, 40000)),
                                 base_url="http://test")

    async def chat(self, i, body, token=None):
        h = {"Origin": "https://shop.example", **({"X-Chat-Session-Token": token} if token else {})}
        async with self.http(f"198.51.{i // 250}.{i % 250 + 1}") as c:
            return await c.post(f"/api/public/instances/{self.w.iA_web.channel.public_id}/chat", json=body,
                                headers=h)

    async def test_webchat_duplicates_and_quota_contention(self):
        s = (await self.chat(0, {"message": "start"})).json()
        cmid = str(uuid.uuid4())
        calls_before = len(self.provider.calls)
        res = await asyncio.gather(*[timed(self.chat(i + 1, {"message": "dup", "session_id": s["session_id"],
                                                             "client_message_id": cmid}, token=s["session_token"]))
                                     for i in range(50)])
        REPORT["debug_duplicates"] = sorted({(r[1].status_code, r[1].text[:80]) if r[1] is not None else (0, r[2])
                                             for r in res}, key=str)
        REPORT["debug_start"] = str(s)[:200]
        self.assertEqual(len(self.provider.calls) - calls_before, 1)          # one generation for 50 duplicates
        REPORT["duplicate_ai_requests_x50"] = summary([r[0] for r in res], sum(1 for r in res if r[2]),
                                                      generations=len(self.provider.calls) - calls_before)
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 20 + len(self.provider.calls)}})
        before = len(self.provider.calls)
        res = await asyncio.gather(*[timed(self.chat(100 + i, {"message": f"q{i}"})) for i in range(100)])
        statuses = [r[1].status_code for r in res if r[1] is not None]
        self.assertEqual(len(self.provider.calls) - before, 20)            # exactly the remaining quota
        settled = await self.db.usage_ledger.count_documents({"state": "settled"})
        REPORT["quota_contention_100_for_20"] = summary([r[0] for r in res], sum(1 for r in res if r[2]),
                                                        generations=len(self.provider.calls) - before,
                                                        ledger_settled=settled,
                                                        http_200=statuses.count(200), http_429=statuses.count(429))

    async def test_telegram_webhook_burst_with_duplicates(self):
        import httpx
        await self.w.call("POST", f"/instances/{self.w.iA_tg.id}/channel/connect", "admin")
        url, header = [(c[2]["url"], c[2]["secret_token"]) for c in self.w.telegram.methods("setWebhook")][-1]
        path = "/api/hooks" + url.split("/api/hooks", 1)[1]

        async def hook(uid):
            upd = {"update_id": uid, "message": {"message_id": uid, "text": f"hi {uid}",
                                                 "chat": {"id": 1000 + uid, "type": "private"},
                                                 "from": {"id": 7, "is_bot": False}}}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
                return await c.post(path, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": header})
        ids = list(range(1, 101)) * 2                                        # 100 updates, each delivered twice
        res = await asyncio.gather(*[timed(hook(i)) for i in ids])
        sends = len(self.w.telegram.methods("sendMessage"))
        REPORT["debug_telegram"] = {"statuses": sorted({r[1].status_code for r in res if r[1] is not None}),
                                    "texts": sorted({c[2]["text"][:60] for c in self.w.telegram.methods("sendMessage")}),
                                    "events": sorted({e.get("kind") for e in await self.store.find("usage_events", {})})}
        self.assertEqual(sends, 100)
        REPORT["telegram_200_deliveries_100_unique"] = summary(
            [r[0] for r in res], sum(1 for r in res if r[2] or r[1].status_code not in (200, 429)),
            replies_sent=sends, http_429=sum(1 for r in res if r[1] is not None and r[1].status_code == 429))

    async def test_discord_gateway_event_burst(self):
        from api.discord import channel_for
        await self.w.call("PUT", f"/instances/{self.w.iA_dc.id}/channel/discord/settings", "admin",
                          {"listen_mode": "all_channels"})
        app = await self.store.get("discord_applications", self.w.eA.id)
        ch = channel_for(self.w.S)

        def ev(i):
            return {"t": "MESSAGE_CREATE", "d": {"id": str(800000000000000000 + i), "channel_id": "222222222222222221",
                                                 "guild_id": "111111111111111111", "content": f"q{i}",
                                                 "author": {"id": str(500000000000000100 + i)},
                                                 "member": {"roles": []}, "mentions": []}}
        events = [ev(i) for i in range(100)] + [ev(i) for i in range(0, 100, 2)]   # replays of half
        res = await asyncio.gather(*[timed(ch.handle_event(app, e)) for e in events])
        posted = len(self.w.discord.messages)
        self.assertEqual(posted, 100)
        REPORT["discord_150_events_100_unique"] = summary([r[0] for r in res], sum(1 for r in res if r[2]),
                                                          replies_posted=posted)

    async def test_concurrent_tenant_operations(self):
        async def make(i):
            who, org = ("owner", self.w.oA.id) if i % 2 == 0 else ("owner_b", self.w.oB.id)
            r = await self.w.call("POST", f"/organisations/{org}/bots", who, {"name": f"Bot {i}"})
            if r.status_code != 201:
                raise RuntimeError(str(r.status_code))
            r2 = await self.w.call("POST", f"/bots/{r.json()['id']}/instances", who,
                                   {"name": f"Web {i}", "channel": {"type": "webchat"}})
            if r2.status_code != 201:
                raise RuntimeError(str(r2.status_code))
            return r2
        res = await asyncio.gather(*[timed(make(i)) for i in range(60)])
        bots_a = await self.db.bots.count_documents({"enterprise_id": self.w.eA.id})
        bots_b = await self.db.bots.count_documents({"enterprise_id": self.w.eB.id})
        cross = await self.db.bot_instances.count_documents({"$expr": {"$ne": ["$enterprise_id", "$enterprise_id"]}})
        REPORT["tenant_operations_60_bot_plus_instance"] = summary([r[0] for r in res], sum(1 for r in res if r[2]),
                                                                   bots_a=bots_a, bots_b=bots_b, cross_tenant=cross)
        self.assertEqual(sum(1 for r in res if r[2]), 0)

    async def test_knowledge_revocation_during_retrieval_and_generation(self):
        import httpx
        src = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/knowledge/sources", "admin",
                                 {"name": "P"})).json()["id"]
        h = {**self.w.headers("admin"), "Content-Type": "text/plain", "X-Requested-With": "BitBotPro"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            await c.put(f"/api/knowledge/sources/{src}/documents/r.txt",
                        content=b"Customers may request a refund within 30 days of delivery.", headers=h)
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.allowed_sources": [src],
                                       "knowledge.minimum_mode": "strict_knowledge_only"}})
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"knowledge.approved_sources": [src], "knowledge.mode": "strict_knowledge_only",
                                       "knowledge.insufficient_fallback": "FALLBACK"}})
        from tests.unit.test_knowledge import GroundedProvider
        prov = GroundedProvider()
        self.w.S.providers = {"anthropic": prov}

        async def revoke_later():
            await asyncio.sleep(0.05)
            await self.w.call("PATCH", f"/knowledge/sources/{src}", "admin", {"status": "revoked"})
            return "revoked"
        tasks = [timed(self.chat(500 + i, {"message": "How long do I have to request a refund?"})) for i in range(60)]
        res = await asyncio.gather(revoke_later(), *tasks)
        REPORT["debug_knowledge"] = sorted({(r[1].status_code, r[1].text[:80]) for r in res[1:] if r[1] is not None})
        replies = [r[1].json().get("reply") for r in res[1:] if r[1] is not None and r[1].status_code == 200]
        # after the revocation completed, nothing may answer from it
        after = [(await self.chat(900 + i, {"message": "How long do I have to request a refund?"})).json()["reply"]
                 for i in range(10)]
        self.assertTrue(all(a == "FALLBACK" for a in after))
        REPORT["knowledge_60_queries_with_revocation"] = summary(
            [r[0] for r in res[1:]], sum(1 for r in res[1:] if r[2]),
            answered=sum(1 for x in replies if x != "FALLBACK"), refused=sum(1 for x in replies if x == "FALLBACK"),
            answered_after_revocation=sum(1 for a in after if a != "FALLBACK"))

    async def test_concurrent_oauth_callbacks(self):
        from urllib.parse import parse_qs, urlparse
        iid = (await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "root",
                                 {"name": "D", "channel": {"type": "discord"}})).json()["id"]
        r = await self.w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        self.w.discord.codes["loadcode123456"] = [{"id": "666666666666666666", "owner": True, "permissions": "0"}]
        self.w.discord.bot_guilds["910000000000000001"].add("666666666666666666")
        res = await asyncio.gather(*[timed(self.w.call(
            "GET", f"/hooks/discord/oauth/callback?state={state}&code=loadcode123456&guild_id=666666666666666666"))
            for _ in range(20)])
        statuses = [r[1].json()["status"] for r in res if r[1] is not None]
        self.assertEqual(statuses.count("connected"), 1)
        REPORT["oauth_callbacks_20_same_state"] = summary([r[0] for r in res], sum(1 for r in res if r[2]),
                                                          connected=statuses.count("connected"),
                                                          rejected=statuses.count("invalid_state"))

    async def test_credential_rotation_and_deletion_races(self):
        # Telegram token rotation (same bot) racing a message burst; then an Organisation deletion racing chats
        cred = self.w.cA_tg.id
        from tests.unit._api_fixtures import TELEGRAM_TOKEN_A
        rotated = TELEGRAM_TOKEN_A.split(":")[0] + ":" + "Z" * 35
        res = await asyncio.gather(
            *[timed(self.w.call("PATCH", f"/credentials/{cred}", "admin", {"secret": rotated if i % 2 else
                                                                           TELEGRAM_TOKEN_A})) for i in range(10)],
            *[timed(self.chat(1200 + i, {"message": f"r{i}"})) for i in range(40)])
        rot = [r[1].status_code for r in res[:10] if r[1] is not None]
        doc = await self.db.credentials.find_one({"_id": cred})
        self.assertNotIn(TELEGRAM_TOKEN_A, str(doc))
        pv = (await self.w.call("POST", f"/organisations/{self.w.oA.id}/deletion-preview", "owner")).json()
        res2 = await asyncio.gather(
            timed(self.w.call("POST", f"/organisations/{self.w.oA.id}/delete", "owner",
                              {"preview_token": pv["preview_token"], "confirm_name": pv["confirm_name"]})),
            *[timed(self.chat(1300 + i, {"message": f"d{i}"})) for i in range(40)])
        from core import deletion
        job = res2[0][1].json()["id"] if res2[0][1] is not None and res2[0][1].status_code == 202 else None
        if job:
            from runtime.telegram import telegram_cleanup_executors
            from runtime.discord import discord_cleanup_executors
            await deletion.run_job(self.store, job, clock=self.w.S.clock,
                                   executors={**telegram_cleanup_executors(self.w.S.box,
                                                                           transport=self.w.S.telegram_transport),
                                              **discord_cleanup_executors(self.w.S.box,
                                                                          transport=self.w.S.discord_transport)})
        before_repurge = sum([await self.db[c].count_documents({"organisation_id": self.w.oA.id})
                              for c in ("bots", "bot_instances", "conversations", "chat_requests", "usage_ledger")])
        from datetime import datetime, timedelta
        later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(seconds=deletion.REPURGE_DELAY_SECONDS + 1))
        await deletion.repurge_due(self.store, now=later.isoformat())
        REPORT["org_deletion_orphans_before_grace_repurge"] = before_repurge
        leftovers = sum([await self.db[c].count_documents({"organisation_id": self.w.oA.id})
                         for c in ("bots", "bot_instances", "conversations", "chat_requests", "usage_ledger")])
        REPORT["rotation_10_patches_with_40_chats"] = summary([r[0] for r in res], sum(1 for r in res if r[2]),
                                                              rotation_statuses=sorted(set(rot)))
        REPORT["org_deletion_racing_40_chats"] = summary([r[0] for r in res2], sum(1 for r in res2 if r[2]),
                                                         leftover_org_records=leftovers)
        self.assertEqual(leftovers, 0)


if __name__ == "__main__":
    unittest.main()
