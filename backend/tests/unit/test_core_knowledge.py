"""Spec 012 §5: Enterprise AI knowledge control (configuration, policy and the
strict-mode fail-closed contract). Scenarios A-J from the specification."""
import unittest

from tests.unit._core_fixtures import Tree, with_overrides
from core.config_registry import ConfigError, KnowledgeAccessScope, KnowledgeMode
from core.hierarchy import Chain, new_id
from core.knowledge import (AnswerDraft, Claim, Decision, Evidence, KnowledgeSource, RefusalReason, Requester,
                            RetrievalResult, RetrievalStatus, SourceKind, authorised_sources, fail_closed,
                            gate_before_generation, plan_for, refusal, validate_answer)
from core.resolver import resolve, validate_override, validate_policy_write

STRICT = "strict_knowledge_only"
PREFERRED = "knowledge_preferred"
GENERAL = "general"


class Base(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()
        self.src_bot = KnowledgeSource(new_id(), self.c.enterprise_id, self.c.organisation.id, self.c.bot.id,
                                       SourceKind.DOCUMENT)
        self.src_org = KnowledgeSource(new_id(), self.c.enterprise_id, self.c.organisation.id, None,
                                       SourceKind.STRUCTURED)
        self.src_ent = KnowledgeSource(new_id(), self.c.enterprise_id, None, None, SourceKind.DOCUMENT)
        other = self.t.full()
        self.src_foreign = KnowledgeSource(new_id(), other.enterprise_id, None, None, SourceKind.DOCUMENT)
        self.catalog = [self.src_bot, self.src_org, self.src_ent, self.src_foreign]

    def chain_with(self, mode=STRICT, sources=None, ent_policy=None, **extra):
        approved = sources if sources is not None else [self.src_bot.id, self.src_org.id, self.src_ent.id,
                                                         self.src_foreign.id]
        # X13: sources are usable only if the Enterprise approves them (deny by default).
        # The foreign source is "approved" on purpose: runtime scoping must still exclude it.
        allowed = sorted({self.src_bot.id, self.src_org.id, self.src_ent.id, self.src_foreign.id, *approved})
        return with_overrides(self.c,
                              enterprise={"policy": {"knowledge.allowed_sources": allowed, **(ent_policy or {})}},
                              bot={"overrides": {"knowledge.mode": mode, "knowledge.approved_sources": approved,
                                                 **extra}})

    def plan_and_scope(self, chain, requester=Requester()):
        plan = plan_for(resolve(chain))
        return plan, authorised_sources(plan, chain, self.catalog, requester)

    def ev(self, source, score=0.9, eid=None, enterprise=None):
        return Evidence(eid or new_id(), source.id, enterprise or source.enterprise_id, score,
                        "Returns take 14 days from the delivery date.")


class KnowledgeScenarioTests(Base):
    def test_A_strict_with_sufficient_evidence_gives_grounded_cited_answer(self):
        chain = self.chain_with(STRICT)
        plan, scope = self.plan_and_scope(chain)
        e1 = self.ev(self.src_bot)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (e1,)), authorised=scope, chain=chain)
        self.assertEqual(gate.decision, Decision.GROUNDED)
        verdict = validate_answer(plan, gate, AnswerDraft((Claim("Returns take 14 days.", frozenset({e1.evidence_id}),
                                                                  quote="Returns take 14 days"),)))
        self.assertTrue(verdict.deliver)
        self.assertEqual(verdict.citations, (self.src_bot.id,))

    def test_B_strict_without_relevant_evidence_refuses_with_fallback(self):
        chain = self.chain_with(STRICT, **{"knowledge.insufficient_fallback": "Please contact support."})
        plan, scope = self.plan_and_scope(chain)
        for retrieval, reason in ((RetrievalResult(RetrievalStatus.OK, ()), RefusalReason.NO_EVIDENCE),
                                  (RetrievalResult(RetrievalStatus.OK, (self.ev(self.src_bot, score=0.1),)),
                                   RefusalReason.INSUFFICIENT_EVIDENCE)):
            with self.subTest(reason=reason):
                gate = gate_before_generation(plan, retrieval, authorised=scope, chain=chain)
                self.assertEqual((gate.decision, gate.reason), (Decision.REFUSE, reason))
                r = refusal(plan, gate.reason)
                self.assertEqual(r.text, "Please contact support.")
                self.assertFalse(r.deliver)

    def test_C_strict_with_knowledge_service_unavailable_fails_closed(self):
        chain = self.chain_with(STRICT)
        plan, scope = self.plan_and_scope(chain)
        for retrieval in (RetrievalResult(RetrievalStatus.UNAVAILABLE), None):
            gate = gate_before_generation(plan, retrieval, authorised=scope, chain=chain)
            self.assertEqual((gate.decision, gate.reason), (Decision.REFUSE, RefusalReason.KNOWLEDGE_UNAVAILABLE))
        verdict = fail_closed(plan, RuntimeError("vector store timeout: 10.0.0.5"))
        self.assertFalse(verdict.deliver)
        self.assertNotIn("10.0.0.5", verdict.text)

    def test_D_strict_with_conflicting_evidence_refuses(self):
        chain = self.chain_with(STRICT)
        plan, scope = self.plan_and_scope(chain)
        e1, e2 = self.ev(self.src_bot), self.ev(self.src_org)
        retrieval = RetrievalResult(RetrievalStatus.OK, (e1, e2), conflicting=frozenset({e1.evidence_id, e2.evidence_id}))
        gate = gate_before_generation(plan, retrieval, authorised=scope, chain=chain)
        self.assertEqual((gate.decision, gate.reason), (Decision.REFUSE, RefusalReason.CONFLICTING_EVIDENCE))

    def test_E_unauthorised_and_cross_tenant_sources_are_excluded(self):
        chain = self.chain_with(STRICT, sources=[self.src_bot.id])        # only the bot's own source approved
        plan, scope = self.plan_and_scope(chain)
        self.assertEqual(scope, frozenset({self.src_bot.id}))
        unapproved = self.ev(self.src_org)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (unapproved,)), authorised=scope, chain=chain)
        self.assertEqual(gate.decision, Decision.REFUSE)
        # evidence claiming an authorised source id but from another enterprise is dropped
        spoofed = self.ev(self.src_bot, enterprise=self.src_foreign.enterprise_id)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (spoofed,)), authorised=scope, chain=chain)
        self.assertEqual(gate.decision, Decision.REFUSE)

    def test_F_enterprise_strict_policy_rejects_general_override(self):
        chain = with_overrides(self.c, enterprise={"policy": {"knowledge.minimum_mode": STRICT}})
        bot_chain = Chain(chain.enterprise, chain.organisation, chain.bot)
        for mode in (GENERAL, PREFERRED):
            with self.subTest(mode=mode):
                with self.assertRaises(ConfigError):
                    validate_override(bot_chain, "knowledge.mode", mode)
                with self.assertRaises(ConfigError):
                    validate_override(chain, "knowledge.mode", mode)
        self.assertEqual(validate_override(bot_chain, "knowledge.mode", STRICT), KnowledgeMode.STRICT_KNOWLEDGE_ONLY)
        # a stored weaker override is ignored, reported, and strict applies
        stored = with_overrides(chain, bot={"overrides": {"knowledge.mode": GENERAL}})
        eff = resolve(stored)
        self.assertEqual(eff.values["knowledge.mode"], KnowledgeMode.STRICT_KNOWLEDGE_ONLY)
        self.assertTrue(any(c.key == "knowledge.mode" and c.level == "bot" for c in eff.conflicts))
        # an Organisation cannot relax the Enterprise minimum
        with self.assertRaises(ConfigError):
            validate_policy_write(Chain(chain.enterprise, chain.organisation), "knowledge.minimum_mode", GENERAL)

    def test_G_general_mode_answers_without_knowledge_base(self):
        chain = self.chain_with(GENERAL, sources=[])
        plan, scope = self.plan_and_scope(chain)
        for retrieval in (None, RetrievalResult(RetrievalStatus.UNAVAILABLE), RetrievalResult(RetrievalStatus.OK, ())):
            gate = gate_before_generation(plan, retrieval, authorised=scope, chain=chain)
            self.assertEqual(gate.decision, Decision.GENERAL)
        verdict = validate_answer(plan, gate, AnswerDraft(uncited_text="Hello! How can I help?"))
        self.assertTrue(verdict.deliver)
        self.assertEqual(verdict.text, "Hello! How can I help?")

    def test_H_preferred_falls_back_only_when_enterprise_permits(self):
        no_fallback = self.chain_with(PREFERRED)
        plan, scope = self.plan_and_scope(no_fallback)
        self.assertFalse(plan.general_fallback_allowed)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, ()), authorised=scope, chain=no_fallback)
        self.assertEqual(gate.decision, Decision.REFUSE)
        permitted = self.chain_with(PREFERRED, ent_policy={"knowledge.general_fallback_allowed": True})
        plan, scope = self.plan_and_scope(permitted)
        self.assertTrue(plan.general_fallback_allowed)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, ()), authorised=scope, chain=permitted)
        self.assertEqual(gate.decision, Decision.GENERAL)
        # with evidence available, preferred mode still grounds
        e1 = self.ev(self.src_bot)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (e1,)), authorised=scope, chain=permitted)
        self.assertEqual(gate.decision, Decision.GROUNDED)

    def test_I_unsupported_generated_claims_are_blocked(self):
        chain = self.chain_with(STRICT)
        plan, scope = self.plan_and_scope(chain)
        e1 = self.ev(self.src_bot)
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (e1,)), authorised=scope, chain=chain)
        drafts = {
            "uncited claim": AnswerDraft((Claim("Shipping is free worldwide."),)),
            "cites evidence never supplied": AnswerDraft((Claim("x", frozenset({new_id()})),)),
            "verifier marked unsupported": AnswerDraft((Claim("x", frozenset({e1.evidence_id}), supported=False),)),
            "free text added in strict mode": AnswerDraft((Claim("ok", frozenset({e1.evidence_id})),),
                                                          uncited_text="Also, we are the market leader."),
            "no claims at all": AnswerDraft(uncited_text="I think so."),
        }
        for name, draft in drafts.items():
            with self.subTest(name):
                self.assertFalse(validate_answer(plan, gate, draft).deliver)

    def test_J_no_cross_tenant_knowledge_in_any_mode(self):
        for mode in (GENERAL, PREFERRED, STRICT):
            with self.subTest(mode=mode):
                chain = self.chain_with(mode, ent_policy={"knowledge.general_fallback_allowed": True})
                plan, scope = self.plan_and_scope(chain)
                self.assertNotIn(self.src_foreign.id, scope)
                foreign = self.ev(self.src_foreign)
                gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (foreign,)),
                                              authorised=scope, chain=chain)
                self.assertNotIn(foreign.evidence_id, gate.evidence_ids)


class KnowledgeInheritanceTests(Base):
    def test_mode_inherits_down_the_hierarchy(self):
        chain = with_overrides(self.c, enterprise={"overrides": {"knowledge.mode": STRICT}})
        eff = resolve(chain)
        self.assertEqual(eff.values["knowledge.mode"], KnowledgeMode.STRICT_KNOWLEDGE_ONLY)
        self.assertEqual(eff.sources["knowledge.mode"].level, "enterprise")

    def test_descendant_may_choose_a_stricter_mode(self):
        chain = with_overrides(self.c, enterprise={"policy": {"knowledge.minimum_mode": PREFERRED}})
        self.assertEqual(validate_override(chain, "knowledge.mode", STRICT), KnowledgeMode.STRICT_KNOWLEDGE_ONLY)
        org = Chain(chain.enterprise, chain.organisation)
        self.assertEqual(validate_policy_write(org, "knowledge.minimum_mode", STRICT), KnowledgeMode.STRICT_KNOWLEDGE_ONLY)

    def test_approved_sources_never_broaden(self):
        a, b, c = new_id(), new_id(), new_id()
        chain = with_overrides(self.c, enterprise={"policy": {"knowledge.allowed_sources": [a, b, c]}},
                               organisation={"overrides": {"knowledge.approved_sources": [a, b]}})
        bot_chain = Chain(chain.enterprise, chain.organisation, chain.bot)
        self.assertEqual(validate_override(bot_chain, "knowledge.approved_sources", [a]), frozenset({a}))
        with self.assertRaises(ConfigError):
            validate_override(bot_chain, "knowledge.approved_sources", [a, c])
        stored = with_overrides(chain, bot={"overrides": {"knowledge.approved_sources": [a, c]}})
        eff = resolve(stored)
        self.assertEqual(eff.values["knowledge.approved_sources"], frozenset({a, b}))
        self.assertTrue(any(x.key == "knowledge.approved_sources" for x in eff.conflicts))
        # policy allow-list caps approvals at any level
        pol = with_overrides(self.c, enterprise={"policy": {"knowledge.allowed_sources": [a]}})
        with self.assertRaises(ConfigError):
            validate_override(Chain(pol.enterprise, pol.organisation, pol.bot), "knowledge.approved_sources", [b])

    def test_unapproved_sources_mean_nothing_is_approved(self):
        chain = with_overrides(self.c, bot={"overrides": {"knowledge.mode": STRICT}})
        plan, scope = self.plan_and_scope(chain)
        self.assertEqual(scope, frozenset())
        gate = gate_before_generation(plan, RetrievalResult(RetrievalStatus.OK, (self.ev(self.src_bot),)),
                                      authorised=scope, chain=chain)
        self.assertEqual(gate.decision, Decision.REFUSE)

    def test_strict_mode_forces_citations_and_evidence_floor(self):
        chain = self.chain_with(STRICT, ent_policy={"knowledge.min_evidence_floor": 0.8})
        bot_chain = Chain(chain.enterprise, chain.organisation, chain.bot)
        with self.assertRaises(ConfigError):
            validate_override(bot_chain, "knowledge.min_evidence_score", 0.6)
        eff = resolve(chain)
        plan = plan_for(eff)
        self.assertTrue(plan.require_citations)
        self.assertGreaterEqual(plan.min_evidence_score, 0.8)

    def test_access_scope_restricts_wider_sources(self):
        chain = self.chain_with(STRICT, ent_policy={"knowledge.access_scope": "bot"})
        plan, scope = self.plan_and_scope(chain)
        self.assertEqual(plan.access_scope, KnowledgeAccessScope.BOT)
        self.assertEqual(scope, frozenset({self.src_bot.id}))
        chain = self.chain_with(STRICT, ent_policy={"knowledge.access_scope": "organisation"})
        _, scope = self.plan_and_scope(chain)
        self.assertEqual(scope, frozenset({self.src_bot.id, self.src_org.id}))

    def test_sources_of_sibling_branches_are_never_in_scope(self):
        sibling_org = self.t.organisation(self.c.enterprise_id)
        sibling_bot = self.t.bot(self.c.organisation.id)
        s1 = KnowledgeSource(new_id(), self.c.enterprise_id, sibling_org.id, None, SourceKind.DOCUMENT)
        s2 = KnowledgeSource(new_id(), self.c.enterprise_id, self.c.organisation.id, sibling_bot.id, SourceKind.DOCUMENT)
        self.catalog += [s1, s2]
        chain = self.chain_with(STRICT, sources=[s1.id, s2.id])
        _, scope = self.plan_and_scope(chain)
        self.assertEqual(scope, frozenset())

    def test_document_level_acls(self):
        restricted = KnowledgeSource(new_id(), self.c.enterprise_id, None, None, SourceKind.DOCUMENT,
                                     acl_groups=frozenset({"finance"}))
        self.catalog.append(restricted)
        chain = self.chain_with(STRICT, sources=[restricted.id, self.src_ent.id])
        plan = plan_for(resolve(chain))
        self.assertNotIn(restricted.id, authorised_sources(plan, chain, self.catalog, Requester()))
        self.assertNotIn(restricted.id, authorised_sources(plan, chain, self.catalog,
                                                           Requester(frozenset({"finance"}), authenticated=False)))
        self.assertIn(restricted.id, authorised_sources(plan, chain, self.catalog,
                                                        Requester(frozenset({"finance"}), authenticated=True)))
        acl_policy = self.chain_with(STRICT, sources=[restricted.id, self.src_ent.id],
                                     ent_policy={"knowledge.document_acl_required": True})
        plan = plan_for(resolve(acl_policy))
        self.assertNotIn(self.src_ent.id, authorised_sources(plan, acl_policy, self.catalog, Requester()))


if __name__ == "__main__":
    unittest.main()
