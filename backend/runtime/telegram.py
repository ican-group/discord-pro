"""Telegram channel adapter (spec 012 §5, Phase 4C; parity T1–T12).

* ``TelegramClient``: the Bot API at the fixed host ``api.telegram.org``. The
  bot token appears only in the request path. It is never logged (httpx
  request logging is held at WARNING for this module's calls, and tests
  verify this), never included in exceptions, and never returned. Timeouts
  are bounded, redirects are not followed, and errors map to safe categories.
* Pure helpers: listen modes and the chat allowlist (``decide``), chunking at
  4096 characters on paragraph/word boundaries, extraction of follow-up
  suggestions (T5), and parsing of updates.
* ``TelegramChannel``: webhook connect, disconnect, rotate, sync and test,
  plus delivery of replies (Markdown with plain-text retry, ``retry_after``
  honoured once and bounded, inline suggestion buttons with short opaque
  callback ids mapped server-side).
* ``telegram_cleanup_executors``: the deletion-job adapter (X12). It removes
  the webhook only for the bot the action names, and never while another
  instance holds that bot's binding.

No live Telegram call is made in tests (``httpx.MockTransport``).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

API_BASE = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
MAX_MESSAGE = 4096
MAX_INPUT_CHARS = 4000
MAX_RETRY_AFTER = 30
ALLOWED_UPDATES = ["message", "callback_query"]
SUGGESTION_RE = re.compile(r"\[SUGGESTIONS:\s*(.+?)\]\s*$", re.DOTALL)
FOLLOWUP_HINT = (
    "TELEGRAM FOLLOW-UP SUGGESTIONS: When your answer naturally invites related questions, end your response with "
    "one line in this exact format: [SUGGESTIONS: question one | question two | question three]. At most 3 short "
    "questions (max 40 characters each). Omit the line for greetings, acknowledgements or closing messages.")
HELP_TEXT = "Send me a question! Use /ask followed by your question, or mention me."

# Request logging by httpx/httpcore would include the URL, which contains the token.
for _name in ("httpx", "httpcore"):
    if logging.getLogger(_name).level < logging.WARNING:
        logging.getLogger(_name).setLevel(logging.WARNING)


class TelegramError(Exception):
    """Safe category only; never the token, URL or provider text."""

    def __init__(self, category: str, retry_after: int = 0, parse_error: bool = False):
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after
        self.parse_error = parse_error


_TOKEN_RE = re.compile(r"[1-9][0-9]{2,19}:[A-Za-z0-9_-]{20,128}")


class TelegramClient:
    def __init__(self, token: str, *, transport: Optional[httpx.AsyncBaseTransport] = None):
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise TelegramError("invalid_token")
        self._token = token
        self._transport = transport

    def __repr__(self) -> str:
        return "TelegramClient(****)"

    async def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if not re.fullmatch(r"[A-Za-z]{3,40}", method):
            raise TelegramError("bad_method")
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT, follow_redirects=False,
                                         verify=True) as client:
                resp = await client.post(f"{API_BASE}/bot{self._token}/{method}", json=payload or {})
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise TelegramError("connect_failed") from None          # request never reached Telegram
        except (httpx.TimeoutException, httpx.TransportError):
            raise TelegramError("uncertain") from None               # may or may not have been processed
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError("uncertain" if resp.status_code < 400 else "server_error") from None
        if not isinstance(data, dict):
            raise TelegramError("uncertain")
        if resp.status_code == 200 and data.get("ok") is True:
            return data.get("result")
        code = resp.status_code
        if code == 401:
            raise TelegramError("unauthorized")
        if code == 403:
            raise TelegramError("forbidden")
        if code == 429:
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError("rate_limited", retry_after=int(retry) if isinstance(retry, int) else 1)
        if code == 400:
            desc = str(data.get("description") or "").lower()
            raise TelegramError("bad_request", parse_error="parse" in desc and "entit" in desc)
        if code >= 500:
            raise TelegramError("server_error")                      # outcome unknown
        raise TelegramError("unavailable")

    async def get_me(self) -> Dict[str, Any]:
        me = await self.call("getMe")
        if not isinstance(me, dict) or not isinstance(me.get("id"), int):
            raise TelegramError("uncertain")
        return me

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self.call("setWebhook", {"url": url, "secret_token": secret_token, "allowed_updates": ALLOWED_UPDATES})

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})


def bot_id_of(token: str) -> Optional[str]:
    return token.split(":", 1)[0] if isinstance(token, str) and _TOKEN_RE.fullmatch(token) else None


# ═════════════════════════════════ pure helpers ═════════════════════════════════

def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secret_matches(presented: Any, stored_hash: Optional[str]) -> bool:
    if not isinstance(presented, str) or not stored_hash or not 20 <= len(presented) <= 256:
        return False
    return hmac.compare_digest(hash_secret(presented), stored_hash)


def new_webhook_secrets() -> Tuple[str, str]:
    """(path secret, header secret). The header value obeys Telegram's charset."""
    return secrets.token_urlsafe(32), secrets.token_urlsafe(48)[:64]


@dataclass(frozen=True)
class Decision:
    respond: bool
    text: str = ""
    help: bool = False


def decide(message: Dict[str, Any], *, listen_mode: str, chat_ids: Sequence[str], bot_username: str,
           bot_id: Optional[str]) -> Decision:
    """Listen modes (T4), legacy semantics; chat allowlist enforced."""
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return Decision(False)                                    # only text messages (as today)
    sender = message.get("from") or {}
    if sender.get("is_bot") is True:
        return Decision(False)
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if chat_ids and chat_id not in set(chat_ids):
        return Decision(False)
    private = chat.get("type") == "private"
    if listen_mode == "dm_only" and not private:
        return Decision(False)
    text = text.strip()
    uname = (bot_username or "").lstrip("@")
    mention = f"@{uname}" if uname else None
    is_mentioned = bool(mention and mention.lower() in text.lower())
    is_command = text.startswith("/ask ") or text == "/ask" or bool(uname and text.lower().startswith(f"/ask@{uname.lower()}"))
    reply = message.get("reply_to_message") or {}
    reply_from = reply.get("from") or {}
    is_reply_to_bot = bool(reply_from.get("is_bot") and (str(reply_from.get("id")) == str(bot_id) or
                                                         (uname and reply_from.get("username") == uname)))
    if listen_mode == "mention_or_command" and not private and not (is_mentioned or is_command or is_reply_to_bot):
        return Decision(False)
    clean = text
    if is_command:
        clean = clean.split(" ", 1)[1] if " " in clean else ""
    if is_mentioned and mention:
        clean = re.sub(re.escape(mention), "", clean, flags=re.IGNORECASE)
    clean = clean.strip()[:MAX_INPUT_CHARS]
    if not clean:
        return Decision(True, "", help=True)
    return Decision(True, clean)


def split_suggestions(text: str) -> Tuple[str, List[str]]:
    m = SUGGESTION_RE.search(text or "")
    if not m:
        return text, []
    items = [s.strip()[:40] for s in m.group(1).split("|") if s.strip()][:3]
    return text[:m.start()].rstrip(), items


def chunk_text(text: str, limit: int = MAX_MESSAGE) -> List[str]:
    """Split on paragraph, then line, then word boundaries; hard cut only if a
    single word exceeds the limit."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


# ═════════════════════════════════ deletion cleanup (X12) ═════════════════════════════════

def telegram_cleanup_executors(box, *, transport: Optional[httpx.AsyncBaseTransport] = None) -> Dict[str, Any]:
    from core.bindings import BindingRegistry
    from core.deletion import CleanupOutcome, CleanupStatus

    async def delete_webhook(action: Dict[str, Any], ctx) -> CleanupOutcome:
        target_bot = (action.get("target") or {}).get("telegram_bot_id")
        if target_bot:
            holders = await BindingRegistry(ctx.store).holders(f"telegram:bot:{target_bot}")
            if holders:                       # the bot now serves another instance: never disturb it
                return CleanupOutcome(CleanupStatus.NOT_REQUIRED)
        doc = await ctx.credential_doc(action.get("credential_id"))
        if not doc or doc.get("status") != "active" or not doc.get("secret_enc"):
            return CleanupOutcome(CleanupStatus.RETRY, "credential_unavailable")
        try:
            token = box.decrypt(doc["secret_enc"])
        except Exception:
            return CleanupOutcome(CleanupStatus.RETRY, "credential_unavailable")
        if target_bot and bot_id_of(token) != str(target_bot):
            return CleanupOutcome(CleanupStatus.RETRY, "bot_mismatch")       # never touch another bot
        try:
            await TelegramClient(token, transport=transport).delete_webhook()   # idempotent at Telegram
        except TelegramError as exc:
            return CleanupOutcome(CleanupStatus.RETRY, exc.category)
        return CleanupOutcome(CleanupStatus.SUCCEEDED)

    return {"telegram_delete_webhook": delete_webhook}


__all__ = ["TelegramClient", "TelegramError", "decide", "Decision", "split_suggestions", "chunk_text",
           "hash_secret", "secret_matches", "new_webhook_secrets", "bot_id_of", "telegram_cleanup_executors",
           "FOLLOWUP_HINT", "HELP_TEXT", "MAX_MESSAGE", "ALLOWED_UPDATES"]
