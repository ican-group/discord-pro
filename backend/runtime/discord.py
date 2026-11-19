"""Discord: Enterprise applications, REST, OAuth and pure helpers (Phase 4D).

D-Q14 (approved 2026-11-18): **each Enterprise has its own Discord
application**, configured and controlled exclusively by platform superadmins
and shared by every Organisation of that Enterprise for its authorised Bot
Instance connections. There is no global application, and none per
Organisation or Bot.

Three things are kept apart:
* the application *configuration* (``discord_applications``, one per
  Enterprise, unique ``application_id``; bot token and client secret
  encrypted, exposed only as fingerprints);
* server *installations* (``discord_installations``, ``<app>:<guild>``),
  recorded by the OAuth callback and by gateway events;
* Bot Instance *bindings* (exclusive claims ``discord:<app>:<guild>`` with
  channel scopes; ``core.bindings``).

The REST client talks only to ``discord.com``. The token is sent only in the
``Authorization`` header and never logged or raised, timeouts are bounded,
and errors map to safe categories (``connect_failed`` = not sent;
``uncertain`` = may have been processed).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from core.hierarchy import DuplicateKey, HierarchyError, is_valid_id

API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
USER_AGENT = "DiscordBot (https://bitbotpro.io, 1.0)"
MAX_MESSAGE = 2000
APPS = "discord_applications"
INSTALLS = "discord_installations"
OAUTH_STATES = "oauth_states"
SNOWFLAKE = re.compile(r"[1-9][0-9]{15,20}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{20,40}\.[A-Za-z0-9_\-]{4,10}\.[A-Za-z0-9_\-]{20,60}")

# Permission bits
ADMINISTRATOR, MANAGE_GUILD = 0x8, 0x20
# Default bot permissions: view channels, send messages, embed links, read history, change nickname.
DEFAULT_PERMISSIONS = 1024 + 2048 + 16384 + 65536 + 67108864
# Gateway intents: GUILDS, GUILD_MEMBERS, GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT
REQUIRED_INTENTS = 1 | 512 | 32768
DEFAULT_INTENTS = 1 | 2 | 512 | 4096 | 32768
ALLOWED_INTENTS = 1 | 2 | 512 | 4096 | 32768 | (1 << 9) | (1 << 10)
OAUTH_SCOPES = "bot identify guilds"

for _name in ("httpx", "httpcore"):
    if logging.getLogger(_name).level < logging.WARNING:
        logging.getLogger(_name).setLevel(logging.WARNING)


class DiscordError(Exception):
    def __init__(self, category: str, retry_after: float = 0.0):
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after


async def _request(method: str, path: str, *, headers: Dict[str, str], transport=None, json: Any = None,
                   data: Any = None) -> Any:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False,
                                     verify=True) as client:
            resp = await client.request(method, API + path, headers={"User-Agent": USER_AGENT, **headers},
                                        json=json, data=data)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise DiscordError("connect_failed") from None
    except (httpx.TimeoutException, httpx.TransportError):
        raise DiscordError("uncertain") from None
    if resp.status_code in (200, 201):
        try:
            return resp.json()
        except ValueError:
            raise DiscordError("uncertain") from None
    if resp.status_code == 204:
        return None
    code = resp.status_code
    if code == 429:
        try:
            retry = float((resp.json() or {}).get("retry_after", 1.0))
        except (ValueError, AttributeError):
            retry = 1.0
        raise DiscordError("rate_limited", retry_after=min(max(retry, 0.0), 3600.0))
    raise DiscordError({400: "bad_request", 401: "unauthorized", 403: "forbidden",
                        404: "not_found"}.get(code, "server_error" if code >= 500 else "bad_request"))


class DiscordRest:
    """Bot-token REST client for one Enterprise application."""

    def __init__(self, token: str, *, transport=None):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise DiscordError("invalid_token")
        self._auth = {"Authorization": f"Bot {token}"}
        self._transport = transport

    def __repr__(self) -> str:
        return "DiscordRest(****)"

    async def call(self, method: str, path: str, json: Any = None) -> Any:
        return await _request(method, path, headers=self._auth, transport=self._transport, json=json)

    async def me(self) -> Dict[str, Any]:
        return await self.call("GET", "/users/@me")

    async def application(self) -> Dict[str, Any]:
        return await self.call("GET", "/oauth2/applications/@me")

    async def guild(self, guild_id: str) -> Dict[str, Any]:
        return await self.call("GET", f"/guilds/{guild_id}")

    async def guild_channels(self, guild_id: str) -> List[Dict[str, Any]]:
        return await self.call("GET", f"/guilds/{guild_id}/channels")

    async def guild_roles(self, guild_id: str) -> List[Dict[str, Any]]:
        return await self.call("GET", f"/guilds/{guild_id}/roles")

    async def guild_member(self, guild_id: str, user_id: str) -> Dict[str, Any]:
        return await self.call("GET", f"/guilds/{guild_id}/members/{user_id}")

    async def create_message(self, channel_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.call("POST", f"/channels/{channel_id}/messages", payload)

    async def typing(self, channel_id: str) -> None:
        await self.call("POST", f"/channels/{channel_id}/typing")

    async def set_nickname(self, guild_id: str, nick: Optional[str]) -> None:
        await self.call("PATCH", f"/guilds/{guild_id}/members/@me", {"nick": nick})

    async def set_avatar(self, data_uri: str) -> None:
        await self.call("PATCH", "/users/@me", {"avatar": data_uri})

    async def leave_guild(self, guild_id: str) -> None:
        await self.call("DELETE", f"/users/@me/guilds/{guild_id}")

    async def open_dm(self, user_id: str) -> Dict[str, Any]:
        return await self.call("POST", "/users/@me/channels", {"recipient_id": user_id})


async def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str,
                        transport=None) -> Dict[str, Any]:
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{10,100}", code):
        raise DiscordError("bad_request")
    return await _request("POST", "/oauth2/token", headers={"Content-Type": "application/x-www-form-urlencoded"},
                          transport=transport,
                          data={"client_id": client_id, "client_secret": client_secret, "code": code,
                                "grant_type": "authorization_code", "redirect_uri": redirect_uri})


async def user_guilds(access_token: str, *, transport=None) -> List[Dict[str, Any]]:
    return await _request("GET", "/users/@me/guilds", headers={"Authorization": f"Bearer {access_token}"},
                          transport=transport)


def can_manage_guild(guild_entry: Dict[str, Any]) -> bool:
    """From Discord's own ``/users/@me/guilds`` data (OAuth user token)."""
    try:
        perms = int(guild_entry.get("permissions", "0"))
    except (TypeError, ValueError):
        perms = 0
    return guild_entry.get("owner") is True or bool(perms & (ADMINISTRATOR | MANAGE_GUILD))


# ═════════════════════════════════ Enterprise application store ═════════════════════════════════

def fingerprint(secret: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:10]


def app_view(doc: Optional[dict], *, redirect_uri: str = "") -> Dict[str, Any]:
    """Superadmin view: never secrets, only fingerprints."""
    if not doc:
        return {"configured": False}
    keys = ("enterprise_id", "application_id", "bot_user_id", "bot_username", "permissions", "intents", "status",
            "gateway_desired", "restart_requested_at", "configured_at", "token_rotated_at", "secret_rotated_at")
    view = {k: doc.get(k) for k in keys}
    view.update(configured=True, token_fingerprint=doc.get("bot_token_fp"),
                client_secret_fingerprint=doc.get("client_secret_fp"), redirect_uri=redirect_uri,
                oauth_scopes=OAUTH_SCOPES)
    return view


def tenant_view(doc: Optional[dict]) -> Dict[str, Any]:
    """What Enterprise and Organisation users may know: whether Discord is usable."""
    return {"configured": bool(doc), "available": bool(doc and doc.get("status") == "enabled")}


async def get_app(store, enterprise_id: str) -> Optional[dict]:
    return await store.get(APPS, enterprise_id) if is_valid_id(enterprise_id) else None


async def app_by_application_id(store, application_id: str) -> Optional[dict]:
    docs = await store.find(APPS, {"application_id": application_id}, limit=1)
    return docs[0] if docs else None


def validate_intents(value: Any) -> int:
    if not isinstance(value, int) or value & ~ALLOWED_INTENTS or value & REQUIRED_INTENTS != REQUIRED_INTENTS:
        raise HierarchyError("Intents must include GUILDS, GUILD_MESSAGES and MESSAGE_CONTENT and only allowed bits.")
    return value


def validate_permissions(value: Any) -> int:
    if not isinstance(value, int) or value < 0 or value >= (1 << 50) or value & ADMINISTRATOR:
        raise HierarchyError("Invalid bot permissions (administrator is never requested).")
    return value


async def record_installation(store, app: dict, guild_id: str, *, now: str, by: str) -> None:
    doc = {"id": f"{app['application_id']}:{guild_id}", "enterprise_id": app["enterprise_id"],
           "application_id": app["application_id"], "guild_id": guild_id, "installed_at": now, "installed_by": by,
           "state": "present", "version": 1}
    try:
        await store.insert(INSTALLS, doc)
    except DuplicateKey:
        await store.update(INSTALLS, doc["id"], {"state": "present", "installed_by": by, "installed_at": now})


async def remove_installation(store, application_id: str, guild_id: str) -> None:
    await store.delete(INSTALLS, f"{application_id}:{guild_id}")


# ═════════════════════════════════ pure helpers ═════════════════════════════════

@dataclass(frozen=True)
class Decision:
    respond: bool
    text: str = ""
    mentioned: bool = False


def decide(event: Dict[str, Any], *, listen_mode: str, channel_ids: Sequence[str], mention_overrides: bool,
           bot_user_id: str, is_dm: bool) -> Decision:
    """Listen modes (D6), legacy semantics. Channel ownership is enforced
    earlier by the claims (an instance only ever sees its own scope)."""
    content = event.get("content")
    if not isinstance(content, str) or not content.strip():
        return Decision(False)
    mentioned = any(str(m.get("id")) == str(bot_user_id) for m in event.get("mentions") or [] if isinstance(m, dict))
    channel = str(event.get("channel_id", ""))
    if is_dm:
        respond = True
    elif listen_mode == "all_channels":
        respond = True
    elif listen_mode == "specific_channels":
        respond = channel in set(channel_ids) or (mention_overrides and mentioned)
    else:
        respond = mentioned
    if not respond:
        return Decision(False, mentioned=mentioned)
    text = re.sub(rf"<@!?{re.escape(str(bot_user_id))}>", "", content).strip()[:4000]
    return Decision(bool(text), text, mentioned)


def split_message(text: str, limit: int = MAX_MESSAGE) -> List[str]:
    """≤2000 characters per message, on paragraph/line/word boundaries, never
    leaving a code fence open across messages (D10 fix)."""
    text = text or ""
    out: List[str] = []
    rest = text
    budget = limit - 8
    while len(rest) > limit:
        window = rest[:budget]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < budget // 2:
            cut = budget
        chunk, rest = rest[:cut].rstrip(), rest[cut:].lstrip()
        if chunk.count("```") % 2 == 1:
            chunk += "\n```"
            rest = "```\n" + rest
        out.append(chunk)
    if rest:
        out.append(rest)
    return out


SAFE_MENTIONS = {"parse": [], "replied_user": False}       # never @everyone/@here/roles (D10)


def staff_role_ids(roles: Iterable[Dict[str, Any]], staff_role_name: str) -> List[str]:
    want = (staff_role_name or "").strip().lower()
    norm = re.sub(r"[\s_\-]", "", want)
    return [str(r["id"]) for r in roles if isinstance(r, dict) and want and
            (str(r.get("name", "")).lower() == want or re.sub(r"[\s_\-]", "", str(r.get("name", "")).lower()) == norm)]


# ═════════════════════════════════ deletion cleanup (X12, D-Q13) ═════════════════════════════════

def discord_cleanup_executors(box, *, transport=None) -> Dict[str, Any]:
    from core.bindings import BindingRegistry
    from core.deletion import CleanupOutcome, CleanupStatus

    async def routing_removal(action: Dict[str, Any], ctx) -> CleanupOutcome:
        """Routing is the claims table: removal is verified, not assumed."""
        held = await BindingRegistry(ctx.store).keys_held_by(action["target"]["instance_id"])
        if held:
            return CleanupOutcome(CleanupStatus.RETRY, "claims_remaining")
        return CleanupOutcome(CleanupStatus.SUCCEEDED)

    async def leave_guild(action: Dict[str, Any], ctx) -> CleanupOutcome:
        t = action["target"]
        holders = await BindingRegistry(ctx.store).holders(f"discord:{t['application_id']}:{t['guild_id']}")
        if holders:                              # another instance of this application still needs the server
            return CleanupOutcome(CleanupStatus.NOT_REQUIRED)
        app = await ctx.store.get(APPS, t.get("application_enterprise_id") or "")
        if not app or app.get("application_id") != t["application_id"] or not app.get("bot_token_enc"):
            return CleanupOutcome(CleanupStatus.RETRY, "application_unavailable")
        try:
            await DiscordRest(box.decrypt(app["bot_token_enc"]), transport=transport).leave_guild(t["guild_id"])
        except DiscordError as exc:
            if exc.category == "not_found":
                pass                             # already gone: idempotent success
            else:
                return CleanupOutcome(CleanupStatus.RETRY, exc.category)
        await remove_installation(ctx.store, t["application_id"], t["guild_id"])
        return CleanupOutcome(CleanupStatus.SUCCEEDED)

    return {"discord_routing_removal": routing_removal, "discord_leave_guild": leave_guild}


__all__ = ["DiscordRest", "DiscordError", "exchange_code", "user_guilds", "can_manage_guild", "app_view",
           "tenant_view", "get_app", "app_by_application_id", "validate_intents", "validate_permissions",
           "record_installation", "remove_installation", "decide", "Decision", "split_message", "SAFE_MENTIONS",
           "staff_role_ids", "discord_cleanup_executors", "APPS", "INSTALLS", "OAUTH_STATES", "AUTHORIZE_URL",
           "DEFAULT_PERMISSIONS", "DEFAULT_INTENTS", "REQUIRED_INTENTS", "OAUTH_SCOPES", "fingerprint"]
