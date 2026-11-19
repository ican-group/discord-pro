"""Spec 012 Phase 3 against a REAL MongoDB (standalone mongod, the production
topology: no replica set, no multi-document transactions).

Runs only when MONGO_TEST_URL points at a disposable server and the database
name starts with ``bbp_itest``; refuses anything that looks like production.
Each test uses its own database, dropped afterwards.
"""
import asyncio
import os
import unittest
import uuid

from tests.unit import _support  # noqa: F401  (sys.path)
from tests.unit._api_fixtures import HAVE_API

MONGO_TEST_URL = os.environ.get("MONGO_TEST_URL", "")
DB_PREFIX = os.environ.get("MONGO_TEST_DB", "bbp_itest")
SAFE = bool(MONGO_TEST_URL) and "bitbotpro-mongo" not in MONGO_TEST_URL and DB_PREFIX.startswith("bbp_itest")
N = 30
GUILD = "666666666666666666"


@unittest.skipUnless(SAFE and HAVE_API, "MONGO_TEST_URL not set (or not a disposable test database)")
class MongoBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        from persistence.mongo import MongoStore, prepare_database
        asyncio.get_running_loop().slow_callback_duration = 10
        self.client = AsyncIOMotorClient(MONGO_TEST_URL, serverSelectionTimeoutMS=10000)
        self.db_name = f"{DB_PREFIX}_{uuid.uuid4().hex[:10]}"
        self.db = self.client[self.db_name]
        await prepare_database(self.db)
        self.store = MongoStore(self.db)

    async def asyncTearDown(self):
        await self.client.drop_database(self.db_name)
        self.client.close()

    async def world(self):
        from tests.unit._api_fixtures import World
        return await World(self.store).build()


class DatabaseGuardTests(MongoBase):
    async def test_prepare_is_idempotent_and_marks_the_database(self):
        from persistence.mongo import prepare_database
        await prepare_database(self.db)
        marker = await self.db.platform_state.find_one({"_id": "schema"})
        self.assertEqual(marker["schema"], "spec012")
        indexes = await self.db.enterprises.index_information()
        self.assertTrue(indexes["uniq_slug"]["unique"])
        self.assertIn("holders.instance_id_1", await self.db.channel_bindings.index_information())
        idx = await self.db.memberships.index_information()
        self.assertTrue(idx["uniq_enterprise_user"]["unique"])

    async def test_refuses_legacy_or_foreign_databases(self):
        from persistence.mongo import DatabaseGuardError, prepare_database
        for setup in ({"workspaces": {"name": "legacy"}}, {"users": {"email": "x@example.test"}}):
            name = f"{DB_PREFIX}_{uuid.uuid4().hex[:10]}"
            db = self.client[name]
            try:
                for col, doc in setup.items():
                    await db[col].insert_one(doc)
                with self.assertRaises(DatabaseGuardError):
                    await prepare_database(db)
                self.assertNotIn("enterprises", await db.list_collection_names())    # nothing created
            finally:
                await self.client.drop_database(name)

    async def test_schema_validators_reject_malformed_documents(self):
        from pymongo.errors import WriteError
        bad = [("enterprises", {"_id": "x", "id": "x"}),
               ("organisations", {"_id": str(uuid.uuid4()), "id": str(uuid.uuid4()), "name": "o", "level": "organisation",
                                  "lifecycle": {"state": "frozen"}, "overrides": {}, "version": 1,
                                  "created_at": "t", "enterprise_id": "not-a-uuid", "policy": {}}),
               ("memberships", {"_id": "e", "id": f"{uuid.uuid4()}:{uuid.uuid4()}", "enterprise_id": str(uuid.uuid4()),
                                "user_id": str(uuid.uuid4()), "version": 1, "role": "owner",
                                "grants": [{"scope_level": "workspace", "scope_id": str(uuid.uuid4()),
                                            "role": "admin"}]}),
               ("memberships", {"_id": "f", "id": f"{uuid.uuid4()}:{uuid.uuid4()}", "enterprise_id": str(uuid.uuid4()),
                                "user_id": str(uuid.uuid4()), "version": 1, "role": None,
                                "grants": [{"scope_level": "organisation", "scope_id": str(uuid.uuid4()),
                                            "role": "owner"}]}),          # owner only at Enterprise level
               ("credentials", {"_id": "c", "id": str(uuid.uuid4()), "enterprise_id": str(uuid.uuid4()),
                                "provider": "emergent", "name": "x", "fingerprint": "f", "status": "active",
                                "secret_enc": "x", "version": 1}),
               ("users", {"_id": "u", "id": str(uuid.uuid4()), "email": "a@b", "is_platform_superadmin": "yes",
                          "token_version": 0, "status": "active", "version": 1})]
        for col, doc in bad:
            with self.subTest(col):
                with self.assertRaises(WriteError):
                    await self.db[col].insert_one(doc)

    async def test_filters_reject_operators(self):
        from core.hierarchy import HierarchyError
        for flt in ({"id": {"$ne": None}}, {"$where": "1"}, {"a.$b": 1}, {"x": [{"$gt": 1}]}):
            with self.assertRaises(HierarchyError):
                await self.store.find("enterprises", flt)
        with self.assertRaises(HierarchyError):
            await self.store.delete_many("enterprises", {})                    # never unscoped


class MongoConcurrencyTests(MongoBase):
    async def test_concurrent_guild_binding_claims(self):
        from core.hierarchy import HierarchyService
        svc = HierarchyService(self.store)
        insts = []
        for i in range(N):
            e = await svc.create_enterprise(name="E", slug=f"e{i}-{uuid.uuid4().hex[:6]}", actor_id="u")
            o = await svc.create_organisation(enterprise_id=e.id, name="O", actor_id="u")
            b = await svc.create_bot(organisation_id=o.id, name="B", actor_id="u")
            ch = {"type": "discord"} if i % 2 else {"type": "discord", "listen_mode": "specific_channels",
                                                    "channel_ids": ["777777777777777771"]}
            insts.append(await svc.create_instance(bot_id=b.id, name="I", channel=ch, actor_id="u"))
        results = await asyncio.gather(*[svc.bind_discord_guild(i.id, GUILD, application_id="910000000000000001", actor_id="u") for i in insts],
                                       return_exceptions=True)
        ok = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(ok), 1)
        holders = await svc.bindings.holders(f"discord:910000000000000001:{GUILD}")
        self.assertEqual(list(holders), [ok[0].id])

    async def test_concurrent_telegram_bot_identity(self):
        from core.hierarchy import HierarchyService
        svc = HierarchyService(self.store)
        e = await svc.create_enterprise(name="E", slug=f"tg-{uuid.uuid4().hex[:6]}", actor_id="u")
        o = await svc.create_organisation(enterprise_id=e.id, name="O", actor_id="u")
        b = await svc.create_bot(organisation_id=o.id, name="B", actor_id="u")
        results = await asyncio.gather(*[
            svc.create_instance(bot_id=b.id, name="t", actor_id="u", telegram_bot_id="4242",
                                channel={"type": "telegram", "credential_id": str(uuid.uuid4())}) for _ in range(N)],
            return_exceptions=True)
        self.assertEqual(sum(1 for r in results if not isinstance(r, BaseException)), 1)
        self.assertEqual(await self.store.count("bot_instances", {"bot_id": b.id}), 1)   # failures left nothing

    async def test_last_owner_races(self):
        from core import memberships as ms
        from core.authz import Actor, Role
        from core.hierarchy import HierarchyService, Level
        svc = HierarchyService(self.store)
        e = await svc.create_enterprise(name="E", slug=f"own-{uuid.uuid4().hex[:6]}", actor_id="u")
        chain = await svc.load_chain(Level.ENTERPRISE, e.id)
        root = Actor(str(uuid.uuid4()), True)
        owners = [str(uuid.uuid4()), str(uuid.uuid4())]
        for u in owners:
            await ms.mutate(self.store, e.id, ms.op_grant(root, chain, user_id=u, role=Role.OWNER, now="t",
                                                          bypass_authority=True))
        for _ in range(10):
            a, b = owners
            results = await asyncio.gather(
                ms.mutate(self.store, e.id, ms.op_revoke(Actor(a), chain, user_id=b)),
                ms.mutate(self.store, e.id, ms.op_revoke(Actor(b), chain, user_id=a)),
                return_exceptions=True)
            self.assertEqual(sum(1 for r in results if not isinstance(r, BaseException)), 1)
            remaining = (await ms.load(self.store, e.id)).owners()
            self.assertEqual(len(remaining), 1)
            new = str(uuid.uuid4())
            await ms.mutate(self.store, e.id, ms.op_grant(root, chain, user_id=new, role=Role.OWNER, now="t",
                                                          bypass_authority=True))
            owners = remaining + [new]
        dup = str(uuid.uuid4())
        await asyncio.gather(*[ms.mutate(self.store, e.id, ms.op_grant(root, chain, user_id=dup, role=Role.VIEWER,
                                                                        now="t", bypass_authority=True))
                               for _ in range(N)])
        self.assertEqual(len((await ms.load(self.store, e.id)).for_user(dup)), 1)

    async def test_superadmin_races(self):
        from core.authz import Actor
        from core.identity import USERS, create_user, ensure_superadmin_guard, get_user, revoke_superadmin
        ids = []
        for i in range(6):
            u = await create_user(self.store, email=f"s{i}-{uuid.uuid4().hex[:4]}@example.test", name="s", now="t")
            await self.store.update(USERS, u.id, {"is_platform_superadmin": True})
            ids.append(u.id)
        await self.db.platform_state.delete_one({"_id": "superadmins"})
        self.assertEqual(await ensure_superadmin_guard(self.store), 6)
        root = Actor(ids[0], True)
        await asyncio.gather(*[revoke_superadmin(self.store, root, u) for u in ids], return_exceptions=True)
        remaining = [u for u in ids if (await get_user(self.store, u)).is_platform_superadmin]
        self.assertEqual(len(remaining), 1)
        guard = await self.db.platform_state.find_one({"_id": "superadmins"})
        self.assertLessEqual(guard["count"], 1)

    async def test_single_use_tokens_and_lifecycle_cas(self):
        from core import deletion as dl
        token, _ = await dl.issue_token(self.store, actor_id="a", level="bot", target_id="t", digest="d",
                                        now="2026-11-18T00:00:00+00:00")
        results = await asyncio.gather(*[dl.consume_token(self.store, token, actor_id="a", level="bot", target_id="t",
                                                          now="2026-11-18T00:00:01+00:00") for _ in range(N)],
                                       return_exceptions=True)
        self.assertEqual(sum(1 for r in results if isinstance(r, dict)), 1)
        self.assertIn("ttl_expires", await self.db.deletion_previews.index_information())


class MongoMembershipProtocolTests(MongoBase):
    """X4 on real MongoDB: per-user documents, exact owner guard, revocation races."""

    async def test_revocation_race_is_linearizable_on_mongo(self):
        from core import memberships as ms
        from core.authz import Actor, Role
        from core.hierarchy import HierarchyService, Level
        svc = HierarchyService(self.store)
        e = await svc.create_enterprise(name="E", slug=f"rv-{uuid.uuid4().hex[:6]}", actor_id="u")
        chain = await svc.load_chain(Level.ENTERPRISE, e.id)
        root, owner = Actor(str(uuid.uuid4()), True), str(uuid.uuid4())

        async def g(u, role):
            return await ms.mutate(self.store, e.id, ms.op_grant(root, chain, user_id=u, role=role, now="t",
                                                                 bypass_authority=True))
        await g(owner, Role.OWNER)
        outcomes = set()
        for i in range(12):
            admin, target = str(uuid.uuid4()), str(uuid.uuid4())
            await g(admin, Role.ADMIN)
            await g(target, Role.VIEWER)
            grant = ms.mutate(self.store, e.id, ms.op_grant(Actor(admin), chain, user_id=target, role=Role.ADMIN,
                                                            now="t"))
            demote = ms.mutate(self.store, e.id, ms.op_grant(Actor(owner), chain, user_id=admin, role=Role.VIEWER,
                                                             now="t"))
            if i % 2:
                d, gr = await asyncio.gather(demote, grant, return_exceptions=True)
            else:
                gr, d = await asyncio.gather(grant, demote, return_exceptions=True)
            self.assertNotIsInstance(d, BaseException)
            if isinstance(gr, BaseException):
                outcomes.add("refused")
                doc = await self.db.memberships.find_one({"_id": f"{e.id}:{target}"})
                self.assertEqual(doc["role"], "viewer")
            else:
                outcomes.add("granted")
                self.assertLess(gr.seq, d.seq)
        guard = await self.db.membership_guards.find_one({"_id": e.id})
        self.assertEqual(guard["owner_count"], await self.db.memberships.count_documents(
            {"enterprise_id": e.id, "role": "owner"}))
        self.assertIsNone(guard["pending"])
        self.assertEqual(outcomes, {"granted", "refused"})

    async def test_unique_document_per_enterprise_user(self):
        from pymongo.errors import DuplicateKeyError
        e, u = str(uuid.uuid4()), str(uuid.uuid4())
        base = {"enterprise_id": e, "user_id": u, "role": "viewer", "grants": [], "version": 1}
        await self.db.memberships.insert_one({"_id": f"{e}:{u}", "id": f"{e}:{u}", **base})
        with self.assertRaises(DuplicateKeyError):                     # same pair under another id
            await self.db.memberships.insert_one({"_id": "other", "id": f"{e}:{u}", **base})


class MongoPhase4ATests(MongoBase):
    """Phase 4A on real MongoDB: sessions, throttling, quotas, credential scope, worker recovery."""

    async def test_login_logout_and_session_revocation_on_mongo(self):
        from api import passwords
        from core.identity import USERS
        w = await self.world()
        w.S.settings.cookie_secure = False
        await self.store.update(USERS, w.users["viewer"].id,
                                {"password_hash": passwords.hash_password("Correct-Horse-Battery-91")})
        csrf = {"X-Requested-With": "BitBotPro"}
        r = await w.call("POST", "/auth/login", None, {"email": "viewer@example.test",
                                                       "password": "Correct-Horse-Battery-91"}, headers=csrf)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        h = {"Authorization": "Bearer " + token, **csrf}
        self.assertEqual((await w.call("GET", "/auth/session", headers=h)).status_code, 200)
        self.assertEqual((await w.call("POST", "/auth/logout", headers=h)).status_code, 200)
        self.assertEqual((await w.call("GET", "/auth/session", headers=h)).status_code, 401)
        self.assertIn("ttl_expires", await self.db.sessions.index_information())
        doc = await self.db.users.find_one({"_id": w.users["viewer"].id})
        self.assertTrue(doc["password_hash"].startswith("$2b$"))

    async def test_concurrent_login_failures_respect_the_limit_on_mongo(self):
        from api.auth import LOGIN_ACCOUNT
        w = await self.world()
        csrf = {"X-Requested-With": "BitBotPro"}
        results = await asyncio.gather(*[w.call("POST", "/auth/login", None,
                                                {"email": "viewer@example.test", "password": "Wrong-Password-1234"},
                                                headers=csrf) for _ in range(12)])
        self.assertTrue(all(r.status_code in (401, 429) for r in results))
        doc = await self.db.auth_throttle.find_one({"_id": {"$regex": "^login-acct:"}})
        self.assertLessEqual(doc["count"], LOGIN_ACCOUNT[0])

    async def test_quota_and_credential_scope_through_the_pipeline_on_mongo(self):
        from core.hierarchy import ChannelType, Level
        from runtime.interfaces import ChannelAddress, GenerationResult, InboundMessage
        from runtime.pipeline import Pipeline
        w = await self.world()

        class Provider:
            name = "anthropic"
            calls = 0

            async def generate(self, request):
                Provider.calls += 1
                return GenerationResult(text="ok")
        r = await w.call("POST", f"/enterprises/{w.eA.id}/credentials", "owner",
                         {"provider": "anthropic", "name": "scoped", "secret": "not-a-real-key-1234567890",
                          "usable_by": [{"level": "bot", "id": w.bA.id}]})
        self.assertEqual(r.status_code, 201, r.text)
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": r.json()["id"]}})
        await w.call("PATCH", f"/enterprises/{w.eA.id}/policy", "owner",
                     {"changes": {"limits.max_monthly_ai_messages": 4}})
        p = Pipeline(self.store, box=w.S.box, clock=w.S.clock, providers={"anthropic": Provider()})
        msg = lambda i: InboundMessage(ChannelAddress(ChannelType.WEBCHAT, public_id=w.iA_web.channel.public_id),
                                       "hi", f"user-{i}")
        outs = await asyncio.gather(*[p.handle(msg(i)) for i in range(12)])
        self.assertEqual(sum(1 for o in outs if o.kind == "answer"), 4)
        self.assertEqual(Provider.calls, 4)
        counter = await self.db.usage_counters.find_one({"enterprise_id": w.eA.id, "organisation_id": None})
        self.assertEqual(counter["count"], 4)

    async def test_worker_recovers_a_crashed_job_on_mongo(self):
        from core import deletion as dl
        from runtime.worker import MockCleanupAdapter, Worker
        w = await self.world()
        B = f"/bots/{w.bA.id}"
        pv = (await w.call("POST", f"{B}/deletion-preview", "owner")).json()
        r = await w.call("POST", f"{B}/delete", "owner", {"preview_token": pv["preview_token"],
                                                          "confirm_name": w.bA.name})
        jid = r.json()["id"]
        job = await self.db.deletion_jobs.find_one({"_id": jid})
        self.assertEqual(job["status"], "awaiting_external_cleanup")
        # simulate a worker that died holding the lease
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await self.db.deletion_jobs.update_one({"_id": jid}, {"$set": {"status": "running", "lease_until": past}})
        adapter = MockCleanupAdapter()
        await Worker(self.store, clock=w.S.clock, worker_id="recovery", executors=adapter.executors()).run_once()
        job = await self.db.deletion_jobs.find_one({"_id": jid})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(adapter.effects), len(set(adapter.effects)))
        self.assertEqual(await self.db.bot_instances.count_documents({"enterprise_id": w.eB.id}), 1)


class MongoWebchatTests(MongoBase):
    """Phase 4B on real MongoDB."""

    async def webchat_world(self):
        from runtime.interfaces import GenerationResult
        w = await self.world()

        class Provider:
            name = "anthropic"
            calls = 0

            async def generate(self, request):
                Provider.calls += 1
                await asyncio.sleep(0.01)
                return GenerationResult(text="mongo reply")
        w.S.providers = {"anthropic": Provider()}
        w.provider_cls = Provider
        r = await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        assert r.status_code == 200, r.text
        return w

    async def pub(self, w, method, path, body=None, headers=None):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
            return await c.request(method, "/api" + path, json=body,
                                   headers={"Origin": "https://shop.example", **(headers or {})})

    async def test_chat_history_logo_and_deletion_on_mongo(self):
        import httpx
        w = await self.webchat_world()
        P = f"/public/instances/{w.iA_web.channel.public_id}"
        r = await self.pub(w, "POST", f"{P}/chat", {"message": "hello"})
        self.assertEqual(r.status_code, 200, r.text)
        sid, tok = r.json()["session_id"], r.json()["session_token"]
        h = await self.pub(w, "GET", f"{P}/sessions/{sid}/history", headers={"X-Chat-Session-Token": tok})
        self.assertEqual([m["role"] for m in h.json()["messages"]], ["user", "assistant"])
        doc = await self.db.conversations.find_one({"_id": sid})
        self.assertEqual(doc["instance_id"], w.iA_web.id)
        self.assertIn("ttl_expires", await self.db.conversations.index_information())
        self.assertIn("ttl_expires", await self.db.chat_sessions.index_information())
        sess = await self.db.chat_sessions.find_one({"_id": sid})
        self.assertNotIn(tok, str(sess))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
            up = await c.put(f"/api/bots/{w.bA.id}/logo", content=png, headers=w.headers("owner"))
        self.assertEqual(up.status_code, 200, up.text)
        logo = await self.pub(w, "GET", f"{P}/logo")
        self.assertEqual(logo.content, png)
        pv = (await w.call("POST", f"/instances/{w.iA_web.id}/deletion-preview", "owner")).json()
        await w.call("POST", f"/instances/{w.iA_web.id}/delete", "owner",
                     {"preview_token": pv["preview_token"], "confirm_name": w.iA_web.name})
        for col in ("conversations", "chat_sessions", "chat_requests"):
            self.assertEqual(await self.db[col].count_documents({"instance_id": w.iA_web.id}), 0, col)
        self.assertEqual((await self.pub(w, "GET", P)).status_code, 404)

    async def test_quota_and_idempotency_under_concurrency_on_mongo(self):
        w = await self.webchat_world()
        await w.call("PATCH", f"/enterprises/{w.eA.id}/policy", "owner",
                     {"changes": {"limits.max_monthly_ai_messages": 3}})
        P = f"/public/instances/{w.iA_web.channel.public_id}"
        sessions = []
        for _ in range(6):
            r = await self.pub(w, "POST", f"{P}/sessions")
            sessions.append((r.json()["session_id"], r.json()["session_token"]))
        results = await asyncio.gather(*[self.pub(w, "POST", f"{P}/chat", {"message": "hi", "session_id": s},
                                                  {"X-Chat-Session-Token": t}) for s, t in sessions])
        self.assertEqual(sorted(r.status_code for r in results), [200] * 3 + [429] * 3)
        self.assertEqual(w.provider_cls.calls, 3)
        sid, tok = sessions[0]
        cmid = "0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
        dup = await asyncio.gather(*[self.pub(w, "POST", f"{P}/chat", {"message": "again", "session_id": sid,
                                                                        "client_message_id": cmid},
                                              {"X-Chat-Session-Token": tok}) for _ in range(3)])
        self.assertTrue(all(r.status_code in (200, 409, 429) for r in dup))
        self.assertLessEqual(w.provider_cls.calls, 3)                         # quota exhausted: nothing billed


class MongoTelegramAndBillingTests(MongoBase):
    """Phase 4C on real MongoDB: billing idempotency and Telegram webhook flow."""

    async def test_billing_failure_injection_on_mongo(self):
        from tests.unit.test_api_webchat import FakeProvider
        from runtime import conversations as conv
        from runtime.usage import StoreUsageTracker
        import httpx
        w = await self.world()
        provider = FakeProvider()
        w.S.providers = {"anthropic": provider}
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        P = f"/public/instances/{w.iA_web.channel.public_id}"

        async def pub(body, headers=None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
                return await c.post("/api" + P + "/chat", json=body,
                                    headers={"Origin": "https://shop.example", **(headers or {})})
        s = (await pub({"message": "first"})).json()
        cmid = "8f7e6d5c-4b3a-4291-8a7b-6c5d4e3f2a1b"
        original = StoreUsageTracker.settle

        async def broken(self, *a, **kw):
            raise RuntimeError("down")
        StoreUsageTracker.settle = broken
        try:
            r = await pub({"message": "q", "session_id": s["session_id"], "client_message_id": cmid},
                          {"X-Chat-Session-Token": s["session_token"]})
        finally:
            StoreUsageTracker.settle = original
        self.assertEqual(r.status_code, 503)
        results = await asyncio.gather(*[pub({"message": "q", "session_id": s["session_id"], "client_message_id": cmid},
                                             {"X-Chat-Session-Token": s["session_token"]}) for _ in range(4)])
        self.assertTrue(all(x.status_code in (200, 409) for x in results))
        self.assertEqual(len(provider.calls), 2)                      # "first" + "q", never regenerated
        ledger = await self.db.usage_ledger.find_one({"_id": f"{s['session_id']}:{cmid}"})
        self.assertEqual(ledger["state"], "settled")
        self.assertEqual(await self.db.usage_events.count_documents({"kind": "llm_call"}), 2)
        doc = await self.db.conversations.find_one({"_id": s["session_id"]})
        self.assertEqual(len(doc["messages"]), 4)

    async def test_telegram_webhook_flow_on_mongo(self):
        import httpx
        from tests.unit.test_api_webchat import FakeProvider
        w = await self.world()
        provider = FakeProvider(text="tg reply")
        w.S.providers = {"anthropic": provider}
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        r = await w.call("POST", f"/instances/{w.iA_tg.id}/channel/connect", "admin")
        self.assertEqual(r.status_code, 200, r.text)
        url, header = [(c[2]["url"], c[2]["secret_token"]) for c in w.telegram.methods("setWebhook")][-1]
        path = "/api/hooks" + url.split("/api/hooks", 1)[1]
        upd = {"update_id": 7, "message": {"message_id": 1, "text": "hi", "chat": {"id": 5, "type": "private"},
                                           "from": {"id": 6, "is_bot": False}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
            for _ in range(3):
                resp = await c.post(path, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": header})
                self.assertEqual(resp.status_code, 200)
            bad = await c.post(path, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "nope" * 10})
            self.assertEqual(bad.status_code, 404)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(w.telegram.methods("sendMessage")), 1)
        inst = await self.db.bot_instances.find_one({"_id": w.iA_tg.id})
        self.assertNotIn(header, str(inst))
        self.assertEqual(await self.db.conversations.count_documents({"instance_id": w.iA_tg.id,
                                                                      "channel": "telegram"}), 1)


class MongoPhase4DTests(MongoBase):
    """Phase 4D on real MongoDB: durable delivery, Enterprise Discord
    applications, OAuth state, shared-server claims and gateway leases."""

    async def test_telegram_delivery_record_and_concurrent_senders_on_mongo(self):
        import httpx
        from tests.unit.test_api_webchat import FakeProvider
        from runtime import delivery as dlv
        from api.telegram import channel_for
        w = await self.world()
        w.S.providers = {"anthropic": FakeProvider(text="tg reply")}
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        await w.call("POST", f"/instances/{w.iA_tg.id}/channel/connect", "admin")
        url, header = [(c[2]["url"], c[2]["secret_token"]) for c in w.telegram.methods("setWebhook")][-1]
        path = "/api/hooks" + url.split("/api/hooks", 1)[1]
        original = type(channel_for(w.S)).deliver

        async def crash(self, did):
            return None                                       # process dies before sending
        type(channel_for(w.S)).deliver = crash
        try:
            upd = {"update_id": 9, "message": {"message_id": 3, "text": "hi", "chat": {"id": 5, "type": "private"},
                                               "from": {"id": 6, "is_bot": False}}}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
                await c.post(path, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": header})
        finally:
            type(channel_for(w.S)).deliver = original
        [d] = await self.store.find(dlv.COLLECTION, {"instance_id": w.iA_tg.id})
        self.assertEqual(d["status"], "pending")
        ch = channel_for(w.S)
        await asyncio.gather(*[dlv.run(self.store, d["id"], ch.send_part, now=w.S.clock(), worker=f"w{i}")
                               for i in range(N)])
        self.assertEqual(len(w.telegram.methods("sendMessage")), 1)          # one send across 30 workers
        doc = await self.db.outbound_deliveries.find_one({"_id": d["id"]})
        self.assertEqual(doc["status"], "delivered")
        self.assertTrue(doc["parts"][0]["message_id"])

    async def test_discord_application_uniqueness_and_secrets_on_mongo(self):
        from tests.unit._api_fixtures import APP_A, DISCORD_TOKEN_A, DISCORD_TOKEN_A2
        w = await self.world()
        r = await w.call("POST", "/enterprises", "root", {"name": "C", "slug": "ent-c"})
        eid = r.json()["id"]
        results = await asyncio.gather(*[w.call("PUT", f"/platform/enterprises/{eid}/discord-application", "root",
                                                {"application_id": APP_A, "bot_token": DISCORD_TOKEN_A2,
                                                 "client_secret": "client-secret-c-123"}) for _ in range(5)])
        self.assertTrue(all(x.status_code == 409 for x in results))         # unique index across Enterprises
        self.assertEqual(await self.db.discord_applications.count_documents({"application_id": APP_A}), 1)
        raw = str(await self.db.discord_applications.find_one({"_id": w.eA.id}))
        self.assertNotIn(DISCORD_TOKEN_A, raw)
        with self.assertRaises(Exception):                                    # validator: no plaintext-only doc
            await self.db.discord_applications.insert_one({"_id": eid, "id": eid, "enterprise_id": eid,
                                                           "application_id": "999999999999999999"})

    async def test_oauth_install_and_shared_server_claims_on_mongo(self):
        from urllib.parse import parse_qs, urlparse
        from tests.unit._api_fixtures import APP_A, CH_A1, CH_A2, GUILD_A
        w = await self.world()
        await w.call("PUT", f"/instances/{w.iA_dc.id}/channel/discord/settings", "admin",
                     {"listen_mode": "specific_channels", "channel_ids": [CH_A1]})
        ids = []
        for i in range(3):
            r = await w.call("POST", f"/bots/{w.bA.id}/instances", "root",
                             {"name": f"D{i}", "channel": {"type": "discord"}})
            ids.append(r.json()["id"])
        for i, iid in enumerate(ids):
            r = await w.call("POST", f"/instances/{iid}/channel/discord/install-url", "admin")
            state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
            code = f"mongocode{i}000"
            w.discord.codes[code] = [{"id": GUILD_A, "owner": True, "permissions": "0"}]
            r = await w.call("GET", f"/hooks/discord/oauth/callback?state={state}&code={code}&guild_id={GUILD_A}")
            self.assertEqual(r.json()["status"], "channel_selection_required")
            replay = await w.call("GET", f"/hooks/discord/oauth/callback?state={state}&code={code}&guild_id={GUILD_A}")
            self.assertEqual(replay.status_code, 400)                          # single use on Mongo
        results = await asyncio.gather(*[w.call("PUT", f"/instances/{iid}/channel/discord/settings", "admin",
                                                {"channel_ids": [CH_A2]}) for iid in ids])
        self.assertEqual(sorted(r.status_code for r in results), [200, 409, 409])
        doc = await self.db.channel_bindings.find_one({"_id": f"discord:{APP_A}:{GUILD_A}"})
        scopes = [s for h in doc["holders"] for s in h["scopes"]]
        self.assertEqual(sorted(scopes), sorted([CH_A1, CH_A2]))              # never overlapping
        ttl = [ix for ix in await self.db.oauth_states.list_indexes().to_list(None) if "expireAfterSeconds" in ix]
        self.assertEqual(len(ttl), 1)

    async def test_gateway_lease_on_mongo(self):
        from runtime.discord_gateway import GatewayManager, MockGatewayConnection
        from api.discord import channel_for
        w = await self.world()
        await w.call("POST", f"/platform/enterprises/{w.eA.id}/discord-application/gateway/start", "root")
        managers = [GatewayManager(self.store, box=w.S.box, channel=channel_for(w.S),
                                   factory=lambda app: MockGatewayConnection(app["application_id"]),
                                   clock=w.S.clock, worker_id=f"w{i}") for i in range(6)]
        outs = await asyncio.gather(*[m.reconcile() for m in managers])
        self.assertEqual(sum(o["started"] for o in outs), 1)                  # one owner, ever
        st = await self.db.discord_gateway_status.find_one({"_id": w.eA.id})
        self.assertEqual(st["state"], "connected")


class MongoPhase4ETests(MongoBase):
    """Phase 4D corrections and 4E Zendesk on real MongoDB: strict schema
    validators, unique ids under concurrency, TTL indexes."""

    async def zendesk_world(self):
        from tests.unit._api_fixtures import ZD_API_TOKEN
        from tests.unit.test_api_webchat import FakeProvider
        w = await self.world()
        w.S.providers = {"anthropic": FakeProvider(text="zendesk answer")}
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        r = await w.call("POST", f"/bots/{w.bA.id}/integrations", "admin",
                         {"type": "zendesk", "name": "Support", "settings": {"reply_mode": "auto_reply"}})
        self.assertEqual(r.status_code, 201, r.text)
        zid = r.json()["id"]
        r = await w.call("POST", f"/organisations/{w.oA.id}/credentials", "admin",
                         {"provider": "zendesk_api_token", "name": "Z", "secret": ZD_API_TOKEN,
                          "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})
        await w.call("PATCH", f"/integrations/{zid}", "admin", {"credential_id": r.json()["id"]})
        await w.call("PUT", f"/integrations/{zid}/zendesk/signing-secret", "admin",
                     {"secret": "mongo-signing-secret-0123456789"})
        return w, zid

    async def test_zendesk_webhook_flow_and_duplicates_on_mongo(self):
        import httpx
        import json as _json
        from datetime import datetime, timezone
        import webhook_auth
        w, zid = await self.zendesk_world()
        url = (await w.call("GET", f"/integrations/{zid}/zendesk/webhook", "admin")).json()["url"]
        path = "/api/" + url.split("/api/", 1)[1]
        body = _json.dumps({"ticket_id": 101, "event_id": "mongo-evt"}).encode()
        ts = datetime.now(timezone.utc).isoformat()
        sig = webhook_auth.zendesk_signature("mongo-signing-secret-0123456789", ts, body)
        headers = {webhook_auth.ZENDESK_TIMESTAMP_HEADER: ts, webhook_auth.ZENDESK_SIGNATURE_HEADER: sig}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
            results = await asyncio.gather(*[c.post(path, content=body, headers=headers) for _ in range(N)])
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(sum(1 for r in results if r.json().get("accepted")), 1)
        self.assertEqual(len(w.S.providers["anthropic"].calls), 1)
        self.assertEqual(len(w.zendesk.public_posts()), 1)
        ev = await self.db.zendesk_events.find_one({"integration_id": zid})
        self.assertEqual(ev["status"], "done")
        d = await self.db.outbound_deliveries.find_one({"integration_id": zid})
        self.assertEqual((d["status"], d["instance_id"]), ("delivered", None))
        self.assertIsInstance(d["expires_at_dt"], datetime)
        self.assertEqual(await self.db.suggestions.count_documents({"integration_id": zid}), 1)
        ledger = [x async for x in self.db.usage_ledger.find({})]
        self.assertEqual([x["state"] for x in ledger], ["settled"])

    async def test_validators_reject_ownerless_or_double_owned_records(self):
        from pymongo.errors import WriteError
        w, zid = await self.zendesk_world()
        base = {"request_key": "x", "channel": "zendesk", "enterprise_id": w.eA.id, "organisation_id": w.oA.id,
                "bot_id": w.bA.id, "target": {}, "parts": [{"index": 0}], "status": "pending", "version": 1}
        with self.assertRaises(WriteError):                                   # neither instance nor integration
            await self.db.outbound_deliveries.insert_one({"_id": "a", "id": "a", **base})
        with self.assertRaises(WriteError):                                   # both
            await self.db.outbound_deliveries.insert_one({"_id": "b", "id": "b", **base, "instance_id": w.iA_tg.id,
                                                          "integration_id": zid})
        with self.assertRaises(WriteError):                                   # invalid reply mode
            await self.db.bot_integrations.update_one({"_id": zid}, {"$set": {"settings.reply_mode": "post_everything"}})
        indexes = {ix["name"]: ix for ix in await self.db.outbound_deliveries.list_indexes().to_list(None)}
        self.assertEqual(indexes["ttl_expires"]["expireAfterSeconds"], 0)

    async def test_zendesk_oauth_state_and_discord_share_link_on_mongo(self):
        from urllib.parse import parse_qs, urlparse
        w, zid = await self.zendesk_world()
        await w.call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                     {"secret": "zendesk-client-secret-mongo", "metadata": {"client_id": "bitbotpro"}})
        r = await w.call("POST", f"/integrations/{zid}/zendesk/oauth/start", "admin", {"subdomain": "acme"})
        self.assertEqual(r.status_code, 200, r.text)
        state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
        w.zendesk.codes["mongocode1234"] = ("access-mongo", "refresh-mongo", "acme.zendesk.com", "admin")
        results = await asyncio.gather(*[w.call("GET", f"/hooks/zendesk/oauth/callback?state={state}&code=mongocode1234")
                                         for _ in range(5)])
        self.assertEqual(sorted(x.json()["status"] for x in results).count("connected"), 1)
        r = await w.call("POST", f"/instances/{w.iA_dc.id}/channel/discord/share-links", "admin",
                         {"expected_guild_id": "666666666666666666"})
        self.assertEqual(r.status_code, 201, r.text)
        r = await w.call("PUT", f"/instances/{w.iA_dc.id}/channel/discord/handoffs/222222222222222221", "admin",
                         {"minutes": 5})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(await self.db.discord_share_links.count_documents({}), 1)

    async def test_bot_deletion_with_zendesk_cleanup_on_mongo(self):
        from core import deletion
        from runtime.zendesk import zendesk_cleanup_executors
        from runtime.telegram import telegram_cleanup_executors
        from runtime.discord import discord_cleanup_executors
        w, zid = await self.zendesk_world()
        r = await w.call("POST", f"/integrations/{zid}/zendesk/webhook/provision", "admin")
        hid = r.json()["provisioned_webhook_id"]
        pv = (await w.call("POST", f"/bots/{w.bA.id}/deletion-preview", "owner")).json()
        r = await w.call("POST", f"/bots/{w.bA.id}/delete", "owner",
                         {"preview_token": pv["preview_token"], "confirm_name": pv["confirm_name"]})
        execs = {**zendesk_cleanup_executors(w.S.box, transport=w.S.zendesk_transport, resolver=w.S.zendesk_resolver),
                 **telegram_cleanup_executors(w.S.box, transport=w.S.telegram_transport),
                 **discord_cleanup_executors(w.S.box, transport=w.S.discord_transport)}
        done = await deletion.run_job(self.store, r.json()["id"], clock=w.S.clock, executors=execs)
        self.assertEqual(done["status"], "completed", done)
        self.assertNotIn(hid, w.zendesk.webhooks)
        self.assertEqual(await self.db.bot_integrations.count_documents({}), 0)


class MongoPhase4KTests(MongoBase):
    """Zendesk corrections (Z-G, Part C) and Phase 4K knowledge on real MongoDB."""

    async def zworld(self):
        from tests.unit._api_fixtures import ZD_API_TOKEN
        from tests.unit.test_api_webchat import FakeProvider
        w = await self.world()
        w.S.providers = {"anthropic": FakeProvider(text="zendesk answer")}
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})
        zid = (await w.call("POST", f"/bots/{w.bA.id}/integrations", "admin",
                            {"type": "zendesk", "name": "Support"})).json()["id"]
        cred = (await w.call("POST", f"/organisations/{w.oA.id}/credentials", "admin",
                             {"provider": "zendesk_api_token", "name": "Z", "secret": ZD_API_TOKEN,
                              "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})).json()["id"]
        await w.call("PATCH", f"/integrations/{zid}", "admin", {"credential_id": cred})
        return w, zid

    async def test_concurrent_workers_generate_and_bill_once_on_mongo(self):
        from api.integrations import service_for
        from runtime.zendesk_integration import event_doc_id
        w, zid = await self.zworld()
        svc = service_for(w.S)
        await svc.accept(await self.store.get("bot_integrations", zid), event_id="m1", ticket_id="101")
        await asyncio.gather(*[service_for(w.S).maintain() for _ in range(N // 3)])
        self.assertEqual(len(w.S.providers["anthropic"].calls), 1)
        ledger = [x async for x in self.db.usage_ledger.find({})]
        self.assertEqual([x["state"] for x in ledger], ["settled"])
        self.assertEqual(await self.db.usage_events.count_documents({"kind": "llm_call"}), 1)
        self.assertEqual(len(w.zendesk.notes()), 1)
        ev = await self.db.zendesk_events.find_one({"_id": event_doc_id(zid, "m1")})
        self.assertEqual((ev["status"], ev["generation"]["state"]), ("done", "settled"))

    async def test_uncertain_outcomes_and_crash_recovery_on_mongo(self):
        from api.integrations import service_for
        from runtime.errors import ProviderUncertain
        from runtime.zendesk_integration import event_doc_id
        w, zid = await self.zworld()
        prov = w.S.providers["anthropic"]
        svc = service_for(w.S)
        integ = await self.store.get("bot_integrations", zid)
        prov.error = ProviderUncertain()
        await svc.accept(integ, event_id="u1", ticket_id="101")
        await svc.process(event_doc_id(zid, "u1"))
        prov.error = None
        for _ in range(3):
            await service_for(w.S).maintain()
        self.assertEqual(len(prov.calls), 1)                                  # never regenerated
        ev = await self.db.zendesk_events.find_one({"_id": event_doc_id(zid, "u1")})
        self.assertEqual(ev["status"], "uncertain")
        led = await self.db.usage_ledger.find_one({"_id": f"zd:{event_doc_id(zid, 'u1')}"})
        self.assertEqual(led["state"], "uncertain")
        await svc.resolve_event(event_doc_id(zid, "u1"), integration_id=zid, enterprise_id=w.eA.id,
                                decision="waive", actor_id=w.users["admin"].id)
        led = await self.db.usage_ledger.find_one({"_id": f"zd:{event_doc_id(zid, 'u1')}"})
        self.assertEqual(led["state"], "released")
        # a worker that died holding no reservation: requeued and processed once
        await svc.accept(integ, event_id="c1", ticket_id="101")
        await self.db.zendesk_events.update_one({"_id": event_doc_id(zid, "c1")},
                                                {"$set": {"status": "processing",
                                                          "lease_until": "2020-01-01T00:00:00+00:00"},
                                                 "$inc": {"version": 1}})
        await service_for(w.S).maintain()
        await service_for(w.S).maintain()
        ev = await self.db.zendesk_events.find_one({"_id": event_doc_id(zid, "c1")})
        self.assertEqual(ev["status"], "done")
        self.assertEqual(len(prov.calls), 2)

    async def test_concurrent_deletion_requests_and_fence_on_mongo(self):
        from api.integrations import service_for
        w, zid = await self.zworld()
        await w.call("PUT", f"/integrations/{zid}/zendesk/signing-secret", "admin",
                     {"secret": "mongo-signing-secret-0123456789"})
        hid = (await w.call("POST", f"/integrations/{zid}/zendesk/webhook/provision", "admin")).json()[
            "provisioned_webhook_id"]
        svc = service_for(w.S)
        # leases and deletion requests race on the same document: no lease is granted after a request
        results = await asyncio.gather(*([svc._lease(zid, f"s{i}") for i in range(5)] +
                                         [svc.request_deletion(zid, actor_id=w.users["admin"].id) for _ in range(5)]))
        doc = await self.store.get("bot_integrations", zid)
        self.assertTrue(doc["deletion"])
        # leases granted before the request may exist; none can be granted after it
        self.assertEqual(await svc._lease(zid, "late"), (False, "integration_unavailable"))
        for holder in list((doc.get("request_leases") or {})):
            await svc._unlease(zid, holder)
        results = await asyncio.gather(*[w.call("DELETE", f"/integrations/{zid}", "admin") for _ in range(5)])
        await service_for(w.S).maintain()
        self.assertIsNone(await self.store.get("bot_integrations", zid))
        self.assertNotIn(hid, w.zendesk.webhooks)
        for action in ("integration.delete_requested", "integration.delete"):
            self.assertEqual(await self.db.audit_events.count_documents({"action": action, "target_id": zid}), 1)

    async def test_knowledge_ingestion_retrieval_and_isolation_on_mongo(self):
        import httpx
        from tests.unit.test_knowledge import GroundedProvider, REFUNDS
        w = await self.world()
        prov = GroundedProvider()
        w.S.providers = {"anthropic": prov}
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"ai.credential_id": w.cA_ai.id}})

        async def upload(who, src, name, text):
            h = {**w.headers(who), "Content-Type": "text/plain", "X-Requested-With": "BitBotPro"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
                return await c.put(f"/api/knowledge/sources/{src}/documents/{name}", content=text.encode(), headers=h)
        src = (await w.call("POST", f"/enterprises/{w.eA.id}/knowledge/sources", "admin", {"name": "P"})).json()["id"]
        src_b = (await w.call("POST", f"/enterprises/{w.eB.id}/knowledge/sources", "owner_b", {"name": "B"})).json()["id"]
        await upload("owner_b", src_b, "b.txt", "Refunds for Enterprise B take 2 days.")
        rs = await asyncio.gather(*[upload("admin", src, "refunds.txt", REFUNDS) for _ in range(5)])
        self.assertTrue(all(r.status_code == 200 for r in rs), [r.text for r in rs])
        self.assertEqual(await self.db.knowledge_documents.count_documents({"source_id": src}), 1)
        await w.call("PATCH", f"/enterprises/{w.eA.id}/policy", "owner",
                     {"changes": {"knowledge.allowed_sources": [src],
                                  "knowledge.minimum_mode": "strict_knowledge_only"}})
        await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner",
                     {"changes": {"knowledge.approved_sources": [src], "knowledge.mode": "strict_knowledge_only",
                                  "knowledge.insufficient_fallback": "Please contact our support team."}})
        P = f"/api/public/instances/{w.iA_web.channel.public_id}/chat"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
            r = await c.post(P, json={"message": "How long do I have to request a refund?"},
                             headers={"Origin": "https://shop.example"})
            self.assertIn("14 days", r.json()["reply"])
            self.assertTrue(all(e.enterprise_id == w.eA.id for e in prov.calls[-1].evidence))
            await w.call("PATCH", f"/knowledge/sources/{src}", "admin", {"status": "revoked"})
            r = await c.post(P, json={"message": "How long do I have to request a refund?"},
                             headers={"Origin": "https://shop.example"})
            self.assertEqual(r.json()["reply"], "Please contact our support team.")
        docs = [d async for d in self.db.knowledge_documents.find({"source_id": src})]
        await w.call("DELETE", f"/knowledge/documents/{docs[0]['_id']}", "admin")
        self.assertEqual(await self.db.knowledge_chunks.count_documents({"source_id": src}), 0)
        self.assertEqual(await self.db.knowledge_files.count_documents({"source_id": src}), 0)
        self.assertEqual(await self.db.knowledge_chunks.count_documents({"source_id": src_b}), 1)


class MongoApiTests(MongoBase):
    async def test_cross_tenant_requests_are_refused_and_change_nothing(self):
        w = await self.world()
        routes = [("GET", f"/enterprises/{w.eA.id}"), ("GET", f"/bots/{w.bA.id}/config"),
                  ("PATCH", f"/bots/{w.bA.id}/config"), ("POST", f"/organisations/{w.oA.id}/bots"),
                  ("POST", f"/instances/{w.iA_web.id}/suspend"), ("POST", f"/bots/{w.bA.id}/deletion-preview"),
                  ("GET", f"/credentials/{w.cA_tg.id}"), ("POST", f"/credentials/{w.cA_tg.id}/revoke"),
                  ("DELETE", f"/enterprises/{w.eA.id}/members/{w.users['viewer'].id}"),
                  ("POST", f"/enterprises/{w.eA.id}/invitations")]
        bodies = {"PATCH": {"changes": {"behaviour.language": "de"}}, "POST": {"name": "x"}}
        for method, path in routes:
            with self.subTest(path=path):
                before = await w.snapshot()
                body = bodies.get(method)
                if path.endswith("/invitations"):
                    body = {"email": "a@example.test", "role": "viewer"}
                if path.endswith("/suspend") or path.endswith("/revoke") or path.endswith("-preview"):
                    body = None
                r = await w.call(method, path, "owner_b", body)
                self.assertEqual(r.status_code, 404)
                self.assertEqual(await w.snapshot(), before)

    async def test_persistence_round_trip_and_inheritance(self):
        w = await self.world()
        r = await w.call("PATCH", f"/enterprises/{w.eA.id}/config", "owner", {"changes": {"behaviour.language": "de"}})
        self.assertEqual(r.status_code, 200, r.text)
        r = await w.call("PATCH", f"/enterprises/{w.eA.id}/policy", "owner",
                         {"changes": {"knowledge.minimum_mode": "strict_knowledge_only"}})
        self.assertEqual(r.status_code, 200, r.text)
        # a fresh service stack on the same database sees the same state
        from api.app import create_app
        from api.common import Services, Settings
        from tests.unit._api_fixtures import FERNET_KEY, JWT_SECRET
        w.S = Services(self.store, Settings(jwt_secret=JWT_SECRET, encryption_key=FERNET_KEY))
        w.app = create_app(w.S)
        cfg = (await w.call("GET", f"/instances/{w.iA_web.id}/config", "viewer")).json()
        self.assertEqual(cfg["values"]["behaviour.language"], "de")
        self.assertEqual(cfg["values"]["knowledge.mode"], "strict_knowledge_only")
        r = await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": {"knowledge.mode": "general"}})
        self.assertEqual(r.status_code, 422)
        doc = await self.db.bots.find_one({"_id": w.bA.id})
        self.assertEqual(doc["overrides"], {})                                       # sparse, nothing copied

    async def test_enterprise_deletion_on_mongo(self):
        w = await self.world()
        pv = (await w.call("POST", f"/enterprises/{w.eA.id}/deletion-preview", "owner")).json()
        r = await w.call("POST", f"/enterprises/{w.eA.id}/delete", "owner",
                         {"preview_token": pv["preview_token"], "confirm_name": w.eA.name})
        self.assertEqual(r.status_code, 202, r.text)
        for col in ("organisations", "bots", "bot_instances", "memberships"):
            self.assertEqual(await self.db[col].count_documents({"enterprise_id": w.eA.id}), 0, col)
            self.assertGreater(await self.db[col].count_documents({"enterprise_id": w.eB.id}), 0, col)
        self.assertIsNone(await self.db.enterprises.find_one({"_id": w.eA.id}))
        self.assertIsNone(await self.db.membership_guards.find_one({"_id": w.eA.id}))
        self.assertEqual(await self.db.channel_bindings.count_documents({"holders.enterprise_id": w.eA.id}), 0)
        job = await self.db.deletion_jobs.find_one({"_id": f"enterprise:{w.eA.id}"})
        # X12: mandatory external cleanup has no adapter yet -> truthfully not completed
        self.assertEqual(job["status"], "awaiting_external_cleanup")
        kept = [c["_id"] async for c in self.db.credentials.find({"enterprise_id": w.eA.id})]
        self.assertEqual(kept, [w.cA_tg.id])                                          # needed by cleanup
        self.assertNotIn("secret", str(job["external_actions"]).replace("secret_enc", ""))
        for a in job["external_actions"]:
            if a["state"] not in ("succeeded", "not_required"):
                r = await w.call("POST", f"/deletion-jobs/{job['_id']}/actions/{a['id']}/resolve", "root",
                                 {"reason": "integration test"})
                self.assertEqual(r.status_code, 200, r.text)
        job = await self.db.deletion_jobs.find_one({"_id": f"enterprise:{w.eA.id}"})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(await self.db.credentials.count_documents({"enterprise_id": w.eA.id}), 0)

    async def test_concurrent_invitation_acceptance_and_owner_removal_via_api(self):
        w = await self.world()
        r = await w.call("POST", f"/enterprises/{w.eA.id}/invitations", "owner",
                         {"email": "outsider@example.test", "role": "viewer", "delivery": "link"})
        token = r.json()["token"]
        results = await asyncio.gather(*[w.call("POST", "/invitations/accept", "outsider", {"token": token})
                                         for _ in range(8)])
        self.assertEqual(sorted(x.status_code for x in results).count(200), 1)
        r = await w.call("PATCH", f"/enterprises/{w.eA.id}/members/{w.users['admin'].id}", "owner", {"role": "owner"})
        self.assertEqual(r.status_code, 200)
        await w.refresh_user("admin")
        await w.refresh_user("owner")
        results = await asyncio.gather(
            w.call("DELETE", f"/enterprises/{w.eA.id}/members/{w.users['admin'].id}", "owner"),
            w.call("DELETE", f"/enterprises/{w.eA.id}/members/{w.users['owner'].id}", "admin"))
        self.assertEqual(sorted(x.status_code for x in results), [200, 403])
        owners = await self.db.memberships.count_documents({"enterprise_id": w.eA.id, "role": "owner"})
        self.assertEqual(owners, 1)
        guard = await self.db.membership_guards.find_one({"_id": w.eA.id})
        self.assertEqual(guard["owner_count"], 1)


if __name__ == "__main__":
    unittest.main()


class MongoPhase6BKeyRotationTests(MongoBase):
    """Phase 6B: key rotation on a real standalone MongoDB (test-only keys)."""

    def ring(self, active, **material):
        from core.credentials import KeyRing, SecretBox
        return SecretBox(KeyRing(material, active))

    async def test_scan_and_field_compare_and_set_on_mongo(self):
        from core.hierarchy import HierarchyError
        for i in (3, 1, 2):
            await self.store.insert("credentials_scan_probe", {"id": f"id{i}", "version": 7, "h": {"x_enc": "a"}})
        self.assertEqual([d["id"] for d in await self.store.scan("credentials_scan_probe", limit=2)], ["id1", "id2"])
        self.assertEqual([d["id"] for d in await self.store.scan("credentials_scan_probe", after="id2")], ["id3"])
        self.assertTrue(await self.store.set_field_if("credentials_scan_probe", "id1", "h.x_enc", "a", "b"))
        self.assertFalse(await self.store.set_field_if("credentials_scan_probe", "id1", "h.x_enc", "a", "c"))
        doc = await self.store.get("credentials_scan_probe", "id1")
        self.assertEqual((doc["h"]["x_enc"], doc["version"]), ("b", 7))
        with self.assertRaises(HierarchyError):
            await self.store.set_field_if("credentials_scan_probe", "id1", "version", "7", "8")

    async def test_rotation_crash_resume_concurrency_and_rollback(self):
        from cryptography.fernet import Fernet
        from core import key_rotation as kr
        from tests.unit._api_fixtures import FERNET_KEY
        w = await self.world()
        k1, k2 = FERNET_KEY, Fernet.generate_key().decode()
        old = self.ring("k1", k1=k1)
        expected = {}
        for coll in kr.ENCRYPTED_COLLECTIONS:
            for doc in await self.store.scan(coll, limit=500):
                for path, value in kr.encrypted_fields(doc):
                    if value:
                        expected[(coll, doc["id"], path)] = old.decrypt(value)
        self.assertGreaterEqual(len(expected), 3)
        new = self.ring("k2", k1=k1, k2=k2)
        state = {"n": 0}

        async def crash(coll, after):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            await kr.rotate(self.store, new, now=w.S.clock, batch=1, on_batch=crash)
        # two operators resume at once: both finish, nothing is lost or double-encrypted
        await asyncio.gather(kr.rotate(self.store, new, now=w.S.clock, batch=1),
                             kr.rotate(self.store, new, now=w.S.clock, batch=2))
        v = await kr.verify(self.store, new)
        self.assertTrue(v["ok"], v)
        got = {}
        only_new = self.ring("k2", k2=k2)
        for (coll, oid, path) in expected:
            doc = await self.store.get(coll, oid)
            cur = doc
            for p in path.split("."):
                cur = cur[p]
            got[(coll, oid, path)] = only_new.decrypt(cur)
        self.assertEqual(got, expected)
        back = self.ring("k1", k1=k1, k2=k2)
        await kr.rotate(self.store, back, now=w.S.clock)
        self.assertTrue((await kr.verify(self.store, back))["ok"])

    async def test_the_operator_command_against_mongo(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest import mock
        from cryptography.fernet import Fernet
        from api import rotate_keys
        from tests.unit._api_fixtures import FERNET_KEY
        await self.world()
        k2 = Fernet.generate_key().decode()
        env = {"MONGO_URL": MONGO_TEST_URL, "DB_NAME": self.db_name,
               "ENCRYPTION_KEYS": f"k1={FERNET_KEY}\nk2={k2}", "ENCRYPTION_ACTIVE_KEY_ID": "k2"}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(out):
                code = rotate_keys.main(list(argv))
            return code, out.getvalue()

        loop = asyncio.get_running_loop()
        code, out = await loop.run_in_executor(None, run, "verify")
        self.assertEqual(code, 1)
        code, out = await loop.run_in_executor(None, run, "rotate")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["verify"]["ok"])
        for k in (FERNET_KEY, k2):
            self.assertNotIn(k, out)
        self.assertNotIn("enc1:", out)
        env["DB_NAME"] = self.db_name + "_absent"
        code, out = await loop.run_in_executor(None, run, "status")
        self.assertEqual(code, 2)                                            # not a Spec 012 database
