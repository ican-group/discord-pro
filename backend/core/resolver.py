"""The single effective-configuration resolver (spec 012 §3).

``resolve(chain)`` computes, for the target of a verified hierarchy chain:

1. effective policy (platform -> Enterprise -> Organisation, tighten-only);
2. effective configuration values and the source of each value;
3. conflicts (invalid or non-compliant stored overrides are ignored and reported);
4. blocked capabilities (e.g. no permitted AI provider or credential);
5. lifecycle availability (the target and every ancestor must be ACTIVE);
6. channel/credential checks for Bot Instances;
7. which settings may be overridden at the target (and by the actor, if given).

Writes are validated by :func:`validate_override` / :func:`apply_override_patch`
and :func:`validate_policy_write` / :func:`apply_policy_patch`, which use the
same registry. Effective values are never written to descendants.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core import config_registry as reg
from core.authz import Action, Actor, Membership, Role, can, effective_role
from core.config_registry import ConfigError, Property, PolicyKey, SecurityClass
from core.credentials import Credential, CredentialError, check_use
from core.hierarchy import Chain, ChannelType, Level, Node
from core.lifecycle import Availability, is_writable


@dataclass(frozen=True)
class Source:
    kind: str                            # "override" | "default" | "platform" | "policy"
    level: Optional[str] = None
    object_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "level": self.level, "object_id": self.object_id}


DEFAULT = Source("default")
PLATFORM = Source("platform")
POLICY = Source("policy")


def _src(node: Node) -> Source:
    return Source("override", node.level.value, node.id)


@dataclass(frozen=True)
class Conflict:
    key: str
    level: str
    object_id: str
    reason: str


@dataclass(frozen=True)
class Effective:
    target_level: str
    target_id: str
    availability: Availability
    values: Dict[str, Any]
    sources: Dict[str, Source]
    policy: Dict[str, Any]
    policy_sources: Dict[str, Source]
    conflicts: Tuple[Conflict, ...]
    blocked: Dict[str, str]                       # capability -> reason
    runtime_blockers: Tuple[str, ...]             # channel/integration issues (instances)
    overridable: Dict[str, bool]                  # at the target (and for the actor, if given)
    credentials: Dict[str, Dict[str, Any]]        # credential_ref values -> public view only
    permissions: Optional[Dict[str, bool]] = None
    role: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.availability.available

    @property
    def runnable(self) -> bool:
        """Available and nothing blocks runtime use (instances)."""
        return self.available and not self.runtime_blockers and "ai" not in self.blocked

    def public_values(self) -> Dict[str, Any]:
        """Only settings classed PUBLIC (safe for anonymous widget clients)."""
        return {k: v for k, v in self.values.items()
                if k in reg.PROPERTIES and reg.PROPERTIES[k].security is SecurityClass.PUBLIC}


# ═════════════════════════════════ policy ═════════════════════════════════

def _policy_nodes(chain: Chain, upto: Optional[Level] = None) -> List[Node]:
    nodes = [n for n in chain.nodes() if n.level in (Level.ENTERPRISE, Level.ORGANISATION)]
    if upto is not None:
        nodes = [n for n in nodes if reg_level_index(n.level) <= reg_level_index(upto)]
    return nodes


def reg_level_index(level: Level) -> int:
    return (Level.ENTERPRISE, Level.ORGANISATION, Level.BOT, Level.BOT_INSTANCE).index(level)


def effective_policy(nodes: Iterable[Node], platform_policy: Mapping[str, Any]
                     ) -> Tuple[Dict[str, Any], Dict[str, Source], List[Conflict]]:
    values: Dict[str, Any] = {}
    sources: Dict[str, Source] = {}
    conflicts: List[Conflict] = []
    nodes = list(nodes)
    for name, key in reg.POLICIES.items():
        value = platform_policy.get(name)
        src = PLATFORM if value is not None else DEFAULT
        for node in nodes:
            if node.level is Level.ORGANISATION and value is None:
                # The Enterprise is authoritative: its effective value (including the
                # unset default) is what an Organisation may tighten.
                value = key.unset_default
            raw_policy = getattr(node, "policy", {}) or {}
            if name not in raw_policy:
                continue
            if node.level not in key.levels:
                conflicts.append(Conflict(name, node.level.value, node.id, "policy not settable at this level"))
                continue
            try:
                new = key.validator(raw_policy[name])
            except ConfigError as exc:
                conflicts.append(Conflict(name, node.level.value, node.id, f"invalid stored policy: {exc}"))
                continue
            if not reg.is_tighter_or_equal(key, value, new):
                conflicts.append(Conflict(name, node.level.value, node.id,
                                          "weaker than the inherited policy; the inherited policy applies"))
            combined = reg.combine(key, value, new)
            if combined != value:
                src = _src(node)
            value = combined
        if value is None:
            value = key.unset_default
        values[name] = value
        sources[name] = src
    for node in nodes:
        for name in (getattr(node, "policy", {}) or {}):
            if name not in reg.POLICIES:
                conflicts.append(Conflict(name, node.level.value, node.id, "unknown policy ignored"))
    return values, sources, conflicts


# ═════════════════════════════════ values ═════════════════════════════════

def _violations(prop: Property, value: Any, policy: Mapping[str, Any]) -> List[str]:
    out = []
    for check in prop.constraints:
        msg = check(value, policy)
        if msg:
            out.append(msg)
    return out


def _resolve_property(prop: Property, nodes: List[Node], policy: Mapping[str, Any]
                      ) -> Tuple[Any, Source, List[Conflict], Optional[str]]:
    """Top-down: build the candidate list (default, then each valid override),
    then take the most specific candidate that complies with the policy."""
    conflicts: List[Conflict] = []
    candidates: List[Tuple[Any, Source, Optional[Node]]] = [(prop.default, DEFAULT, None)]
    current = prop.default
    for node in nodes:
        overrides = node.overrides or {}
        if prop.name not in overrides:
            continue
        if node.level not in prop.levels:
            conflicts.append(Conflict(prop.name, node.level.value, node.id, "not overridable at this level"))
            continue
        try:
            value = prop.validator(overrides[prop.name])
        except ConfigError as exc:
            conflicts.append(Conflict(prop.name, node.level.value, node.id, f"invalid stored value: {exc}"))
            continue
        if prop.narrowing and current is not None and value is not None and not set(value) <= set(current):
            conflicts.append(Conflict(prop.name, node.level.value, node.id, "broadens the inherited value"))
            continue
        candidates.append((value, _src(node), node))
        current = value
    for value, src, node in reversed(candidates):
        problems = _violations(prop, value, policy)
        if not problems:
            return value, src, conflicts, None
        if node is not None:
            conflicts.append(Conflict(prop.name, node.level.value, node.id, "; ".join(problems)))
    if prop.policy_fallback is not None:
        # Tighten-only settings clamp to the policy-mandated value (always safe).
        value = prop.policy_fallback(policy)
        if not _violations(prop, value, policy):
            return value, POLICY, conflicts, None
    return None, Source("blocked"), conflicts, f"no permitted value ({'; '.join(_violations(prop, prop.default, policy))})"


def resolve(chain: Chain, *, platform_policy: Mapping[str, Any] = reg.PLATFORM_POLICY,
            credentials: Mapping[str, Credential] = (), actor: Optional[Actor] = None,
            memberships: Iterable[Membership] = ()) -> Effective:
    credentials = dict(credentials or {})
    memberships = list(memberships)
    nodes = chain.nodes()
    target = chain.target
    policy, policy_sources, conflicts = effective_policy(_policy_nodes(chain), platform_policy)

    values: Dict[str, Any] = {}
    sources: Dict[str, Source] = {}
    blocked: Dict[str, str] = {}
    for name, prop in reg.PROPERTIES.items():
        value, src, prop_conflicts, block = _resolve_property(prop, nodes, policy)
        conflicts.extend(prop_conflicts)
        values[name] = value
        sources[name] = src
        if block:
            blocked[name] = block
    for node in nodes:
        for name in (node.overrides or {}):
            if name not in reg.PROPERTIES:
                conflicts.append(Conflict(name, node.level.value, node.id, "unknown setting ignored"))

    # ── cross-property rules: AI provider, model and credential ──
    provider = values.get("ai.provider")
    if provider is None:
        blocked["ai"] = "no AI provider is permitted by policy"
    else:
        model = values.get("ai.model")
        allow = reg.MODEL_ALLOWLIST.get(provider, ())
        if model is not None and allow and model not in allow:
            src = sources["ai.model"]
            conflicts.append(Conflict("ai.model", src.level or "default", src.object_id or "",
                                      f"model not available for provider {provider}"))
            model = None
            sources["ai.model"] = DEFAULT
        if model is None:
            model = reg.DEFAULT_MODEL[provider]
            allowed_models = policy.get("ai.allowed_models")
            if allowed_models is not None and model not in allowed_models:
                blocked["ai"] = "no permitted model for the selected provider"
        values["ai.model"] = model
    cred_views: Dict[str, Dict[str, Any]] = {}
    cred_id = values.get("ai.credential_id")
    if provider is not None and target.level in (Level.BOT, Level.BOT_INSTANCE):
        if cred_id is None:
            blocked.setdefault("ai", "no AI credential is configured")
        else:
            try:
                cred = check_use(credentials.get(cred_id), referencing_chain=chain, slot=f"ai.{provider}",
                                 policy=policy)
                cred_views["ai.credential_id"] = cred.public_view()
            except CredentialError as exc:
                src = sources["ai.credential_id"]
                conflicts.append(Conflict("ai.credential_id", src.level or "default", src.object_id or "", str(exc)))
                blocked.setdefault("ai", str(exc))

    # ── channel checks (Bot Instances) ──
    runtime_blockers: List[str] = []
    inst = chain.instance
    if inst is not None:
        ctype = inst.channel.type
        allowed_channels = policy.get("channels.allowed")
        if allowed_channels is not None and ctype.value not in allowed_channels:
            runtime_blockers.append(f"channel {ctype.value} is not permitted by policy")
        if ctype is ChannelType.WEBCHAT:
            if policy.get("webchat.public_access_allowed") is False:
                runtime_blockers.append("public web chat is not permitted by policy")
            embed_policy = policy.get("webchat.allowed_embed_origins")
            own = frozenset(inst.channel.allowed_origins)
            if embed_policy is not None:
                if own and not own <= embed_policy:
                    conflicts.append(Conflict("channel.allowed_origins", inst.level.value, inst.id,
                                              "includes origins not permitted by policy; restricted to the permitted set"))
                values["channel.allowed_origins"] = (own & embed_policy) if own else frozenset(embed_policy)
            else:
                values["channel.allowed_origins"] = own or None      # None = any origin
        elif ctype is ChannelType.TELEGRAM:
            cid = inst.channel.credential_id
            if cid is None:
                runtime_blockers.append("no Telegram bot token is configured")
            else:
                try:
                    cred = check_use(credentials.get(cid), referencing_chain=chain, slot="channel.telegram",
                                     policy=policy)
                    cred_views["channel.credential_id"] = cred.public_view()
                except CredentialError as exc:
                    runtime_blockers.append(str(exc))
        elif ctype is ChannelType.DISCORD:
            if not inst.channel.guild_id:
                runtime_blockers.append("the Discord bot has not been installed in a server yet")

    # ── overridability and permissions ──
    grant = effective_role(actor, chain, memberships) if actor else None
    writable = is_writable(target.lifecycle)
    overridable = {}
    for name, prop in reg.PROPERTIES.items():
        ok = writable and target.level in prop.levels
        if ok and actor is not None:
            needed = Role[prop.min_role.upper()]
            ok = bool(grant and grant.role is not None and grant.role >= needed) and \
                can(actor, Action.EDIT_CONFIG, chain, memberships, policy).allowed
        overridable[name] = ok
    permissions = None
    if actor is not None:
        permissions = {a.value: can(actor, a, chain, memberships, policy).allowed for a in Action}

    return Effective(
        target_level=target.level.value, target_id=target.id, availability=chain.availability(),
        values=values, sources=sources, policy=policy, policy_sources=policy_sources,
        conflicts=tuple(conflicts), blocked=blocked, runtime_blockers=tuple(runtime_blockers),
        overridable=overridable, credentials=cred_views, permissions=permissions,
        role=(grant.role.name.lower() if grant and grant.role else None))


# ═════════════════════════════ Bot Integrations (X2, Phase 4E) ═════════════════════════════

def resolve_integration(chain: Chain, integration: Mapping[str, Any], *,
                        platform_policy: Mapping[str, Any] = reg.PLATFORM_POLICY,
                        credentials: Mapping[str, Credential] = ()) -> Effective:
    """Effective configuration of a Bot Integration: the Bot's effective values
    (``chain`` targets the integration's Bot) plus the integration's sparse
    overrides for ``reg.INTEGRATION_PROPERTIES``. Every override goes through
    the property's validator, the effective policy constraints and the
    narrowing rule; an override that fails keeps the inherited value and is
    reported as a conflict. Policies are inherited unchanged."""
    if chain.target.level is not Level.BOT or integration.get("bot_id") != chain.bot.id \
            or integration.get("enterprise_id") != chain.enterprise_id:
        raise ConfigError("integration does not belong to this bot")
    credentials = dict(credentials or {})
    base = resolve(chain, platform_policy=platform_policy, credentials=credentials)
    values, sources = dict(base.values), dict(base.sources)
    conflicts = list(base.conflicts)
    src = Source("override", "integration", integration.get("id"))
    for key, raw in (integration.get("overrides") or {}).items():
        prop = reg.PROPERTIES.get(key)
        if prop is None or key not in reg.INTEGRATION_PROPERTIES:
            conflicts.append(Conflict(key, "integration", integration.get("id", ""), "not overridable on an integration"))
            continue
        try:
            value = prop.validator(raw)
        except ConfigError as exc:
            conflicts.append(Conflict(key, "integration", integration.get("id", ""), f"invalid stored value: {exc}"))
            continue
        inherited = values.get(key)
        if prop.narrowing and inherited is not None and value is not None and not set(value) <= set(inherited):
            conflicts.append(Conflict(key, "integration", integration.get("id", ""), "broadens the inherited value"))
            continue
        problems = _violations(prop, value, base.policy)
        if problems:
            conflicts.append(Conflict(key, "integration", integration.get("id", ""), "; ".join(problems)))
            continue
        values[key] = value
        sources[key] = src
    blockers = list(base.runtime_blockers)
    allowed = base.policy.get("integrations.allowed")
    if allowed is not None and integration.get("type") not in allowed:
        blockers.append(f"integration {integration.get('type')} is not permitted by policy")
    if integration.get("status") != "enabled":
        blockers.append("the integration is disabled")
    cred_views = dict(base.credentials)
    cid = integration.get("credential_id")
    if not cid:
        blockers.append("no Zendesk credential is configured")
    else:
        try:
            cred = check_use(credentials.get(cid), referencing_chain=chain, slot=f"integration.{integration.get('type')}",
                             policy=base.policy)
            cred_views["integration.credential_id"] = cred.public_view()
        except CredentialError as exc:
            blockers.append(str(exc))
    from dataclasses import replace as _replace
    return _replace(base, target_level="integration", target_id=integration.get("id"), values=values,
                    sources=sources, conflicts=tuple(conflicts), runtime_blockers=tuple(blockers),
                    credentials=cred_views,
                    overridable={k: k in reg.INTEGRATION_PROPERTIES for k in reg.PROPERTIES})


def validate_integration_override(chain: Chain, integration: Mapping[str, Any], key: str, value: Any, *,
                                  platform_policy: Mapping[str, Any] = reg.PLATFORM_POLICY,
                                  credentials: Mapping[str, Credential] = ()) -> Any:
    """Write-time check of one integration override (rejected when invalid)."""
    prop = reg.get_property(key)
    if key not in reg.INTEGRATION_PROPERTIES:
        raise ConfigError(f"{key} cannot be set on an integration")
    if not is_writable(chain.bot.lifecycle):
        raise ConfigError("archived or deleted objects are read-only")
    try:
        normalised = prop.validator(value)
    except ConfigError as exc:
        raise ConfigError(f"{key} {exc}")
    base = resolve(chain, platform_policy=platform_policy, credentials=credentials)
    problems = _violations(prop, normalised, base.policy)
    if problems:
        raise ConfigError(f"{key}: {'; '.join(problems)}")
    inherited = base.values.get(key)
    if prop.narrowing and inherited is not None and normalised is not None and not set(normalised) <= set(inherited):
        raise ConfigError(f"{key} cannot include items outside the inherited value")
    return normalised


def apply_integration_override_patch(chain: Chain, integration: Mapping[str, Any], patch: Mapping[str, Any],
                                     **kw) -> Dict[str, Any]:
    if not isinstance(patch, Mapping) or not patch:
        raise ConfigError("nothing to change")
    current = dict(integration.get("overrides") or {})
    for key, value in patch.items():
        if value is None:
            reg.get_property(key)
            current.pop(key, None)
        else:
            current[key] = _storable(validate_integration_override(chain, integration, key, value, **kw))
    return current


# ═════════════════════════════════ write validation ═════════════════════════════════

def _parent_chain(chain: Chain) -> Optional[Chain]:
    if chain.instance is not None:
        return Chain(chain.enterprise, chain.organisation, chain.bot)
    if chain.bot is not None:
        return Chain(chain.enterprise, chain.organisation)
    if chain.organisation is not None:
        return Chain(chain.enterprise)
    return None


def validate_override(chain: Chain, key: str, value: Any, *,
                      platform_policy: Mapping[str, Any] = reg.PLATFORM_POLICY,
                      credentials: Mapping[str, Credential] = ()) -> Any:
    """Validate one override at ``chain.target``; return the normalised value or
    raise :class:`ConfigError` (invalid overrides are rejected when saved)."""
    prop = reg.get_property(key)
    target = chain.target
    if not is_writable(target.lifecycle):
        raise ConfigError("archived or deleted objects are read-only")
    if target.level not in prop.levels:
        raise ConfigError(f"{key} cannot be set on a {target.level.value}")
    try:
        normalised = prop.validator(value)
    except ConfigError as exc:
        raise ConfigError(f"{key} {exc}")
    policy, _, _ = effective_policy(_policy_nodes(chain), platform_policy)
    problems = _violations(prop, normalised, policy)
    if problems:
        raise ConfigError(f"{key}: {'; '.join(problems)}")
    if prop.narrowing:
        parent = _parent_chain(chain)
        inherited = resolve(parent, platform_policy=platform_policy, credentials=credentials).values[key] \
            if parent else prop.default
        if inherited is not None and normalised is not None and not set(normalised) <= set(inherited):
            raise ConfigError(f"{key} cannot include items outside the inherited value")
    if key == "ai.model" and normalised is not None:
        provider = resolve(chain, platform_policy=platform_policy, credentials=credentials).values["ai.provider"]
        allow = reg.MODEL_ALLOWLIST.get(provider or "", ())
        if allow and normalised not in allow:
            raise ConfigError(f"ai.model is not available for provider {provider}")
    if prop.security is SecurityClass.CREDENTIAL_REF and normalised is not None:
        provider = resolve(chain, platform_policy=platform_policy, credentials=credentials).values["ai.provider"]
        try:
            check_use(dict(credentials or {}).get(normalised), referencing_chain=chain,
                      slot=f"ai.{provider}", policy=policy)
        except CredentialError as exc:
            raise ConfigError(str(exc))
    return normalised


def apply_override_patch(chain: Chain, patch: Mapping[str, Any], **kw) -> Dict[str, Any]:
    """Return the new override map for ``chain.target``. ``None`` removes an
    override (restoring the inherited value). All-or-nothing."""
    if not isinstance(patch, Mapping) or not patch:
        raise ConfigError("nothing to change")
    current = dict(chain.target.overrides or {})
    for key in patch:
        reg.get_property(key)
    # Stage 0: dependent AI keys are validated in order, each against the overrides
    # already accepted in this patch (a provider switch and its new model/credential
    # are one change, not three that each fail against the old provider).
    first = [k for k in ("ai.provider", "ai.model", "ai.credential_id") if k in patch]
    for key in first + [k for k in patch if k not in first]:
        value = patch[key]
        if value is None:
            current.pop(key, None)
        else:
            current[key] = _storable(validate_override(chain, key, value, **kw))
        if key in first:
            chain = _with_target_overrides(chain, current)
    return current


def _with_target_overrides(chain: Chain, overrides: Mapping[str, Any]) -> Chain:
    from dataclasses import replace
    target = replace(chain.target, overrides=dict(overrides))
    field = {Level.ENTERPRISE: "enterprise", Level.ORGANISATION: "organisation", Level.BOT: "bot",
             Level.BOT_INSTANCE: "instance"}[target.level]
    return replace(chain, **{field: target})


def validate_policy_write(chain: Chain, key: str, value: Any, *,
                          platform_policy: Mapping[str, Any] = reg.PLATFORM_POLICY) -> Any:
    """A policy write at an Enterprise/Organisation may only tighten its parent's
    effective policy."""
    pkey: PolicyKey = reg.get_policy(key)
    target = chain.target
    if target.level not in pkey.levels:
        raise ConfigError(f"{key} cannot be set on a {target.level.value}")
    if not is_writable(target.lifecycle):
        raise ConfigError("archived or deleted objects are read-only")
    try:
        normalised = pkey.validator(value)
    except ConfigError as exc:
        raise ConfigError(f"{key} {exc}")
    if target.level is Level.ENTERPRISE:
        inherited = platform_policy.get(key)    # only the platform is above an Enterprise
    else:
        parent_nodes = [n for n in _policy_nodes(chain) if n.id != target.id]
        parent_policy, _, _ = effective_policy(parent_nodes, platform_policy)
        inherited = parent_policy.get(key)
    if not reg.is_tighter_or_equal(pkey, inherited, normalised):
        raise ConfigError(f"{key} would weaken the inherited policy")
    return normalised


def apply_policy_patch(chain: Chain, patch: Mapping[str, Any], **kw) -> Dict[str, Any]:
    if not isinstance(patch, Mapping) or not patch:
        raise ConfigError("nothing to change")
    current = dict(getattr(chain.target, "policy", {}) or {})
    for key, value in patch.items():
        reg.get_policy(key)
        if value is None:
            current.pop(key, None)
        else:
            current[key] = _storable(validate_policy_write(chain, key, value, **kw))
    return current


def _storable(value: Any) -> Any:
    """Convert normalised values (frozensets, enums, tuples) to plain stored values."""
    from enum import Enum
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return [_storable(v) for v in value]
    return value


__all__ = ["Source", "Conflict", "Effective", "effective_policy", "resolve", "validate_override",
           "apply_override_patch", "validate_policy_write", "apply_policy_patch"]
