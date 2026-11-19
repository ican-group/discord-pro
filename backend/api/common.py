"""Shared API plumbing: services, the one authentication dependency, target
loading + authorisation, error translation and views.

Every handler follows spec §11: authenticate → load the target by id → derive
ancestry from the database → authorise. No request header other than the
session (cookie or ``Authorization: Bearer``) is consulted for identity or
tenancy; ``X-Instance-ID``/``X-Org-ID``/``X-Workspace-ID``/``X-Enterprise-ID``
are ignored.
"""
from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import jwt
from fastapi import HTTPException, Request

from core import memberships as ms
from core import resolver
from core.authz import Action, Actor, AuthzError, RoleGrant, can, effective_role
from core.bindings import BindingConflict
from core.config_registry import ConfigError, PLATFORM_POLICY
from core.credentials import CredentialError, SecretBox
from core.deletion import DeletionError
from core.hierarchy import (Chain, ConcurrentModification, HierarchyError, HierarchyService, IntegrityError, Level,
                            LifecycleState, Node, NotFound, binding_to_doc)
from core.identity import SESSION_TYPE, IdentityError, User, get_user, session_claims_valid
from core.lifecycle import LifecycleError

from starlette.convertors import Convertor, register_url_convertor


class _Choice(Convertor):
    regex = ""
    def convert(self, value: str) -> str:
        return value
    def to_string(self, value: str) -> str:
        return value


def _choice(name: str, *values: str) -> None:
    conv = type(f"{name}Convertor", (_Choice,), {"regex": "|".join(values)})
    register_url_convertor(name, conv())


# Generic routes only match the listed collection names (no route shadowing).
_choice("objtype", "enterprises", "organisations", "bots", "instances")
_choice("scopetype", "enterprises", "organisations", "bots")
_choice("ownertype", "enterprises", "organisations")
_choice("policytype", "enterprises", "organisations")
_choice("gateway_op", "start", "stop", "restart")

TYPE_LEVEL = {"enterprises": Level.ENTERPRISE, "organisations": Level.ORGANISATION, "bots": Level.BOT,
              "instances": Level.BOT_INSTANCE}
LEVEL_TYPE = {v: k for k, v in TYPE_LEVEL.items()}
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "BitBotPro"
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Settings:
    jwt_secret: str
    encryption_key: Any            # one Fernet key (key id k1) or a core.credentials.KeyRing (Phase 6B)
    cookie_name: str = "bbp_session"
    session_ttl_seconds: int = 7 * 24 * 3600
    cookie_secure: bool = True                      # HTTPS only (tests run in-process)
    reset_url_base: str = "https://bitbotpro.io/app/reset-password#token="   # Phase 5 admin app page
    invitation_url_base: str = "https://bitbotpro.io/app/invitations#token="  # Phase 6A F3 (fragment, never sent to a server)
    public_api_origin: str = "https://api.bitbotpro.io"     # serves embed.js and the widget page (Phase 4B)
    webchat_daily_limit_per_instance: int = 2000            # legacy CHAT_DAILY_LIMIT_PER_BOT default (W7)
    telegram_validate_on_save: bool = True                  # getMe when a Telegram credential is created (T1)
    discord_validate_on_save: bool = True                   # token must belong to the stated application (D-Q14)
    zendesk_validate_on_save: bool = True                   # API tokens are checked with users/me (Z4/Z5)
    # Phase 5 (C14): when set, OAuth callbacks redirect the browser to the admin app's result page
    # (``<origin>/oauth/<provider>/result?status=…``) instead of answering JSON. Only the status code
    # and non-secret ids travel; the state, code and tokens never leave the API.
    oauth_result_origin: Optional[str] = None
    # Phase 5: exact origins of the admin app allowed to call the API with the session cookie
    # (CORS with credentials). Empty: no CORS headers at all (same-origin deployments, tests).
    admin_origins: tuple = ()

    def __post_init__(self):
        if not isinstance(self.jwt_secret, str) or len(self.jwt_secret) < 32:
            raise ValueError("jwt_secret must be at least 32 characters")


class Mailer:
    """Outbound email interface. Phase 4A has no live delivery: the default
    implementation drops messages and never logs tokens."""

    async def send_password_reset(self, email: str, reset_url: str) -> None:   # pragma: no cover - interface
        return None

    async def send_invitation(self, email: str, *, subject: str, text: str, html: str) -> None:  # pragma: no cover
        """Phase 6A F3: an Enterprise invitation (rendered by ``api.mail_templates``).
        The body contains the single-use link; implementations must never log it."""
        return None

    async def send_discord_invite(self, email: str, install_url: str, *, organisation_name: str,
                                  instance_name: str, expires_at: str) -> None:   # pragma: no cover - interface
        """D3 invitation email (Phase 4D C2). Names arrive as plain text without
        control characters; an implementation must still escape them for HTML."""
        return None


class Services:
    def __init__(self, store, settings: Settings, *, clock: Callable[[], str] = _now, mailer: Optional[Mailer] = None):
        self.store = store
        self.settings = settings
        self.clock = clock
        self.mailer = mailer or Mailer()
        self.hierarchy = HierarchyService(store, clock=clock)
        self.box = SecretBox(settings.encryption_key)
        self.providers: dict = {}           # AI provider adapters (test doubles in 4A; real ones later)
        # Phase 4K: the real, store-backed knowledge engine shared by every channel
        from runtime.knowledge_store import StoreCatalog, StoreRetriever
        self.retriever = StoreRetriever(store, clock=clock)
        self.catalog = StoreCatalog(store)
        self.telegram_transport = None      # httpx transport for the Telegram Bot API (tests: MockTransport)
        self.telegram_sleep = None          # injectable delay for Telegram retry_after (tests)
        self.discord_transport = None       # httpx transport for discord.com (tests: MockTransport)
        self.zendesk_transport = None       # httpx transport for *.zendesk.com (tests: MockTransport)
        self.zendesk_resolver = None        # DNS resolver for the outbound guard (tests: fake DNS)
        self.platform_policy = PLATFORM_POLICY


# ═════════════════════════════════ errors ═════════════════════════════════

def translate(fn):
    """Map domain errors to HTTP. Messages of domain errors are written to be
    safe for callers (no secrets, no other tenants' data)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except HTTPException:
            raise
        except (NotFound, IntegrityError):
            raise HTTPException(404, "Not found.")
        except AuthzError:
            raise HTTPException(403, "You do not have permission to do this.")
        except (ConcurrentModification, BindingConflict, LifecycleError, DeletionError) as exc:
            raise HTTPException(409, str(exc))
        except (ConfigError, CredentialError, IdentityError, HierarchyError) as exc:
            raise HTTPException(422, str(exc))
    return wrapper


# ═════════════════════════════════ authentication ═════════════════════════════════

async def issue_session(services: Services, user: User) -> str:
    """Create a server-side session record and its signed token (``sid`` claim)."""
    from core.identity import create_session
    session = await create_session(services.store, user, now=services.clock(),
                                   ttl_seconds=services.settings.session_ttl_seconds)
    iat = int(time.time())
    payload = {"sub": user.id, "tv": user.token_version, "typ": SESSION_TYPE, "sid": session["id"], "iat": iat,
               "exp": iat + services.settings.session_ttl_seconds}
    return jwt.encode(payload, services.settings.jwt_secret, algorithm="HS256")


def set_session_cookie(response, services: Services, token: str) -> None:
    response.set_cookie(services.settings.cookie_name, token, max_age=services.settings.session_ttl_seconds,
                        httponly=True, secure=services.settings.cookie_secure, samesite="strict", path="/api")


def clear_session_cookie(response, services: Services) -> None:
    response.delete_cookie(services.settings.cookie_name, path="/api", secure=services.settings.cookie_secure,
                           httponly=True, samesite="strict")


OAUTH_RESULT_IDS = ("instance_id", "guild_id", "integration_id", "subdomain")


def oauth_result(settings: "Settings", provider: str, status: int, code: str, message: str, extra: dict):
    """The callback answer (C14): a 303 to the admin app's result page when
    ``oauth_result_origin`` is configured, otherwise the JSON page."""
    from urllib.parse import urlencode
    from fastapi.responses import JSONResponse, RedirectResponse
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if settings.oauth_result_origin:
        q = {"status": code, **{k: str(v) for k, v in extra.items() if k in OAUTH_RESULT_IDS and v}}
        return RedirectResponse(f"{settings.oauth_result_origin.rstrip('/')}/oauth/{provider}/result?{urlencode(q)}",
                                status_code=303, headers=headers)
    return JSONResponse({"status": code, "detail": message, **extra}, status_code=status, headers=headers)


def require_csrf_header(request: Request) -> None:
    """Unsafe requests that can carry the session cookie must send the custom
    header (a cross-site form or image cannot set it)."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Missing request header.")


def make_principal_dependency(services: Services):
    async def principal(request: Request) -> User:
        """The one authentication dependency: identity, session validity and
        version, account status. Superadmin status comes from the database."""
        unauth = HTTPException(401, "Authentication required.")
        token, from_cookie = None, False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif request.cookies.get(services.settings.cookie_name):
            token, from_cookie = request.cookies[services.settings.cookie_name], True
        if not token or len(token) > 4096:
            raise unauth
        try:
            payload = jwt.decode(token, services.settings.jwt_secret, algorithms=["HS256"],
                                 options={"require": ["exp", "iat", "sub", "sid"]})
        except jwt.PyJWTError:
            raise unauth
        user = await get_user(services.store, payload.get("sub"))
        if not session_claims_valid(payload, user):
            raise unauth
        from core.identity import session_valid
        if not await session_valid(services.store, payload.get("sid"), user.id, now=services.clock()):
            raise unauth                                     # logged out, revoked or expired session
        request.state.session_id = payload["sid"]
        if from_cookie and request.method not in _SAFE_METHODS and \
                request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "Missing request header.")       # cookie sessions need the CSRF header
        return user
    return principal


# ═════════════════════════════════ authorisation ═════════════════════════════════

@dataclass
class Access:
    actor: Actor
    chain: Chain
    roster: ms.MemberSet          # the actor's memberships in this Enterprise
    policy: Dict[str, Any]
    grant: RoleGrant

    @property
    def target(self) -> Node:
        return self.chain.target


def policy_nodes(chain: Chain) -> List[Node]:
    return [n for n in chain.nodes() if n.level in (Level.ENTERPRISE, Level.ORGANISATION)]


async def load_access(services: Services, user: User, level: Level, object_id: str,
                      action: Optional[Action] = Action.READ) -> Access:
    """Load the target chain from the database and authorise. Unknown ids,
    objects in deletion and objects the user has no role on are all 404."""
    chain = await services.hierarchy.load_chain(level, object_id)          # NotFound/IntegrityError -> 404
    if any(n.lifecycle.state is LifecycleState.DELETED for n in chain.nodes()):
        raise NotFound("Not found.")
    await ms.resolve_stale(services.store, chain.enterprise_id)        # finish a crashed requester's change
    roster = await ms.load_for(services.store, chain.enterprise_id, [user.id])
    actor = user.actor()
    policy, _, _ = resolver.effective_policy(policy_nodes(chain), services.platform_policy)
    grant = effective_role(actor, chain, roster.members)
    if not grant.superadmin and grant.role is None:
        raise NotFound("Not found.")
    if action is not None:
        d = can(actor, action, chain, roster.members, policy)
        if not d.allowed:
            raise AuthzError(d.reason)
    return Access(actor, chain, roster, policy, grant)


def require_superadmin(user: User) -> None:
    if not user.is_platform_superadmin:
        raise NotFound("Not found.")                    # platform routes do not exist for tenants


# ═════════════════════════════════ views ═════════════════════════════════

def node_view(node: Node, chain: Optional[Chain] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": node.id, "type": LEVEL_TYPE[node.level], "name": node.name,
                           "lifecycle": node.lifecycle.to_doc(), "overrides": dict(node.overrides or {}),
                           "created_at": node.created_at, "updated_at": node.updated_at, "version": node.version}
    for f in ("enterprise_id", "organisation_id", "bot_id", "slug", "plan", "description"):
        if hasattr(node, f):
            out[f] = getattr(node, f)
    if hasattr(node, "policy"):
        out["policy"] = dict(node.policy or {})
    if node.level is Level.BOT:
        out["logo"] = node.logo
    if node.level is Level.BOT_INSTANCE:
        out["channel"] = binding_to_doc(node.channel)
    if chain is not None:
        a = chain.availability()
        out["available"] = a.available
        out["unavailable_reason"] = a.reason or None
    return out


def jsonable(value: Any) -> Any:
    from enum import Enum
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return value
