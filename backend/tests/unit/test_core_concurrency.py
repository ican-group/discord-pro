"""Concurrency invariants of the Spec 012 core (Phase 3), on the in-memory
store. Every store operation yields to the event loop, so concurrent
coroutines interleave between every read and write — the same race windows a
real database has. The same scenarios run against a real MongoDB in
``tests/integration/test_spec012_mongo.py``.
"""
import asyncio
import unittest

from tests.unit._core_fixtures import Tree, run

from core import bindings as bnd
from core import memberships as ms
from core.authz import Actor, AuthzError, Role
from core.hierarchy import (ConcurrentModification, HierarchyError, InMemoryStore, Level, LifecycleAction,
                            NotFound, new_id)
from core.identity import (USERS, create_user, ensure_superadmin_guard, get_user, grant_superadmin,
                           revoke_superadmin)
from core.lifecycle import Authority, LifecycleError

GUILD = "222222222222222222"
CH1, CH2 = "333333333333333331", "333333333333333332"
N = 25


async def gather_outcomes(coros):
    results = await asyncio.gather(*coros, return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    unexpected = [e for e in errors if not isinstance(e, (HierarchyError, AuthzError, LifecycleError))]
    if unexpected:
        raise unexpected[0]
    return ok, errors


class BindingConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.bots = []
        for _ in range(3):                                # three tenants
            e = self.t.enterprise()
            o = self.t.organisation(e.id)
            self.bots.append(self.t.bot(o.id))

    def discord_instances(self, n, **channel):
        out = []
        for i in range(n):
            out.append(self.t.instance(self.bots[i % 3].id, {"type": "discord", **channel}))
        return out

    def test_concurrent_whole_guild_claims_bind_exactly_one_instance(self):
        insts = self.discord_instances(N)

        async def main():
            return await gather_outcomes([self.t.svc.bind_discord_guild(i.id, GUILD, application_id="910000000000000001", actor_id="u") for i in insts])
        ok, errors = run(main())
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errors), N - 1)
        holders = run(self.t.svc.bindings.holders(f"discord:910000000000000001:{GUILD}"))
        self.assertEqual(list(holders), [ok[0].id])

    def test_concurrent_whole_guild_and_channel_claims_never_overlap(self):
        whole = self.discord_instances(10)
        chans = self.discord_instances(10, listen_mode="specific_channels", channel_ids=[CH1])
        chans2 = self.discord_instances(10, listen_mode="specific_channels", channel_ids=[CH2])

        async def main():
            coros = [self.t.svc.bind_discord_guild(i.id, GUILD, application_id="910000000000000001", actor_id="u") for i in whole + chans + chans2]
            return await gather_outcomes(coros)
        ok, _ = run(main())
        holders = run(self.t.svc.bindings.holders(f"discord:910000000000000001:{GUILD}"))
        scopes = [tuple(h["scopes"]) for h in holders.values()]
        # either exactly one whole-guild holder, or at most one holder per channel
        if ("*",) in scopes:
            self.assertEqual(scopes, [("*",)])
        else:
            self.assertLessEqual(sorted(scopes), [(CH1,), (CH2,)])
            self.assertEqual(len(scopes), len(set(scopes)))
        self.assertEqual(len(ok), len(holders))

    def test_concurrent_channel_rebinds_never_overlap(self):
        a, b = self.discord_instances(2, listen_mode="specific_channels", channel_ids=[CH1])[0], None
        a = run(self.t.svc.bind_discord_guild(a.id, GUILD, application_id="910000000000000001", actor_id="u"))
        b = self.discord_instances(1, listen_mode="specific_channels", channel_ids=[CH2])[0]
        b = run(self.t.svc.bind_discord_guild(b.id, GUILD, application_id="910000000000000001", actor_id="u"))

        async def main():                          # both try to move onto the other's channel at once
            return await gather_outcomes([
                self.t.svc.update(Level.BOT_INSTANCE, a.id, {"channel": {"channel_ids": [CH2]}}, actor_id="u"),
                self.t.svc.update(Level.BOT_INSTANCE, b.id, {"channel": {"channel_ids": [CH1]}}, actor_id="u")])
        run(main())
        holders = run(self.t.svc.bindings.holders(f"discord:910000000000000001:{GUILD}"))
        flat = [c for h in holders.values() for c in h["scopes"]]
        self.assertEqual(len(flat), len(set(flat)))           # no channel held twice
        # stored instances agree with the claims
        for inst in (a, b):
            cur = self.t.chain(Level.BOT_INSTANCE, inst.id).instance
            self.assertEqual(sorted(holders[inst.id]["scopes"]), sorted(cur.channel.channel_ids))

    def test_concurrent_telegram_claims_by_credential_and_by_bot_identity(self):
        cred = new_id()

        async def same_credential():
            return await gather_outcomes([
                self.t.svc.create_instance(bot_id=self.bots[i % 3].id, name="t", actor_id="u",
                                           channel={"type": "telegram", "credential_id": cred},
                                           telegram_bot_id="555") for i in range(N)])
        ok, _ = run(same_credential())
        self.assertEqual(len(ok), 1)

        async def same_bot_two_credentials():          # one token stored twice must not serve two instances
            return await gather_outcomes([
                self.t.svc.create_instance(bot_id=self.bots[i % 3].id, name="t", actor_id="u",
                                           channel={"type": "telegram", "credential_id": new_id()},
                                           telegram_bot_id="777") for i in range(N)])
        ok, _ = run(same_bot_two_credentials())
        self.assertEqual(len(ok), 1)

    def test_failed_instance_insert_releases_the_claim(self):
        cred = new_id()
        store = self.t.store
        original = store.insert

        async def failing_insert(collection, doc):
            if collection == "bot_instances":
                raise HierarchyError("simulated failure")
            return await original(collection, doc)
        store.insert = failing_insert
        with self.assertRaises(HierarchyError):
            run(self.t.svc.create_instance(bot_id=self.bots[0].id, name="t", actor_id="u",
                                           channel={"type": "telegram", "credential_id": cred}))
        store.insert = original
        self.assertEqual(run(self.t.svc.bindings.holders(f"telegram:credential:{cred}")), {})
        self.t.instance(self.bots[1].id, {"type": "telegram", "credential_id": cred})   # reusable

    def test_reconcile_removes_only_stale_claims(self):
        from datetime import datetime, timedelta, timezone
        reg = self.t.svc.bindings
        ghost = new_id()
        run(reg.claim(ghost, new_id(), {"webchat:pk_ghostghostghostghostghost": ("*",)}))

        async def expected(iid):
            return {}
        claimed = datetime.fromisoformat("2026-11-18T00:00:00+00:00")      # the Tree clock
        self.assertEqual(run(reg.reconcile(expected, now=claimed + timedelta(seconds=1))), 0)   # grace period
        later = claimed + timedelta(seconds=bnd.RECONCILE_GRACE_SECONDS + 5)
        self.assertEqual(run(reg.reconcile(expected, now=later)), 1)
        self.assertEqual(run(reg.holders("webchat:pk_ghostghostghostghostghost")), {})


class MembershipConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.e = self.t.enterprise()
        self.chain = self.t.chain(Level.ENTERPRISE, self.e.id)
        self.root = Actor(new_id(), is_platform_superadmin=True)
        self.owners = [new_id(), new_id()]
        for u in self.owners:
            run(ms.mutate(self.t.store, self.e.id, ms.op_grant(self.root, self.chain, user_id=u,
                                                               role=Role.OWNER, now="t", bypass_authority=True)))

    def test_two_owners_removing_each_other_concurrently_leaves_one(self):
        for _ in range(10):
            a, b = self.owners

            async def main():
                return await gather_outcomes([
                    ms.mutate(self.t.store, self.e.id, ms.op_revoke(Actor(a), self.chain, user_id=b)),
                    ms.mutate(self.t.store, self.e.id, ms.op_revoke(Actor(b), self.chain, user_id=a))])
            ok, errors = run(main())
            self.assertEqual((len(ok), len(errors)), (1, 1))
            owners = run(ms.load(self.t.store, self.e.id)).owners()
            self.assertEqual(len(owners), 1)
            new = new_id()                                # restore two owners for the next round
            run(ms.mutate(self.t.store, self.e.id, ms.op_grant(self.root, self.chain, user_id=new,
                                                               role=Role.OWNER, now="t", bypass_authority=True)))
            self.owners = owners + [new]

    def test_concurrent_self_demotions_keep_an_owner(self):
        a, b = self.owners

        async def main():
            return await gather_outcomes([
                ms.mutate(self.t.store, self.e.id, ms.op_grant(Actor(u), self.chain, user_id=u,
                                                               role=Role.ADMIN, now="t")) for u in (a, b)])
        ok, errors = run(main())
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(run(ms.load(self.t.store, self.e.id)).owners()), 1)

    def test_concurrent_duplicate_grants_produce_one_membership(self):
        u = new_id()

        async def main():
            return await gather_outcomes([
                ms.mutate(self.t.store, self.e.id, ms.op_grant(self.root, self.chain, user_id=u, role=Role.VIEWER,
                                                               now="t", bypass_authority=True)) for _ in range(N)])
        ok, _ = run(main())
        self.assertEqual(len(ok), N)                       # idempotent, no errors
        roster = run(ms.load(self.t.store, self.e.id))
        self.assertEqual(len(roster.for_user(u)), 1)

    def test_org_grant_racing_enterprise_removal_never_leaves_a_dangling_member(self):
        o = self.t.organisation(self.e.id)
        ochain = self.t.chain(Level.ORGANISATION, o.id)
        for _ in range(10):
            u = new_id()
            run(ms.mutate(self.t.store, self.e.id, ms.op_grant(self.root, self.chain, user_id=u,
                                                               role=Role.VIEWER, now="t", bypass_authority=True)))

            async def main():
                return await gather_outcomes([
                    ms.mutate(self.t.store, self.e.id, ms.op_grant(Actor(self.owners[0]), ochain, user_id=u,
                                                                   role=Role.EDITOR, now="t")),
                    ms.mutate(self.t.store, self.e.id, ms.op_revoke(Actor(self.owners[0]), self.chain, user_id=u))])
            run(main())
            mine = run(ms.load(self.t.store, self.e.id)).for_user(u)
            has_ent = any(m.scope_level is Level.ENTERPRISE for m in mine)
            self.assertTrue(has_ent or not mine, "organisation membership without Enterprise membership")


class SuperadminConcurrencyTests(unittest.TestCase):
    def test_concurrent_demotions_never_remove_the_last_superadmin(self):
        async def main():
            store = InMemoryStore()
            ids = []
            for i in range(5):
                u = await create_user(store, email=f"s{i}@example.test", name="s", now="t")
                await store.update(USERS, u.id, {"is_platform_superadmin": True})
                ids.append(u.id)
            self.assertEqual(await ensure_superadmin_guard(store), 5)
            root = Actor(ids[0], is_platform_superadmin=True)
            ok, errors = await gather_outcomes([revoke_superadmin(store, root, u) for u in ids])
            remaining = [u for u in ids if (await get_user(store, u)).is_platform_superadmin]
            guard = (await store.get("platform_state", "superadmins"))["count"]
            return ok, errors, remaining, guard
        ok, errors, remaining, guard = run(main())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(len(errors), 1)
        self.assertLessEqual(guard, len(remaining))              # lower bound holds

    def test_promotion_and_demotion_interleaving_keeps_lower_bound(self):
        async def main():
            store = InMemoryStore()
            a = await create_user(store, email="a@example.test", name="a", now="t")
            b = await create_user(store, email="b@example.test", name="b", now="t")
            await store.update(USERS, a.id, {"is_platform_superadmin": True})
            await ensure_superadmin_guard(store)
            root = Actor(a.id, is_platform_superadmin=True)
            await gather_outcomes([grant_superadmin(store, root, b.id), revoke_superadmin(store, root, a.id)])
            actual = await store.count(USERS, {"is_platform_superadmin": True})
            guard = (await store.get("platform_state", "superadmins"))["count"]
            return actual, guard
        actual, guard = run(main())
        self.assertGreaterEqual(actual, 1)
        self.assertLessEqual(guard, actual)

    def test_guard_must_exist(self):
        async def main():
            store = InMemoryStore()
            a = await create_user(store, email="a@example.test", name="a", now="t")
            await store.update(USERS, a.id, {"is_platform_superadmin": True})
            with self.assertRaises(AuthzError):
                await revoke_superadmin(store, Actor(a.id, True), a.id)
        run(main())


class LifecycleAndDeletionConcurrencyTests(unittest.TestCase):
    def test_concurrent_transitions_on_one_version_apply_once(self):
        t = Tree()
        c = t.full()
        bot = c.bot

        async def main():
            return await gather_outcomes([
                t.svc.change_lifecycle(Level.BOT, bot.id, LifecycleAction.SUSPEND, authority=Authority.OBJECT,
                                       actor_id="u", expect_version=bot.version),
                t.svc.change_lifecycle(Level.BOT, bot.id, LifecycleAction.ARCHIVE, authority=Authority.OBJECT,
                                       actor_id="u", expect_version=bot.version)])
        ok, errors = run(main())
        self.assertEqual((len(ok), len(errors)), (1, 1))
        self.assertIsInstance(errors[0], (ConcurrentModification, LifecycleError))

    def test_child_created_during_parent_deletion_is_removed(self):
        t = Tree()
        e = t.enterprise()
        o = t.organisation(e.id)

        async def main():
            return await gather_outcomes(
                [t.svc.create_bot(organisation_id=o.id, name=f"b{i}", actor_id="u") for i in range(10)] +
                [t.svc.change_lifecycle(Level.ORGANISATION, o.id, LifecycleAction.BEGIN_DELETE,
                                        authority=Authority.ENTERPRISE, actor_id="u")])
        run(main())
        survivors = run(t.store.find("bots", {"organisation_id": o.id}))
        # every surviving bot was created before the deletion began; the job sweeps them
        self.assertTrue(all(b["created_at"] <= t.chain(Level.ORGANISATION, o.id).organisation.lifecycle.changed_at
                            for b in survivors))
        with self.assertRaises(NotFound):
            run(t.svc.create_bot(organisation_id=o.id, name="late", actor_id="u"))

    def test_confirmation_token_is_single_use_under_concurrency(self):
        from core import deletion as dl

        async def main():
            store = InMemoryStore()
            token, _ = await dl.issue_token(store, actor_id="a", level="bot", target_id="t", digest="d",
                                            now="2026-11-18T00:00:00+00:00")
            return await asyncio.gather(*[dl.consume_token(store, token, actor_id="a", level="bot", target_id="t",
                                                           now="2026-11-18T00:00:01+00:00") for _ in range(N)],
                                        return_exceptions=True)
        results = run(main())
        self.assertEqual(sum(1 for r in results if isinstance(r, dict)), 1)


if __name__ == "__main__":
    unittest.main()


class PermissionRevocationConcurrencyTests(unittest.TestCase):
    """X4: a membership change cannot take effect on authority that was revoked
    concurrently. Every change is ordered by the Enterprise guard; a change
    that succeeded must be ordered before any revocation of its actor."""

    def setUp(self):
        self.t = Tree()
        self.e = self.t.enterprise()
        self.o = self.t.organisation(self.e.id)
        self.chain = self.t.chain(Level.ENTERPRISE, self.e.id)
        self.ochain = self.t.chain(Level.ORGANISATION, self.o.id)
        self.root = Actor(new_id(), is_platform_superadmin=True)
        self.owner = new_id()
        self.grant(self.owner, Role.OWNER)

    def grant(self, uid, role, chain=None, admit=False):
        return run(ms.mutate(self.t.store, self.e.id, ms.op_grant(self.root, chain or self.chain, user_id=uid,
                                                                  role=role, now="t", bypass_authority=True,
                                                                  admit=admit)))

    def test_grant_racing_demotion_of_the_granter_is_linearizable(self):
        outcomes = set()
        for i in range(16):
            admin, target = new_id(), new_id()
            self.grant(admin, Role.ADMIN)
            self.grant(target, Role.VIEWER)

            async def main():
                grant = ms.mutate(self.t.store, self.e.id, ms.op_grant(Actor(admin), self.chain, user_id=target,
                                                                       role=Role.ADMIN, now="t"))
                demote = ms.mutate(self.t.store, self.e.id, ms.op_grant(Actor(self.owner), self.chain, user_id=admin,
                                                                        role=Role.VIEWER, now="t"))
                if i % 2:                                              # vary which request starts first
                    d, g = await asyncio.gather(demote, grant, return_exceptions=True)
                    return g, d
                return tuple(await asyncio.gather(grant, demote, return_exceptions=True))
            granted, demoted = run(main())
            outcomes.add(isinstance(granted, BaseException))
            self.assertNotIsInstance(demoted, BaseException)
            if isinstance(granted, BaseException):
                self.assertIsInstance(granted, AuthzError)             # refused on the fresh snapshot
                role = run(ms.load_for(self.t.store, self.e.id, [target])).for_user(target)[0].role
                self.assertEqual(role, Role.VIEWER)
            else:
                self.assertLess(granted.seq, demoted.seq)              # ordered before the revocation
        self.assertEqual(outcomes, {True, False})                     # both interleavings were exercised

    def test_delegation_change_racing_an_organisation_admin_grant_is_linearizable(self):
        for _ in range(10):
            org_admin, target = new_id(), new_id()
            self.grant(org_admin, Role.ADMIN, self.ochain, admit=True)
            self.grant(target, Role.VIEWER)
            ent = self.t.chain(Level.ENTERPRISE, self.e.id).enterprise
            policy = {**ent.policy, "delegation.org_admins_manage_members": False}

            async def main():
                return await asyncio.gather(
                    ms.mutate(self.t.store, self.e.id, ms.op_grant(Actor(org_admin), self.ochain, user_id=target,
                                                                   role=Role.EDITOR, now="t")),
                    ms.mutate(self.t.store, self.e.id, ms.op_set_enterprise_policy(
                        Actor(self.owner), self.chain, new_policy=policy, expect_version=ent.version, now="t")),
                    return_exceptions=True)
            granted, changed = run(main())
            self.assertNotIsInstance(changed, BaseException)
            if not isinstance(granted, BaseException):
                self.assertLess(granted.seq, changed.seq)
            else:
                self.assertIsInstance(granted, AuthzError)
            # reset delegation for the next round
            ent = self.t.chain(Level.ENTERPRISE, self.e.id).enterprise
            run(ms.mutate(self.t.store, self.e.id, ms.op_set_enterprise_policy(
                Actor(self.owner), self.chain, new_policy={}, expect_version=ent.version, now="t")))

    def _commit_without_resolving(self, op):
        """Simulate a requester that crashes right after committing its intent."""
        original = ms._finish

        async def crash(store, eid, intent_id):
            return ms.Result(intent_id, None)
        ms._finish = crash
        try:
            run(ms.mutate(self.t.store, self.e.id, op))
        finally:
            ms._finish = original
        guard = run(self.t.store.get(ms.GUARDS, self.e.id))
        self.assertEqual(guard["pending"]["state"], "prepared")
        return guard["pending"]["id"]

    def test_superadmin_authority_revoked_before_verification_aborts_the_change(self):
        async def setup():
            sa = await create_user(self.t.store, email="sa@example.test", name="sa", now="t")
            await self.t.store.update(USERS, sa.id, {"is_platform_superadmin": True})
            return sa
        sa = run(setup())
        target = new_id()
        intent = self._commit_without_resolving(
            ms.op_grant(Actor(sa.id, is_platform_superadmin=True), self.chain, user_id=target, role=Role.OWNER, now="t"))
        self.assertEqual(run(ms.owner_count(self.t.store, self.e.id)), 2)        # provisional
        run(self.t.store.update(USERS, sa.id, {"is_platform_superadmin": False}))   # revoked meanwhile
        from datetime import datetime, timedelta, timezone
        self.assertTrue(run(ms.resolve_stale(self.t.store, self.e.id,
                                             now=datetime.now(timezone.utc) + timedelta(minutes=1))))
        guard = run(self.t.store.get(ms.GUARDS, self.e.id))
        self.assertIsNone(guard["pending"])
        self.assertEqual([r["outcome"] for r in guard["recent"] if r["id"] == intent], ["aborted:authority"])
        self.assertEqual(run(ms.load_for(self.t.store, self.e.id, [target])).members, ())
        self.assertEqual(guard["owner_count"], 1)                                 # restored exactly

    def test_disabled_actor_cannot_complete_a_change(self):
        async def setup():
            u = await create_user(self.t.store, email="adm@example.test", name="a", now="t")
            return u
        adm = run(setup())
        self.grant(adm.id, Role.ADMIN)
        target = new_id()
        self.grant(target, Role.VIEWER)
        self._commit_without_resolving(ms.op_grant(Actor(adm.id), self.chain, user_id=target, role=Role.EDITOR,
                                                   now="t"))
        run(self.t.store.update(USERS, adm.id, {"status": "disabled"}))
        # the next change in this Enterprise resolves the leftover intent first (and aborts it)
        self.grant(new_id(), Role.VIEWER)
        self.assertEqual(run(ms.load_for(self.t.store, self.e.id, [target])).for_user(target)[0].role, Role.VIEWER)

    def test_leftover_intent_is_rolled_forward_before_the_next_change(self):
        target = new_id()
        self.grant(target, Role.VIEWER)
        self._commit_without_resolving(ms.op_grant(Actor(self.owner), self.chain, user_id=target, role=Role.EDITOR,
                                                   now="t"))
        other = new_id()
        result = self.grant(other, Role.VIEWER)
        self.assertEqual(run(ms.load_for(self.t.store, self.e.id, [target])).for_user(target)[0].role, Role.EDITOR)
        guard = run(self.t.store.get(ms.GUARDS, self.e.id))
        applied = [r for r in guard["recent"] if r["outcome"] == "applied"]
        self.assertEqual(applied[-1]["seq"], result.seq)
        self.assertGreater(result.seq, applied[-2]["seq"])                      # leftover applied first

    def test_owner_count_stays_exact_under_concurrent_churn(self):
        users = [new_id() for _ in range(6)]
        for u in users:
            self.grant(u, Role.ADMIN)

        async def main():
            ops = []
            for i, u in enumerate(users):
                role = Role.OWNER if i % 2 == 0 else Role.VIEWER
                ops.append(ms.mutate(self.t.store, self.e.id, ms.op_grant(Actor(self.owner), self.chain, user_id=u,
                                                                          role=role, now="t")))
                ops.append(ms.mutate(self.t.store, self.e.id, ms.op_revoke(Actor(self.owner), self.chain,
                                                                           user_id=users[(i + 1) % len(users)])))
            return await asyncio.gather(*ops, return_exceptions=True)
        run(main())
        roster = run(ms.load(self.t.store, self.e.id))
        self.assertEqual(run(ms.owner_count(self.t.store, self.e.id)), len(roster.owners()))
        self.assertGreaterEqual(len(roster.owners()), 1)

    def test_one_document_per_enterprise_user(self):
        u = new_id()
        self.grant(u, Role.VIEWER)
        self.grant(u, Role.EDITOR, self.ochain)
        docs = run(self.t.store.find(ms.COLLECTION, {"enterprise_id": self.e.id, "user_id": u}))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], f"{self.e.id}:{u}")
        self.assertEqual([g["scope_id"] for g in docs[0]["grants"]], [self.o.id])
