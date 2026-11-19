"""Audit records (spec 012 §8-9). Append-only; details never contain secrets."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.authz import Actor
from core.hierarchy import DuplicateKey, new_id

COLLECTION = "audit_events"
_SENSITIVE = ("secret", "token", "password", "key", "authorization", "cookie")


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {str(k)[:64]: ("[redacted]" if any(s in str(k).lower() for s in _SENSITIVE) else _clean(v, depth + 1))
                for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


def stable_event_id(name: str) -> str:
    """A deterministic id in the UUIDv4 shape required by the schema, derived from
    a stable workflow name (e.g. ``integration.delete_requested:<id>:<request>``)."""
    import hashlib
    h = hashlib.sha256(("bitbotpro:audit:" + name).encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{'89ab'[int(h[16], 16) % 4]}{h[17:20]}-{h[20:32]}"


async def record(store, *, actor: Optional[Actor], action: str, enterprise_id: Optional[str],
                 target_level: Optional[str], target_id: Optional[str], now: str,
                 details: Optional[Dict[str, Any]] = None, event_id: Optional[str] = None) -> dict:
    """``event_id`` (optional) makes the write idempotent: a durable workflow that
    is resumed after a crash records each audit event exactly once."""
    doc = {"id": event_id or new_id(), "at": now, "action": action,
           "actor_id": actor.user_id if actor else None,
           "actor_superadmin": bool(actor and actor.is_platform_superadmin),
           "enterprise_id": enterprise_id, "target_level": target_level, "target_id": target_id,
           "details": _clean(details or {}), "version": 1}
    try:
        await store.insert(COLLECTION, doc)
    except DuplicateKey:
        if event_id is None:
            raise
        return await store.get(COLLECTION, event_id) or doc
    return doc


__all__ = ["COLLECTION", "record", "stable_event_id"]
