"""Permanent deletion (spec 012 §8; D-Q3; X12 approved 2026-11-18).

Workflow
--------
1. :func:`preview`: exact counts per collection, the external cleanup that
   will be required, safeguards, and a digest of the structural counts.
   :func:`issue_token` stores only the SHA-256 of a short-lived, single-use
   confirmation token bound to (actor, object, digest).
2. Confirmation (API): :func:`consume_token` atomically takes the token. The
   API re-checks authorisation, the typed name and the digest, marks the
   target DELETED with a compare-and-swap (so it and every descendant become
   unavailable at once), and creates the job with a deterministic id
   (``<level>:<id>``) so repeated confirmations converge on one job.
3. :func:`run_job`: idempotent, resumable and leased. Data is removed
   descendant-first; every query is pinned to the Enterprise id.

External cleanup (X12)
----------------------
Each external resource gets an action record (no secrets, only references):
``id, action, target, mandatory, state, attempts, next_attempt_at,
error_class, credential_id, history``.

* Action states: ``pending`` → ``succeeded`` | ``not_required`` |
  ``retry_wait`` (back-off) → … | ``failed`` (mandatory, retries exhausted:
  needs an operator) | ``abandoned`` (best-effort, retries exhausted).
  Resolved = ``succeeded``, ``not_required``, or ``abandoned`` for a
  best-effort action.
* Job states: ``pending`` → ``running`` → ``awaiting_external_cleanup`` →
  ``completed``. Failure states: ``failed`` (an internal step failed;
  resumable) and ``cleanup_failed`` (a mandatory action exhausted its
  retries; an operator retries it or records that it is not required).
  **A job is ``completed`` only when every mandatory action has succeeded or
  is established as not required.**
* Credentials needed by an unresolved action are kept (still encrypted in
  ``credentials``; the job holds only the credential id) and are deleted only
  once the actions using them have resolved. The API also refuses to revoke
  or delete a credential that an unresolved action still needs.
* Discord leave-guild is best-effort and is decided **when it runs**, from the
  live binding claims of every tenant. If any instance still holds the guild
  it is ``not_required``. Platform credentials are never touched.
* Executors (Phase 4 adapters) must be idempotent; "already gone" counts as
  success. Phase 3.1 registers none, so jobs that need external cleanup stay
  ``awaiting_external_cleanup``.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from core import bindings as bnd
from core import memberships as ms
from core.authz import Actor, AuthzError
from core.hierarchy import (COLLECTION, ChannelType, ConcurrentModification, DuplicateKey, HierarchyError, Level,
                            LifecycleState, NotFound, binding_from_doc)
from core.identity import USERS, get_user, revoke_superadmin

PREVIEWS = "deletion_previews"
JOBS = "deletion_jobs"
CREDENTIALS = "credentials"
INVITATIONS = "invitations"
BOT_INTEGRATIONS = "bot_integrations"
TOKEN_TTL_SECONDS = 300
REPURGE_DELAY_SECONDS = 300          # > request leases (120 s) and provider/channel timeouts
LEASE_SECONDS = 120
MAX_ATTEMPTS_MANDATORY = 10
MAX_ATTEMPTS_BEST_EFFORT = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600

OWNER_FIELD = {Level.ENTERPRISE: "enterprise_id", Level.ORGANISATION: "organisation_id",
               Level.BOT: "bot_id", Level.BOT_INSTANCE: "instance_id"}
DESCENDANT_LEVELS = {Level.ENTERPRISE: (Level.BOT_INSTANCE, Level.BOT, Level.ORGANISATION),
                     Level.ORGANISATION: (Level.BOT_INSTANCE, Level.BOT),
                     Level.BOT: (Level.BOT_INSTANCE,), Level.BOT_INSTANCE: ()}
RUNTIME_COLLECTIONS = ("conversations", "chat_sessions", "chat_requests", "suggestions", "usage_events",
                       "usage_counters", "usage_ledger", "channel_state", "outbound_deliveries",
                       "oauth_states", "discord_share_links", "zendesk_events",
                       # Phase 4K: Enterprise deletion removes all; Organisation deletion its own sources
                       "knowledge_chunks", "knowledge_files", "knowledge_documents", "knowledge_sources")
# D-Q14: the Enterprise's own Discord application (with its encrypted token) is
# needed by leave-guild cleanup, so it goes only when the Enterprise job finalises.
ENTERPRISE_FINAL_COLLECTIONS = ("discord_applications", "discord_installations", "discord_gateway_status")
BOT_OWNED_COLLECTIONS = ("bot_integrations", "api_endpoints", "bot_logos", "zendesk_app_keys",
                         "api_integrations", "api_activity")             # R3: API integrations

STEPS = ("release_bindings", "invitations", "memberships", "runtime_data", "bot_owned", "credentials",
         "descendants", "target")
IMMEDIATE_STEPS = ("release_bindings", "invitations", "memberships")   # access revoked at confirmation

RESOLVED = ("succeeded", "not_required")
ACTIVE_JOB_STATES = ("pending", "running", "awaiting_external_cleanup", "cleanup_failed", "failed")
_ERROR_CLASS = re.compile(r"[a-z][a-z0-9_]{0,39}")


class DeletionError(HierarchyError):
    pass


def _now_dt(now: str) -> datetime:
    return datetime.fromisoformat(now)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def job_id_for(level: str, target_id: str) -> str:
    return f"{level}:{target_id}"


# ═════════════════════════════════ scope ═════════════════════════════════

def _scoped(level: Level, target_id: str, enterprise_id: str) -> Dict[str, str]:
    """Ownership filter for records *below or at* the target (always tenant-pinned)."""
    if level is Level.ENTERPRISE:
        return {"enterprise_id": enterprise_id}
    return {"enterprise_id": enterprise_id, OWNER_FIELD[level]: target_id}


async def _subtree_ids(store, level: Level, target_id: str, enterprise_id: str) -> Dict[Level, List[str]]:
    out: Dict[Level, List[str]] = {level: [target_id]}
    for lvl in DESCENDANT_LEVELS[level]:
        docs = await store.find(COLLECTION[lvl], _scoped(level, target_id, enterprise_id))
        out[lvl] = sorted(d["id"] for d in docs if d.get("enterprise_id") == enterprise_id)
    return out


def _scope_ids(level: Level, ids: Dict[Level, List[str]]) -> List[str]:
    return sorted(set(ids.get(level, [])) | set(ids.get(Level.ORGANISATION, [])) | set(ids.get(Level.BOT, [])))


async def preview(store, level: Level, target_id: str, enterprise_id: str) -> Dict[str, Any]:
    ids = await _subtree_ids(store, level, target_id, enterprise_id)
    flt = _scoped(level, target_id, enterprise_id)
    counts: Dict[str, int] = {}
    for lvl in (Level.ORGANISATION, Level.BOT, Level.BOT_INSTANCE):
        if lvl in DESCENDANT_LEVELS[level] or lvl is level:
            counts[COLLECTION[lvl]] = len(ids.get(lvl, []))
    if level is Level.ENTERPRISE:
        counts["enterprises"] = 1
    if level in (Level.ENTERPRISE, Level.ORGANISATION):
        counts[CREDENTIALS] = await store.count(CREDENTIALS, flt)
    scope_ids = set(_scope_ids(level, ids))
    roster = await ms.load(store, enterprise_id)
    counts["memberships"] = len(roster.members) if level is Level.ENTERPRISE else \
        sum(1 for m in roster.members if m.scope_id in scope_ids)
    invites = await store.find(INVITATIONS, {"enterprise_id": enterprise_id, "status": "pending"})
    counts[INVITATIONS] = len(invites) if level is Level.ENTERPRISE else \
        sum(1 for i in invites if i.get("scope_id") in scope_ids)
    structural = dict(counts)
    for col in RUNTIME_COLLECTIONS:
        counts[col] = await store.count(col, flt)
    if level is not Level.BOT_INSTANCE:
        for col in BOT_OWNED_COLLECTIONS:
            counts[col] = await store.count(col, flt)
    actions = await external_actions(store, ids.get(Level.BOT_INSTANCE, []), level, target_id, enterprise_id)
    return {"level": level.value, "target_id": target_id, "enterprise_id": enterprise_id, "counts": counts,
            "digest": hashlib.sha256(json.dumps(structural, sort_keys=True).encode()).hexdigest(),
            "external_actions": actions}


def _action(action: str, ref: str, target: Dict[str, Any], *, mandatory: bool,
            credential_id: Optional[str] = None) -> Dict[str, Any]:
    return {"id": f"{action}:{ref}", "action": action, "target": target, "mandatory": mandatory,
            "credential_id": credential_id, "state": "pending", "attempts": 0, "next_attempt_at": None,
            "error_class": None, "resolution": None, "history": []}


async def external_actions(store, instance_ids: List[str], level: Level, target_id: str,
                           enterprise_id: str) -> List[Dict[str, Any]]:
    """External cleanup required by deleting this subtree (references only)."""
    out: Dict[str, Dict[str, Any]] = {}
    for iid in instance_ids:
        doc = await store.get(COLLECTION[Level.BOT_INSTANCE], iid)
        if not doc or doc.get("enterprise_id") != enterprise_id:
            continue
        try:
            ch = binding_from_doc(doc.get("channel") or {})
        except HierarchyError:
            continue
        if ch.type is ChannelType.DISCORD and ch.guild_id and ch.application_id:
            target = {"instance_id": iid, "guild_id": ch.guild_id, "application_id": ch.application_id,
                      "application_enterprise_id": enterprise_id}
            a = _action("discord_routing_removal", iid, target, mandatory=True)
            out[a["id"]] = a
            # D-Q13: best-effort, decided when it runs from the live claims of that application
            a = _action("discord_leave_guild", f"{ch.application_id}:{ch.guild_id}",
                        {"guild_id": ch.guild_id, "application_id": ch.application_id,
                         "application_enterprise_id": enterprise_id}, mandatory=False)
            out[a["id"]] = a
        elif ch.type is ChannelType.TELEGRAM and ch.credential_id:
            a = _action("telegram_delete_webhook", iid, {"instance_id": iid,
                                                         "telegram_bot_id": doc.get("telegram_bot_id")},
                        mandatory=True, credential_id=ch.credential_id)
            out[a["id"]] = a
    if level is not Level.BOT_INSTANCE:
        for integ in await store.find(BOT_INTEGRATIONS, _scoped(level, target_id, enterprise_id)):
            if integ.get("type") == "zendesk" and integ.get("enterprise_id") == enterprise_id:
                webhook = integ.get("webhook") or {}
                hook = webhook.get("zendesk_webhook_id")
                prov = webhook.get("provision") or {}
                # Z-C: cleanup uses the provisioning credential and account, never the current one
                a = _action("zendesk_webhook_subscription_removal", integ["id"],
                            {"integration_id": integ["id"], "webhook_id": hook, "subdomain": prov.get("subdomain")},
                            mandatory=True, credential_id=prov.get("credential_id") if hook else None)
                out[a["id"]] = a
    if level in (Level.ENTERPRISE, Level.ORGANISATION):
        for c in await store.find(CREDENTIALS, _scoped(level, target_id, enterprise_id)):
            if c.get("provider") == "zendesk_oauth" and c.get("status") == "active":
                a = _action("zendesk_token_revocation", c["id"], {"credential_id": c["id"]}, mandatory=True,
                            credential_id=c["id"])
                out[a["id"]] = a
    return [out[k] for k in sorted(out)]


# ═════════════════════════════════ confirmation tokens ═════════════════════════════════

async def issue_token(store, *, actor_id: str, level: str, target_id: str, digest: str, now: str,
                      ttl_seconds: int = TOKEN_TTL_SECONDS) -> Tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = _now_dt(now) + timedelta(seconds=ttl_seconds)
    await store.insert(PREVIEWS, {"id": _hash(token), "actor_id": actor_id, "level": level, "target_id": target_id,
                                  "digest": digest, "expires_at": expires.isoformat(), "expires_at_dt": expires,
                                  "version": 1})
    return token, expires.isoformat()


async def consume_token(store, token: Any, *, actor_id: str, level: str, target_id: str, now: str) -> dict:
    invalid = DeletionError("The confirmation is invalid or has expired. Request a new deletion preview.")
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        raise invalid
    doc = await store.take(PREVIEWS, _hash(token))           # atomic: single use
    if doc is None or doc.get("actor_id") != actor_id or doc.get("level") != level \
            or doc.get("target_id") != target_id:
        raise invalid
    if _now_dt(now) >= datetime.fromisoformat(doc["expires_at"]):
        raise invalid
    return doc


# ═════════════════════════════════ executors (Phase 4 adapters) ═════════════════════════════════

class CleanupStatus(str, Enum):
    SUCCEEDED = "succeeded"
    NOT_REQUIRED = "not_required"
    RETRY = "retry"


@dataclass(frozen=True)
class CleanupOutcome:
    status: CleanupStatus
    error_class: Optional[str] = None          # sanitised category, never provider text


@dataclass(frozen=True)
class CleanupContext:
    store: Any
    now: str

    async def credential_doc(self, credential_id: Optional[str]) -> Optional[dict]:
        """The encrypted credential record (the executor decrypts in memory)."""
        if not credential_id:
            return None
        return await self.store.get(CREDENTIALS, credential_id)


Executor = Callable[[Dict[str, Any], CleanupContext], Awaitable[CleanupOutcome]]


def _sanitise(error_class: Optional[str]) -> str:
    value = (error_class or "error").strip().lower()
    return value if _ERROR_CLASS.fullmatch(value) else "error"


def _backoff(attempts: int) -> int:
    return min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_MAX_SECONDS)


def action_resolved(a: Dict[str, Any]) -> bool:
    return a["state"] in RESOLVED or (a["state"] == "abandoned" and not a["mandatory"])


async def unresolved_credential_holds(store, credential_id: str) -> List[str]:
    """Deletion jobs whose unresolved external cleanup still needs this credential."""
    jobs = await store.find(JOBS, {"external_actions.credential_id": credential_id})
    return sorted(j["id"] for j in jobs
                  if any(a.get("credential_id") == credential_id and not action_resolved(a)
                         for a in j.get("external_actions") or []))


# ═════════════════════════════════ jobs ═════════════════════════════════

async def create_job(store, *, level: str, target_id: str, enterprise_id: Optional[str], target_name: str,
                     actor: Actor, now: str, external: List[Dict[str, Any]],
                     counts: Dict[str, int]) -> Tuple[dict, bool]:
    """Returns (job, created). A second confirmation returns the existing job."""
    actions = [{**a, "state": "pending", "attempts": 0, "next_attempt_at": None, "error_class": None,
                "resolution": None, "history": []} for a in external]
    doc = {"id": job_id_for(level, target_id), "target_level": level, "target_id": target_id,
           "enterprise_id": enterprise_id, "target_name": target_name, "requested_by": actor.user_id,
           "requested_by_superadmin": actor.is_platform_superadmin, "requested_at": now, "status": "pending",
           "steps_done": [], "deleted": {}, "preview_counts": counts, "external_actions": actions,
           "attempts": 0, "lease_until": None, "error": None, "completed_at": None, "version": 1}
    try:
        await store.insert(JOBS, doc)
        return doc, True
    except DuplicateKey:
        return await store.get(JOBS, doc["id"]), False


async def _acquire(store, job_id: str, now: str, worker: str) -> Optional[dict]:
    for _ in range(16):
        job = await store.get(JOBS, job_id)
        if job is None or job["status"] in ("completed", "cleanup_failed"):
            return None
        if job["status"] == "running" and job.get("lease_until") and \
                _now_dt(now) < datetime.fromisoformat(job["lease_until"]):
            return None                                      # another worker holds the lease
        lease = (_now_dt(now) + timedelta(seconds=LEASE_SECONDS)).isoformat()
        try:
            await store.update(JOBS, job_id, {"status": "running", "lease_until": lease, "worker": worker,
                                              "attempts": job.get("attempts", 0) + 1, "error": None},
                               expect_version=job["version"])
            return await store.get(JOBS, job_id)
        except ConcurrentModification:
            continue
    return None


def _held_credentials(job: dict) -> List[str]:
    return sorted({a["credential_id"] for a in job.get("external_actions") or []
                   if a.get("credential_id") and not action_resolved(a)})


async def _step(store, job: dict, name: str) -> int:
    level = Level(job["target_level"])
    tid, eid = job["target_id"], job["enterprise_id"]
    flt = _scoped(level, tid, eid)
    if name == "release_bindings":
        ids = await _subtree_ids(store, level, tid, eid)
        reg = bnd.BindingRegistry(store)
        n = 0
        for iid in ids.get(Level.BOT_INSTANCE, []):
            keys = await reg.keys_held_by(iid)
            await reg.release(iid, keys)
            n += len(keys)
        return n
    if name == "invitations":
        if level is Level.ENTERPRISE:
            return await store.delete_many(INVITATIONS, {"enterprise_id": eid})
        scope_ids = _scope_ids(level, await _subtree_ids(store, level, tid, eid))
        return await store.delete_many(INVITATIONS, {"enterprise_id": eid, "scope_id": scope_ids}) if scope_ids else 0
    if name == "memberships":
        if level is Level.ENTERPRISE:
            return await ms.delete_enterprise_memberships(store, eid)
        if level is Level.BOT_INSTANCE:
            return 0
        scope_ids = set(_scope_ids(level, await _subtree_ids(store, level, tid, eid)))
        before = sum(1 for m in (await ms.load(store, eid)).members if m.scope_id in scope_ids)
        await ms.mutate(store, eid, ms.op_remove_scopes(scope_ids))
        return before
    if name == "runtime_data":
        return sum([await store.delete_many(col, flt) for col in RUNTIME_COLLECTIONS])
    if name == "bot_owned":
        if level is Level.BOT_INSTANCE:
            return 0
        return sum([await store.delete_many(col, flt) for col in BOT_OWNED_COLLECTIONS])
    if name == "credentials":
        if level not in (Level.ENTERPRISE, Level.ORGANISATION):
            return 0
        held = set(_held_credentials(job))               # kept until their cleanup has resolved
        n = 0
        for c in await store.find(CREDENTIALS, flt):
            if c["id"] not in held and c.get("enterprise_id") == eid:
                n += int(await store.delete(CREDENTIALS, c["id"]))
        return n
    if name == "descendants":
        n = 0
        for lvl in DESCENDANT_LEVELS[level]:                 # deepest first
            n += await store.delete_many(COLLECTION[lvl], flt)
        return n
    if name == "target":
        if level is Level.ENTERPRISE:
            await ms.delete_enterprise_memberships(store, eid)          # sweep late writes
        doc = await store.get(COLLECTION[level], tid)
        if doc is None:
            return 0
        if (doc.get("lifecycle") or {}).get("state") != LifecycleState.DELETED.value:
            raise DeletionError("Target is not marked for deletion.")
        if level is not Level.ENTERPRISE and doc.get("enterprise_id") != eid:
            raise DeletionError("Target ownership mismatch.")
        return int(await store.delete(COLLECTION[level], tid))
    raise DeletionError("Unknown step.")


async def _cleanup_pass(store, job: dict, *, clock: Callable[[], str],
                        executors: Mapping[str, Executor]) -> Tuple[dict, List[Tuple[str, Dict[str, Any]]]]:
    """Attempt every due, unresolved action once. Returns the job and audit notes."""
    now = clock()
    actions = [dict(a) for a in job.get("external_actions") or []]
    notes: List[Tuple[str, Dict[str, Any]]] = []
    for a in actions:
        if action_resolved(a) or a["state"] == "failed":
            continue
        if a.get("next_attempt_at") and _now_dt(now) < _now_dt(a["next_attempt_at"]):
            continue
        if a["action"] == "discord_leave_guild":
            key = f"discord:{a['target'].get('application_id')}:{a['target']['guild_id']}"
            holders = await bnd.BindingRegistry(store).holders(key)
            if holders:                                       # another instance (any tenant) still needs it
                a.update(state="not_required", resolution={"reason": "guild_in_use", "at": now})
                notes.append(("deletion.cleanup_not_required", {"action": a["id"], "reason": "guild_in_use"}))
                continue
        executor = executors.get(a["action"])
        if executor is None:
            a.update(error_class="no_adapter")              # Phase 3.1: nothing is executed; stays pending
            continue
        try:
            # one idempotency key per (job, action): an adapter re-invoked after a crash
            # must recognise the key and not repeat the external effect
            outcome = await executor({**a, "job_id": job["id"], "idempotency_key": f"{job['id']}:{a['id']}"},
                                     CleanupContext(store, now))
        except Exception as exc:                              # never trust executor exceptions to be safe text
            outcome = CleanupOutcome(CleanupStatus.RETRY, _sanitise(type(exc).__name__.lower()))
        a["attempts"] = int(a.get("attempts", 0)) + 1
        a["history"] = (list(a.get("history") or []) + [{"at": now, "outcome": outcome.status.value,
                                                         "error_class": outcome.error_class and
                                                         _sanitise(outcome.error_class)}])[-10:]
        if outcome.status is CleanupStatus.SUCCEEDED:
            a.update(state="succeeded", error_class=None, next_attempt_at=None)
            notes.append(("deletion.cleanup_succeeded", {"action": a["id"]}))
        elif outcome.status is CleanupStatus.NOT_REQUIRED:
            a.update(state="not_required", error_class=None, next_attempt_at=None,
                     resolution={"reason": "adapter_reported_not_required", "at": now})
            notes.append(("deletion.cleanup_not_required", {"action": a["id"]}))
        else:
            limit = MAX_ATTEMPTS_MANDATORY if a["mandatory"] else MAX_ATTEMPTS_BEST_EFFORT
            a["error_class"] = _sanitise(outcome.error_class)
            if a["attempts"] >= limit:
                a.update(state="failed" if a["mandatory"] else "abandoned", next_attempt_at=None)
            else:
                a.update(state="retry_wait",
                         next_attempt_at=(_now_dt(now) + timedelta(seconds=_backoff(a["attempts"]))).isoformat())
            notes.append(("deletion.cleanup_failed", {"action": a["id"], "error_class": a["error_class"],
                                                      "attempts": a["attempts"], "state": a["state"]}))
    job = await _save(store, job, external_actions=actions)
    return job, notes


async def run_job(store, job_id: str, *, clock: Callable[[], str], worker: str = "inline",
                  only: Optional[Tuple[str, ...]] = None,
                  executors: Optional[Mapping[str, Executor]] = None) -> Optional[dict]:
    """Run (or resume) a hierarchy deletion job. ``only`` limits the run to the
    given data steps (the immediate part at confirmation). Safe to call
    repeatedly and concurrently (leased)."""
    from core import audit
    job = await _acquire(store, job_id, clock(), worker)
    if job is None:
        return await store.get(JOBS, job_id)
    notes: List[Tuple[str, Dict[str, Any]]] = []
    try:
        for _sweep in range(5):
            for name in STEPS:
                if only is not None and name not in only:
                    continue
                if name in job["steps_done"] and name != "target":
                    continue
                if name == "target":
                    # children created concurrently with the deletion are removed first
                    ids = await _subtree_ids(store, Level(job["target_level"]), job["target_id"], job["enterprise_id"])
                    if any(ids.get(lvl) for lvl in DESCENDANT_LEVELS[Level(job["target_level"])]):
                        job = await _save(store, job, steps_done=[])     # run every step again
                        break
                n = await _step(store, job, name)
                deleted = dict(job["deleted"])
                deleted[name] = deleted.get(name, 0) + n
                job = await _save(store, job, steps_done=sorted(set(job["steps_done"]) | {name},
                                                                key=STEPS.index), deleted=deleted)
            else:
                break
        if only is not None:
            job = await _save(store, job, status="pending", lease_until=None)
        else:
            job, notes = await _cleanup_pass(store, job, clock=clock, executors=executors or {})
            actions = job.get("external_actions") or []
            if all(action_resolved(a) for a in actions):
                # all cleanup resolved: credentials kept for it can go now
                if job["enterprise_id"] and Level(job["target_level"]) in (Level.ENTERPRISE, Level.ORGANISATION):
                    flt = _scoped(Level(job["target_level"]), job["target_id"], job["enterprise_id"])
                    n = await store.delete_many(CREDENTIALS, flt)
                    deleted = dict(job["deleted"])
                    deleted["retained_credentials"] = deleted.get("retained_credentials", 0) + n
                    job = await _save(store, job, deleted=deleted)
                if job["enterprise_id"] and Level(job["target_level"]) is Level.ENTERPRISE:
                    n = sum([await store.delete_many(col, {"enterprise_id": job["enterprise_id"]})
                             for col in ENTERPRISE_FINAL_COLLECTIONS])
                    deleted = dict(job["deleted"])
                    deleted["discord_application"] = deleted.get("discord_application", 0) + n
                    job = await _save(store, job, deleted=deleted)
                # Phase 4F: requests already in flight at confirmation may still write runtime
                # records after the purge; a grace re-purge (worker) removes them later
                job = await _save(store, job, status="completed", lease_until=None, completed_at=clock(),
                                  repurge_at=(_now_dt(clock()) + timedelta(seconds=REPURGE_DELAY_SECONDS)).isoformat(),
                                  repurged=False)
            elif any(a["state"] == "failed" for a in actions):
                job = await _save(store, job, status="cleanup_failed", lease_until=None)
            else:
                job = await _save(store, job, status="awaiting_external_cleanup", lease_until=None)
    except Exception as exc:                                 # recorded, resumable; no details leak
        job = await store.get(JOBS, job_id) or job
        try:
            job = await _save(store, job, status="failed", lease_until=None, error=type(exc).__name__)
        except HierarchyError:
            pass
    for action, details in notes:
        await audit.record(store, actor=None, action=action, enterprise_id=job.get("enterprise_id"),
                           target_level="deletion_job", target_id=job_id, now=clock(), details=details)
    return job


async def repurge_due(store, *, now: str, limit: int = 20) -> int:
    """Grace re-purge of a completed job's tenant-scoped runtime data, once,
    after ``REPURGE_DELAY_SECONDS`` (Phase 4F load finding: in-flight requests
    could write runtime records after the original purge)."""
    done = 0
    for job in await store.find(JOBS, {"status": "completed", "repurged": False}, limit=limit):
        if not job.get("repurge_at") or _now_dt(now) < _now_dt(job["repurge_at"]):
            continue
        n = await _step(store, job, "runtime_data")
        if Level(job["target_level"]) is not Level.BOT_INSTANCE:
            n += await _step(store, job, "bot_owned")
        try:
            await _save(store, job, repurged=True, repurged_records=n)
        except Exception:
            continue
        done += 1
    return done


async def resolve_action(store, job_id: str, action_id: str, *, actor: Actor, reason: str,
                         clock: Callable[[], str]) -> dict:
    """Operator decision (platform superadmin): an unresolved action is
    explicitly established as not required. Audited by the caller."""
    if not actor.is_platform_superadmin:
        raise AuthzError("Only platform superadmins can do this.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise DeletionError("A reason (1-500 characters) is required.")
    for _ in range(16):
        job = await store.get(JOBS, job_id)
        if job is None:
            raise NotFound("Not found.")
        actions = [dict(a) for a in job.get("external_actions") or []]
        target = next((a for a in actions if a["id"] == action_id), None)
        if target is None:
            raise NotFound("Not found.")
        if action_resolved(target):
            return job
        target.update(state="not_required", next_attempt_at=None,
                      resolution={"reason": "operator", "note": reason.strip(), "by": actor.user_id, "at": clock()})
        fields: Dict[str, Any] = {"external_actions": actions}
        if job["status"] == "cleanup_failed":
            fields["status"] = "awaiting_external_cleanup"
        try:
            await store.update(JOBS, job_id, fields, expect_version=job["version"])
            return await store.get(JOBS, job_id)
        except ConcurrentModification:
            continue
    raise ConcurrentModification()


async def retry_failed(store, job_id: str, *, clock: Callable[[], str]) -> dict:
    """Operator retry: failed mandatory actions get a fresh retry budget."""
    for _ in range(16):
        job = await store.get(JOBS, job_id)
        if job is None:
            raise NotFound("Not found.")
        actions = [dict(a) for a in job.get("external_actions") or []]
        for a in actions:
            if a["state"] == "failed":
                a.update(state="pending", attempts=0, next_attempt_at=None)
        fields: Dict[str, Any] = {"external_actions": actions}
        if job["status"] == "cleanup_failed":
            fields["status"] = "awaiting_external_cleanup"
        try:
            await store.update(JOBS, job_id, fields, expect_version=job["version"])
            return await store.get(JOBS, job_id)
        except ConcurrentModification:
            continue
    raise ConcurrentModification()


async def _save(store, job: dict, **fields) -> dict:
    await store.update(JOBS, job["id"], fields, expect_version=job["version"])
    return {**job, **fields, "version": job["version"] + 1}


# ═════════════════════════════════ users ═════════════════════════════════

async def preview_user(store, user_id: str) -> Dict[str, Any]:
    user = await get_user(store, user_id)
    if user is None:
        raise NotFound("Not found.")
    enterprises = await ms.enterprises_of(store, user_id)
    blocked: List[Dict[str, str]] = []
    memberships = 0
    for eid in enterprises:
        mine = await ms.load_for(store, eid, [user_id])
        memberships += len(mine.members)
        if user_id in mine.owners() and await ms.owner_count(store, eid) <= 1:
            blocked.append({"reason": "last_owner", "enterprise_id": eid})
    if user.is_platform_superadmin:
        from core.identity import PLATFORM_STATE, SUPERADMIN_GUARD_ID
        guard = await store.get(PLATFORM_STATE, SUPERADMIN_GUARD_ID)
        if guard is None or guard["count"] < 2:
            blocked.append({"reason": "last_superadmin"})
    invites = await store.count(INVITATIONS, {"email": user.email, "status": "pending"}) + \
        await store.count(INVITATIONS, {"invited_by": user_id, "status": "pending"})
    counts = {"users": 1, "memberships": memberships, INVITATIONS: invites}
    return {"level": "user", "target_id": user_id, "counts": counts, "blocked": blocked,
            "digest": hashlib.sha256(json.dumps(counts, sort_keys=True).encode()).hexdigest()}


async def delete_user(store, user_id: str, *, actor: Actor, clock: Callable[[], str]) -> dict:
    """Idempotent user deletion (spec §8). Shared resources stay with their
    scopes; audit entries keep only the user id. Memberships are removed first
    (each Enterprise through the membership protocol, refusing for a last
    Owner), then the account is deactivated (sessions end), superadmin status
    revoked through the guard, invitations removed and the user deleted."""
    system = Actor(user_id=actor.user_id, is_platform_superadmin=True)
    user = await get_user(store, user_id)
    job, _ = await create_job(store, level="user", target_id=user_id, enterprise_id=None,
                              target_name="", actor=actor, now=clock(), external=[], counts={})
    if job["status"] == "completed":
        return job
    try:
        removed = 0
        for eid in await ms.enterprises_of(store, user_id):
            removed += len((await ms.load_for(store, eid, [user_id])).members)
            await ms.mutate(store, eid, ms.op_remove_user(user_id), clock=clock)
        if user is not None:
            for _ in range(16):
                u = await get_user(store, user_id)
                if u is None:
                    break
                try:
                    await store.update(USERS, user_id, {"status": "disabled", "token_version": u.token_version + 1},
                                       expect_version=u.version)
                    break
                except ConcurrentModification:
                    continue
            if user.is_platform_superadmin:
                await revoke_superadmin(store, system, user_id)
        inv = await store.delete_many(INVITATIONS, {"invited_by": user_id, "status": "pending"})
        if user is not None:
            inv += await store.delete_many(INVITATIONS, {"email": user.email, "status": "pending"})
        gone = int(await store.delete(USERS, user_id))
        for eid in await ms.enterprises_of(store, user_id):          # sweep entries added meanwhile
            await ms.mutate(store, eid, ms.op_remove_user(user_id, enforce_owner=False), clock=clock)
        job = await store.get(JOBS, job["id"])
        job = await _save(store, job, status="completed", completed_at=clock(),
                          steps_done=["memberships", "deactivate", "superadmin", "invitations", "user"],
                          deleted={"memberships": removed, INVITATIONS: inv, "users": gone})
    except (AuthzError, HierarchyError) as exc:
        job = await store.get(JOBS, job["id"])
        job = await _save(store, job, status="failed", error=str(exc)[:200])
    return job


__all__ = ["PREVIEWS", "JOBS", "STEPS", "IMMEDIATE_STEPS", "DeletionError", "job_id_for", "preview",
           "external_actions", "issue_token", "consume_token", "CleanupStatus", "CleanupOutcome", "CleanupContext",
           "Executor", "action_resolved", "unresolved_credential_holds", "create_job", "run_job",
           "resolve_action", "retry_failed", "preview_user", "delete_user"]
