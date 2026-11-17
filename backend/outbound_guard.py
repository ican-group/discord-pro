"""Outbound request guards (spec 010). Standard library only.

User-influenced outbound destinations in the API:

* **API integrations** (``base_url``, ``doc_source_url``, endpoint paths from
  discovered documentation, LLM-chosen parameters): every request goes through
  :func:`guarded_request` — strict URL parsing (no alternative IP encodings,
  credentials, fragments or internal host names), public-address check of
  every DNS answer, connection pinned to the checked address with TLS
  verification against the real host name, no redirects, no proxies,
  sanitised headers (Host and hop-by-hop headers cannot be set), a size cap,
  and fixed error messages (spec 011).

* **Ollama** (per-bot ``ollama_base_url``): any organisation member can set it,
  so without a guard it is a server-side request forgery (SSRF) primitive into
  the database network, the WireGuard edge, cloud metadata, etc.
  Policy: Ollama is **disabled** unless the operator lists exact trusted origins
  in ``OLLAMA_ALLOWED_BASE_URLS`` (comma-separated). A bot's URL must equal one
  of them. At request time the host is resolved once, *every* address must be a
  public unicast address, and the connection is pinned to the checked address
  (no second DNS lookup, so DNS rebinding cannot swap in an internal address).
  Redirects are never followed. Private/loopback targets are allowed only when
  ``APP_ENV=development`` and ``OLLAMA_ALLOW_PRIVATE_NETWORKS=1``; link-local,
  cloud-metadata, multicast, reserved and unspecified addresses are always
  blocked.

* **Zendesk** (``subdomain``): stored OAuth/API tokens are sent to
  ``https://<subdomain>.zendesk.com``. The subdomain must be a single DNS label
  (``[a-z0-9-]``, 1-63 chars, no leading/trailing hyphen); URLs are only ever
  built by :func:`zendesk_url`, which re-validates and checks the final host.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import List, Mapping, Sequence, Tuple
from urllib.parse import urlsplit

import client_ip as _client_ip


class OutboundBlocked(ValueError):
    """A destination is not allowed. ``str(exc)`` is safe to show to users."""


# ════════════════════════════ Zendesk ════════════════════════════

ZENDESK_DOMAIN = "zendesk.com"
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Characters allowed in a Zendesk API path (after the host). No '#', '\\', '@',
# whitespace or control characters; the host is fixed regardless.
_ZENDESK_PATH = re.compile(r"/[A-Za-z0-9/._~\-]*(?:\?[A-Za-z0-9._~\-=&%+,:]*)?")
ZENDESK_SUBDOMAIN_ERROR = (
    "Invalid Zendesk subdomain. Use only the subdomain, e.g. 'acme' for acme.zendesk.com "
    "(letters, digits and hyphens)."
)


def normalize_zendesk_subdomain(raw) -> str:
    """Return the bare subdomain or raise :class:`OutboundBlocked`.

    Accepted spellings: ``acme``, ``acme.zendesk.com``, ``https://acme.zendesk.com``
    (one optional trailing ``/``), any letter case. Everything else — other
    schemes, paths, ports, credentials, query strings, fragments, extra labels,
    non-ASCII characters — is rejected.
    """
    if not isinstance(raw, str):
        raise OutboundBlocked(ZENDESK_SUBDOMAIN_ERROR)
    s = raw.strip().lower()
    if s.startswith("https://"):
        s = s[len("https://"):]
    if s.endswith("/"):
        s = s[:-1]
    suffix = "." + ZENDESK_DOMAIN
    if s.endswith(suffix):
        s = s[: -len(suffix)]
    if not _DNS_LABEL.fullmatch(s):
        raise OutboundBlocked(ZENDESK_SUBDOMAIN_ERROR)
    return s


def zendesk_host(subdomain) -> str:
    return f"{normalize_zendesk_subdomain(subdomain)}.{ZENDESK_DOMAIN}"


def zendesk_url(subdomain, path: str) -> str:
    """``https://<subdomain>.zendesk.com<path>`` — the only way to build a Zendesk URL."""
    host = zendesk_host(subdomain)
    if not isinstance(path, str) or path.startswith("//") or not _ZENDESK_PATH.fullmatch(path):
        raise OutboundBlocked("Invalid Zendesk API path.")
    url = f"https://{host}{path}"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != host or parts.port is not None \
            or parts.username or parts.password or parts.fragment:
        raise OutboundBlocked("Invalid Zendesk URL.")
    return url


# ═════════════════════ Shared host / URL validation ═════════════════════

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")
# Names that only ever resolve inside private networks (defence in depth; the
# address check would block them anyway).
_INTERNAL_SUFFIXES = ("localhost", "local", "internal", "localdomain", "home.arpa", "lan", "intranet")


def canonical_host(host, *, allow_internal_names: bool = False) -> str:
    """Return the canonical host (``[v6]`` bracketed) or raise :class:`OutboundBlocked`.

    IP literals must be in canonical form: decimal/octal/hex/shortened IPv4
    spellings (``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``127.1``) and IPv6
    zone IDs are rejected rather than interpreted. Host names must be ASCII
    LDH labels without a trailing dot; unless ``allow_internal_names`` they need
    at least two labels, an alphabetic TLD and no internal suffix.
    """
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
        if ip.version == 4:
            if str(ip) != h:
                raise OutboundBlocked("Invalid IP address format.")
            return h
        return f"[{ip.compressed}]"
    if ":" in h:
        raise OutboundBlocked("Invalid IP address format.")
    labels = h.split(".")
    if len(h) > 253 or not all(_LABEL.fullmatch(lbl) for lbl in labels):
        raise OutboundBlocked("Invalid host name.")
    # A numeric/hex last label is an IP spelling, never a DNS name (127.1, 0x7f.1, 2130706433).
    if re.fullmatch(r"(?:0x[0-9a-f]*|[0-9]+)", labels[-1]):
        raise OutboundBlocked("Invalid IP address format.")
    if not allow_internal_names:
        if len(labels) < 2 or not _TLD.fullmatch(labels[-1]):
            raise OutboundBlocked("Internal host names are not allowed.")
        if any(h == sfx or h.endswith("." + sfx) for sfx in _INTERNAL_SUFFIXES):
            raise OutboundBlocked("Internal host names are not allowed.")
    return h


def parse_outbound_url(raw, *, allow_http: bool = False, allow_internal_names: bool = False) -> dict:
    """Strictly parse an outbound URL into ``{scheme, host, port, origin, target}``.

    ``target`` is path + optional query. Rejects other schemes, credentials,
    fragments, backslashes, whitespace/control and non-ASCII characters,
    invalid ports and non-canonical hosts (see :func:`canonical_host`).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise OutboundBlocked("A URL is required.")
    s = raw.strip()
    if not s.isascii() or any(ord(c) < 0x21 or ord(c) == 0x7F or c in "\\#" for c in s):
        raise OutboundBlocked("Invalid URL.")
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        raise OutboundBlocked("Invalid URL.")
    scheme = (parts.scheme or "").lower()
    allowed = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed:
        raise OutboundBlocked("Only https:// URLs are allowed." if not allow_http else "Only http(s) URLs are allowed.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise OutboundBlocked("URLs must not contain credentials.")
    host = canonical_host(parts.hostname or "", allow_internal_names=allow_internal_names)
    port = port if port is not None else _DEFAULT_PORTS[scheme]
    if not 0 < port < 65536:
        raise OutboundBlocked("Invalid port.")
    origin = f"{scheme}://{host}" + ("" if port == _DEFAULT_PORTS[scheme] else f":{port}")
    path = parts.path or "/"
    if not path.startswith("/"):
        raise OutboundBlocked("Invalid URL path.")
    target = path + (f"?{parts.query}" if parts.query else "")
    return {"scheme": scheme, "host": host, "port": port, "origin": origin, "target": target}


# ════════════════════════════ Ollama ════════════════════════════

OLLAMA_ALLOWED_BASE_URLS = "OLLAMA_ALLOWED_BASE_URLS"
OLLAMA_ALLOW_PRIVATE_NETWORKS = "OLLAMA_ALLOW_PRIVATE_NETWORKS"
OLLAMA_DISABLED_ERROR = "Ollama is disabled on this deployment."
OLLAMA_NOT_ALLOWED_ERROR = "This Ollama URL is not on this server's allowlist."
OLLAMA_UNSAFE_ADDRESS_ERROR = "The Ollama server address is not allowed."

# Cloud metadata endpoints: blocked even when private networks are allowed.
_METADATA_ADDRESSES = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254",      # AWS / GCP / Azure / OpenStack / DigitalOcean
    "169.254.170.2",        # AWS ECS task metadata
    "100.100.100.200",      # Alibaba Cloud
    "192.0.0.192",          # Oracle Cloud (legacy)
    "fd00:ec2::254",        # AWS IPv6 IMDS
))
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(raw) -> str:
    """``scheme://host[:port]`` for an http(s) base URL, or raise :class:`OutboundBlocked`.

    Rejects credentials, paths other than ``/``, query strings, fragments,
    whitespace/control characters and invalid ports. Default ports are dropped.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise OutboundBlocked("An Ollama URL is required.")
    s = raw.strip()
    if any(ord(c) < 0x21 or ord(c) == 0x7F or c == "\\" for c in s):
        raise OutboundBlocked("Invalid Ollama URL.")
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        raise OutboundBlocked("Invalid Ollama URL.")
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        raise OutboundBlocked("The Ollama URL must start with http:// or https://.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise OutboundBlocked("The Ollama URL must not contain credentials.")
    if parts.path not in ("", "/") or parts.query or parts.fragment or s.endswith(("?", "#")):
        raise OutboundBlocked("The Ollama URL must be a base URL without a path, query or fragment.")
    host = canonical_host(host, allow_internal_names=True)
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def ollama_allowlist(env: Mapping[str, str]) -> Tuple[str, ...]:
    """Normalised trusted origins from ``OLLAMA_ALLOWED_BASE_URLS``.

    Raises :class:`OutboundBlocked` on any invalid entry (fail closed: a typo
    must not silently widen or disable the policy).
    """
    raw = (env.get(OLLAMA_ALLOWED_BASE_URLS) or "").strip()
    if not raw:
        return ()
    out: List[str] = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            out.append(normalize_origin(item))
    return tuple(dict.fromkeys(out))


def ollama_private_networks_allowed(env: Mapping[str, str]) -> bool:
    import runtime_config as _rc
    if _rc.get_app_env(env) == _rc.APP_ENV_PRODUCTION:
        return False
    return (env.get(OLLAMA_ALLOW_PRIVATE_NETWORKS) or "").strip() == "1"


def ollama_enabled(env: Mapping[str, str]) -> bool:
    try:
        return bool(ollama_allowlist(env))
    except OutboundBlocked:
        return False


def check_ollama_base_url(raw, env: Mapping[str, str]) -> str:
    """Static policy check (no DNS): return the normalised allowed origin or raise."""
    try:
        allow = ollama_allowlist(env)
    except OutboundBlocked:
        raise OutboundBlocked(OLLAMA_DISABLED_ERROR)
    if not allow:
        raise OutboundBlocked(OLLAMA_DISABLED_ERROR)
    origin = normalize_origin(raw)
    if origin not in allow:
        raise OutboundBlocked(OLLAMA_NOT_ALLOWED_ERROR)
    return origin


def address_allowed(addr, *, allow_private: bool = False) -> bool:
    """True if ``addr`` may be contacted.

    Always blocked: unspecified, multicast, reserved, link-local (incl. cloud
    metadata) and known metadata addresses. Loopback and private/shared/ULA
    ranges are blocked unless ``allow_private`` (development only). IPv4
    addresses embedded in IPv6 (mapped, 6to4, Teredo, NAT64) are checked too.
    """
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0] if isinstance(addr, str) else addr)
    except ValueError:
        return False
    candidates = [ip]
    if ip.version == 6 and ip.ipv4_mapped is not None:
        candidates = [ip.ipv4_mapped]        # ::ffff:a.b.c.d is exactly the IPv4 address
    elif ip.version == 6:
        for embedded in (ip.sixtofour, ip.teredo[1] if ip.teredo else None):
            if embedded is not None:
                candidates.append(embedded)
        if ip in ipaddress.ip_network("64:ff9b::/96"):             # NAT64 well-known prefix
            candidates.append(ipaddress.ip_address(int(ip) & 0xFFFFFFFF))
    for c in candidates:
        if c in _METADATA_ADDRESSES or c.is_unspecified or c.is_multicast or c.is_link_local:
            return False
        if c.is_loopback:
            if allow_private:
                continue
            return False
        if c.is_reserved:
            return False
        if c.is_global:
            continue
        if not allow_private:
            return False
        if not (c.is_loopback or c.is_private):
            return False
    return True


async def _system_resolve(host: str, port: int) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def resolve_pinned_address(host: str, port: int, *, allow_private: bool = False,
                                 resolver=None, unsafe_error: str = OLLAMA_UNSAFE_ADDRESS_ERROR,
                                 unresolvable_error: str = "The server host name could not be resolved.") -> str:
    """Resolve ``host`` once and return one address to connect to.

    Every resolved address must pass :func:`address_allowed` — a single internal
    answer blocks the request, so mixed public/internal records cannot be used.
    Raises :class:`OutboundBlocked`.
    """
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        addrs: Sequence[str] = [ipaddress.ip_address(bare).compressed]
    except ValueError:
        try:
            addrs = await (resolver or _system_resolve)(bare, port)
        except (OSError, UnicodeError):
            raise OutboundBlocked(unresolvable_error)
    addrs = list(dict.fromkeys(a.split("%", 1)[0] for a in addrs))
    if not addrs or not all(address_allowed(a, allow_private=allow_private) for a in addrs):
        raise OutboundBlocked(unsafe_error)
    return addrs[0]


def pinned_request(origin: str, address: str, path: str) -> Tuple[str, dict, dict]:
    """(url, headers, httpx extensions) that connect to ``address`` while
    presenting the original host name (Host header, TLS SNI and certificate
    verification use the host name, not the IP)."""
    parts = urlsplit(origin)
    scheme, host = parts.scheme, parts.hostname
    port = parts.port or _DEFAULT_PORTS[scheme]
    ip = ipaddress.ip_address(address)
    ip_host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    host_header = parts.netloc  # host[:port] exactly as in the allowed origin
    url = f"{scheme}://{ip_host}:{port}{path}"
    extensions = {"sni_hostname": host} if scheme == "https" else {}
    return url, {"Host": host_header}, extensions


async def prepare_ollama_request(base_url, env: Mapping[str, str], path: str = "/api/chat",
                                 *, resolver=None) -> Tuple[str, dict, dict]:
    """Full policy for one Ollama request: allowlist, DNS check, IP pinning."""
    origin = check_ollama_base_url(base_url, env)
    parts = urlsplit(origin)
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    address = await resolve_pinned_address(
        parts.hostname, port, allow_private=ollama_private_networks_allowed(env), resolver=resolver)
    return pinned_request(origin, address, path)


# ═══════════════════ Generic outbound requests (API integrations) ═══════════════════

INTEGRATIONS_ALLOW_PRIVATE_NETWORKS = "API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS"
DESTINATION_NOT_ALLOWED_ERROR = "The destination address is not allowed."
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
# Headers a caller may never set: routing, framing, proxy and forwarding headers.
FORBIDDEN_HEADER_NAMES = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade",
    "te", "trailer", "expect", "proxy-authorization", "proxy-authenticate", "proxy-connection",
    "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "x-original-url", "x-rewrite-url",
}) | _client_ip.CLIENT_ADDRESS_HEADERS   # client-address headers are named only in client_ip.py


class OutboundError(Exception):
    """Request failed after the policy allowed it. ``str(exc)`` is a fixed, safe message."""


def validate_header_name(name) -> str:
    if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name.strip()):
        raise OutboundBlocked("Invalid header name.")
    name = name.strip()
    if name.lower() in FORBIDDEN_HEADER_NAMES or name.lower().startswith(("proxy-", "sec-")):
        raise OutboundBlocked(f"The header '{name}' cannot be set.")
    return name


def sanitize_headers(headers) -> dict:
    """Validated copy of caller headers: allowed names, string values without CR/LF/NUL."""
    out = {}
    for name, value in (headers or {}).items():
        name = validate_header_name(name)
        if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise OutboundBlocked(f"Invalid value for header '{name}'.")
        out[name] = value
    return out


def integrations_private_networks_allowed(env: Mapping[str, str]) -> bool:
    import runtime_config as _rc
    if _rc.get_app_env(env) == _rc.APP_ENV_PRODUCTION:
        return False
    return (env.get(INTEGRATIONS_ALLOW_PRIVATE_NETWORKS) or "").strip() == "1"


def parse_integration_url(raw, env: Mapping[str, str]) -> dict:
    """URL policy for API integrations: https only and public host names in
    production; development may allow http and internal names with
    ``API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS=1``."""
    dev_private = integrations_private_networks_allowed(env)
    u = parse_outbound_url(raw, allow_http=dev_private, allow_internal_names=dev_private)
    literal = u["host"][1:-1] if u["host"].startswith("[") else u["host"]
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return u                       # host name: checked against every DNS answer at request time
    if not address_allowed(literal, allow_private=dev_private):
        raise OutboundBlocked(DESTINATION_NOT_ALLOWED_ERROR)
    return u


def _default_client(timeout: float):
    import httpx
    # verify=True (system CAs), no redirects, no proxy/env configuration.
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)


async def guarded_request(method: str, url: str, *, env: Mapping[str, str], headers=None, params=None,
                          timeout: float = 20.0, max_size: int = 500_000,
                          resolver=None, client_factory=None) -> dict:
    """Perform one policy-checked request. Returns
    ``{status, reason, headers, content (bytes, <= max_size), truncated, url}``.

    Raises :class:`OutboundBlocked` (policy) or :class:`OutboundError` (network/TLS/
    timeout) — both with fixed messages that never contain upstream content.
    Redirect responses (3xx) are returned as-is and never followed.
    """
    import httpx
    u = parse_integration_url(url, env)
    safe_headers = sanitize_headers(headers)
    address = await resolve_pinned_address(
        u["host"], u["port"], allow_private=integrations_private_networks_allowed(env),
        resolver=resolver, unsafe_error=DESTINATION_NOT_ALLOWED_ERROR,
        unresolvable_error="The destination host name could not be resolved.")
    pinned_url, host_header, extensions = pinned_request(u["origin"], address, u["target"])
    # Host is always ours (callers cannot set it: FORBIDDEN_HEADER_NAMES).
    send_headers = {**safe_headers, **host_header}
    client = (client_factory or _default_client)(timeout)
    try:
        async with client:
            async with client.stream(method.upper(), pinned_url, headers=send_headers,
                                     params=params or None, extensions=extensions) as resp:
                body = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_size:
                        truncated = True
                        break
                return {"status": resp.status_code, "reason": resp.reason_phrase or "",
                        "headers": dict(resp.headers), "content": bytes(body[:max_size]),
                        "truncated": truncated, "url": u["origin"] + u["target"]}
    except httpx.TimeoutException:
        raise OutboundError("The request timed out.")
    except httpx.ConnectError as exc:
        text = str(exc).upper()
        if "CERTIFICATE" in text or "SSL" in text or "TLS" in text:
            raise OutboundError("TLS certificate verification failed.")
        raise OutboundError("Could not connect to the destination.")
    except httpx.HTTPError:
        raise OutboundError("The request failed.")
