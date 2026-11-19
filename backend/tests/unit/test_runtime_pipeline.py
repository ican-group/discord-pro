"""Runtime pipeline foundation (Phase 4A): tenant resolution, availability,
effective configuration, the strict-knowledge enforcement boundary, AI
configuration errors, credential scope and revocation, quotas and safe
errors. Test doubles only: no external service is contacted."""
import asyncio
import json
import unittest

from tests.unit._core_fixtures import Tree, run

from core.config_registry import KnowledgeMode
from core.credentials import Provider, SecretBox, credential_to_doc, new_credential
from core.hierarchy import ChannelType, Level, LifecycleAction, new_id
from core.knowledge import Claim, Evidence, KnowledgeSource, RetrievalResult, RetrievalStatus, SourceKind
from core.lifecycle import Authority
from runtime.errors import ProviderAuthError, ProviderRateLimited
from runtime.interfaces import ChannelAddress, GenerationResult, InboundMessage
from runtime.pipeline import Pipeline

FERNET = "c3BlYzAxMi10ZXN0LW9ubHktZmVybmV0LWtleS0zMmI="
API_KEY = "not-a-real-provider-key-0123456789"
GUILD, CH1, CH2 = "777777777777777777", "888888888888888881", "888888888888888882"
NOW = "2026-11-18T12:00:00+00:00"


class FakeProvider:
    name = "anthropic"

    def __init__(self, result=None, error=None):
        self.result = result or GenerationResult(text="General answer.")
        self.error = error
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result


class FakeCatalog:
    def __init__(self, sources):
        self.list = list(sources)

    async def sources(self, enterprise_id):
        return [s for s in self.list if s.enterprise_id == enterprise_id] + \
            [s for s in self.list if s.enterprise_id != enterprise_id]       # even if a catalog misbehaves


class FakeRetriever:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def retrieve(self, query, *, chain, plan, authorised):
        self.calls.append(set(authorised))
        if self.error:
            raise self.error
        return self.result


class World:
    def __init__(self):
        self.t = Tree()
        self.box = SecretBox(FERNET)
        self.e = self.t.enterprise()
        self.o = self.t.organisation(self.e.id)
        self.b = self.t.bot(self.o.id)
        self.i = self.t.instance(self.b.id)                         # webchat
        self.other = self.t.full()                                   # another tenant
        self.cred = self.credential(self.e.id)
        self.set_overrides(Level.BOT, self.b.id, {"ai.credential_id": self.cred.id,
                                                  "behaviour.language": "de"})
        self.src = KnowledgeSource(new_id(), self.e.id, self.o.id, self.b.id, SourceKind.DOCUMENT)
        self.foreign_src = KnowledgeSource(new_id(), self.other.enterprise_id, None, None, SourceKind.DOCUMENT)

    def credential(self, enterprise_id, usable_by=()):
        chain = self.t.chain(Level.ENTERPRISE, enterprise_id)
        c = new_credential(credential_id=new_id(), chain=chain, provider=Provider.ANTHROPIC, name="ai", secret=API_KEY,
                           box=self.box, actor_id="u", now=NOW, usable_by=usable_by)
        run(self.t.store.insert("credentials", credential_to_doc(c, 1)))
        return c

    def set_overrides(self, level, oid, overrides):
        node = self.t.chain(level, oid).target
        run(self.t.svc.set_overrides(level, oid, {**node.overrides, **overrides}))

    def set_policy(self, level, oid, policy):
        node = self.t.chain(level, oid).target
        run(self.t.svc.set_policy(level, oid, {**node.policy, **policy}))

    def strict(self, sources=None):
        sources = [self.src.id] if sources is None else sources
        self.set_policy(Level.ENTERPRISE, self.e.id, {"knowledge.allowed_sources": sources,
                                                      "knowledge.minimum_mode": "strict_knowledge_only"})
        self.set_overrides(Level.BOT, self.b.id, {"knowledge.approved_sources": sources,
                                                  "knowledge.insufficient_fallback": "Please contact support."})

    def pipeline(self, provider=None, retriever=None, catalog=None):
        self.provider = provider or FakeProvider()
        return Pipeline(self.t.store, box=self.box, clock=lambda: NOW, providers={"anthropic": self.provider},
                        retriever=retriever, catalog=catalog or FakeCatalog([self.src, self.foreign_src]))

    def web(self, text="What is the refund policy?", user="visitor-1", instance=None):
        inst = instance or self.i
        return InboundMessage(ChannelAddress(ChannelType.WEBCHAT, public_id=inst.channel.public_id), text, user)

    def ev(self, source=None, score=0.9, enterprise=None):
        source = source or self.src
        return Evidence(new_id(), source.id, enterprise or source.enterprise_id, score, "Refunds take 14 days.")


class RoutingTests(unittest.TestCase):
    def test_webchat_routes_to_its_instance_and_unknown_ids_are_silent(self):
        w = World()
        out = run(w.pipeline().handle(w.web()))
        self.assertEqual((out.kind, out.instance_id), ("answer", w.i.id))
        out = run(w.pipeline().handle(InboundMessage(ChannelAddress(ChannelType.WEBCHAT, public_id="pk_" + "x" * 30),
                                                     "hi", "u")))
        self.assertEqual((out.kind, out.deliver, out.admin_issue), ("silent", False, "no_route"))

    def test_discord_channel_scopes_route_and_dm_ambiguity_fails_closed(self):
        w = World()
        a = w.t.instance(w.b.id, {"type": "discord", "listen_mode": "specific_channels", "channel_ids": [CH1]})
        a = run(w.t.svc.bind_discord_guild(a.id, GUILD, application_id="910000000000000001", actor_id="u"))
        b = w.t.instance(w.b.id, {"type": "discord", "listen_mode": "specific_channels", "channel_ids": [CH2]})
        b = run(w.t.svc.bind_discord_guild(b.id, GUILD, application_id="910000000000000001", actor_id="u"))
        p = w.pipeline()
        APP = "910000000000000001"
        msg = lambda ch: InboundMessage(ChannelAddress(ChannelType.DISCORD, application_id=APP, guild_id=GUILD,
                                                       channel_id=ch), "hi", "u1")
        self.assertEqual(run(p.handle(msg(CH1))).instance_id, a.id)
        self.assertEqual(run(p.handle(msg(CH2))).instance_id, b.id)
        self.assertEqual(run(p.handle(msg("888888888888888883"))).admin_issue, "no_route")
        dm = InboundMessage(ChannelAddress(ChannelType.DISCORD, application_id=APP, is_dm=True,
                                           dm_shared_guilds=(GUILD,)), "hi", "u1")
        self.assertEqual(run(p.handle(dm)).admin_issue, "no_route")               # DMs off by default (D-Q5)
        for inst in (a, b):
            run(w.t.svc.update(Level.BOT_INSTANCE, inst.id, {"channel": {"dm_enabled": True}}, actor_id="u"))
        out = run(p.handle(dm))
        self.assertEqual((out.deliver, out.admin_issue), (False, "ambiguous_binding"))   # never "first found"
        run(w.t.svc.update(Level.BOT_INSTANCE, b.id, {"channel": {"dm_enabled": False}}, actor_id="u"))
        self.assertEqual(run(p.handle(dm)).instance_id, a.id)
        other_app = InboundMessage(ChannelAddress(ChannelType.DISCORD, application_id="920000000000000002",
                                                  guild_id=GUILD, channel_id=CH1), "hi", "u1")
        self.assertEqual(run(p.handle(other_app)).admin_issue, "no_route")     # another Enterprise's application

    def test_claim_and_document_must_agree(self):
        w = World()
        cred = new_id()
        tg = run(w.t.svc.create_instance(bot_id=w.b.id, name="tg", actor_id="u", telegram_bot_id="4242",
                                         channel={"type": "telegram", "credential_id": cred}))
        p = w.pipeline()
        addr = lambda hint=None: InboundMessage(ChannelAddress(ChannelType.TELEGRAM, telegram_bot_id="4242",
                                                               instance_hint=hint), "hi", "u")
        self.assertEqual(run(p.handle(addr(tg.id))).instance_id, tg.id)
        self.assertEqual(run(p.handle(addr(w.i.id))).admin_issue, "binding_mismatch")   # wrong webhook path
        # a forged claim pointing at a foreign instance is refused
        run(w.t.svc.bindings.claim(w.other.instance.id, w.other.enterprise_id, {"webchat:pk_forgedforgedforgedforged": ("*",)}))
        forged = InboundMessage(ChannelAddress(ChannelType.WEBCHAT, public_id="pk_forgedforgedforgedforged"), "x", "u")
        self.assertEqual(run(p.handle(forged)).admin_issue, "binding_mismatch")

    def test_inactive_ancestor_makes_the_instance_silent(self):
        w = World()
        run(w.t.svc.change_lifecycle(Level.ORGANISATION, w.o.id, LifecycleAction.SUSPEND, authority=Authority.OBJECT,
                                     actor_id="u"))
        p = w.pipeline()
        out = run(p.handle(w.web()))
        self.assertEqual((out.kind, out.deliver, out.admin_issue), ("silent", False, "unavailable"))
        self.assertEqual(w.provider.requests, [])


class ConfigurationTests(unittest.TestCase):
    def test_effective_configuration_reaches_the_provider_without_leaking_the_key(self):
        w = World()
        p = w.pipeline()
        out = run(p.handle(w.web()))
        req = w.provider.requests[0]
        self.assertEqual((req.language, req.provider, req.knowledge_mode), ("de", "anthropic", "general"))
        self.assertEqual(req.api_key.reveal(), API_KEY)
        self.assertNotIn(API_KEY, repr(req) + repr(out))
        events = json.dumps(run(w.t.store.find("usage_events", {})), default=str)
        self.assertNotIn(API_KEY, events)
        self.assertNotIn(API_KEY, json.dumps(run(p.runtime_config(w.i.id)), default=str))

    def test_missing_ai_configuration_is_an_admin_issue_with_a_safe_reply(self):
        w = World()
        w.set_overrides(Level.BOT, w.b.id, {"ai.credential_id": None})
        node = w.t.chain(Level.BOT, w.b.id).bot
        run(w.t.svc.set_overrides(Level.BOT, w.b.id, {k: v for k, v in node.overrides.items()
                                                      if k != "ai.credential_id"}))
        out = run(w.pipeline().handle(w.web()))
        self.assertEqual((out.kind, out.admin_issue), ("fallback", "ai_not_configured"))
        self.assertNotIn("knowledge base", out.text.lower())                   # D-Q1
        self.assertEqual(w.provider.requests, [])

    def test_channel_policy_blocks_the_instance(self):
        w = World()
        w.set_policy(Level.ENTERPRISE, w.e.id, {"channels.allowed": ["telegram"]})
        out = run(w.pipeline().handle(w.web()))
        self.assertEqual((out.deliver, out.admin_issue), (False, "channel_blocked"))


class StrictKnowledgeBoundaryTests(unittest.TestCase):
    def check_refusal(self, w, pipeline, msg=None):
        out = run(pipeline.handle(msg or w.web()))
        self.assertEqual((out.kind, out.text), ("refusal", "Please contact support."))
        self.assertEqual(w.provider.requests, [])                              # no AI call at all
        return out

    def test_no_knowledge_service_means_refusal(self):
        w = World()
        w.strict()
        self.check_refusal(w, w.pipeline(retriever=None))

    def test_no_approved_evidence_means_no_factual_answer(self):
        w = World()
        w.strict(sources=[])                                                    # nothing approved
        retr = FakeRetriever(RetrievalResult(RetrievalStatus.OK, (w.ev(),)))
        self.check_refusal(w, w.pipeline(retriever=retr))
        self.assertEqual(retr.calls, [])                                         # nothing to search

    def test_unavailable_insufficient_and_failing_retrieval_refuse(self):
        for retr in (FakeRetriever(RetrievalResult(RetrievalStatus.UNAVAILABLE)),
                     FakeRetriever(RetrievalResult(RetrievalStatus.OK, ())),
                     FakeRetriever(error=TimeoutError("vector store 10.0.0.5 timed out"))):
            w = World()
            w.strict()
            out = self.check_refusal(w, w.pipeline(retriever=retr))
            self.assertNotIn("10.0.0.5", out.text)
        w = World()
        w.strict()
        self.check_refusal(w, w.pipeline(retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK,
                                                                                 (w.ev(score=0.05),)))))

    def test_contradictory_evidence_does_not_produce_an_answer(self):
        w = World()
        w.strict()
        retr = FakeRetriever(RetrievalResult(RetrievalStatus.OK, (w.ev(), w.ev()), conflicting=True))
        self.check_refusal(w, w.pipeline(retriever=retr))

    def test_cross_tenant_evidence_is_discarded(self):
        w = World()
        w.strict()
        retr = FakeRetriever(RetrievalResult(RetrievalStatus.OK, (w.ev(source=w.foreign_src),)))
        self.check_refusal(w, w.pipeline(retriever=retr))
        self.assertEqual(retr.calls, [{w.src.id}])                              # only the own approved source

    def test_grounded_answer_requires_cited_claims(self):
        w = World()
        w.strict()
        ev = w.ev()
        good = FakeProvider(GenerationResult(claims=(Claim("Refunds take 14 days.", frozenset({ev.evidence_id}),
                                                           quote="Refunds take 14 days"),)))
        p = w.pipeline(provider=good, retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK, (ev,))))
        out = run(p.handle(w.web()))
        self.assertEqual((out.kind, out.citations), ("answer", (w.src.id,)))
        req = good.requests[0]
        self.assertTrue(req.structured_claims_required)
        self.assertEqual([e.evidence_id for e in req.evidence], [ev.evidence_id])
        for bad in (GenerationResult(text="Refunds take 14 days, and also general advice."),       # free text
                    GenerationResult(claims=(Claim("Invented.", frozenset({"made-up-id"})),)),     # unknown cite
                    GenerationResult(claims=(Claim("Uncited.", frozenset()),))):                    # uncited
            w2 = World()
            w2.strict()
            ev2 = w2.ev()
            prov = FakeProvider(bad)
            p2 = w2.pipeline(provider=prov, retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK, (ev2,))))
            out = run(p2.handle(w2.web()))
            self.assertEqual((out.kind, out.text), ("refusal", "Please contact support."))

    def test_no_silent_fallback_to_general_knowledge(self):
        w = World()
        w.strict()
        out = run(w.pipeline(retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK, ()))).handle(w.web()))
        self.assertEqual(out.kind, "refusal")
        # knowledge_preferred falls back to GENERAL only when the Enterprise permits it
        for allowed, expected in ((False, "refusal"), (True, "answer")):
            w = World()
            w.set_policy(Level.ENTERPRISE, w.e.id, {"knowledge.allowed_sources": [w.src.id],
                                                    "knowledge.general_fallback_allowed": allowed})
            w.set_overrides(Level.BOT, w.b.id, {"knowledge.mode": "knowledge_preferred",
                                                "knowledge.approved_sources": [w.src.id]})
            out = run(w.pipeline(retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK, ()))).handle(w.web()))
            self.assertEqual(out.kind, expected)

    def test_general_mode_works_without_a_knowledge_base(self):
        w = World()
        out = run(w.pipeline(retriever=None).handle(w.web()))
        self.assertEqual((out.kind, out.text), ("answer", "General answer."))
        self.assertFalse(w.provider.requests[0].structured_claims_required)

    def test_provider_failure_in_strict_mode_fails_closed_without_details(self):
        w = World()
        w.strict()
        ev = w.ev()
        from runtime.errors import ProviderUnavailable
        prov = FakeProvider(error=ProviderUnavailable())               # definitely not generated
        out = run(w.pipeline(provider=prov, retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK, (ev,))))
                  .handle(w.web()))
        self.assertEqual((out.kind, out.billed), ("refusal", False))
        # Part C: an unexpected failure cannot prove nothing was generated -> uncertain, nothing delivered
        prov = FakeProvider(error=RuntimeError("upstream 500 at https://api.example/v1 key=" + API_KEY))
        out = run(w.pipeline(provider=prov, retriever=FakeRetriever(RetrievalResult(RetrievalStatus.OK, (ev,))))
                  .handle(w.web()))
        self.assertEqual((out.kind, out.deliver, out.billed), ("uncertain", False, True))
        self.assertNotIn(API_KEY, out.text + str(out))
        self.assertNotIn("upstream", out.text)


class LedgerIdentityTests(unittest.TestCase):
    def test_only_definitely_unbilled_keys_can_be_reserved_again(self):
        from runtime.usage import AlreadyReserved, StoreUsageTracker
        w = World()
        chain = run(w.t.svc.load_chain(Level.BOT_INSTANCE, w.i.id))
        u = StoreUsageTracker(w.t.store)
        now = "2026-11-18T10:00:00+00:00"
        r = run(u.reserve_ai_message(chain, now=now, key="k-uncertain"))
        run(u.mark_uncertain("k-uncertain", now=now))
        with self.assertRaises(AlreadyReserved):
            run(u.reserve_ai_message(chain, now=now, key="k-uncertain"))       # may have generated: never reused
        r = run(u.reserve_ai_message(chain, now=now, key="k-released"))
        run(u.release(r, now=now))
        self.assertIsNotNone(run(u.reserve_ai_message(chain, now=now, key="k-released")))   # definitely unbilled
        r = run(u.reserve_ai_message(chain, now=now, key="k-settled"))
        run(u.settle(r, chain, now=now))
        with self.assertRaises(AlreadyReserved):
            run(u.reserve_ai_message(chain, now=now, key="k-settled"))


class CredentialScopeRuntimeTests(unittest.TestCase):
    def test_scope_and_revocation_fail_closed_at_runtime(self):
        w = World()
        other_bot = w.t.bot(w.o.id)
        scoped = w.credential(w.e.id, usable_by=(("bot", other_bot.id),))
        w.set_overrides(Level.BOT, other_bot.id, {"ai.credential_id": scoped.id})
        # the bot's own override pointing at a credential scoped to a different bot
        w.set_overrides(Level.BOT, w.b.id, {"ai.credential_id": scoped.id})
        out = run(w.pipeline().handle(w.web()))
        self.assertEqual(out.kind, "fallback")
        self.assertEqual(w.provider.requests, [])
        # revocation
        w2 = World()
        run(w2.t.store.update("credentials", w2.cred.id, {"status": "revoked", "secret_enc": ""}))
        out = run(w2.pipeline().handle(w2.web()))
        self.assertEqual(out.kind, "fallback")
        self.assertEqual(w2.provider.requests, [])


class QuotaAndErrorTests(unittest.TestCase):
    def test_enterprise_quota_is_never_exceeded_under_concurrency(self):
        w = World()
        w.set_policy(Level.ENTERPRISE, w.e.id, {"limits.max_monthly_ai_messages": 3})
        p = w.pipeline()

        async def main():
            return await asyncio.gather(*[p.handle(w.web(user=f"u{i}")) for i in range(10)])
        outs = run(main())
        self.assertEqual(sum(1 for o in outs if o.kind == "answer"), 3)
        self.assertEqual(sum(1 for o in outs if o.kind == "quota_exceeded"), 7)
        self.assertEqual(len(w.provider.requests), 3)
        counter = run(w.t.store.get("usage_counters", f"{w.e.id}:2026-11"))
        self.assertEqual(counter["count"], 3)

    def test_organisation_sub_quota_and_release_on_provider_failure(self):
        w = World()
        w.set_policy(Level.ORGANISATION, w.o.id, {"limits.max_monthly_ai_messages": 1})
        prov = FakeProvider(error=ProviderRateLimited())
        out = run(w.pipeline(provider=prov).handle(w.web()))
        self.assertEqual(out.kind, "busy")
        self.assertEqual(run(w.t.store.get("usage_counters", f"{w.o.id}:2026-11"))["count"], 0)   # released
        p = w.pipeline()
        self.assertEqual(run(p.handle(w.web(user="a"))).kind, "answer")
        self.assertEqual(run(p.handle(w.web(user="b"))).kind, "quota_exceeded")
        self.assertEqual(run(w.t.store.get("usage_counters", f"{w.e.id}:2026-11"))["count"], 1)   # E released too

    def test_end_user_rate_limit(self):
        w = World()
        w.set_overrides(Level.BOT, w.b.id, {"limits.end_user_msgs_per_minute": 2})
        p = w.pipeline()
        kinds = [run(p.handle(w.web(user="same"))).kind for _ in range(3)]
        self.assertEqual(kinds, ["answer", "answer", "rate_limited"])
        self.assertEqual(run(p.handle(w.web(user="someone-else"))).kind, "answer")

    def test_provider_auth_error_is_an_admin_issue(self):
        w = World()
        out = run(w.pipeline(provider=FakeProvider(error=ProviderAuthError())).handle(w.web()))
        self.assertEqual((out.kind, out.admin_issue), ("fallback", "provider_auth"))


class TenantIsolationTests(unittest.TestCase):
    def test_runtime_never_uses_another_tenants_credential(self):
        w = World()
        foreign = w.credential(w.other.enterprise_id)
        node = w.t.chain(Level.BOT, w.b.id).bot
        run(w.t.store.update("bots", w.b.id, {"overrides": {**node.overrides, "ai.credential_id": foreign.id}}))
        out = run(w.pipeline().handle(w.web()))
        self.assertEqual(out.kind, "fallback")
        self.assertEqual(w.provider.requests, [])


if __name__ == "__main__":
    unittest.main()
