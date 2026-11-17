"""Small security primitives (spec 008 Stage 2). Standard library only.

* ``secret_matches`` — constant-time comparison for shared secrets (webhook path
  secrets), with the same work whether or not a stored secret exists (R-P7.1).
* Zendesk sidebar keys — stored only as a hash and looked up by hash (R-P4.3).
  Keys are 256-bit random tokens, so an unkeyed, namespaced SHA-256 is a
  sufficient one-way function and no additional secret is needed.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

# Used when no stored secret exists, so the comparison still runs.
_DUMMY_SECRET = "0" * 43
MAX_SECRET_LENGTH = 256


def secret_matches(expected: Optional[str], provided: Optional[str]) -> bool:
    """True only if ``expected`` is a non-empty string equal to ``provided``.

    Uses ``hmac.compare_digest``. A missing/empty stored secret is compared
    against a dummy value and always fails, so callers can treat "unknown
    instance" and "wrong secret" identically.
    """
    exp = expected if isinstance(expected, str) and expected else None
    prov = provided if isinstance(provided, str) else ""
    if len(prov) > MAX_SECRET_LENGTH:
        prov = prov[:MAX_SECRET_LENGTH]
        exp_ok = False
    else:
        exp_ok = exp is not None
    same = hmac.compare_digest((exp or _DUMMY_SECRET).encode("utf-8"), prov.encode("utf-8"))
    return exp_ok and same


# ── Zendesk sidebar keys ────────────────────────────────────────────────────
SIDEBAR_KEY_PREFIX = "zsk_"
_SIDEBAR_HASH_NAMESPACE = b"bitbotpro:zendesk-sidebar-key:v1:"
SIDEBAR_KEY_HASH_FIELD = "sidebar_api_key_hash"
SIDEBAR_KEY_HINT_FIELD = "sidebar_api_key_hint"
LEGACY_SIDEBAR_KEY_FIELD = "sidebar_api_key"  # plaintext; no longer written or used for auth


def new_sidebar_key() -> str:
    return f"{SIDEBAR_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_sidebar_key(key: str) -> str:
    return hashlib.sha256(_SIDEBAR_HASH_NAMESPACE + key.encode("utf-8")).hexdigest()


def sidebar_key_hint(key: str) -> str:
    """Non-secret hint for the admin UI: prefix and last 4 characters."""
    return f"{SIDEBAR_KEY_PREFIX}…{key[-4:]}"


def masked_sidebar_key(hint: Optional[str]) -> str:
    """Display value for an existing key. Never a usable key."""
    return f"{hint or SIDEBAR_KEY_PREFIX + '…'} (hidden; regenerate to get a new key)"


def sidebar_key_fields(key: str) -> dict:
    """The fields stored for a sidebar key (hash and hint only)."""
    return {SIDEBAR_KEY_HASH_FIELD: hash_sidebar_key(key), SIDEBAR_KEY_HINT_FIELD: sidebar_key_hint(key)}


def sidebar_key_candidate_hash(raw_header: Optional[str]) -> Optional[str]:
    """Hash of a presented sidebar key, or None if it cannot be a valid key."""
    key = (raw_header or "").strip()
    if not key or len(key) > 128 or not key.startswith(SIDEBAR_KEY_PREFIX):
        return None
    return hash_sidebar_key(key)
