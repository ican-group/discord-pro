"""Permanent deletion core (spec 012 §8, D-Q3, D-Q13): preview counts, token
binding and expiry, idempotent resumable descendant-first jobs, tenant
scoping, user deletion safeguards."""
import unittest

from tests.unit._core_fixtures import Tree, run

from core import deletion as dl
from core import memberships as ms
from core.authz import Actor, Role
from core.hierarchy import Level, LifecycleAction, new_id
from core.identity import USERS, create_user, ensure_superadmin_guard, get_user
from core.lifecycle import Authority

GUILD = "444444444444444444"
NOW = "2026-11-18T00:00:00+00:00"


class World:
    """Enterprise A (2 orgs, bots, instances incl. Discord) and Enterprise B."""

    def __init__(self):
        t = self.t = Tree()
        self.root = Actor(new_id(), is_platform_superadmin=True)
        self.eA, self.eB = t.enterprise(), t.enterprise()
        self.oA1, self.oA2, self.oB = t.organisation(self.eA.id), t.organisation(self.eA.id), t.organisation(self.eB.id)
        self.bA1, self.bA2, self.bB = t.bot(self.oA1.id), t.bot(self.oA2.id), t.bot(self.oB.id)
        self.iA1 = t.instance(self.bA1.id)
        self.iA1d = t.instance(self.bA1.id, {"type": "discord"})
        self.iA1d = run(t.svc.bind_discord_guild(self.iA1d.id, GUILD, application_id="910000000000000001", actor_id="u"))
        self.iA2 = t.instance(self.bA2.id)
        self.iB = t.instance(self.bB.id)
        self.users = {k: new_id() for k in ("ownerA", "orgA1", "ownerB")}
        for eid, uid, scope_chain, role, admit in (
                (self.eA.id, self.users["ownerA"], t.chain(Level.ENTERPRISE, self.eA.id), Role.OWNER, False),
                (self.eA.id, self.users["orgA1"], t.chain(Level.ORGANISATION, self.oA1.id), Role.ADMIN, True),
                (self.eB.id, self.users["ownerB"], t.chain(Level.ENTERPRISE, self.eB.id), Role.OWNER, False)):
            run(ms.mutate(t.store, eid, ms.op_grant(self.root, scope_chain, user_id=uid, role=role, now=NOW,
                                                    bypass_authority=True, admit=admit)))
        for eid, oid in ((self.eA.id, self.oA1.id), (self.eA.id, None), (self.eB.id, self.oB.id)):
            run(t.store.insert("credentials", {"id": new_id(), "enterprise_id": eid, "organisation_id": oid,
                                               "provider": "openai", "name": "c", "fingerprint": "f",
                                               "status": "active", "secret_enc": "x", "version": 1}))
        for eid, oid, bid, iid in ((self.eA.id, self.oA1.id, self.bA1.id, self.iA1.id),
                                   (self.eB.id, self.oB.id, self.bB.id, self.iB.id)):
            run(t.store.insert("conversations", {"id": new_id(), "enterprise_id": eid, "organisation_id": oid,
                                                 "bot_id": bid, "instance_id": iid}))

    def begin(self, level, oid):
        return run(self.t.svc.change_lifecycle(level, oid, LifecycleAction.BEGIN_DELETE,
                                               authority=Authority.ENTERPRISE, actor_id="u"))

    def job(self, level, obj):
        self.begin(level, obj.id)
        pv = run(dl.preview(self.t.store, level, obj.id, self.eA.id))
        job, _ = run(dl.create_job(self.t.store, level=level.value, target_id=obj.id, enterprise_id=self.eA.id,
                                   target_name=obj.name, actor=self.root, now=NOW,
                                   external=pv["external_actions"], counts=pv["counts"]))
        return job

    def count(self, col, **flt):
        return run(self.t.store.count(col, flt))


def clock():
    return NOW


class Clock:
    def __init__(self, start=NOW):
        from datetime import datetime
        self.t = datetime.fromisoformat(start)

    def __call__(self):
        return self.t.isoformat()

    def advance(self, seconds):
        from datetime import timedelta
        self.t += timedelta(seconds=seconds)


def ok_executors(calls=None):
    async def ok(action, ctx):
        if calls is not None:
            calls.append(action["id"])
        return dl.CleanupOutcome(dl.CleanupStatus.SUCCEEDED)
    return {name: ok for name in ("discord_routing_removal", "discord_leave_guild", "telegram_delete_webhook",
                                  "zendesk_token_revocation", "zendesk_webhook_subscription_removal")}


class PreviewTests(unittest.TestCase):
    def test_preview_counts_are_exact_and_scoped(self):
        w = World()
        pv = run(dl.preview(w.t.store, Level.ORGANISATION, w.oA1.id, w.eA.id))
        c = pv["counts"]
        self.assertEqual(c["bots"], 1)
        self.assertEqual(c["bot_instances"], 2)
        self.assertEqual(c["organisations"], 1)
        self.assertEqual(c["credentials"], 1)              # only the Organisation-owned one
        self.assertEqual(c["memberships"], 1)              # orgA1's organisation membership
        self.assertEqual(c["conversations"], 1)
        pv_e = run(dl.preview(w.t.store, Level.ENTERPRISE, w.eA.id, w.eA.id))
        self.assertEqual(pv_e["counts"]["organisations"], 2)
        self.assertEqual(pv_e["counts"]["credentials"], 2)
        self.assertEqual(pv_e["counts"]["bot_instances"], 3)

    def test_preview_lists_required_external_cleanup(self):
        w = World()
        pv = run(dl.preview(w.t.store, Level.BOT_INSTANCE, w.iA1d.id, w.eA.id))
        actions = {a["action"]: a for a in pv["external_actions"]}
        self.assertTrue(actions["discord_routing_removal"]["mandatory"])
        self.assertFalse(actions["discord_leave_guild"]["mandatory"])          # best-effort, decided at run time
        self.assertTrue(all(a["state"] == "pending" for a in actions.values()))

    def test_token_is_bound_single_use_and_expires(self):
        store = World().t.store
        token, _ = run(dl.issue_token(store, actor_id="a", level="bot", target_id="t", digest="d", now=NOW,
                                      ttl_seconds=60))
        for kw in ({"actor_id": "b"}, {"level": "organisation"}, {"target_id": "other"}):
            t2, _ = run(dl.issue_token(store, actor_id="a", level="bot", target_id="t", digest="d", now=NOW))
            args = {"actor_id": "a", "level": "bot", "target_id": "t", **kw}
            with self.assertRaises(dl.DeletionError):
                run(dl.consume_token(store, t2, now=NOW, **args))
        with self.assertRaises(dl.DeletionError):
            run(dl.consume_token(store, token, actor_id="a", level="bot", target_id="t",
                                 now="2026-11-18T00:01:01+00:00"))                      # expired
        t3, _ = run(dl.issue_token(store, actor_id="a", level="bot", target_id="t", digest="d", now=NOW))
        self.assertEqual(run(dl.consume_token(store, t3, actor_id="a", level="bot", target_id="t", now=NOW))["digest"], "d")
        with self.assertRaises(dl.DeletionError):
            run(dl.consume_token(store, t3, actor_id="a", level="bot", target_id="t", now=NOW))   # used
        self.assertNotIn(t3, str(store.collections.get("deletion_previews")))               # only hashes stored


class JobTests(unittest.TestCase):
    def test_organisation_deletion_is_descendant_first_and_never_touches_others(self):
        w = World()
        job = w.job(Level.ORGANISATION, w.oA1)
        job = run(dl.run_job(w.t.store, job["id"], clock=clock))
        # data is gone, but mandatory Discord cleanup has no adapter in Phase 3.1: not "completed"
        self.assertEqual(job["status"], "awaiting_external_cleanup")
        self.assertEqual(job["steps_done"], list(dl.STEPS))
        self.assertEqual(w.count("organisations", id=w.oA1.id), 0)
        self.assertEqual(w.count("bots", organisation_id=w.oA1.id), 0)
        self.assertEqual(w.count("bot_instances", organisation_id=w.oA1.id), 0)
        self.assertEqual(w.count("credentials", organisation_id=w.oA1.id), 0)    # not needed by cleanup
        self.assertEqual(w.count("conversations", organisation_id=w.oA1.id), 0)
        # untouched: the Enterprise, the sibling Organisation, Enterprise credentials, the other tenant
        self.assertEqual(w.count("enterprises", id=w.eA.id), 1)
        self.assertEqual(w.count("organisations", id=w.oA2.id), 1)
        self.assertEqual(w.count("bot_instances", id=w.iA2.id), 1)
        self.assertEqual(w.count("credentials", enterprise_id=w.eA.id), 1)
        self.assertEqual(w.count("bot_instances", enterprise_id=w.eB.id), 1)
        self.assertEqual(w.count("conversations", enterprise_id=w.eB.id), 1)
        roster = run(ms.load(w.t.store, w.eA.id))
        self.assertEqual([m.user_id for m in roster.members], [w.users["ownerA"]])     # org membership gone
        self.assertEqual(run(w.t.svc.bindings.holders(f"discord:910000000000000001:{GUILD}")), {})          # binding released
        self.assertEqual({a["state"] for a in job["external_actions"]}, {"pending"})
        self.assertEqual({a["error_class"] for a in job["external_actions"] if a["state"] == "pending"},
                         {"no_adapter"})

    def test_crash_mid_job_then_resume_is_idempotent(self):
        w = World()
        job = w.job(Level.ENTERPRISE, w.eA)
        store = w.t.store
        original = store.delete_many

        async def flaky(collection, flt):
            if collection == "conversations":
                raise RuntimeError("simulated crash at 10.0.0.5")
            return await original(collection, flt)
        store.delete_many = flaky
        job = run(dl.run_job(store, job["id"], clock=clock))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "RuntimeError")          # type only; no details leak
        store.delete_many = original
        job = run(dl.run_job(store, job["id"], clock=clock, executors=ok_executors()))
        self.assertEqual(job["status"], "completed")
        again = run(dl.run_job(store, job["id"], clock=clock))   # re-running a completed job is a no-op
        self.assertEqual(again["status"], "completed")
        for col in ("organisations", "bots", "bot_instances", "credentials", "conversations"):
            self.assertEqual(w.count(col, enterprise_id=w.eA.id), 0, col)
        self.assertEqual(w.count("enterprises", id=w.eA.id), 0)
        self.assertEqual(w.count("memberships", enterprise_id=w.eA.id), 0)
        self.assertEqual(w.count("bot_instances", enterprise_id=w.eB.id), 1)
        self.assertEqual(w.count("memberships", enterprise_id=w.eB.id), 1)

    def test_leased_job_is_not_run_twice(self):
        w = World()
        job = w.job(Level.BOT, w.bA1)
        run(w.t.store.update("deletion_jobs", job["id"], {"status": "running",
                                                          "lease_until": "2999-01-01T00:00:00+00:00"},
                             expect_version=job["version"]))
        out = run(dl.run_job(w.t.store, job["id"], clock=clock))
        self.assertEqual(out["status"], "running")
        self.assertEqual(w.count("bots", id=w.bA1.id), 1)

    def test_target_must_be_marked_deleted(self):
        w = World()
        job, _ = run(dl.create_job(w.t.store, level="bot", target_id=w.bA1.id, enterprise_id=w.eA.id,
                                   target_name="x", actor=w.root, now=NOW, external=[], counts={}))
        out = run(dl.run_job(w.t.store, job["id"], clock=clock))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(w.count("bots", id=w.bA1.id), 1)

    def test_repeated_confirmation_converges_on_one_job(self):
        w = World()
        a, created_a = run(dl.create_job(w.t.store, level="bot", target_id=w.bA1.id, enterprise_id=w.eA.id,
                                         target_name="x", actor=w.root, now=NOW, external=[], counts={}))
        b, created_b = run(dl.create_job(w.t.store, level="bot", target_id=w.bA1.id, enterprise_id=w.eA.id,
                                         target_name="x", actor=w.root, now=NOW, external=[], counts={}))
        self.assertEqual((a["id"], created_a, created_b), (b["id"], True, False))


TG_TOKEN_MARKER = "123456789:SECRET-TOKEN-NEVER-IN-JOBS"


class GraceRepurgeTests(unittest.TestCase):
    def test_records_written_by_in_flight_requests_after_the_purge_are_removed_once(self):
        w = World()
        c = Clock()
        job = w.job(Level.ORGANISATION, w.oA1)
        job = run(dl.run_job(w.t.store, job["id"], clock=c, executors=ok_executors()))
        self.assertEqual(job["status"], "completed")
        # an in-flight request finishes after the purge and writes a runtime record
        run(w.t.store.insert("chat_requests", {"id": "late", "enterprise_id": w.eA.id, "organisation_id": w.oA1.id,
                                               "instance_id": w.iA1d.id, "session_id": "s", "status": "done",
                                               "version": 1}))
        run(w.t.store.insert("chat_requests", {"id": "other", "enterprise_id": w.eA.id, "organisation_id": w.oA2.id,
                                               "instance_id": w.iA2.id, "session_id": "s", "status": "done",
                                               "version": 1}))
        self.assertEqual(run(dl.repurge_due(w.t.store, now=c())), 0)            # not before the grace period
        c.advance(dl.REPURGE_DELAY_SECONDS + 1)
        self.assertEqual(run(dl.repurge_due(w.t.store, now=c())), 1)
        self.assertIsNone(run(w.t.store.get("chat_requests", "late")))
        self.assertIsNotNone(run(w.t.store.get("chat_requests", "other")))       # sibling Organisation untouched
        self.assertEqual(run(dl.repurge_due(w.t.store, now=c())), 0)             # once


class ExternalCleanupTests(unittest.TestCase):
    """X12: external cleanup state machine (no live calls; fake executors)."""

    def setUp(self):
        self.w = World()
        t = self.w.t
        self.cred_id = new_id()
        run(t.store.insert("credentials", {"id": self.cred_id, "enterprise_id": self.w.eA.id,
                                           "organisation_id": self.w.oA2.id, "provider": "telegram_bot",
                                           "name": "tg", "fingerprint": "f", "status": "active",
                                           "secret_enc": "encrypted:" + TG_TOKEN_MARKER, "version": 1}))
        self.tg = run(t.svc.create_instance(bot_id=self.w.bA2.id, name="tg", actor_id="u", telegram_bot_id="123456789",
                                            channel={"type": "telegram", "credential_id": self.cred_id}))

    def test_without_adapters_the_job_truthfully_awaits_cleanup(self):
        job = self.w.job(Level.ORGANISATION, self.w.oA2)
        job = run(dl.run_job(self.w.t.store, job["id"], clock=clock))
        self.assertEqual(job["status"], "awaiting_external_cleanup")
        tg = next(a for a in job["external_actions"] if a["action"] == "telegram_delete_webhook")
        self.assertEqual((tg["state"], tg["mandatory"], tg["credential_id"]), ("pending", True, self.cred_id))
        # the credential the cleanup needs is preserved (still encrypted), and held
        self.assertEqual(self.w.count("credentials", id=self.cred_id), 1)
        self.assertEqual(run(dl.unresolved_credential_holds(self.w.t.store, self.cred_id)), [job["id"]])
        self.assertNotIn(TG_TOKEN_MARKER, repr(job))                     # references only, never secrets

    def test_successful_cleanup_completes_and_then_releases_the_credential(self):
        calls = []
        job = self.w.job(Level.ORGANISATION, self.w.oA2)
        job = run(dl.run_job(self.w.t.store, job["id"], clock=clock, executors=ok_executors(calls)))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(calls, [f"telegram_delete_webhook:{self.tg.id}"])
        self.assertEqual(self.w.count("credentials", id=self.cred_id), 0)
        self.assertEqual(job["deleted"]["retained_credentials"], 1)
        self.assertEqual(run(dl.unresolved_credential_holds(self.w.t.store, self.cred_id)), [])
        run(dl.run_job(self.w.t.store, job["id"], clock=clock, executors=ok_executors(calls)))
        self.assertEqual(len(calls), 1)                                   # repeated runs never repeat cleanup

    def test_failures_retry_with_backoff_and_never_count_as_success(self):
        clk = Clock()
        attempts = []

        async def flaky(action, ctx):
            attempts.append(clk())
            raise RuntimeError("HTTP 500 from api.telegram.org token=" + TG_TOKEN_MARKER)
        job = self.w.job(Level.ORGANISATION, self.w.oA2)
        store = self.w.t.store
        job = run(dl.run_job(store, job["id"], clock=clk, executors={"telegram_delete_webhook": flaky}))
        tg = next(a for a in job["external_actions"] if a["action"] == "telegram_delete_webhook")
        self.assertEqual((job["status"], tg["state"], tg["attempts"], tg["error_class"]),
                         ("awaiting_external_cleanup", "retry_wait", 1, "runtimeerror"))
        self.assertNotIn(TG_TOKEN_MARKER, repr(job))
        run(dl.run_job(store, job["id"], clock=clk, executors={"telegram_delete_webhook": flaky}))
        self.assertEqual(len(attempts), 1)                                # not due yet: no new attempt
        for _ in range(dl.MAX_ATTEMPTS_MANDATORY):
            clk.advance(dl.BACKOFF_MAX_SECONDS + 1)
            job = run(dl.run_job(store, job["id"], clock=clk, executors={"telegram_delete_webhook": flaky}))
        tg = next(a for a in job["external_actions"] if a["action"] == "telegram_delete_webhook")
        self.assertEqual((job["status"], tg["state"]), ("cleanup_failed", "failed"))
        self.assertEqual(len(attempts), dl.MAX_ATTEMPTS_MANDATORY)
        self.assertEqual(self.w.count("credentials", id=self.cred_id), 1)   # still needed
        audit = run(store.find("audit_events", {"action": "deletion.cleanup_failed"}))
        self.assertEqual(len(audit), dl.MAX_ATTEMPTS_MANDATORY)
        self.assertNotIn(TG_TOKEN_MARKER, repr(audit))
        # a cleanup_failed job is not picked up again by workers ...
        clk.advance(dl.BACKOFF_MAX_SECONDS + 1)
        self.assertEqual(run(dl.run_job(store, job["id"], clock=clk))["status"], "cleanup_failed")
        # ... until an operator retries (fresh budget) and the adapter now succeeds
        run(dl.retry_failed(store, job["id"], clock=clk))
        job = run(dl.run_job(store, job["id"], clock=clk, executors=ok_executors()))
        self.assertEqual(job["status"], "completed")

    def test_operator_can_establish_an_action_is_not_required(self):
        job = self.w.job(Level.ORGANISATION, self.w.oA2)
        store = self.w.t.store
        job = run(dl.run_job(store, job["id"], clock=clock))
        action_id = f"telegram_delete_webhook:{self.tg.id}"
        with self.assertRaises(Exception):
            run(dl.resolve_action(store, job["id"], action_id, actor=Actor(new_id()), reason="x", clock=clock))
        run(dl.resolve_action(store, job["id"], action_id, actor=self.w.root, reason="bot deleted at Telegram",
                              clock=clock))
        job = run(dl.run_job(store, job["id"], clock=clock))
        self.assertEqual(job["status"], "completed")
        tg = next(a for a in job["external_actions"] if a["id"] == action_id)
        self.assertEqual(tg["resolution"]["reason"], "operator")

    def test_best_effort_exhaustion_does_not_block_completion_but_mandatory_does(self):
        clk = Clock()

        async def fail(action, ctx):
            return dl.CleanupOutcome(dl.CleanupStatus.RETRY, "Provider Said: NO <html>")
        ex = {**ok_executors(), "discord_leave_guild": fail}
        job = self.w.job(Level.BOT_INSTANCE, self.w.iA1d)
        store = self.w.t.store
        for _ in range(dl.MAX_ATTEMPTS_BEST_EFFORT + 1):
            job = run(dl.run_job(store, job["id"], clock=clk, executors=ex))
            clk.advance(dl.BACKOFF_MAX_SECONDS + 1)
        leave = next(a for a in job["external_actions"] if a["action"] == "discord_leave_guild")
        self.assertEqual((leave["state"], leave["error_class"]), ("abandoned", "error"))   # sanitised
        self.assertEqual(job["status"], "completed")

    def test_leave_guild_is_decided_at_execution_from_live_claims(self):
        w = self.w
        # another tenant's instance uses a different channel of the same guild
        other = w.t.instance(w.bB.id, {"type": "discord"})
        run(w.t.svc.update(Level.BOT_INSTANCE, w.iA1d.id,
                           {"channel": {"listen_mode": "specific_channels", "channel_ids": ["555555555555555551"]}},
                           actor_id="u"))
        w.t.store.collections["bot_instances"][other.id]["channel"]["listen_mode"] = "specific_channels"
        w.t.store.collections["bot_instances"][other.id]["channel"]["channel_ids"] = ["555555555555555552"]
        run(w.t.svc.bind_discord_guild(other.id, GUILD, application_id="910000000000000001", actor_id="u"))
        calls = []
        job = w.job(Level.BOT_INSTANCE, w.iA1d)
        job = run(dl.run_job(w.t.store, job["id"], clock=clock, executors=ok_executors(calls)))
        leave = next(a for a in job["external_actions"] if a["action"] == "discord_leave_guild")
        self.assertEqual((leave["state"], leave["resolution"]["reason"]), ("not_required", "guild_in_use"))
        self.assertNotIn(f"discord_leave_guild:910000000000000001:{GUILD}", calls)        # the shared guild is never left
        self.assertEqual(job["status"], "completed")
        # deleting the last instance on that guild does call leave-guild
        w.begin(Level.BOT_INSTANCE, other.id)
        pv = run(dl.preview(w.t.store, Level.BOT_INSTANCE, other.id, w.eB.id))
        job2, _ = run(dl.create_job(w.t.store, level="bot_instance", target_id=other.id, enterprise_id=w.eB.id,
                                    target_name="o", actor=w.root, now=NOW, external=pv["external_actions"],
                                    counts=pv["counts"]))
        job2 = run(dl.run_job(w.t.store, job2["id"], clock=clock, executors=ok_executors(calls)))
        self.assertIn(f"discord_leave_guild:910000000000000001:{GUILD}", calls)
        self.assertEqual(job2["status"], "completed")


class UserDeletionTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.store = self.w.t.store

    def test_last_owner_is_blocked_and_shared_resources_stay(self):
        uid = self.w.users["ownerA"]
        run(self.store.insert(USERS, {"id": uid, "email": "ownera@example.test", "name": "o",
                                      "is_platform_superadmin": False, "token_version": 0, "status": "active",
                                      "version": 1}))
        pv = run(dl.preview_user(self.store, uid))
        self.assertEqual([b["reason"] for b in pv["blocked"]], ["last_owner"])
        job = run(dl.delete_user(self.store, uid, actor=self.w.root, clock=clock))
        self.assertEqual(job["status"], "failed")
        self.assertIsNotNone(run(get_user(self.store, uid)))
        # with a second owner the deletion succeeds and the Enterprise keeps its data
        echain = self.w.t.chain(Level.ENTERPRISE, self.w.eA.id)
        run(ms.mutate(self.store, self.w.eA.id, ms.op_grant(self.w.root, echain, user_id=new_id(),
                                                            role=Role.OWNER, now=NOW, bypass_authority=True)))
        run(self.store.delete("deletion_jobs", f"user:{uid}"))
        job = run(dl.delete_user(self.store, uid, actor=self.w.root, clock=clock))
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(run(get_user(self.store, uid)))
        self.assertFalse(run(ms.load(self.store, self.w.eA.id)).for_user(uid))
        self.assertEqual(self.w.count("bots", enterprise_id=self.w.eA.id), 2)

    def test_last_superadmin_is_blocked(self):
        u = run(create_user(self.store, email="root@example.test", name="r", now=NOW))
        run(self.store.update(USERS, u.id, {"is_platform_superadmin": True}))
        run(ensure_superadmin_guard(self.store))
        pv = run(dl.preview_user(self.store, u.id))
        self.assertIn("last_superadmin", [b["reason"] for b in pv["blocked"]])


if __name__ == "__main__":
    unittest.main()
