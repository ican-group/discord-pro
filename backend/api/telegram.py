"""Telegram channel routes (Phase 4C; parity T1–T12).

* ``POST /hooks/telegram/{instance_id}/{secret}``: the webhook. Authenticated
  *before* any expensive work (size check, then one document read and two
  constant-time secret checks: the path secret and the required
  ``X-Telegram-Bot-Api-Secret-Token`` header), then rate-limited, then
  deduplicated (durable request idempotency per ``update_id``) and processed
  after the 200 response. The instance comes only from the authenticated path
  and its stored binding; nothing in the body (such as a bot id) is trusted
  for ownership.
* ``POST /instances/{id}/channel/{connect,disconnect,rotate-secret,test}``,
  ``GET /instances/{id}/channel/status``: Telegram instances (MANAGE_CHANNEL
  for changes, READ for status).
* ``POST /credentials/{id}/test``: ``getMe`` for Telegram bot credentials.
"""
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from api.common import Services, load_access, translate
from core import audit, throttle
from core.authz import Action
from core.credentials import CredentialStatus, Provider, check_manage, load_credential
from core.hierarchy import COLLECTION, ChannelType, Level, NotFound, is_valid_id
from core.identity import User
from runtime.telegram import TelegramClient, TelegramError, secret_matches

MAX_BODY = 256 * 1024
HOOK_RATE = (300, 60)
CRED_TEST_RATE = (30, 3600)                  # Phase 6A F4: credential tests per user per hour
HEADER = "x-telegram-bot-api-secret-token"


def channel_for(S: Services):
    from runtime.telegram_channel import TelegramChannel
    return TelegramChannel(S.store, box=S.box, clock=S.clock, public_api_origin=S.settings.public_api_origin,
                           transport=S.telegram_transport, providers=S.providers, retriever=S.retriever,
                           catalog=S.catalog, platform_policy=S.platform_policy,
                           **({"sleep": S.telegram_sleep} if S.telegram_sleep else {}))


async def sync_subtree(S: Services, level: Level, object_id: str, enterprise_id: str) -> None:
    """Lifecycle hook: reconcile every connected Telegram webhook below an object
    (suspend/archive ⇒ removed, resume/unarchive ⇒ registered). Best effort;
    failures are recorded on the instance and retried by the worker."""
    field = {Level.ENTERPRISE: "enterprise_id", Level.ORGANISATION: "organisation_id", Level.BOT: "bot_id",
             Level.BOT_INSTANCE: "id"}[level]
    docs = await S.store.find(COLLECTION[Level.BOT_INSTANCE], {"enterprise_id": enterprise_id, field: object_id,
                                                               "channel.type": "telegram"})
    channel = channel_for(S)
    for doc in docs:
        state = doc.get("telegram_webhook") or {}
        if state.get("desired") or state.get("registered"):
            try:
                await channel.sync(doc["id"])
            except Exception:
                pass


def register(router: APIRouter, S: Services, principal) -> None:
    @router.post("/hooks/telegram/{instance_id}/{secret}")
    async def telegram_hook(instance_id: str, secret: str, request: Request, background: BackgroundTasks):
        nf = JSONResponse({"detail": "Not found."}, status_code=404)
        try:
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return JSONResponse({"detail": "Payload too large."}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Bad request."}, status_code=400)
        if not is_valid_id(instance_id):
            return nf
        doc = await S.store.get(COLLECTION[Level.BOT_INSTANCE], instance_id)
        state = (doc or {}).get("telegram_webhook") or {}
        if not doc or (doc.get("channel") or {}).get("type") != "telegram" or not state.get("desired") \
                or not secret_matches(secret, state.get("path_hash")) \
                or not secret_matches(request.headers.get(HEADER), state.get("header_hash")):
            return nf                                        # unauthenticated: no further work
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY:
                return JSONResponse({"detail": "Payload too large."}, status_code=413)
        key = throttle.throttle_key("tg-hook", instance_id)
        if not (await throttle.hit(S.store, key, limit=HOOK_RATE[0], window_seconds=HOOK_RATE[1],
                                   now=S.clock())).allowed:
            return JSONResponse({"detail": "Too many requests."}, status_code=429)
        try:
            update = json.loads(bytes(body))
        except ValueError:
            return JSONResponse({"detail": "Bad request."}, status_code=400)
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            return JSONResponse({"detail": "Bad request."}, status_code=400)
        background.add_task(channel_for(S).handle_update, instance_id, update)
        return {"ok": True}

    async def telegram_access(user: User, instance_id: str, action: Action):
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id, action)
        if a.target.channel.type is not ChannelType.TELEGRAM:
            raise NotFound("Not found.")
        return a

    def mapped(exc: TelegramError) -> HTTPException:
        messages = {"credential_unavailable": "The Telegram bot token is not available for this instance.",
                    "unauthorized": "Telegram rejected the bot token.",
                    "bot_mismatch": "The token belongs to a different Telegram bot.",
                    "not_connected": "The Telegram channel is not connected.",
                    "rate_limited": "Telegram is rate limiting requests. Try again shortly."}
        return HTTPException(422 if exc.category != "rate_limited" else 429,
                             messages.get(exc.category, "Telegram is not reachable right now."))

    async def act(user: User, instance_id: str, op: str):
        action = Action.MANAGE_CHANNEL if op != "status" else Action.READ
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id, action)
        if a.target.channel.type is ChannelType.DISCORD and op in ("disconnect", "test", "status"):
            from api.discord import discord_channel_op         # Phase 4D: same routes, Discord semantics
            result = await discord_channel_op(S, a, op)
            if op != "status":
                await audit.record(S.store, actor=a.actor, action=f"discord.{op}", enterprise_id=a.chain.enterprise_id,
                                   target_level="bot_instance", target_id=instance_id, now=S.clock(),
                                   details={k: v for k, v in result.items() if k in ("ok", "left_server")})
            return result
        a = await telegram_access(user, instance_id, action)
        channel = channel_for(S)
        try:
            if op == "connect":
                result = await channel.connect(instance_id)
            elif op == "disconnect":
                result = await channel.disconnect(instance_id)
            elif op == "rotate":
                result = await channel.rotate(instance_id)
            elif op == "test":
                result = await channel.test(instance_id)
            else:
                result = channel.public_status(await channel._state(instance_id))
        except TelegramError as exc:
            raise mapped(exc)
        if op != "status":
            await audit.record(S.store, actor=a.actor, action=f"telegram.{op}", enterprise_id=a.chain.enterprise_id,
                               target_level="bot_instance", target_id=instance_id, now=S.clock(),
                               details={k: v for k, v in result.items() if k in ("registered", "ok", "sync_error")})
        return result

    @router.post("/instances/{instance_id}/channel/connect")
    @translate
    async def connect(instance_id: str, user: User = Depends(principal)):
        return await act(user, instance_id, "connect")

    @router.post("/instances/{instance_id}/channel/disconnect")
    @translate
    async def disconnect(instance_id: str, user: User = Depends(principal)):
        return await act(user, instance_id, "disconnect")

    @router.post("/instances/{instance_id}/channel/rotate-secret")
    @translate
    async def rotate(instance_id: str, user: User = Depends(principal)):
        return await act(user, instance_id, "rotate")

    @router.post("/instances/{instance_id}/channel/test")
    @translate
    async def test(instance_id: str, user: User = Depends(principal)):
        return await act(user, instance_id, "test")

    @router.get("/instances/{instance_id}/channel/status")
    @translate
    async def status(instance_id: str, user: User = Depends(principal)):
        return await act(user, instance_id, "status")

    @router.get("/instances/{instance_id}/deliveries")
    @translate
    async def list_deliveries(instance_id: str, status: str = "", user: User = Depends(principal)):
        """Operator visibility for outbound replies (T-A): uncertain or failed
        deliveries are listed for admins; message text is not included."""
        from runtime import delivery as dlv
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id, Action.MANAGE_CHANNEL)
        flt = {"enterprise_id": a.chain.enterprise_id, "instance_id": instance_id}
        if status:
            if status not in ("pending", "sending", "retry", "delivered", "failed_before_send", "uncertain",
                              "resolved"):
                raise HTTPException(422, "Unknown status.")
            flt["status"] = status
        docs = await S.store.find(dlv.COLLECTION, flt, sort="created_at", descending=True, limit=200)
        return {"deliveries": [dlv.public_view(d) for d in docs]}

    @router.post("/instances/{instance_id}/deliveries/{delivery_id}/resolve")
    @translate
    async def resolve_delivery(instance_id: str, delivery_id: str, body: dict, user: User = Depends(principal)):
        """An admin records that an ``uncertain`` delivery was reviewed (it is never
        resent); afterwards it follows the terminal retention policy."""
        from runtime import delivery as dlv
        a = await load_access(S, user, Level.BOT_INSTANCE, instance_id, Action.MANAGE_CHANNEL)
        note = body.get("note") if isinstance(body, dict) else None
        if not isinstance(note, str) or not note.strip():
            raise HTTPException(422, "A note (1-500 characters) is required.")
        doc = await dlv.resolve(S.store, delivery_id, instance_id=instance_id, enterprise_id=a.chain.enterprise_id,
                                note=note.strip(), actor_id=user.id, now=S.clock())
        await audit.record(S.store, actor=a.actor, action="delivery.resolve", enterprise_id=a.chain.enterprise_id,
                           target_level="bot_instance", target_id=instance_id, now=S.clock(),
                           details={"delivery_id": delivery_id})
        return dlv.public_view(doc)

    @router.post("/credentials/{credential_id}/test")
    @translate
    async def test_credential(credential_id: str, user: User = Depends(principal)):
        loaded = await load_credential(S.store, credential_id)
        if loaded is None:
            raise NotFound("Not found.")
        cred, _ = loaded
        level = Level.ORGANISATION if cred.organisation_id else Level.ENTERPRISE
        a = await load_access(S, user, level, cred.organisation_id or cred.enterprise_id, None)
        if a.chain.enterprise_id != cred.enterprise_id:
            raise NotFound("Not found.")
        check_manage(a.actor, a.chain, a.roster.members, a.policy)
        if cred.status is not CredentialStatus.ACTIVE:
            raise HTTPException(422, "The credential has been revoked.")
        d = await throttle.hit(S.store, throttle.throttle_key("cred-test", user.id), limit=CRED_TEST_RATE[0],
                               window_seconds=CRED_TEST_RATE[1], now=S.clock())
        if not d.allowed:
            raise HTTPException(429, "Too many tests. Try again later.", headers={"Retry-After": str(d.retry_after)})
        await audit.record(S.store, actor=a.actor, action="credential.test", enterprise_id=cred.enterprise_id,
                           target_level="credential", target_id=cred.id, now=S.clock(),
                           details={"provider": cred.provider.value})
        if cred.provider in (Provider.ANTHROPIC, Provider.OPENAI):
            # Phase 6A F4: fixed provider host, read-only (list models), sanitised category only
            provider = S.providers.get(cred.provider.value)
            if provider is None or not hasattr(provider, "check"):
                raise HTTPException(422, "Testing this credential type is not available.")
            r = await provider.check(S.box.decrypt(cred.secret_enc))
            return {"ok": bool(r.get("ok")), "category": r.get("category")}
        if cred.provider in (Provider.ZENDESK_API_TOKEN, Provider.ZENDESK_OAUTH):
            # through the guarded Zendesk client (outbound_guard: fixed *.zendesk.com host); read-only users/me
            from runtime.zendesk import ZendeskError, client_for
            doc = await S.store.get("credentials", cred.id)
            try:
                me = await client_for(doc, S.box, transport=S.zendesk_transport, resolver=S.zendesk_resolver).me()
            except ZendeskError as exc:
                return {"ok": False, "category": getattr(exc, "category", None) or "unavailable"}
            return {"ok": True, "category": None, "zendesk_role": str((me or {}).get("role") or "")[:20]}
        if cred.provider is not Provider.TELEGRAM_BOT:
            raise HTTPException(422, "Testing this credential type is not available.")
        try:
            me = await TelegramClient(S.box.decrypt(cred.secret_enc), transport=S.telegram_transport).get_me()
        except TelegramError as exc:
            raise mapped(exc)
        ok = str(me["id"]) == cred.metadata.get("telegram_bot_id")
        return {"ok": ok, "bot_username": me.get("username")}
