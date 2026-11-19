"""Membership storage and atomic membership changes (spec 012 §9; X4 option C,
approved 2026-11-18). Works on a standalone MongoDB (no transactions).

Storage
-------
* ``memberships``: **one document per (enterprise_id, user_id)**
  (``id = "<enterprise_id>:<user_id>"``, plus a unique compound index). It holds
  the user's Enterprise role (if any) and their Organisation/Bot grants. A user
  belongs to an Enterprise exactly when this document exists, so Organisation
  and Bot grants can never outlive the Enterprise membership (removing the
  document removes everything at once).
* ``membership_guards``: one small document per Enterprise: ``version``, exact
  ``owner_count``, a sequence number, the in-flight ``pending`` intent and the
  outcomes of recent intents.

Concurrency: a serialised intent protocol
-----------------------------------------
Every membership change, and every Enterprise policy change that affects
membership authority (``delegation.*``), goes through the Enterprise guard:

1. **Snapshot.** Read the guard (version *g*, no pending intent); read the
   member documents involved (actor and target) and the Enterprise's
   delegation policy; re-read the guard. If it is still *g* the snapshot is
   consistent, because these documents are only ever written through this
   protocol.
2. **Plan.** Pure permission checks against the snapshot: grant limits, no
   escalation, Enterprise prerequisite, delegation policy, and the last-Owner
   rule (the exact ``owner_count`` lives in the guard).
3. **Commit.** A compare-and-swap of the guard from *g* to *g+1* stores the
   intent (state ``prepared``) and the new ``owner_count``. This is the
   serialisation point. Two operations can never both commit against the
   same snapshot, so a revocation of the actor's authority either committed
   before (our compare-and-swap fails, we re-read and re-check) or is ordered
   after us.
4. **Verify.** Whoever resolves the intent re-checks its account-level
   preconditions *now*: the actor account is active and, if the decision relied
   on platform-superadmin authority, the actor is still a superadmin. The
   documents to change must still be at their expected versions. Then the
   intent moves to ``applying``, or is aborted (owner count restored,
   outcome recorded). A superadmin demoted before this point therefore
   cannot complete a change.
5. **Apply.** Idempotent writes, each tagged with the intent id, then the guard
   clears ``pending`` and records the outcome with a sequence number.

Any later operation, or a stale-intent sweep, rolls a crashed requester's
intent forward or aborts it. The sequence numbers give the order in which
changes took effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from core.authz import (Action, Actor, AuthzError, Membership, Role, can, check_grant, effective_role)
from core.hierarchy import (COLLECTION as HIERARCHY_COLLECTION, Chain, ConcurrentModification, DuplicateKey,
                            HierarchyError, Level, NotFound, from_doc, is_valid_id, new_id)

COLLECTION = "memberships"
GUARDS = "membership_guards"
USERS = "users"
_MAX_ATTEMPTS = 64
_RECENT = 100
STALE_INTENT_SECONDS = 5

LAST_OWNER_MESSAGE = "An Enterprise must keep at least one Owner. Add another Owner first."


def doc_id(enterprise_id: str, user_id: str) -> str:
    return f"{enterprise_id}:{user_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


Entry = Tuple[Membership, Dict[str, str]]          # membership + {granted_by, granted_at}


# ═════════════════════════════════ documents ⇄ memberships ═════════════════════════════════

def entries_from_doc(doc: Optional[dict], enterprise_id: str) -> List[Entry]:
    if not doc or doc.get("enterprise_id") != enterprise_id:
        return []                                         # never trust a foreign document
    uid = doc["user_id"]
    out: List[Entry] = []
    if doc.get("role"):
        out.append((Membership(uid, enterprise_id, Level.ENTERPRISE, enterprise_id, Role[doc["role"].upper()]),
                    {"granted_by": doc.get("granted_by", ""), "granted_at": doc.get("granted_at", "")}))
    for g in doc.get("grants") or []:
        out.append((Membership(uid, enterprise_id, Level(g["scope_level"]), g["scope_id"], Role[g["role"].upper()]),
                    {"granted_by": g.get("granted_by", ""), "granted_at": g.get("granted_at", "")}))
    return [(m.validated(), meta) for m, meta in out]


def doc_from_entries(enterprise_id: str, user_id: str, entries: Sequence[Entry]) -> dict:
    role, meta_e, grants, seen = None, {}, [], set()
    for m, meta in entries:
        m = m.validated()
        if m.user_id != user_id or m.enterprise_id != enterprise_id:
            raise AuthzError("Invalid membership reference.")
        if m.scope_id in seen:
            raise AuthzError("Duplicate membership.")
        seen.add(m.scope_id)
        if m.scope_level is Level.ENTERPRISE:
            role, meta_e = m.role.name.lower(), meta
        else:
            grants.append({"scope_level": m.scope_level.value, "scope_id": m.scope_id, "role": m.role.name.lower(),
                           "granted_by": meta.get("granted_by", ""), "granted_at": meta.get("granted_at", "")})
    grants.sort(key=lambda g: (g["scope_level"], g["scope_id"]))
    return {"id": doc_id(enterprise_id, user_id), "enterprise_id": enterprise_id, "user_id": user_id, "role": role,
            "granted_by": meta_e.get("granted_by", ""), "granted_at": meta_e.get("granted_at", ""),
            "grants": grants}


@dataclass(frozen=True)
class MemberSet:
    """Memberships of some (or all) users of one Enterprise."""
    enterprise_id: str
    entries: Tuple[Entry, ...] = ()

    @property
    def members(self) -> Tuple[Membership, ...]:
        return tuple(m for m, _ in self.entries)

    def for_user(self, user_id: str) -> List[Membership]:
        return [m for m in self.members if m.user_id == user_id]

    def entries_for(self, user_id: str) -> List[Entry]:
        return [e for e in self.entries if e[0].user_id == user_id]

    def at_scope(self, scope_id: str) -> List[Membership]:
        return [m for m in self.members if m.scope_id == scope_id]

    def owners(self) -> List[str]:
        return sorted({m.user_id for m in self.members
                       if m.scope_level is Level.ENTERPRISE and m.role is Role.OWNER})


Roster = MemberSet   # backwards-compatible name used by callers


def _set_from_docs(enterprise_id: str, docs: Iterable[Optional[dict]]) -> MemberSet:
    entries: List[Entry] = []
    for d in docs:
        entries.extend(entries_from_doc(d, enterprise_id))
    return MemberSet(enterprise_id, tuple(entries))


async def load(store, enterprise_id: str) -> MemberSet:
    """All memberships of an Enterprise (listings, previews, tests). Not used
    on the per-request authorisation path."""
    if not is_valid_id(enterprise_id):
        return MemberSet(enterprise_id)
    docs = await store.find(COLLECTION, {"enterprise_id": enterprise_id})
    return _set_from_docs(enterprise_id, docs)


async def load_for(store, enterprise_id: str, user_ids: Iterable[str]) -> MemberSet:
    """Memberships of specific users (authorisation: one indexed read per user)."""
    if not is_valid_id(enterprise_id):
        return MemberSet(enterprise_id)
    docs = [await store.get(COLLECTION, doc_id(enterprise_id, u)) for u in dict.fromkeys(user_ids) if is_valid_id(u)]
    return _set_from_docs(enterprise_id, docs)


async def at_scope(store, enterprise_id: str, level: Level, scope_id: str) -> List[Membership]:
    if level is Level.ENTERPRISE:
        docs = await store.find(COLLECTION, {"enterprise_id": enterprise_id})
        docs = [d for d in docs if d.get("role")]
    else:
        docs = await store.find(COLLECTION, {"enterprise_id": enterprise_id, "grants.scope_id": scope_id})
    return _set_from_docs(enterprise_id, docs).at_scope(scope_id)


async def enterprises_of(store, user_id: str) -> List[str]:
    docs = await store.find(COLLECTION, {"user_id": user_id})
    return sorted({d["enterprise_id"] for d in docs})


async def owner_count(store, enterprise_id: str) -> int:
    g = await store.get(GUARDS, enterprise_id)
    return int(g["owner_count"]) if g else 0


# ═════════════════════════════════ operations ═════════════════════════════════

@dataclass(frozen=True)
class Require:
    """Account precondition re-checked when the intent is verified."""
    user_id: str
    equals: Dict[str, Any]
    missing_ok: bool = False


@dataclass
class Snapshot:
    enterprise_id: str
    members: MemberSet                       # memberships of the users involved
    owner_count: int
    policy: Dict[str, Any]                   # fresh Enterprise-level effective policy (delegation)
    enterprise_version: int


@dataclass
class Plan:
    changes: Dict[str, List[Entry]] = field(default_factory=dict)    # user -> new entries ([] = no membership)
    requires: List[Require] = field(default_factory=list)
    extra: List[dict] = field(default_factory=list)                  # e.g. Enterprise policy update
    allow_no_owner: bool = False


UserIds = Union[Sequence[str], Callable[[Any, str], Awaitable[Sequence[str]]]]


@dataclass
class Op:
    user_ids: UserIds
    plan: Callable[[Snapshot], Plan]


@dataclass(frozen=True)
class Result:
    intent_id: Optional[str]
    seq: Optional[int]                        # position in the Enterprise's change order (None = no-op)


def actor_requires(actor: Actor) -> List[Require]:
    if actor.is_platform_superadmin:
        return [Require(actor.user_id, {"is_platform_superadmin": True, "status": "active"})]
    return [Require(actor.user_id, {"status": "active"}, missing_ok=True)]


def _target_requires(user_id: str) -> List[Require]:
    return [Require(user_id, {"status": "active"}, missing_ok=True)]


def op_grant(actor: Actor, chain: Chain, *, user_id: str, role: Role, now: str,
             bypass_authority: bool = False, admit: bool = False, keep_higher: bool = False,
             authority: Optional[Callable[[Snapshot], None]] = None) -> Op:
    """Add or change ``user_id``'s membership at ``chain.target``.

    ``bypass_authority`` is only for server-validated flows (initial owner);
    ``authority`` replaces the default grant check (accepted invitation: the
    inviter's authority is re-validated against the snapshot); ``admit`` lets
    an invitation admit a new member at Organisation/Bot scope; ``keep_higher``
    never lowers an existing role."""
    scope = chain.target

    def plan(s: Snapshot) -> Plan:
        members = list(s.members.members)
        existing = next((m for m in members if m.user_id == user_id and m.scope_id == scope.id), None)
        if authority is not None:
            authority(s)
        elif not bypass_authority:
            check_grant(actor, chain, members, s.policy, target_user_id=user_id, new_role=role,
                        existing_role=existing.role if existing else None)
        if scope.level is not Level.ENTERPRISE and not admit and not any(m.user_id == user_id for m in members):
            raise AuthzError("The user must already belong to this Enterprise.")
        new_role = existing.role if keep_higher and existing is not None and existing.role > role else role
        entries = [e for e in s.members.entries_for(user_id) if e[0].scope_id != scope.id]
        entries.append((Membership(user_id, chain.enterprise_id, scope.level, scope.id, new_role).validated(),
                        {"granted_by": actor.user_id, "granted_at": now}))
        requires = [] if bypass_authority else actor_requires(actor)
        return Plan(changes={user_id: entries}, requires=requires + _target_requires(user_id))
    return Op([actor.user_id, user_id], plan)


def op_revoke(actor: Actor, chain: Chain, *, user_id: str) -> Op:
    """Remove ``user_id``'s membership at ``chain.target``. Members may always
    leave themselves (except the last Owner). Removing the Enterprise
    membership removes the user's Organisation and Bot grants with it."""
    scope = chain.target

    def plan(s: Snapshot) -> Plan:
        members = list(s.members.members)
        existing = next((m for m in members if m.user_id == user_id and m.scope_id == scope.id), None)
        if existing is None:
            raise NotFound("Not found.")
        if user_id != actor.user_id:
            d = can(actor, Action.MANAGE_MEMBERS, chain, members, s.policy)
            if not d.allowed:
                raise AuthzError(d.reason)
            grant = effective_role(actor, chain, members)
            if not grant.superadmin and existing.role > grant.role:
                raise AuthzError("You cannot remove a member whose role is above your own.")
            if existing.role is Role.OWNER and not can(actor, Action.MANAGE_OWNERS, chain, members, s.policy).allowed:
                raise AuthzError("requires owner")
        if scope.level is Level.ENTERPRISE:
            entries: List[Entry] = []
        else:
            entries = [e for e in s.members.entries_for(user_id) if e[0].scope_id != scope.id]
        return Plan(changes={user_id: entries}, requires=actor_requires(actor))
    return Op([actor.user_id, user_id], plan)


def op_remove_user(user_id: str, *, enforce_owner: bool = True) -> Op:
    """User deletion: the user's membership in this Enterprise ends."""
    def plan(s: Snapshot) -> Plan:
        return Plan(changes={user_id: []}, allow_no_owner=not enforce_owner)
    return Op([user_id], plan)


def op_remove_scopes(scope_ids: Iterable[str]) -> Op:
    """Organisation/Bot deletion: drop grants attached to those scopes."""
    ids = sorted(set(scope_ids))

    async def users(store, enterprise_id: str) -> List[str]:
        if not ids:
            return []
        docs = await store.find(COLLECTION, {"enterprise_id": enterprise_id, "grants.scope_id": ids})
        return sorted(d["user_id"] for d in docs)

    def plan(s: Snapshot) -> Plan:
        changes: Dict[str, List[Entry]] = {}
        for m in s.members.members:
            if m.scope_id in ids and m.user_id not in changes:
                changes[m.user_id] = [e for e in s.members.entries_for(m.user_id) if e[0].scope_id not in ids]
        return Plan(changes=changes)
    return Op(users, plan)


def op_set_enterprise_policy(actor: Actor, chain: Chain, *, new_policy: Dict[str, Any], expect_version: int,
                             now: str) -> Op:
    """Enterprise policy writes that change delegation (membership authority)
    are serialised with membership changes through the guard."""
    if chain.target.level is not Level.ENTERPRISE:
        raise HierarchyError("Not an Enterprise.")

    def plan(s: Snapshot) -> Plan:
        d = can(actor, Action.EDIT_POLICY, chain, list(s.members.members), s.policy)
        if not d.allowed:
            raise AuthzError(d.reason)
        return Plan(requires=actor_requires(actor),
                    extra=[{"kind": "update", "collection": HIERARCHY_COLLECTION[Level.ENTERPRISE],
                            "id": chain.enterprise_id, "expect": expect_version,
                            "fields": {"policy": new_policy, "updated_at": now}}])
    return Op([actor.user_id], plan)


# ═════════════════════════════════ the intent protocol ═════════════════════════════════

async def _guard(store, enterprise_id: str) -> dict:
    g = await store.get(GUARDS, enterprise_id)
    if g is not None:
        return g
    owners = sum(1 for d in await store.find(COLLECTION, {"enterprise_id": enterprise_id}) if d.get("role") == "owner")
    try:
        await store.insert(GUARDS, {"id": enterprise_id, "enterprise_id": enterprise_id, "owner_count": owners,
                                    "seq": 0, "pending": None, "recent": [], "version": 1})
    except DuplicateKey:
        pass
    return await store.get(GUARDS, enterprise_id)


async def _enterprise_policy(store, enterprise_id: str) -> Tuple[Dict[str, Any], int]:
    from core import resolver
    from core.config_registry import PLATFORM_POLICY
    doc = await store.get(HIERARCHY_COLLECTION[Level.ENTERPRISE], enterprise_id)
    if doc is None:
        return dict(resolver.effective_policy([], PLATFORM_POLICY)[0]), 0
    node = from_doc(Level.ENTERPRISE, doc)
    return dict(resolver.effective_policy([node], PLATFORM_POLICY)[0]), node.version


async def mutate(store, enterprise_id: str, op: Op, *, clock: Callable[[], str] = _now) -> Result:
    """Run ``op`` through the intent protocol (see module docstring).
    Raises AuthzError/NotFound from the plan, AuthzError if the actor's
    authority is gone at verification, ConcurrentModification on conflicts."""
    if not is_valid_id(enterprise_id):
        raise NotFound("Not found.")
    for _ in range(_MAX_ATTEMPTS):
        guard = await _guard(store, enterprise_id)
        if guard.get("pending"):
            await _resolve(store, enterprise_id)
            continue
        uids = op.user_ids if not callable(op.user_ids) else await op.user_ids(store, enterprise_id)
        docs = {u: await store.get(COLLECTION, doc_id(enterprise_id, u)) for u in dict.fromkeys(uids)}
        policy, ent_version = await _enterprise_policy(store, enterprise_id)
        check = await store.get(GUARDS, enterprise_id)
        if check is None or check["version"] != guard["version"] or check.get("pending"):
            continue                                       # snapshot not consistent: retry
        snap = Snapshot(enterprise_id, _set_from_docs(enterprise_id, docs.values()), int(guard["owner_count"]),
                        policy, ent_version)
        plan = op.plan(snap)
        ops: List[dict] = []
        delta = 0
        for uid, entries in plan.changes.items():
            if uid not in docs:
                raise HierarchyError("Membership change outside the snapshot.")
            old = docs[uid]
            old_v = int(old["version"]) if old else 0
            was_owner = bool(old and old.get("role") == "owner")
            if entries:
                new_doc = doc_from_entries(enterprise_id, uid, entries)
                if old and all(old.get(k) == new_doc[k] for k in ("role", "grants")):
                    continue                               # no change
                ops.append({"kind": "put", "collection": COLLECTION, "id": new_doc["id"], "expect": old_v,
                            "doc": new_doc})
                is_owner = new_doc["role"] == "owner"
            else:
                if old is None:
                    continue
                ops.append({"kind": "delete", "collection": COLLECTION, "id": doc_id(enterprise_id, uid),
                            "expect": old_v})
                is_owner = False
            delta += int(is_owner) - int(was_owner)
        ops.extend(plan.extra)
        if not ops:
            return Result(None, None)
        before = int(guard["owner_count"])
        after = before + delta
        if before >= 1 and after < 1 and not plan.allow_no_owner:
            raise AuthzError(LAST_OWNER_MESSAGE)
        intent = {"id": new_id(), "state": "prepared", "created_at": clock(), "ops": ops,
                  "requires": [{"user_id": r.user_id, "equals": r.equals, "missing_ok": r.missing_ok}
                               for r in plan.requires],
                  "owner_count_before": before}
        try:
            await store.update(GUARDS, enterprise_id, {"pending": intent, "owner_count": after},
                               expect_version=guard["version"])
        except ConcurrentModification:
            continue
        return await _finish(store, enterprise_id, intent["id"])
    raise ConcurrentModification()


async def _finish(store, enterprise_id: str, intent_id: str) -> Result:
    for _ in range(_MAX_ATTEMPTS):
        g = await store.get(GUARDS, enterprise_id)
        if g and g.get("pending") and g["pending"]["id"] == intent_id:
            await _resolve(store, enterprise_id)
            continue
        for r in (g or {}).get("recent") or []:
            if r["id"] == intent_id:
                if r["outcome"] == "applied":
                    return Result(intent_id, r.get("seq"))
                if r["outcome"] == "aborted:conflict":
                    raise ConcurrentModification()
                raise AuthzError("Your permissions changed while this request was processed.")
        raise ConcurrentModification()
    raise ConcurrentModification()


async def _requirements_hold(store, requires: List[dict]) -> bool:
    for r in requires:
        doc = await store.get(USERS, r["user_id"]) if is_valid_id(r["user_id"]) else None
        if doc is None:
            if r.get("missing_ok"):
                continue
            return False
        if any(doc.get(k) != v for k, v in r["equals"].items()):
            return False
    return True


async def _op_state(store, op: dict, intent_id: str) -> str:
    """'ready' (expected version), 'applied' (already done) or 'conflict'."""
    cur = await store.get(op["collection"], op["id"])
    if op["kind"] == "delete":
        if cur is None:
            return "applied"
        return "ready" if int(cur.get("version", 0)) == op["expect"] else "conflict"
    if cur is not None and cur.get("last_intent") == intent_id:
        return "applied"
    cur_v = int(cur.get("version", 0)) if cur else 0
    return "ready" if cur_v == op["expect"] else "conflict"


async def _abort(store, enterprise_id: str, guard: dict, outcome: str) -> None:
    p = guard["pending"]
    recent = (list(guard.get("recent") or []) + [{"id": p["id"], "outcome": outcome, "seq": None}])[-_RECENT:]
    try:
        await store.update(GUARDS, enterprise_id, {"pending": None, "owner_count": p["owner_count_before"],
                                                   "recent": recent}, expect_version=guard["version"])
    except ConcurrentModification:
        pass


async def _resolve(store, enterprise_id: str) -> None:
    """Verify, apply and clear the pending intent (idempotent; safe to run from
    any process, any number of times)."""
    guard = await store.get(GUARDS, enterprise_id)
    if not guard or not guard.get("pending"):
        return
    p = guard["pending"]
    if p["state"] == "prepared":
        if not await _requirements_hold(store, p["requires"]):
            await _abort(store, enterprise_id, guard, "aborted:authority")
            return
        states = [await _op_state(store, op, p["id"]) for op in p["ops"]]
        if "conflict" in states:
            await _abort(store, enterprise_id, guard, "aborted:conflict")
            return
        try:
            await store.update(GUARDS, enterprise_id, {"pending": {**p, "state": "applying"}},
                               expect_version=guard["version"])
        except ConcurrentModification:
            return                                          # someone else progressed it
        guard = await store.get(GUARDS, enterprise_id)
        p = guard["pending"]
        if not p:
            return
    applied_any = False
    for op in p["ops"]:
        state = await _op_state(store, op, p["id"])
        if state == "applied":
            applied_any = True
            continue
        if state == "conflict":
            if not applied_any:                             # nothing applied yet: abort cleanly
                await _abort(store, enterprise_id, guard, "aborted:conflict")
                return
            raise ConcurrentModification()                  # cannot happen for guard-exclusive documents
        await _apply(store, op, p["id"])
        applied_any = True
    seq = int(guard.get("seq", 0)) + 1
    recent = (list(guard.get("recent") or []) + [{"id": p["id"], "outcome": "applied", "seq": seq}])[-_RECENT:]
    try:
        await store.update(GUARDS, enterprise_id, {"pending": None, "seq": seq, "recent": recent},
                           expect_version=guard["version"])
    except ConcurrentModification:
        pass


async def _apply(store, op: dict, intent_id: str) -> None:
    try:
        if op["kind"] == "delete":
            await store.delete(op["collection"], op["id"], expect_version=op["expect"])
        elif op["kind"] == "put":
            fields = {k: v for k, v in op["doc"].items() if k != "id"}
            fields["last_intent"] = intent_id
            if op["expect"] == 0:
                await store.insert(op["collection"], {"id": op["id"], **fields, "version": 1})
            else:
                await store.update(op["collection"], op["id"], fields, expect_version=op["expect"])
        elif op["kind"] == "update":
            await store.update(op["collection"], op["id"], {**op["fields"], "last_intent": intent_id},
                               expect_version=op["expect"])
    except (DuplicateKey, ConcurrentModification, NotFound):
        if await _op_state(store, op, intent_id) != "applied":   # another resolver applied it first
            raise ConcurrentModification()


async def resolve_stale(store, enterprise_id: str, *, older_than_seconds: int = STALE_INTENT_SECONDS,
                        now: Optional[datetime] = None) -> bool:
    """Roll forward (or abort) an intent left behind by a crashed requester.
    Cheap when nothing is pending (one read)."""
    g = await store.get(GUARDS, enterprise_id) if is_valid_id(enterprise_id) else None
    p = (g or {}).get("pending")
    if not p:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(p["created_at"])
    except (ValueError, KeyError):
        created = now - timedelta(seconds=older_than_seconds + 1)
    if now - created < timedelta(seconds=older_than_seconds):
        return False
    await _resolve(store, enterprise_id)
    return True


async def delete_enterprise_memberships(store, enterprise_id: str) -> int:
    """Enterprise deletion: the Enterprise is already DELETED (no access), so
    its documents are removed directly."""
    n = await store.delete_many(COLLECTION, {"enterprise_id": enterprise_id})
    await store.delete(GUARDS, enterprise_id)
    return n


__all__ = ["COLLECTION", "GUARDS", "doc_id", "MemberSet", "Roster", "load", "load_for", "at_scope",
           "enterprises_of", "owner_count", "Require", "Snapshot", "Plan", "Op", "Result", "actor_requires",
           "op_grant", "op_revoke", "op_remove_user", "op_remove_scopes", "op_set_enterprise_policy", "mutate",
           "resolve_stale", "delete_enterprise_memberships", "LAST_OWNER_MESSAGE"]
