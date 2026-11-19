"""Conversation sessions, conversation storage and request idempotency
(spec 012 §3.2 `conversations`, Phase 4B; Webchat first, reusable by later
channels).

Sessions (``chat_sessions``):
* id = random UUIDv4; a separate 256-bit **session token** is returned once and
  stored only as SHA-256. Every use needs the id *and* the token
  (constant-time compare), so guessing or swapping ids is useless.
* Bound to one Bot Instance and its full ancestry; a session is accepted only
  for the instance its request resolved to. Expiry: 30 days (TTL index).

Conversations (``conversations``): one document per session, with ancestry,
messages ``{id, role, text, at, citations}``, and ``expires_at_dt`` refreshed
from the effective ``retention.conversation_days`` policy (TTL index).
Appends use compare-and-swap; at most ``MAX_STORED_MESSAGES`` are kept.
Messages hold no credentials and no request metadata such as IP addresses.

Requests (``chat_requests``): ``<session>:<client_message_id>`` with a unique
id makes a message idempotent. A retry, a duplicate request or a streaming
reconnect replays the stored result instead of running the pipeline (and
billing) again.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.hierarchy import Chain, ConcurrentModification, DuplicateKey, NotFound, is_valid_id, new_id

SESSIONS = "chat_sessions"
CONVERSATIONS = "conversations"
REQUESTS = "chat_requests"
SESSION_TTL_DAYS = 30
REQUEST_TTL_HOURS = 24
MAX_STORED_MESSAGES = 400
_MAX_ATTEMPTS = 64


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ancestry(chain: Chain) -> Dict[str, str]:
    return {"enterprise_id": chain.enterprise.id, "organisation_id": chain.organisation.id,
            "bot_id": chain.bot.id, "instance_id": chain.instance.id}


PREVIEW_SESSION_TTL_HOURS = 2


async def create_session(store, chain: Chain, *, now: str, preview_by: Optional[str] = None) -> Tuple[dict, str]:
    """``preview_by`` (F5) is set only by the authenticated admin preview route:
    the session's traffic is labelled ``preview`` in usage (still counted and
    billed like live traffic). The flag lives only on the server-side session."""
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=PREVIEW_SESSION_TTL_HOURS) if preview_by else timedelta(days=SESSION_TTL_DAYS)
    expires = datetime.fromisoformat(now) + ttl
    doc = {"id": new_id(), "token_hash": _hash(token), **_ancestry(chain), "channel": "webchat",
           "created_at": now, "expires_at": expires.isoformat(), "expires_at_dt": expires, "version": 1}
    if preview_by:
        doc.update({"preview": True, "preview_by": preview_by})
    await store.insert(SESSIONS, doc)
    return doc, token


async def verify_session(store, chain: Chain, session_id: Any, token: Any, *, now: str) -> Optional[dict]:
    """The session must exist, match the token, belong to this exact instance
    (and ancestry) and not be expired. Every failure looks the same."""
    if not is_valid_id(session_id) or not isinstance(token, str) or not 20 <= len(token) <= 200:
        return None
    doc = await store.get(SESSIONS, session_id)
    if doc is None:
        return None
    if not hmac.compare_digest(doc.get("token_hash", ""), _hash(token)):
        return None
    if any(doc.get(k) != v for k, v in _ancestry(chain).items()):
        return None
    if datetime.fromisoformat(now) >= datetime.fromisoformat(doc["expires_at"]):
        return None
    return doc


async def history(store, chain: Chain, session_id: str, *, limit: int = 100) -> List[dict]:
    doc = await store.get(CONVERSATIONS, session_id)
    if doc is None or any(doc.get(k) != v for k, v in _ancestry(chain).items()):
        return []
    return list(doc.get("messages") or [])[-limit:]


async def user_message_count(store, session_id: str) -> int:
    doc = await store.get(CONVERSATIONS, session_id)
    return sum(1 for m in (doc or {}).get("messages") or [] if m.get("role") == "user")


async def append_turn(store, chain: Chain, session_id: str, messages: List[dict], *, now: str,
                      retention_days: Optional[int], channel: str = "webchat") -> None:
    """Idempotent: messages whose ids are already stored are skipped."""
    expires = datetime.fromisoformat(now) + timedelta(days=int(retention_days or 730))
    for _ in range(_MAX_ATTEMPTS):
        doc = await store.get(CONVERSATIONS, session_id)
        try:
            if doc is None:
                await store.insert(CONVERSATIONS, {
                    "id": session_id, "session_id": session_id, **_ancestry(chain), "channel": channel,
                    "messages": messages[-MAX_STORED_MESSAGES:], "created_at": now, "updated_at": now,
                    "expires_at": expires.isoformat(), "expires_at_dt": expires, "version": 1})
                return
            if any(doc.get(k) != v for k, v in _ancestry(chain).items()):
                raise NotFound("Not found.")                     # never write into another tenant's record
            known = {m.get("id") for m in doc.get("messages") or []}
            fresh = [m for m in messages if m.get("id") not in known]
            if not fresh:
                return
            await store.update(CONVERSATIONS, session_id, {
                "messages": (list(doc.get("messages") or []) + fresh)[-MAX_STORED_MESSAGES:],
                "updated_at": now, "expires_at": expires.isoformat(), "expires_at_dt": expires},
                expect_version=doc["version"])
            return
        except (DuplicateKey, ConcurrentModification):
            continue
    raise ConcurrentModification()


def request_id(session_id: str, client_message_id: str) -> str:
    return f"{session_id}:{client_message_id}"


def reply_message_id(rid: str) -> str:
    """Deterministic, so a replayed or completed request never duplicates a message."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bitbotpro:reply:" + rid))


REQUEST_LEASE_SECONDS = 120


async def begin_request(store, chain: Chain, session_id: str, client_message_id: str, *,
                        now: str, channel: str = "webchat") -> Tuple[str, Optional[dict]]:
    """Claim a client message for processing (durable idempotency).

    Returns one of ``new`` (claimed: process it), ``busy`` (another request holds
    a live lease), ``stale`` (the holder's lease expired: a crash), or
    ``generated``/``done``/``uncertain`` with the stored record."""
    rid = request_id(session_id, client_message_id)
    expires = datetime.fromisoformat(now) + timedelta(hours=REQUEST_TTL_HOURS)
    lease = (datetime.fromisoformat(now) + timedelta(seconds=REQUEST_LEASE_SECONDS)).isoformat()
    try:
        await store.insert(REQUESTS, {"id": rid, "session_id": session_id, **_ancestry(chain), "channel": channel,
                                      "status": "processing", "lease_until": lease, "created_at": now,
                                      "expires_at_dt": expires, "version": 1})
        return "new", None
    except DuplicateKey:
        doc = await store.get(REQUESTS, rid)
        if doc is None or any(doc.get(k) != v for k, v in _ancestry(chain).items()):
            return "busy", None
        if doc["status"] in ("generated", "done", "uncertain"):
            return doc["status"], doc
        if datetime.fromisoformat(now) < datetime.fromisoformat(doc.get("lease_until") or now):
            return "busy", doc
        return "stale", doc


async def takeover(store, doc: dict, *, now: str) -> bool:
    """Re-claim a stale request (only when nothing was billed for it)."""
    lease = (datetime.fromisoformat(now) + timedelta(seconds=REQUEST_LEASE_SECONDS)).isoformat()
    try:
        await store.update(REQUESTS, doc["id"], {"lease_until": lease}, expect_version=doc["version"])
        return True
    except (ConcurrentModification, NotFound):
        return False


async def _set_status(store, rid: str, from_states, fields: Dict[str, Any]) -> bool:
    for _ in range(_MAX_ATTEMPTS):
        doc = await store.get(REQUESTS, rid)
        if doc is None or doc.get("status") not in from_states:
            return False
        try:
            await store.update(REQUESTS, rid, fields, expect_version=doc["version"])
            return True
        except ConcurrentModification:
            continue
    raise ConcurrentModification()


async def mark_generated(store, rid: str, result: Dict[str, Any]) -> None:
    if not await _set_status(store, rid, ("processing",), {"status": "generated", "result": result}):
        raise ConcurrentModification()


async def mark_uncertain(store, rid: str) -> None:
    await _set_status(store, rid, ("processing", "generated"), {"status": "uncertain"})


async def finish_request(store, session_id: str, client_message_id: str, result: Dict[str, Any]) -> None:
    await _set_status(store, request_id(session_id, client_message_id), ("processing", "generated"),
                      {"status": "done", "result": result})


async def abandon_request(store, session_id: str, client_message_id: str) -> None:
    """Remove a request record whose processing failed before any billable work
    (so the client may retry with the same id)."""
    await store.delete(REQUESTS, request_id(session_id, client_message_id))


async def complete_request(store, usage, chain: Chain, record: dict, *, now: str,
                           retention_days: Optional[int], delivery_builder=None) -> Dict[str, Any]:
    """Finish a request whose answer is durably stored (status ``generated``):
    conversation append, usage settlement and completion. Every step is
    idempotent, so the API (on retry) and the worker (after a crash) may both
    run it, any number of times, without duplicating messages or billing."""
    from runtime.usage import Reservation
    res = record["result"]
    if res.get("messages"):
        await append_turn(store, chain, record["session_id"], res["messages"], now=now,
                          retention_days=retention_days, channel=record.get("channel", "webchat"))
    if res.get("billed"):
        await usage.settle(Reservation(record["id"], ()), chain, now=now, details=res.get("usage") or {})
    spec = res.get("delivery")
    if spec and spec.get("text"):
        # The durable delivery record exists before the request is done, so a crash
        # anywhere after generation still ends in a send (or an explicit uncertainty).
        from runtime import delivery as dlv
        parts = await delivery_builder(chain, spec) if delivery_builder else [{"text": spec["text"]}]
        await dlv.ensure(store, chain, record["id"], channel=spec["channel"], target=spec["target"], parts=parts,
                         now=now)
    await _set_status(store, record["id"], ("processing", "generated"), {"status": "done", "result": res})
    return res


__all__ = ["complete_request", "SESSIONS", "CONVERSATIONS", "REQUESTS", "create_session", "verify_session", "history",
           "user_message_count", "append_turn", "begin_request", "finish_request", "abandon_request", "takeover",
           "mark_generated", "mark_uncertain", "reply_message_id", "request_id",
           "SESSION_TTL_DAYS", "MAX_STORED_MESSAGES"]
