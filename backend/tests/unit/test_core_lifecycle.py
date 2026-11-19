"""Spec 012: lifecycle states, suspension authority and hierarchy availability."""
import unittest

from tests.unit._core_fixtures import Tree, run, with_state
from core.hierarchy import Level
from core.lifecycle import (Authority, Lifecycle, LifecycleAction as A, LifecycleError, LifecycleState as S,
                            apply_action, can_create_child_under, is_writable)

NOW = "2026-11-18T00:00:00+00:00"


def act(lc, action, authority=Authority.OBJECT):
    return apply_action(lc, action, authority=authority, actor_id="u", now=NOW)


class TransitionTests(unittest.TestCase):
    def test_suspend_and_resume_by_same_authority(self):
        lc = act(Lifecycle(), A.SUSPEND)
        self.assertEqual((lc.state, lc.authority), (S.SUSPENDED, Authority.OBJECT))
        self.assertEqual(act(lc, A.RESUME).state, S.ACTIVE)

    def test_lower_authority_cannot_lift_higher_suspension(self):
        for imposed in (Authority.ENTERPRISE, Authority.PLATFORM):
            lc = act(Lifecycle(), A.SUSPEND, imposed)
            with self.assertRaises(LifecycleError):
                act(lc, A.RESUME, Authority.OBJECT)
            with self.assertRaises(LifecycleError):
                act(lc, A.ARCHIVE, Authority.OBJECT)
            self.assertEqual(act(lc, A.RESUME, imposed).state, S.ACTIVE)
        lc = act(Lifecycle(), A.SUSPEND, Authority.ENTERPRISE)
        self.assertEqual(act(lc, A.RESUME, Authority.PLATFORM).state, S.ACTIVE)

    def test_suspension_can_be_escalated(self):
        lc = act(act(Lifecycle(), A.SUSPEND), A.SUSPEND, Authority.PLATFORM)
        self.assertEqual(lc.authority, Authority.PLATFORM)
        with self.assertRaises(LifecycleError):
            act(lc, A.SUSPEND, Authority.OBJECT)

    def test_archive_and_unarchive(self):
        lc = act(Lifecycle(), A.ARCHIVE)
        self.assertEqual(lc.state, S.ARCHIVED)
        with self.assertRaises(LifecycleError):
            act(lc, A.SUSPEND)
        self.assertEqual(act(lc, A.UNARCHIVE).state, S.ACTIVE)

    def test_deleted_is_terminal(self):
        lc = act(Lifecycle(), A.BEGIN_DELETE)
        for action in A:
            with self.assertRaises(LifecycleError):
                act(lc, action, Authority.PLATFORM)

    def test_invalid_transitions(self):
        with self.assertRaises(LifecycleError):
            act(Lifecycle(), A.RESUME)
        with self.assertRaises(LifecycleError):
            act(Lifecycle(), A.UNARCHIVE)

    def test_writability_and_child_creation(self):
        self.assertTrue(is_writable(Lifecycle()))
        self.assertTrue(is_writable(act(Lifecycle(), A.SUSPEND)))
        self.assertFalse(is_writable(act(Lifecycle(), A.ARCHIVE)))
        self.assertFalse(is_writable(act(Lifecycle(), A.BEGIN_DELETE)))
        self.assertTrue(can_create_child_under(Lifecycle()))
        self.assertTrue(can_create_child_under(act(Lifecycle(), A.SUSPEND)))
        self.assertFalse(can_create_child_under(act(Lifecycle(), A.SUSPEND, Authority.ENTERPRISE)))
        self.assertFalse(can_create_child_under(act(Lifecycle(), A.ARCHIVE)))

    def test_round_trip(self):
        lc = act(Lifecycle(), A.SUSPEND, Authority.ENTERPRISE)
        self.assertEqual(Lifecycle.from_doc(lc.to_doc()), lc)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.chain = self.t.full()

    def test_all_active_is_available(self):
        self.assertTrue(self.chain.availability().available)

    def test_any_inactive_ancestor_blocks_every_descendant(self):
        for attr in ("enterprise", "organisation", "bot", "instance"):
            for state_lc in (act(Lifecycle(), A.SUSPEND), act(Lifecycle(), A.ARCHIVE), act(Lifecycle(), A.BEGIN_DELETE)):
                with self.subTest(attr=attr, state=state_lc.state):
                    av = with_state(self.chain, attr, state_lc).availability()
                    self.assertFalse(av.available)
                    self.assertEqual(av.blocking_level, getattr(self.chain, attr).level.value)
                    self.assertEqual(av.blocking_state, state_lc.state)

    def test_highest_blocking_ancestor_is_reported(self):
        c = with_state(self.chain, "bot", act(Lifecycle(), A.ARCHIVE))
        c = with_state(c, "enterprise", act(Lifecycle(), A.SUSPEND, Authority.PLATFORM))
        self.assertEqual(c.availability().blocking_level, "enterprise")

    def test_parent_resume_keeps_child_state(self):
        """Lifecycle is stored per object: resuming the parent never touches the child."""
        t = self.t
        org = self.chain.organisation
        bot = self.chain.bot
        run(t.svc.change_lifecycle(Level.BOT, bot.id, A.SUSPEND, authority=Authority.OBJECT, actor_id="u"))
        run(t.svc.change_lifecycle(Level.ORGANISATION, org.id, A.SUSPEND, authority=Authority.OBJECT, actor_id="u"))
        run(t.svc.change_lifecycle(Level.ORGANISATION, org.id, A.RESUME, authority=Authority.OBJECT, actor_id="u"))
        chain = t.chain(Level.BOT_INSTANCE, self.chain.instance.id)
        self.assertEqual(chain.organisation.lifecycle.state, S.ACTIVE)
        self.assertEqual(chain.bot.lifecycle.state, S.SUSPENDED)
        self.assertFalse(chain.availability().available)
        self.assertEqual(chain.availability().blocking_level, "bot")

    def test_availability_is_not_copied_into_children(self):
        t = self.t
        run(t.svc.change_lifecycle(Level.ENTERPRISE, self.chain.enterprise.id, A.SUSPEND,
                                   authority=Authority.PLATFORM, actor_id="u"))
        for level, obj in ((Level.ORGANISATION, self.chain.organisation), (Level.BOT, self.chain.bot),
                           (Level.BOT_INSTANCE, self.chain.instance)):
            stored = run(t.store.get({"organisation": "organisations", "bot": "bots",
                                      "bot_instance": "bot_instances"}[level.value], obj.id))
            self.assertEqual(stored["lifecycle"]["state"], "active")
        self.assertFalse(t.chain(Level.BOT_INSTANCE, self.chain.instance.id).availability().available)


if __name__ == "__main__":
    unittest.main()
