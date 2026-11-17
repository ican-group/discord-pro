"""Account-takeover protection primitives (spec 008 Stage 3: P2, N5, N6).

Standard library only, so every rule is unit-testable offline.

Verification and password-reset codes
-------------------------------------
* A code is 6 random digits (``secrets``), valid for ``CODE_TTL``.
* Only ``HMAC-SHA256(key, purpose ‖ user_id ‖ code_id ‖ code)`` is stored, with a
  random per-issuance ``code_id`` and an explicit expiry. The plaintext code
  exists only in the email; it is never stored or logged.
* Binding the hash to purpose, user and code_id means a code cannot be replayed
  across accounts, across purposes, or after it was replaced.
* Comparison is constant-time.
* Attempt counting and invalidation (5 failures) are done by the caller with
  ``rate_limit.MongoCounter`` keyed by ``attempt_key(user_id, code_id)``;
  consumption is a single conditional update on ``code_id`` + hash, so a code
  can be used at most once even under concurrency.

Session revocation
------------------
* Every JWT carries ``tv`` (token version). Changing or resetting a password
  increments the user's ``token_version``; tokens with an older ``tv`` are
  rejected. Tokens issued before this change have no ``tv`` and are treated as
  version 0, so existing sessions stay valid until the first password change.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=15)
CODE_MAX_FAILED_ATTEMPTS = 5
# Attempt-counter window: must outlive the code (codes expire after 15 minutes).
CODE_ATTEMPT_WINDOW_SECONDS = 3600

INVALID_CODE_DETAIL = "Invalid or expired code. Request a new one."

PURPOSE_VERIFY = "verify"
PURPOSE_RESET = "reset"


@dataclass(frozen=True)
class CodeFields:
    """MongoDB field names for one code purpose on the user document."""
    hash: str
    id: str
    expiry: str
    legacy_plaintext: str          # pre-Stage-3 plaintext field; never written or accepted
    legacy_extra: tuple = ()


VERIFY_FIELDS = CodeFields("verification_code_hash", "verification_code_id", "verification_code_expiry",
                           "verification_code")
RESET_FIELDS = CodeFields("reset_code_hash", "reset_code_id", "reset_code_expires", "reset_code")

FIELDS_BY_PURPOSE = {PURPOSE_VERIFY: VERIFY_FIELDS, PURPOSE_RESET: RESET_FIELDS}

# Fields that must never be returned by any API response that includes a user.
SENSITIVE_USER_FIELDS = (
    "hashed_password", "password_hash",
    "verification_code", "verification_code_expiry", "verification_code_hash", "verification_code_id",
    "reset_code", "reset_code_expires", "reset_code_hash", "reset_code_id",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def normalise_code(code: Any) -> Optional[str]:
    """Return the 6-digit code, or None if the input cannot be a valid code."""
    if not isinstance(code, str):
        return None
    c = code.strip()
    if len(c) != CODE_DIGITS or not c.isdigit() or not c.isascii():
        return None
    return c


def hash_code(key: bytes, purpose: str, user_id: str, code_id: str, code: str) -> str:
    if not key:
        raise ValueError("code HMAC key is required")
    msg = "\x1f".join(("bitbotpro-code-v1", purpose, user_id, code_id, code)).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def issue_code(key: bytes, purpose: str, user_id: str, now: Optional[datetime] = None):
    """Return ``(plaintext_code, set_fields, unset_fields)`` for a new code.

    ``set_fields`` contains only the hash, id and expiry; ``unset_fields``
    removes any legacy plaintext code.
    """
    f = FIELDS_BY_PURPOSE[purpose]
    now = now or utcnow()
    code = generate_code()
    code_id = uuid.uuid4().hex
    set_fields = {
        f.hash: hash_code(key, purpose, user_id, code_id, code),
        f.id: code_id,
        f.expiry: (now + CODE_TTL).isoformat(),
    }
    return code, set_fields, {f.legacy_plaintext: ""}


def clear_code_fields(purpose: str) -> Dict[str, str]:
    """``$unset`` document removing every field of a code (current and legacy)."""
    f = FIELDS_BY_PURPOSE[purpose]
    return {f.hash: "", f.id: "", f.expiry: "", f.legacy_plaintext: ""}


def attempt_key(user_id: str, code_id: str) -> str:
    return f"{user_id}.{code_id}"


def _parse_expiry(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


@dataclass(frozen=True)
class StoredCode:
    code_id: str
    code_hash: str
    expires_at: datetime


def stored_code(user: Optional[Mapping[str, Any]], purpose: str) -> Optional[StoredCode]:
    """The user's current hashed code for ``purpose``, or None if there is no
    usable code (missing, legacy plaintext only, or malformed expiry)."""
    if not user:
        return None
    f = FIELDS_BY_PURPOSE[purpose]
    code_id, code_hash = user.get(f.id), user.get(f.hash)
    expires_at = _parse_expiry(user.get(f.expiry))
    if not (isinstance(code_id, str) and code_id and isinstance(code_hash, str) and code_hash and expires_at):
        return None
    return StoredCode(code_id, code_hash, expires_at)


def code_matches(key: bytes, purpose: str, user_id: str, stored: StoredCode, presented: Any,
                 now: Optional[datetime] = None) -> bool:
    """Constant-time check of a presented code against the stored hash, including
    explicit expiry. Always computes an HMAC, even for malformed input."""
    now = now or utcnow()
    code = normalise_code(presented)
    candidate = hash_code(key, purpose, user_id, stored.code_id, code or "------")
    same = hmac.compare_digest(candidate, stored.code_hash)
    return bool(code) and same and now < stored.expires_at


# ── JWT token versions (N5) ──────────────────────────────────────────────────
TOKEN_VERSION_CLAIM = "tv"
TOKEN_VERSION_FIELD = "token_version"


def user_token_version(user: Optional[Mapping[str, Any]]) -> int:
    try:
        return int((user or {}).get(TOKEN_VERSION_FIELD) or 0)
    except (TypeError, ValueError):
        return -1  # malformed: matches no token


def token_version_matches(payload: Mapping[str, Any], user: Optional[Mapping[str, Any]]) -> bool:
    """True if the JWT's ``tv`` equals the user's current ``token_version``.
    Missing ``tv`` (tokens issued before Stage 3) counts as 0."""
    if not user:
        return False
    raw = payload.get(TOKEN_VERSION_CLAIM, 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False
    return raw == user_token_version(user)


DUMMY_PASSWORD = b"bitbotpro-timing-equaliser-not-a-password"
