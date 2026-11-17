"""Single-use OAuth `state` values (spec 008 Stage 6, P5). Standard library only.

* `state` is 256 random bits (URL-safe). Only its SHA-256 is stored, as the `_id`
  of a document in `oauth_states`, together with the provider, the intended
  action (mode), the initiating user, the target instance and an explicit
  expiry. A leaked database therefore cannot be used to complete a flow.
* The callback consumes the state with one atomic `find_one_and_delete`, then
  checks the expiry explicitly (the optional TTL index only garbage-collects):
  a state can be used at most once, and never after it expires.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

COLLECTION = "oauth_states"
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{40,100}$")
_NAMESPACE = b"bitbotpro:oauth-state:v1:"

MODE_INSTALL = "install"   # an admin connects their own Discord server
MODE_SHARE = "share"       # a link the admin sends to a third-party server owner
TTL = {MODE_INSTALL: timedelta(minutes=10), MODE_SHARE: timedelta(days=7)}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def is_well_formed(state: Any) -> bool:
    return isinstance(state, str) and bool(_STATE_RE.fullmatch(state))


def state_key(state: str) -> str:
    return hashlib.sha256(_NAMESPACE + state.encode("utf-8")).hexdigest()


def state_record(state: str, *, provider: str, mode: str, user_id: str, instance_id: str,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    if mode not in TTL:
        raise ValueError(f"unknown OAuth mode {mode!r}")
    now = now or utcnow()
    return {
        "_id": state_key(state), "provider": provider, "mode": mode,
        "user_id": user_id, "instance_id": instance_id,
        "created_at": now, "expires_at": now + TTL[mode],
    }


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_live(record: Optional[Mapping[str, Any]], provider: str, now: Optional[datetime] = None) -> bool:
    """True if a consumed record belongs to `provider` and has not expired."""
    if not record or record.get("provider") != provider or record.get("mode") not in TTL:
        return False
    expires = _aware(record.get("expires_at"))
    return expires is not None and (now or utcnow()) < expires


async def issue(collection, *, provider: str, mode: str, user_id: str, instance_id: str) -> str:
    state = new_state()
    await collection.insert_one(state_record(state, provider=provider, mode=mode, user_id=user_id,
                                             instance_id=instance_id))
    return state


async def consume(collection, state: Any, provider: str) -> Optional[Dict[str, Any]]:
    """Atomically remove and return the live record for `state`, or None."""
    if not is_well_formed(state):
        return None
    record = await collection.find_one_and_delete({"_id": state_key(state), "provider": provider})
    return record if is_live(record, provider) else None
