"""Discord channel service (Phase 4D; parity D4–D12, D-Q5, D-Q14).

Events arrive from an Enterprise application's gateway connection
(``runtime.discord_gateway``). The application that received an event is
authoritative: routing uses that application's claims only, and the routed
instance must belong to the same Enterprise as the application, so an event
of Enterprise A's application can never reach Enterprise B.

Message handling:
* bots (including itself) are ignored; duplicates (gateway resumes/replays)
  are suppressed by durable request idempotency on the Discord message id;
* **staff handoff (D11, legacy bug fixed).** Staff are identified from
  Discord's own data: the member's role ids in the gateway event, matched
  against the guild's role list fetched with the bot token. The configured
  role *name* is only the lookup key; message text never grants authority.
  A staff message starts or refreshes a handoff for that instance and channel
  (durable, in ``channel_state``). While it is active the AI stays silent for
  everyone. The first message after the cooldown ends it, posts the follow-up
  message and is answered. ``!bot resume`` and ``!bot status`` are
  staff-only;
* listen modes (D6) inside the instance's claimed scope;
* **passive mode (D12):** unaddressed messages are answered only when the
  model (or, in strict mode, the evidence check) is confident; otherwise
  silence. A refusal, limit or error before any AI call is silent and
  unbilled; a generated ``[SKIP]`` is billed once and never posted;
* replies use durable delivery (``runtime.delivery``) with Discord's
  ``nonce`` + ``enforce_nonce``, so a send that timed out may be retried
  safely inside the nonce window; outside it the outcome is ``uncertain``.
  Mentions are never parsed from model output (no @everyone/@here/role
  pings).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core import resolver
from core.credentials import SecretBox, enterprise_credentials
from core.hierarchy import COLLECTION, ChannelType, HierarchyError, HierarchyService, Level
from runtime import conversations as conv
from runtime import delivery as dlv
from runtime.discord import (SAFE_MENTIONS, DiscordError, DiscordRest, get_app, decide, record_installation,
                             remove_installation, split_message, staff_role_ids)
from runtime.errors import RuntimeFailure
from runtime.interfaces import ChannelAddress, InboundMessage
from runtime.pipeline import Pipeline
from runtime.routing import resolve_instance
from runtime.usage import StoreUsageTracker

STATE = "channel_state"
APOLOGY = "Sorry, something went wrong. Please try again."
NONCE_WINDOW_SECONDS = 120          # Discord deduplicates enforced nonces for a few minutes
ROLE_CACHE_SECONDS = 300
PASSIVE_HINT = (
    "PASSIVE MODE: You are monitoring a busy channel. Do NOT respond to small talk, statements, greetings, opinions "
    "or off-topic chatter. Only answer when the message is clearly a question or request for help AND you can give "
    "a useful, accurate answer. Otherwise output exactly [SKIP] and nothing else. Never mention passive mode.")


def _uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bitbotpro:" + name))


class DiscordChannel:
    def __init__(self, store, *, box: SecretBox, clock, transport=None, providers=None, retriever=None, catalog=None,
                 platform_policy=None):
        from core.config_registry import PLATFORM_POLICY
        self.store = store
        self.box = box
        self.clock = clock
        self.transport = transport
        self.providers = providers or {}
        self.retriever = retriever
        self.catalog = catalog
        self.platform_policy = platform_policy or PLATFORM_POLICY
        self.hierarchy = HierarchyService(store, clock=clock)
        self._roles: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def rest(self, app: dict) -> DiscordRest:
        return DiscordRest(self.box.decrypt(app["bot_token_enc"]), transport=self.transport)

    # ── events ──
    async def handle_event(self, app: dict, event: Dict[str, Any]) -> str:
        if not app or app.get("status") != "enabled":
            return "application_disabled"
        kind, data = event.get("t"), event.get("d") or {}
        if kind == "GUILD_CREATE" and data.get("id"):
            await record_installation(self.store, app, str(data["id"]), now=self.clock(), by="gateway")
            return "installed"
        if kind == "GUILD_DELETE" and data.get("id"):
            await remove_installation(self.store, app["application_id"], str(data["id"]))
            return "uninstalled"
        if kind != "MESSAGE_CREATE":
            return "ignored"
        return await self._message(app, data)

    async def _message(self, app: dict, d: Dict[str, Any]) -> str:
        author = d.get("author") or {}
        if author.get("bot") or str(author.get("id")) == str(app.get("bot_user_id")):
            return "ignored_bot"
        author_id, channel_id, msg_id = str(author.get("id", "")), str(d.get("channel_id", "")), str(d.get("id", ""))
        guild_id = str(d["guild_id"]) if d.get("guild_id") else None
        rest = self.rest(app)
        if guild_id:
            address = ChannelAddress(ChannelType.DISCORD, application_id=app["application_id"], guild_id=guild_id,
                                     channel_id=channel_id)
        else:
            address = ChannelAddress(ChannelType.DISCORD, application_id=app["application_id"], is_dm=True,
                                     dm_shared_guilds=tuple(await self._dm_guilds(app, rest, author_id)))
        try:
            iid = await resolve_instance(self.store, address)
            chain = await self.hierarchy.load_chain(Level.BOT_INSTANCE, iid)
        except (RuntimeFailure, HierarchyError) as exc:
            return getattr(exc, "category", "no_route")
        if chain.enterprise_id != app["enterprise_id"]:
            return "foreign_enterprise"                     # cross-Enterprise isolation (never expected)
        if not chain.availability().available:
            return "unavailable"
        binding = chain.instance.channel
        if guild_id:
            handled = await self._staff_and_handoff(app, rest, chain, d, guild_id, channel_id, msg_id)
            if handled:
                return handled
        decision = decide(d, listen_mode=binding.listen_mode, channel_ids=binding.channel_ids,
                          mention_overrides=binding.mention_overrides_channels, bot_user_id=str(app.get("bot_user_id")),
                          is_dm=not guild_id)
        if not decision.respond:
            return "not_addressed"
        passive = bool(binding.passive_mode and guild_id and not decision.mentioned)
        session_id = _uid(f"dc:{iid}:{channel_id}:{author_id}")
        cmid = _uid(f"dc-msg:{app['application_id']}:{msg_id}")
        rid = conv.request_id(session_id, cmid)
        status, _ = await conv.begin_request(self.store, chain, session_id, cmid, now=self.clock(), channel="discord")
        if status != "new":
            await self.deliver(dlv.delivery_id(rid))
            return f"duplicate:{status}"
        if not passive:
            try:
                await rest.typing(channel_id)
            except DiscordError:
                pass
        target = {"channel_id": channel_id, "reply_to": msg_id,
                  "mention": author_id if binding.reply_style == "with_mention" else None}
        label = await self._run(chain, address, session_id, cmid, rid, decision.text, author_id, target, passive)
        return label

    async def _dm_guilds(self, app: dict, rest: DiscordRest, author_id: str) -> List[str]:
        """Guilds of this application's DM-enabled instances the author shares (D-Q5)."""
        docs = await self.store.find(COLLECTION[Level.BOT_INSTANCE],
                                     {"enterprise_id": app["enterprise_id"], "channel.application_id": app["application_id"],
                                      "channel.dm_enabled": True})
        shared = []
        for g in sorted({(doc.get("channel") or {}).get("guild_id") for doc in docs} - {None}):
            try:
                await rest.guild_member(g, author_id)
                shared.append(g)
            except DiscordError:
                continue
        return shared

    async def _roles_for(self, rest: DiscordRest, app_id: str, guild_id: str) -> List[Dict[str, Any]]:
        key = f"{app_id}:{guild_id}"
        hit = self._roles.get(key)
        if hit and time.monotonic() - hit[0] < ROLE_CACHE_SECONDS:
            return hit[1]
        roles = await rest.guild_roles(guild_id)
        self._roles[key] = (time.monotonic(), roles if isinstance(roles, list) else [])
        return self._roles[key][1]

    async def is_staff(self, rest: DiscordRest, app_id: str, guild_id: str, member: Optional[Dict[str, Any]],
                       role_name: str) -> bool:
        if not role_name or not isinstance(member, dict):
            return False
        try:
            wanted = set(staff_role_ids(await self._roles_for(rest, app_id, guild_id), role_name))
        except DiscordError:
            return False                                         # cannot verify: not staff (fail closed)
        return bool(wanted & {str(r) for r in member.get("roles") or []})

    async def _staff_and_handoff(self, app, rest, chain, d, guild_id: str, channel_id: str, msg_id: str) -> Optional[str]:
        binding = chain.instance.channel
        text = (d.get("content") or "").strip().lower()
        staff = await self.is_staff(rest, app["application_id"], guild_id, d.get("member"), binding.staff_role_name)
        hkey = f"dc-handoff:{chain.instance.id}:{channel_id}"
        now = datetime.fromisoformat(self.clock())
        handoff = await self.store.get(STATE, hkey)
        if text in ("!bot resume", "!bot status"):
            if not staff:
                return "command_denied"                          # staff-only commands
            if text == "!bot resume":
                if handoff:
                    await self.store.delete(STATE, hkey)
                notice = "I'm back! How can I help?"
            else:
                active = bool(handoff and now < datetime.fromisoformat(handoff["active_until"]))
                remaining = (datetime.fromisoformat(handoff["active_until"]) - now).total_seconds() / 60 if active else 0
                notice = (f"**Bot Status — silent (handoff active)**\nCooldown remaining: ~{remaining:.1f} min\n"
                          f"Listen mode: `{binding.listen_mode}` · Passive mode: `{binding.passive_mode}`\n"
                          "Type `!bot resume` to clear handoff immediately." if active else
                          f"**Bot Status — active**\nListen mode: `{binding.listen_mode}` · "
                          f"Passive mode: `{binding.passive_mode}`\nStaff role: `{binding.staff_role_name or 'not configured'}`")
            await self._notice(chain, f"dc-cmd:{app['application_id']}:{msg_id}", channel_id, notice, msg_id)
            return "command"
        cooldown = int(binding.handoff_cooldown_minutes or 0)
        if staff and cooldown > 0:
            until = now + timedelta(minutes=cooldown)
            fields = {"active_until": until.isoformat(), "expires_at_dt": until + timedelta(days=1),
                      "last_staff_message": msg_id}
            if handoff is None:
                try:
                    await self.store.insert(STATE, {"id": hkey, "kind": "discord_handoff",
                                                    "enterprise_id": chain.enterprise_id,
                                                    "organisation_id": chain.organisation.id, "bot_id": chain.bot.id,
                                                    "instance_id": chain.instance.id, "channel_id": channel_id,
                                                    **fields, "version": 1})
                except Exception:
                    await self.store.update(STATE, hkey, fields)
            else:
                await self.store.update(STATE, hkey, fields)
            return "handoff_staff"
        if handoff:
            if now < datetime.fromisoformat(handoff["active_until"]):
                return "handoff_silent"                          # AI suppressed during handoff
            await self.store.delete(STATE, hkey)                  # cooldown over: follow-up, then answer
            await self._notice(chain, f"dc-follow:{app['application_id']}:{msg_id}", channel_id,
                               binding.handoff_followup_message, None)
        return None

    async def _knowledge_handoff(self, chain, channel_id: str) -> None:
        """D9: knowledge was insufficient and human handoff is enabled. Only where a
        human channel exists (a staff role and a cooldown are configured) does the
        AI pause in this channel for staff; otherwise the configured fallback was
        the whole response (no invented support channel)."""
        binding = chain.instance.channel
        if not binding.staff_role_name or int(binding.handoff_cooldown_minutes or 0) <= 0:
            return
        key = f"dc-handoff:{chain.instance.id}:{channel_id}"
        until = datetime.fromisoformat(self.clock()) + timedelta(minutes=int(binding.handoff_cooldown_minutes))
        fields = {"active_until": until.isoformat(), "expires_at_dt": until + timedelta(days=1),
                  "source": "knowledge_insufficient"}
        if await self.store.get(STATE, key) is None:
            try:
                await self.store.insert(STATE, {"id": key, "kind": "discord_handoff", "enterprise_id": chain.enterprise_id,
                                                "organisation_id": chain.organisation.id, "bot_id": chain.bot.id,
                                                "instance_id": chain.instance.id, "channel_id": channel_id, **fields,
                                                "version": 1})
                return
            except Exception:
                pass
        await self.store.update(STATE, key, fields)

    async def _notice(self, chain, key: str, channel_id: str, text: str, reply_to: Optional[str]) -> None:
        if not text:
            return
        await dlv.ensure(self.store, chain, key, channel="discord",
                         target={"channel_id": channel_id, "reply_to": reply_to, "mention": None},
                         parts=[{"text": p} for p in split_message(text)], now=self.clock())
        await self.deliver(dlv.delivery_id(key))

    async def _run(self, chain, address, session_id, cmid, rid, text, author_id, target, passive) -> str:
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
                if outcome.kind == "skipped":
                    return {"http_status": 204, "billed": outcome.billed, "usage": dict(outcome.usage), "messages": []}
                return {"http_status": 200, "message_id": reply_id, "text": outcome.text,
                        "citations": list(outcome.citations), "billed": outcome.billed, "usage": dict(outcome.usage),
                        "messages": [{"id": cmid, "role": "user", "text": text, "at": now},
                                     {"id": reply_id, "role": "assistant", "text": outcome.text, "at": now,
                                      "citations": list(outcome.citations), "kind": outcome.kind}],
                        "delivery": {"channel": "discord", "target": target, "text": outcome.text}}

            async def on_generated(outcome):
                await conv.mark_generated(self.store, rid, result_for(outcome))

            outcome = await pipeline.handle(InboundMessage(
                address, text, end_user_id=author_id, conversation_key=session_id,
                history=tuple((m["role"], m["text"]) for m in hist if m.get("role") in ("user", "assistant")),
                format_hint=PASSIVE_HINT if passive else "", passive=passive), billing_key=rid, on_generated=on_generated)
            if outcome.kind == "uncertain":
                await conv.mark_uncertain(self.store, rid)
                if not passive:
                    await self._notice(chain, rid + ":notice", target["channel_id"], APOLOGY, target["reply_to"])
                return "uncertain"
            if outcome.kind == "skipped" and not outcome.billed:
                await conv.finish_request(self.store, session_id, cmid, {"http_status": 204})
                await usage.record(chain, "skipped", now=self.clock(), details={"reason": "passive"})
                return "skipped"
            if not outcome.deliver and outcome.kind != "skipped":
                await conv.finish_request(self.store, session_id, cmid, {"http_status": 404})
                return "silent"
            if outcome.kind in ("rate_limited", "quota_exceeded"):
                await conv.finish_request(self.store, session_id, cmid, {"http_status": 429})
                await self._notice(chain, rid + ":notice", target["channel_id"], outcome.text, target["reply_to"])
                return outcome.kind
            if outcome.kind == "busy" or outcome.admin_issue == "provider_error":
                await conv.abandon_request(self.store, session_id, cmid)
                await self._notice(chain, rid + ":notice", target["channel_id"], APOLOGY, target["reply_to"])
                return "provider_failure"
            if not outcome.billed:
                await conv.mark_generated(self.store, rid, result_for(outcome))
            record = await self.store.get(conv.REQUESTS, rid)
            await conv.complete_request(self.store, usage, chain, record, now=self.clock(), retention_days=retention,
                                        delivery_builder=self.delivery_parts)
            if outcome.kind == "skipped":
                await usage.record(chain, "skipped", now=self.clock(), details={"reason": "passive_generated"})
                return "skipped"
            await self.deliver(dlv.delivery_id(rid))
            if outcome.handoff:
                await self._knowledge_handoff(chain, target["channel_id"])
            return outcome.kind
        except Exception:
            doc = await self.store.get(conv.REQUESTS, rid)
            if not (doc and doc.get("status") == "generated"):
                if await usage.ledger_state(rid) in (None, "released", "rejected"):
                    await conv.abandon_request(self.store, session_id, cmid)
                else:
                    await conv.mark_uncertain(self.store, rid)
            return "error"

    # ── delivery ──
    async def delivery_parts(self, chain, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        text = spec.get("text") or ""
        mention = (spec.get("target") or {}).get("mention")
        if mention:
            text = f"<@{mention}> {text}"
        return [{"text": p} for p in split_message(text)]

    async def deliver(self, did: str) -> Optional[dict]:
        return await dlv.run(self.store, did, self.send_part, now=self.clock())

    async def send_part(self, doc: Dict[str, Any], part: Dict[str, Any]) -> "dlv.SendResult":
        try:
            chain = await self.hierarchy.load_chain(Level.BOT_INSTANCE, doc["instance_id"])
        except HierarchyError:
            return dlv.SendResult("not_sent_final", error="instance_gone")
        if not chain.availability().available:              # Phase 6A (H): suspended/archived/deleting
            return dlv.SendResult("not_sent_final", error="unavailable")
        app = await get_app(self.store, chain.enterprise_id)
        if chain.enterprise_id != doc["enterprise_id"] or not app or app.get("status") != "enabled" \
                or app.get("application_id") != chain.instance.channel.application_id:
            return dlv.SendResult("not_sent_final", error="application_unavailable")
        target = doc["target"]
        mentions = dict(SAFE_MENTIONS)
        if target.get("mention"):
            mentions["users"] = [str(target["mention"])]
        payload: Dict[str, Any] = {"content": part["text"], "allowed_mentions": mentions,
                                   "nonce": part["nonce"], "enforce_nonce": True}
        if part["index"] == 0 and target.get("reply_to"):
            payload["message_reference"] = {"message_id": str(target["reply_to"]), "fail_if_not_exists": False}
        try:
            msg = await self.rest(app).create_message(str(target["channel_id"]), payload)
            return dlv.SendResult("delivered", message_id=str((msg or {}).get("id") or ""))
        except DiscordError as exc:
            if exc.category in ("connect_failed",):
                return dlv.SendResult("not_sent", error=exc.category)
            if exc.category == "rate_limited":
                return dlv.SendResult("not_sent", retry_after=int(exc.retry_after) + 1, error="rate_limited")
            if exc.category in ("bad_request", "forbidden", "not_found", "unauthorized", "invalid_token"):
                return dlv.SendResult("not_sent_final", error=exc.category)
            age = (datetime.fromisoformat(self.clock()) - datetime.fromisoformat(doc["created_at"])).total_seconds()
            if age < NONCE_WINDOW_SECONDS:
                return dlv.SendResult("not_sent", error="retry_with_nonce")   # Discord dedupes the nonce
            return dlv.SendResult("uncertain", error=exc.category)


__all__ = ["DiscordChannel", "PASSIVE_HINT", "NONCE_WINDOW_SECONDS"]
