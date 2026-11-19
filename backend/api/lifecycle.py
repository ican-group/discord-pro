"""Lifecycle endpoints: suspend, resume, archive, unarchive (spec §7, D-Q2)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.common import TYPE_LEVEL, Services, load_access, node_view, translate
from core import audit
from core.authz import Action, lifecycle_authority
from core.identity import User
from core.lifecycle import LifecycleAction


class LifecycleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = ""
    expected_version: int | None = None


OPS = {"suspend": (LifecycleAction.SUSPEND, Action.SUSPEND), "resume": (LifecycleAction.RESUME, Action.SUSPEND),
       "archive": (LifecycleAction.ARCHIVE, Action.ARCHIVE), "unarchive": (LifecycleAction.UNARCHIVE, Action.ARCHIVE)}


def register(router: APIRouter, S: Services, principal) -> None:
    def make(op: str):
        lc_action, perm = OPS[op]

        @router.post(f"/{{type_:objtype}}/{{object_id}}/{op}", name=f"lifecycle_{op}")
        @translate
        async def handler(type_: str, object_id: str, body: LifecycleBody | None = None,
                          user: User = Depends(principal)):
            if type_ not in TYPE_LEVEL:
                raise HTTPException(404, "Not found.")
            level = TYPE_LEVEL[type_]
            body = body or LifecycleBody()
            a = await load_access(S, user, level, object_id, perm)
            authority = lifecycle_authority(a.actor, a.chain, a.roster.members)
            node = await S.hierarchy.change_lifecycle(
                level, object_id, lc_action, authority=authority, actor_id=user.id, reason=body.reason,
                expect_version=body.expected_version if body.expected_version is not None else a.target.version)
            await audit.record(S.store, actor=a.actor, action=f"lifecycle.{op}", enterprise_id=a.chain.enterprise_id,
                               target_level=level.value, target_id=object_id, now=S.clock(),
                               details={"authority": authority.name.lower(), "reason": body.reason})
            chain = await S.hierarchy.load_chain(level, object_id)
            from api.telegram import sync_subtree
            await sync_subtree(S, level, object_id, chain.enterprise_id)     # lifecycle-driven webhooks (T2)
            return node_view(node, chain)

    for op in OPS:
        make(op)
