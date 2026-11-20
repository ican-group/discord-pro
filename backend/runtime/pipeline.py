"""The one runtime pipeline (spec 012 §10, Phase 4A foundation).

``Pipeline.handle(message)`` runs, in order:

1. channel binding → Bot Instance (``runtime.routing``; ambiguous ⇒ refuse);
2. chain load with ancestry verification (Enterprise → Organisation → Bot →
   Instance);
3. lifecycle availability (every ancestor active), else silent;
4. effective configuration and Enterprise policy (``core.resolver``, the
   single resolver), including credential scope (X6) and channel policy;
5. per end-user rate limit;
6. knowledge plan and **strict-knowledge enforcement boundary**: authorised
   sources (deny by default), retrieval, ``gate_before_generation``. A
   refusal happens *before* any AI call; GENERAL needs no knowledge base;
7. AI provider configuration (missing or blocked ⇒ admin configuration
   issue and a safe fallback reply, never a "no knowledge base" text);
8. availability re-check, quota reservation (Enterprise and Organisation);
9. generation with the decrypted credential in memory only;
10. ``validate_answer``: in strict mode only cited claims over the gated
    evidence are deliverable;
11. usage event; typed errors mapped to fixed safe messages.

Retrieval (4K) and providers are injected; Phase 4A uses test doubles. Strict
Knowledge Only is therefore enforced at this boundary but is **not complete
end to end until 4K** supplies real retrieval and evidence validation.
"""
from __future__ import annotations

import asyncio

from typing import Any, Callable, Dict, Mapping, Optional

from core import resolver
from core.config_registry import KnowledgeMode, PLATFORM_POLICY
from core.credentials import CredentialError, SecretBox, check_use, enterprise_credentials
from core.hierarchy import HierarchyError, HierarchyService, Level
from core.knowledge import (RefusalReason, AnswerDraft, Decision, RetrievalResult, RetrievalStatus, authorised_sources, fail_closed,
                            gate_before_generation, plan_for, refusal, validate_answer)
from runtime.errors import (MESSAGES, AmbiguousRoute, BindingMismatch, NoRoute, ProviderAuthError, ProviderError,
                            ProviderRateLimited, ProviderUncertain)
from runtime.interfaces import (AIProvider, EmptyCatalog, GenerationRequest, InboundMessage, KnowledgeCatalog,
                                KnowledgeRetriever, Outcome, Secret)
from runtime.routing import resolve_instance
from runtime.usage import AlreadyReserved, StoreUsageTracker

SILENT = Outcome(kind="silent", deliver=False)


RETRIEVAL_TIMEOUT_SECONDS = 5.0


class _IntegrationUsage:
    """Usage tracker view that attributes every event to a Bot Integration
    (Enterprise, Organisation and Bot come from the chain)."""

    def __init__(self, inner, integration_id: str):
        self._inner, self._iid = inner, integration_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def reserve_ai_message(self, chain, *, now, key=None, labels=None):
        return await self._inner.reserve_ai_message(chain, now=now, key=key,
                                                    labels={**(labels or {}), "integration_id": self._iid})

    async def record(self, chain, kind, *, now, details=None, event_id=None):
        return await self._inner.record(chain, kind, now=now, details={**(details or {}), "integration_id": self._iid},
                                        event_id=event_id)

    async def settle(self, reservation, chain, *, now, details=None):
        return await self._inner.settle(reservation, chain, now=now,
                                        details={**(details or {}), "integration_id": self._iid})


class Pipeline:
    def __init__(self, store, *, box: SecretBox, clock: Callable[[], str],
                 providers: Mapping[str, AIProvider], retriever: Optional[KnowledgeRetriever] = None,
                 catalog: Optional[KnowledgeCatalog] = None, usage: Optional[StoreUsageTracker] = None,
                 platform_policy=PLATFORM_POLICY):
        self.store = store
        self.box = box
        self.clock = clock
        self.providers: Dict[str, AIProvider] = dict(providers)
        self.retriever = retriever
        self.catalog = catalog or EmptyCatalog()
        self.usage = usage or StoreUsageTracker(store, platform_policy=platform_policy)
        self.platform_policy = platform_policy
        self.hierarchy = HierarchyService(store, clock=clock)

    async def handle(self, message: InboundMessage, *, billing_key: Optional[str] = None,
                     on_generated=None, usage_labels: Optional[Dict[str, Any]] = None) -> Outcome:
        """Run the pipeline. Passive messages (D12) never produce a visible
        refusal, fallback, limit or error notice: those become ``skipped``."""
        # usage_labels: server-set attribution (F5 preview), never client input
        outcome = await self._handle(message, billing_key=billing_key, on_generated=on_generated,
                                     labels=dict(usage_labels or {}))
        if message.passive and outcome.kind in ("refusal", "fallback", "rate_limited", "quota_exceeded", "busy"):
            from dataclasses import replace as _replace
            return _replace(outcome, kind="skipped", deliver=False, text="")
        return outcome

    async def _handle(self, message: InboundMessage, *, billing_key: Optional[str] = None,
                      on_generated=None, labels: Optional[Dict] = None) -> Outcome:
        """``billing_key`` makes billing idempotent (one reservation and one
        settlement per key, ever). ``on_generated(outcome)`` durably stores a
        generated answer *before* usage is settled; if it fails, the outcome is
        ``uncertain``: billed at most once, never regenerated automatically."""
        integ_id = message.integration_id
        tracker = self.usage if integ_id is None else _IntegrationUsage(self.usage, integ_id)
        if integ_id is not None:
            # Phase 4E: a Bot Integration (Zendesk) is resolved from its own record and
            # its Bot chain, never from anything in the inbound payload.
            iid = None
            integ = await self.store.get("bot_integrations", integ_id)
            if integ is None:
                return Outcome(kind="silent", deliver=False, admin_issue="no_route", integration_id=integ_id)
            try:
                chain = await self.hierarchy.load_chain(Level.BOT, integ["bot_id"])
            except HierarchyError:
                return Outcome(kind="silent", deliver=False, admin_issue="integrity", integration_id=integ_id)
            if chain.enterprise_id != integ.get("enterprise_id") or chain.organisation.id != integ.get("organisation_id"):
                return Outcome(kind="silent", deliver=False, admin_issue="integrity", integration_id=integ_id)
        else:
            try:
                iid = await resolve_instance(self.store, message.address)
            except (NoRoute, AmbiguousRoute, BindingMismatch) as exc:
                return Outcome(kind="silent", deliver=False, admin_issue=exc.category)
            try:
                chain = await self.hierarchy.load_chain(Level.BOT_INSTANCE, iid)
            except HierarchyError:
                return Outcome(kind="silent", deliver=False, admin_issue="integrity")
        if not chain.availability().available:
            return Outcome(kind="silent", deliver=False, instance_id=iid, admin_issue="unavailable",
                           integration_id=integ_id)
        plan = None
        try:
            creds = await enterprise_credentials(self.store, chain.enterprise_id)
            if integ_id is not None:
                eff = resolver.resolve_integration(chain, integ, platform_policy=self.platform_policy,
                                                   credentials=creds)
            else:
                eff = resolver.resolve(chain, platform_policy=self.platform_policy, credentials=creds)
            if eff.runtime_blockers:
                return Outcome(kind="silent", deliver=False, instance_id=iid, admin_issue="channel_blocked",
                               integration_id=integ_id)
            fallback = eff.values.get("behaviour.fallback_message") or ""
            now = self.clock()
            if not await tracker.allow_end_user(chain, message.end_user_id,
                                                   eff.values["limits.end_user_msgs_per_minute"], now=now):
                return Outcome(kind="rate_limited", deliver=True, text=MESSAGES["rate_limited"], instance_id=iid)

            # ── strict-knowledge enforcement boundary ──
            plan = plan_for(eff)
            authorised: frozenset = frozenset()
            retrieval: Optional[RetrievalResult] = None
            if plan.mode is not KnowledgeMode.GENERAL:
                authorised = authorised_sources(plan, chain, await self.catalog.sources(chain.enterprise_id),
                                                message.requester)
                if self.retriever is None:
                    retrieval = None                                # knowledge service missing => unavailable
                elif authorised:
                    try:
                        retrieval = await asyncio.wait_for(
                            self.retriever.retrieve(message.text, chain=chain, plan=plan, authorised=authorised),
                            timeout=RETRIEVAL_TIMEOUT_SECONDS)
                    except Exception:                          # outage or timeout: knowledge unavailable
                        retrieval = RetrievalResult(RetrievalStatus.UNAVAILABLE)
                else:
                    retrieval = RetrievalResult(RetrievalStatus.OK, ())     # nothing approved: no evidence
            gate = gate_before_generation(plan, retrieval, authorised=authorised, chain=chain)
            if gate.decision is Decision.REFUSE:
                verdict = refusal(plan, gate.reason)
                await tracker.record(chain, "refusal", now=now, details={"reason": gate.reason.value})
                if plan.human_handoff:
                    await tracker.record(chain, "handoff", now=now, details={"reason": gate.reason.value})
                return Outcome(kind="refusal", deliver=True, text=verdict.text or fallback, instance_id=iid,
                               admin_issue="handoff" if plan.human_handoff else None, handoff=plan.human_handoff,
                               integration_id=integ_id)

            # ── AI provider configuration (D-Q1) ──
            if "ai" in eff.blocked:
                await tracker.record(chain, "config_error", now=now, details={"issue": "ai_blocked"})
                return Outcome(kind="fallback", deliver=True, text=fallback, instance_id=iid,
                               admin_issue="ai_not_configured")
            provider_name = eff.values["ai.provider"]
            provider = self.providers.get(provider_name)
            cred_id = eff.values.get("ai.credential_id")
            try:
                cred = check_use(creds.get(cred_id), referencing_chain=chain, slot=f"ai.{provider_name}",
                                 policy=eff.policy)                  # revocation / scope fail closed here too
                api_key = Secret(self.box.decrypt(cred.secret_enc))
            except CredentialError:
                await tracker.record(chain, "config_error", now=now, details={"issue": "credential"})
                return Outcome(kind="fallback", deliver=True, text=fallback, instance_id=iid,
                               admin_issue="credential_unavailable")
            if provider is None:
                await tracker.record(chain, "config_error", now=now, details={"issue": "provider"})
                return Outcome(kind="fallback", deliver=True, text=fallback, instance_id=iid,
                               admin_issue="provider_not_available")

            # ── R3: live API data (never in strict mode; untrusted data, not knowledge) ──
            api_context = ""
            if plan.mode is not KnowledgeMode.STRICT_KNOWLEDGE_ONLY and not message.passive and \
                    eff.values.get("tools.api_calls_enabled") is True and \
                    eff.policy.get("tools.external_api_calls_allowed") is not False:
                api_context = await self._api_data(chain, eff, creds, provider, provider_name, api_key, message,
                                                   tracker, billing_key, now, labels)

            # ── availability re-check right before the provider call ──
            if not await self._still_available(chain, iid, integ_id):
                return Outcome(kind="silent", deliver=False, instance_id=iid, admin_issue="unavailable",
                               integration_id=integ_id)
            try:
                reservation = await tracker.reserve_ai_message(chain, now=now, key=billing_key,
                                                               labels=labels)
            except AlreadyReserved:
                return Outcome(kind="uncertain", deliver=False, instance_id=iid, admin_issue="already_reserved")
            if reservation is None:
                await tracker.record(chain, "quota_exceeded", now=now)
                return Outcome(kind="quota_exceeded", deliver=True, text=MESSAGES["quota_exceeded"], instance_id=iid,
                               admin_issue="quota_exceeded")
            request = GenerationRequest(
                provider=provider_name, model=eff.values["ai.model"], api_key=api_key,
                persona=eff.values["behaviour.persona"],
                instructions=(eff.values["behaviour.instructions"] + ("\n\n" + api_context if api_context else "")).strip(),
                tone_instructions=eff.values["behaviour.tone_instructions"], language=eff.values["behaviour.language"],
                user_text=message.text, knowledge_mode=plan.mode.value,
                evidence=gate.evidence if gate.decision is Decision.GROUNDED else (),
                structured_claims_required=plan.strict or gate.decision is Decision.GROUNDED,
                history=tuple(message.history[-20:]))
            if message.format_hint and not request.structured_claims_required:
                from dataclasses import replace as _replace
                request = _replace(request, instructions=(request.instructions + "\n\n" + message.format_hint).strip())
            try:
                result = await provider.generate(request)
            except ProviderUncertain:
                # Part C: the provider may have generated (and charged). Never release,
                # never regenerate automatically: the reservation becomes uncertain and is
                # reconciled explicitly (counted against quota until resolved).
                await tracker.mark_uncertain(reservation.key, now=now)
                await tracker.record(chain, "provider_uncertain", now=now)
                return Outcome(kind="uncertain", deliver=False, instance_id=iid, admin_issue="provider_uncertain",
                               billed=True, integration_id=integ_id)
            except ProviderRateLimited:
                await tracker.release(reservation, now=now)
                return Outcome(kind="busy", deliver=True, text=MESSAGES["busy"], instance_id=iid,
                               admin_issue="provider_rate_limited")
            except ProviderAuthError:
                await tracker.release(reservation, now=now)
                await tracker.record(chain, "config_error", now=now, details={"issue": "provider_auth"})
                return Outcome(kind="fallback", deliver=True, text=fallback, instance_id=iid,
                               admin_issue="provider_auth")
            except ProviderError:                                     # definite failure: nothing generated
                await tracker.release(reservation, now=now)
                verdict = fail_closed(plan, RuntimeError("provider"))
                return Outcome(kind="refusal", deliver=True, text=verdict.text or fallback, instance_id=iid,
                               admin_issue="provider_error")
            except Exception:
                # an unexpected adapter failure cannot prove that nothing was generated
                await tracker.mark_uncertain(reservation.key, now=now)
                return Outcome(kind="uncertain", deliver=False, instance_id=iid, admin_issue="provider_uncertain",
                               billed=True, integration_id=integ_id)
            verdict = validate_answer(plan, gate, AnswerDraft(claims=tuple(result.claims),
                                                              uncited_text=result.text or ""))
            if verdict.deliver and gate.decision is Decision.GROUNDED and \
                    eff.policy.get("knowledge.semantic_verification") is True:
                verdict = await self._semantic_check(chain, eff, provider, provider_name, api_key, result, gate,
                                                     verdict, tracker, billing_key, now, labels)
            if verdict.deliver and verdict.provenance and hasattr(self.retriever, "verify"):
                cited = {eid for _, _, eid in verdict.provenance}
                try:
                    live = await self.retriever.verify(chain.enterprise_id,
                                                       [e for e in gate.evidence if e.evidence_id in cited])
                except Exception:
                    live = False
                if not live:                                  # revoked/deleted/replaced during generation
                    from core.knowledge import AnswerVerdict
                    verdict = AnswerVerdict(False, reason=RefusalReason.KNOWLEDGE_UNAVAILABLE)
            usage = {"delivered": verdict.deliver, "mode": plan.mode.value, "provider": provider_name,
                     **({"integration_id": integ_id} if integ_id else {}),
                     "input_tokens": int(result.input_tokens or 0), "output_tokens": int(result.output_tokens or 0)}
            skip = message.passive and (not verdict.deliver or
                                        (verdict.text or "").strip().upper().startswith("[SKIP]"))
            if skip:
                # passive mode: the model (or the evidence check) chose silence; the
                # generation happened, so it is billed once, but nothing is delivered
                usage["skipped"] = True
                outcome = Outcome(kind="skipped", deliver=False, instance_id=iid, billed=True,
                                  usage=tuple(usage.items()))
            elif not verdict.deliver:
                text = verdict.text or refusal(plan, verdict.reason).text or fallback
                if plan.human_handoff:
                    await tracker.record(chain, "handoff", now=now,
                                         details={"reason": (verdict.reason.value if verdict.reason else "refused")})
                outcome = Outcome(kind="refusal", deliver=True, text=text, instance_id=iid, billed=True,
                                  usage=tuple(usage.items()), admin_issue="handoff" if plan.human_handoff else None,
                                  handoff=plan.human_handoff, integration_id=integ_id)
            else:
                outcome = Outcome(kind="answer", deliver=True, text=verdict.text,
                                  citations=tuple(verdict.citations or ()), instance_id=iid, billed=True,
                                  provenance=tuple(verdict.provenance or ()), integration_id=integ_id,
                                  usage=tuple(usage.items()))
            # ── Phase 6A (H): availability re-check after generation ──
            # An object suspended, archived or deleted while the provider ran gets nothing
            # stored or delivered. The provider did run, so the reservation is settled once.
            if not await self._still_available(chain, iid, integ_id):
                try:
                    await tracker.settle(reservation, chain, now=now, details={**usage, "delivered": False,
                                                                               "undelivered": "unavailable"})
                except Exception:
                    pass
                return Outcome(kind="silent", deliver=False, instance_id=iid, billed=True, admin_issue="unavailable",
                               integration_id=integ_id)
            if on_generated is not None:
                try:
                    await on_generated(outcome)            # durable result first ...
                except Exception:
                    # Reservation stays (billed at most once); the request becomes
                    # uncertain and is never regenerated automatically.
                    return Outcome(kind="uncertain", deliver=False, instance_id=iid, admin_issue="persist_failed",
                                   billed=True)
            try:
                await tracker.settle(reservation, chain, now=now, details=usage)   # ... then settlement
            except Exception:
                pass                                       # idempotent settle is retried by the completion path
            return outcome
        except Exception:
            # Never leak internals; strict mode fails closed with its configured fallback.
            if plan is not None:
                verdict = fail_closed(plan, RuntimeError("internal"))
                return Outcome(kind="refusal", deliver=True, text=verdict.text, instance_id=iid, admin_issue="internal")
            return Outcome(kind="silent", deliver=False, instance_id=iid, admin_issue="internal")

    async def _still_available(self, chain, iid, integ_id) -> bool:
        """Fresh lifecycle availability of the target (and, for a Bot Integration,
        that it is still enabled and attached). Any load failure counts as gone."""
        try:
            if integ_id is not None:
                fresh = await self.hierarchy.load_chain(Level.BOT, chain.bot.id)
                again = await self.store.get("bot_integrations", integ_id)
                still = bool(again and again.get("status") == "enabled" and again.get("bot_id") == chain.bot.id
                             and not again.get("deletion"))
            else:
                fresh = await self.hierarchy.load_chain(Level.BOT_INSTANCE, iid)
                still = True
        except HierarchyError:
            return False
        return still and fresh.availability().available

    async def _api_data(self, chain, eff, creds, provider, provider_name, api_key, message, tracker, billing_key,
                        now, labels) -> str:
        """R3: one routing call picks up to MAX_CALLS_PER_MESSAGE enabled GET endpoints of
        the Bot's API integrations; they run through the outbound guard and their results
        become a delimited, untrusted data block. Any failure ⇒ answer without API data.
        The routing call is reserved and settled like any AI message (usage, quotas)."""
        from runtime import api_calls as ac
        try:
            integs, eps = await ac.enabled_for_bot(self.store, chain.enterprise_id, chain.bot.id)
        except Exception:
            return ""
        if not eps:
            return ""
        try:
            reservation = await tracker.reserve_ai_message(chain, now=now,
                                                           key=(billing_key + ":api") if billing_key else None,
                                                           labels={**(labels or {}), "purpose": "api_routing"})
        except AlreadyReserved:
            return ""
        if reservation is None:
            return ""                                        # quota reached: answer without API data
        request = GenerationRequest(
            provider=provider_name, model=eff.values["ai.model"], api_key=api_key, persona="",
            instructions=ac.ROUTING_INSTRUCTIONS, tone_instructions="", language="en",
            user_text=ac.routing_payload(message.text, list(eps.values())), knowledge_mode="api_routing",
            evidence=(), structured_claims_required=False, history=(), max_output_tokens=400)
        try:
            out = await provider.generate(request)
        except ProviderUncertain:
            await tracker.mark_uncertain(reservation.key, now=now)
            return ""
        except ProviderError:
            await tracker.release(reservation, now=now)
            return ""
        except Exception:
            await tracker.mark_uncertain(reservation.key, now=now)
            return ""
        try:
            await tracker.settle(reservation, chain, now=now,
                                 details={"purpose": "api_routing", "input_tokens": int(out.input_tokens or 0),
                                          "output_tokens": int(out.output_tokens or 0)})
        except Exception:
            pass
        calls = ac.parse_routing(out.text or "", eps)
        if not calls:
            return ""

        async def run(ep, params):
            integ = integs[ep["integration_id"]]
            secret = None
            if integ.get("credential_id"):
                try:
                    cred = check_use(creds.get(integ["credential_id"]), referencing_chain=chain,
                                     slot=ac.CREDENTIAL_SLOT, policy=eff.policy)
                    secret = self.box.decrypt(cred.secret_enc)
                except CredentialError:
                    r = {"success": False, "status": 0, "error": "The API credential is not available."}
                    await ac.log_activity(self.store, integration=integ, endpoint=ep, source="conversation",
                                          result=r, now=now)
                    return integ, ep, r
            r = await ac.execute(integ, ep, params, secret, timeout=ac.CONVERSATION_TIMEOUT)
            await ac.log_activity(self.store, integration=integ, endpoint=ep, source="conversation", result=r, now=now)
            return integ, ep, r

        results = await asyncio.gather(*(run(ep, params) for ep, params in calls))
        await tracker.record(chain, "api_calls", now=now,
                             details={"calls": len(results), "succeeded": sum(1 for _, _, r in results if r["success"])})
        return ac.data_block(results)

    async def _semantic_check(self, chain, eff, provider, provider_name, api_key, result, gate, verdict, tracker,
                              billing_key, now, labels=None):
        """K-H: an additional, fail-closed verification call (never answer text)."""
        from core.knowledge import AnswerVerdict
        from runtime import semantic_verifier as sv
        refused = AnswerVerdict(False, reason=RefusalReason.UNSUPPORTED_CLAIM)
        try:
            reservation = await tracker.reserve_ai_message(chain, now=now,
                                                           key=(billing_key + ":verify") if billing_key else None,
                                                           labels={**(labels or {}), "purpose": "verification"})
        except AlreadyReserved:
            return refused
        if reservation is None:
            await tracker.record(chain, "semantic_verification", now=now, details={"result": "quota"})
            return refused                                   # cannot verify ⇒ fail closed
        claims = tuple(result.claims)
        request = GenerationRequest(
            provider=provider_name, model=eff.values["ai.model"], api_key=api_key, persona="", instructions=sv.INSTRUCTIONS,
            tone_instructions="", language="en", user_text=sv.payload(claims, gate.evidence),
            knowledge_mode="verification", evidence=(), structured_claims_required=False, history=(),
            max_output_tokens=512)
        try:
            out = await provider.generate(request)
        except ProviderUncertain:
            await tracker.mark_uncertain(reservation.key, now=now)
            return refused
        except ProviderError:
            await tracker.release(reservation, now=now)
            await tracker.record(chain, "semantic_verification", now=now, details={"result": "provider_error"})
            return refused
        except Exception:
            await tracker.mark_uncertain(reservation.key, now=now)
            return refused
        check = sv.parse(out.text, len(claims))
        try:
            await tracker.settle(reservation, chain, now=now,
                                 details={"verification": check.reason, "input_tokens": int(out.input_tokens or 0),
                                          "output_tokens": int(out.output_tokens or 0)})
        except Exception:
            pass
        await tracker.record(chain, "semantic_verification", now=now,
                             details={"result": check.reason, "issues": ",".join(check.issues)[:200]})
        return verdict if check.ok else refused

    @staticmethod
    def usage_for(services) -> StoreUsageTracker:
        return StoreUsageTracker(services.store, platform_policy=services.platform_policy)

    async def runtime_config(self, instance_id: str) -> dict:
        """Admin preview of what the pipeline would resolve for an instance
        (no provider call, no secrets)."""
        chain = await self.hierarchy.load_chain(Level.BOT_INSTANCE, instance_id)
        creds = await enterprise_credentials(self.store, chain.enterprise_id)
        eff = resolver.resolve(chain, platform_policy=self.platform_policy, credentials=creds)
        plan = plan_for(eff)
        provider = eff.values.get("ai.provider")
        return {
            "instance_id": instance_id, "channel": chain.instance.channel.type.value,
            "ancestry": chain.ids(), "available": chain.availability().available,
            "unavailable_reason": chain.availability().reason or None,
            "runtime_blockers": list(eff.runtime_blockers), "blocked": dict(eff.blocked),
            "ai": {"provider": provider, "model": eff.values.get("ai.model"),
                   "credential": eff.credentials.get("ai.credential_id"),
                   "provider_available": provider in self.providers},
            "knowledge": {"mode": plan.mode.value, "strict": plan.strict,
                          "approved_sources": len(plan.approved_sources), "require_citations": plan.require_citations,
                          "general_fallback_allowed": plan.general_fallback_allowed,
                          "retrieval_engine": self.retriever is not None},
            "limits": {"end_user_msgs_per_minute": eff.values.get("limits.end_user_msgs_per_minute"),
                       "max_monthly_ai_messages": eff.policy.get("limits.max_monthly_ai_messages")},
            "runnable": eff.runnable and self.retriever is not None if plan.strict else eff.runnable,
        }


__all__ = ["Pipeline", "SILENT"]
