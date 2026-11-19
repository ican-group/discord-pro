"""One permission model (spec 012 §9).

Roles: OWNER (Enterprise only) > ADMIN > EDITOR > VIEWER. Memberships exist at
Enterprise, Organisation or Bot scope and apply downwards. A user's effective
role on an object is the highest role among their memberships on that object
and its ancestors, limited by Enterprise delegation policy. Platform
superadmins (``users.is_platform_superadmin``) act above Enterprises.

This module is independent of configuration inheritance: it decides *who may
do what*; the resolver only reports the result.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Dict, Iterable, List, Optional, Sequence

from core.hierarchy import Chain, Level, is_valid_id
from core.lifecycle import Authority


class AuthzError(PermissionError):
    """Denied. Callers turn unknown/unauthorised targets into a non-disclosing 404."""


class Role(IntEnum):
    VIEWER = 1
    EDITOR = 2
    ADMIN = 3
    OWNER = 4


MEMBERSHIP_SCOPES = (Level.ENTERPRISE, Level.ORGANISATION, Level.BOT)


@dataclass(frozen=True)
class Membership:
    user_id: str
    enterprise_id: str
    scope_level: Level
    scope_id: str
    role: Role

    def validated(self) -> "Membership":
        if not (is_valid_id(self.user_id) and is_valid_id(self.enterprise_id) and is_valid_id(self.scope_id)):
            raise AuthzError("Invalid membership reference.")
        if self.scope_level not in MEMBERSHIP_SCOPES:
            raise AuthzError("Memberships exist at Enterprise, Organisation or Bot level.")
        if self.role is Role.OWNER and self.scope_level is not Level.ENTERPRISE:
            raise AuthzError("Owner is an Enterprise-level role.")
        if self.scope_level is Level.ENTERPRISE and self.scope_id != self.enterprise_id:
            raise AuthzError("Enterprise membership scope mismatch.")
        return self


@dataclass(frozen=True)
class Actor:
    user_id: str
    is_platform_superadmin: bool = False


class Action(str, Enum):
    READ = "read"                                   # object, effective config (with sources), conversations, usage
    EDIT_CONFIG = "edit_config"                     # overrides (subject to per-property min_role)
    USE_CREDENTIAL = "use_credential"               # reference a credential
    CREATE_CHILD = "create_child"
    MANAGE_CHANNEL = "manage_channel"               # bindings, connect/disconnect
    SUSPEND = "suspend"
    ARCHIVE = "archive"
    EDIT_POLICY = "edit_policy"
    MANAGE_CREDENTIALS = "manage_credentials"
    MANAGE_MEMBERS = "manage_members"
    DELETE = "delete"                               # permanent deletion of this object (needs parent authority)
    MANAGE_OWNERS = "manage_owners"
    MANAGE_BILLING = "manage_billing"


MIN_ROLE: Dict[Action, Role] = {
    Action.READ: Role.VIEWER,
    Action.EDIT_CONFIG: Role.EDITOR,
    Action.USE_CREDENTIAL: Role.EDITOR,
    Action.CREATE_CHILD: Role.ADMIN,
    Action.MANAGE_CHANNEL: Role.ADMIN,
    Action.SUSPEND: Role.ADMIN,
    Action.ARCHIVE: Role.ADMIN,
    Action.EDIT_POLICY: Role.ADMIN,
    Action.MANAGE_CREDENTIALS: Role.ADMIN,
    Action.MANAGE_MEMBERS: Role.ADMIN,
    Action.DELETE: Role.ADMIN,
    Action.MANAGE_OWNERS: Role.OWNER,
    Action.MANAGE_BILLING: Role.OWNER,
}

# Delegation policy that limits actions when the actor's authority comes only
# from an Organisation- or Bot-level membership.
DELEGATION: Dict[Action, Dict[Level, str]] = {
    Action.MANAGE_MEMBERS: {Level.ORGANISATION: "delegation.org_admins_manage_members",
                            Level.BOT: "delegation.bot_admins_manage_members"},
    Action.MANAGE_CREDENTIALS: {Level.ORGANISATION: "delegation.org_admins_manage_credentials"},
}


@dataclass(frozen=True)
class RoleGrant:
    role: Optional[Role]
    source_level: Optional[Level]          # the scope the winning membership is attached to
    superadmin: bool = False


def effective_role(actor: Actor, chain: Chain, memberships: Iterable[Membership]) -> RoleGrant:
    """Highest role among the actor's memberships on the chain (same Enterprise only)."""
    if actor.is_platform_superadmin:
        return RoleGrant(Role.OWNER, None, superadmin=True)
    chain_ids = {n.level: n.id for n in chain.nodes()}
    best: Optional[Membership] = None
    for m in memberships:
        if m.user_id != actor.user_id or m.enterprise_id != chain.enterprise_id:
            continue                                    # never cross-tenant
        if chain_ids.get(m.scope_level) != m.scope_id:
            continue
        if best is None or m.role > best.role or (m.role == best.role and
                                                  MEMBERSHIP_SCOPES.index(m.scope_level) < MEMBERSHIP_SCOPES.index(best.scope_level)):
            best = m
    if best is None:
        return RoleGrant(None, None)
    return RoleGrant(best.role, best.scope_level)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


def can(actor: Actor, action: Action, chain: Chain, memberships: Iterable[Membership],
        policy: Dict[str, object]) -> Decision:
    """May ``actor`` perform ``action`` on ``chain.target``? ``policy`` is the
    target's effective policy (``core.resolver``)."""
    grant = effective_role(actor, chain, memberships)
    if grant.superadmin:
        return Decision(True)
    if grant.role is None:
        return Decision(False, "not a member")
    if grant.role < MIN_ROLE[action]:
        return Decision(False, f"requires {MIN_ROLE[action].name.lower()}")
    target = chain.target.level
    if action in (Action.MANAGE_OWNERS, Action.MANAGE_BILLING) and target is not Level.ENTERPRISE:
        return Decision(False, "enterprise-level action")
    if action is Action.EDIT_POLICY and target not in (Level.ENTERPRISE, Level.ORGANISATION):
        return Decision(False, "policies exist only on Enterprises and Organisations")
    if action is Action.DELETE:
        # Enterprises and (X7, approved 2026-11-18) Organisations: Enterprise Owners only.
        if target in (Level.ENTERPRISE, Level.ORGANISATION):
            if grant.role < Role.OWNER:
                return Decision(False, "requires owner")
        elif grant.source_level == target:
            # Deleting needs authority above the object: an Organisation admin cannot
            # delete their own Organisation; an Enterprise admin can.
            return Decision(False, "requires authority over the parent")
    delegated = DELEGATION.get(action, {}).get(grant.source_level)
    if delegated and policy.get(delegated) is False:
        return Decision(False, f"restricted by policy {delegated}")
    return Decision(True)


def require(actor: Actor, action: Action, chain: Chain, memberships: Iterable[Membership],
            policy: Dict[str, object]) -> None:
    d = can(actor, action, chain, memberships, policy)
    if not d.allowed:
        raise AuthzError(d.reason)


def lifecycle_authority(actor: Actor, chain: Chain, memberships: Iterable[Membership]) -> Authority:
    """Authority used for suspensions: superadmin > Enterprise owner acting on a
    descendant (or the Enterprise itself) > object admins."""
    grant = effective_role(actor, chain, memberships)
    if grant.superadmin:
        return Authority.PLATFORM
    if grant.role is Role.OWNER:
        return Authority.ENTERPRISE
    return Authority.OBJECT


# ═════════════════════════════════ membership management ═════════════════════════════════

def check_grant(actor: Actor, chain: Chain, memberships: Sequence[Membership], policy: Dict[str, object], *,
                target_user_id: str, new_role: Role, existing_role: Optional[Role] = None) -> None:
    """Validate adding or changing a membership at ``chain.target`` (an Enterprise,
    Organisation or Bot). Raises :class:`AuthzError`."""
    scope = chain.target.level
    if scope not in MEMBERSHIP_SCOPES:
        raise AuthzError("Memberships exist at Enterprise, Organisation or Bot level.")
    require(actor, Action.MANAGE_MEMBERS, chain, memberships, policy)
    grant = effective_role(actor, chain, memberships)
    if new_role is Role.OWNER or existing_role is Role.OWNER:
        if scope is not Level.ENTERPRISE:
            raise AuthzError("Owner is an Enterprise-level role.")
        require(actor, Action.MANAGE_OWNERS, chain, memberships, policy)
    if not grant.superadmin:
        if new_role > grant.role:
            raise AuthzError("You cannot grant a role above your own.")
        if existing_role is not None and existing_role > grant.role:
            raise AuthzError("You cannot change a member whose role is above your own.")
        if target_user_id == actor.user_id and existing_role is not None and new_role > existing_role:
            raise AuthzError("You cannot raise your own role.")
    if scope is not Level.ENTERPRISE and not any(
            m.user_id == target_user_id and m.enterprise_id == chain.enterprise_id for m in memberships):
        raise AuthzError("The user must already belong to this Enterprise.")


def ensure_owner_remains(enterprise_memberships: Sequence[Membership], enterprise_id: str, *,
                         removing_user_id: str) -> None:
    """Block removal/demotion/deletion that would leave an Enterprise without an Owner.
    (Deleting the Enterprise itself is the explicit way to remove the last owner.)"""
    owners = {m.user_id for m in enterprise_memberships
              if m.enterprise_id == enterprise_id and m.scope_level is Level.ENTERPRISE and m.role is Role.OWNER}
    if owners == {removing_user_id}:
        raise AuthzError("An Enterprise must keep at least one Owner. Add another Owner first.")


def ensure_superadmin_remains(superadmin_ids: Iterable[str], *, removing_user_id: str) -> None:
    ids = set(superadmin_ids)
    if ids == {removing_user_id}:
        raise AuthzError("The last platform superadmin cannot be removed or demoted.")


def memberships_to_revoke_for_user(memberships: Iterable[Membership], user_id: str) -> List[Membership]:
    return [m for m in memberships if m.user_id == user_id]
