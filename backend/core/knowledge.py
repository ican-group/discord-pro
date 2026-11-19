"""Enterprise AI knowledge control (spec 012 §5) — Phase 2 foundation.

This module defines:

* knowledge-source ownership and scope rules (tenant isolation, access scope,
  approval lists, document-level access);
* :class:`KnowledgePlan`, derived only from the resolver's effective
  configuration and policy;
* the **strict-mode fail-closed runtime contract** that the Phase 4 runtime
  pipeline must implement:

      plan = plan_for(effective)                     # 1. policies resolved
      sources = authorised_sources(plan, chain, catalog, requester)   # 2. scope
      retrieval = <retrieval engine>(query, sources)  # 3. approved sources only
      gate = gate_before_generation(plan, retrieval)  # 4. relevance/sufficiency
      if gate.decision is REFUSE: deliver refusal(plan, gate)
      draft = <model>(constrained to gate.evidence, structured claims+citations)   # 5-6
      verdict = validate_answer(plan, gate, draft)    # 7. support validation
      deliver verdict.text or refusal(plan, verdict)  # 8. refuse / fallback

  Any exception anywhere in steps 2-7 must be passed to :func:`fail_closed`,
  which never lets an unsupported answer through in STRICT_KNOWLEDGE_ONLY mode.

No generative system can guarantee zero hallucinations; these controls make
STRICT_KNOWLEDGE_ONLY answers evidence-bound by construction: the model only
sees approved evidence, its output must be structured claims that each cite
evidence supplied to it, and anything else is refused.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Iterable, List, Optional, Tuple

from core.config_registry import (KNOWLEDGE_SCOPE_ORDER, KnowledgeAccessScope, KnowledgeMode, strictness)
from core.hierarchy import Chain, is_valid_id


# ═════════════════════════════════ sources ═════════════════════════════════

class SourceKind(str, Enum):
    DOCUMENT = "document"
    STRUCTURED = "structured"
    INTEGRATION = "integration"            # an authorised bot integration (e.g. spec 011 API)


class SourceStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"                    # Phase 4K: never retrievable again until re-activated
    ARCHIVED = "archived"


@dataclass(frozen=True)
class KnowledgeSource:
    """A knowledge source owned by an Enterprise, Organisation or Bot."""
    id: str
    enterprise_id: str
    organisation_id: Optional[str]
    bot_id: Optional[str]
    kind: SourceKind
    status: SourceStatus = SourceStatus.ACTIVE
    acl_groups: FrozenSet[str] = frozenset()      # document-level access (empty = no restriction)

    @property
    def owner_scope(self) -> KnowledgeAccessScope:
        if self.bot_id:
            return KnowledgeAccessScope.BOT
        if self.organisation_id:
            return KnowledgeAccessScope.ORGANISATION
        return KnowledgeAccessScope.ENTERPRISE


@dataclass(frozen=True)
class Requester:
    """Who is asking (end user / agent). ``groups`` come from an authenticated
    identity source; anonymous requesters have none."""
    groups: FrozenSet[str] = frozenset()
    authenticated: bool = False


# ═════════════════════════════════ plan ═════════════════════════════════

@dataclass(frozen=True)
class KnowledgePlan:
    mode: KnowledgeMode
    approved_sources: FrozenSet[str]              # explicit approvals (None stored => empty)
    min_evidence_score: float
    require_citations: bool
    general_fallback_allowed: bool                # only meaningful for KNOWLEDGE_PREFERRED
    access_scope: KnowledgeAccessScope
    document_acl_required: bool
    insufficient_fallback: str
    human_handoff: bool

    @property
    def strict(self) -> bool:
        return self.mode is KnowledgeMode.STRICT_KNOWLEDGE_ONLY


def plan_for(effective) -> KnowledgePlan:
    """Build the plan from a ``core.resolver.Effective`` (the only source of truth)."""
    v, p = effective.values, effective.policy
    mode = v["knowledge.mode"]
    if mode is None:                                   # blocked: no permitted mode -> strictest
        mode = KnowledgeMode.STRICT_KNOWLEDGE_ONLY
    approved = v["knowledge.approved_sources"]
    allowed = p.get("knowledge.allowed_sources")
    # X13: deny by default — only sources inside the Enterprise-approved set are usable,
    # whatever is stored lower down (defence in depth on top of write validation).
    approved_set = frozenset(approved or ()) & frozenset(allowed or ())
    return KnowledgePlan(
        mode=mode,
        approved_sources=approved_set,
        min_evidence_score=float(v["knowledge.min_evidence_score"] if v["knowledge.min_evidence_score"] is not None
                                 else max(0.5, p["knowledge.min_evidence_floor"])),
        require_citations=bool(v["knowledge.require_citations"]) or bool(p["knowledge.require_citations"]) or
        mode is KnowledgeMode.STRICT_KNOWLEDGE_ONLY,
        general_fallback_allowed=(mode is KnowledgeMode.KNOWLEDGE_PREFERRED and
                                  p["knowledge.general_fallback_allowed"] is True),
        access_scope=p["knowledge.access_scope"],
        document_acl_required=bool(p["knowledge.document_acl_required"]),
        insufficient_fallback=v["knowledge.insufficient_fallback"] or "",
        human_handoff=bool(v["knowledge.human_handoff"]),
    )


def source_in_scope(source: KnowledgeSource, chain: Chain, scope: KnowledgeAccessScope) -> bool:
    """Tenant isolation + access scope. Never true across Enterprises."""
    if source.enterprise_id != chain.enterprise_id:
        return False
    org = chain.organisation.id if chain.organisation else None
    bot = chain.bot.id if chain.bot else None
    # The source must sit on the bot's own branch of the hierarchy...
    if source.organisation_id and source.organisation_id != org:
        return False
    if source.bot_id and source.bot_id != bot:
        return False
    # ...and must not be wider than the permitted access scope.
    return strictness(source.owner_scope, KNOWLEDGE_SCOPE_ORDER) >= strictness(scope, KNOWLEDGE_SCOPE_ORDER)


def authorised_sources(plan: KnowledgePlan, chain: Chain, catalog: Iterable[KnowledgeSource],
                       requester: Requester) -> FrozenSet[str]:
    """Ids of sources the retrieval engine may query for this request."""
    out = set()
    for s in catalog:
        if not is_valid_id(s.id) or s.status is not SourceStatus.ACTIVE:
            continue
        if s.id not in plan.approved_sources:
            continue
        if not source_in_scope(s, chain, plan.access_scope):
            continue
        if s.acl_groups:
            if not (requester.authenticated and requester.groups & s.acl_groups):
                continue
        elif plan.document_acl_required and s.kind is SourceKind.DOCUMENT:
            continue                                   # policy requires ACLs: unrestricted documents excluded
        out.add(s.id)
    return frozenset(out)


# ═════════════════════════════════ retrieval contract ═════════════════════════════════

class RetrievalStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_id: str
    enterprise_id: str
    score: float                                   # relevance 0..1 (engine-defined, calibrated)
    text: str = ""
    document_id: str = ""                          # provenance (Phase 4K)
    document_version: int = 0
    span: Tuple[int, int] = (0, 0)                 # character offsets of the chunk in the document text


@dataclass(frozen=True)
class RetrievalResult:
    status: RetrievalStatus
    evidence: Tuple[Evidence, ...] = ()
    conflicting: FrozenSet[str] = frozenset()      # evidence ids the engine found contradictory


class Decision(str, Enum):
    GROUNDED = "grounded"         # answer constrained to evidence
    GENERAL = "general"           # ordinary AI answer (GENERAL mode, or permitted fallback)
    REFUSE = "refuse"


class RefusalReason(str, Enum):
    KNOWLEDGE_UNAVAILABLE = "knowledge_unavailable"
    NO_EVIDENCE = "no_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    MISSING_CITATIONS = "missing_citations"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class GenerationGate:
    decision: Decision
    evidence: Tuple[Evidence, ...] = ()
    reason: Optional[RefusalReason] = None

    @property
    def evidence_ids(self) -> FrozenSet[str]:
        return frozenset(e.evidence_id for e in self.evidence)


def gate_before_generation(plan: KnowledgePlan, retrieval: Optional[RetrievalResult], *,
                           authorised: FrozenSet[str], chain: Chain) -> GenerationGate:
    """Steps 3-4: keep only authorised, same-tenant, sufficiently relevant
    evidence, then decide whether generation may proceed."""
    usable: List[Evidence] = []
    if retrieval is not None and retrieval.status is RetrievalStatus.OK:
        for e in retrieval.evidence:
            if e.source_id not in authorised or e.enterprise_id != chain.enterprise_id:
                continue                                   # unauthorised / cross-tenant: excluded silently
            if not (isinstance(e.score, (int, float)) and e.score >= plan.min_evidence_score):
                continue
            usable.append(e)

    def no_knowledge(reason: RefusalReason) -> GenerationGate:
        if plan.mode is KnowledgeMode.GENERAL or plan.general_fallback_allowed:
            return GenerationGate(Decision.GENERAL, tuple(usable))
        return GenerationGate(Decision.REFUSE, reason=reason)

    if plan.mode is KnowledgeMode.GENERAL:
        return GenerationGate(Decision.GENERAL if not usable else Decision.GROUNDED, tuple(usable))
    if retrieval is None or retrieval.status is not RetrievalStatus.OK:
        return no_knowledge(RefusalReason.KNOWLEDGE_UNAVAILABLE)
    if not retrieval.evidence:
        return no_knowledge(RefusalReason.NO_EVIDENCE)
    if not usable:
        return no_knowledge(RefusalReason.INSUFFICIENT_EVIDENCE)
    if retrieval.conflicting & {e.evidence_id for e in usable}:
        # D7 (Phase 4K): contradictory evidence triggers the configured fallback / handoff in
        # every knowledge mode (never a general answer that silently picks a side)
        return GenerationGate(Decision.REFUSE, reason=RefusalReason.CONFLICTING_EVIDENCE)
    return GenerationGate(Decision.GROUNDED, tuple(usable))


# ═════════════════════════════════ answer validation ═════════════════════════════════

@dataclass(frozen=True)
class Claim:
    text: str
    evidence_ids: FrozenSet[str] = frozenset()
    supported: Optional[bool] = None   # verdict of an optional support verifier (None = not checked)
    quote: str = ""                    # Phase 4K: a verbatim span from one of the cited evidence texts


@dataclass(frozen=True)
class AnswerDraft:
    """Structured model output required in GROUNDED mode: every substantive
    statement is a claim with citations. ``uncited_text`` carries any remaining
    free text (greetings are allowed only outside strict mode)."""
    claims: Tuple[Claim, ...] = ()
    uncited_text: str = ""


@dataclass(frozen=True)
class AnswerVerdict:
    deliver: bool
    text: str = ""
    citations: Tuple[str, ...] = ()                    # source ids, traceable
    reason: Optional[RefusalReason] = None
    provenance: Tuple[Tuple[str, str, str], ...] = ()  # (source_id, document_id, evidence_id) per cited evidence
    removed_claims: int = 0                            # unsupported claims dropped (knowledge_preferred only)


# ═════════════════════════════ deterministic claim support (Phase 4K) ═════════════════════════════
#
# Limits (documented): these checks are lexical, not semantic. They prove that a
# claim quotes text that really is in evidence the model was given, that every
# number it states appears in that evidence, and that most of its content words
# come from that evidence. They cannot prove logical entailment (a claim can
# re-combine supported words into an unsupported statement, e.g. by changing
# negation outside the quoted span). They make unsupported or fabricated
# statements detectable and refused; they do not guarantee zero hallucinations.

MIN_QUOTE_CHARS = 12
MIN_SUPPORT_RATIO = 0.6
_STOP = frozenset("""a an and are as at be been but by can could do does for from had has have how i if in into is it
its may might must no not of on or our shall should so such than that the their them then there these they this
those to was we were what when where which who will with would you your yours yes please""".split())
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NEGATION = frozenset({"no", "not", "never", "none", "cannot", "can't", "without", "nor", "neither"})


def normalise_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"[\u2018\u2019\u201b]", "'", text)
    text = re.sub(r"[\u201c\u201d]", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def tokens(text: str) -> List[str]:
    """Normalised content terms (shared by indexing, retrieval and validation)."""
    return [stem(t) for t in re.findall(r"[^\W_]+(?:'[^\W_]+)?", normalise_text(text)) if t not in _STOP]


def numbers(text: str) -> FrozenSet[str]:
    return frozenset(n.replace(",", "") for n in _NUMBER.findall(normalise_text(text)))


# Layer 2 (Phase 4F): alignment with the evidence *sentence* the claim quotes.
ALIGNMENT_CHECKS = True
_MODAL = frozenset({"must", "may", "can", "cannot", "shall", "should", "required", "require", "requires",
                    "optional", "mandatory", "prohibited", "permitted", "allowed", "eligible", "only", "need"})
_CONDITION = frozenset({"unless", "except", "excluding", "exclude", "if", "provided", "only", "within", "after",
                        "before", "least", "most", "over", "under", "above", "below", "until", "maximum",
                        "minimum", "later", "earlier", "approval", "approved"})
_MONTHS = frozenset("january february march april may june july august september october november december".split())


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+", text) if s.strip()]


def _qualifiers(text: str) -> FrozenSet[str]:
    words = set(normalise_text(text).replace("'", " ").split())
    words = {w.strip('.,;:!?()"') for w in words}
    out = (words & (_MODAL | _CONDITION | _NEGATION | _MONTHS)) | {"#" + n for n in numbers(text)}
    if re.search(r"[€$£]", text):
        out |= {"#currency"}
    return frozenset(out)


def sentence_aligned(claim: Claim, cited: Iterable[Evidence]) -> bool:
    """Every qualifier the claim states (numbers, currency, dates, negations,
    modality/permission, conditions/exceptions) must appear in the anchor
    sentence(s) — the evidence sentences containing the quote — and the claim
    must not drop the anchor sentence's own conditions, exceptions, negations,
    modals or the numbers inside the quote. Most content words must come from
    the anchor sentence itself (not from elsewhere in the evidence)."""
    quote = normalise_text(claim.quote)
    anchors = [s for e in cited for s in _sentences(e.text) if quote and quote in normalise_text(s)]
    if not anchors:
        # a quote spanning sentences: the sentences it touches
        anchors = [s for e in cited for s in _sentences(e.text)
                   if any(part and part in normalise_text(s) for part in quote.split(". "))]
    if not anchors:
        return False
    anchor = " ".join(anchors)
    claim_q, anchor_q = _qualifiers(claim.text), _qualifiers(anchor)
    if not claim_q <= anchor_q:
        return False                                          # a qualifier the sentence does not state
    keep = {q for q in anchor_q if q in _NEGATION or q in _CONDITION or q in _MODAL} | \
           {"#" + n for n in numbers(claim.quote)}
    if not keep <= claim_q:
        return False                                          # a condition / exception / limit was dropped
    claim_terms = set(tokens(claim.text))
    if claim_terms and len(claim_terms & set(tokens(anchor))) / len(claim_terms) < 0.7:
        return False                                          # content assembled from elsewhere
    return True


def claim_supported(claim: Claim, cited: Iterable[Evidence]) -> bool:
    cited = list(cited)
    if not cited:
        return False
    quote = normalise_text(claim.quote)
    if len(quote) < MIN_QUOTE_CHARS or not any(quote in normalise_text(e.text) for e in cited):
        return False                                          # fabricated or paraphrased "quote"
    evidence_text = " ".join(e.text for e in cited)
    if not numbers(claim.text) <= numbers(evidence_text):
        return False                                          # a number the evidence does not state
    claim_terms = set(tokens(claim.text))
    if claim_terms:
        support = len(claim_terms & set(tokens(evidence_text))) / len(claim_terms)
        if support < MIN_SUPPORT_RATIO:
            return False
    if (set(normalise_text(claim.text).split()) & _NEGATION) and \
            not (set(normalise_text(evidence_text).split()) & _NEGATION):
        return False                                          # a negation the evidence never makes
    if ALIGNMENT_CHECKS and not sentence_aligned(claim, cited):
        return False
    return True


def validate_answer(plan: KnowledgePlan, gate: GenerationGate, draft: AnswerDraft) -> AnswerVerdict:
    """Step 7, after generation and before delivery. Every claim must cite
    evidence that was supplied to the model *and* be deterministically
    supported by it (verbatim quote, numbers, lexical support, negation).
    Strict mode refuses the whole answer on any violation (no partial
    answers); knowledge-preferred drops unsupported claims and refuses if none
    remain. Unknown evidence ids (fabricated citations) refuse in every
    grounded mode."""
    if gate.decision is Decision.REFUSE:
        return AnswerVerdict(False, reason=gate.reason)
    if gate.decision is Decision.GENERAL:
        text = " ".join([c.text for c in draft.claims] + ([draft.uncited_text] if draft.uncited_text else []))
        return AnswerVerdict(True, text.strip())
    supplied = gate.evidence_ids
    by_id = {e.evidence_id: e for e in gate.evidence}
    if not draft.claims:
        return AnswerVerdict(False, reason=RefusalReason.UNSUPPORTED_CLAIM)
    kept: List[Claim] = []
    removed = 0
    for c in draft.claims:
        if not c.evidence_ids:
            if plan.require_citations or plan.strict:
                return AnswerVerdict(False, reason=RefusalReason.MISSING_CITATIONS)
            removed += 1
            continue
        if not c.evidence_ids <= supplied:
            return AnswerVerdict(False, reason=RefusalReason.UNSUPPORTED_CLAIM)      # fabricated citation
        ok = c.supported is not False and claim_supported(c, (by_id[i] for i in c.evidence_ids))
        if not ok:
            if plan.strict:
                return AnswerVerdict(False, reason=RefusalReason.UNSUPPORTED_CLAIM)
            removed += 1
            continue
        kept.append(c)
    if not kept:
        return AnswerVerdict(False, reason=RefusalReason.UNSUPPORTED_CLAIM)
    if plan.strict and draft.uncited_text.strip():
        return AnswerVerdict(False, reason=RefusalReason.UNSUPPORTED_CLAIM)
    cited_ids = [eid for c in kept for eid in sorted(c.evidence_ids)]
    citations = tuple(dict.fromkeys(by_id[eid].source_id for eid in cited_ids))
    provenance = tuple(dict.fromkeys((by_id[eid].source_id, by_id[eid].document_id, eid) for eid in cited_ids))
    text = " ".join(c.text for c in kept)
    if draft.uncited_text.strip() and not plan.strict:
        text = f"{text} {draft.uncited_text.strip()}"
    return AnswerVerdict(True, text.strip(), citations, provenance=provenance, removed_claims=removed)


def refusal(plan: KnowledgePlan, reason: Optional[RefusalReason]) -> AnswerVerdict:
    """End-user response for a refusal: the configured fallback, never internal detail."""
    return AnswerVerdict(False, plan.insufficient_fallback, (), reason)


def fail_closed(plan: KnowledgePlan, exc: BaseException) -> AnswerVerdict:
    """Any failure in retrieval, generation or validation. Strict and
    knowledge-preferred-without-fallback modes refuse; nothing is guessed."""
    return refusal(plan, RefusalReason.INTERNAL_ERROR)


__all__ = ["SourceKind", "SourceStatus", "KnowledgeSource", "Requester", "KnowledgePlan", "plan_for",
           "source_in_scope", "authorised_sources", "RetrievalStatus", "Evidence", "RetrievalResult",
           "Decision", "RefusalReason", "GenerationGate", "gate_before_generation", "Claim", "AnswerDraft",
           "AnswerVerdict", "validate_answer", "refusal", "fail_closed", "tokens", "numbers", "normalise_text",
           "claim_supported", "stem", "MIN_QUOTE_CHARS", "sentence_aligned"]
