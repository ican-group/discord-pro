"""Deletion preview/confirmation, deletion jobs, users and audit records
(spec §8, D-Q3)."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from api.common import TYPE_LEVEL, Services, load_access, require_superadmin, translate
from core import audit
from core import deletion as dl
from core import memberships as ms
from core.authz import Action, AuthzError
from core.hierarchy import ConcurrentModification, Level, LifecycleAction, NotFound
from core.identity import User, get_user, grant_superadmin, revoke_superadmin
from core.lifecycle import Authority, LifecycleError


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeleteConfirm(_Body):
    preview_token: str
    confirm_name: str


class UserDeleteConfirm(_Body):
    preview_token: str
    confirm_email: str


class SuperadminBody(_Body):
    grant: bool


class ResolveActionBody(_Body):
    reason: str


def _job_view(job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if job is None:
        raise NotFound("Not found.")
    keys = ("id", "target_level", "target_id", "enterprise_id", "status", "steps_done", "deleted",
            "preview_counts", "external_actions", "requested_by", "requested_at", "completed_at", "error", "attempts")
    return {k: job.get(k) for k in keys}


def register(router: APIRouter, S: Services, principal) -> None:
    # ── hierarchy objects ──
    @router.post("/{type_:objtype}/{object_id}/deletion-preview")
    @translate
    async def deletion_preview(type_: str, object_id: str, user: User = Depends(principal)):
        if type_ not in TYPE_LEVEL:
            raise HTTPException(404, "Not found.")
        level = TYPE_LEVEL[type_]
        a = await load_access(S, user, level, object_id, Action.DELETE)
        pv = await dl.preview(S.store, level, object_id, a.chain.enterprise_id)
        token, expires = await dl.issue_token(S.store, actor_id=user.id, level=level.value, target_id=object_id,
                                              digest=pv["digest"], now=S.clock())
        return {**pv, "preview_token": token, "expires_at": expires, "confirm_name": a.target.name,
                "backups_note": "Copies in database backups persist until the backups age out."}

    @router.post("/{type_:objtype}/{object_id}/delete", status_code=202)
    @translate
    async def confirm_delete(type_: str, object_id: str, body: DeleteConfirm, background: BackgroundTasks,
                             user: User = Depends(principal)):
        if type_ not in TYPE_LEVEL:
            raise HTTPException(404, "Not found.")
        level = TYPE_LEVEL[type_]
        a = await load_access(S, user, level, object_id, Action.DELETE)        # re-authorised now
        token_doc = await dl.consume_token(S.store, body.preview_token, actor_id=user.id, level=level.value,
                                           target_id=object_id, now=S.clock())     # single use
        if body.confirm_name != a.target.name:
            raise dl.DeletionError("The typed name does not match. Request a new deletion preview.")
        pv = await dl.preview(S.store, level, object_id, a.chain.enterprise_id)
        if pv["digest"] != token_doc["digest"]:
            raise dl.DeletionError("The data to be deleted changed since the preview. Request a new deletion preview.")
        try:
            await S.hierarchy.change_lifecycle(level, object_id, LifecycleAction.BEGIN_DELETE,
                                               authority=Authority.PLATFORM if user.is_platform_superadmin
                                               else Authority.ENTERPRISE, actor_id=user.id,
                                               reason="permanent deletion", expect_version=a.target.version)
        except (ConcurrentModification, LifecycleError):
            raise dl.DeletionError("This object changed or is already being deleted. Reload and try again.")
        job, _created = await dl.create_job(S.store, level=level.value, target_id=object_id,
                                            enterprise_id=a.chain.enterprise_id, target_name=a.target.name,
                                            actor=a.actor, now=S.clock(), external=pv["external_actions"],
                                            counts=pv["counts"])
        job = await dl.run_job(S.store, job["id"], clock=S.clock, only=dl.IMMEDIATE_STEPS)  # access revoked now
        await audit.record(S.store, actor=a.actor, action="deletion.confirm", enterprise_id=a.chain.enterprise_id,
                           target_level=level.value, target_id=object_id, now=S.clock(),
                           details={"job_id": job["id"], "counts": pv["counts"]})
        background.add_task(dl.run_job, S.store, job["id"], clock=S.clock)
        return _job_view(job)

    @router.get("/deletion-jobs/{job_id}")
    @translate
    async def get_job(job_id: str, user: User = Depends(principal)):
        if not isinstance(job_id, str) or len(job_id) > 100:
            raise NotFound("Not found.")
        job = await S.store.get(dl.JOBS, job_id)
        if job is None:
            raise NotFound("Not found.")
        if not user.is_platform_superadmin and job.get("requested_by") != user.id:
            eid = job.get("enterprise_id")
            ok = False
            if eid:
                try:
                    await load_access(S, user, Level.ENTERPRISE, eid, Action.DELETE)
                    ok = True
                except (NotFound, AuthzError):
                    ok = False
            if not ok:
                raise NotFound("Not found.")
        return _job_view(job)

    @router.post("/deletion-jobs/{job_id}/resume")
    @translate
    async def resume_job(job_id: str, user: User = Depends(principal)):
        """Operator: retry failed steps and give failed cleanup actions a fresh retry budget."""
        require_superadmin(user)
        job = await S.store.get(dl.JOBS, job_id)
        if job is None or job.get("target_level") == "user":
            raise NotFound("Not found.")
        await dl.retry_failed(S.store, job_id, clock=S.clock)
        await audit.record(S.store, actor=user.actor(), action="deletion.resume", enterprise_id=job.get("enterprise_id"),
                           target_level="deletion_job", target_id=job_id, now=S.clock(), details={})
        return _job_view(await dl.run_job(S.store, job_id, clock=S.clock))

    @router.post("/deletion-jobs/{job_id}/actions/{action_id}/resolve")
    @translate
    async def resolve_action(job_id: str, action_id: str, body: ResolveActionBody, user: User = Depends(principal)):
        """Operator: explicitly establish that an external cleanup action is not required."""
        require_superadmin(user)
        job = await S.store.get(dl.JOBS, job_id)
        if job is None or job.get("target_level") == "user":
            raise NotFound("Not found.")
        await dl.resolve_action(S.store, job_id, action_id, actor=user.actor(), reason=body.reason, clock=S.clock)
        await audit.record(S.store, actor=user.actor(), action="deletion.cleanup_resolved_by_operator",
                           enterprise_id=job.get("enterprise_id"), target_level="deletion_job", target_id=job_id,
                           now=S.clock(), details={"action": action_id, "reason": body.reason})
        return _job_view(await dl.run_job(S.store, job_id, clock=S.clock))

    # ── users ──
    async def user_preview(actor: User, target_id: str):
        pv = await dl.preview_user(S.store, target_id)
        token, expires = await dl.issue_token(S.store, actor_id=actor.id, level="user", target_id=target_id,
                                              digest=pv["digest"], now=S.clock())
        return {**pv, "preview_token": token, "expires_at": expires}

    async def user_delete(actor: User, target_id: str, body: UserDeleteConfirm):
        target = await get_user(S.store, target_id)
        if target is None:
            raise NotFound("Not found.")
        token_doc = await dl.consume_token(S.store, body.preview_token, actor_id=actor.id, level="user",
                                           target_id=target_id, now=S.clock())
        if body.confirm_email.strip().lower() != target.email:
            raise dl.DeletionError("The typed email address does not match. Request a new deletion preview.")
        pv = await dl.preview_user(S.store, target_id)
        if pv["digest"] != token_doc["digest"]:
            raise dl.DeletionError("The account changed since the preview. Request a new deletion preview.")
        if pv["blocked"]:
            raise dl.DeletionError("This account cannot be deleted yet: "
                                   + ", ".join(b["reason"].replace("_", " ") for b in pv["blocked"]) + ".")
        job = await dl.delete_user(S.store, target_id, actor=actor.actor(), clock=S.clock)
        await audit.record(S.store, actor=actor.actor(), action="user.delete", enterprise_id=None,
                           target_level="user", target_id=target_id, now=S.clock(),
                           details={"job_id": job["id"], "status": job["status"]})
        return _job_view(job)

    @router.get("/users/me")
    @translate
    async def me(user: User = Depends(principal)):
        out = []
        for eid in await ms.enterprises_of(S.store, user.id):
            roster = await ms.load_for(S.store, eid, [user.id])
            out += [{"enterprise_id": eid, "scope_type": m.scope_level.value, "scope_id": m.scope_id,
                     "role": m.role.name.lower()} for m in roster.for_user(user.id)]
        return {**user.public_view(), "memberships": out}

    @router.post("/users/me/deletion-preview")
    @translate
    async def my_deletion_preview(user: User = Depends(principal)):
        return await user_preview(user, user.id)

    @router.post("/users/me/delete", status_code=202)
    @translate
    async def delete_me(body: UserDeleteConfirm, user: User = Depends(principal)):
        return await user_delete(user, user.id, body)

    @router.post("/platform/users/{user_id}/deletion-preview")
    @translate
    async def platform_user_deletion_preview(user_id: str, user: User = Depends(principal)):
        require_superadmin(user)
        return await user_preview(user, user_id)

    @router.post("/platform/users/{user_id}/delete", status_code=202)
    @translate
    async def platform_user_delete(user_id: str, body: UserDeleteConfirm, user: User = Depends(principal)):
        require_superadmin(user)
        return await user_delete(user, user_id, body)

    @router.post("/platform/users/{user_id}/superadmin")
    @translate
    async def set_superadmin(user_id: str, body: SuperadminBody, user: User = Depends(principal)):
        require_superadmin(user)
        if body.grant:
            changed = await grant_superadmin(S.store, user.actor(), user_id)
        else:
            changed = await revoke_superadmin(S.store, user.actor(), user_id)
        await audit.record(S.store, actor=user.actor(), action="superadmin.grant" if body.grant else "superadmin.revoke",
                           enterprise_id=None, target_level="user", target_id=user_id, now=S.clock(),
                           details={"changed": changed})
        target = await get_user(S.store, user_id)
        return target.public_view() if target else {"id": user_id}

    # ── audit ──
    @router.get("/enterprises/{enterprise_id}/audit")
    @translate
    async def enterprise_audit(enterprise_id: str, limit: int = Query(100, ge=1, le=500),
                               user: User = Depends(principal)):
        await load_access(S, user, Level.ENTERPRISE, enterprise_id, Action.MANAGE_MEMBERS)   # admin+
        docs = await S.store.find(audit.COLLECTION, {"enterprise_id": enterprise_id}, sort="at",
                                  descending=True, limit=limit)
        return {"events": [{k: v for k, v in d.items() if k != "version"} for d in docs]}

    @router.get("/platform/audit")
    @translate
    async def platform_audit(limit: int = Query(100, ge=1, le=500), user: User = Depends(principal)):
        require_superadmin(user)
        docs = await S.store.find(audit.COLLECTION, {}, sort="at", descending=True, limit=limit)
        return {"events": [{k: v for k, v in d.items() if k != "version"} for d in docs]}
