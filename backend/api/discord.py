"""Discord routes (Phase 4D; D-Q14, parity D1–D19).

**Platform superadmins only** (``/platform/enterprises/{eid}/discord-application``):
create/replace the Enterprise's Discord application (application id, bot
token, client secret, permissions, intents), rotate the token or secret,
enable/disable, start/stop/restart its gateway, set the bot avatar, read
the full (secret-free) status. Tenants get 404 on every one of these.

**Enterprise members** (READ): ``GET /enterprises/{eid}/discord`` returns
only whether Discord is configured and available.

**Organisation/instance admins** (MANAGE_CHANNEL on the instance):
* ``POST /instances/{id}/channel/discord/install-url``: the Enterprise
  application's OAuth URL with a strong, single-use, 10-minute state bound to
  user, Enterprise, instance and application id.
* ``GET /hooks/discord/oauth/callback``: no session cookie is assumed (it is
  SameSite=Strict and the request is a cross-site navigation). Authority
  comes from the state record: the user is reloaded and must still be
  active, on the same token version, and still hold MANAGE_CHANNEL on the
  instance; the application must still be the Enterprise's and enabled. The
  code is exchanged with the Enterprise client secret, and the user's
  authority over the server is checked against Discord's own data
  (owner, ADMINISTRATOR or MANAGE_GUILD) even when the bot is already
  installed. The bot must be present. Then the binding is claimed
  atomically; when a whole-server claim is impossible because the server is
  shared, a pending installation (30 min, this instance only) waits for a
  channel selection.
* ``GET  /instances/{id}/channel/discord/channels``: text channels with
  ``selected`` / ``available`` / ``unavailable`` (never who holds them).
* ``PUT  /instances/{id}/channel/discord/settings``: channels (validated
  against the server), listen mode, reply style, passive mode, staff role,
  handoff cooldown and follow-up, DMs.
* ``PUT  /instances/{id}/channel/discord/nickname``: only when this instance
  is the only holder of the server for its application; otherwise
  superadmin.
* ``disconnect``/``test``/``status`` share the generic channel routes.
"""
import base64
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from api.common import Services, load_access, oauth_result, require_superadmin, translate
from core import audit
from core.authz import Action
from core.bindings import WHOLE, BindingConflict, BindingRegistry
from core import throttle
from core.hierarchy import (COLLECTION, ChannelType, ConcurrentModification, DuplicateKey, HierarchyError, Level,
                            NotFound, binding_from_doc, binding_to_doc, is_valid_id)
from core.identity import User, get_user
from runtime import discord as dc
from runtime.discord_gateway import STATUS as GW_STATUS, status_view

STATE_TTL = timedelta(minutes=10)
SHARE_TTL = timedelta(days=7)                       # legacy share-link lifetime (D3)
SHARES = "discord_share_links"
HANDOFF_PREFIX = "dc-handoff"
INVITE_RATE = (20, 3600)                            # invite emails per user per hour
PENDING_TTL = timedelta(minutes=30)
TEXT_CHANNEL_TYPES = (0, 5)
MAX_AVATAR_BYTES = 256 * 1024
SETTINGS_FIELDS = ("listen_mode", "channel_ids", "mention_overrides_channels", "reply_style", "passive_mode",
                   "staff_role_name", "handoff_cooldown_minutes", "handoff_followup_message", "dm_enabled")


class AppBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_id: str
    bot_token: Optional[str] = None
    client_secret: Optional[str] = None
    permissions: Optional[int] = None
    intents: Optional[int] = None


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class AvatarBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str                 # data:image/(png|jpeg|gif);base64,...


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listen_mode: Optional[str] = None
    channel_ids: Optional[List[str]] = None
    mention_overrides_channels: Optional[bool] = None
    reply_style: Optional[str] = None
    passive_mode: Optional[bool] = None
    staff_role_name: Optional[str] = None
    handoff_cooldown_minutes: Optional[int] = None
    handoff_followup_message: Optional[str] = None
    dm_enabled: Optional[bool] = None


class ShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_guild_id: Optional[str] = None        # optional: the link only works for this server
    email: Optional[str] = None                    # optional: send the link to a server administrator


class HandoffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int


class NickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: Optional[str] = None


def channel_for(S: Services, clock=None):
    from runtime.discord_channel import DiscordChannel
    return DiscordChannel(S.store, box=S.box, clock=clock or S.clock, transport=S.discord_transport,
                          providers=S.providers, retriever=S.retriever, catalog=S.catalog,
                          platform_policy=S.platform_policy)


def _plain(value: str) -> str:
    """Names for emails: no control characters or line breaks (header/format injection)."""
    return re.sub(r"[\x00-\x1f\x7f<>]", " ", str(value or ""))[:100].strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def redirect_uri(S: Services) -> str:
    return f"{S.settings.public_api_origin}/api/hooks/discord/oauth/callback"


def rest_for(S: Services, app: dict) -> dc.DiscordRest:
    return dc.DiscordRest(S.box.decrypt(app["bot_token_enc"]), transport=S.discord_transport)


def mapped(exc: dc.DiscordError) -> HTTPException:
    messages = {"unauthorized": "Discord rejected the bot token.",
                "invalid_token": "That is not a valid Discord bot token.",
                "forbidden": "The bot is missing a Discord permission for this.",
                "not_found": "The bot is not in that Discord server.",
                "rate_limited": "Discord is rate limiting requests. Try again shortly."}
    return HTTPException(429 if exc.category == "rate_limited" else 422,
                         messages.get(exc.category, "Discord is not reachable right now."))


async def discord_channel_check(S: Services, chain, current, changes: Dict[str, Any]) -> None:
    """Channel selection must name text channels of the instance's own server."""
    want = changes.get("channel_ids")
    for key in ("guild_id", "application_id"):
        if key in changes and changes[key] != getattr(current, key):
            raise HierarchyError("The Discord guild is set by the install flow.")
    if want is None or list(want) == list(current.channel_ids):
        return
    if not current.guild_id:
        if want:
            raise HierarchyError("Connect the instance to a Discord server before selecting channels.")
        return
    app = await dc.get_app(S.store, chain.enterprise_id)
    if not app or app.get("application_id") != current.application_id:
        raise HierarchyError("The Enterprise Discord application is not available.")
    try:
        channels = await rest_for(S, app).guild_channels(current.guild_id)
    except dc.DiscordError as exc:
        raise mapped(exc)
    valid = {str(c.get("id")) for c in channels or [] if isinstance(c, dict) and c.get("type") in TEXT_CHANNEL_TYPES}
    bad = [c for c in want if str(c) not in valid]
    if bad:
        raise HierarchyError("Some channels are not text channels of this Discord server.")


def register(router: APIRouter, S: Services, principal) -> None:
    # ═════════════════════ platform superadmin: the Enterprise application ═════════════════════
    async def enterprise_or_404(eid: str):
        if not is_valid_id(eid):
            raise NotFound("Not found.")
        return await S.hierarchy.load_chain(Level.ENTERPRISE, eid)

    async def app_or_404(eid: str) -> dict:
        app = await dc.get_app(S.store, eid)
        if app is None:
            raise HTTPException(404, "No Discord application is configured for this Enterprise.")
        return app

    async def validate_token(application_id: str, token: str) -> Dict[str, Any]:
        """The token must belong to exactly this application (no cross-wiring)."""
        rest = dc.DiscordRest(token, transport=S.discord_transport)
        if not S.settings.discord_validate_on_save:
            return {}
        try:
            app_info = await rest.application()
            me = await rest.me()
        except dc.DiscordError as exc:
            raise mapped(exc)
        if str((app_info or {}).get("id")) != application_id:
            raise HTTPException(422, "The bot token belongs to a different Discord application.")
        return {"bot_user_id": str(me.get("id")), "bot_username": str(me.get("username", ""))[:100]}

    async def superaudit(user: User, action: str, eid: str, details: Dict[str, Any]) -> None:
        await audit.record(S.store, actor=user.actor(), action=action, enterprise_id=eid,
                           target_level="enterprise", target_id=eid, now=S.clock(), details=details)

    async def save(app: dict, **fields) -> dict:
        fields["updated_at"] = S.clock()
        await S.store.update(dc.APPS, app["id"], fields, expect_version=app["version"])
        return await S.store.get(dc.APPS, app["id"])

    def full_view(app: Optional[dict], gw: Optional[dict]) -> Dict[str, Any]:
        return {**dc.app_view(app, redirect_uri=redirect_uri(S)), "gateway": status_view(gw)}

    base = "/platform/enterprises/{enterprise_id}/discord-application"

    @router.get(base)
    @translate
    async def get_application(enterprise_id: str, user: User = Depends(principal)):
        require_superadmin(user)
        await enterprise_or_404(enterprise_id)
        return full_view(await dc.get_app(S.store, enterprise_id), await S.store.get(GW_STATUS, enterprise_id))

    @router.put(base)
    @translate
    async def put_application(enterprise_id: str, body: AppBody, user: User = Depends(principal)):
        require_superadmin(user)
        await enterprise_or_404(enterprise_id)
        if not dc.SNOWFLAKE.fullmatch(body.application_id):
            raise HTTPException(422, "Invalid Discord application id.")
        current = await dc.get_app(S.store, enterprise_id)
        permissions = dc.validate_permissions(body.permissions if body.permissions is not None else
                                              (current or {}).get("permissions", dc.DEFAULT_PERMISSIONS))
        intents = dc.validate_intents(body.intents if body.intents is not None else
                                      (current or {}).get("intents", dc.DEFAULT_INTENTS))
        now = S.clock()
        replacing = current is not None and current["application_id"] != body.application_id
        if current is None or replacing:
            if not body.bot_token or not body.client_secret:
                raise HTTPException(422, "A new application needs its bot token and client secret.")
            if replacing:
                # never orphan bindings: the old application's servers must be disconnected first
                used = await S.store.count(COLLECTION[Level.BOT_INSTANCE],
                                           {"enterprise_id": enterprise_id,
                                            "channel.application_id": current["application_id"]})
                if used:
                    raise HTTPException(409, "Disconnect every instance from the current application first.")
        if body.bot_token:
            ident = await validate_token(body.application_id, body.bot_token)
        elif current is not None and not replacing:
            ident = {}
        else:
            raise HTTPException(422, "A bot token is required.")
        fields: Dict[str, Any] = {"application_id": body.application_id, "permissions": permissions,
                                  "intents": intents, **ident}
        if body.bot_token:
            fields.update(bot_token_enc=S.box.encrypt(body.bot_token), bot_token_fp=dc.fingerprint(body.bot_token),
                          token_rotated_at=now)
        if body.client_secret:
            if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", body.client_secret):
                raise HTTPException(422, "Invalid client secret.")
            fields.update(client_secret_enc=S.box.encrypt(body.client_secret),
                          client_secret_fp=dc.fingerprint(body.client_secret), secret_rotated_at=now)
        try:
            if current is None:
                await S.store.insert(dc.APPS, {"id": enterprise_id, "enterprise_id": enterprise_id,
                                               "status": "enabled", "gateway_desired": "stopped",
                                               "restart_requested_at": None, "configured_at": now,
                                               "configured_by": user.id, "updated_at": now, "version": 1, **fields})
            else:
                if replacing:
                    fields.update(gateway_desired="stopped", restart_requested_at=now)
                await save(current, **fields)
        except DuplicateKey:
            raise HTTPException(409, "That Discord application is already assigned to another Enterprise.")
        await superaudit(user, "discord_application.replace" if replacing else
                         ("discord_application.create" if current is None else "discord_application.update"),
                         enterprise_id, {"application_id": body.application_id, "token_changed": bool(body.bot_token),
                                         "secret_changed": bool(body.client_secret)})
        return full_view(await dc.get_app(S.store, enterprise_id), await S.store.get(GW_STATUS, enterprise_id))

    @router.post(base + "/rotate-token")
    @translate
    async def rotate_token(enterprise_id: str, body: SecretBody, user: User = Depends(principal)):
        require_superadmin(user)
        await enterprise_or_404(enterprise_id)
        app = await app_or_404(enterprise_id)
        ident = await validate_token(app["application_id"], body.value)
        now = S.clock()
        app = await save(app, bot_token_enc=S.box.encrypt(body.value), bot_token_fp=dc.fingerprint(body.value),
                         token_rotated_at=now, restart_requested_at=now, **ident)
        gw = await S.store.get(GW_STATUS, enterprise_id)
        if gw and gw.get("state") == "failed_auth":
            await S.store.update(GW_STATUS, enterprise_id, {"state": "stopped", "attempts": 0,
                                                            "next_attempt_at": None})
        await superaudit(user, "discord_application.rotate_token", enterprise_id,
                         {"application_id": app["application_id"]})
        return full_view(app, await S.store.get(GW_STATUS, enterprise_id))

    @router.post(base + "/rotate-secret")
    @translate
    async def rotate_secret(enterprise_id: str, body: SecretBody, user: User = Depends(principal)):
        require_superadmin(user)
        await enterprise_or_404(enterprise_id)
        app = await app_or_404(enterprise_id)
        if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", body.value):
            raise HTTPException(422, "Invalid client secret.")
        app = await save(app, client_secret_enc=S.box.encrypt(body.value), client_secret_fp=dc.fingerprint(body.value),
                         secret_rotated_at=S.clock())
        await superaudit(user, "discord_application.rotate_secret", enterprise_id,
                         {"application_id": app["application_id"]})
        return full_view(app, await S.store.get(GW_STATUS, enterprise_id))

    async def set_fields(enterprise_id: str, user: User, action: str, **fields):
        require_superadmin(user)
        await enterprise_or_404(enterprise_id)
        app = await save(await app_or_404(enterprise_id), **fields)
        await superaudit(user, action, enterprise_id, {"application_id": app["application_id"]})
        return full_view(app, await S.store.get(GW_STATUS, enterprise_id))

    @router.post(base + "/enable")
    @translate
    async def enable(enterprise_id: str, user: User = Depends(principal)):
        return await set_fields(enterprise_id, user, "discord_application.enable", status="enabled")

    @router.post(base + "/disable")
    @translate
    async def disable(enterprise_id: str, user: User = Depends(principal)):
        return await set_fields(enterprise_id, user, "discord_application.disable", status="disabled",
                                gateway_desired="stopped")

    @router.post(base + "/gateway/{op:gateway_op}")
    @translate
    async def gateway(enterprise_id: str, op: str, user: User = Depends(principal)):
        if op == "start":
            return await set_fields(enterprise_id, user, "discord_gateway.start", gateway_desired="running")
        if op == "stop":
            return await set_fields(enterprise_id, user, "discord_gateway.stop", gateway_desired="stopped")
        return await set_fields(enterprise_id, user, "discord_gateway.restart", gateway_desired="running",
                                restart_requested_at=S.clock())

    @router.post(base + "/avatar")
    @translate
    async def avatar(enterprise_id: str, body: AvatarBody, user: User = Depends(principal)):
        """D16: the avatar belongs to the Enterprise application (every server
        sees it), so it is superadmin-only."""
        require_superadmin(user)
        await enterprise_or_404(enterprise_id)
        app = await app_or_404(enterprise_id)
        m = re.fullmatch(r"data:image/(png|jpeg|gif);base64,([A-Za-z0-9+/=]+)", body.image or "")
        if not m:
            raise HTTPException(422, "The avatar must be a PNG, JPEG or GIF data URI.")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except ValueError:
            raise HTTPException(422, "The avatar is not valid base64.")
        if len(raw) > MAX_AVATAR_BYTES:
            raise HTTPException(413, "The avatar is too large.")
        magic = {"png": b"\x89PNG", "jpeg": b"\xff\xd8\xff", "gif": b"GIF8"}[m.group(1)]
        if not raw.startswith(magic):
            raise HTTPException(422, "The avatar content does not match its type.")
        try:
            await rest_for(S, app).set_avatar(body.image)
        except dc.DiscordError as exc:
            raise mapped(exc)
        await superaudit(user, "discord_application.avatar", enterprise_id,
                         {"application_id": app["application_id"], "bytes": len(raw)})
        return {"ok": True}

    # ═════════════════════ Enterprise members: availability only ═════════════════════
    @router.get("/enterprises/{enterprise_id}/discord")
    @translate
    async def enterprise_discord(enterprise_id: str, user: User = Depends(principal)):
        # Phase 6A: any member inside the Enterprise (e.g. an Organisation admin) may see whether the
        # Enterprise application is configured/available — only those two booleans, nothing else.
        from api.hierarchy import _navigation_access
        await _navigation_access(S, user, Level.ENTERPRISE, enterprise_id)
        return dc.tenant_view(await dc.get_app(S.store, enterprise_id))

    # ═════════════════════ instance admins: install, channels, settings ═════════════════════
    async def discord_access(user: User, instance_id: str, action: Action):
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id, action)
        if a.target.channel.type is not ChannelType.DISCORD:
            raise NotFound("Not found.")
        return a

    async def usable_app(chain) -> dict:
        app = await dc.get_app(S.store, chain.enterprise_id)
        if not app or app.get("status") != "enabled":
            raise HTTPException(409, "Discord is not available for this Enterprise.")
        return app

    @router.post("/instances/{instance_id}/channel/discord/install-url")
    @translate
    async def install_url(instance_id: str, user: User = Depends(principal)):
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        if not a.chain.availability().available:
            raise HTTPException(409, "This instance is not active.")
        app = await usable_app(a.chain)
        state = secrets.token_urlsafe(32)
        expires = datetime.fromisoformat(S.clock()) + STATE_TTL
        await S.store.insert(dc.OAUTH_STATES, {
            "id": _hash(state), "kind": "discord_install", "user_id": user.id, "user_tv": user.token_version,
            "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id,
            "bot_id": a.chain.bot.id, "instance_id": instance_id, "application_id": app["application_id"],
            "expires_at": expires.isoformat(), "expires_at_dt": expires, "version": 1})
        query = {"client_id": app["application_id"], "scope": dc.OAUTH_SCOPES, "permissions": str(app["permissions"]),
                 "response_type": "code", "redirect_uri": redirect_uri(S), "state": state}
        await audit.record(S.store, actor=a.actor, action="discord.install_started",
                           enterprise_id=a.chain.enterprise_id, target_level="bot_instance", target_id=instance_id,
                           now=S.clock(), details={})
        return {"url": f"{dc.AUTHORIZE_URL}?{urlencode(query)}", "expires_at": expires.isoformat()}

    def page(status: int, code: str, message: str, **extra):
        return oauth_result(S.settings, "discord", status, code, message, extra)

    @router.get("/hooks/discord/oauth/callback")
    async def oauth_callback(request: Request):
        q = request.query_params
        state, code, guild_id = q.get("state") or "", q.get("code") or "", q.get("guild_id") or ""
        if not (20 <= len(state) <= 128):
            return page(400, "invalid_state", "This install link is invalid or has expired.")
        rec = await S.store.take(dc.OAUTH_STATES, _hash(state))          # single use, even on failure
        if not rec or rec.get("kind") != "discord_install" or \
                datetime.fromisoformat(S.clock()) >= datetime.fromisoformat(rec["expires_at"]):
            return page(400, "invalid_state", "This install link is invalid or has expired.")
        if q.get("error"):
            return page(400, "cancelled", "The Discord installation was cancelled.")
        if not code or not dc.SNOWFLAKE.fullmatch(guild_id):
            return page(400, "invalid_request", "Discord did not return a server for this installation.")
        try:
            user = await get_user(S.store, rec["user_id"])
            if user is None or user.status != "active" or user.token_version != rec["user_tv"]:
                raise NotFound("gone")
            a = await discord_access(user, rec["instance_id"], Action.MANAGE_CHANNEL)   # re-authorised now
        except Exception:
            return page(403, "not_authorised", "You are no longer allowed to connect this instance.")
        if a.chain.enterprise_id != rec["enterprise_id"] or not a.chain.availability().available:
            return page(403, "not_authorised", "You are no longer allowed to connect this instance.")
        app = await dc.get_app(S.store, a.chain.enterprise_id)
        if not app or app.get("status") != "enabled" or app["application_id"] != rec["application_id"]:
            return page(409, "application_changed", "The Enterprise Discord application changed. Start again.")
        share = None
        if rec.get("share_ref"):
            share = await S.store.get(SHARES, rec["share_ref"])
            if not share or share.get("status") != "active" or share.get("instance_id") != rec["instance_id"] \
                    or share.get("enterprise_id") != rec["enterprise_id"] \
                    or datetime.fromisoformat(S.clock()) >= datetime.fromisoformat(share["expires_at"]):
                return page(410, "share_link_inactive", "This invitation link is no longer valid.")
            if share.get("expected_guild_id") and share["expected_guild_id"] != guild_id:
                return page(403, "wrong_server", "This invitation is for a different Discord server.")
        try:
            tokens = await dc.exchange_code(client_id=app["application_id"],
                                            client_secret=S.box.decrypt(app["client_secret_enc"]), code=code,
                                            redirect_uri=redirect_uri(S), transport=S.discord_transport)
            access = (tokens or {}).get("access_token")
            if not isinstance(access, str) or not access:
                raise dc.DiscordError("bad_request")
            guilds = await dc.user_guilds(access, transport=S.discord_transport)
        except dc.DiscordError:
            return page(400, "oauth_failed", "Discord did not confirm the authorisation. Start again.")
        entry = next((g for g in guilds or [] if isinstance(g, dict) and str(g.get("id")) == guild_id), None)
        if entry is None or not dc.can_manage_guild(entry):
            await audit.record(S.store, actor=a.actor, action="discord.install_denied",
                               enterprise_id=a.chain.enterprise_id, target_level="bot_instance",
                               target_id=rec["instance_id"], now=S.clock(), details={"guild_id": guild_id})
            return page(403, "not_server_manager", "You need Manage Server permission in that Discord server.")
        try:
            await rest_for(S, app).guild(guild_id)          # the bot really is in the server
        except dc.DiscordError:
            return page(409, "bot_not_present", "The bot was not added to that server. Start again.")
        if share is not None:
            try:                                                   # one successful use per invitation
                await S.store.update(SHARES, share["id"], {"status": "used", "used_at": S.clock(),
                                                           "used_for_guild": guild_id},
                                     expect_version=share["version"])
            except ConcurrentModification:
                return page(409, "share_link_used", "This invitation link has already been used.")
        await dc.record_installation(S.store, app, guild_id, now=S.clock(), by=user.id)
        current = a.target.channel
        keep = list(current.channel_ids) if current.guild_id == guild_id else []
        try:
            await S.hierarchy.bind_discord_guild(rec["instance_id"], guild_id, application_id=app["application_id"],
                                                 actor_id=user.id, channel_ids=keep)
        except BindingConflict:
            # shared server: wait for a channel selection by an admin of this instance
            until = datetime.fromisoformat(S.clock()) + PENDING_TTL
            doc = {"id": f"pending:{rec['instance_id']}", "kind": "discord_pending",
                   "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id,
                   "bot_id": a.chain.bot.id, "instance_id": rec["instance_id"],
                   "application_id": app["application_id"], "guild_id": guild_id, "verified_by": user.id,
                   "expires_at": until.isoformat(), "expires_at_dt": until, "version": 1}
            await S.store.delete_many(dc.OAUTH_STATES, {"id": doc["id"]})
            await S.store.insert(dc.OAUTH_STATES, doc)
            await audit.record(S.store, actor=a.actor, action="discord.install_pending_channels",
                               enterprise_id=a.chain.enterprise_id, target_level="bot_instance",
                               target_id=rec["instance_id"], now=S.clock(), details={"guild_id": guild_id})
            return page(200, "channel_selection_required",
                        "This server is shared. Choose the channels this instance should use.",
                        instance_id=rec["instance_id"], guild_id=guild_id)
        except HierarchyError:
            return page(409, "not_connectable", "This instance cannot be connected right now.")
        await audit.record(S.store, actor=a.actor, action="discord.connect", enterprise_id=a.chain.enterprise_id,
                           target_level="bot_instance", target_id=rec["instance_id"], now=S.clock(),
                           details={"guild_id": guild_id, "application_id": app["application_id"]})
        return page(200, "connected", "Discord connected.", instance_id=rec["instance_id"], guild_id=guild_id)

    async def pending_for(instance_id: str, enterprise_id: str) -> Optional[dict]:
        doc = await S.store.get(dc.OAUTH_STATES, f"pending:{instance_id}")
        if not doc or doc.get("enterprise_id") != enterprise_id or \
                datetime.fromisoformat(S.clock()) >= datetime.fromisoformat(doc["expires_at"]):
            return None
        return doc

    async def guild_of(a) -> tuple:
        """(guild id, pending record) for channel listing / selection."""
        ch = a.target.channel
        if ch.guild_id:
            return ch.guild_id, None
        pend = await pending_for(a.target.id, a.chain.enterprise_id)
        if pend:
            return pend["guild_id"], pend
        raise HTTPException(409, "This instance is not connected to a Discord server.")

    @router.get("/instances/{instance_id}/channel/discord/channels")
    @translate
    async def list_channels(instance_id: str, user: User = Depends(principal)):
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        app = await usable_app(a.chain)
        guild_id, _ = await guild_of(a)
        try:
            channels = await rest_for(S, app).guild_channels(guild_id)
        except dc.DiscordError as exc:
            raise mapped(exc)
        holders = await BindingRegistry(S.store).holders(f"discord:{app['application_id']}:{guild_id}")
        others = [h for iid, h in holders.items() if iid != instance_id]
        whole_taken = any(WHOLE in (h.get("scopes") or [WHOLE]) for h in others)
        taken = {s for h in others for s in h.get("scopes") or []}
        mine = set(a.target.channel.channel_ids)
        out = []
        for c in channels or []:
            if not isinstance(c, dict) or c.get("type") not in TEXT_CHANNEL_TYPES:
                continue
            cid = str(c.get("id"))
            state = "selected" if cid in mine else ("unavailable" if whole_taken or cid in taken else "available")
            out.append({"id": cid, "name": str(c.get("name", ""))[:100], "state": state})
        return {"guild_id": guild_id, "whole_server_available": not others, "channels": out}

    @router.put("/instances/{instance_id}/channel/discord/settings")
    @translate
    async def put_settings(instance_id: str, body: SettingsBody, user: User = Depends(principal)):
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        current = a.target.channel
        if not current.guild_id:
            pend = await pending_for(instance_id, a.chain.enterprise_id)
            if pend is None:
                await discord_channel_check(S, a.chain, current, changes)
                merged = {**binding_to_doc(current), **changes}
                node = await S.hierarchy.update(Level.BOT_INSTANCE, instance_id,
                                                {"channel": binding_to_doc(binding_from_doc(merged))},
                                                actor_id=user.id)
                return {"channel": binding_to_doc(node.channel)}
            app = await usable_app(a.chain)
            if app["application_id"] != pend["application_id"]:
                raise HTTPException(409, "The Enterprise Discord application changed. Start again.")
            chans = changes.get("channel_ids") or []
            if not chans:
                raise HTTPException(422, "Choose at least one channel; this server is shared.")
            probe = binding_from_doc({**binding_to_doc(current), "guild_id": pend["guild_id"],
                                      "application_id": pend["application_id"]})
            await discord_channel_check(S, a.chain, probe, {"channel_ids": chans})
            rest = {k: v for k, v in changes.items() if k != "channel_ids"}
            if rest:
                merged = binding_from_doc({**binding_to_doc(current), **rest}).validated()
                await S.hierarchy.update(Level.BOT_INSTANCE, instance_id, {"channel": binding_to_doc(merged)},
                                         actor_id=user.id)
            node = await S.hierarchy.bind_discord_guild(instance_id, pend["guild_id"],
                                                        application_id=pend["application_id"], actor_id=user.id,
                                                        channel_ids=chans)
            await S.store.delete_many(dc.OAUTH_STATES, {"id": pend["id"]})
            await audit.record(S.store, actor=a.actor, action="discord.connect", enterprise_id=a.chain.enterprise_id,
                               target_level="bot_instance", target_id=instance_id, now=S.clock(),
                               details={"guild_id": pend["guild_id"], "channels": len(chans)})
            return {"channel": binding_to_doc(node.channel)}
        await discord_channel_check(S, a.chain, current, changes)
        merged = {**binding_to_doc(current), **changes}
        node = await S.hierarchy.update(Level.BOT_INSTANCE, instance_id,
                                        {"channel": binding_to_doc(binding_from_doc(merged))}, actor_id=user.id)
        await audit.record(S.store, actor=a.actor, action="discord.settings", enterprise_id=a.chain.enterprise_id,
                           target_level="bot_instance", target_id=instance_id, now=S.clock(),
                           details={"fields": sorted(changes)})
        return {"channel": binding_to_doc(node.channel)}

    # ── D3: share links and invitation emails (Phase 4D correction C2) ──
    def share_view(doc: dict) -> Dict[str, Any]:
        return {k: doc.get(k) for k in ("id", "status", "expected_guild_id", "created_at", "expires_at", "used_at",
                                        "used_for_guild", "invited")}

    @router.post("/instances/{instance_id}/channel/discord/share-links", status_code=201)
    @translate
    async def create_share_link(instance_id: str, body: ShareBody, user: User = Depends(principal)):
        """A link the instance admin can hand to a Discord server administrator.
        It names only an opaque, revocable, single-use token; the OAuth state is
        created fresh when the link is opened, so no state, token or secret is
        ever in the shared URL."""
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        if not a.chain.availability().available:
            raise HTTPException(409, "This instance is not active.")
        app = await usable_app(a.chain)
        if body.expected_guild_id is not None and not dc.SNOWFLAKE.fullmatch(body.expected_guild_id):
            raise HTTPException(422, "Invalid Discord server id.")
        email = None
        if body.email is not None:
            email = body.email.strip()
            if len(email) > 254 or not re.fullmatch(r"[^@\s<>\"',;]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email):
                raise HTTPException(422, "Invalid email address.")
            if not (await throttle.hit(S.store, throttle.throttle_key("dc-invite", user.id), limit=INVITE_RATE[0],
                                       window_seconds=INVITE_RATE[1], now=S.clock())).allowed:
                raise HTTPException(429, "Too many invitations. Try again later.")
        token = secrets.token_urlsafe(32)
        now = S.clock()
        expires = datetime.fromisoformat(now) + SHARE_TTL
        doc = {"id": str(uuid.uuid4()), "token_hash": _hash(token),
               "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id,
               "bot_id": a.chain.bot.id, "instance_id": instance_id, "application_id": app["application_id"],
               "created_by": user.id, "expected_guild_id": body.expected_guild_id, "status": "active",
               "invited": bool(email), "created_at": now, "expires_at": expires.isoformat(),
               "expires_at_dt": expires + timedelta(days=30), "version": 1}
        await S.store.insert(SHARES, doc)
        url = f"{S.settings.public_api_origin}/api/public/discord/install/{token}"
        sent = None
        if email:
            try:                                    # Phase 6B: a delivery failure never fails the request
                await S.mailer.send_discord_invite(email, url, organisation_name=_plain(a.chain.organisation.name),
                                                   instance_name=_plain(a.target.name), expires_at=expires.isoformat())
                sent = True
            except Exception:                       # never log the message (it carries the install link)
                sent = False
        await audit.record(S.store, actor=a.actor, action="discord.share_link_created",
                           enterprise_id=a.chain.enterprise_id, target_level="bot_instance", target_id=instance_id,
                           now=now, details={"share_id": doc["id"], "invited": bool(email),
                                             "expected_guild_id": body.expected_guild_id})
        return {**share_view(doc), "url": url, **({"email_sent": sent} if email else {})}

    @router.get("/instances/{instance_id}/channel/discord/share-links")
    @translate
    async def list_share_links(instance_id: str, user: User = Depends(principal)):
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        docs = await S.store.find(SHARES, {"enterprise_id": a.chain.enterprise_id, "instance_id": instance_id},
                                  sort="created_at", descending=True, limit=100)
        return {"share_links": [share_view(d) for d in docs]}

    @router.delete("/instances/{instance_id}/channel/discord/share-links/{share_id}")
    @translate
    async def revoke_share_link(instance_id: str, share_id: str, user: User = Depends(principal)):
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        doc = await S.store.get(SHARES, share_id) if is_valid_id(share_id) else None
        if not doc or doc.get("instance_id") != instance_id or doc.get("enterprise_id") != a.chain.enterprise_id:
            raise NotFound("Not found.")
        if doc["status"] == "active":
            await S.store.update(SHARES, share_id, {"status": "revoked", "revoked_at": S.clock()},
                                 expect_version=doc["version"])
        await audit.record(S.store, actor=a.actor, action="discord.share_link_revoked",
                           enterprise_id=a.chain.enterprise_id, target_level="bot_instance", target_id=instance_id,
                           now=S.clock(), details={"share_id": share_id})
        return share_view(await S.store.get(SHARES, share_id))

    @router.get("/public/discord/install/{token}")
    async def open_share_link(token: str):
        """Opened by the invited server administrator (no BitBotPro account).
        The creator's authority, the link and the Enterprise application are
        all re-checked now; then a fresh 10-minute OAuth state is issued."""
        gone = page(410, "share_link_inactive", "This invitation link is no longer valid.")
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return gone
        docs = await S.store.find(SHARES, {"token_hash": _hash(token)}, limit=1)
        share = docs[0] if docs else None
        if not share or share.get("status") != "active" or \
                datetime.fromisoformat(S.clock()) >= datetime.fromisoformat(share["expires_at"]):
            return gone
        try:
            creator = await get_user(S.store, share["created_by"])
            if creator is None or creator.status != "active":
                raise NotFound("gone")
            a = await discord_access(creator, share["instance_id"], Action.MANAGE_CHANNEL)
        except Exception:
            return gone
        if a.chain.enterprise_id != share["enterprise_id"] or not a.chain.availability().available:
            return gone
        app = await dc.get_app(S.store, a.chain.enterprise_id)
        if not app or app.get("status") != "enabled" or app["application_id"] != share["application_id"]:
            return gone
        state = secrets.token_urlsafe(32)
        expires = datetime.fromisoformat(S.clock()) + STATE_TTL
        await S.store.insert(dc.OAUTH_STATES, {
            "id": _hash(state), "kind": "discord_install", "user_id": creator.id, "user_tv": creator.token_version,
            "enterprise_id": a.chain.enterprise_id, "organisation_id": a.chain.organisation.id,
            "bot_id": a.chain.bot.id, "instance_id": share["instance_id"], "application_id": app["application_id"],
            "share_ref": share["id"], "expires_at": expires.isoformat(), "expires_at_dt": expires, "version": 1})
        query = {"client_id": app["application_id"], "scope": dc.OAUTH_SCOPES, "permissions": str(app["permissions"]),
                 "response_type": "code", "redirect_uri": redirect_uri(S), "state": state}
        if share.get("expected_guild_id"):
            query.update(guild_id=share["expected_guild_id"], disable_guild_select="true")
        return RedirectResponse(f"{dc.AUTHORIZE_URL}?{urlencode(query)}", status_code=302,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    # ── D11: staff-handoff administration (Phase 4D correction C3) ──
    def handoff_view(doc: dict) -> Dict[str, Any]:
        until = datetime.fromisoformat(doc["active_until"])
        remaining = max(0, int((until - datetime.fromisoformat(S.clock())).total_seconds()))
        return {"channel_id": doc["channel_id"], "active": remaining > 0, "active_until": doc["active_until"],
                "remaining_seconds": remaining, "source": doc.get("source", "staff_message")}

    @router.get("/instances/{instance_id}/channel/discord/handoffs")
    @translate
    async def list_handoffs(instance_id: str, user: User = Depends(principal)):
        a = await discord_access(user, instance_id, Action.READ)
        docs = await S.store.find("channel_state", {"enterprise_id": a.chain.enterprise_id,
                                                     "instance_id": instance_id, "kind": "discord_handoff"})
        views = [handoff_view(d) for d in docs]
        return {"handoffs": [v for v in views if v["active"]]}

    def handoff_key(instance_id: str, channel_id: str) -> str:
        if not dc.SNOWFLAKE.fullmatch(channel_id or ""):
            raise HTTPException(422, "Invalid Discord channel id.")
        return f"{HANDOFF_PREFIX}:{instance_id}:{channel_id}"

    @router.put("/instances/{instance_id}/channel/discord/handoffs/{channel_id}")
    @translate
    async def set_handoff(instance_id: str, channel_id: str, body: HandoffBody, user: User = Depends(principal)):
        """Pause AI replies in one of this instance's channels (admin control)."""
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        key = handoff_key(instance_id, channel_id)
        ch = a.target.channel
        if not ch.guild_id:
            raise HTTPException(409, "This instance is not connected to a Discord server.")
        if not 1 <= body.minutes <= 1440:
            raise HTTPException(422, "minutes must be between 1 and 1440.")
        if ch.channel_ids:
            if channel_id not in ch.channel_ids:
                raise HTTPException(422, "That channel is not one of this instance's channels.")
        else:
            await discord_channel_check(S, a.chain, ch, {"channel_ids": [channel_id]})   # a real text channel
        until = datetime.fromisoformat(S.clock()) + timedelta(minutes=body.minutes)
        fields = {"active_until": until.isoformat(), "expires_at_dt": until + timedelta(days=1), "source": "admin",
                  "set_by": user.id}
        existing = await S.store.get("channel_state", key)
        if existing is None:
            try:
                await S.store.insert("channel_state", {
                    "id": key, "kind": "discord_handoff", "enterprise_id": a.chain.enterprise_id,
                    "organisation_id": a.chain.organisation.id, "bot_id": a.chain.bot.id,
                    "instance_id": instance_id, "channel_id": channel_id, **fields, "version": 1})
            except DuplicateKey:
                await S.store.update("channel_state", key, fields)
        else:
            await S.store.update("channel_state", key, fields)
        await audit.record(S.store, actor=a.actor, action="discord.handoff_set", enterprise_id=a.chain.enterprise_id,
                           target_level="bot_instance", target_id=instance_id, now=S.clock(),
                           details={"channel_id": channel_id, "minutes": body.minutes})
        return handoff_view(await S.store.get("channel_state", key))

    @router.post("/instances/{instance_id}/channel/discord/handoffs/{channel_id}/resume")
    @translate
    async def resume_handoff(instance_id: str, channel_id: str, user: User = Depends(principal)):
        """Resume AI handling now (same effect as a staff member's ``!bot resume``)."""
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        key = handoff_key(instance_id, channel_id)
        removed = await S.store.delete_many("channel_state", {"id": key, "enterprise_id": a.chain.enterprise_id,
                                                                "instance_id": instance_id})
        await audit.record(S.store, actor=a.actor, action="discord.handoff_resume",
                           enterprise_id=a.chain.enterprise_id, target_level="bot_instance", target_id=instance_id,
                           now=S.clock(), details={"channel_id": channel_id, "was_active": bool(removed)})
        return {"channel_id": channel_id, "active": False}

    @router.put("/instances/{instance_id}/channel/discord/nickname")
    @translate
    async def nickname(instance_id: str, body: NickBody, user: User = Depends(principal)):
        """D15: the nickname is per server. A tenant may change it only where
        its instance is the application's only user of that server."""
        a = await discord_access(user, instance_id, Action.MANAGE_CHANNEL)
        app = await usable_app(a.chain)
        ch = a.target.channel
        if not ch.guild_id:
            raise HTTPException(409, "This instance is not connected to a Discord server.")
        holders = await BindingRegistry(S.store).holders(f"discord:{app['application_id']}:{ch.guild_id}")
        if set(holders) - {instance_id} and not user.is_platform_superadmin:
            raise HTTPException(403, "This server is shared; only a platform administrator can change the nickname.")
        nick = (body.nickname or "").strip() or None
        if nick is not None and (len(nick) > 32 or re.search(r"[@#:`]|discord", nick, re.IGNORECASE)):
            raise HTTPException(422, "Nicknames are 1-32 characters without @ # : ` or the word Discord.")
        try:
            await rest_for(S, app).set_nickname(ch.guild_id, nick)
        except dc.DiscordError as exc:
            raise mapped(exc)
        await audit.record(S.store, actor=a.actor, action="discord.nickname", enterprise_id=a.chain.enterprise_id,
                           target_level="bot_instance", target_id=instance_id, now=S.clock(),
                           details={"guild_id": ch.guild_id, "cleared": nick is None})
        return {"ok": True, "nickname": nick}


# ═════════════════════ generic channel operations (dispatched from api.telegram) ═════════════════════

async def discord_channel_op(S: Services, a, op: str) -> Dict[str, Any]:
    instance_id = a.target.id
    ch = a.target.channel
    app = await dc.get_app(S.store, a.chain.enterprise_id)
    if op == "status":
        gw = await S.store.get(GW_STATUS, a.chain.enterprise_id)
        pend = await S.store.get(dc.OAUTH_STATES, f"pending:{instance_id}")
        return {"type": "discord", "connected": bool(ch.guild_id), "guild_id": ch.guild_id,
                "channel_ids": list(ch.channel_ids), "listen_mode": ch.listen_mode, "passive_mode": ch.passive_mode,
                "dm_enabled": ch.dm_enabled, "application": dc.tenant_view(app),
                "pending_channel_selection": bool(pend), "gateway": {"state": status_view(gw)["state"]}}
    if op == "test":
        if not ch.guild_id:
            raise HTTPException(409, "This instance is not connected to a Discord server.")
        if not app or app.get("status") != "enabled" or app["application_id"] != ch.application_id:
            raise HTTPException(409, "Discord is not available for this Enterprise.")
        try:
            await rest_for(S, app).guild(ch.guild_id)
        except dc.DiscordError as exc:
            raise mapped(exc)
        return {"ok": True}
    if op == "disconnect":
        if not ch.guild_id:
            await S.store.delete_many(dc.OAUTH_STATES, {"id": f"pending:{instance_id}"})
            return {"connected": False, "left_server": False}
        guild_id, application_id = ch.guild_id, ch.application_id
        await S.hierarchy.unbind_discord(instance_id, actor_id=a.actor.user_id)
        left = False
        # best effort, decided now from the live claims: only if nobody else uses the server
        holders = await BindingRegistry(S.store).holders(f"discord:{application_id}:{guild_id}")
        if not holders and app and app.get("application_id") == application_id and app.get("bot_token_enc"):
            try:
                await rest_for(S, app).leave_guild(guild_id)
                left = True
            except dc.DiscordError as exc:
                left = exc.category == "not_found"
            if left:
                await dc.remove_installation(S.store, application_id, guild_id)
        return {"connected": False, "left_server": left}
    raise NotFound("Not found.")


__all__ = ["register", "channel_for", "discord_channel_op", "discord_channel_check", "redirect_uri"]
