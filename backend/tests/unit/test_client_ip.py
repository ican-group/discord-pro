"""Client-IP resolution (spec 008, R-IP.1–R-IP.6): spoofed-header regression tests.

All addresses are documentation/test ranges (RFC 5737 / RFC 3849).
"""
import ast
import importlib.util
import unittest
from types import SimpleNamespace

from tests.unit._support import BACKEND_DIR
import client_ip as cip

PEER = "192.0.2.10"           # the real TCP peer
SPOOFED = "203.0.113.66"      # what an attacker claims to be
PROXY = "198.51.100.5"        # a (future) verified cloudflared address

SPOOF_HEADERS = {
    "X-Forwarded-For": SPOOFED,
    "X-Real-IP": SPOOFED,
    "Forwarded": f"for={SPOOFED}",
    "CF-Connecting-IP": SPOOFED,
    "True-Client-IP": SPOOFED,
    "X-Client-IP": SPOOFED,
}


def req(peer, headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer is not None else None,
                           headers=headers or {})


class DefaultResolverTests(unittest.TestCase):
    """Current private deployment: nothing is trusted."""

    def test_default_resolver_trusts_no_proxy(self):
        self.assertEqual(cip.get_resolver().trusted_proxies, frozenset())

    def test_peer_used_without_headers(self):
        self.assertEqual(cip.client_ip(req(PEER)), PEER)

    def test_every_spoofed_header_ignored(self):
        for name, value in SPOOF_HEADERS.items():
            with self.subTest(header=name):
                self.assertEqual(cip.client_ip(req(PEER, {name: value})), PEER)
        self.assertEqual(cip.client_ip(req(PEER, SPOOF_HEADERS)), PEER)

    def test_spoofed_header_ignored_even_from_loopback_or_private_peer(self):
        for peer in ("127.0.0.1", "10.0.0.1", "172.17.0.1", "::1"):
            with self.subTest(peer=peer):
                self.assertEqual(cip.client_ip(req(peer, SPOOF_HEADERS)), str(cip._parse_ip(peer)))

    def test_missing_or_non_ip_peer_is_unknown_not_header(self):
        for peer in (None, "", "testclient", "unix:/tmp/x.sock", "localhost"):
            with self.subTest(peer=peer):
                self.assertEqual(cip.client_ip(req(peer, SPOOF_HEADERS)), cip.UNKNOWN_CLIENT)

    def test_ipv4_mapped_ipv6_peer_normalised(self):
        self.assertEqual(cip.client_ip(req("::ffff:192.0.2.10")), PEER)


class TrustedProxyTests(unittest.TestCase):
    """Future Tunnel design: header honoured only from an exact trusted peer."""

    def setUp(self):
        self.r = cip.ClientIPResolver(cip.parse_trusted_proxies(PROXY))

    def test_trusted_peer_uses_cf_connecting_ip(self):
        self.assertEqual(self.r.resolve(PROXY, {"CF-Connecting-IP": SPOOFED}), SPOOFED)
        self.assertEqual(self.r.resolve(PROXY, {"cf-connecting-ip": SPOOFED}), SPOOFED)

    def test_untrusted_peer_with_forged_cf_header_uses_peer(self):
        self.assertEqual(self.r.resolve(PEER, {"CF-Connecting-IP": SPOOFED}), PEER)

    def test_trusted_peer_never_uses_other_forwarding_headers(self):
        headers = {k: v for k, v in SPOOF_HEADERS.items() if k != "CF-Connecting-IP"}
        self.assertEqual(self.r.resolve(PROXY, headers), PROXY)

    def test_malformed_cf_header_falls_back_to_peer(self):
        bad = ["", " ", "not-an-ip", f"{SPOOFED}, 192.0.2.99", f"{SPOOFED}:443", f"[{SPOOFED}]",
               "999.1.1.1", "0.0.0.0", "::", "fe80::1%eth0", "1" * 100, f"{SPOOFED}\r\nX: y"]
        for value in bad:
            with self.subTest(value=value):
                self.assertEqual(self.r.resolve(PROXY, {"CF-Connecting-IP": value}), PROXY)

    def test_duplicate_cf_header_falls_back_to_peer(self):
        class Multi:
            def getlist(self, name):
                return [SPOOFED, "192.0.2.99"] if name == cip.CF_CONNECTING_IP else []
        self.assertEqual(self.r.resolve(PROXY, Multi()), PROXY)

    def test_ipv6_header_value_accepted_from_trusted_peer(self):
        self.assertEqual(self.r.resolve(PROXY, {"CF-Connecting-IP": "2001:db8::7"}), "2001:db8::7")

    def test_configure_replaces_process_resolver_and_restores(self):
        original = cip.get_resolver()
        try:
            cip.configure(self.r)
            self.assertEqual(cip.client_ip(req(PROXY, {"CF-Connecting-IP": SPOOFED})), SPOOFED)
            self.assertEqual(cip.client_ip(req(PEER, {"CF-Connecting-IP": SPOOFED})), PEER)
        finally:
            cip.configure(original)
        self.assertEqual(cip.client_ip(req(PROXY, {"CF-Connecting-IP": SPOOFED})), PROXY)


class TrustedProxyConfigTests(unittest.TestCase):
    def test_empty_means_none(self):
        for raw in (None, "", "   "):
            self.assertEqual(cip.parse_trusted_proxies(raw), frozenset())

    def test_exact_addresses_only(self):
        got = cip.parse_trusted_proxies(f"{PROXY}, 2001:db8::5")
        self.assertEqual({str(a) for a in got}, {PROXY, "2001:db8::5"})

    def test_rejects_ranges_wildcards_and_junk(self):
        for raw in ("*", "0.0.0.0/0", "198.51.100.0/24", "::/0", "0.0.0.0", "::", "224.0.0.1",
                    "localhost", "cloudflared", f"{PROXY},", f"{PROXY},,192.0.2.1", "198.51.100.5:80"):
            with self.subTest(raw=raw):
                with self.assertRaises(cip.TrustedProxyConfigError):
                    cip.parse_trusted_proxies(raw)


class RateLimitKeyTests(unittest.TestCase):
    def test_ipv4_unchanged(self):
        self.assertEqual(cip.rate_limit_key(PEER), PEER)

    def test_ipv6_grouped_by_64(self):
        a = cip.rate_limit_key("2001:db8:1:2:aaaa::1")
        b = cip.rate_limit_key("2001:db8:1:2:bbbb::2")
        c = cip.rate_limit_key("2001:db8:1:3::1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, "2001:db8:1:2::/64")

    def test_unknown(self):
        self.assertEqual(cip.rate_limit_key("unknown"), cip.UNKNOWN_CLIENT)


@unittest.skipUnless(importlib.util.find_spec("starlette"), "starlette not installed")
class StarletteRequestTests(unittest.TestCase):
    """Real Starlette Request objects, including repeated headers."""

    def _request(self, peer, raw_headers):
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/", "headers": raw_headers,
                 "client": (peer, 12345), "server": ("api", 8000), "scheme": "http", "query_string": b""}
        return Request(scope)

    def test_spoofed_headers_ignored(self):
        raw = [(k.lower().encode(), v.encode()) for k, v in SPOOF_HEADERS.items()]
        self.assertEqual(cip.client_ip(self._request(PEER, raw)), PEER)

    def test_repeated_cf_header_from_trusted_peer_rejected(self):
        r = cip.ClientIPResolver(cip.parse_trusted_proxies(PROXY))
        raw = [(b"cf-connecting-ip", SPOOFED.encode()), (b"cf-connecting-ip", b"192.0.2.99")]
        request = self._request(PROXY, raw)
        self.assertEqual(r.resolve(request.client.host, request.headers), PROXY)


class CentralisationGuardTests(unittest.TestCase):
    """R-IP.1: only client_ip.py may read forwarding headers or request.client."""

    FORBIDDEN_STRINGS = ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip",
                         "x-client-ip", "forwarded")

    def test_no_other_module_reads_forwarding_headers_or_peer(self):
        offenders = []
        for path in sorted(BACKEND_DIR.glob("*.py")):
            if path.name == "client_ip.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    low = node.value.strip().lower()
                    if low in self.FORBIDDEN_STRINGS:
                        offenders.append(f"{path.name}:{node.lineno} header {node.value!r}")
                if (isinstance(node, ast.Attribute) and node.attr == "client"
                        and isinstance(node.value, ast.Name) and node.value.id in ("request", "req")):
                    offenders.append(f"{path.name}:{node.lineno} request.client")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
