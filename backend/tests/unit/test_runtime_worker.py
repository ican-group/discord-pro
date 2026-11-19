"""Worker foundation (Phase 4A): restart safety, leases, bounded retries,
no duplicate external effects, binding cleanup, membership intent sweep,
Discord desired state, tenant isolation, sanitised audit. Mock adapters only."""
import json
import unittest
from datetime import datetime, timedelta

from tests.unit._core_fixtures import Tree, run

from core import deletion as dl
from core import memberships as ms
from core.authz import Actor, Role
from core.hierarchy import Level, LifecycleAction, new_id
from core.lifecycle import Authority
from runtime.worker import MockCleanupAdapter, MockConnectionManager, Worker, discord_desired_state

GUILD = "999999999999999999"
SECRET_MARKER = "123456:SECRET-TELEGRAM-TOKEN-NEVER-LOGGED"


class Clock:
    def __init__(self):
        self.t = datetime.fromisoformat("2026-11-18T00:00:00+00:00")

    def __call__(self):
        return self.t.isoformat()

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class World:
    def __init__(self):
        self.t = Tree()
        self.clock = Clock()
        self.root = Actor(new_id(), True)
        self.eA, self.eB = self.t.enterprise(), self.t.enterprise()
        self.oA, self.oB = self.t.organisation(self.eA.id), self.t.organisation(self.eB.id)
        self.bA, self.bB = self.t.bot(self.oA.id), self.t.bot(self.oB.id)
        self.cred = new_id()
        run(self.t.store.insert("credentials", {"id": self.cred, "enterprise_id": self.eA.id,
                                                "organisation_id": self.oA.id, "provider": "telegram_bot",
                                                "name": "tg", "fingerprint": "f", "status": "active",
                                                "secret_enc": "enc:" + SECRET_MARKER, "version": 1}))
        self.tg = run(self.t.svc.create_instance(bot_id=self.bA.id, name="tg", actor_id="u", telegram_bot_id="123456",
                                                 channel={"type": "telegram", "credential_id": self.cred}))
        self.dc = self.t.instance(self.bA.id, {"type": "discord"})
        self.dc = run(self.t.svc.bind_discord_guild(self.dc.id, GUILD, application_id="910000000000000001", actor_id="u"))
        self.web_b = self.t.instance(self.bB.id)

    def delete(self, level, obj):
        run(self.t.svc.change_lifecycle(level, obj.id, LifecycleAction.BEGIN_DELETE, authority=Authority.ENTERPRISE,
                                        actor_id="u"))
        pv = run(dl.preview(self.t.store, level, obj.id, obj.enterprise_id if level is not Level.ENTERPRISE else obj.id))
        job, _ = run(dl.create_job(self.t.store, level=level.value, target_id=obj.id,
                                   enterprise_id=pv["enterprise_id"], target_name=obj.name, actor=self.root,
                                   now=self.clock(), external=pv["external_actions"], counts=pv["counts"]))
        return job["id"]

    def worker(self, name="w1", adapter=None, **kw):
        self.adapter = adapter or MockCleanupAdapter()
        return Worker(self.t.store, clock=self.clock, worker_id=name, executors=self.adapter.executors(), **kw)

    def job(self, jid):
        return run(self.t.store.get("deletion_jobs", jid))


class WorkerJobTests(unittest.TestCase):
    def test_worker_completes_a_job_with_mock_cleanup_exactly_once(self):
        w = World()
        jid = w.delete(Level.BOT, w.bA)
        report = run(w.worker().run_once())
        self.assertEqual(report.jobs_run, [jid])
        job = w.job(jid)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(sorted(a["action"] for a in job["external_actions"]),
                         ["discord_leave_guild", "discord_routing_removal", "telegram_delete_webhook"])
        self.assertEqual(len(w.adapter.effects), 3)
        first = w.adapter
        run(w.worker(adapter=first).run_once())                      # nothing runs twice
        self.assertEqual(len(first.calls), 3)
        self.assertEqual(run(w.t.store.count("bot_instances", {"enterprise_id": w.eB.id})), 1)   # other tenant intact

    def test_live_lease_is_respected_and_expired_lease_is_recovered(self):
        w = World()
        jid = w.delete(Level.BOT, w.bA)
        job = w.job(jid)
        lease = (w.clock.t + timedelta(seconds=dl.LEASE_SECONDS)).isoformat()
        run(w.t.store.update("deletion_jobs", jid, {"status": "running", "lease_until": lease, "worker": "crashed"},
                             expect_version=job["version"]))
        report = run(w.worker("w2").run_once())
        self.assertEqual((report.jobs_run, report.jobs_skipped), ([], [jid]))
        self.assertEqual(w.adapter.calls, [])
        w.clock.advance(dl.LEASE_SECONDS + 1)                         # the crashed worker never comes back
        report = run(w.worker("w2").run_once())
        self.assertEqual(report.jobs_run, [jid])
        self.assertEqual(w.job(jid)["status"], "completed")

    def test_crash_after_external_effect_does_not_duplicate_it(self):
        w = World()
        jid = w.delete(Level.BOT_INSTANCE, w.tg)
        adapter = MockCleanupAdapter()
        store = w.t.store
        original = store.update
        state = {"crashed": False}

        async def crash_once(collection, oid, fields, **kw):
            if collection == "deletion_jobs" and "external_actions" in fields and not state["crashed"]:
                state["crashed"] = True
                raise RuntimeError("process killed")              # after the adapter call, before the save
            return await original(collection, oid, fields, **kw)
        store.update = crash_once
        run(w.worker("w1", adapter=adapter).run_once())
        store.update = original
        self.assertNotEqual(w.job(jid)["status"], "completed")
        w.clock.advance(dl.LEASE_SECONDS + 1)
        run(w.worker("w2", adapter=adapter).run_once())
        self.assertEqual(w.job(jid)["status"], "completed")
        key = f"{jid}:telegram_delete_webhook:{w.tg.id}"
        self.assertEqual(adapter.calls.count(key), 2)                 # re-invoked with the same key ...
        self.assertEqual(adapter.effects.count(key), 1)               # ... but the effect happened once

    def test_bounded_retries_and_backoff(self):
        w = World()
        jid = w.delete(Level.BOT_INSTANCE, w.tg)
        adapter = MockCleanupAdapter(fail_times={"telegram_delete_webhook": 100})
        for _ in range(dl.MAX_ATTEMPTS_MANDATORY + 3):
            run(w.worker(adapter=adapter).run_once())
            w.clock.advance(dl.BACKOFF_MAX_SECONDS + 1)
        job = w.job(jid)
        self.assertEqual(job["status"], "cleanup_failed")
        self.assertEqual(len(adapter.calls), dl.MAX_ATTEMPTS_MANDATORY)   # bounded
        self.assertEqual(adapter.effects, [])

    def test_failed_internal_steps_are_retried_a_bounded_number_of_times(self):
        w = World()
        jid = w.delete(Level.BOT, w.bA)
        store = w.t.store
        original = store.delete_many

        async def broken(collection, flt):
            if collection == "conversations":
                raise RuntimeError("disk full at /data/" + SECRET_MARKER)
            return await original(collection, flt)
        store.delete_many = broken
        worker = w.worker(max_job_attempts=3)
        for _ in range(6):
            run(worker.run_once())
        job = w.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("failed", 3))
        audit = json.dumps(run(store.find("audit_events", {})), default=str)
        self.assertIn("worker.job_failed", audit)
        self.assertNotIn(SECRET_MARKER, audit + json.dumps(job, default=str))
        store.delete_many = original
        run(dl.retry_failed(store, jid, clock=w.clock))           # operator resume is still possible
        run(dl.run_job(store, jid, clock=w.clock, executors=w.adapter.executors()))
        self.assertEqual(w.job(jid)["status"], "completed")


class WorkerMaintenanceTests(unittest.TestCase):
    def test_stale_binding_claims_are_reconciled_after_the_grace_period(self):
        w = World()
        ghost = new_id()
        run(w.t.svc.bindings.claim(ghost, w.eA.id, {"webchat:pk_ghostghostghostghostgh": ("*",)}))
        self.assertEqual(run(w.worker().run_once()).claims_reconciled, 0)
        w.clock.advance(3600)
        self.assertEqual(run(w.worker().run_once()).claims_reconciled, 1)
        self.assertEqual(run(w.t.svc.bindings.holders("webchat:pk_ghostghostghostghostgh")), {})
        self.assertTrue(run(w.t.svc.bindings.holders(f"discord:910000000000000001:{GUILD}")))         # live claims untouched

    def test_crashed_membership_intent_is_swept(self):
        w = World()
        chain = w.t.chain(Level.ENTERPRISE, w.eA.id)
        original = ms._finish

        async def crash(store, eid, intent_id):
            return ms.Result(intent_id, None)
        ms._finish = crash
        run(ms.mutate(w.t.store, w.eA.id, ms.op_grant(w.root, chain, user_id=new_id(), role=Role.OWNER, now="t",
                                                      bypass_authority=True)))
        ms._finish = original
        w.clock.advance(60)
        self.assertEqual(run(w.worker().run_once()).intents_resolved, 0)   # clock is fake; real-time check
        report = run(Worker(w.t.store, clock=lambda: (datetime.now().astimezone() + timedelta(minutes=5)).isoformat(),
                            worker_id="w3").run_once())
        self.assertEqual(report.intents_resolved, 1)
        self.assertIsNone(run(w.t.store.get(ms.GUARDS, w.eA.id))["pending"])

    def test_discord_desired_state_and_connection_sync(self):
        w = World()
        mgr = MockConnectionManager()
        report = run(w.worker(connection_manager=mgr).run_once())
        self.assertEqual(mgr.desired, {"910000000000000001": {GUILD: [w.dc.id]}})
        self.assertEqual(report.desired_guilds, 1)
        jid = w.delete(Level.BOT_INSTANCE, w.dc)
        run(w.worker(connection_manager=mgr).run_once())
        self.assertEqual(mgr.desired, {})
        self.assertEqual(w.job(jid)["status"], "completed")

    def test_heartbeat(self):
        w = World()
        run(w.worker("w-heart").run_once())
        beat = run(w.t.store.get("worker_heartbeats", "w-heart"))
        self.assertEqual(beat["state"], "idle")


if __name__ == "__main__":
    unittest.main()
