"""API Integrations: read-only lookups (spec 015).

Pure functions plus one guarded HTTP call; no database access. server.py stores the
configuration and calls :func:`run_lookup` from both the admin tester and the chat paths, so the
tester shows exactly what the bot receives.

Ported from BitBotPro (origin/bitbotpro-v1.0.0):
- outbound guard: ``1c20d2a:backend/outbound_guard.py`` (URL policy, public-address check of every
  DNS answer, connection pinned to the checked address, no redirects, header sanitising);
- import and request building: ``02bbd40:backend/runtime/api_calls.py`` (``parse_openapi``,
  ``parse_reference``, ``build_request``, ``auth_for``).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit

MAX_ENDPOINTS = 500
MAX_RESPONSE_SIZE = 500_000
REQUEST_TIMEOUT = 10.0
MAX_FIELDS = 30
RAW_PREVIEW_CHARS = 100_000
VALUE_CHARS = 300
AUTH_TYPES = ("none", "header", "bearer", "query")
INPUT_SOURCES = ("tx_hash", "fixed", "none")
FIELD_FORMATS = ("auto", "number", "text", "timestamp")
INPUT_PLACEHOLDER = "$input"


class ConfigError(ValueError):
    """Invalid configuration or input. ``str(exc)`` is safe to show."""


# ═════════════════════════════ outbound guard (from 1c20d2a) ═════════════════════════════

class OutboundBlocked(ValueError):
    """A destination is not allowed. ``str(exc)`` is safe to show."""


class OutboundError(Exception):
    """The request failed after the policy allowed it. ``str(exc)`` is a fixed message."""


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")
_INTERNAL_SUFFIXES = ("localhost", "local", "internal", "localdomain", "home.arpa", "lan", "intranet")
_METADATA_ADDRESSES = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "fd00:ec2::254"))
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
FORBIDDEN_HEADER_NAMES = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade", "te", "trailer",
    "expect", "proxy-authorization", "proxy-authenticate", "proxy-connection", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-port", "x-original-url", "x-rewrite-url", "forwarded",
    "x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip", "x-client-ip", "cookie",
})
DESTINATION_NOT_ALLOWED = "The destination address is not allowed."


def canonical_host(host: Any) -> str:
    """Canonical public host name or IP literal, or raise OutboundBlocked. Non-canonical IPv4
    spellings (2130706433, 0x7f.1, 127.1), zone IDs and internal names are rejected."""
    if not isinstance(host, str) or not host or not host.isascii():
        raise OutboundBlocked("Invalid host name.")
    h = host.lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if "%" in h:
        raise OutboundBlocked("Invalid host name.")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 4 and str(ip) != h:
            raise OutboundBlocked("Invalid IP address format.")
        if not address_allowed(ip):
            raise OutboundBlocked(DESTINATION_NOT_ALLOWED)
        return h if ip.version == 4 else f"[{ip.compressed}]"
    if ":" in h:
        raise OutboundBlocked("Invalid IP address format.")
    labels = h.split(".")
    if len(h) > 253 or not all(_LABEL.fullmatch(lbl) for lbl in labels):
        raise OutboundBlocked("Invalid host name.")
    if re.fullmatch(r"(?:0x[0-9a-f]*|[0-9]+)", labels[-1]):
        raise OutboundBlocked("Invalid IP address format.")
    if len(labels) < 2 or not _TLD.fullmatch(labels[-1]) or \
            any(h == sfx or h.endswith("." + sfx) for sfx in _INTERNAL_SUFFIXES):
        raise OutboundBlocked("Internal host names are not allowed.")
    return h


def address_allowed(addr: Any) -> bool:
    """True only for public unicast addresses (IPv4 embedded in IPv6 is checked too)."""
    try:
        ip = addr if isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else \
            ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    candidates = [ip]
    if ip.version == 6 and ip.ipv4_mapped is not None:
        candidates = [ip.ipv4_mapped]
    elif ip.version == 6:
        for embedded in (ip.sixtofour, ip.teredo[1] if ip.teredo else None):
            if embedded is not None:
                candidates.append(embedded)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            candidates.append(ipaddress.ip_address(int(ip) & 0xFFFFFFFF))
    for c in candidates:
        if c in _METADATA_ADDRESSES or c.is_unspecified or c.is_multicast or c.is_link_local \
                or c.is_loopback or c.is_reserved or not c.is_global:
            return False
    return True


def parse_base_url(raw: Any) -> Dict[str, str]:
    """``{origin, base_path, host, port}`` for an integration base URL: https, public host,
    no credentials, query or fragment."""
    if not isinstance(raw, str) or not raw.strip():
        raise OutboundBlocked("A base URL is required.")
    s = raw.strip()
    if not s.isascii() or any(ord(c) < 0x21 or ord(c) == 0x7F or c in "\\#" for c in s):
        raise OutboundBlocked("Invalid URL.")
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        raise OutboundBlocked("Invalid URL.")
    if (parts.scheme or "").lower() != "https":
        raise OutboundBlocked("Only https:// URLs are allowed.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise OutboundBlocked("URLs must not contain credentials.")
    if parts.query or s.endswith("?"):
        raise OutboundBlocked("The base URL must not contain a query string.")
    host = canonical_host(parts.hostname or "")
    port = port or 443
    if not 0 < port < 65536:
        raise OutboundBlocked("Invalid port.")
    base_path = (parts.path or "").rstrip("/")
    if base_path and (not _SAFE_PATH.fullmatch(base_path) or "//" in base_path or "{" in base_path
                      or any(seg in ("..", ".") for seg in base_path.split("/"))):
        raise OutboundBlocked("Invalid base URL path.")
    origin = "https://" + host + ("" if port == 443 else f":{port}")
    return {"origin": origin, "base_path": base_path, "host": host, "port": port}


def validate_header_name(name: Any) -> str:
    if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name.strip()):
        raise OutboundBlocked("Invalid header name.")
    name = name.strip()
    if name.lower() in FORBIDDEN_HEADER_NAMES or name.lower().startswith(("proxy-", "sec-")):
        raise OutboundBlocked(f"The header '{name}' cannot be set.")
    return name


def _sanitize_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    out = {}
    for name, value in headers.items():
        name = validate_header_name(name)
        if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise OutboundBlocked(f"Invalid value for header '{name}'.")
        out[name] = value
    return out


async def _system_resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _pinned_address(host: str, port: int, resolver=None) -> str:
    """Resolve once; every answer must be public, then connect to the first one."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        addrs: Sequence[str] = [ipaddress.ip_address(bare).compressed]
    except ValueError:
        try:
            addrs = await (resolver or _system_resolve)(bare, port)
        except (OSError, UnicodeError):
            raise OutboundError("The API host name could not be resolved.")
    addrs = list(dict.fromkeys(a.split("%", 1)[0] for a in addrs))
    if not addrs or not all(address_allowed(a) for a in addrs):
        raise OutboundBlocked(DESTINATION_NOT_ALLOWED)
    return addrs[0]


def _default_client(timeout: float):
    import httpx
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)


# Tests replace these to run without network access.
NETWORK: Dict[str, Any] = {"resolver": None, "client_factory": None}


async def guarded_get(origin: str, host: str, port: int, target: str, headers: Mapping[str, str],
                      *, timeout: float = REQUEST_TIMEOUT, max_size: int = MAX_RESPONSE_SIZE) -> Dict[str, Any]:
    """One GET to ``origin + target``, connected to a checked public address of ``host``.
    Returns ``{status, content, truncated, content_type}``; raises OutboundBlocked/OutboundError."""
    import httpx
    address = await _pinned_address(host, port, NETWORK["resolver"])
    ip = ipaddress.ip_address(address)
    ip_host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    send = {**_sanitize_headers(headers), "Host": urlsplit(origin).netloc}
    client = (NETWORK["client_factory"] or _default_client)(timeout)
    try:
        async with client:
            # Connect to the checked IP; TLS SNI and certificate verification use the host name.
            async with client.stream("GET", f"https://{ip_host}:{port}{target}", headers=send,
                                     extensions={"sni_hostname": host.strip("[]")}) as resp:
                body = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_size:
                        truncated = True
                        break
                return {"status": resp.status_code, "content": bytes(body[:max_size]), "truncated": truncated,
                        "content_type": resp.headers.get("content-type", "")}
    except httpx.TimeoutException:
        raise OutboundError("The API did not respond in time.")
    except httpx.ConnectError as exc:
        text = str(exc).upper()
        if "CERTIFICATE" in text or "SSL" in text or "TLS" in text:
            raise OutboundError("TLS certificate verification failed.")
        raise OutboundError("Could not connect to the API.")
    except httpx.HTTPError:
        raise OutboundError("The request to the API failed.")


# ═════════════════════════════ import (from 02bbd40) ═════════════════════════════

_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/{}\-]*")


def _safe_endpoint_path(path: Any) -> Optional[str]:
    p = str(path or "")[:500]
    if not p.startswith("/"):
        p = "/" + p
    if not _SAFE_PATH.fullmatch(p) or "//" in p or any(seg in ("..", ".") for seg in p.split("/")):
        return None
    return p


def _resolve_ref(ref: Any, root: dict) -> dict:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    node: Any = root
    for part in ref[2:].split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def _endpoint(method: str, path: str, op: dict) -> dict:
    return {"method": method.upper(), "path": path,
            "summary": str(op.get("summary") or op.get("title") or "")[:300],
            "description": str(op.get("description") or "")[:1000], "parameters": []}


def _param(p: dict, path: str) -> Optional[dict]:
    name = str(p.get("name") or "")[:80]
    where = p.get("in") or ("path" if "{" + name + "}" in path else "query")
    if not name or where not in ("path", "query"):
        return None
    schema = p.get("schema") if isinstance(p.get("schema"), dict) else {}
    return {"name": name, "in": where, "required": bool(p.get("required", where == "path")),
            "type": str(schema.get("type") or p.get("type") or "string")[:20],
            "description": str(p.get("description") or "")[:300]}


def parse_openapi(spec: Any) -> List[dict]:
    """OpenAPI 3.x / Swagger 2.0 JSON → endpoints (all methods listed; only GET selectable)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise ConfigError("This is not an OpenAPI/Swagger document (no 'paths').")
    out: List[dict] = []
    for raw_path, item in spec["paths"].items():
        path = _safe_endpoint_path(raw_path)
        if path is None or not isinstance(item, dict):
            continue
        shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            ep = _endpoint(method, path, op)
            own = op.get("parameters") if isinstance(op.get("parameters"), list) else []
            for p in shared + own:
                if isinstance(p, dict) and "$ref" in p:
                    p = _resolve_ref(p["$ref"], spec)
                if isinstance(p, dict) and (q := _param(p, path)):
                    ep["parameters"].append(q)
            out.append(ep)
            if len(out) >= MAX_ENDPOINTS:
                return out
    if not out:
        raise ConfigError("No endpoints were found in the document.")
    return out


def parse_reference(data: Any) -> List[dict]:
    """Custom JSON reference: a list of ``{path, method, summary, parameters: [{name, in,
    required, description}]}``, or ``{"endpoints": [...]}``."""
    items = data if isinstance(data, list) else (data.get("endpoints") if isinstance(data, dict) else None)
    if isinstance(data, dict) and items is None:
        items = [x for v in data.values() if isinstance(v, list) for x in v]
    out: List[dict] = []
    for item in items or []:
        if not isinstance(item, dict) or not (item.get("path") or item.get("endpoint")):
            continue
        path = _safe_endpoint_path(item.get("path") or item.get("endpoint"))
        if path is None:
            continue
        ep = _endpoint(str(item.get("method") or "GET"), path, item)
        for p in item.get("parameters") or item.get("params") or []:
            if isinstance(p, dict) and (q := _param(p, path)):
                ep["parameters"].append(q)
        out.append(ep)
        if len(out) >= MAX_ENDPOINTS:
            break
    if not out:
        raise ConfigError("No endpoints were found in the document.")
    return out


def parse_document(doc: Any) -> Tuple[str, List[dict]]:
    """("openapi" | "reference", endpoints). Each endpoint gets a stable id from method + path."""
    kind = "openapi" if isinstance(doc, dict) and isinstance(doc.get("paths"), dict) else "reference"
    eps = parse_openapi(doc) if kind == "openapi" else parse_reference(doc)
    seen, unique = set(), []
    for ep in eps:
        key = f"{ep['method']} {ep['path']}"
        if key not in seen:
            seen.add(key)
            unique.append({**ep, "id": key})
    return kind, unique


# ═════════════════════════════ lookup configuration ═════════════════════════════

_QUERY_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_FIELD_PATH = re.compile(r"[^.\[\]]+|\[\d+\]")


def validate_auth(auth_type: Any, auth_name: Any) -> Tuple[str, str]:
    auth_type = auth_type or "none"
    if auth_type not in AUTH_TYPES:
        raise ConfigError("Unknown authentication type.")
    name = (auth_name or "").strip()
    if auth_type == "header":
        name = validate_header_name(name or "")
        if name.lower() == "authorization":
            name = "Authorization"
    elif auth_type == "query":
        if not _QUERY_NAME.fullmatch(name):
            raise ConfigError("Enter the query parameter name for the API key.")
    else:
        name = ""
    return auth_type, name


def field_path_tokens(path: str) -> List[Any]:
    """'0.fee' / '[0].fee' / 'outputs[0].amount' → [0, 'fee'] / ['outputs', 0, 'amount']."""
    if not isinstance(path, str) or not path.strip() or len(path) > 200:
        raise ConfigError("Enter a field path, e.g. 0.fee or outputs[0].value.")
    tokens: List[Any] = []
    for tok in _FIELD_PATH.findall(path.strip()):
        if tok.startswith("["):
            tokens.append(int(tok[1:-1]))
        else:
            tokens.append(int(tok) if tok.isdigit() else tok)
    if not tokens:
        raise ConfigError("Enter a field path, e.g. 0.fee or outputs[0].value.")
    return tokens


def validate_lookup(lookup: Mapping[str, Any], endpoints: Mapping[str, dict], *, require_fields: bool = True) -> dict:
    """Normalised lookup config, or ConfigError. The endpoint must be an imported GET endpoint;
    every path parameter needs a value source; exactly one input must be the transaction hash."""
    name = str(lookup.get("name") or "").strip()[:80]
    if not name and require_fields:
        raise ConfigError("Give the lookup a name.")
    ep = endpoints.get(str(lookup.get("endpoint_id") or ""))
    if ep is None:
        raise ConfigError("Choose an imported endpoint.")
    if ep["method"] != "GET":
        raise ConfigError("Only GET endpoints can be used for lookups.")
    given = {str(i.get("param")): i for i in lookup.get("inputs") or [] if isinstance(i, Mapping)}
    inputs = []
    for p in ep["parameters"]:
        i = given.get(p["name"], {})
        source = i.get("source") or "none"
        if source not in INPUT_SOURCES:
            raise ConfigError(f"Unknown value source for '{p['name']}'.")
        template = str(i.get("template") or INPUT_PLACEHOLDER)[:200]
        value = str(i.get("value") or "")[:500]
        if source == "tx_hash" and INPUT_PLACEHOLDER not in template:
            raise ConfigError(f"The template for '{p['name']}' must contain {INPUT_PLACEHOLDER}.")
        if source == "fixed" and not value:
            raise ConfigError(f"Enter a fixed value for '{p['name']}'.")
        if source == "none" and p["in"] == "path":
            raise ConfigError(f"The path parameter '{p['name']}' needs a value.")
        inputs.append({"param": p["name"], "in": p["in"], "source": source,
                       "template": template if source == "tx_hash" else INPUT_PLACEHOLDER,
                       "value": value if source == "fixed" else ""})
    if sum(i["source"] == "tx_hash" for i in inputs) != 1:
        raise ConfigError("Exactly one parameter must take the transaction hash from the message.")
    fields = []
    for f in (lookup.get("fields") or [])[:MAX_FIELDS]:
        if not isinstance(f, Mapping):
            continue
        path = str(f.get("path") or "").strip()
        field_path_tokens(path)
        fmt = f.get("format") or "auto"
        if fmt not in FIELD_FORMATS:
            raise ConfigError(f"Unknown format for '{path}'.")
        try:
            scale = int(f.get("scale") or 0)
        except (TypeError, ValueError):
            raise ConfigError(f"The decimal shift for '{path}' must be a whole number.")
        if not 0 <= scale <= 18:
            raise ConfigError(f"The decimal shift for '{path}' must be between 0 and 18.")
        fields.append({"path": path, "label": (str(f.get("label") or "").strip() or path)[:80],
                       "unit": str(f.get("unit") or "").strip()[:20], "scale": scale, "format": fmt})
    if not fields and require_fields:
        raise ConfigError("Choose at least one response field to show.")
    return {"name": name, "kind": "transaction", "endpoint_id": ep["id"], "method": "GET", "path": ep["path"],
            "inputs": inputs, "fields": fields, "enabled": bool(lookup.get("enabled", True))}


# ═════════════════════════════ input extraction ═════════════════════════════

_TX_HASH = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


def extract_tx_hashes(text: Any) -> List[str]:
    """Distinct 64-character hex strings that aren't part of a longer hex run, lower-cased."""
    if not isinstance(text, str):
        return []
    return list(dict.fromkeys(m.group(0).lower() for m in _TX_HASH.finditer(text[:20000])))


def tester_input(text: Any) -> str:
    """The hash for the admin tester: exactly one in the text, else ConfigError."""
    hashes = extract_tx_hashes(text)
    if not hashes:
        raise ConfigError("No transaction hash found. Enter a 64-character hexadecimal transaction hash.")
    if len(hashes) > 1:
        raise ConfigError("More than one transaction hash found. Enter one.")
    return hashes[0]


# ═════════════════════════════ request ═════════════════════════════

def build_request(integration: Mapping[str, Any], lookup: Mapping[str, Any], tx_hash: str,
                  secret: Optional[str]) -> Dict[str, Any]:
    """``{origin, host, port, target, headers, display_url}`` for one lookup call. Path values
    are percent-encoded (they can't add segments, a host or a query); the URL must stay on the
    integration's origin and under its base path; credentials go only to that origin."""
    if not re.fullmatch(r"[0-9a-f]{64}", tx_hash or ""):
        raise ConfigError("Invalid transaction hash.")
    base = parse_base_url(integration.get("base_url"))
    path = _safe_endpoint_path(lookup.get("path"))
    if path is None:
        raise ConfigError("The endpoint path is not valid.")
    query: List[Tuple[str, str]] = []
    for i in lookup.get("inputs") or []:
        if i["source"] == "none":
            continue
        value = i["template"].replace(INPUT_PLACEHOLDER, tx_hash) if i["source"] == "tx_hash" else i["value"]
        if i["in"] == "path":
            path = path.replace("{" + i["param"] + "}", quote(value, safe=""))
        else:
            query.append((i["param"], value[:500]))
    if re.search(r"\{[^}]*\}", path):
        raise ConfigError("A path parameter has no value.")
    target = base["base_path"] + path
    display = target + ("?" + urlencode(query) if query else "")
    headers = {"Accept": "application/json", "User-Agent": "BridgeBot-API-Lookup/1.0"}
    auth_type = integration.get("auth_type") or "none"
    if secret:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_type == "header":
            headers[validate_header_name(integration.get("auth_name") or "")] = secret
        elif auth_type == "query":
            query.append((integration.get("auth_name") or "api_key", secret))
    target += "?" + urlencode(query) if query else ""
    # Re-parse the final URL: same origin, under the base path.
    final = urlsplit(base["origin"] + target)
    if final.scheme != "https" or f"https://{final.netloc}" != base["origin"] or \
            not (final.path == base["base_path"] + path):
        raise OutboundBlocked("The endpoint does not resolve to the integration's base URL.")
    return {"origin": base["origin"], "host": base["host"], "port": base["port"], "target": target,
            "headers": headers, "display_url": base["origin"] + display}


# ═════════════════════════════ formatting ═════════════════════════════

_MISSING = object()


def get_path(data: Any, path: str) -> Any:
    node = data
    for tok in field_path_tokens(path):
        if isinstance(tok, int) and isinstance(node, list):
            node = node[tok] if -len(node) <= tok < len(node) else _MISSING
        elif isinstance(node, dict):
            node = node.get(str(tok), _MISSING)
        else:
            node = _MISSING
        if node is _MISSING:
            return _MISSING
    return node


def _number(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip())):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation:
            return None
    return None


def format_value(value: Any, field: Mapping[str, Any]) -> str:
    fmt = field.get("format") or "auto"
    if value is None:
        return "none"
    if fmt == "timestamp":
        n = _number(value)
        if n is not None and 0 <= n < 10**11:
            return datetime.fromtimestamp(int(n), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        fmt = "text"
    if fmt in ("auto", "number"):
        n = _number(value)
        if n is not None:
            scale = int(field.get("scale") or 0)
            n = n.scaleb(-scale) if scale else n
            text = f"{n:,f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return text
        if fmt == "number":
            fmt = "text"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:VALUE_CHARS]
    return str(value)[:VALUE_CHARS]


def format_fields(data: Any, fields: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for f in fields:
        value = get_path(data, f["path"])
        found = value is not _MISSING
        out.append({"label": f["label"], "path": f["path"], "unit": f.get("unit") or "", "found": found,
                    "value": format_value(value, f) if found else None})
    return out


def fields_text(rows: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(f"{r['label']}: " + (f"{r['value']}{' ' + r['unit'] if r['unit'] else ''}"
                                          if r["found"] else "not available") for r in rows)


# ═════════════════════════════ the lookup (tester and bot) ═════════════════════════════

async def run_lookup(integration: Mapping[str, Any], lookup: Mapping[str, Any], tx_hash: str,
                     secret: Optional[str], *, include_raw: bool = False) -> Dict[str, Any]:
    """Call the lookup's endpoint for ``tx_hash`` and format the selected fields.

    ``status``: ok | not_found | api_error | blocked | invalid. ``text`` is what the bot gets
    (selected fields only). ``raw`` (tester only) is the parsed response, truncated for display.
    """
    result: Dict[str, Any] = {"status": "invalid", "message": "", "tx_hash": tx_hash, "http_status": None,
                              "duration_ms": 0, "request_url": None, "fields": [], "text": ""}
    try:
        req = build_request(integration, lookup, tx_hash, secret)
    except (ConfigError, OutboundBlocked) as exc:
        result["message"] = str(exc)
        return result
    result["request_url"] = req["display_url"]
    started = time.monotonic()
    try:
        resp = await guarded_get(req["origin"], req["host"], req["port"], req["target"], req["headers"])
    except OutboundBlocked as exc:
        result.update(status="blocked", message=str(exc))
        return result
    except OutboundError as exc:
        result.update(status="api_error", message=str(exc))
        return result
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
    status = resp["status"]
    result["http_status"] = status
    if resp["truncated"]:
        result.update(status="api_error", message=f"The response was larger than {MAX_RESPONSE_SIZE:,} bytes.")
        return result
    if 300 <= status < 400:
        result.update(status="api_error", message=f"The API redirected (HTTP {status}); redirects are not followed.")
        return result
    if status == 404:
        result.update(status="not_found", message="The API found no data for this transaction (HTTP 404).")
        return result
    if status >= 400:
        result.update(status="api_error", message=f"The API returned an error (HTTP {status}).")
        return result
    try:
        data = json.loads(resp["content"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        result.update(status="api_error", message="The API did not return JSON.")
        return result
    if include_raw:
        raw_text = json.dumps(data, ensure_ascii=False, indent=2)
        result["raw"] = raw_text[:RAW_PREVIEW_CHARS]
        result["raw_truncated"] = len(raw_text) > RAW_PREVIEW_CHARS
        result["paths"] = leaf_paths(data)
    rows = format_fields(data, lookup.get("fields") or [])
    result["fields"] = rows
    if data in ([], {}, None) or (rows and not any(r["found"] for r in rows)):
        result.update(status="not_found", message="The API returned no data for this transaction.")
        return result
    result.update(status="ok", message="", text=fields_text(rows))
    return result


def bot_block(lookup_name: str, result: Mapping[str, Any]) -> str:
    """The text the bot receives: selected fields, or a short status. Never raw data, URLs or keys."""
    h = result.get("tx_hash") or ""
    if result.get("status") == "ok":
        body = f"Transaction {h}\n{result['text']}"
    elif result.get("status") == "not_found":
        body = f"Transaction {h}\nStatus: not found — {result.get('message')}"
    else:
        body = f"Transaction {h}\nStatus: lookup failed — {result.get('message') or 'the API could not be reached'}"
    return f"Lookup: {lookup_name}\n{body}"


def leaf_paths(data: Any, limit: int = 200) -> List[Dict[str, Any]]:
    """Scalar leaf paths of a response (first list item only), for the field picker."""
    out: List[Dict[str, Any]] = []

    def walk(node: Any, prefix: str, depth: int) -> None:
        if len(out) >= limit or depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k), depth + 1)
        elif isinstance(node, list):
            if node:
                walk(node[0], f"{prefix}[0]" if prefix else "0", depth + 1)
        else:
            out.append({"path": prefix, "sample": format_value(node, {"format": "text"})[:80]})

    walk(data, "", 0)
    return out
