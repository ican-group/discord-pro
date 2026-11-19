"""Authentication and session routes (spec 012 §9, Phase 4A).

One identity model (``users``, ``is_platform_superadmin`` the only superadmin
field) and one session model: a server-side session record whose id travels
in a signed token. The token is delivered in an httpOnly, Secure,
SameSite=Strict cookie scoped to ``/api``; Bearer is accepted for API
clients. Every unsafe request that can carry the cookie, login included,
must send the CSRF header.

Throttling (shared store, atomic): failed logins are limited per account
(5 per 15 minutes) and per client IP (30 per 15 minutes); password-reset
requests per account (3 per hour) and per IP (10 per hour). Responses never
reveal whether an account exists.
"""
import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from api import passwords
from api.common import (Services, clear_session_cookie, issue_session, require_csrf_header, set_session_cookie,
                        translate)
from client_ip import client_ip, rate_limit_key
from core import audit, throttle
from core.identity import (User, consume_password_reset, find_user_by_email, get_user, issue_password_reset,
                           password_hash_of, peek_password_reset, revoke_all_sessions, revoke_session, set_password_hash)

LOGIN_ACCOUNT = (5, 15 * 60)
LOGIN_IP = (30, 15 * 60)
RESET_ACCOUNT = (3, 3600)
RESET_IP = (10, 3600)
INVALID_LOGIN = "Invalid email or password."


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    email: str
    password: str


class ChangePasswordBody(_Body):
    current_password: str
    new_password: str


class ForgotBody(_Body):
    email: str


class ResetBody(_Body):
    token: str
    new_password: str


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(429, "Too many attempts. Please try again later.",
                         headers={"Retry-After": str(max(1, int(retry_after)))})


_log = logging.getLogger("bitbotpro.auth")
AUTH_RESPONSE_FLOOR_MS = 250          # spec 008 response floor, ported (Phase 6B)


async def _response_floor(started: float) -> None:
    """Pad account-recovery answers to a minimum duration, whatever happened inside."""
    remaining = AUTH_RESPONSE_FLOOR_MS / 1000 - (time.monotonic() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)


def register(router: APIRouter, S: Services, principal) -> None:
    async def keys(request: Request, email: str):
        ip = rate_limit_key(client_ip(request))
        return throttle.throttle_key("login-ip", ip), throttle.throttle_key("login-acct", (email or "").strip().lower())

    async def record(user_id: Optional[str], action: str, details=None):
        from core.authz import Actor
        await audit.record(S.store, actor=Actor(user_id) if user_id else None, action=action, enterprise_id=None,
                           target_level="user", target_id=user_id, now=S.clock(), details=details or {})

    async def _send_reset(email: str, url: str) -> None:
        try:
            await S.mailer.send_password_reset(email, url)
        except Exception as exc:                    # never the link: it carries the token
            _log.warning("password reset e-mail not delivered (%s)", type(exc).__name__)

    @router.post("/auth/login")
    @translate
    async def login(body: LoginBody, request: Request, response: Response):
        require_csrf_header(request)
        ip_key, acct_key = await keys(request, body.email)
        now = S.clock()
        for key, (limit, window) in ((ip_key, LOGIN_IP), (acct_key, LOGIN_ACCOUNT)):
            d = await throttle.blocked(S.store, key, limit=limit, window_seconds=window, now=now)
            if not d.allowed:
                raise _too_many(d.retry_after)
        user = await find_user_by_email(S.store, body.email)
        stored = await password_hash_of(S.store, user.id) if user else ""
        ok = passwords.verify_password(body.password, stored)          # dummy hash when unknown
        if not ok or user is None or user.status != "active":
            await throttle.hit(S.store, ip_key, limit=LOGIN_IP[0], window_seconds=LOGIN_IP[1], now=now)
            await throttle.hit(S.store, acct_key, limit=LOGIN_ACCOUNT[0], window_seconds=LOGIN_ACCOUNT[1], now=now)
            if user is not None:
                await record(user.id, "auth.login_failed")
            raise HTTPException(401, INVALID_LOGIN)
        await throttle.reset(S.store, acct_key)
        token = await issue_session(S, user)
        set_session_cookie(response, S, token)
        await record(user.id, "auth.login")
        return {"user": user.public_view()}

    @router.post("/auth/logout")
    @translate
    async def logout(request: Request, response: Response, user: User = Depends(principal)):
        require_csrf_header(request)
        await revoke_session(S.store, request.state.session_id)
        clear_session_cookie(response, S)
        await record(user.id, "auth.logout")
        return {"status": "logged_out"}

    @router.post("/auth/logout-all")
    @translate
    async def logout_all(request: Request, response: Response, user: User = Depends(principal)):
        require_csrf_header(request)
        n = await revoke_all_sessions(S.store, user.id)
        clear_session_cookie(response, S)
        await record(user.id, "auth.logout_all", {"sessions": n})
        return {"status": "logged_out", "sessions_ended": n}

    @router.get("/auth/session")
    @translate
    async def session(user: User = Depends(principal)):
        return {"user": user.public_view(), "email_verified": user.email_verified}

    @router.post("/auth/change-password")
    @translate
    async def change_password(body: ChangePasswordBody, request: Request, response: Response,
                              user: User = Depends(principal)):
        require_csrf_header(request)
        _, acct_key = await keys(request, user.email)
        now = S.clock()
        d = await throttle.blocked(S.store, acct_key, limit=LOGIN_ACCOUNT[0], window_seconds=LOGIN_ACCOUNT[1], now=now)
        if not d.allowed:
            raise _too_many(d.retry_after)
        if not passwords.verify_password(body.current_password, await password_hash_of(S.store, user.id)):
            await throttle.hit(S.store, acct_key, limit=LOGIN_ACCOUNT[0], window_seconds=LOGIN_ACCOUNT[1], now=now)
            raise HTTPException(403, "The current password is not correct.")
        problem = (passwords.superadmin_password_error if user.is_platform_superadmin
                   else passwords.user_password_error)(body.new_password, user.email)
        if problem:
            raise HTTPException(422, problem)
        await set_password_hash(S.store, user.id, passwords.hash_password(body.new_password))   # ends all sessions
        fresh = await get_user(S.store, user.id)
        set_session_cookie(response, S, await issue_session(S, fresh))                        # keep this client in
        await record(user.id, "auth.password_changed")
        return {"status": "password_changed"}

    @router.post("/auth/forgot-password", status_code=202)
    @translate
    async def forgot_password(body: ForgotBody, request: Request, background: BackgroundTasks):
        require_csrf_header(request)
        started = time.monotonic()
        now = S.clock()
        ip = rate_limit_key(client_ip(request))
        for key, (limit, window) in ((throttle.throttle_key("reset-ip", ip), RESET_IP),
                                     (throttle.throttle_key("reset-acct", body.email.strip().lower()), RESET_ACCOUNT)):
            d = await throttle.hit(S.store, key, limit=limit, window_seconds=window, now=now)
            if not d.allowed:
                raise _too_many(d.retry_after)
        user = await find_user_by_email(S.store, body.email)
        if user is not None and user.status == "active":
            token = await issue_password_reset(S.store, user, now=now)
            # Phase 6B security review: the e-mail is sent AFTER the response (background task) and
            # a delivery failure is swallowed, so neither the answer nor its timing reveals whether
            # the account exists (previously the provider call ran inline and a failure was a 500).
            background.add_task(_send_reset, user.email, S.settings.reset_url_base + token)
            await record(user.id, "auth.password_reset_requested")
        await _response_floor(started)
        return {"status": "accepted"}                          # identical whether or not the account exists

    @router.post("/auth/reset-password")
    @translate
    async def reset_password(body: ResetBody, request: Request):
        require_csrf_header(request)
        invalid = HTTPException(400, "This reset link is invalid or has expired.")
        user_id = await peek_password_reset(S.store, body.token, now=S.clock())
        user = await get_user(S.store, user_id) if user_id else None
        if user is None or user.status != "active":
            raise invalid
        problem = (passwords.superadmin_password_error if user.is_platform_superadmin
                   else passwords.user_password_error)(body.new_password, user.email)
        if problem:
            raise HTTPException(422, problem)                   # token not spent: the user can retry
        if await consume_password_reset(S.store, body.token, now=S.clock()) != user.id:   # atomic single use
            raise invalid
        await set_password_hash(S.store, user.id, passwords.hash_password(body.new_password))   # ends all sessions
        await record(user.id, "auth.password_reset")
        return {"status": "password_reset"}
