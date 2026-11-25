"""API Integrations: read-only lookups (spec 015).

Pure functions plus one guarded HTTP call; no database access. server.py stores the
configuration and calls :func:`run_lookup` from both the admin tester and the chat paths, so the
tester shows exactly what the bot receives.

Ported from BitBotPro (origin/bitbotpro-v1.0.0):
- outbound guard: ``1c20d2a:backend/outbound_guard.py`` (URL policy, public-address check of every
  DNS answer, connection pinned to the checked address, no redirects, header sanitising);
- import and request building: ``02bbd40:backend/runtime/api_calls.py`` (``parse_openapi``,
  ``parse_reference``, ``build_request``, ``auth_for``).

Spec 017 adds, with BitBotPro 1c20d2a (``api_integrations_routes.py`` discovery and
``test_connection``, ``api_diagnostics.py`` schema validation) as the reference: import from a
documentation URL, documented response schemas, import change reports, a connection check that
reports host reachability and credential verification separately, observed endpoint outcomes and a
response-quality comparison of documented fields with the actual JSON.
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
# How the API reports an unknown transaction: only an empty result, or also HTTP 404 (e.g. Blockfrost).
NOT_FOUND_MODES = ("empty", "empty_or_404")
MAX_DOC_SIZE = 2_000_000          # documentation download / upload
DOC_TIMEOUT = 20.0
SCHEMA_MAX_DEPTH = 10
SCHEMA_MAX_NODES = 2000           # per endpoint response schema
QUALITY_MAX_ITEMS = 50            # array items compared per array
QUALITY_MAX_ROWS = 400


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
            "description": str(op.get("description") or "")[:1000], "parameters": [],
            "response_schema": None, "requires_auth": None}


# ── documented response schemas (reference: 1c20d2a api_integrations._flatten_schema) ──

_JSON_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


def normalize_schema(schema: Any, root: dict, *, _depth: int = 0, _refs: Tuple[str, ...] = (),
                     _budget: Optional[List[int]] = None) -> Optional[dict]:
    """A compact JSON-schema node for response comparison, or None when nothing is documented:
    ``{types, nullable, properties, required, open, items}``. ``types`` is empty when any type
    is allowed. ``$ref`` (cycles stop), ``allOf`` (merged), ``oneOf``/``anyOf`` (a single
    non-null variant becomes nullable; otherwise the union of types, not descended),
    OpenAPI 3.1 type lists, ``nullable`` and Swagger ``x-nullable`` are handled."""
    budget = _budget if _budget is not None else [SCHEMA_MAX_NODES]
    if not isinstance(schema, dict) or _depth > SCHEMA_MAX_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in _refs:
            return {"types": [], "nullable": False}
        target = _resolve_ref(ref, root)
        if not target:
            return None
        node = normalize_schema(target, root, _depth=_depth + 1, _refs=_refs + (ref,), _budget=budget)
        if node is not None and schema.get("nullable") is True:
            node["nullable"] = True
        return node
    kw = dict(_depth=_depth + 1, _refs=_refs, _budget=budget)
    if isinstance(schema.get("allOf"), list):
        merged: dict = {"types": [], "nullable": False}
        props: dict = {}
        required: List[str] = []
        for sub in schema["allOf"][:10]:
            n = normalize_schema(sub, root, **kw)
            if not n:
                continue
            props.update(n.get("properties") or {})
            required += [r for r in n.get("required") or [] if r not in required]
            if n.get("types") and not merged["types"]:
                merged["types"] = n["types"]
            if n.get("items") is not None:
                merged["items"] = n["items"]
            merged["open"] = merged.get("open", False) or n.get("open", False)
        own = normalize_schema({k: v for k, v in schema.items() if k != "allOf"}, root, **kw) \
            if any(k in schema for k in ("properties", "type")) else None
        if own:
            props.update(own.get("properties") or {})
            required += [r for r in own.get("required") or [] if r not in required]
        if props:
            merged.update(types=["object"], properties=props, required=required)
        return merged
    variants = schema.get("oneOf") if isinstance(schema.get("oneOf"), list) else schema.get("anyOf")
    if isinstance(variants, list):
        nodes = [normalize_schema(v, root, **kw) for v in variants[:10]]
        nulls = [n for n in nodes if n and n.get("types") == ["null"]]
        rest = [n for n in nodes if n and n.get("types") != ["null"]]
        if len(rest) == 1:
            node = dict(rest[0])
            node["nullable"] = bool(nulls) or node.get("nullable", False)
            return node
        if any(not n or not n.get("types") for n in rest):
            return {"types": [], "nullable": bool(nulls)}
        types = sorted({t for n in rest for t in n["types"]})
        return {"types": types, "nullable": bool(nulls) or any(n.get("nullable") for n in rest)}
    raw_type = schema.get("type")
    types = [raw_type] if isinstance(raw_type, str) else [t for t in raw_type if isinstance(t, str)] \
        if isinstance(raw_type, list) else []
    types = [t for t in types if t in _JSON_TYPES]
    nullable = "null" in types or schema.get("nullable") is True or schema.get("x-nullable") is True
    types = [t for t in types if t != "null"] or ([] if not ("null" in types and len(types) == 1) else ["null"])
    if not types and isinstance(schema.get("properties"), dict):
        types = ["object"]
    if not types and "items" in schema:
        types = ["array"]
    node: dict = {"types": types, "nullable": nullable}
    if "object" in types:
        props = {}
        for name, sub in list((schema.get("properties") or {}).items())[:300]:
            child = normalize_schema(sub, root, **kw)
            props[str(name)[:120]] = child if child is not None else {"types": [], "nullable": True}
        node["properties"] = props
        req = schema.get("required")
        node["required"] = [r for r in req if isinstance(r, str) and r in props] if isinstance(req, list) else []
        addl = schema.get("additionalProperties")
        node["open"] = not props and (addl is True or isinstance(addl, dict) or addl is None)
    if "array" in types:
        node["items"] = normalize_schema(schema.get("items"), root, **kw)
    return node


def _response_schema(op: dict, root: dict, swagger2: bool) -> Optional[dict]:
    """The documented JSON schema of the first success response (200, 201, other 2xx, default)."""
    responses = op.get("responses") if isinstance(op.get("responses"), dict) else {}
    codes = [c for c in ("200", "201") if c in responses] + \
        sorted(str(c) for c in responses if str(c).startswith("2") and str(c) not in ("200", "201"))
    if "default" in responses:
        codes.append("default")
    for code in codes:
        resp = responses.get(code) if code in responses else responses.get(int(code)) if code.isdigit() else None
        if isinstance(resp, dict) and "$ref" in resp:
            resp = _resolve_ref(resp["$ref"], root)
        if not isinstance(resp, dict):
            continue
        raw = None
        if swagger2:
            raw = resp.get("schema")
        else:
            content = resp.get("content") if isinstance(resp.get("content"), dict) else {}
            media = content.get("application/json") or next(
                (v for k, v in content.items() if "json" in str(k).lower()), None)
            raw = media.get("schema") if isinstance(media, dict) else None
        node = normalize_schema(raw, root)
        if node is not None:
            return node
    return None


def _requires_auth(op: dict, root: dict) -> Optional[bool]:
    """True/False from the operation's (or the document's) security requirement; None if unknown."""
    sec = op.get("security", root.get("security"))
    if not isinstance(sec, list):
        return None
    if not sec or any(isinstance(r, dict) and not r for r in sec):
        return False
    return True


def schema_field_count(node: Optional[dict]) -> int:
    if not isinstance(node, dict):
        return 0
    n = 0
    for child in (node.get("properties") or {}).values():
        n += 1 + schema_field_count(child)
    return n + schema_field_count(node.get("items"))


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
            ep["response_schema"] = _response_schema(op, spec, str(spec.get("swagger", "")).startswith("2"))
            ep["requires_auth"] = _requires_auth(op, spec)
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


# ═════════════════════════════ import from a URL, change report (spec 017) ═════════════════════════════

def parse_doc_url(raw: Any) -> Dict[str, Any]:
    """``{origin, host, port, target, url}`` for a documentation URL: the same rules as a base URL
    (https, public host, no credentials or fragment) but a query string is allowed."""
    if not isinstance(raw, str) or not raw.strip():
        raise OutboundBlocked("A documentation URL is required.")
    s = raw.strip()
    if len(s) > 2000 or not s.isascii() or any(ord(c) < 0x21 or ord(c) == 0x7F or c in "\\#" for c in s):
        raise OutboundBlocked("Invalid URL.")
    try:
        parts = urlsplit(s)
    except ValueError:
        raise OutboundBlocked("Invalid URL.")
    base = parse_base_url(f"{parts.scheme}://{parts.netloc}")
    path = parts.path or "/"
    if not _SAFE_PATH.fullmatch(path) or "//" in path or any(seg in ("..", ".") for seg in path.split("/")):
        raise OutboundBlocked("Invalid documentation URL path.")
    target = path + ("?" + parts.query if parts.query else "")
    return {"origin": base["origin"], "host": base["host"], "port": base["port"], "target": target,
            "url": base["origin"] + target}


def _auth_parts(integration: Mapping[str, Any], secret: Optional[str]) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Credential headers and query pairs for the integration's auth type (empty without a secret)."""
    headers: Dict[str, str] = {}
    query: List[Tuple[str, str]] = []
    auth_type = integration.get("auth_type") or "none"
    if secret:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_type == "header":
            headers[validate_header_name(integration.get("auth_name") or "")] = secret
        elif auth_type == "query":
            query.append((integration.get("auth_name") or "api_key", secret))
    return headers, query


async def fetch_document(integration: Mapping[str, Any], doc: Mapping[str, Any], secret: Optional[str]) -> Any:
    """Download and decode a JSON documentation file through the outbound guard. The key is sent
    only when the document is on the integration's own origin. Raises ConfigError,
    OutboundBlocked or OutboundError, all with messages safe to show."""
    headers = {"Accept": "application/json", "User-Agent": "BridgeBot-API-Import/1.0"}
    target = doc["target"]
    try:
        same_origin = parse_base_url(integration.get("base_url"))["origin"] == doc["origin"]
    except OutboundBlocked:
        same_origin = False
    if same_origin:
        auth_headers, auth_query = _auth_parts(integration, secret)
        headers.update(auth_headers)
        if auth_query:
            target += ("&" if "?" in target else "?") + urlencode(auth_query)
    resp = await guarded_get(doc["origin"], doc["host"], doc["port"], target, headers,
                             timeout=DOC_TIMEOUT, max_size=MAX_DOC_SIZE)
    if resp["truncated"]:
        raise ConfigError(f"The documentation is larger than {MAX_DOC_SIZE // 1_000_000} MB.")
    if 300 <= resp["status"] < 400:
        raise ConfigError(f"The documentation URL redirected (HTTP {resp['status']}); redirects are not "
                          "followed. Use the final URL.")
    if resp["status"] != 200:
        raise ConfigError(f"The documentation URL returned HTTP {resp['status']}.")
    try:
        return json.loads(resp["content"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ConfigError("The documentation is not valid JSON. YAML isn't supported; use the JSON version.")


_DIFF_KEYS = ("summary", "description", "parameters", "response_schema", "requires_auth")


def diff_endpoints(old: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Endpoint ids that are new, changed or removed by a re-import, and how many are unchanged."""
    def fp(e):
        return json.dumps({k: e.get(k) for k in _DIFF_KEYS}, sort_keys=True, default=str)
    before = {e["id"]: fp(e) for e in old}
    after = {e["id"]: fp(e) for e in new}
    return {"new": [i for i in after if i not in before],
            "updated": [i for i in after if i in before and before[i] != after[i]],
            "removed": [i for i in before if i not in after],
            "unchanged": sum(1 for i in after if i in before and before[i] == after[i])}


def base_path_warning(base_url: Any, endpoints: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """A warning when the base URL's path is repeated by the documented paths (for example base
    ``https://ada.api.gridscan.io/api`` with paths ``/api/txs/{hash}``): every request would go to
    ``/api/api/...``."""
    try:
        base = parse_base_url(base_url)
    except OutboundBlocked:
        return None
    bp = base["base_path"]
    paths = [e.get("path") or "" for e in endpoints]
    if not bp or not paths:
        return None
    repeated = sum(1 for p in paths if p == bp or p.startswith(bp + "/"))
    if repeated * 2 < len(paths):
        return None
    return (f"The base URL ends in {bp} and the documented paths also start with {bp}, so requests would go to "
            f"{base['origin']}{bp}{bp}/…. Change the base URL to {base['origin']}.")


def doc_coverage(endpoints: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """What the documentation describes for GET endpoints. Says nothing about the live API."""
    gets = [e for e in endpoints if e.get("method") == "GET"]
    params = [p for e in gets for p in e.get("parameters") or []]
    return {"get_endpoints": len(gets),
            "with_response_schema": sum(1 for e in gets if e.get("response_schema")),
            "documented_fields": sum(schema_field_count(e.get("response_schema")) for e in gets),
            "with_summary": sum(1 for e in gets if e.get("summary") or e.get("description")),
            "parameters": len(params), "parameters_described": sum(1 for p in params if p.get("description")),
            "auth_documented": sum(1 for e in gets if e.get("requires_auth") is not None)}


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
    not_found_on = lookup.get("not_found_on") or "empty"
    if not_found_on not in NOT_FOUND_MODES:
        raise ConfigError("Unknown setting for how the API reports an unknown transaction.")
    if not fields and require_fields:
        raise ConfigError("Choose at least one response field to show.")
    return {"name": name, "kind": "transaction", "endpoint_id": ep["id"], "method": "GET", "path": ep["path"],
            "inputs": inputs, "fields": fields, "not_found_on": not_found_on,
            "enabled": bool(lookup.get("enabled", True))}


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
    return _finish_request(base, path, query, integration, secret)


def build_endpoint_request(integration: Mapping[str, Any], endpoint: Mapping[str, Any],
                           values: Mapping[str, Any], secret: Optional[str]) -> Dict[str, Any]:
    """A GET to an imported endpoint with sample parameter values (connection check). Same URL
    rules as :func:`build_request`; every path parameter and required query parameter needs a value."""
    base = parse_base_url(integration.get("base_url"))
    path = _safe_endpoint_path(endpoint.get("path"))
    if path is None:
        raise ConfigError("The endpoint path is not valid.")
    query: List[Tuple[str, str]] = []
    for p in endpoint.get("parameters") or []:
        value = str((values or {}).get(p["name"]) or "").strip()[:500]
        if not value:
            if p["in"] == "path" or p.get("required"):
                raise ConfigError(f"Enter a sample value for '{p['name']}'.")
            continue
        if p["in"] == "path":
            path = path.replace("{" + p["name"] + "}", quote(value, safe=""))
        else:
            query.append((p["name"], value))
    return _finish_request(base, path, query, integration, secret)


def _finish_request(base: Mapping[str, Any], path: str, query: List[Tuple[str, str]],
                    integration: Mapping[str, Any], secret: Optional[str]) -> Dict[str, Any]:
    if re.search(r"\{[^}]*\}", path):
        raise ConfigError("A path parameter has no value.")
    target = base["base_path"] + path
    display = target + ("?" + urlencode(query) if query else "")
    headers = {"Accept": "application/json", "User-Agent": "BridgeBot-API-Lookup/1.0"}
    auth_headers, auth_query = _auth_parts(integration, secret)
    headers.update(auth_headers)
    query = query + auth_query
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

def _unavailable(result: Dict[str, Any], reason: str, message: str) -> Dict[str, Any]:
    result.update(status="unavailable", reason=reason, message=message)
    return result


async def run_lookup(integration: Mapping[str, Any], lookup: Mapping[str, Any], tx_hash: str,
                     secret: Optional[str], *, include_raw: bool = False,
                     schema: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Call the lookup's endpoint for ``tx_hash`` and format the selected fields.

    ``status``:
    - ``ok``: the selected fields (``text``, what the bot gets);
    - ``not_found``: the API answered successfully with an empty result (or HTTP 404 when the
      lookup's ``not_found_on`` is ``empty_or_404``, for APIs that report unknown ids that way);
    - ``unavailable``: anything else — a missing or misconfigured endpoint, auth or server
      errors, redirects, non-JSON, a response without the configured fields, network failures,
      blocked destinations. ``reason`` says which; the bot must not infer that the transaction
      doesn't exist.
    ``raw`` (tester only) is the parsed response, truncated for display; ``quality`` (tester
    only) compares it with the endpoint's documented ``schema`` (:func:`compare_response`).
    """
    result: Dict[str, Any] = {"status": "unavailable", "reason": "", "message": "", "tx_hash": tx_hash,
                              "http_status": None, "duration_ms": 0, "request_url": None, "fields": [], "text": ""}
    try:
        req = build_request(integration, lookup, tx_hash, secret)
    except (ConfigError, OutboundBlocked) as exc:
        return _unavailable(result, "configuration", str(exc))
    result["request_url"] = req["display_url"]
    started = time.monotonic()
    try:
        resp = await guarded_get(req["origin"], req["host"], req["port"], req["target"], req["headers"])
    except OutboundBlocked as exc:
        return _unavailable(result, "blocked", str(exc))
    except OutboundError as exc:
        return _unavailable(result, "network", str(exc))
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
    status = resp["status"]
    result["http_status"] = status
    if resp["truncated"]:
        return _unavailable(result, "too_large", f"The response was larger than {MAX_RESPONSE_SIZE:,} bytes.")
    if 300 <= status < 400:
        return _unavailable(result, "redirect", f"The API redirected (HTTP {status}); redirects are not followed.")
    if status == 404 and lookup.get("not_found_on") == "empty_or_404":
        result.update(status="not_found", reason="http_404",
                      message="The API reported that this transaction was not found (HTTP 404).")
        return result
    if status == 404:
        return _unavailable(result, "endpoint", "The API endpoint was not found (HTTP 404). Check the base URL and endpoint path.")
    if status in (401, 403):
        return _unavailable(result, "auth", f"The API refused the request (HTTP {status}). Check the API key.")
    if status == 429:
        return _unavailable(result, "rate_limited", "The API is rate limiting requests (HTTP 429).")
    if status >= 500:
        return _unavailable(result, "server", f"The API had a server error (HTTP {status}).")
    if status >= 400:
        return _unavailable(result, "request", f"The API rejected the request (HTTP {status}). Check the input template.")
    try:
        data = json.loads(resp["content"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _unavailable(result, "not_json", "The API did not return JSON.")
    if include_raw:
        raw_text = json.dumps(data, ensure_ascii=False, indent=2)
        result["raw"] = raw_text[:RAW_PREVIEW_CHARS]
        result["raw_truncated"] = len(raw_text) > RAW_PREVIEW_CHARS
        result["paths"] = leaf_paths(data)
    if include_raw and data not in ([], {}, None):
        result["quality"] = compare_response(data, schema)
    if data in ([], {}, None):
        result.update(status="not_found", reason="empty",
                      message="The API returned no data for this transaction hash.")
        return result
    rows = format_fields(data, lookup.get("fields") or [])
    result["fields"] = rows
    if rows and not any(r["found"] for r in rows):
        return _unavailable(result, "fields", "The response doesn't contain any of the configured fields. Check the field paths.")
    result.update(status="ok", text=fields_text(rows))
    return result


def bot_block(lookup_name: str, result: Mapping[str, Any]) -> str:
    """The text the bot receives: selected fields, or a short status. Never raw data, URLs or keys."""
    h = result.get("tx_hash") or ""
    if result.get("status") == "ok":
        body = f"Transaction {h}\n{result['text']}"
    elif result.get("status") == "not_found":
        body = f"Transaction {h}\nStatus: not found — the API answered and has no transaction with this hash."
    else:
        body = (f"Transaction {h}\nStatus: lookup unavailable — the transaction could not be checked "
                "(a problem with the lookup service or its setup). This says nothing about whether the "
                "transaction exists.")
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


# ═════════════════════════════ observed endpoint outcomes (spec 017) ═════════════════════════════

def observed_outcome(result: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """What a tester or bot call observed about its endpoint, or None when no request reached the
    network (invalid configuration or input, BridgeBot's own rate limit)."""
    reason = result.get("reason") or ""
    if reason == "configuration" or (reason == "rate_limited" and result.get("http_status") is None):
        return None
    status = result.get("status")
    outcome = {"ok": "passed", "not_found": "not_found"}.get(status, "failed")
    return {"outcome": outcome, "http_status": result.get("http_status"), "reason": reason or None,
            "message": (result.get("message") or "")[:300], "duration_ms": result.get("duration_ms")}


# ═════════════════════════════ connection check (spec 017; reference 1c20d2a test_connection) ═════════════════════════════

async def _probe(req: Mapping[str, Any], label: str, with_key: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"label": label, "url": req["display_url"], "with_key": with_key,
                           "http_status": None, "duration_ms": 0, "error": None}
    started = time.monotonic()
    try:
        resp = await guarded_get(req["origin"], req["host"], req["port"], req["target"], req["headers"],
                                 max_size=MAX_RESPONSE_SIZE)
        out["http_status"] = resp["status"]
    except OutboundBlocked as exc:
        out["error"] = "blocked"; out["message"] = str(exc)
    except OutboundError as exc:
        out["error"] = "network"; out["message"] = str(exc)
    out["duration_ms"] = int((time.monotonic() - started) * 1000)
    return out


def _state(state: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"state": state, "message": message, **extra}


async def connection_check(integration: Mapping[str, Any], endpoints: Mapping[str, Mapping[str, Any]],
                           secret: Optional[str]) -> Dict[str, Any]:
    """Host reachability and credential verification, reported separately.

    - Reachability: the configured public health endpoint, called without credentials. Any HTTP
      answer means the host is reachable; only 2xx means the health check passed. Without a health
      endpoint, an HTTP answer to the credential check shows reachability.
    - Credentials: the configured protected GET endpoint with its sample input, with the key. Only a
      2xx with the key *and* a 401/403 for the same request without it counts as ``accepted``;
      401/403 with the key is ``rejected``. Anything else is ``not_verified`` with the reason.
    ``ok`` is true only when the host is reachable and the key was accepted (or none is used)."""
    settings = integration.get("connection") or {}
    probes: List[Dict[str, Any]] = []
    reach: Optional[Dict[str, Any]] = None

    health_id = settings.get("health_endpoint_id") or ""
    if health_id:
        ep = endpoints.get(health_id)
        if not ep:
            reach = _state("not_configured", "The health check endpoint is no longer in the imported documentation.")
        else:
            try:
                req = build_endpoint_request(integration, ep, {}, None)
            except (ConfigError, OutboundBlocked) as exc:
                reach = _state("not_configured", str(exc))
            else:
                p = await _probe(req, f"Health check: GET {ep['path']}", False)
                probes.append(p)
                if p["error"] == "blocked":
                    reach = _state("blocked", p["message"])
                elif p["error"]:
                    reach = _state("unreachable", f"Host not reachable: {p['message']}")
                elif 200 <= p["http_status"] < 300:
                    reach = _state("reachable", f"Host reachable: the health check answered HTTP {p['http_status']}.")
                else:
                    reach = _state("unhealthy", f"The host answered, but the health check returned HTTP {p['http_status']}.")
                p["outcome"] = reach["state"]

    auth_type = integration.get("auth_type") or "none"
    cred_id = settings.get("credential_endpoint_id") or ""
    ep = endpoints.get(cred_id) if cred_id else None
    if auth_type == "none":
        cred = _state("not_required", "This integration doesn't send a key.")
    elif not secret:
        cred = _state("missing", "No key is saved, so there is nothing to verify.")
    elif not cred_id:
        cred = _state("not_checked", "Choose a protected GET endpoint and a valid sample input to verify the key.")
    elif not ep:
        cred = _state("not_checked", "The credential check endpoint is no longer in the imported documentation.")
    elif ep.get("method") != "GET":
        cred = _state("not_checked", "Only GET endpoints can be used to verify the key.")
    elif ep.get("requires_auth") is False:
        cred = _state("not_checked", "The documentation marks this endpoint as public, so it can't verify a key.")
    else:
        try:
            with_key = build_endpoint_request(integration, ep, settings.get("credential_params") or {}, secret)
            without_key = build_endpoint_request(integration, ep, settings.get("credential_params") or {}, None)
        except (ConfigError, OutboundBlocked) as exc:
            cred = _state("not_checked", str(exc))
        else:
            p = await _probe(with_key, f"Credential check: GET {ep['path']} with the key", True)
            probes.append(p)
            st = p["http_status"]
            if p["error"]:
                cred = _state("not_verified", f"Couldn't reach the API to verify the key: {p['message']}")
            elif st in (401, 403):
                cred = _state("rejected", f"The API rejected the key (HTTP {st}).")
            elif 200 <= st < 300:
                q = await _probe(without_key, f"Credential check: GET {ep['path']} without a key", False)
                probes.append(q)
                if q["http_status"] in (401, 403):
                    cred = _state("accepted", f"Key accepted: HTTP {st} with the key and HTTP {q['http_status']} without it.")
                elif q["http_status"] and 200 <= q["http_status"] < 300:
                    cred = _state("not_verified", "The endpoint also answered without a key, so this doesn't show "
                                                  "that the key works. Choose an endpoint that requires a key.")
                else:
                    cred = _state("not_verified", f"HTTP {st} with the key, but the request without a key returned "
                                  f"{('HTTP ' + str(q['http_status'])) if q['http_status'] else q.get('message')}, "
                                  "so the endpoint wasn't shown to require a key.")
                q["outcome"] = "expected" if cred["state"] == "accepted" else "inconclusive"
            elif st == 404:
                cred = _state("not_verified", "The sample input wasn't found (HTTP 404), so the key wasn't verified. "
                                              "Use a sample that exists.")
            elif st == 429:
                cred = _state("not_verified", "The API is rate limiting requests (HTTP 429); try again later.")
            elif 300 <= st < 400:
                cred = _state("not_verified", f"The API redirected (HTTP {st}); redirects are not followed.")
            elif st >= 500:
                cred = _state("not_verified", f"The API had a server error (HTTP {st}).")
            else:
                cred = _state("not_verified", f"The API rejected the sample input (HTTP {st}). Check the sample values.")
            p["outcome"] = cred["state"]

    if reach is None:
        answered = next((p for p in probes if p["http_status"] is not None), None)
        failed = next((p for p in probes if p["error"]), None)
        if answered:
            reach = _state("reachable", f"Host reachable: it answered the credential check (HTTP {answered['http_status']}). "
                                        "No health check endpoint is set.")
        elif failed:
            reach = _state("blocked" if failed["error"] == "blocked" else "unreachable", failed["message"])
        else:
            reach = _state("not_configured", "Choose a public health check endpoint to check that the host is reachable.")
    ok = reach["state"] == "reachable" and cred["state"] in ("accepted", "not_required")
    return {"ok": ok, "reachability": reach, "credentials": cred, "probes": probes}


# ═════════════════════════════ response quality (spec 017; reference 1c20d2a api_diagnostics) ═════════════════════════════

def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _type_ok(actual: str, node: Mapping[str, Any]) -> bool:
    types = node.get("types") or []
    if actual == "null":
        return bool(node.get("nullable")) or not types or "null" in types
    return not types or actual in types or (actual == "integer" and "number" in types)


def compare_response(data: Any, schema: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compare an actual JSON response with the documented response schema.

    Every documented field is followed into nested objects and every array item (up to
    ``QUALITY_MAX_ITEMS`` per array); array items share a path such as ``outputs[].amount``.
    Each path gets one status: ``missing_required``, ``type_difference``, ``unexpected`` (present
    but not documented), ``missing_optional`` (documented as optional, absent here — not an error)
    or ``ok``. ``count``/``of`` say in how many of the objects at that level it happened."""
    if not schema:
        return {"documented": False, "message": "The documentation has no response schema for this endpoint, "
                                                "so there is nothing to compare."}
    rows: Dict[str, Dict[str, Any]] = {}
    visits: Dict[str, int] = {}
    sampled = [False]

    def row(path: str, parent: str) -> Dict[str, Any]:
        return rows.setdefault(path, {"path": path, "parent": parent, "documented": False, "required": False,
                                      "expected": [], "nullable": False, "actual": set(),
                                      "present": 0, "missing": 0, "mismatch": 0})

    def walk(value: Any, node: Optional[Mapping[str, Any]], path: str, parent: str, required: bool) -> None:
        if len(rows) >= QUALITY_MAX_ROWS:
            return
        r = row(path, parent)
        actual = _json_type(value)
        r["present"] += 1
        r["actual"].add(actual)
        if node is None:
            return
        r.update(documented=True, required=r["required"] or required, expected=node.get("types") or [],
                 nullable=bool(node.get("nullable")))
        if not _type_ok(actual, node):
            r["mismatch"] += 1
            return
        if actual == "object" and isinstance(node.get("properties"), dict):
            props = node["properties"]
            if not path.endswith("[]"):          # array items are counted by the array loop
                visits[path] = visits.get(path, 0) + 1
            req = set(node.get("required") or [])
            for name, child in props.items():
                cp = f"{path}.{name}" if path else name
                if name in value:
                    walk(value[name], child, cp, path, name in req)
                elif len(rows) < QUALITY_MAX_ROWS:
                    m = row(cp, path)
                    m.update(documented=True, required=m["required"] or name in req,
                             expected=(child or {}).get("types") or [], nullable=bool((child or {}).get("nullable")))
                    m["missing"] += 1
            if props and not node.get("open"):
                for name, v in value.items():
                    if name not in props:
                        walk(v, None, f"{path}.{name}" if path else str(name), path, False)
        elif actual == "array" and node.get("items") is not None:
            visits.setdefault(path + "[]", 0)
            if len(value) > QUALITY_MAX_ITEMS:
                sampled[0] = True
            for item in value[:QUALITY_MAX_ITEMS]:
                visits[path + "[]"] += 1
                walk(item, node["items"], path + "[]", path + "[]", True)

    walk(data, schema, "", "", True)
    out: List[Dict[str, Any]] = []
    for r in rows.values():
        if not r["path"] or r["path"].endswith("[]") and not r["documented"]:
            continue
        of = max(visits.get(r["parent"], 1), 1) if r["parent"] or r["path"] else 1
        if not r["documented"]:
            status, count = "unexpected", r["present"]
        elif r["mismatch"]:
            status, count = "type_difference", r["mismatch"]
        elif r["missing"] and r["required"]:
            status, count = "missing_required", r["missing"]
        elif r["missing"]:
            status, count = "missing_optional", r["missing"]
        else:
            status, count = "ok", r["present"]
        if r["path"].endswith("[]") and status == "ok":
            continue
        out.append({"path": r["path"], "status": status, "count": count, "of": of, "documented": r["documented"],
                    "required": r["required"], "missing": r["missing"], "mismatch": r["mismatch"],
                    "expected": r["expected"] + (["null"] if r["nullable"] else []),
                    "actual": sorted(r["actual"])})
    order = {"missing_required": 0, "type_difference": 1, "unexpected": 2, "missing_optional": 3, "ok": 4}
    out.sort(key=lambda x: (order[x["status"]], x["path"]))
    root = rows.get("", {})
    summary = {k: sum(1 for x in out if x["status"] == k) for k in order}
    summary["documented_fields"] = sum(1 for x in out if x["documented"])
    summary["compared_fields"] = len(out)
    result = {"documented": True, "summary": summary, "fields": out, "sampled": sampled[0],
              "limited": len(rows) >= QUALITY_MAX_ROWS}
    if root.get("mismatch"):
        result["root_mismatch"] = {"expected": root["expected"], "actual": sorted(root["actual"])}
    return result
