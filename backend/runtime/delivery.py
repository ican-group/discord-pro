"""Durable outbound delivery (Phase 4D; Telegram T-A correction, also used by
Discord).

One delivery record per reply (``outbound_deliveries``), with a stable id
derived from the request it answers. It holds the full ancestry, the target
(chat or channel, the message replied to) and the reply *parts* (chunks)
with their own state. Nothing secret is stored: tokens are resolved by the
channel's sender at send time.

States (record):
``pending`` → ``sending`` → ``delivered``; ``retry`` (definitely not sent, with
back-off); ``failed_before_send`` (definitely not sent, final); ``uncertain``
(a send whose outcome cannot be known); ``resolved`` (an uncertain delivery an
operator has reviewed; never resent). Part states: ``pending``, ``sending``,
``delivered`` (with the provider message id), ``uncertain``.

Guarantees on standalone MongoDB:
* **No concurrent duplicate sends.** A compare-and-swap lease claims the
  record; only the lease holder sends.
* **No blind retries.** A part is marked ``sending`` *before* the call. If a
  worker dies with a part in ``sending``, the outcome is unknown: the record
  becomes ``uncertain`` and is never resent automatically (operator
  visibility). A part that was still ``pending`` when a worker died is safe to
  send.
* **Safe retries** only when the channel proves the message was not sent:
  connection refused before sending, a rate limit (the API rejected it), or a
  validation error (final, not retried).
* Telegram has no idempotency key for ``sendMessage``, so delivery there is
  *at most once with explicit uncertainty*, not exactly once. Discord
  supports ``nonce`` + ``enforce_nonce`` for a short window, so its sender may
  report a timed-out send as safe to retry inside that window (see
  ``runtime.discord``).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.hierarchy import Chain, ConcurrentModification, DuplicateKey, NotFound

COLLECTION = "outbound_deliveries"
# C5 retention (Phase 4D correction): only *terminal* records expire (MongoDB TTL on
# ``expires_at_dt``). Pending, sending, retry and uncertain records carry no expiry,
# so they are never removed while their outcome matters. 30 days is far longer than
# the request-dedupe window (``conversations.REQUEST_TTL_HOURS`` = 24 h), so an
# expired delivery can never re-enable a duplicate send or a second charge.
TERMINAL_RETENTION_DAYS = 30
TERMINAL_STATES = ("delivered", "failed_before_send", "resolved")
LEASE_SECONDS = 60
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 30, 120, 600, 1800)


@dataclass(frozen=True)
class SendResult:
    status: str                       # "delivered" | "not_sent" | "not_sent_final" | "uncertain"
    message_id: Optional[str] = None
    retry_after: int = 0
    error: Optional[str] = None       # sanitised category


Sender = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[SendResult]]


def delivery_id(request_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bitbotpro:delivery:" + request_key))


def _ancestry(chain: Chain, integration_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Enterprise → Organisation → Bot → (Bot Instance | Bot Integration)."""
    if (chain.instance is None) == (integration_id is None):
        raise ValueError("a delivery belongs to exactly one instance or integration")
    return {"enterprise_id": chain.enterprise.id, "organisation_id": chain.organisation.id,
            "bot_id": chain.bot.id, "instance_id": chain.instance.id if chain.instance else None,
            "integration_id": integration_id}


async def ensure(store, chain: Chain, request_key: str, *, channel: str, target: Dict[str, Any],
                 parts: List[Dict[str, Any]], now: str, integration_id: Optional[str] = None) -> dict:
    """Create the delivery once (idempotent per request key)."""
    did = delivery_id(request_key)
    doc = {"id": did, "request_key": request_key, "channel": channel, **_ancestry(chain, integration_id),
           "target": target,
           "parts": [{"index": i, "text": p.get("text", ""), "extra": p.get("extra") or {}, "state": "pending",
                      "message_id": None, "nonce": did.replace("-", "")[:20] + f"{i:02d}"}
                     for i, p in enumerate(parts)],
           "status": "pending", "attempts": 0, "next_attempt_at": None, "lease_until": None, "last_error": None,
           "created_at": now, "updated_at": now, "version": 1}
    try:
        await store.insert(COLLECTION, doc)
        return doc
    except DuplicateKey:
        return await store.get(COLLECTION, did)


def _expiry(now: str, retention_days: int = TERMINAL_RETENTION_DAYS) -> datetime:
    return datetime.fromisoformat(now) + timedelta(days=max(2, int(retention_days)))


async def _save(store, doc: dict, **fields) -> dict:
    if fields.get("status") in TERMINAL_STATES:
        fields.setdefault("expires_at_dt", _expiry(fields.get("updated_at") or doc.get("updated_at")))
    await store.update(COLLECTION, doc["id"], fields, expect_version=doc["version"])
    return {**doc, **fields, "version": doc["version"] + 1}


async def run(store, did: str, sender: Sender, *, now: str, worker: str = "inline") -> Optional[dict]:
    """Send whatever is safely sendable for one delivery. Never sends a part
    whose earlier attempt has an unknown outcome."""
    doc = await store.get(COLLECTION, did)
    if doc is None or doc["status"] not in ("pending", "retry"):
        return doc
    if doc.get("next_attempt_at") and datetime.fromisoformat(now) < datetime.fromisoformat(doc["next_attempt_at"]):
        return doc
    lease = (datetime.fromisoformat(now) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    try:
        doc = await _save(store, doc, status="sending", lease_until=lease, worker=worker,
                          attempts=int(doc.get("attempts", 0)) + 1, updated_at=now)
    except ConcurrentModification:
        return await store.get(COLLECTION, did)                 # someone else claimed it
    parts = [dict(p) for p in doc["parts"]]
    for part in parts:
        if part["state"] == "delivered":
            continue
        part["state"] = "sending"                                 # durable *before* the external call
        doc = await _save(store, doc, parts=parts, updated_at=now)
        try:
            result = await sender(doc, part)
        except Exception:
            result = SendResult("uncertain", error="sender_error")
        if result.status == "delivered":
            part.update(state="delivered", message_id=result.message_id)
            doc = await _save(store, doc, parts=parts, updated_at=now)
            continue
        if result.status in ("not_sent", "not_sent_final"):
            part["state"] = "pending"
            final = result.status == "not_sent_final" or doc["attempts"] >= MAX_ATTEMPTS
            delay = max(result.retry_after, BACKOFF_SECONDS[min(doc["attempts"] - 1, len(BACKOFF_SECONDS) - 1)])
            nxt = (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat()
            return await _save(store, doc, parts=parts, status="failed_before_send" if final else "retry",
                               next_attempt_at=None if final else nxt, lease_until=None,
                               last_error=result.error or "not_sent", updated_at=now)
        part["state"] = "uncertain"
        return await _save(store, doc, parts=parts, status="uncertain", lease_until=None,
                           last_error=result.error or "uncertain", updated_at=now)
    return await _save(store, doc, status="delivered", lease_until=None, last_error=None, updated_at=now)


async def recover(store, *, now: str) -> Dict[str, int]:
    """After a crash: a lease that expired while a part was ``sending`` means
    the outcome is unknown, so the record becomes ``uncertain``. Otherwise the
    record goes back to ``pending`` (nothing was in flight)."""
    out = {"uncertain": 0, "resumed": 0}
    for doc in await store.find(COLLECTION, {"status": "sending"}):
        if not doc.get("lease_until") or datetime.fromisoformat(now) < datetime.fromisoformat(doc["lease_until"]):
            continue
        parts = [dict(p) for p in doc["parts"]]
        in_flight = any(p["state"] == "sending" for p in parts)
        for p in parts:
            if p["state"] == "sending":
                p["state"] = "uncertain"
        try:
            await _save(store, doc, parts=parts, status="uncertain" if in_flight else "pending", lease_until=None,
                        last_error="worker_lost" if in_flight else doc.get("last_error"), updated_at=now)
            out["uncertain" if in_flight else "resumed"] += 1
        except ConcurrentModification:
            continue
    return out


async def reconcile_part(store, did: str, index: int, *, message_id: str, now: str) -> Optional[dict]:
    """An ``uncertain`` part was proven delivered (e.g. the comment was found on
    the provider). Parts never attempted stay ``pending`` and may now be sent;
    nothing is ever resent."""
    doc = await store.get(COLLECTION, did)
    if doc is None or doc["status"] != "uncertain":
        return doc
    parts = [dict(p) for p in doc["parts"]]
    target = next((p for p in parts if p["index"] == index), None)
    if target is None or target["state"] != "uncertain":
        return doc
    target.update(state="delivered", message_id=message_id)
    if any(p["state"] == "uncertain" for p in parts):
        status = "uncertain"
    elif any(p["state"] == "pending" for p in parts):
        status = "pending"
    else:
        status = "delivered"
    try:
        return await _save(store, doc, parts=parts, status=status, last_error=None if status != "uncertain"
                           else doc.get("last_error"), reconciled_at=now, updated_at=now)
    except ConcurrentModification:
        return await store.get(COLLECTION, did)


async def due(store, *, now: str, limit: int = 100) -> List[dict]:
    docs = await store.find(COLLECTION, {"status": ["pending", "retry"]}, limit=limit, sort="created_at")
    return [d for d in docs if not d.get("next_attempt_at")
            or datetime.fromisoformat(now) >= datetime.fromisoformat(d["next_attempt_at"])]


async def resolve(store, did: str, *, enterprise_id: str, note: str, actor_id: str, now: str,
                  instance_id: Optional[str] = None, integration_id: Optional[str] = None) -> dict:
    """Operator review of an ``uncertain`` delivery (tenant-pinned). It becomes
    ``resolved`` (never resent) and then expires like other terminal records."""
    doc = await store.get(COLLECTION, did)
    owner_ok = (instance_id is not None and doc is not None and doc.get("instance_id") == instance_id) or \
               (integration_id is not None and doc is not None and doc.get("integration_id") == integration_id)
    if doc is None or not owner_ok or doc.get("enterprise_id") != enterprise_id:
        raise NotFound("Not found.")
    if doc["status"] != "uncertain":
        raise ConcurrentModification("Only uncertain deliveries can be resolved.")
    return await _save(store, doc, status="resolved", resolution={"by": actor_id, "at": now, "note": note[:500]},
                       updated_at=now)


def public_view(doc: dict) -> Dict[str, Any]:
    return {"id": doc["id"], "channel": doc["channel"], "status": doc["status"], "attempts": doc.get("attempts", 0),
            "parts": [{"index": p["index"], "state": p["state"], "message_id": p.get("message_id")}
                      for p in doc.get("parts") or []],
            "last_error": doc.get("last_error"), "created_at": doc.get("created_at"),
            "expires_at": doc["expires_at_dt"].isoformat() if doc.get("expires_at_dt") else None,
            "updated_at": doc.get("updated_at")}


__all__ = ["COLLECTION", "SendResult", "Sender", "delivery_id", "ensure", "run", "recover", "due", "resolve",
           "reconcile_part",
           "public_view", "TERMINAL_RETENTION_DAYS", "TERMINAL_STATES"]
