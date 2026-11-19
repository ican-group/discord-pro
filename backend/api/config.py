"""Effective configuration, overrides and policies (spec §4, §11).

Reads and writes go exclusively through ``core.resolver`` (the single
resolver); writes are validated against the registry and the node's current
effective policy and stored with compare-and-swap on the version that was
validated.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.common import TYPE_LEVEL, Services, jsonable, load_access, translate
from core import audit, resolver
from core.authz import Action, AuthzError
from core.config_registry import ConfigError, PROPERTIES
from core.credentials import enterprise_credentials
from core.hierarchy import Level
from core.identity import User


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: Dict[str, Any]


def effective_view(eff: resolver.Effective) -> Dict[str, Any]:
    return {
        "target": {"type": eff.target_level, "id": eff.target_id},
        "available": eff.available, "unavailable_reason": eff.availability.reason or None,
        "runnable": eff.runnable,
        "values": jsonable(eff.values), "sources": {k: v.to_dict() for k, v in eff.sources.items()},
        "policy": jsonable(eff.policy), "policy_sources": {k: v.to_dict() for k, v in eff.policy_sources.items()},
        "conflicts": [c.__dict__ for c in eff.conflicts], "blocked": dict(eff.blocked),
        "runtime_blockers": list(eff.runtime_blockers), "overridable": dict(eff.overridable),
        "credentials": jsonable(eff.credentials), "permissions": eff.permissions, "role": eff.role,
    }


def register(router: APIRouter, S: Services, principal) -> None:
    def level_of(type_: str) -> Level:
        if type_ not in TYPE_LEVEL:
            raise HTTPException(404, "Not found.")
        return TYPE_LEVEL[type_]

    async def resolve_for(a, user: User) -> resolver.Effective:
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        return resolver.resolve(a.chain, platform_policy=S.platform_policy, credentials=creds,
                                actor=a.actor, memberships=a.roster.members)

    @router.get("/{type_:objtype}/{object_id}/config")
    @translate
    async def get_config(type_: str, object_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, level_of(type_), object_id)
        return effective_view(await resolve_for(a, user))

    @router.patch("/{type_:objtype}/{object_id}/config")
    @translate
    async def patch_config(type_: str, object_id: str, body: Patch, user: User = Depends(principal)):
        level = level_of(type_)
        a = await load_access(S, user, level, object_id, Action.EDIT_CONFIG)
        eff = await resolve_for(a, user)
        for key in body.changes:
            if key not in PROPERTIES:
                raise ConfigError(f"unknown setting {key}")
            if not eff.overridable.get(key):
                raise AuthzError(f"{key} cannot be changed here by you")
        creds = await enterprise_credentials(S.store, a.chain.enterprise_id)
        if "knowledge.approved_sources" in body.changes:
            from api.knowledge import check_source_refs
            await check_source_refs(S, a.chain, body.changes["knowledge.approved_sources"])
        new = resolver.apply_override_patch(a.chain, body.changes, platform_policy=S.platform_policy,
                                            credentials=creds)
        await S.hierarchy.set_overrides(level, object_id, new, expect_version=a.target.version)
        await audit.record(S.store, actor=a.actor, action="config.update", enterprise_id=a.chain.enterprise_id,
                           target_level=level.value, target_id=object_id, now=S.clock(),
                           details={"keys": sorted(body.changes)})
        a2 = await load_access(S, user, level, object_id)
        return effective_view(await resolve_for(a2, user))

    @router.get("/{type_:policytype}/{object_id}/policy")
    @translate
    async def get_policy(type_: str, object_id: str, user: User = Depends(principal)):
        level = level_of(type_)
        if level not in (Level.ENTERPRISE, Level.ORGANISATION):
            raise HTTPException(404, "Not found.")
        a = await load_access(S, user, level, object_id)
        policy, sources, conflicts = resolver.effective_policy(
            [n for n in a.chain.nodes() if n.level in (Level.ENTERPRISE, Level.ORGANISATION)], S.platform_policy)
        return {"own": jsonable(getattr(a.target, "policy", {}) or {}), "effective": jsonable(policy),
                "sources": {k: v.to_dict() for k, v in sources.items()},
                "conflicts": [c.__dict__ for c in conflicts]}

    @router.patch("/{type_:policytype}/{object_id}/policy")
    @translate
    async def patch_policy(type_: str, object_id: str, body: Patch, user: User = Depends(principal)):
        level = level_of(type_)
        if level not in (Level.ENTERPRISE, Level.ORGANISATION):
            raise HTTPException(404, "Not found.")
        a = await load_access(S, user, level, object_id, Action.EDIT_POLICY)
        if "knowledge.allowed_sources" in body.changes:
            from api.knowledge import check_source_refs
            await check_source_refs(S, a.chain, body.changes["knowledge.allowed_sources"])
        new = resolver.apply_policy_patch(a.chain, body.changes, platform_policy=S.platform_policy)
        if level is Level.ENTERPRISE and any(k.startswith("delegation.") for k in body.changes):
            # delegation changes membership authority: serialised with membership changes (X4)
            from core import memberships as ms
            await ms.mutate(S.store, object_id, ms.op_set_enterprise_policy(
                a.actor, a.chain, new_policy=new, expect_version=a.target.version, now=S.clock()), clock=S.clock)
        else:
            await S.hierarchy.set_policy(level, object_id, new, expect_version=a.target.version)
        await audit.record(S.store, actor=a.actor, action="policy.update", enterprise_id=a.chain.enterprise_id,
                           target_level=level.value, target_id=object_id, now=S.clock(),
                           details={"keys": sorted(body.changes)})
        return await get_policy(type_, object_id, user)
