"""The single configuration and policy registry (spec 012 §3-5).

Every inheritable setting and every enforced policy is declared here once. The
resolver (``core.resolver``) and every future writer/reader use only this
registry; there are no per-integration configuration implementations.

Settings (``Property``) flow Enterprise -> Organisation -> Bot -> Bot Instance.
A child stores only explicit overrides. Policies (``PolicyKey``) are set at the
platform, Enterprise and Organisation levels and may only become stricter.

Security classes:
* ``PUBLIC``          may be shown to anonymous end users (widget text);
* ``INTERNAL``        administrators only;
* ``CREDENTIAL_REF``  the id of a credential, never a secret value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from core.hierarchy import Level, is_valid_id


E, O, B, I = Level.ENTERPRISE, Level.ORGANISATION, Level.BOT, Level.BOT_INSTANCE
ALL_LEVELS = frozenset({E, O, B, I})


class ConfigError(ValueError):
    """Invalid configuration value. ``str(exc)`` is safe to show to admins."""


class SecurityClass(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CREDENTIAL_REF = "credential_ref"


class Combine(str, Enum):
    """How a policy combines down the tree (child can only tighten)."""
    INTERSECT = "intersect"         # allowed sets; None = unrestricted
    MIN = "min"                     # maximum limits; None = unlimited
    MAX = "max"                     # minimum floors
    ALLOW = "allow"                 # permission booleans: AND (False sticks)
    REQUIRE = "require"             # mandatory controls: OR (True sticks)
    STRICTEST = "strictest"         # ordered enums: the stricter value wins


# ═════════════════════════════════ knowledge enums ═════════════════════════════════

class KnowledgeMode(str, Enum):
    GENERAL = "general"
    KNOWLEDGE_PREFERRED = "knowledge_preferred"
    STRICT_KNOWLEDGE_ONLY = "strict_knowledge_only"


KNOWLEDGE_MODE_ORDER = (KnowledgeMode.GENERAL, KnowledgeMode.KNOWLEDGE_PREFERRED, KnowledgeMode.STRICT_KNOWLEDGE_ONLY)


class KnowledgeAccessScope(str, Enum):
    """Widest scope from which a bot may draw knowledge sources (narrower = stricter)."""
    ENTERPRISE = "enterprise"
    ORGANISATION = "organisation"
    BOT = "bot"


KNOWLEDGE_SCOPE_ORDER = (KnowledgeAccessScope.ENTERPRISE, KnowledgeAccessScope.ORGANISATION, KnowledgeAccessScope.BOT)


def strictness(value: Any, order: Tuple[Any, ...]) -> int:
    return order.index(value)


# ═════════════════════════════════ validators ═════════════════════════════════

def v_text(limit: int) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str) or len(value) > limit:
            raise ConfigError(f"must be text of at most {limit} characters")
        if any(ord(c) < 0x20 and c not in "\n\t" for c in value):
            raise ConfigError("must not contain control characters")
        return value
    return check


def v_enum(enum_cls) -> Callable[[Any], Any]:
    def check(value: Any):
        try:
            return enum_cls(value)
        except ValueError:
            raise ConfigError("must be one of " + ", ".join(m.value for m in enum_cls))
    return check


def v_choice(*choices: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in choices:
            raise ConfigError("must be one of " + ", ".join(choices))
        return value
    return check


def v_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("must be true or false")
    return value


def v_int(lo: int, hi: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ConfigError(f"must be an integer between {lo} and {hi}")
        return value
    return check


def v_float(lo: float, hi: float) -> Callable[[Any], float]:
    def check(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= float(value) <= hi:
            raise ConfigError(f"must be a number between {lo} and {hi}")
        return float(value)
    return check


def v_id_set(limit: int) -> Callable[[Any], FrozenSet[str]]:
    def check(value: Any) -> FrozenSet[str]:
        if not isinstance(value, (list, tuple, set, frozenset)) or len(value) > limit:
            raise ConfigError(f"must be a list of at most {limit} ids")
        if not all(is_valid_id(v) for v in value):
            raise ConfigError("contains an invalid id")
        return frozenset(value)
    return check


def v_str_set(choices: Tuple[str, ...]) -> Callable[[Any], FrozenSet[str]]:
    def check(value: Any) -> FrozenSet[str]:
        if not isinstance(value, (list, tuple, set, frozenset)) or not set(value) <= set(choices):
            raise ConfigError("must be a subset of " + ", ".join(choices))
        return frozenset(value)
    return check


_BCP47 = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")
_ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}")
_MODEL = re.compile(r"[a-z0-9][a-z0-9.:_-]{0,79}")


def v_language(value: Any) -> str:
    if not isinstance(value, str) or not _BCP47.fullmatch(value):
        raise ConfigError("must be a language tag such as en or en-GB")
    return value


def v_credential_ref(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not is_valid_id(value):
        raise ConfigError("must reference a credential")
    return value


def v_model(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise ConfigError("must be a model identifier")
    return value


def v_model_set(value: Any) -> FrozenSet[str]:
    if not isinstance(value, (list, tuple, set, frozenset)) or len(value) > 100:
        raise ConfigError("must be a list of at most 100 model ids")
    return frozenset(v_model(m) for m in value)


def v_tone_examples(value: Any) -> Tuple[Dict[str, str], ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 10:
        raise ConfigError("must be a list of at most 10 examples")
    out = []
    for ex in value:
        if not isinstance(ex, dict) or set(ex) != {"user", "bot"}:
            raise ConfigError("each example needs 'user' and 'bot'")
        out.append({"user": v_text(500)(ex["user"]), "bot": v_text(1500)(ex["bot"])})
    return tuple(out)


THEME_KEYS = ("accent", "accent_light", "background", "surface", "panel", "text", "text_secondary",
              "border", "user_bubble", "bot_bubble", "input_background", "radius", "font")


def v_theme(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict) or not set(value) <= set(THEME_KEYS):
        raise ConfigError("unknown theme key")
    out = {}
    for k, v in value.items():
        if k == "font":
            if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9 ,'-]{1,80}", v):
                raise ConfigError("invalid font")
        elif k == "radius":
            if not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,2}px", v):
                raise ConfigError("radius must look like 12px")
        elif not isinstance(v, str) or not _HEX_COLOR.fullmatch(v):
            raise ConfigError(f"{k} must be a hex colour")
        out[k] = v
    return out


def v_https_url(value: Any) -> str:
    if value == "":
        return ""
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith("https://") \
            or any(c in value for c in " \t\r\n\"'<>\\"):
        raise ConfigError("must be an https URL")
    return value


def v_origin_set(value: Any) -> Optional[FrozenSet[str]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set, frozenset)) or len(value) > 100:
        raise ConfigError("must be a list of at most 100 https origins")
    out = frozenset(v.strip().lower().rstrip("/") for v in value if isinstance(v, str))
    if len(out) != len(value) or not all(_ORIGIN.fullmatch(o) for o in out):
        raise ConfigError("must contain https origins (scheme://host[:port])")
    return out


# ═════════════════════════════════ platform constants ═════════════════════════════════

AI_PROVIDERS = ("anthropic", "openai", "ollama")
MODEL_ALLOWLIST: Dict[str, Tuple[str, ...]] = {
    "anthropic": ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-opus-4-5", "claude-opus-4-8"),
    "openai": ("gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-5", "gpt-5-mini", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "o3", "o4-mini"),
    "ollama": (),   # any locally installed model; Ollama itself is platform-allowlisted (spec 010)
}
DEFAULT_MODEL = {"anthropic": "claude-sonnet-4-6", "openai": "gpt-4o", "ollama": "llama3.1"}
CHANNELS = ("discord", "telegram", "webchat")
INTEGRATIONS = ("zendesk", "api")


# ═════════════════════════════════ policies ═════════════════════════════════

@dataclass(frozen=True)
class PolicyKey:
    name: str
    combine: Combine
    validator: Callable[[Any], Any]
    unset_default: Any                    # effective value when no level sets it
    levels: FrozenSet[Level] = frozenset({E, O})
    order: Tuple[Any, ...] = ()           # for STRICTEST, least -> most strict
    description: str = ""


def _set_or_none(inner: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def check(value: Any):
        return None if value is None else inner(value)
    return check


POLICIES: Dict[str, PolicyKey] = {p.name: p for p in (
    PolicyKey("ai.allowed_providers", Combine.INTERSECT, _set_or_none(v_str_set(AI_PROVIDERS)), None,
              description="AI providers descendants may use"),
    PolicyKey("ai.allowed_models", Combine.INTERSECT, _set_or_none(v_model_set), None),
    PolicyKey("channels.allowed", Combine.INTERSECT, _set_or_none(v_str_set(CHANNELS)), None),
    PolicyKey("integrations.allowed", Combine.INTERSECT, _set_or_none(v_str_set(INTEGRATIONS)), None),
    PolicyKey("tools.external_api_calls_allowed", Combine.ALLOW, v_bool, True),
    PolicyKey("webchat.public_access_allowed", Combine.ALLOW, v_bool, True),
    PolicyKey("webchat.allowed_embed_origins", Combine.INTERSECT, v_origin_set, None),
    # Knowledge control (spec 012 §5)
    PolicyKey("knowledge.minimum_mode", Combine.STRICTEST, v_enum(KnowledgeMode), KnowledgeMode.GENERAL,
              order=KNOWLEDGE_MODE_ORDER, description="descendants cannot select a less strict knowledge mode"),
    PolicyKey("knowledge.general_fallback_allowed", Combine.ALLOW, v_bool, False,
              description="whether KNOWLEDGE_PREFERRED may fall back to general AI knowledge"),
    # X13 (approved 2026-11-18): deny by default. Only sources the Enterprise lists here
    # can be approved anywhere in its tree; unset and explicit empty both mean none.
    PolicyKey("knowledge.allowed_sources", Combine.INTERSECT, _set_or_none(v_id_set(1000)), frozenset(),
              description="Enterprise-approved knowledge sources (deny by default); descendants may only narrow"),
    PolicyKey("knowledge.access_scope", Combine.STRICTEST, v_enum(KnowledgeAccessScope), KnowledgeAccessScope.ENTERPRISE,
              order=KNOWLEDGE_SCOPE_ORDER, description="widest scope sources may come from"),
    PolicyKey("knowledge.min_evidence_floor", Combine.MAX, v_float(0.0, 1.0), 0.0),
    PolicyKey("knowledge.require_citations", Combine.REQUIRE, v_bool, False),
    PolicyKey("knowledge.semantic_verification", Combine.REQUIRE, v_bool, False,
              description="K-H (experimental): a second model call verifies grounded claims; fails closed"),
    PolicyKey("knowledge.document_acl_required", Combine.REQUIRE, v_bool, False,
              description="retrieval must enforce document-level access lists"),
    # Security, limits, retention
    PolicyKey("security.require_webhook_signatures", Combine.REQUIRE, v_bool, False),
    PolicyKey("zendesk.public_replies_allowed", Combine.ALLOW, v_bool, True,
              description="Phase 4E: whether Zendesk integrations may post public comments (auto_reply)"),
    PolicyKey("limits.max_end_user_msgs_per_minute", Combine.MIN, _set_or_none(v_int(1, 10_000)), None),
    PolicyKey("limits.max_monthly_ai_messages", Combine.MIN, _set_or_none(v_int(0, 100_000_000)), None),
    PolicyKey("retention.conversation_days", Combine.MIN, _set_or_none(v_int(1, 3650)), None),
    # Credentials and delegation (Enterprise only)
    PolicyKey("credentials.allow_enterprise_credentials", Combine.ALLOW, v_bool, True, levels=frozenset({E}),
              description="whether bots may use Enterprise-owned credentials"),
    PolicyKey("delegation.org_admins_manage_members", Combine.ALLOW, v_bool, True, levels=frozenset({E})),
    PolicyKey("delegation.org_admins_manage_credentials", Combine.ALLOW, v_bool, True, levels=frozenset({E})),
    PolicyKey("delegation.bot_admins_manage_members", Combine.ALLOW, v_bool, True, levels=frozenset({E})),
)}

# Platform policy: the first level of every policy chain (cannot be weakened by tenants).
PLATFORM_POLICY: Dict[str, Any] = {
    "ai.allowed_providers": frozenset({"anthropic", "openai"}),   # + "ollama" only if OLLAMA_ALLOWED_BASE_URLS is set
    "security.require_webhook_signatures": True,                  # D-Q10
    "limits.max_end_user_msgs_per_minute": 60,
    "retention.conversation_days": 730,
}


def combine(key: PolicyKey, current: Any, new: Any) -> Any:
    """Tighten ``current`` with ``new`` (None = not set)."""
    if new is None:
        return current
    if current is None:
        return new
    c = key.combine
    if c is Combine.INTERSECT:
        return frozenset(current) & frozenset(new)
    if c is Combine.MIN:
        return min(current, new)
    if c is Combine.MAX:
        return max(current, new)
    if c is Combine.ALLOW:
        return bool(current) and bool(new)
    if c is Combine.REQUIRE:
        return bool(current) or bool(new)
    if c is Combine.STRICTEST:
        return new if strictness(new, key.order) >= strictness(current, key.order) else current
    raise ConfigError("unknown combine rule")


def is_tighter_or_equal(key: PolicyKey, parent_effective: Any, child: Any) -> bool:
    """A policy write is valid only if it does not weaken the parent's effective policy."""
    if child is None or parent_effective is None:
        return True
    return combine(key, parent_effective, child) == child


# ═════════════════════════════════ properties ═════════════════════════════════

Constraint = Callable[[Any, Dict[str, Any]], Optional[str]]   # (value, effective policy) -> violation or None


def _c_in_set(policy_name: str) -> Constraint:
    def check(value, pol):
        allowed = pol.get(policy_name)
        if allowed is not None and value not in allowed:
            return f"not permitted by policy {policy_name}"
        return None
    return check


def _c_subset(policy_name: str) -> Constraint:
    def check(value, pol):
        allowed = pol.get(policy_name)
        if allowed is not None and value is not None and not set(value) <= set(allowed):
            return f"includes sources not permitted by policy {policy_name}"
        return None
    return check


def _c_mode(value, pol):
    floor = pol.get("knowledge.minimum_mode") or KnowledgeMode.GENERAL
    if strictness(value, KNOWLEDGE_MODE_ORDER) < strictness(floor, KNOWLEDGE_MODE_ORDER):
        return f"policy requires at least {floor.value}"
    return None


def _c_min_floor(policy_name: str) -> Constraint:
    def check(value, pol):
        floor = pol.get(policy_name)
        if floor is not None and value < floor:
            return f"must be at least {floor} (policy {policy_name})"
        return None
    return check


def _c_max_limit(policy_name: str) -> Constraint:
    def check(value, pol):
        limit = pol.get(policy_name)
        if limit is not None and value > limit:
            return f"must be at most {limit} (policy {policy_name})"
        return None
    return check


def _c_required_true(policy_name: str) -> Constraint:
    def check(value, pol):
        if pol.get(policy_name) and not value:
            return f"required by policy {policy_name}"
        return None
    return check


def _c_allowed_flag(policy_name: str) -> Constraint:
    def check(value, pol):
        if value and pol.get(policy_name) is False:
            return f"not permitted by policy {policy_name}"
        return None
    return check


@dataclass(frozen=True)
class Property:
    name: str
    default: Any
    levels: FrozenSet[Level]                      # where it may be set (override)
    validator: Callable[[Any], Any]
    security: SecurityClass = SecurityClass.INTERNAL
    inheritable: bool = True
    min_role: str = "editor"                      # role needed to override (core.authz.Role name)
    constraints: Tuple[Constraint, ...] = ()
    narrowing: bool = False                       # set values: an override must be a subset of the inherited value
    policy_fallback: Optional[Callable[[Dict[str, Any]], Any]] = None   # safe value when nothing complies
    description: str = ""


PROPERTIES: Dict[str, Property] = {p.name: p for p in (
    # AI provider
    Property("ai.provider", "anthropic", frozenset({E, O, B}), v_choice(*AI_PROVIDERS),
             constraints=(_c_in_set("ai.allowed_providers"),), min_role="admin"),
    Property("ai.model", None, frozenset({E, O, B}), _set_or_none(v_model),
             constraints=(_c_in_set("ai.allowed_models"),), min_role="admin",
             description="None = provider default; must belong to the effective provider"),
    Property("ai.credential_id", None, frozenset({E, O, B}), v_credential_ref,
             security=SecurityClass.CREDENTIAL_REF, min_role="admin"),
    # Behaviour
    Property("behaviour.persona", "You are a helpful, accurate assistant.", frozenset({B, I}), v_text(4000)),
    Property("behaviour.instructions", "", frozenset({B, I}), v_text(8000)),
    Property("behaviour.tone_instructions", "", frozenset({B, I}), v_text(4000)),
    Property("behaviour.tone_examples", (), frozenset({B}), v_tone_examples),
    Property("behaviour.language", "en", ALL_LEVELS, v_language),
    Property("behaviour.fallback_message", "Sorry, I can't help with that right now.", ALL_LEVELS, v_text(1000),
             security=SecurityClass.PUBLIC),
    # Knowledge control (spec 012 §5)
    Property("knowledge.mode", KnowledgeMode.GENERAL, ALL_LEVELS, v_enum(KnowledgeMode),
             constraints=(_c_mode,), min_role="admin",
             policy_fallback=lambda pol: pol.get("knowledge.minimum_mode") or KnowledgeMode.STRICT_KNOWLEDGE_ONLY),
    Property("knowledge.approved_sources", None, ALL_LEVELS, _set_or_none(v_id_set(1000)),
             constraints=(_c_subset("knowledge.allowed_sources"),), narrowing=True, min_role="admin",
             description="None = not restricted at this level (runtime treats as nothing approved)"),
    Property("knowledge.min_evidence_score", 0.5, ALL_LEVELS, v_float(0.0, 1.0),
             constraints=(_c_min_floor("knowledge.min_evidence_floor"),), min_role="admin",
             policy_fallback=lambda pol: max(0.5, pol.get("knowledge.min_evidence_floor") or 0.0)),
    Property("knowledge.require_citations", False, ALL_LEVELS, v_bool,
             constraints=(_c_required_true("knowledge.require_citations"),), min_role="admin",
             policy_fallback=lambda pol: True),
    Property("knowledge.insufficient_fallback",
             "I'm sorry, I can't answer that from the information I have been given.", ALL_LEVELS, v_text(1000),
             security=SecurityClass.PUBLIC),
    Property("knowledge.human_handoff", False, ALL_LEVELS, v_bool),
    # Tools, limits
    Property("tools.api_calls_enabled", False, frozenset({B, I}), v_bool,
             constraints=(_c_allowed_flag("tools.external_api_calls_allowed"),), min_role="admin"),
    Property("limits.end_user_msgs_per_minute", 10, ALL_LEVELS, v_int(1, 10_000),
             constraints=(_c_max_limit("limits.max_end_user_msgs_per_minute"),), min_role="admin"),
    # Webchat appearance (instances of type webchat; Bot sets defaults)
    Property("widget.title", "", frozenset({B, I}), v_text(80), security=SecurityClass.PUBLIC),
    Property("widget.welcome_message", "", frozenset({B, I}), v_text(2000), security=SecurityClass.PUBLIC),
    Property("widget.theme", {}, frozenset({B, I}), v_theme, security=SecurityClass.PUBLIC),
    Property("widget.default_open", False, frozenset({B, I}), v_bool, security=SecurityClass.PUBLIC),
    Property("widget.custom_css_url", "", frozenset({B, I}), v_https_url, security=SecurityClass.PUBLIC),
)}

# X2 (closed in Phase 4E): settings a Zendesk Bot Integration may override on top
# of its Bot's effective values. Same validators, constraints and narrowing
# rules as everywhere else (core.resolver.resolve_integration); policies are
# never set on integrations, so Enterprise restrictions cannot be weakened.
INTEGRATION_PROPERTIES = frozenset({
    "behaviour.persona", "behaviour.instructions", "behaviour.tone_instructions", "behaviour.language",
    "behaviour.fallback_message", "knowledge.mode", "knowledge.approved_sources", "knowledge.min_evidence_score",
    "knowledge.require_citations", "knowledge.insufficient_fallback", "knowledge.human_handoff",
})

ROLE_ORDER = ("viewer", "editor", "admin", "owner")


def get_property(name: str) -> Property:
    try:
        return PROPERTIES[name]
    except KeyError:
        raise ConfigError(f"unknown setting {name}")


def get_policy(name: str) -> PolicyKey:
    try:
        return POLICIES[name]
    except KeyError:
        raise ConfigError(f"unknown policy {name}")


def registry_table() -> list:
    """Human-readable registry (docs, admin UI)."""
    rows = []
    for p in PROPERTIES.values():
        rows.append({"name": p.name, "default": p.default,
                     "inheritable": p.inheritable, "overridable_at": sorted(level.value for level in p.levels),
                     "security": p.security.value, "min_role": p.min_role,
                     "policies": [c.__name__ if hasattr(c, "__name__") else "constraint" for c in p.constraints],
                     "narrowing": p.narrowing})
    return rows

