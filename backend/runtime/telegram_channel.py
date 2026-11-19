"""Telegram channel service (Phase 4C): webhook state and message handling.

Webhook state lives on the Bot Instance document as ``telegram_webhook``:
``desired`` (admin intent), ``registered`` (state at Telegram), the path and
header secrets (encrypted, for re-registration) with their SHA-256 hashes (for
constant-time verification), ``bot_username``, ``registered_at`` and a
sanitised ``sync_error``.

``sync`` reconciles desired with actual state. The webhook is registered only
while the instance is desired-connected **and** available (every ancestor
active, channel allowed, Telegram credential usable), and removed otherwise.
Lifecycle changes call ``sync``; the worker retries failures.

Updates run through the shared pipeline. The conversation session is the
chat (``telegram:{instance}:{chat}``, group-wide, as today); the per-user rate
limit uses chat and user; the billing key is the update (durable
idempotency, so a redelivered update never bills twice). Replies are
delivered at most once, so a duplicate delivery never repeats a message.
"""
from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core import resolver
from core.credentials import CredentialError, SecretBox, check_use, enterprise_credentials
from core.hierarchy import (COLLECTION, ChannelType, ConcurrentModification, DuplicateKey, HierarchyError,
                            HierarchyService, Level, NotFound)
from runtime import conversations as conv
from runtime import delivery as dlv
from runtime.interfaces import ChannelAddress, InboundMessage
from runtime.pipeline import Pipeline
from runtime.telegram import (FOLLOWUP_HINT, HELP_TEXT, MAX_RETRY_AFTER, TelegramClient, TelegramError, chunk_text,
                              decide, hash_secret, new_webhook_secrets, split_suggestions)
from runtime.usage import StoreUsageTracker

STATE = "channel_state"
SUGGESTION_TTL_HOURS = 24
APOLOGY = "Sorry, something went wrong. Please try again."


def _uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bitbotpro:" + name))


class TelegramChannel:
    def __init__(self, store, *, box: SecretBox, clock: Callable[[], str], public_api_origin: str,
                 transport=None, providers=None, retriever=None, catalog=None, platform_policy=None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        from core.config_registry import PLATFORM_POLICY
        self.store = store
        self.box = box
        self.clock = clock
        self.origin = public_api_origin.rstrip("/")
        self.transport = transport
        self.providers = providers or {}
        self.retriever = retriever
        self.catalog = catalog
        self.platform_policy = platform_policy or PLATFORM_POLICY
        self.sleep = sleep
        self.hierarchy = HierarchyService(store, clock=clock)

    # ── helpers ──
    async def _chain(self, instance_id: str):
        chain = await self.hierarchy.load_chain(Level.BOT_INSTANCE, instance_id)
        if chain.instance.channel.type is not ChannelType.TELEGRAM:
            raise NotFound("Not found.")
        return chain

    async def _token(self, chain) -> str:
        """The instance's bot token, only through the authorised credential check
        (same Enterprise and branch, usage scope, active, telegram provider)."""
        cid = chain.instance.channel.credential_id
        creds = await enterprise_credentials(self.store, chain.enterprise_id)
        eff = resolver.resolve(chain, platform_policy=self.platform_policy, credentials=creds)
        try:
            cred = check_use(creds.get(cid), referencing_chain=chain, slot="channel.telegram", policy=eff.policy)
            return self.box.decrypt(cred.secret_enc)
        except CredentialError:
            raise TelegramError("credential_unavailable")

    def client(self, token: str) -> TelegramClient:
        return TelegramClient(token, transport=self.transport)

    async def _state(self, instance_id: str) -> dict:
        doc = await self.store.get(COLLECTION[Level.BOT_INSTANCE], instance_id)
        return dict((doc or {}).get("telegram_webhook") or {})

    async def _save(self, instance_id: str, fields: Dict[str, Any]) -> dict:
        for _ in range(32):
            doc = await self.store.get(COLLECTION[Level.BOT_INSTANCE], instance_id)
            if doc is None:
                raise NotFound("Not found.")
            state = {**(doc.get("telegram_webhook") or {}), **fields}
            try:
                await self.store.update(COLLECTION[Level.BOT_INSTANCE], instance_id, {"telegram_webhook": state},
                                        expect_version=doc["version"])
                return state
            except ConcurrentModification:
                continue
        raise ConcurrentModification()

    def public_status(self, state: dict) -> Dict[str, Any]:
        return {"connected": bool(state.get("desired")), "registered": bool(state.get("registered")),
                "registered_at": state.get("registered_at"), "bot_username": state.get("bot_username"),
                "sync_error": state.get("sync_error")}

    # ── connection management ──
    async def connect(self, instance_id: str) -> Dict[str, Any]:
        chain = await self._chain(instance_id)
        token = await self._token(chain)
        me = await self.client(token).get_me()
        if str(me["id"]) != str(chain.instance.telegram_bot_id):
            raise TelegramError("bot_mismatch")
        path, header = new_webhook_secrets()
        await self._save(instance_id, {
            "desired": True, "registered": False, "bot_username": me.get("username") or "",
            "path_hash": hash_secret(path), "header_hash": hash_secret(header),
            "path_enc": self.box.encrypt(path), "header_enc": self.box.encrypt(header), "sync_error": None})
        return await self.sync(instance_id)

    async def disconnect(self, instance_id: str) -> Dict[str, Any]:
        await self._chain(instance_id)
        await self._save(instance_id, {"desired": False})
        state = await self.sync(instance_id)
        if not state["registered"]:
            await self._save(instance_id, {"path_hash": None, "header_hash": None, "path_enc": None,
                                           "header_enc": None})
        return self.public_status(await self._state(instance_id))

    async def rotate(self, instance_id: str) -> Dict[str, Any]:
        """New secrets; the old URL and header stop working immediately."""
        await self._chain(instance_id)
        state = await self._state(instance_id)
        if not state.get("desired"):
            raise TelegramError("not_connected")
        path, header = new_webhook_secrets()
        await self._save(instance_id, {"path_hash": hash_secret(path), "header_hash": hash_secret(header),
                                       "path_enc": self.box.encrypt(path), "header_enc": self.box.encrypt(header),
                                       "registered": False})
        return await self.sync(instance_id)

    async def sync(self, instance_id: str) -> Dict[str, Any]:
        """Make Telegram match the desired state (idempotent)."""
        chain = await self._chain(instance_id)
        state = await self._state(instance_id)
        creds = await enterprise_credentials(self.store, chain.enterprise_id)
        eff = resolver.resolve(chain, platform_policy=self.platform_policy, credentials=creds)
        want = bool(state.get("desired")) and chain.availability().available and not eff.runtime_blockers
        try:
            if want and not state.get("registered"):
                token = await self._token(chain)
                url = f"{self.origin}/api/hooks/telegram/{instance_id}/{self.box.decrypt(state['path_enc'])}"
                await self.client(token).set_webhook(url, self.box.decrypt(state["header_enc"]))
                state = await self._save(instance_id, {"registered": True, "registered_at": self.clock(),
                                                       "sync_error": None})
            elif not want and state.get("registered"):
                token = await self._token(chain)
                await self.client(token).delete_webhook()
                state = await self._save(instance_id, {"registered": False, "sync_error": None})
            elif state.get("sync_error"):
                state = await self._save(instance_id, {"sync_error": None})
        except TelegramError as exc:
            state = await self._save(instance_id, {"sync_error": exc.category})
        return self.public_status(state)

    async def test(self, instance_id: str) -> Dict[str, Any]:
        chain = await self._chain(instance_id)
        me = await self.client(await self._token(chain)).get_me()
        return {"ok": str(me["id"]) == str(chain.instance.telegram_bot_id), "bot_username": me.get("username")}

    # ── updates ──
    async def handle_update(self, instance_id: str, update: Dict[str, Any]) -> Optional[str]:
        """Process one authenticated update. Returns a short result label (tests)."""
        try:
            chain = await self._chain(instance_id)
        except HierarchyError:
            return "unknown_instance"
        inst = chain.instance
        state = await self._state(instance_id)
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return "invalid"
        binding = inst.channel
        if "callback_query" in update:
            resolved = await self._suggestion(instance_id, update["callback_query"], binding)
            if resolved is None:
                return "ignored"
            chat_id, from_id, text, reply_to = resolved
            decision_text, help_only = text, False
        elif "message" in update and isinstance(update["message"], dict):
            msg = update["message"]
            d = decide(msg, listen_mode=binding.listen_mode, chat_ids=binding.chat_ids,
                       bot_username=state.get("bot_username") or "", bot_id=inst.telegram_bot_id)
            if not d.respond:
                return "ignored"
            chat_id, from_id = (msg.get("chat") or {}).get("id"), (msg.get("from") or {}).get("id")
            reply_to, decision_text, help_only = msg.get("message_id"), d.text, d.help
        else:
            return "ignored"
        if not chain.availability().available:
            return "unavailable"
        try:
            token = await self._token(chain)
        except TelegramError:
            return "credential_unavailable"
        client = self.client(token)
        if "callback_query" in update:
            try:
                await client.call("answerCallbackQuery", {"callback_query_id": str(update["callback_query"].get("id", ""))})
            except TelegramError:
                pass
        if help_only:
            key = f"tg-help:{instance_id}:{update_id}"
            await dlv.ensure(self.store, chain, key, channel="telegram", target={"chat_id": chat_id, "reply_to": reply_to},
                             parts=[{"text": HELP_TEXT}], now=self.clock())
            await self.deliver(dlv.delivery_id(key))
            return "help"
        session_id = _uid(f"tg:{instance_id}:{chat_id}")
        cmid = _uid(f"tg-update:{instance_id}:{update_id}")
        rid = conv.request_id(session_id, cmid)
        status, record = await conv.begin_request(self.store, chain, session_id, cmid, now=self.clock(),
                                                  channel="telegram")
        if status != "new":
            # Redelivered update: never reprocessed. A pending delivery (e.g. after a crash
            # before sending) may be sent now; sent or uncertain ones are never resent.
            await self.deliver(dlv.delivery_id(rid))
            return f"duplicate:{status}"
        try:
            await client.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass
        result = await self._run(chain, session_id, cmid, rid, decision_text, chat_id, from_id, reply_to)
        if result.get("send"):                    # short notices (apology, limits) also go through a delivery record
            await dlv.ensure(self.store, chain, rid + ":notice", channel="telegram",
                             target={"chat_id": chat_id, "reply_to": reply_to},
                             parts=[{"text": result["send"]}], now=self.clock())
            await self.deliver(dlv.delivery_id(rid + ":notice"))
        elif result.get("deliver"):
            await self.deliver(dlv.delivery_id(rid))
        return result.get("label", "answered")

    async def _run(self, chain, session_id: str, cmid: str, rid: str, text: str, chat_id, from_id,
                   reply_to=None) -> Dict[str, Any]:
        usage = StoreUsageTracker(self.store, platform_policy=self.platform_policy)
        creds = await enterprise_credentials(self.store, chain.enterprise_id)
        eff = resolver.resolve(chain, platform_policy=self.platform_policy, credentials=creds)
        retention = eff.policy.get("retention.conversation_days")
        reply_id = conv.reply_message_id(rid)
        try:
            hist = await conv.history(self.store, chain, session_id, limit=20)
            pipeline = Pipeline(self.store, box=self.box, clock=self.clock, providers=self.providers,
                                retriever=self.retriever, catalog=self.catalog, platform_policy=self.platform_policy)

            def result_for(outcome) -> Dict[str, Any]:
                now = self.clock()
                clean, suggestions = outcome.text, []
                if outcome.kind == "answer" and not outcome.citations:     # never on grounded/strict output
                    clean, suggestions = split_suggestions(outcome.text)
                return {"http_status": 200, "message_id": reply_id, "text": clean, "suggestions": suggestions,
                        "citations": list(outcome.citations), "billed": outcome.billed, "usage": dict(outcome.usage),
                        "messages": [{"id": cmid, "role": "user", "text": text, "at": now},
                                     {"id": reply_id, "role": "assistant", "text": clean, "at": now,
                                      "citations": list(outcome.citations), "kind": outcome.kind}],
                        "delivery": {"channel": "telegram", "target": {"chat_id": chat_id, "reply_to": reply_to},
                                     "text": clean, "suggestions": suggestions}}

            async def on_generated(outcome):
                await conv.mark_generated(self.store, rid, result_for(outcome))

            outcome = await pipeline.handle(InboundMessage(
                ChannelAddress(ChannelType.TELEGRAM, telegram_bot_id=chain.instance.telegram_bot_id,
                               instance_hint=chain.instance.id),
                text, end_user_id=f"{chat_id}:{from_id}", conversation_key=session_id,
                history=tuple((m["role"], m["text"]) for m in hist if m.get("role") in ("user", "assistant")),
                format_hint=FOLLOWUP_HINT), billing_key=rid, on_generated=on_generated)
            if outcome.kind == "uncertain":
                await conv.mark_uncertain(self.store, rid)
                return {"send": APOLOGY, "label": "uncertain"}
            if outcome.kind == "skipped":
                await conv.finish_request(self.store, session_id, cmid, {"http_status": 204})
                return {"label": "skipped"}
            if not outcome.deliver:
                await conv.finish_request(self.store, session_id, cmid, {"http_status": 404})
                return {"label": "silent"}
            if outcome.kind in ("rate_limited", "quota_exceeded"):
                await conv.finish_request(self.store, session_id, cmid, {"http_status": 429})
                return {"send": outcome.text, "label": outcome.kind}
            if outcome.kind == "busy" or outcome.admin_issue == "provider_error":
                await conv.abandon_request(self.store, session_id, cmid)
                return {"send": APOLOGY, "label": "provider_failure"}
            if outcome.billed:
                record = await self.store.get(conv.REQUESTS, rid)
            else:
                await conv.mark_generated(self.store, rid, result_for(outcome))   # same durable path, unbilled
                record = await self.store.get(conv.REQUESTS, rid)
            await conv.complete_request(self.store, usage, chain, record, now=self.clock(), retention_days=retention,
                                        delivery_builder=self.delivery_parts)
            return {"deliver": True, "label": outcome.kind}
        except Exception:
            doc = await self.store.get(conv.REQUESTS, rid)
            if not (doc and doc.get("status") == "generated"):
                if await usage.ledger_state(rid) in (None, "released", "rejected"):
                    await conv.abandon_request(self.store, session_id, cmid)
                else:
                    await conv.mark_uncertain(self.store, rid)
            return {"send": APOLOGY, "label": "error"}

    # ── delivery (T-A: durable, see runtime.delivery) ──
    async def delivery_parts(self, chain, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Chunks (≤4096) with the reply on the first and suggestion buttons on the last."""
        markup = None
        if spec.get("suggestions"):
            buttons = []
            for s in spec["suggestions"]:
                ref = secrets.token_urlsafe(16)
                expires = datetime.fromisoformat(self.clock()) + timedelta(hours=SUGGESTION_TTL_HOURS)
                await self.store.insert(STATE, {
                    "id": f"tg-sugg:{ref}", "kind": "telegram_suggestion", "enterprise_id": chain.enterprise_id,
                    "organisation_id": chain.organisation.id, "bot_id": chain.bot.id, "instance_id": chain.instance.id,
                    "chat_id": str(spec["target"]["chat_id"]), "text": s, "expires_at": expires.isoformat(),
                    "expires_at_dt": expires, "version": 1})
                buttons.append([{"text": s, "callback_data": f"s:{ref}"}])     # ≤ 64 bytes, opaque
            markup = {"inline_keyboard": buttons}
        chunks = chunk_text(spec.get("text") or "") or [""]
        return [{"text": c, "extra": {"reply_markup": markup} if markup and i == len(chunks) - 1 else {}}
                for i, c in enumerate(chunks)]

    async def deliver(self, did: str) -> Optional[dict]:
        return await dlv.run(self.store, did, self.send_part, now=self.clock())

    async def send_part(self, doc: Dict[str, Any], part: Dict[str, Any]) -> "dlv.SendResult":
        """Telegram sender. Only proven non-delivery is retryable; a timeout or
        5xx after the request was sent is ``uncertain`` (never resent)."""
        try:
            chain = await self._chain(doc["instance_id"])
            if chain.enterprise_id != doc["enterprise_id"]:
                return dlv.SendResult("not_sent_final", error="ownership")
            if not chain.availability().available:          # Phase 6A (H): suspended/archived/deleting
                return dlv.SendResult("not_sent_final", error="unavailable")
            token = await self._token(chain)
        except (HierarchyError, TelegramError):
            return dlv.SendResult("not_sent", error="credential_unavailable")
        client = self.client(token)
        payload: Dict[str, Any] = {"chat_id": doc["target"]["chat_id"], "text": part["text"], "parse_mode": "Markdown"}
        if part["index"] == 0 and doc["target"].get("reply_to"):
            payload["reply_to_message_id"] = doc["target"]["reply_to"]
        payload.update(part.get("extra") or {})
        slept = False
        while True:
            try:
                result = await client.call("sendMessage", payload)
                mid = result.get("message_id") if isinstance(result, dict) else None
                return dlv.SendResult("delivered", message_id=str(mid) if mid is not None else None)
            except TelegramError as exc:
                if exc.parse_error and "parse_mode" in payload:
                    payload = {k: v for k, v in payload.items() if k != "parse_mode"}   # T6: plain-text retry
                    continue
                if exc.category == "rate_limited":
                    if exc.retry_after <= MAX_RETRY_AFTER and not slept:
                        slept = True
                        await self.sleep(exc.retry_after)                              # T9: honour once
                        continue
                    return dlv.SendResult("not_sent", retry_after=exc.retry_after, error="rate_limited")
                if exc.category == "connect_failed":
                    return dlv.SendResult("not_sent", error="connect_failed")
                if exc.category in ("bad_request", "forbidden", "unauthorized", "invalid_token"):
                    return dlv.SendResult("not_sent_final", error=exc.category)
                return dlv.SendResult("uncertain", error=exc.category)

    async def _suggestion(self, instance_id: str, cq: Dict[str, Any], binding):
        data = cq.get("data")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not isinstance(data, str) or not data.startswith("s:") or len(data) > 64 or chat_id is None:
            return None
        doc = await self.store.get(STATE, f"tg-sugg:{data[2:]}")
        if not doc or doc.get("instance_id") != instance_id or doc.get("chat_id") != str(chat_id):
            return None                                         # another bot's/chat's button: ignore
        if binding.chat_ids and str(chat_id) not in set(binding.chat_ids):
            return None
        return chat_id, (cq.get("from") or {}).get("id"), doc["text"], msg.get("message_id")


__all__ = ["TelegramChannel", "STATE"]
