"""Atomicity of MongoDB-backed security counters and single-use codes against a
REAL MongoDB (spec 008 Stage 3).

Runs only when MONGO_TEST_URL is set, and refuses to run against anything that
looks like the production database. Intended for a disposable mongod on an
isolated, internal Docker network (deploy/scripts/run-stage3-integration-tests.sh).
"""
import asyncio
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from tests.unit import _support  # noqa: F401  (sys.path)

MONGO_TEST_URL = os.environ.get("MONGO_TEST_URL", "")
DB_NAME = os.environ.get("MONGO_TEST_DB", "bbp_itest")
SAFE = bool(MONGO_TEST_URL) and "bitbotpro-mongo" not in MONGO_TEST_URL and DB_NAME != "bitbotpro" \
    and DB_NAME.startswith("bbp_itest")


@unittest.skipUnless(SAFE, "MONGO_TEST_URL not set (or not a disposable test database)")
class MongoAtomicityTests(unittest.TestCase):
    def setUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.run_id = uuid.uuid4().hex[:8]
        self.db_name = f"{DB_NAME}_{self.run_id}"

    def _with_db(self, coro_fn):
        from motor.motor_asyncio import AsyncIOMotorClient

        async def runner():
            client = AsyncIOMotorClient(MONGO_TEST_URL, serverSelectionTimeoutMS=10000)
            try:
                db = client[self.db_name]
                try:
                    return await coro_fn(db)
                finally:
                    await client.drop_database(self.db_name)
            finally:
                client.close()
        return asyncio.run(runner())

    def test_concurrent_increments_on_one_key_are_never_lost(self):
        import rate_limit as rl

        async def body(db):
            c = rl.MongoCounter(db[rl.COUNTERS_COLLECTION], "itest", 5, 900)
            results = await asyncio.gather(*(c.record_failure("acct") for _ in range(200)))
            doc = await db[rl.COUNTERS_COLLECTION].find_one({"_id": "itest:acct"})
            return results, doc
        results, doc = self._with_db(body)
        self.assertEqual(doc["count"], 200)
        self.assertEqual(sorted(d.count for d in results), list(range(1, 201)))
        self.assertEqual(sum(1 for d in results if d.allowed), 4)

    def test_concurrent_first_insert_race_on_new_keys(self):
        import rate_limit as rl

        async def body(db):
            c = rl.MongoCounter(db[rl.COUNTERS_COLLECTION], "race", 1000, 900)
            keys = [f"k{i}" for i in range(20)]
            await asyncio.gather(*(c.record_failure(k) for k in keys for _ in range(25)))
            return {d["_id"]: d["count"] async for d in db[rl.COUNTERS_COLLECTION].find({})}
        counts = self._with_db(body)
        self.assertEqual(len(counts), 20)
        self.assertTrue(all(v == 25 for v in counts.values()), counts)

    def test_expired_window_restarts_even_if_document_remains(self):
        import rate_limit as rl

        async def body(db):
            col = db[rl.COUNTERS_COLLECTION]
            await col.insert_one({"_id": "itest:old", "count": 999,
                                  "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)})
            c = rl.MongoCounter(col, "itest", 5, 900)
            before = await c.check("old")
            after = await c.record_failure("old")
            return before, after
        before, after = self._with_db(body)
        self.assertTrue(before.allowed)
        self.assertEqual(after.count, 1)

    def test_single_use_code_consumption_under_concurrency(self):
        import auth_security as a

        async def body(db):
            key = b"integration-test-key-not-real-0123456789abc"
            code, set_f, _ = a.issue_code(key, a.PURPOSE_RESET, "u1")
            await db.users.insert_one({"id": "u1", "token_version": 0, **set_f})
            f = a.RESET_FIELDS

            async def consume():
                r = await db.users.update_one(
                    {"id": "u1", f.id: set_f[f.id], f.hash: set_f[f.hash]},
                    {"$set": {"hashed_password": "x"}, "$unset": a.clear_code_fields(a.PURPOSE_RESET),
                     "$inc": {a.TOKEN_VERSION_FIELD: 1}})
                return r.modified_count
            results = await asyncio.gather(*(consume() for _ in range(50)))
            user = await db.users.find_one({"id": "u1"})
            return results, user
        results, user = self._with_db(body)
        self.assertEqual(sum(results), 1)                 # exactly one winner
        self.assertEqual(user["token_version"], 1)
        self.assertNotIn("reset_code_hash", user)

    def test_ttl_index_command_on_disposable_db(self):
        import io
        import manage
        import rate_limit as rl

        async def body(db):
            out = io.StringIO()
            dry = await manage.security_indexes(db, apply=False, confirm=False, out=out)
            applied = await manage.security_indexes(db, apply=True, confirm=True, out=out)
            again = await manage.security_indexes(db, apply=True, confirm=True, out=out)
            info = await db[rl.COUNTERS_COLLECTION].index_information()
            return dry, applied, again, info
        dry, applied, again, info = self._with_db(body)
        self.assertEqual((dry, applied, again), (2, 0, 0))
        idx = info[rl.TTL_INDEX_NAME]
        self.assertEqual(idx["key"], [("expires_at", 1)])
        self.assertEqual(idx["expireAfterSeconds"], 0)
        self.assertEqual(set(info), {"_id_", rl.TTL_INDEX_NAME})

    def test_conversation_appends_are_atomic_and_instance_scoped(self):
        """Same upsert as server.save_conversation (spec 008 Stage 4)."""
        async def save(db, sid, iid, i):
            await db.conversations.update_one(
                {"session_id": sid, "instance_id": iid},
                {"$push": {"messages": {"$each": [{"role": "user", "content": f"m{i}"},
                                                  {"role": "assistant", "content": "r"}]}},
                 "$set": {"updated_at": "t"},
                 "$setOnInsert": {"platform": "web", "created_at": "t"}},
                upsert=True)

        async def body(db):
            sid = "web_" + "s" * 32
            await save(db, sid, "bot-a", -1)                      # session exists (token issued after 1st message)
            await asyncio.gather(*(save(db, sid, "bot-a", i) for i in range(100)))
            await save(db, sid, "bot-b", 0)                       # same ID, other bot: separate document
            a = await db.conversations.find({"session_id": sid, "instance_id": "bot-a"}).to_list(10)
            b = await db.conversations.find({"session_id": sid, "instance_id": "bot-b"}).to_list(10)
            return a, b
        a, b = self._with_db(body)
        self.assertEqual((len(a), len(b)), (1, 1))
        self.assertEqual(len(a[0]["messages"]), 202)
        self.assertNotIn("bot-b", repr(a[0]))
        self.assertEqual(len(b[0]["messages"]), 2)

    def test_invitation_claim_has_exactly_one_winner(self):
        """Same conditional update as server.accept_invite (spec 008 Stage 8)."""
        async def body(db):
            await db.invitations.insert_one({"token": "T" * 40, "status": "pending"})

            async def claim():
                return await db.invitations.find_one_and_update(
                    {"token": "T" * 40, "status": "pending"}, {"$set": {"status": "accepting"}},
                    projection={"_id": 0}, return_document=True)
            return await asyncio.gather(*(claim() for _ in range(50)))
        results = self._with_db(body)
        self.assertEqual(sum(1 for r in results if r), 1)

    def test_oauth_state_consumed_exactly_once(self):
        import oauth_state as o

        async def body(db):
            col = db[o.COLLECTION]
            state = await o.issue(col, provider="discord", mode=o.MODE_INSTALL, user_id="u1", instance_id="bot-a")
            results = await asyncio.gather(*(o.consume(col, state, "discord") for _ in range(50)))
            stored = await col.count_documents({})
            return results, stored
        results, stored = self._with_db(body)
        winners = [r for r in results if r]
        self.assertEqual(len(winners), 1)
        self.assertEqual((winners[0]["user_id"], winners[0]["instance_id"], winners[0]["mode"]), ("u1", "bot-a", "install"))
        self.assertEqual(stored, 0)


if __name__ == "__main__":
    unittest.main()
