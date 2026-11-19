"""X13 (approved 2026-11-18): Enterprise knowledge-source approval is deny by
default. A source can be approved or used anywhere in an Enterprise only if
the Enterprise's ``knowledge.allowed_sources`` policy lists it; descendants
may narrow and never broaden; unset and explicit-empty both mean none."""
import unittest

from tests.unit._core_fixtures import Tree, with_overrides
from core.config_registry import ConfigError
from core.hierarchy import Chain, new_id
from core.knowledge import (Decision, Evidence, KnowledgeSource, Requester, RetrievalResult, RetrievalStatus,
                            SourceKind, authorised_sources, gate_before_generation, plan_for)
from core.resolver import apply_override_patch, resolve, validate_policy_write

STRICT = "strict_knowledge_only"


class EnterpriseSourceApprovalTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()
        self.a = KnowledgeSource(new_id(), self.c.enterprise_id, self.c.organisation.id, self.c.bot.id,
                                 SourceKind.DOCUMENT)
        self.b = KnowledgeSource(new_id(), self.c.enterprise_id, self.c.organisation.id, None, SourceKind.DOCUMENT)
        other = self.t.full()
        self.foreign = KnowledgeSource(new_id(), other.enterprise_id, None, None, SourceKind.DOCUMENT)
        self.catalog = [self.a, self.b, self.foreign]

    def levels(self, chain):
        return {"organisation": Chain(chain.enterprise, chain.organisation),
                "bot": Chain(chain.enterprise, chain.organisation, chain.bot),
                "instance": chain}

    def with_enterprise_policy(self, allowed):
        policy = {} if allowed is None else {"knowledge.allowed_sources": allowed}
        return with_overrides(self.c, enterprise={"policy": policy})

    def strict_refuses(self, chain):
        plan = plan_for(resolve(chain))
        scope = authorised_sources(plan, chain, self.catalog, Requester())
        ev = Evidence(new_id(), self.a.id, self.c.enterprise_id, 0.95, "text")
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (ev,)), authorised=scope, chain=chain)
        return plan, scope, gate

    def test_absent_or_empty_enterprise_approval_means_nothing_is_approved(self):
        for allowed in (None, []):
            chain = self.with_enterprise_policy(allowed)
            with self.subTest(enterprise_policy=allowed):
                for name, target in self.levels(chain).items():
                    with self.subTest(level=name):
                        with self.assertRaises(ConfigError):
                            apply_override_patch(target, {"knowledge.approved_sources": [self.a.id]})
                stored = with_overrides(chain, bot={"overrides": {"knowledge.mode": STRICT,
                                                                  "knowledge.approved_sources": [self.a.id]}})
                plan, scope, gate = self.strict_refuses(stored)
                self.assertEqual(plan.approved_sources, frozenset())
                self.assertEqual(scope, frozenset())
                self.assertEqual(gate.decision, Decision.REFUSE)          # strict fails closed

    def test_organisation_policy_cannot_add_sources(self):
        for allowed in (None, [self.a.id]):
            chain = self.with_enterprise_policy(allowed)
            with self.subTest(enterprise_policy=allowed):
                with self.assertRaises(ConfigError):
                    validate_policy_write(Chain(chain.enterprise, chain.organisation), "knowledge.allowed_sources",
                                          [self.b.id])
        chain = self.with_enterprise_policy([self.a.id, self.b.id])
        self.assertEqual(validate_policy_write(Chain(chain.enterprise, chain.organisation),
                                               "knowledge.allowed_sources", [self.a.id]), frozenset({self.a.id}))

    def test_descendants_cannot_add_unapproved_sources(self):
        chain = self.with_enterprise_policy([self.a.id])
        for name, target in self.levels(chain).items():
            with self.subTest(level=name):
                with self.assertRaises(ConfigError):
                    apply_override_patch(target, {"knowledge.approved_sources": [self.b.id]})
                with self.assertRaises(ConfigError):
                    apply_override_patch(target, {"knowledge.approved_sources": [self.a.id, self.b.id]})

    def test_valid_narrowing(self):
        chain = self.with_enterprise_policy([self.a.id, self.b.id])
        org = apply_override_patch(self.levels(chain)["organisation"], {"knowledge.approved_sources": [self.a.id,
                                                                                                        self.b.id]})
        chain = with_overrides(chain, organisation={"overrides": org})
        bot = apply_override_patch(self.levels(chain)["bot"], {"knowledge.approved_sources": [self.a.id]})
        chain = with_overrides(chain, bot={"overrides": {**bot, "knowledge.mode": STRICT}})
        with self.assertRaises(ConfigError):                       # instance cannot re-broaden past the bot
            apply_override_patch(chain, {"knowledge.approved_sources": [self.a.id, self.b.id]})
        plan = plan_for(resolve(chain))
        self.assertEqual(plan.approved_sources, frozenset({self.a.id}))
        self.assertEqual(authorised_sources(plan, chain, self.catalog, Requester()), frozenset({self.a.id}))

    def test_source_removed_at_enterprise_level_stops_being_usable(self):
        chain = self.with_enterprise_policy([self.a.id, self.b.id])
        chain = with_overrides(chain, bot={"overrides": {"knowledge.mode": STRICT,
                                                         "knowledge.approved_sources": [self.a.id, self.b.id]}})
        self.assertEqual(plan_for(resolve(chain)).approved_sources, frozenset({self.a.id, self.b.id}))
        tightened = with_overrides(chain, enterprise={"policy": {"knowledge.allowed_sources": [self.a.id]}})
        eff = resolve(tightened)
        plan = plan_for(eff)
        self.assertNotIn(self.b.id, plan.approved_sources)
        self.assertTrue(any(c.key == "knowledge.approved_sources" for c in eff.conflicts))   # reported

    def test_stored_invalid_overrides_are_ignored(self):
        chain = self.with_enterprise_policy([self.a.id])
        for bad in ([self.b.id], ["not-a-uuid"], "everything", [self.a.id, self.b.id]):
            with self.subTest(stored=bad):
                stored = with_overrides(chain, bot={"overrides": {"knowledge.mode": STRICT,
                                                                  "knowledge.approved_sources": bad}})
                eff = resolve(stored)
                self.assertTrue(any(c.key == "knowledge.approved_sources" for c in eff.conflicts))
                self.assertNotIn(self.b.id, plan_for(eff).approved_sources)

    def test_cross_tenant_source_references_are_never_usable(self):
        chain = self.with_enterprise_policy([self.a.id, self.foreign.id])     # even if listed by mistake
        chain = with_overrides(chain, bot={"overrides": {"knowledge.mode": STRICT,
                                                         "knowledge.approved_sources": [self.a.id, self.foreign.id]}})
        plan = plan_for(resolve(chain))
        scope = authorised_sources(plan, chain, self.catalog, Requester())
        self.assertEqual(scope, frozenset({self.a.id}))
        ev = Evidence(new_id(), self.foreign.id, self.foreign.enterprise_id, 0.99, "text")
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (ev,)), authorised=scope, chain=chain)
        self.assertEqual(gate.decision, Decision.REFUSE)

    def test_general_mode_still_works_without_any_sources(self):
        chain = with_overrides(self.with_enterprise_policy(None), bot={"overrides": {"knowledge.mode": "general"}})
        plan = plan_for(resolve(chain))
        self.assertFalse(plan.strict)
        gate = gate_before_generation(plan, None, authorised=frozenset(), chain=chain)
        self.assertEqual(gate.decision, Decision.GENERAL)


if __name__ == "__main__":
    unittest.main()
