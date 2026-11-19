"""Enterprise / Organisation / Bot / Bot Instance endpoints (spec §11)."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.common import Access, Services, load_access, node_view, require_superadmin, translate
from core import audit
from core import memberships as ms
from core.authz import Action, AuthzError, Role, effective_role
from core.credentials import check_reference_permission, check_use, load_credential
from core.hierarchy import COLLECTION, ChannelType, Level, LifecycleState, NotFound, from_doc
from core.identity import User, get_user


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnterpriseCreate(_Body):
    name: str
    slug: str
    plan: str = "free"
    owner_user_id: Optional[str] = None


class EnterprisePatch(_Body):
    name: Optional[str] = None
    plan: Optional[str] = None


class ChildCreate(_Body):
    name: str
    description: str = ""


class ChildPatch(_Body):
    name: Optional[str] = None
    description: Optional[str] = None


class InstanceCreate(_Body):
    name: str
    channel: Dict[str, Any]


class InstancePatch(_Body):
    name: Optional[str] = None
    channel: Optional[Dict[str, Any]] = None


def _changes(body: BaseModel) -> Dict[str, Any]:
    return {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}


async def telegram_binding_check(S: Services, access: Access, channel: Dict[str, Any]) -> Optional[str]:
    """A Telegram credential reference must be usable here (same Enterprise,
    owner branch, policy, active, right provider) and the actor needs the
    *use* permission. Returns the Telegram bot id used as a binding key."""
    cred_id = channel.get("credential_id")
    if cred_id is None:
        return None
    loaded = await load_credential(S.store, cred_id)
    check_reference_permission(access.actor, access.chain, access.roster.members, access.policy)
    cred = check_use(loaded[0] if loaded else None, referencing_chain=access.chain, slot="channel.telegram",
                     policy=access.policy)
    return cred.metadata.get("telegram_bot_id")


async def _visible_children(S: Services, user: User, parent: Access, child_level: Level) -> List[Dict[str, Any]]:
    """Children the user may see: all of them with a role on the parent,
    otherwise only branches that contain one of the user's memberships."""
    children = await S.hierarchy.children(child_level, parent.target.id)
    children = [c for c in children if c.lifecycle.state is not LifecycleState.DELETED]
    if parent.grant.superadmin or parent.grant.role is not None:
        return [node_view(c) for c in children]
    mine = [m for m in parent.roster.for_user(user.id)]
    allowed = set()
    for m in mine:
        allowed.add(m.scope_id)
        if m.scope_level is Level.BOT:
            doc = await S.store.get(COLLECTION[Level.BOT], m.scope_id)
            if doc and doc.get("enterprise_id") == parent.chain.enterprise_id:
                allowed.add(doc["organisation_id"])
    return [node_view(c) for c in children if c.id in allowed]


async def _navigation_access(S: Services, user: User, level: Level, object_id: str) -> Access:
    """Parent access for listing children: a role on the parent, or any
    membership inside it (then the list is filtered)."""
    chain = await S.hierarchy.load_chain(level, object_id)
    if any(n.lifecycle.state is LifecycleState.DELETED for n in chain.nodes()):
        raise NotFound("Not found.")
    roster = await ms.load_for(S.store, chain.enterprise_id, [user.id])
    actor = user.actor()
    grant = effective_role(actor, chain, roster.members)
    if not grant.superadmin and grant.role is None:
        ids = {chain.target.id}
        mine = roster.for_user(user.id)
        if not mine:
            raise NotFound("Not found.")
        if level is Level.ORGANISATION:
            bots = [m.scope_id for m in mine if m.scope_level is Level.BOT]
            inside = False
            for bid in bots:
                doc = await S.store.get(COLLECTION[Level.BOT], bid)
                inside = inside or bool(doc and doc.get("organisation_id") in ids)
            if not inside:
                raise NotFound("Not found.")
        elif level is not Level.ENTERPRISE:
            raise NotFound("Not found.")
    from api.common import policy_nodes
    from core import resolver
    policy, _, _ = resolver.effective_policy(policy_nodes(chain), S.platform_policy)
    return Access(actor, chain, roster, policy, grant)


def register(router: APIRouter, S: Services, principal) -> None:
    async def audit_write(access_or_user, action: str, level: Optional[Level], target_id: Optional[str],
                          enterprise_id: Optional[str], details: Optional[Dict[str, Any]] = None):
        actor = access_or_user.actor if isinstance(access_or_user, Access) else access_or_user.actor()
        await audit.record(S.store, actor=actor, action=action, enterprise_id=enterprise_id,
                           target_level=level.value if level else None, target_id=target_id, now=S.clock(),
                           details=details)

    # ── Enterprises ──
    @router.get("/enterprises")
    @translate
    async def list_enterprises(user: User = Depends(principal)):
        if user.is_platform_superadmin:
            docs = await S.store.find(COLLECTION[Level.ENTERPRISE], {}, sort="created_at")
            ids = [d["id"] for d in docs]
        else:
            ids = await ms.enterprises_of(S.store, user.id)
        out = []
        for eid in ids:
            doc = await S.store.get(COLLECTION[Level.ENTERPRISE], eid)
            if not doc:
                continue
            ent = from_doc(Level.ENTERPRISE, doc)
            if ent.lifecycle.state is LifecycleState.DELETED:
                continue
            roster = await ms.load_for(S.store, eid, [user.id])
            role = next((m.role.name.lower() for m in roster.for_user(user.id)
                         if m.scope_level is Level.ENTERPRISE), None)
            out.append({"id": ent.id, "name": ent.name, "slug": ent.slug, "lifecycle": ent.lifecycle.to_doc(),
                        "role": role})
        return {"enterprises": out}

    @router.post("/enterprises", status_code=201)
    @translate
    async def create_enterprise(body: EnterpriseCreate, user: User = Depends(principal)):
        require_superadmin(user)                       # tenants do not self-create Enterprises (Phase 3)
        owner_id = body.owner_user_id or user.id
        owner = await get_user(S.store, owner_id)
        if owner is None or owner.status != "active":
            raise NotFound("Not found.")
        ent = await S.hierarchy.create_enterprise(name=body.name, slug=body.slug, plan=body.plan, actor_id=user.id)
        chain = await S.hierarchy.load_chain(Level.ENTERPRISE, ent.id)
        await ms.mutate(S.store, ent.id, ms.op_grant(user.actor(), chain, user_id=owner_id, role=Role.OWNER,
                                                     now=S.clock(), bypass_authority=True), clock=S.clock)
        await audit_write(user, "enterprise.create", Level.ENTERPRISE, ent.id, ent.id, {"owner": owner_id})
        return node_view(ent, chain)

    @router.get("/enterprises/{enterprise_id}")
    @translate
    async def get_enterprise(enterprise_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, Level.ENTERPRISE, enterprise_id)
        return {**node_view(a.target, a.chain), "role": a.grant.role.name.lower() if a.grant.role else None}

    @router.patch("/enterprises/{enterprise_id}")
    @translate
    async def patch_enterprise(enterprise_id: str, body: EnterprisePatch, user: User = Depends(principal)):
        changes = _changes(body)
        a = await load_access(S, user, Level.ENTERPRISE, enterprise_id,
                              Action.MANAGE_BILLING if "plan" in changes else Action.EDIT_POLICY)
        node = await S.hierarchy.update(Level.ENTERPRISE, enterprise_id, changes, actor_id=user.id)
        await audit_write(a, "enterprise.update", Level.ENTERPRISE, enterprise_id, enterprise_id,
                          {"fields": sorted(changes)})
        return node_view(node)

    # ── Organisations / Bots (generic child routes) ──
    def child_routes(parent_type: str, parent_level: Level, child_type: str, child_level: Level, create):
        @router.get(f"/{parent_type}/{{parent_id}}/{child_type}", name=f"list_{child_type}")
        @translate
        async def list_children(parent_id: str, user: User = Depends(principal)):
            a = await _navigation_access(S, user, parent_level, parent_id)
            return {child_type: await _visible_children(S, user, a, child_level)}

        @router.post(f"/{parent_type}/{{parent_id}}/{child_type}", status_code=201, name=f"create_{child_type}")
        @translate
        async def create_child(parent_id: str, body: ChildCreate, user: User = Depends(principal)):
            a = await load_access(S, user, parent_level, parent_id, Action.CREATE_CHILD)
            node = await create(a, body, user)
            await audit_write(a, f"{child_level.value}.create", child_level, node.id, a.chain.enterprise_id)
            return node_view(node)

        @router.get(f"/{child_type}/{{object_id}}", name=f"get_{child_level.value}")
        @translate
        async def get_child(object_id: str, user: User = Depends(principal)):
            a = await load_access(S, user, child_level, object_id)
            return {**node_view(a.target, a.chain), "role": a.grant.role.name.lower() if a.grant.role else None}

        @router.patch(f"/{child_type}/{{object_id}}", name=f"patch_{child_level.value}")
        @translate
        async def patch_child(object_id: str, body: ChildPatch, user: User = Depends(principal)):
            a = await load_access(S, user, child_level, object_id, Action.EDIT_CONFIG)
            changes = _changes(body)
            node = await S.hierarchy.update(child_level, object_id, changes, actor_id=user.id)
            await audit_write(a, f"{child_level.value}.update", child_level, object_id, a.chain.enterprise_id,
                              {"fields": sorted(changes)})
            return node_view(node)

    async def create_org(a: Access, body: ChildCreate, user: User):
        return await S.hierarchy.create_organisation(enterprise_id=a.target.id, name=body.name,
                                                     description=body.description, actor_id=user.id)

    async def create_bot(a: Access, body: ChildCreate, user: User):
        return await S.hierarchy.create_bot(organisation_id=a.target.id, name=body.name,
                                            description=body.description, actor_id=user.id)

    child_routes("enterprises", Level.ENTERPRISE, "organisations", Level.ORGANISATION, create_org)
    child_routes("organisations", Level.ORGANISATION, "bots", Level.BOT, create_bot)

    # ── Bot Instances ──
    @router.get("/bots/{bot_id}/instances")
    @translate
    async def list_instances(bot_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id)
        children = await S.hierarchy.children(Level.BOT_INSTANCE, bot_id)
        return {"instances": [node_view(c) for c in children if c.lifecycle.state is not LifecycleState.DELETED]}

    @router.post("/bots/{bot_id}/instances", status_code=201)
    @translate
    async def create_instance(bot_id: str, body: InstanceCreate, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT, bot_id, Action.CREATE_CHILD)
        tg = None
        if body.channel.get("type") == ChannelType.TELEGRAM.value:
            tg = await telegram_binding_check(S, a, body.channel)
        node = await S.hierarchy.create_instance(bot_id=bot_id, name=body.name, channel=body.channel,
                                                 actor_id=user.id, telegram_bot_id=tg)
        await audit_write(a, "bot_instance.create", Level.BOT_INSTANCE, node.id, a.chain.enterprise_id,
                          {"channel": node.channel.type.value})
        return node_view(node)

    @router.get("/instances/{instance_id}")
    @translate
    async def get_instance(instance_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id)
        return {**node_view(a.target, a.chain), "role": a.grant.role.name.lower() if a.grant.role else None}

    @router.patch("/instances/{instance_id}")
    @translate
    async def patch_instance(instance_id: str, body: InstancePatch, user: User = Depends(principal)):
        changes = _changes(body)
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id,
                              Action.MANAGE_CHANNEL if "channel" in changes else Action.EDIT_CONFIG)
        tg = None
        if "channel" in changes and a.target.channel.type is ChannelType.TELEGRAM \
                and changes["channel"].get("credential_id") not in (None, a.target.channel.credential_id):
            doc = await S.store.get(COLLECTION[Level.BOT_INSTANCE], instance_id)
            hook = (doc or {}).get("telegram_webhook") or {}
            if hook.get("desired") or hook.get("registered"):
                raise HTTPException(409, "Disconnect the Telegram channel before changing its bot token.")
            tg = await telegram_binding_check(S, a, changes["channel"])
        if "channel" in changes and a.target.channel.type is ChannelType.DISCORD:
            from api.discord import discord_channel_check        # channels must belong to the bound server
            await discord_channel_check(S, a.chain, a.target.channel, changes["channel"])
        node = await S.hierarchy.update(Level.BOT_INSTANCE, instance_id, changes, actor_id=user.id,
                                        telegram_bot_id=tg)
        await audit_write(a, "bot_instance.update", Level.BOT_INSTANCE, instance_id, a.chain.enterprise_id,
                          {"fields": sorted(changes)})
        return node_view(node)
