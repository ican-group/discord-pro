"""Users, sessions and platform superadmins (spec 012 §9).

* ``users.is_platform_superadmin`` is the only superadmin field.
* Sessions are JWTs ``{sub, tv, iat, exp, typ}`` (signed by the API layer). A
  session is valid only while the user exists, is active and ``tv`` equals the
  user's current ``token_version`` (bumped on password change/reset, role
  changes and deletion).
* **Last superadmin safeguard without transactions.** The ``platform_state``
  document ``superadmins`` holds ``count``, a *lower bound* of the number of
  superadmins: demotion first decrements it with a compare-and-swap that
  requires ``count >= 2`` and only then clears the user's flag; promotion sets
  the flag first and then increments. Every interleaving (and every crash)
  therefore keeps ``count <= actual``, and a demotion can only proceed while at
  least one other superadmin is guaranteed to remain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from core.authz import Actor, AuthzError
from core.hierarchy import ConcurrentModification, DuplicateKey, HierarchyError, NotFound, is_valid_id, new_id

USERS = "users"
PLATFORM_STATE = "platform_state"
SUPERADMIN_GUARD_ID = "superadmins"
SESSION_TYPE = "s012"              # legacy (pre-012) tokens lack this and are rejected
_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}")
_MAX_ATTEMPTS = 64


class IdentityError(HierarchyError):
    pass


def normalise_email(value: Any) -> str:
    if not isinstance(value, str) or not _EMAIL_RE.fullmatch(value.strip()):
        raise IdentityError("A valid email address is required.")
    return value.strip().lower()


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str
    is_platform_superadmin: bool
    token_version: int
    status: str                      # "active" | "disabled"
    email_verified: bool
    version: int

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "User":
        return cls(id=doc["id"], email=doc.get("email", ""), name=doc.get("name", ""),
                   is_platform_superadmin=doc.get("is_platform_superadmin") is True,
                   token_version=int(doc.get("token_version", 0)), status=doc.get("status", "active"),
                   email_verified=doc.get("email_verified") is True, version=int(doc.get("version", 0)))

    def public_view(self) -> Dict[str, Any]:
        return {"id": self.id, "email": self.email, "name": self.name,
                "is_platform_superadmin": self.is_platform_superadmin}

    def actor(self) -> Actor:
        return Actor(user_id=self.id, is_platform_superadmin=self.is_platform_superadmin)


async def get_user(store, user_id: Any) -> Optional[User]:
    if not is_valid_id(user_id):
        return None
    doc = await store.get(USERS, user_id)
    return User.from_doc(doc) if doc else None


async def create_user(store, *, email: str, name: str, now: str, password_hash: str = "",
                      email_verified: bool = False, user_id: Optional[str] = None) -> User:
    """Create a (non-superadmin) user. Superadmin status is granted only
    through :func:`grant_superadmin` or the bootstrap command."""
    doc = {"id": user_id or new_id(), "email": normalise_email(email), "name": (name or "").strip()[:120],
           "password_hash": password_hash, "is_platform_superadmin": False, "token_version": 0,
           "status": "active", "email_verified": bool(email_verified), "created_at": now, "version": 1}
    try:
        await store.insert(USERS, doc)
    except DuplicateKey:
        raise IdentityError("An account with this email address already exists.")
    return User.from_doc(doc)


def session_claims_valid(payload: Mapping[str, Any], user: Optional[User]) -> bool:
    """Pure check of decoded JWT claims against the stored user."""
    if user is None or user.status != "active":
        return False
    if payload.get("typ") != SESSION_TYPE or payload.get("sub") != user.id:
        return False
    tv = payload.get("tv")
    return isinstance(tv, int) and not isinstance(tv, bool) and tv == user.token_version


async def bump_token_version(store, user_id: str) -> None:
    """End every session of the user (compare-and-swap loop)."""
    for _ in range(_MAX_ATTEMPTS):
        user = await get_user(store, user_id)
        if user is None:
            return
        try:
            await store.update(USERS, user_id, {"token_version": user.token_version + 1},
                               expect_version=user.version)
            return
        except ConcurrentModification:
            continue
    raise ConcurrentModification()


# ═════════════════════════════════ superadmin guard ═════════════════════════════════

async def ensure_superadmin_guard(store) -> int:
    """Create the guard from the current user documents. Call once at startup /
    bootstrap, before any promotion or demotion (both refuse without it)."""
    doc = await store.get(PLATFORM_STATE, SUPERADMIN_GUARD_ID)
    if doc is not None:
        return int(doc["count"])
    actual = await store.count(USERS, {"is_platform_superadmin": True, "status": "active"})
    try:
        await store.insert(PLATFORM_STATE, {"id": SUPERADMIN_GUARD_ID, "count": actual, "version": 1})
    except DuplicateKey:
        pass
    return int((await store.get(PLATFORM_STATE, SUPERADMIN_GUARD_ID))["count"])


async def _guard_add(store, delta: int, *, require_min: int = 0) -> None:
    for _ in range(_MAX_ATTEMPTS):
        doc = await store.get(PLATFORM_STATE, SUPERADMIN_GUARD_ID)
        if doc is None:
            raise AuthzError("Platform superadmin state is not initialised.")
        if doc["count"] < require_min:
            raise AuthzError("The last platform superadmin cannot be removed or demoted.")
        try:
            await store.update(PLATFORM_STATE, SUPERADMIN_GUARD_ID, {"count": doc["count"] + delta},
                               expect_version=doc["version"])
            return
        except ConcurrentModification:
            continue
    raise ConcurrentModification()


async def _set_flag(store, user_id: str, value: bool) -> bool:
    """Set the flag (and end sessions) if it differs. Returns True if changed."""
    for _ in range(_MAX_ATTEMPTS):
        user = await get_user(store, user_id)
        if user is None:
            raise NotFound("Not found.")
        if user.is_platform_superadmin is value:
            return False
        try:
            await store.update(USERS, user_id, {"is_platform_superadmin": value,
                                                "token_version": user.token_version + 1},
                               expect_version=user.version)
            return True
        except ConcurrentModification:
            continue
    raise ConcurrentModification()


async def grant_superadmin(store, actor: Actor, user_id: str) -> bool:
    if not actor.is_platform_superadmin:
        raise AuthzError("Only platform superadmins can do this.")
    user = await get_user(store, user_id)
    if user is None or user.status != "active":
        raise NotFound("Not found.")
    if await store.get(PLATFORM_STATE, SUPERADMIN_GUARD_ID) is None:
        raise AuthzError("Platform superadmin state is not initialised.")
    changed = await _set_flag(store, user_id, True)     # flag first ...
    if changed:
        await _guard_add(store, +1)                     # ... then the lower bound
    return changed


async def revoke_superadmin(store, actor: Actor, user_id: str) -> bool:
    """Demote (also used by user deletion). Refuses for the last superadmin."""
    if not actor.is_platform_superadmin:
        raise AuthzError("Only platform superadmins can do this.")
    user = await get_user(store, user_id)
    if user is None:
        raise NotFound("Not found.")
    if not user.is_platform_superadmin:
        return False
    await _guard_add(store, -1, require_min=2)          # lower bound first ...
    try:
        changed = await _set_flag(store, user_id, False)  # ... then the flag
    except BaseException:
        await _guard_add(store, +1)
        raise
    if not changed:                                     # someone else demoted them meanwhile
        await _guard_add(store, +1)
    return changed


# ═════════════════════════════════ sessions (Phase 4A) ═════════════════════════════════

SESSIONS = "sessions"
PASSWORD_RESETS = "password_resets"
BOOTSTRAP_MARKER_ID = "superadmin_bootstrap"
RESET_TTL_SECONDS = 30 * 60


def _hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def _plus(now: str, seconds: int):
    from datetime import datetime, timedelta
    return datetime.fromisoformat(now) + timedelta(seconds=seconds)


async def find_user_by_email(store, email: Any) -> Optional[User]:
    try:
        norm = normalise_email(email)
    except IdentityError:
        return None
    docs = await store.find(USERS, {"email": norm}, limit=1)
    return User.from_doc(docs[0]) if docs else None


async def password_hash_of(store, user_id: str) -> str:
    doc = await store.get(USERS, user_id) if is_valid_id(user_id) else None
    return (doc or {}).get("password_hash", "") or ""


async def create_session(store, user: User, *, now: str, ttl_seconds: int) -> dict:
    """A server-side session record: the JWT carries only its id (``sid``), so a
    single session can be revoked (logout) without touching the others."""
    expires = _plus(now, ttl_seconds)
    doc = {"id": new_id(), "user_id": user.id, "created_at": now, "expires_at": expires.isoformat(),
           "expires_at_dt": expires, "revoked": False, "version": 1}
    await store.insert(SESSIONS, doc)
    return doc


async def session_valid(store, sid: Any, user_id: str, *, now: str) -> bool:
    from datetime import datetime
    doc = await store.get(SESSIONS, sid) if is_valid_id(sid) else None
    if doc is None or doc.get("revoked") is not False or doc.get("user_id") != user_id:
        return False
    return datetime.fromisoformat(now) < datetime.fromisoformat(doc["expires_at"])


async def revoke_session(store, sid: str) -> None:
    await store.delete(SESSIONS, sid)


async def revoke_all_sessions(store, user_id: str) -> int:
    """End every session of the user: session records go, and the token version
    is bumped so any token minted from an old record is rejected too."""
    n = await store.delete_many(SESSIONS, {"user_id": user_id})
    await bump_token_version(store, user_id)
    return n


async def set_password_hash(store, user_id: str, password_hash: str) -> None:
    """Change the password and end every session (spec §9)."""
    for _ in range(_MAX_ATTEMPTS):
        user = await get_user(store, user_id)
        if user is None:
            raise NotFound("Not found.")
        try:
            await store.update(USERS, user_id, {"password_hash": password_hash}, expect_version=user.version)
            break
        except ConcurrentModification:
            continue
    await revoke_all_sessions(store, user_id)


async def issue_password_reset(store, user: User, *, now: str) -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    expires = _plus(now, RESET_TTL_SECONDS)
    await store.delete_many(PASSWORD_RESETS, {"user_id": user.id})      # one live reset per user
    await store.insert(PASSWORD_RESETS, {"id": _hash_token(token), "user_id": user.id, "created_at": now,
                                         "expires_at": expires.isoformat(), "expires_at_dt": expires, "version": 1})
    return token


async def consume_password_reset(store, token: Any, *, now: str) -> Optional[str]:
    """Single use (atomic take). Returns the user id, or None if invalid/expired."""
    from datetime import datetime
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        return None
    doc = await store.take(PASSWORD_RESETS, _hash_token(token))
    if doc is None or datetime.fromisoformat(now) >= datetime.fromisoformat(doc["expires_at"]):
        return None
    return doc["user_id"]


async def peek_password_reset(store, token: Any, *, now: str) -> Optional[str]:
    """Read-only check (used to validate the new password before the token is spent)."""
    from datetime import datetime
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        return None
    doc = await store.get(PASSWORD_RESETS, _hash_token(token))
    if doc is None or datetime.fromisoformat(now) >= datetime.fromisoformat(doc["expires_at"]):
        return None
    return doc["user_id"]


async def bootstrap_superadmin(store, *, email: str, name: str, password_hash: str, now: str) -> User:
    """Create the first platform superadmin. Refuses if any superadmin exists or a
    bootstrap already happened (one-time marker with a unique id, so two
    concurrent bootstraps cannot both succeed)."""
    if not password_hash:
        raise IdentityError("A password is required.")
    if await store.count(USERS, {"is_platform_superadmin": True}) > 0:
        raise IdentityError("A platform superadmin already exists; bootstrap is not available.")
    try:
        await store.insert(PLATFORM_STATE, {"id": BOOTSTRAP_MARKER_ID, "at": now, "version": 1})
    except DuplicateKey:
        raise IdentityError("Bootstrap has already been performed.")
    await ensure_superadmin_guard(store)                 # count is 0 here: no superadmin exists
    user = await create_user(store, email=email, name=name, now=now, password_hash=password_hash,
                             email_verified=True)
    await store.update(USERS, user.id, {"is_platform_superadmin": True}, expect_version=user.version)
    await _guard_add(store, +1)                          # flag first, then the lower bound
    return await get_user(store, user.id)


__all__ = ["USERS", "PLATFORM_STATE", "SUPERADMIN_GUARD_ID", "SESSION_TYPE", "IdentityError", "User",
           "normalise_email", "get_user", "create_user", "session_claims_valid", "bump_token_version",
           "ensure_superadmin_guard", "grant_superadmin", "revoke_superadmin", "SESSIONS", "PASSWORD_RESETS",
           "find_user_by_email", "password_hash_of", "create_session", "session_valid", "revoke_session",
           "revoke_all_sessions", "set_password_hash", "issue_password_reset", "consume_password_reset", "peek_password_reset",
           "bootstrap_superadmin"]
