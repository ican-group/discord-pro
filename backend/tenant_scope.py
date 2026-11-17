"""Which bots a user may see (spec 008 Stage 2, R-N2.1). Standard library only.

Mirrors the V2 rules of ``server._verify_instance_access``: a user may access a
bot if they are a member of its organisation, or an ``owner``/``admin`` of the
organisation's workspace. Superadmins (``role`` or ``platform_role``) may
access every bot. The result is never broader than what the access check would
allow, so the login and ``/auth/me`` responses cannot disclose other tenants'
bots.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

WORKSPACE_ADMIN_ROLES = ("owner", "admin")
DEFAULT_LIMIT = 100


def is_superadmin(user: Dict[str, Any]) -> bool:
    return user.get("role") == "superadmin" or user.get("platform_role") == "superadmin"


async def accessible_bot_filter(db, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """MongoDB filter for the bots ``user`` may access, or None for no bots."""
    if is_superadmin(user):
        return {}
    uid = user.get("id")
    if not uid:
        return None
    memberships = await db.workspace_members.find(
        {"user_id": uid, "role": {"$in": list(WORKSPACE_ADMIN_ROLES)}},
        {"_id": 0, "workspace_id": 1},
    ).to_list(1000)
    admin_ws = sorted({m["workspace_id"] for m in memberships if m.get("workspace_id")})
    clauses: List[Dict[str, Any]] = [{"member_ids": uid}]
    if admin_ws:
        clauses.append({"workspace_id": {"$in": admin_ws}})
    orgs = await db.organisations.find({"$or": clauses}, {"_id": 0, "id": 1}).to_list(5000)
    org_ids = sorted({o["id"] for o in orgs if o.get("id")})
    if not org_ids:
        return None
    return {"org_id": {"$in": org_ids}}


async def accessible_instances(db, user: Dict[str, Any], limit: int = DEFAULT_LIMIT) -> List[Dict[str, str]]:
    """``[{"id", "name"}]`` for the bots ``user`` may access (login and /auth/me)."""
    flt = await accessible_bot_filter(db, user)
    if flt is None:
        return []
    bots = await db.bots.find(flt, {"_id": 0, "id": 1, "name": 1}).to_list(limit)
    return [{"id": b["id"], "name": b.get("name", "")} for b in bots if b.get("id")]
