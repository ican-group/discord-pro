"""Centralised client-IP resolution (spec 008, R-IP.1–R-IP.6).

This module is the ONLY place that may decide a request's client IP. No other
module may read ``X-Forwarded-For``, ``X-Real-IP``, ``Forwarded``,
``CF-Connecting-IP`` or ``request.client`` directly (enforced by an AST test).

Default (current private deployment): **no proxy is trusted**, so the client IP
is always the real TCP peer and every forwarded-IP header is ignored.

Future Cloudflare Tunnel support: a ``ClientIPResolver`` may be given an explicit
set of trusted proxy addresses (the cloudflared container's fixed address). Only
when the immediate TCP peer is one of those exact addresses is the single
``CF-Connecting-IP`` header honoured, and only if it holds exactly one valid IP
address. Anything else falls back to the peer. There are no wildcards, ranges or
"trust all" options.

Standard library only.
"""
from __future__ import annotations

import ipaddress
from typing import FrozenSet, Iterable, Mapping, Optional, Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Returned when the peer address is missing or not an IP address (e.g. a Unix
# socket or a test client). All such requests share one rate-limit bucket.
UNKNOWN_CLIENT = "unknown"

# The only forwarded-IP header that may ever be honoured, and only from a
# trusted proxy. Lower-case for case-insensitive lookup.
CF_CONNECTING_IP = "cf-connecting-ip"

TRUSTED_PROXY_ENV = "TRUSTED_PROXY_IPS"

# Upper bound on header length we are willing to parse.
_MAX_HEADER_LEN = 64


class TrustedProxyConfigError(ValueError):
    """Invalid TRUSTED_PROXY_IPS configuration."""


def _parse_ip(value: Optional[str]) -> Optional[IPAddress]:
    """Parse exactly one IP address; return None for anything else."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > _MAX_HEADER_LEN:
        return None
    # Reject lists, ports, brackets, zone IDs and whitespace inside the value.
    if any(c in text for c in ",; []%\t"):
        return None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def parse_trusted_proxies(raw: Optional[str]) -> FrozenSet[IPAddress]:
    """Parse TRUSTED_PROXY_IPS: comma-separated **exact** IP addresses.

    Empty or unset means no trusted proxy (the default). CIDR ranges, wildcards,
    hostnames and unspecified/multicast addresses are rejected, because each
    trusted entry must be an explicitly verified proxy connection.
    """
    if raw is None or not raw.strip():
        return frozenset()
    trusted = set()
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            raise TrustedProxyConfigError(f"{TRUSTED_PROXY_ENV} contains an empty entry")
        if "*" in entry or "/" in entry:
            raise TrustedProxyConfigError(
                f"{TRUSTED_PROXY_ENV} accepts exact addresses only (no wildcards or ranges): {entry!r}"
            )
        addr = _parse_ip(entry)
        if addr is None:
            raise TrustedProxyConfigError(f"{TRUSTED_PROXY_ENV} entry is not an IP address: {entry!r}")
        if addr.is_unspecified or addr.is_multicast:
            raise TrustedProxyConfigError(f"{TRUSTED_PROXY_ENV} entry is not a usable proxy address: {entry!r}")
        trusted.add(addr)
    return frozenset(trusted)


def _header(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    """Case-insensitive single-header lookup that works for dicts and Starlette Headers."""
    if not headers:
        return None
    getter = getattr(headers, "getlist", None)
    if getter is not None:  # Starlette Headers: refuse repeated headers
        values = getter(name)
        return values[0] if len(values) == 1 else None
    found = [v for k, v in headers.items() if str(k).lower() == name]
    return found[0] if len(found) == 1 else None


class ClientIPResolver:
    """Resolves the client IP from the TCP peer and, only for trusted peers,
    from ``CF-Connecting-IP``."""

    def __init__(self, trusted_proxies: Iterable[IPAddress] = ()):
        self._trusted: FrozenSet[IPAddress] = frozenset(trusted_proxies)

    @property
    def trusted_proxies(self) -> FrozenSet[IPAddress]:
        return self._trusted

    def resolve(self, peer_host: Optional[str], headers: Optional[Mapping[str, str]] = None) -> str:
        peer = _parse_ip(peer_host)
        if peer is None:
            return UNKNOWN_CLIENT
        if peer in self._trusted:
            forwarded = _parse_ip(_header(headers, CF_CONNECTING_IP))
            if forwarded is not None and not forwarded.is_unspecified:
                return str(forwarded)
        return str(peer)


# Process-wide resolver. Trusts nothing; Stage 10 of spec 008 configures it.
_resolver = ClientIPResolver()


def get_resolver() -> ClientIPResolver:
    return _resolver


def configure(resolver: ClientIPResolver) -> None:
    """Replace the process-wide resolver (startup only; Tunnel stage)."""
    global _resolver
    _resolver = resolver


def client_ip(request) -> str:
    """The client IP for a Starlette/FastAPI request. The single entry point."""
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None) if client is not None else None
    return _resolver.resolve(peer, getattr(request, "headers", None))


def rate_limit_key(ip: str) -> str:
    """Bucket key for per-IP limits: IPv4 as-is, IPv6 grouped by /64 (R-IP.5)."""
    addr = _parse_ip(ip)
    if addr is None:
        return UNKNOWN_CLIENT
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network(f"{addr}/64", strict=False))
    return str(addr)
