"""Runtime interfaces (spec 012 §10, Phase 4A).

Channel adapters (Discord, Telegram, Webchat; Phase 4B-4D), the Zendesk
integration adapter (4E), AI providers, knowledge retrieval (4K) and usage
tracking all meet the one pipeline through these types. Phase 4A ships only
the interfaces and test doubles; no live service is contacted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Iterable, List, Optional, Protocol, Sequence, Tuple

from core.hierarchy import Chain, ChannelType
from core.knowledge import Claim, Evidence, KnowledgePlan, KnowledgeSource, Requester, RetrievalResult


class Secret:
    """A decrypted credential held in memory for one provider call. ``repr``/``str``
    never show the value; only ``reveal()`` does."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__


# ═════════════════════════════════ channel side ═════════════════════════════════

@dataclass(frozen=True)
class ChannelAddress:
    """Where an inbound event came from, as authenticated by the channel adapter."""
    channel: ChannelType
    # Discord (application_id = the Enterprise application whose gateway received the event)
    application_id: Optional[str] = None
    guild_id: Optional[str] = None
    channel_id: Optional[str] = None
    is_dm: bool = False
    dm_shared_guilds: Tuple[str, ...] = ()          # guilds the DM author shares with the platform bot
    # Telegram
    telegram_bot_id: Optional[str] = None
    instance_hint: Optional[str] = None             # from the (secret-authenticated) webhook path
    # Webchat
    public_id: Optional[str] = None


@dataclass(frozen=True)
class InboundMessage:
    address: ChannelAddress
    text: str
    end_user_id: str                                # channel-scoped opaque id (never used across tenants)
    conversation_key: str = ""
    requester: Requester = field(default_factory=Requester)
    history: Tuple[Tuple[str, str], ...] = ()       # earlier turns: ("user"|"assistant", text), oldest first
    format_hint: str = ""                           # channel output hint, used only for unstructured answers
    passive: bool = False                           # Discord passive mode (D12): stay silent unless confident
    integration_id: Optional[str] = None            # Phase 4E: a Bot Integration (Zendesk) instead of an address


@dataclass(frozen=True)
class Outcome:
    """What the adapter should do. ``text`` is always safe for end users."""
    kind: str               # answer | refusal | fallback | silent | rate_limited | quota_exceeded | busy
    deliver: bool
    text: str = ""
    citations: Tuple[str, ...] = ()
    instance_id: Optional[str] = None
    admin_issue: Optional[str] = None               # category for admins (never shown to end users)
    billed: bool = False                            # a provider generation was reserved/settled for this key
    usage: Tuple[Tuple[str, Any], ...] = ()         # non-secret usage details (provider, tokens, mode)
    integration_id: Optional[str] = None            # set for Bot Integration (Zendesk) requests
    provenance: Tuple[Tuple[str, str, str], ...] = ()   # (source_id, document_id, evidence_id) of cited evidence
    handoff: bool = False                           # D9: insufficient knowledge and human handoff is enabled


class ChannelAdapter(Protocol):
    channel: ChannelType

    async def receive(self, raw: Any) -> Optional[InboundMessage]: ...     # authenticate + normalise
    async def deliver(self, message: InboundMessage, outcome: Outcome) -> None: ...


# ═════════════════════════════════ AI provider ═════════════════════════════════

@dataclass(frozen=True)
class GenerationRequest:
    provider: str
    model: str
    api_key: Secret
    persona: str
    instructions: str
    tone_instructions: str
    language: str
    user_text: str
    knowledge_mode: str
    evidence: Tuple[Evidence, ...] = ()
    structured_claims_required: bool = False        # strict / grounded answers: claims with citations only
    history: Tuple[Tuple[str, str], ...] = ()
    max_output_tokens: int = 1024


@dataclass(frozen=True)
class GenerationResult:
    claims: Tuple[Claim, ...] = ()
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class AIProvider(Protocol):
    name: str

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...


# ═════════════════════════════════ knowledge (4K) ═════════════════════════════════

class KnowledgeCatalog(Protocol):
    async def sources(self, enterprise_id: str) -> Sequence[KnowledgeSource]: ...


class KnowledgeRetriever(Protocol):
    async def retrieve(self, query: str, *, chain: Chain, plan: KnowledgePlan,
                       authorised: frozenset) -> RetrievalResult: ...


class EmptyCatalog:
    """Phase 4A default: no knowledge sources exist yet."""

    async def sources(self, enterprise_id: str) -> List[KnowledgeSource]:
        return []


__all__ = ["Secret", "ChannelAddress", "InboundMessage", "Outcome", "ChannelAdapter", "GenerationRequest",
           "GenerationResult", "AIProvider", "KnowledgeCatalog", "KnowledgeRetriever", "EmptyCatalog"]
