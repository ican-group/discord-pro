"""Memberships and invitations (spec §9, §11).

Every membership change runs through the serialised intent protocol of
``core.memberships``: grant limits, the Enterprise-membership prerequisite,
delegation policy and the last-Owner safeguard are checked against one
consistent snapshot, and the change cannot take effect on authority that was
revoked concurrently. Invitation tokens are stored only as SHA-256 hashes
and travel in request bodies, never in URLs.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from api.common import TYPE_LEVEL, Services, load_access, translate
from core import audit, throttle
from core import memberships as ms
from core.authz import Action, AuthzError, Role, can, effective_role
from core.hierarchy import ConcurrentModification, Level, LifecycleState, NotFound, is_valid_id, new_id
from core.identity import (USERS, IdentityError, User, bump_token_version, create_user, find_user_by_email, get_user,
                           normalise_email)

MEMBER_SCOPES = {"enterprises": Level.ENTERPRISE, "organisations": Level.ORGANISATION, "bots": Level.BOT}
INVITATIONS = "invitations"
INVITATION_TTL_DAYS = 7
INVITE_MAIL_USER = (30, 3600)            # F3: invitation e-mails per inviter per hour
INVITE_MAIL_TO = (5, 86400)              # F3: invitation e-mails per recipient address per day
REGISTER_IP = (20, 3600)                 # 6C B-ACCT: account creation attempts per client IP per hour
REGISTER_TOKEN = (5, 3600)               # 6C B-ACCT: attempts per invitation token per hour


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberAdd(_Body):
    user_id: str
    role: str


class MemberPatch(_Body):
    role: str


class InvitationCreate(_Body):
    email: str
    role: str
    scope_type: str = "enterprises"
    scope_id: Optional[str] = None
    delivery: str = "email"          # "email" (F3) | "link": the token is returned once to the creator instead


class RegisterBody(_Body):
    token: str
    name: str
    password: str


class TokenBody(_Body):
    token: str


def _role(value: str) -> Role:
    try:
        return Role[str(value).upper()]
    except KeyError:
        raise HTTPException(422, "Unknown role.")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _invitation_view(doc: Dict[str, Any], now: str) -> Dict[str, Any]:
    """Public view: never the token or its hash. A pending invitation past its
    expiry is reported as ``expired`` (X14)."""
    view = {k: doc.get(k) for k in ("id", "email", "enterprise_id", "scope_level", "scope_id", "role", "status",
                                    "invited_by", "created_at", "expires_at")}
    if view["status"] == "pending" and _expired(doc, now):
        view["status"] = "expired"
    return view


def _expired(doc: Dict[str, Any], now: str) -> bool:
    return datetime.fromisoformat(now) >= datetime.fromisoformat(doc["expires_at"])


def check_invite_authority(actor, chain, roster: ms.Roster, policy, role: Role) -> None:
    d = can(actor, Action.MANAGE_MEMBERS, chain, roster.members, policy)
    if not d.allowed:
        raise AuthzError(d.reason)
    if role is Role.OWNER:
        if chain.target.level is not Level.ENTERPRISE:
            raise AuthzError("Owner is an Enterprise-level role.")
        if not can(actor, Action.MANAGE_OWNERS, chain, roster.members, policy).allowed:
            raise AuthzError("requires owner")
    grant = effective_role(actor, chain, roster.members)
    if not grant.superadmin and role > grant.role:
        raise AuthzError("You cannot grant a role above your own.")


def register(router: APIRouter, S: Services, principal) -> None:
    def scope_level(scope_type: str) -> Level:
        if scope_type not in MEMBER_SCOPES:
            raise HTTPException(404, "Not found.")
        return MEMBER_SCOPES[scope_type]

    async def member_views(a, scope_id: str):
        out = []
        for m in await ms.at_scope(S.store, a.chain.enterprise_id, a.target.level, scope_id):
            u = await get_user(S.store, m.user_id)
            out.append({"user_id": m.user_id, "email": u.email if u else None, "name": u.name if u else None,
                        "role": m.role.name.lower(), "scope_type": m.scope_level.value, "scope_id": m.scope_id})
        return out

    async def record(a, action: str, details: Dict[str, Any]):
        await audit.record(S.store, actor=a.actor, action=action, enterprise_id=a.chain.enterprise_id,
                           target_level=a.target.level.value, target_id=a.target.id, now=S.clock(), details=details)

    @router.get("/{scope_type:scopetype}/{scope_id}/members")
    @translate
    async def list_members(scope_type: str, scope_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, scope_level(scope_type), scope_id)
        return {"members": await member_views(a, scope_id)}

    @router.post("/{scope_type:scopetype}/{scope_id}/members", status_code=201)
    @translate
    async def add_member(scope_type: str, scope_id: str, body: MemberAdd, user: User = Depends(principal)):
        level = scope_level(scope_type)
        a = await load_access(S, user, level, scope_id, Action.MANAGE_MEMBERS)
        if level is Level.ENTERPRISE and not user.is_platform_superadmin:
            raise AuthzError("Use an invitation to add people to an Enterprise.")
        target = await get_user(S.store, body.user_id)
        if target is None or target.status != "active":
            raise NotFound("Not found.")
        role = _role(body.role)
        base = ms.op_grant(a.actor, a.chain, user_id=target.id, role=role, now=S.clock())

        def only_new(snap: ms.Snapshot):
            if any(m.user_id == target.id and m.scope_id == scope_id for m in snap.members.members):
                raise HTTPException(409, "This person is already a member here.")
            return base.plan(snap)
        await ms.mutate(S.store, a.chain.enterprise_id, ms.Op(base.user_ids, only_new), clock=S.clock)
        await record(a, "member.add", {"user_id": target.id, "role": role.name.lower()})
        return {"members": await member_views(a, scope_id)}

    @router.patch("/{scope_type:scopetype}/{scope_id}/members/{member_id}")
    @translate
    async def change_member(scope_type: str, scope_id: str, member_id: str, body: MemberPatch,
                            user: User = Depends(principal)):
        a = await load_access(S, user, scope_level(scope_type), scope_id, Action.MANAGE_MEMBERS)
        role = _role(body.role)
        base = ms.op_grant(a.actor, a.chain, user_id=member_id, role=role, now=S.clock())

        def only_existing(snap: ms.Snapshot):
            if not any(m.user_id == member_id and m.scope_id == scope_id for m in snap.members.members):
                raise NotFound("Not found.")
            return base.plan(snap)
        await ms.mutate(S.store, a.chain.enterprise_id, ms.Op(base.user_ids, only_existing), clock=S.clock)
        await bump_token_version(S.store, member_id)              # role changes end sessions (spec §9)
        await record(a, "member.role", {"user_id": member_id, "role": role.name.lower()})
        return {"members": await member_views(a, scope_id)}

    @router.delete("/{scope_type:scopetype}/{scope_id}/members/{member_id}")
    @translate
    async def remove_member(scope_type: str, scope_id: str, member_id: str, user: User = Depends(principal)):
        a = await load_access(S, user, scope_level(scope_type), scope_id, Action.READ)
        await ms.mutate(S.store, a.chain.enterprise_id, ms.op_revoke(a.actor, a.chain, user_id=member_id),
                        clock=S.clock)
        await bump_token_version(S.store, member_id)              # access changed: end sessions
        await record(a, "member.remove", {"user_id": member_id})
        return {"members": await member_views(a, scope_id)}

    # ── invitations ──
    @router.post("/enterprises/{enterprise_id}/invitations", status_code=201)
    @translate
    async def invite(enterprise_id: str, body: InvitationCreate, user: User = Depends(principal)):
        level = scope_level(body.scope_type)
        scope_id = body.scope_id or enterprise_id
        if level is Level.ENTERPRISE and scope_id != enterprise_id:
            raise HTTPException(422, "Invalid scope.")
        a = await load_access(S, user, level, scope_id, Action.MANAGE_MEMBERS)
        if a.chain.enterprise_id != enterprise_id:
            raise NotFound("Not found.")                     # scope must belong to the Enterprise in the URL
        role = _role(body.role)
        check_invite_authority(a.actor, a.chain, a.roster, a.policy, role)
        if body.delivery not in ("email", "link"):
            raise HTTPException(422, "delivery must be email or link.")
        email = normalise_email(body.email)
        if body.delivery == "email":
            # F3 rate limits: per inviter and per recipient address (counted before anything is sent)
            for key, lim in ((throttle.throttle_key("invite-mail-user", user.id), INVITE_MAIL_USER),
                             (throttle.throttle_key("invite-mail-to", email), INVITE_MAIL_TO)):
                d = await throttle.hit(S.store, key, limit=lim[0], window_seconds=lim[1], now=S.clock())
                if not d.allowed:
                    raise HTTPException(429, "Too many invitations. Try again later.",
                                        headers={"Retry-After": str(d.retry_after)})
        token = secrets.token_urlsafe(32)
        now = S.clock()
        doc = {"id": new_id(), "token_hash": _hash(token), "email": normalise_email(body.email),
               "enterprise_id": enterprise_id, "scope_level": level.value, "scope_id": scope_id,
               "role": role.name.lower(), "status": "pending", "invited_by": user.id, "created_at": now,
               "delivery": body.delivery,           # 6C: only e-mailed invitations may create an account
               "expires_at": (datetime.fromisoformat(now) + timedelta(days=INVITATION_TTL_DAYS)).isoformat(),
               "version": 1}
        await S.store.insert(INVITATIONS, doc)
        await record(a, "invitation.create", {"invitation_id": doc["id"], "role": doc["role"], "delivery": body.delivery})
        if body.delivery == "link":
            # The raw token is returned exactly once, here, to the authorised creator.
            return {"invitation": _invitation_view(doc, now), "token": token, "email_sent": False}
        from api.mail_templates import invitation_email
        ent = await S.store.get("enterprises", enterprise_id)
        msg = invitation_email(enterprise_name=(ent or {}).get("name", ""), role=doc["role"],
                               inviter_name=user.name or user.email, accept_url=S.settings.invitation_url_base + token,
                               expires_at=doc["expires_at"])
        try:
            await S.mailer.send_invitation(doc["email"], **msg)
            sent = True
        except Exception:                      # never log the message (it carries the link)
            sent = False
        return {"invitation": _invitation_view(doc, now), "email_sent": sent}

    @router.get("/enterprises/{enterprise_id}/invitations")
    @translate
    async def list_invitations(enterprise_id: str, include_expired: bool = False, user: User = Depends(principal)):
        a = await load_access(S, user, Level.ENTERPRISE, enterprise_id, None)
        out = []
        for doc in await S.store.find(INVITATIONS, {"enterprise_id": enterprise_id, "status": "pending"}):
            try:
                await load_access(S, user, Level(doc["scope_level"]), doc["scope_id"], Action.MANAGE_MEMBERS)
            except (NotFound, AuthzError):
                continue
            view = _invitation_view(doc, S.clock())
            if view["status"] == "expired" and not include_expired:
                continue
            out.append(view)
        return {"invitations": out}

    @router.delete("/invitations/{invitation_id}")
    @translate
    async def revoke_invitation(invitation_id: str, user: User = Depends(principal)):
        doc = await S.store.get(INVITATIONS, invitation_id) if is_valid_id(invitation_id) else None
        if doc is None:
            raise NotFound("Not found.")
        a = await load_access(S, user, Level(doc["scope_level"]), doc["scope_id"], Action.MANAGE_MEMBERS)
        if a.chain.enterprise_id != doc["enterprise_id"] or doc["status"] != "pending":
            raise NotFound("Not found.")
        await S.store.update(INVITATIONS, invitation_id, {"status": "revoked"}, expect_version=doc["version"])
        await record(a, "invitation.revoke", {"invitation_id": invitation_id})
        return {"status": "revoked"}

    async def _pending(token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise NotFound("Not found.")
        docs = await S.store.find(INVITATIONS, {"token_hash": _hash(token)})
        doc = docs[0] if docs else None
        if doc is None or doc["status"] != "pending" or _expired(doc, S.clock()):
            raise NotFound("Not found.")
        return doc

    @router.post("/invitations/register", status_code=201)
    @translate
    async def register_by_invitation(body: RegisterBody, request: Request, response: Response):
        """6C B-ACCT: a person invited by e-mail who has no account creates one and joins.

        Public (the invitation token is the credential), CSRF header required, throttled per IP
        and per token. Only e-mail-delivered invitations qualify: the token reached the invited
        address, which proves ownership, so the account is created e-mail-verified. The claim
        is single use and atomic; the role is granted with the inviter's re-validated authority
        (no escalation, no cross-tenant grant); any failure removes the new account again."""
        from api import passwords
        from api.common import issue_session, require_csrf_header, set_session_cookie
        from client_ip import client_ip, rate_limit_key
        require_csrf_header(request)
        now = S.clock()
        for key, lim in ((throttle.throttle_key("invite-register-ip", rate_limit_key(client_ip(request))), REGISTER_IP),
                         (throttle.throttle_key("invite-register-token", _hash(body.token or "")), REGISTER_TOKEN)):
            d = await throttle.hit(S.store, key, limit=lim[0], window_seconds=lim[1], now=now)
            if not d.allowed:
                raise HTTPException(429, "Too many attempts. Try again later.",
                                    headers={"Retry-After": str(d.retry_after)})
        doc = await _pending(body.token)                  # unknown, used, revoked or expired: uniform 404
        if doc.get("delivery") != "email":
            raise HTTPException(409, "This invitation can only be accepted by signing in to an existing account. "
                                     "Ask the sender for an invitation by e-mail to create a new account.")
        name = " ".join("".join(ch for ch in (body.name or "") if ch.isprintable()).split())[:120]
        if not name:
            raise HTTPException(422, "Enter your name.")
        problem = passwords.user_password_error(body.password or "", doc["email"])
        if problem:
            raise HTTPException(422, problem)
        if await find_user_by_email(S.store, doc["email"]) is not None:
            raise HTTPException(409, "An account already exists for this e-mail address. "
                                     "Sign in, then open the invitation link again.")
        user_id = new_id()
        await _claim_and_grant_new(doc, user_id, name=name, password_hash=passwords.hash_password(body.password))
        user = await get_user(S.store, user_id)
        token = await issue_session(S, user)
        set_session_cookie(response, S, token)
        await audit.record(S.store, actor=user.actor(), action="user.register_by_invitation",
                           enterprise_id=doc["enterprise_id"], target_level="user", target_id=user_id, now=S.clock(),
                           details={"invitation_id": doc["id"]})
        await audit.record(S.store, actor=user.actor(), action="invitation.accept", enterprise_id=doc["enterprise_id"],
                           target_level=doc["scope_level"], target_id=doc["scope_id"], now=S.clock(),
                           details={"invitation_id": doc["id"], "role": doc["role"], "new_account": True})
        return {"user": user.public_view(), "enterprise_id": doc["enterprise_id"], "scope_type": doc["scope_level"],
                "scope_id": doc["scope_id"]}

    async def _claim_and_grant_new(doc: Dict[str, Any], user_id: str, *, name: str, password_hash: str) -> None:
        """Create the verified account under the claimed invitation; undo both on any failure."""
        created = False
        try:
            try:
                await create_user(S.store, email=doc["email"], name=name, now=S.clock(), password_hash=password_hash,
                                  email_verified=True, user_id=user_id)
                created = True
            except IdentityError:                        # a concurrent registration created this address
                raise HTTPException(409, "An account already exists for this e-mail address. "
                                         "Sign in, then open the invitation link again.")
            await _claim_and_grant(doc, user_id)
        except BaseException:
            if created:
                await S.store.delete(USERS, user_id)     # never leave an account without its membership
            raise

    @router.post("/invitations/lookup")
    @translate
    async def lookup(body: TokenBody, user: User = Depends(principal)):
        doc = await _pending(body.token)
        if doc["email"] != user.email or not user.email_verified:
            raise NotFound("Not found.")                     # X14: only the verified addressee may look it up
        ent = await S.store.get("enterprises", doc["enterprise_id"])
        return {"enterprise_name": (ent or {}).get("name"), "role": doc["role"], "scope_type": doc["scope_level"],
                "expires_at": doc["expires_at"]}

    async def _claim_and_grant(doc: Dict[str, Any], user_id: str) -> None:
        """Claim the invitation atomically (single use, even under concurrency), then grant the
        role with the inviter's authority re-validated on the same serialised snapshot. On any
        failure the claim is released. Shared by accept (existing account) and register (6C)."""
        try:
            await S.store.update(INVITATIONS, doc["id"], {"status": "accepted", "accepted_by": user_id,
                                                          "accepted_at": S.clock()}, expect_version=doc["version"])
        except ConcurrentModification:
            raise NotFound("Not found.")
        try:
            chain = await S.hierarchy.load_chain(Level(doc["scope_level"]), doc["scope_id"])
            if chain.enterprise_id != doc["enterprise_id"] or any(
                    n.lifecycle.state is LifecycleState.DELETED for n in chain.nodes()):
                raise NotFound("Not found.")
            inviter = await get_user(S.store, doc["invited_by"])
            if inviter is None or inviter.status != "active":
                raise AuthzError("inviter no longer active")
            role = Role[doc["role"].upper()]

            def inviter_still_authorised(snap: ms.Snapshot) -> None:
                # re-validated against the same serialised snapshot the grant is written from
                check_invite_authority(inviter.actor(), chain, snap.members, snap.policy, role)
            await ms.mutate(S.store, doc["enterprise_id"],
                            ms.op_grant(inviter.actor(), chain, user_id=user_id, role=role, now=S.clock(),
                                        admit=True, keep_higher=True, authority=inviter_still_authorised),
                            clock=S.clock)
        except BaseException:
            cur = await S.store.get(INVITATIONS, doc["id"])
            if cur and cur["status"] == "accepted" and cur.get("accepted_by") == user_id:
                await S.store.update(INVITATIONS, doc["id"], {"status": "pending", "accepted_by": None,
                                                              "accepted_at": None}, expect_version=cur["version"])
            raise

    @router.post("/invitations/accept")
    @translate
    async def accept(body: TokenBody, user: User = Depends(principal)):
        doc = await _pending(body.token)
        if doc["email"] != user.email or not user.email_verified:
            raise NotFound("Not found.")                     # only the verified addressee can accept
        await _claim_and_grant(doc, user.id)
        await audit.record(S.store, actor=user.actor(), action="invitation.accept", enterprise_id=doc["enterprise_id"],
                           target_level=doc["scope_level"], target_id=doc["scope_id"], now=S.clock(),
                           details={"invitation_id": doc["id"], "role": doc["role"]})
        return {"status": "accepted", "enterprise_id": doc["enterprise_id"], "scope_type": doc["scope_level"],
                "scope_id": doc["scope_id"]}
