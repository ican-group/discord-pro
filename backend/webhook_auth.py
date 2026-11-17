"""Webhook authentication helpers (spec 008 Stage 7, P7). Standard library only
(FastAPI imported lazily for the request-body helper).

* Zendesk: `X-Zendesk-Webhook-Signature` = base64(HMAC-SHA256(signing secret,
  `X-Zendesk-Webhook-Signature-Timestamp` + raw body)); timestamps older or newer
  than 5 minutes are rejected (replay).
* Telegram: `X-Telegram-Bot-Api-Secret-Token` must equal the `secret_token` the
  webhook was registered with (constant-time comparison).
* Request bodies of public POST routes are read with a hard size limit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional

import rate_limit as _rate_limit
import security_utils as _sec

ZENDESK_SIGNATURE_HEADER = "X-Zendesk-Webhook-Signature"
ZENDESK_TIMESTAMP_HEADER = "X-Zendesk-Webhook-Signature-Timestamp"
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
REPLAY_TOLERANCE_SECONDS = 300
MAX_WEBHOOK_BODY_BYTES = 256 * 1024


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value or len(value) > 64:
        return None
    text = value.strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def zendesk_signature(signing_secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(signing_secret.encode("utf-8"), timestamp.encode("utf-8") + body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def zendesk_signature_valid(signing_secret: Optional[str], timestamp: Optional[str], body: bytes,
                            signature: Optional[str], now: Optional[datetime] = None,
                            tolerance: int = REPLAY_TOLERANCE_SECONDS) -> bool:
    """Constant-time signature check plus a replay window on the signed timestamp."""
    if not (isinstance(signing_secret, str) and signing_secret and isinstance(signature, str) and signature
            and isinstance(timestamp, str)):
        return False
    expected = zendesk_signature(signing_secret, timestamp, body)
    same = hmac.compare_digest(expected.encode(), signature.strip().encode())
    ts = _parse_timestamp(timestamp)
    fresh = ts is not None and abs(((now or datetime.now(timezone.utc)) - ts).total_seconds()) <= tolerance
    return same and fresh


def telegram_secret_valid(expected: Optional[str], header_value: Optional[str]) -> bool:
    return _sec.secret_matches(expected, header_value)


async def read_body_limited(request, max_bytes: int = MAX_WEBHOOK_BODY_BYTES) -> bytes:
    """Read the raw body, refusing more than max_bytes (413) before buffering it all."""
    from fastapi import HTTPException  # lazy
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


# Per-instance webhook rate limit (spec 008, R-RL.2): 120 deliveries/minute.
WEBHOOK_LIMITER = _rate_limit.SlidingWindowLimiter(120, 60, max_keys=10_000)


def check_webhook_rate(provider: str, instance_id: str) -> None:
    decision = WEBHOOK_LIMITER.hit(f"{provider}:{str(instance_id)[:128]}")
    if not decision.allowed:
        raise _rate_limit.too_many_requests_exception(decision.retry_after)
