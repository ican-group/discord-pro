"""Defence-in-depth redaction of credentials in log output (spec 004, R8).

Wraps the formatter of every installed handler so the *final* formatted line,
including uvicorn access-log request paths, query strings and exception
tracebacks, is scrubbed before it is written. Standard library only.

Specific code paths must still avoid logging secrets in the first place; this is
a safety net, not a substitute.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, List, Tuple

REDACTED = "[REDACTED]"

_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Secret path segments: webhook secrets and invitation tokens.
    (re.compile(r"(/(?:telegram|zendesk)/webhook/[^/\s?\"']+/)[^/\s?\"']+"), r"\1" + REDACTED),
    (re.compile(r"(/invite/)[^/\s?\"']+"), r"\1" + REDACTED),
    # Telegram bot tokens (also inside https://api.telegram.org/bot<token>/...).
    (re.compile(r"(?<![0-9])(\d{5,12}):[A-Za-z0-9_-]{30,}"), r"\1:" + REDACTED),
    # Credential-bearing query/form parameters.
    (re.compile(r"(?i)([?&](?:code|state|token|access_token|refresh_token|id_token|api_key|apikey|key|"
                r"secret|client_secret|password|passwd|signature|sig)=)[^&\s\"']*"), r"\1" + REDACTED),
    # Authorization headers and bearer tokens.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # JWTs.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # Credentials embedded in connection URIs (mongodb, postgresql, redis, amqp, http...).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20}://)[^/\s:@\"']+:[^/\s@\"']+@"), r"\1" + REDACTED + "@"),
    # Common provider key formats.
    (re.compile(r"\b(sk-ant-|sk-proj-|sk-|re_|whsec_|xox[abpr]-)[A-Za-z0-9_-]{12,}"), r"\1" + REDACTED),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Delegates to the original formatter, then redacts the formatted line."""

    def __init__(self, inner: logging.Formatter | None):
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


DEFAULT_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")


def install(logger_names: Iterable[str] = DEFAULT_LOGGERS) -> int:
    """Wrap the formatter of every handler on the given loggers. Idempotent.
    Returns the number of handlers newly wrapped."""
    wrapped = 0
    for name in logger_names:
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(handler.formatter))
                wrapped += 1
    return wrapped


# ── Log-injection sanitising (spec 008, R-L1.1) ─────────────────────────────
# For request-derived values (headers, query parameters, OAuth error text...)
# interpolated into log messages. Not yet applied to any call site (Stage 1).
SANITIZE_DEFAULT_MAX_LEN = 200
_TRUNCATION_MARK = "...[truncated]"
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_char(ch: str) -> str:
    named = _NAMED_ESCAPES.get(ch)
    if named:
        return named
    code = ord(ch)
    # C0 controls, DEL, C1 controls (incl. NEL U+0085).
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return f"\\x{code:02x}"
    # Line/paragraph separators, bidi embeddings/overrides/isolates, BOM, and
    # other format characters that can forge or visually reorder log lines.
    if code in (0x2028, 0x2029, 0x200E, 0x200F, 0x061C, 0xFEFF) or 0x202A <= code <= 0x202E \
            or 0x2066 <= code <= 0x2069:
        return f"\\u{code:04x}"
    return ch


def sanitize_for_log(value: object, max_len: int = SANITIZE_DEFAULT_MAX_LEN) -> str:
    """Return a single-line, length-capped, escaped representation of ``value``.

    - CR, LF and every other control/line-separator/bidi character is escaped,
      so one value can never produce more than one log line or reorder text.
    - Backslashes are escaped first, so an attacker cannot pre-forge ``\\n``
      sequences that look like the escapes produced here.
    - The output never exceeds ``max_len`` characters; truncation is marked and
      never splits an escape sequence.
    """
    if max_len < len(_TRUNCATION_MARK) + 1:
        raise ValueError("max_len too small")
    text = value if isinstance(value, str) else str(value)
    out: List[str] = []
    length = 0
    budget = max_len - len(_TRUNCATION_MARK)
    for i, ch in enumerate(text):
        piece = "\\\\" if ch == "\\" else _escape_char(ch)
        if length + len(piece) > budget:
            # Does the rest fit without truncation? (Each char escapes to >= 1
            # char, so only a short remainder can possibly fit.)
            if len(text) - i <= max_len - length:
                rest = "".join("\\\\" if c == "\\" else _escape_char(c) for c in text[i:])
                if length + len(rest) <= max_len:
                    out.append(rest)
                    return "".join(out)
            out.append(_TRUNCATION_MARK)
            return "".join(out)
        out.append(piece)
        length += len(piece)
    return "".join(out)
