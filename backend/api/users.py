"""User discovery (Phase 6A, F2).

* ``GET /platform/users?q=&after=&limit=``: platform superadmins only (404 for
  everyone else). Bounded e-mail prefix search with cursor pagination.
* ``GET /{enterprises|organisations|bots}/{id}/member-candidates?q=``: for
  someone who may manage members at that scope. Only users who already belong
  to the same Enterprise are returned (the membership prerequisite); there is
  no global directory for tenants. Results are capped; the query must be at
  least 3 characters so the list cannot be enumerated by scrolling.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.common import TYPE_LEVEL, Services, load_access, require_superadmin, translate
from core import memberships as ms
from core.authz import Action
from core.hierarchy import Level, NotFound
from core.identity import USERS, User, get_user, normalise_email

MAX_PAGE = 50
CANDIDATE_LIMIT = 20
ENTERPRISE_SCAN_LIMIT = 5000


def _q(q: Optional[str]) -> str:
    q = (q or "").strip().lower()
    if len(q) > 254:
        raise HTTPException(422, "The search is too long.")
    return q


def _user_view(doc: dict) -> dict:
    return {"id": doc["id"], "email": doc.get("email"), "name": doc.get("name"), "status": doc.get("status", "active"),
            "is_platform_superadmin": bool(doc.get("is_platform_superadmin")), "created_at": doc.get("created_at")}


def register(router: APIRouter, S: Services, principal) -> None:
    @router.get("/platform/users")
    @translate
    async def platform_users(q: Optional[str] = Query(None), after: Optional[str] = Query(None),
                             limit: int = Query(25, ge=1, le=MAX_PAGE), user: User = Depends(principal)):
        require_superadmin(user)
        prefix = _q(q)
        cursor = _q(after) or None
        docs = await S.store.find_prefix(USERS, "email", prefix, after=cursor, limit=limit + 1)
        page = docs[:limit]
        return {"users": [_user_view(d) for d in page],
                "next": page[-1]["email"] if len(docs) > limit and page else None}

    @router.get("/{scope_type:scopetype}/{scope_id}/member-candidates")
    @translate
    async def member_candidates(scope_type: str, scope_id: str, q: Optional[str] = Query(None),
                                user: User = Depends(principal)):
        level = TYPE_LEVEL[scope_type]
        a = await load_access(S, user, level, scope_id, Action.MANAGE_MEMBERS)
        prefix = _q(q)
        if len(prefix) < 3:
            raise HTTPException(422, "Type at least 3 characters of the e-mail address.")
        docs = await S.store.find(ms.COLLECTION, {"enterprise_id": a.chain.enterprise_id}, limit=ENTERPRISE_SCAN_LIMIT)
        here = {m.user_id for m in await ms.at_scope(S.store, a.chain.enterprise_id, level, scope_id)}
        out = []
        for uid in sorted({d["user_id"] for d in docs} - here):
            u = await get_user(S.store, uid)
            if u is None or u.status != "active" or not u.email.startswith(prefix):
                continue
            out.append({"id": u.id, "email": u.email, "name": u.name})
            if len(out) >= CANDIDATE_LIMIT:
                break
        out.sort(key=lambda x: x["email"])
        return {"candidates": out, "note": "Only people who already belong to this Enterprise are listed. "
                                           "Invite anyone else by e-mail."}


__all__ = ["register", "normalise_email", "NotFound"]
