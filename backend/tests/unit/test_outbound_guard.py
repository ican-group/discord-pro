"""Spec 010 — outbound request guard (Ollama SSRF, Zendesk host injection).
Standard library only; runs on the host and in the Docker test target."""
import asyncio
import unittest

from tests.unit._support import BACKEND_DIR  # noqa: F401  (adds backend/ to sys.path)
import outbound_guard as g

PROD = {"APP_ENV": "production", "OLLAMA_ALLOWED_BASE_URLS": "https://ollama.example.com, http://93.184.215.14:11434"}


def run(coro):
    return asyncio.run(coro)


class ZendeskSubdomainTests(unittest.TestCase):
    def test_accepted_spellings(self):
        cases = {"acme": "acme", "ACME": "acme", " acme ": "acme", "acme.zendesk.com": "acme",
                 "https://acme.zendesk.com": "acme", "https://acme.zendesk.com/": "acme",
                 "Acme.Zendesk.Com": "acme", "acme\r\n": "acme", "\tacme": "acme", "a": "a", "a-b-1": "a-b-1", "midnight-gd": "midnight-gd",
                 "x" * 63: "x" * 63}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(g.normalize_zendesk_subdomain(raw), want)

    def test_rejected_host_injection_and_url_manipulation(self):
        bad = [
            "", "   ", None, 5, "-acme", "acme-", "x" * 64, "ac_me", "ac me", "acme.", ".acme",
            # fragments / paths / queries / ports / credentials / schemes
            "evil.example#", "evil.example#.zendesk.com", "evil.example/", "evil.example/x",
            "acme.zendesk.com/path", "acme.zendesk.com?x=1", "acme?", "acme#frag",
            "acme:443", "acme.zendesk.com:443", "user@evil.example", "acme@evil.example",
            "http://acme.zendesk.com", "ftp://acme.zendesk.com", "//acme.zendesk.com",
            "https://acme.zendesk.com//", "javascript:alert(1)",
            # extra labels / look-alike domains
            "acme.zendesk.com.evil.example", "evil.example", "acme.evil", "acme.zendesk.co",
            "acme.zendesk.com.", "a.b",
            # encodings, control characters, unicode
            "acme%2eevil", "acme%23", "acme\\evil", "acme\nevil", "acme\r\nHost: evil", "ac\tme", "acme\x00",
            "аcme", "acmé", "ａcme",
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(g.OutboundBlocked):
                    g.normalize_zendesk_subdomain(raw)

    def test_urls_only_ever_target_zendesk(self):
        self.assertEqual(g.zendesk_url("acme", "/api/v2/users/me.json"),
                         "https://acme.zendesk.com/api/v2/users/me.json")
        self.assertEqual(g.zendesk_url("https://Acme.zendesk.com/", "/oauth/tokens"),
                         "https://acme.zendesk.com/oauth/tokens")
        self.assertEqual(g.zendesk_url("acme", "/api/v2/tickets/12.json?include=users"),
                         "https://acme.zendesk.com/api/v2/tickets/12.json?include=users")

    def test_url_rejects_bad_subdomain_or_path(self):
        for sub, path in [("evil.example#", "/x"), ("acme", "//evil.example/x"), ("acme", "x"),
                          ("acme", "/x#frag"), ("acme", "/x\\y"), ("acme", "/x y"), ("acme", "/x\r\nHost: evil"),
                          ("acme", "/@evil.example"), ("acme", "")]:
            with self.subTest(sub=sub, path=path):
                with self.assertRaises(g.OutboundBlocked):
                    g.zendesk_url(sub, path)


class OllamaOriginTests(unittest.TestCase):
    def test_normalize_origin(self):
        cases = {
            "https://Ollama.Example.com": "https://ollama.example.com",
            "https://ollama.example.com/": "https://ollama.example.com",
            "https://ollama.example.com:443": "https://ollama.example.com",
            "http://203.0.113.10:11434/": "http://203.0.113.10:11434",
            "http://[2001:DB8::1]:11434": "http://[2001:db8::1]:11434",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(g.normalize_origin(raw), want)

    def test_normalize_origin_rejects(self):
        for raw in ["", None, "ollama.example.com", "ftp://ollama.example.com", "file:///etc/passwd",
                    "gopher://127.0.0.1:6379", "https://user:pw@ollama.example.com", "https://a@ollama.example.com",
                    "https://ollama.example.com/api", "https://ollama.example.com/?x=1", "https://ollama.example.com#f",
                    "https://ollama.example.com?", "https://ollama.example.com:99999", "https://ollama.example.com:abc",
                    "https://ollama.example.com\\@evil", "https://ollama .example.com", "https://ollama.example.com\n/x",
                    "https://ollama.exa\tmple.com",
                    "https://", "https://-bad-.example..com"]:
            with self.subTest(raw=raw):
                with self.assertRaises(g.OutboundBlocked):
                    g.normalize_origin(raw)

    def test_disabled_without_allowlist(self):
        for env in ({}, {"APP_ENV": "production"}, {"OLLAMA_ALLOWED_BASE_URLS": "  "}):
            with self.subTest(env=env):
                self.assertFalse(g.ollama_enabled(env))
                with self.assertRaises(g.OutboundBlocked) as ctx:
                    g.check_ollama_base_url("https://ollama.example.com", env)
                self.assertEqual(str(ctx.exception), g.OLLAMA_DISABLED_ERROR)

    def test_invalid_allowlist_fails_closed(self):
        env = {"OLLAMA_ALLOWED_BASE_URLS": "https://ok.example.com, https://bad.example.com/path"}
        self.assertFalse(g.ollama_enabled(env))
        with self.assertRaises(g.OutboundBlocked):
            g.check_ollama_base_url("https://ok.example.com", env)

    def test_only_exact_allowlisted_origins(self):
        self.assertEqual(g.check_ollama_base_url("https://OLLAMA.example.com/", PROD), "https://ollama.example.com")
        self.assertEqual(g.check_ollama_base_url("http://93.184.215.14:11434", PROD), "http://93.184.215.14:11434")
        for raw in ["http://ollama.example.com", "https://ollama.example.com:8443", "https://evil.example.com",
                    "https://ollama.example.com.evil.example", "http://93.184.215.14:11435",
                    "http://localhost:11434", "http://127.0.0.1:11434", "http://169.254.169.254",
                    "http://bitbotpro-mongo:27017", "http://10.50.0.1"]:
            with self.subTest(raw=raw):
                with self.assertRaises(g.OutboundBlocked):
                    g.check_ollama_base_url(raw, PROD)


class AddressPolicyTests(unittest.TestCase):
    BLOCKED_ALWAYS = ["169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254", "fe80::1",
                      "fe80::1%eth0", "0.0.0.0", "::", "224.0.0.1", "ff02::1", "240.0.0.1", "255.255.255.255",
                      "100.64.0.1", "64:ff9b::a9fe:a9fe", "not-an-ip"]
    PRIVATE = ["127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1", "10.0.0.1", "10.50.0.1", "172.16.0.1",
               "172.20.0.3", "192.168.0.32", "fc00::1", "fd12:3456::1", "2002:7f00:1::", "2002:a00:1::"]
    PUBLIC = ["8.8.8.8", "203.0.114.1", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"]

    def test_production_blocks_everything_internal(self):
        for a in self.BLOCKED_ALWAYS + self.PRIVATE:
            with self.subTest(addr=a):
                self.assertFalse(g.address_allowed(a))
        for a in self.PUBLIC:
            with self.subTest(addr=a):
                self.assertTrue(g.address_allowed(a))

    def test_development_private_mode_still_blocks_metadata_and_link_local(self):
        for a in self.BLOCKED_ALWAYS:
            with self.subTest(addr=a):
                self.assertFalse(g.address_allowed(a, allow_private=True))
        for a in self.PRIVATE:
            with self.subTest(addr=a):
                self.assertTrue(g.address_allowed(a, allow_private=True))

    def test_private_mode_is_ignored_in_production(self):
        self.assertFalse(g.ollama_private_networks_allowed({"APP_ENV": "production", "OLLAMA_ALLOW_PRIVATE_NETWORKS": "1"}))
        self.assertFalse(g.ollama_private_networks_allowed({"OLLAMA_ALLOW_PRIVATE_NETWORKS": "1"}))  # default env = production
        self.assertTrue(g.ollama_private_networks_allowed({"APP_ENV": "development", "OLLAMA_ALLOW_PRIVATE_NETWORKS": "1"}))
        self.assertFalse(g.ollama_private_networks_allowed({"APP_ENV": "development"}))


class ResolutionAndPinningTests(unittest.TestCase):
    def resolver(self, answers):
        calls = []

        async def resolve(host, port):
            calls.append((host, port))
            if isinstance(answers, Exception):
                raise answers
            return list(answers)
        return resolve, calls

    def test_public_answer_is_pinned_and_resolved_once(self):
        res, calls = self.resolver(["203.0.114.7", "203.0.114.8"])
        url, headers, ext = run(g.prepare_ollama_request("https://ollama.example.com", PROD, resolver=res))
        self.assertEqual(url, "https://203.0.114.7:443/api/chat")
        self.assertEqual(headers, {"Host": "ollama.example.com"})
        self.assertEqual(ext, {"sni_hostname": "ollama.example.com"})
        self.assertEqual(calls, [("ollama.example.com", 443)])

    def test_dns_answers_with_any_internal_address_are_blocked(self):
        for answers in (["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["203.0.114.7", "10.0.0.5"],
                        ["::1"], ["203.0.114.7", "fd00:ec2::254"], ["::ffff:10.0.0.1"], []):
            with self.subTest(answers=answers):
                res, _ = self.resolver(answers)
                with self.assertRaises(g.OutboundBlocked):
                    run(g.prepare_ollama_request("https://ollama.example.com", PROD, resolver=res))

    def test_rebinding_cannot_change_the_connected_address(self):
        """The first (checked) answer is used for the connection; a later lookup never happens."""
        answers = iter([["203.0.114.7"], ["127.0.0.1"]])

        async def rebinding(host, port):
            return next(answers)
        url, _, _ = run(g.prepare_ollama_request("https://ollama.example.com", PROD, resolver=rebinding))
        self.assertTrue(url.startswith("https://203.0.114.7:443/"))
        self.assertEqual(next(answers), ["127.0.0.1"])  # second answer was never consumed

    def test_resolution_failure_is_blocked(self):
        res, _ = self.resolver(OSError("nxdomain"))
        with self.assertRaises(g.OutboundBlocked):
            run(g.prepare_ollama_request("https://ollama.example.com", PROD, resolver=res))

    def test_ip_literal_origins_are_checked_without_dns(self):
        res, calls = self.resolver(["10.0.0.1"])
        url, headers, ext = run(g.prepare_ollama_request("http://93.184.215.14:11434", PROD, resolver=res))
        self.assertEqual(calls, [])
        self.assertEqual((url, headers, ext), ("http://93.184.215.14:11434/api/chat", {"Host": "93.184.215.14:11434"}, {}))

    def test_allowlisted_internal_ip_is_still_blocked(self):
        env = {"APP_ENV": "production", "OLLAMA_ALLOWED_BASE_URLS":
               "http://10.0.0.9:11434,http://127.0.0.1:11434,http://169.254.169.254,http://203.0.113.10:11434"}
        for raw in ("http://10.0.0.9:11434", "http://127.0.0.1:11434", "http://169.254.169.254",
                    "http://203.0.113.10:11434"):  # last: TEST-NET-3 documentation range, not global
            with self.subTest(raw=raw):
                with self.assertRaises(g.OutboundBlocked):
                    run(g.prepare_ollama_request(raw, env))

    def test_development_private_mode_allows_local_ollama(self):
        env = {"APP_ENV": "development", "OLLAMA_ALLOW_PRIVATE_NETWORKS": "1",
               "OLLAMA_ALLOWED_BASE_URLS": "http://localhost:11434"}
        res, _ = self.resolver(["127.0.0.1"])
        url, headers, _ = run(g.prepare_ollama_request("http://localhost:11434", env, resolver=res))
        self.assertEqual((url, headers), ("http://127.0.0.1:11434/api/chat", {"Host": "localhost:11434"}))

    def test_ipv6_pinning_uses_brackets(self):
        url, headers, _ = g.pinned_request("https://ollama.example.com", "2606:4700::1", "/api/chat")
        self.assertEqual(url, "https://[2606:4700::1]:443/api/chat")


class IntegrationURLPolicyTests(unittest.TestCase):
    """Spec 011: strict URL/host/header policy for API integrations (production)."""
    ENV = {"APP_ENV": "production"}

    def test_alternative_ip_encodings_are_rejected(self):
        for host in ["2130706433", "0x7f000001", "0x7f.1", "0177.0.0.1", "0177.1", "127.1", "127.0.1",
                     "01.2.3.4", "1.2.3.4.5", "0", "0x0", "000.000.000.000", "4294967295",
                     "[fe80::1%25eth0]", "[::ffff:7f00:1%1]", "١٢٧.0.0.1", "①②⑦.0.0.1"]:
            with self.subTest(host=host):
                with self.assertRaises(g.OutboundBlocked):
                    g.parse_integration_url(f"https://{host}/x", self.ENV)

    def test_internal_names_are_rejected_in_production(self):
        for host in ["bitbotpro-mongo", "bitbotpro-postgres", "bitbotpro-api", "mongo", "localhost",
                     "api.localhost", "metadata.google.internal", "x.internal", "nas.local", "router.lan",
                     "box.home.arpa", "db.localdomain"]:
            with self.subTest(host=host):
                with self.assertRaises(g.OutboundBlocked):
                    g.parse_integration_url(f"https://{host}:27017/", self.ENV)

    def test_malformed_and_dangerous_urls_are_rejected(self):
        for url in ["http://api.example.com/", "ftp://api.example.com/", "file:///etc/passwd", "gopher://x.example.com/",
                    "https://user:pw@api.example.com/", "https://api.example.com@evil.example/",
                    "https://api.example.com#@evil.example/", "https://api.example.com/x#frag",
                    "https://api.example.com\\@evil.example/", "https://api.example.com:0/", "https://api.example.com:99999/",
                    "https://api.example.com:abc/", "https://api.exämple.com/", "https://api.example.com./",
                    "https://api .example.com/", "https://api.example.com/\nHost: evil", "//api.example.com/", "api.example.com",
                    "https://", "", None]:
            with self.subTest(url=url):
                with self.assertRaises(g.OutboundBlocked):
                    g.parse_integration_url(url, self.ENV)

    def test_legitimate_https_urls(self):
        u = g.parse_integration_url("https://API.Example.com/v1/pools?limit=5", self.ENV)
        self.assertEqual(u, {"scheme": "https", "host": "api.example.com", "port": 443,
                             "origin": "https://api.example.com", "target": "/v1/pools?limit=5"})
        self.assertEqual(g.parse_integration_url("https://api.example.com:8443", self.ENV)["origin"],
                         "https://api.example.com:8443")
        self.assertEqual(g.parse_integration_url("https://93.184.215.14/x", self.ENV)["host"], "93.184.215.14")

    def test_development_private_mode(self):
        dev = {"APP_ENV": "development", "API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS": "1"}
        self.assertEqual(g.parse_integration_url("http://localhost:8001/x", dev)["origin"], "http://localhost:8001")
        self.assertTrue(g.integrations_private_networks_allowed(dev))
        self.assertFalse(g.integrations_private_networks_allowed({"API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS": "1"}))
        with self.assertRaises(g.OutboundBlocked):   # alt encodings are rejected even in development
            g.parse_integration_url("http://2130706433/", dev)

    def test_header_policy(self):
        for name in ["Host", "host", " HOST ", "Content-Length", "Transfer-Encoding", "Connection", "Upgrade",
                     "Proxy-Authorization", "proxy-anything", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host",
                     "X-Real-IP", "CF-Connecting-IP", "Sec-Fetch-Site", "Bad Name", "X:Y", "", "a" * 200, None]:
            with self.subTest(name=name):
                with self.assertRaises(g.OutboundBlocked):
                    g.validate_header_name(name)
        for value in ["a\r\nHost: evil", "a\nb", "a\x00", 5, None]:
            with self.subTest(value=value):
                with self.assertRaises(g.OutboundBlocked):
                    g.sanitize_headers({"X-API-Key": value})
        self.assertEqual(g.sanitize_headers({"Authorization": "Bearer t", "X-API-Key": "k"}),
                         {"Authorization": "Bearer t", "X-API-Key": "k"})


if __name__ == "__main__":
    unittest.main()
