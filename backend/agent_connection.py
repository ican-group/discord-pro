"""GridScan Agent connection (spec 024).

BridgeBot can hand a bot's live Cardano lookups to a GridScan Agent: a separate service on this
server that gives a model all of the Gridscan API's endpoints as tools. The agent picks and chains
the calls and keeps its own per-session memory of addresses and hashes.

The agent's address is set by the server operator (``GRIDSCAN_AGENT_URL``, usually
``http://127.0.0.1:8099``). Workspaces can't enter a URL, so this connection can't be pointed at
another host on the server's network.

BridgeBot calls two of the agent's endpoints:
- ``GET /api/health``: whether it's up and how many endpoints (tools) it loaded;
- ``POST /api/chat``: ``{message, session_id}``, which returns ``{reply, tool_calls, raw_json}``.
  A message starting with ``/raw`` makes the agent return each API response it fetched in
  ``raw_json``, taken from the API call itself rather than written by its model.

Pure helpers plus two small HTTP calls.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional

import httpx

KIND = "gridscan_agent"
DEFAULT_NAME = "GridScan Agent"
CHAT_TIMEOUT = 150.0            # the agent may chain several API calls and model turns
HEALTH_TIMEOUT = 10.0
# The agent cuts any single API response at 50,000 characters and appends this marker (agent.py).
AGENT_TRUNCATION_MARKER = "\n... (truncated)"
AGENT_TRUNCATION_LIMIT = 50000
# Discord: each message must stay under 2,000 characters, with room for a mention.
DISCORD_PART_CHARS = 1750
# Up to this many messages per payload are posted inline; a longer payload is attached as a file
# (complete), with a message saying so. Nothing is cut without saying so.
DISCORD_INLINE_MAX_PARTS = 10


def agent_url() -> str:
    """The operator-configured agent address, without a trailing slash, or '' when not set."""
    return (os.environ.get("GRIDSCAN_AGENT_URL") or "").strip().rstrip("/")


def _headers() -> Dict[str, str]:
    token = (os.environ.get("GRIDSCAN_AGENT_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def session_id(instance_id: str, conversation: str) -> str:
    """A stable agent session per bot and conversation. Discord conversations are
    ``discord:<user>:<channel>``, so each user in each channel of each bot has its own memory."""
    return f"bridgebot:{instance_id}:{conversation}"


# ─────────────────────────────── raw JSON requests ───────────────────────────────

_RAW_PATTERNS = [
    r"^\s*/raw\b",
    r"\braw\b(?:\W+\w+){0,3}?\W+(?:json|response|responses|payload|data|output|result)\b",
    r"\b(?:exact|full|complete|entire|whole|unmodified|unedited|original|actual)\b(?:\W+\w+){0,2}?\W+"
    r"(?:json|api\s+response|response\s+json|payload)\b",
    r"\b(?:show|give|send|display|print|dump|paste|post|return)\b(?:\W+(?:me|us|the|its|it|this|that|api))*\W+json\b",
    r"\bjson\s+(?:payload|response|dump)\b",
    r"\b(?:api|json)\s+(?:payload|response)\s+(?:as\s+is|unchanged|verbatim)\b",
    r"\bverbatim\b",
]
_RAW_RE = re.compile("|".join(f"(?:{p})" for p in _RAW_PATTERNS), re.IGNORECASE)


def wants_raw(text: str) -> bool:
    """Whether the user asks for the API's own JSON rather than an answer written from it.
    "Reply with one JSON code block containing txHash and lovelace" is a formatted answer, not raw."""
    return bool(_RAW_RE.search(text or ""))


_FRESH_RE = re.compile(r"\b(?:look\s*up|lookup|fetch|refresh|fresh|re-?run|re-?check|query|current|latest|right\s+now|again)\b",
                       re.IGNORECASE)


def wants_fresh(text: str) -> bool:
    """The user explicitly asks for a lookup ("look up transaction …", "check it again"), so an
    answer from the agent's memory isn't enough."""
    return bool(_FRESH_RE.search(text or ""))


def agent_message(text: str, raw: bool, refetch: bool = False) -> str:
    """The message for the agent. ``refetch``: a raw request the agent answered from memory (its model
    wrote the JSON); ask once more for the API response itself."""
    text = (text or "").strip()
    if raw and text.lower().startswith("/raw"):
        text = text[4:].strip()
    if refetch:
        text = f"Call the API again now and return its response; don't reuse earlier results. {text}"
    return f"/raw {text}" if raw else text


# ─────────────────────────────── HTTP ───────────────────────────────

async def health(base: Optional[str] = None) -> Dict[str, Any]:
    """``{ok, http_status, duration_ms, tools_loaded, initialized, message}``."""
    base = base or agent_url()
    t0 = time.monotonic()
    out: Dict[str, Any] = {"ok": False, "http_status": None, "tools_loaded": None, "initialized": None, "message": ""}
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT, follow_redirects=False) as client:
            r = await client.get(f"{base}/api/health", headers=_headers())
        out["http_status"] = r.status_code
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        out["tools_loaded"] = data.get("tools_loaded")
        out["initialized"] = data.get("initialized")
        out["ok"] = r.status_code == 200 and data.get("status") == "ok"
        out["message"] = "Agent is up" if out["ok"] else f"Unexpected answer (HTTP {r.status_code})"
    except httpx.TimeoutException:
        out["message"] = "Timed out"
    except httpx.HTTPError as exc:
        out["message"] = f"Can't connect ({type(exc).__name__})"
    except ValueError:
        out["message"] = "The answer wasn't JSON"
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return out


async def chat(message: str, session: str, base: Optional[str] = None, timeout: float = CHAT_TIMEOUT) -> Dict[str, Any]:
    """POST /api/chat. Returns the agent's JSON plus ``ok``, ``http_status``, ``duration_ms`` and
    ``error`` (a short reason when not ok)."""
    base = base or agent_url()
    t0 = time.monotonic()
    out: Dict[str, Any] = {"ok": False, "http_status": None, "error": "", "reply": "", "tool_calls": [], "raw_json": None}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            r = await client.post(f"{base}/api/chat", json={"message": message, "session_id": session}, headers=_headers())
        out["http_status"] = r.status_code
        if r.status_code != 200:
            detail = ""
            try:
                detail = str(r.json().get("detail") or "")[:200]
            except ValueError:
                pass
            out["error"] = f"HTTP {r.status_code}" + (f": {detail}" if detail else "")
        else:
            data = r.json()
            out.update(reply=data.get("reply") or "", tool_calls=data.get("tool_calls") or [],
                       raw_json=data.get("raw_json"), tokens_used=data.get("tokens_used") or {}, ok=True)
            if (out["reply"] or "").startswith("LLM error:"):     # the agent reports its own model failures this way
                out["ok"], out["error"] = False, "the agent's model call failed"
    except httpx.TimeoutException:
        out["error"] = "timed out"
    except httpx.HTTPError as exc:
        out["error"] = f"can't connect ({type(exc).__name__})"
    except ValueError:
        out["error"] = "the answer wasn't JSON"
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return out


# ─────────────────────────────── results ───────────────────────────────

def call_summary(tool_calls: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The agent's API calls without their arguments or full paths (no addresses or hashes): for
    the activity log and the prompt."""
    return [{"tool": str(c.get("tool") or "?")[:120], "status": c.get("status_code"),
             "ok": bool(c.get("success")), "ms": c.get("duration_ms")} for c in tool_calls or []]


_FULL_ID = re.compile(r"(?:addr1|stake1|pool1|drep(?:_script)?1)[a-z0-9]{50,110}|[0-9a-f]{64}")


def looked_up_ids(tool_calls: List[Mapping[str, Any]]) -> List[str]:
    """Full identifiers the agent's successful calls were made with ("tx_utxos: hash=43ab…92b"), so
    the bot can write them in full even when the agent's answer shortened them. Prompt only, never
    logged."""
    out: List[str] = []
    for c in tool_calls or []:
        if not c.get("success") or not isinstance(c.get("arguments"), Mapping):
            continue
        for k, v in c["arguments"].items():
            if isinstance(v, str) and _FULL_ID.fullmatch(v.strip()):
                line = f"{str(c.get('tool') or '?')[:120]}: {str(k)[:40]}={v.strip()}"
                if line not in out:
                    out.append(line)
    return out[:20]


def payload_text(data: Any) -> Dict[str, Any]:
    """The payload as text exactly as the agent returned it, and whether the agent cut it.

    The agent passes each API response on as indented JSON; when it's over 50,000 characters it
    cuts it and adds a marker, so it no longer parses and arrives here as a string."""
    if isinstance(data, str):
        cut = data.endswith(AGENT_TRUNCATION_MARKER.strip()) or len(data) >= AGENT_TRUNCATION_LIMIT
        return {"text": data, "agent_truncated": cut, "is_json": False}
    return {"text": json.dumps(data, indent=2, ensure_ascii=False, default=str), "agent_truncated": False, "is_json": True}


def _fence_safe(text: str) -> str:
    # A backtick inside a string would end the Discord code block. "`" is the same JSON value.
    return text.replace("`", "\\u0060")


def _split(text: str, size: int) -> List[str]:
    parts = []
    while len(text) > size:
        end = text.rfind("\n", 0, size) + 1 or size
        parts.append(text[:end])
        text = text[end:]
    parts.append(text)
    return parts


def _label(item: Mapping[str, Any]) -> str:
    return str(item.get("tool") or "API response")


def raw_discord(raw_json: List[Mapping[str, Any]], *, at: str) -> Dict[str, Any]:
    """Discord messages for raw payloads: ``{messages, files}``.

    Each payload is posted exactly as received, split at line breaks into fenced parts labelled
    "part i/n". A payload that would need more than DISCORD_INLINE_MAX_PARTS messages is attached
    as a .json file instead, complete, with a message giving its size. If the agent itself cut a
    payload, the heading says so."""
    messages: List[str] = []
    files: List[Dict[str, Any]] = []
    total = len(raw_json)
    for n, item in enumerate(raw_json, 1):
        p = payload_text(item.get("data"))
        text = p["text"]
        which = f" ({n} of {total})" if total > 1 else ""
        head = (f"**Raw API response{which}** · `{_label(item)}` via GridScan Agent · fresh at {at} · "
                f"{len(text):,} characters, unmodified")
        if p["agent_truncated"]:
            head += ("\n⚠️ GridScan Agent cut this response at 50,000 characters before passing it on, so it "
                     "is incomplete (and not valid JSON). Ask for a narrower query, such as fewer results per page.")
        parts = _split(_fence_safe(text), DISCORD_PART_CHARS)
        if len(parts) > DISCORD_INLINE_MAX_PARTS:
            name = re.sub(r"[^A-Za-z0-9_.-]+", "-", _label(item))[:80].strip("-") or "response"
            files.append({"filename": f"{name}.json", "content": text.encode("utf-8")})
            messages.append(f"{head}\nToo long to post inline ({len(parts)} messages), so the complete response "
                            f"is attached as `{name}.json`.")
            continue
        for i, part in enumerate(parts, 1):
            label = head if i == 1 else ""
            if len(parts) > 1:
                label = f"{label}\nPart {i}/{len(parts)}" if label else f"Part {i}/{len(parts)}"
            messages.append(f"{label}\n```json\n{part}\n```")
    return {"messages": messages, "files": files}


def raw_text(raw_json: List[Mapping[str, Any]], *, at: str, markdown: bool) -> str:
    """Raw payloads for web chat (plain text) or the external API (Markdown), complete."""
    out = []
    total = len(raw_json)
    for n, item in enumerate(raw_json, 1):
        p = payload_text(item.get("data"))
        which = f" ({n} of {total})" if total > 1 else ""
        head = f"Raw API response{which}: {_label(item)} via GridScan Agent, fresh at {at}, {len(p['text']):,} characters, unmodified"
        if p["agent_truncated"]:
            head += (". GridScan Agent cut this response at 50,000 characters before passing it on, so it is "
                     "incomplete (and not valid JSON)")
        out.append(f"{head}\n```json\n{p['text']}\n```" if markdown else f"{head}\n{p['text']}")
    return "\n\n".join(out)


def no_payload_reply(result: Mapping[str, Any]) -> str:
    """A raw request where the agent made no API call (or none succeeded)."""
    calls = result.get("tool_calls") or []
    if calls:
        failed = ", ".join(f"{c.get('tool')} (HTTP {c.get('status_code')})" for c in calls[:3])
        return f"GridScan Agent's API call failed, so there's no response to show: {failed}."
    msg = "GridScan Agent didn't call the API for this message, so there's no raw response to show."
    reply = (result.get("reply") or "").strip()
    # A short question back (which address?) is useful. JSON the agent's model wrote from memory is not
    # an API response, so it is never shown as one.
    if reply and "```" not in reply and "{" not in reply:
        msg += f" GridScan Agent said: {reply[:600]}"
    else:
        msg += " Ask again with the address or hash, for example \"show me the raw JSON for transaction <hash>\"."
    return msg


def capability_note(tools_loaded: Optional[int]) -> str:
    n = f"all {tools_loaded}" if tools_loaded else "all of its"
    return ("Lookups go through GridScan Agent, which can call " + n + " Gridscan API endpoints for Cardano "
            "mainnet: addresses and their UTxOs and transactions, stake accounts and rewards, transactions, "
            "blocks, pools, epochs, assets, metadata, governance (DReps, proposals, committee) and network "
            "status. It remembers the addresses and hashes from earlier in this conversation, so the user can "
            "say \"that transaction\" or \"this address\". Users can also ask for the raw JSON of a lookup.")


def lookup_block(result: Mapping[str, Any], *, at: str, reply_format: str) -> str:
    """What the bot's prompt receives for an agent lookup: the calls it made and its answer."""
    calls = call_summary(result.get("tool_calls") or [])
    lines = ["Source: GridScan Agent (Gridscan API, Cardano mainnet)"]
    if not result.get("ok"):
        lines += [f"Status: lookup unavailable (GridScan Agent: {result.get('error') or 'no answer'})",
                  f"Reply format: {reply_format}"]
        return "\n".join(lines)
    if not calls:
        lines += ["Status: no fresh API call for this message. GridScan Agent answered from earlier lookups in "
                  "this conversation (real data, but not refreshed now); if you use its figures, say they are from "
                  "the earlier lookup. If it is asking for something (an address, a hash), pass that question on.",
                  f"Reply format: {reply_format}",
                  "AGENT ANSWER (from earlier lookups in this conversation; use its figures, addresses, hashes and "
                  "units exactly):",
                  (result.get("reply") or "").strip()[:12000]]
        return "\n".join(lines)
    ok = [c for c in calls if c["ok"]]
    status = "ok" if ok and len(ok) == len(calls) else ("partly failed" if ok else "lookup unavailable")
    ids = looked_up_ids(result.get("tool_calls") or [])
    lines += [f"Status: {status} (fresh lookup at {at})",
              "API calls: " + "; ".join(f"{c['tool']} → HTTP {c['status']}" for c in calls)]
    if ids:
        lines.append("Looked up (full values): " + "; ".join(ids))
    lines += [f"Reply format: {reply_format}",
              "AGENT ANSWER (written by GridScan Agent from the API responses above; use its figures, "
              "addresses, hashes and units exactly; if it includes JSON the user asked for, reproduce that "
              "JSON unchanged):",
              (result.get("reply") or "").strip()[:12000]]
    return "\n".join(lines)
