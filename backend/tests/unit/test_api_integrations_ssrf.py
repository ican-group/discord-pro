"""Spec 011 — API integrations SSRF regression tests (in-process).

All outbound traffic is intercepted: DNS answers come from a fake resolver and
HTTP requests go to an httpx MockTransport that records every request, so the
tests prove where requests would be sent, with which headers, and that blocked
requests never leave the process. One test runs a real local TLS server to
prove that IP pinning keeps certificate host-name verification.
No real network or database.
"""
import asyncio
import copy
import importlib.util
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.unit._support import BACKEND_DIR  # noqa: F401

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "motor", "httpx", "bcrypt", "jwt", "jose", "cryptography"))

TEST_ENV = {
    "APP_MODE": "api", "APP_ENV": "production", "CORS_ORIGINS": "https://bitbotpro.io",
    "MONGO_URL": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=300&connectTimeoutMS=300",
    "DB_NAME": "unit_test_never_used", "JWT_SECRET": "unit-test-jwt-secret-not-real-0123456789",
    "CODE_HMAC_KEY": "unit-test-code-hmac-key-not-real-abcdefghijklmnop",
    "PUBLIC_APP_URL": "https://bitbotpro.io", "PUBLIC_API_URL": "https://api.bitbotpro.io",
    "AUTH_RESPONSE_FLOOR_MS": "0", "ENCRYPTION_KEY": "dW5pdC10ZXN0LWZlcm5ldC1rZXktbm90LXJlYWwtMDA=",
}
SECRET = "UNITTEST-integration-secret-0000000000"
INTERNAL = "INTERNAL-SERVICE-CONTENT-must-not-leak"
PUBLIC_IP = "203.0.114.10"
EVIL_IP = "203.0.114.66"

server = None
client = None


def setUpModule():
    global server, client
    if not HAVE_DEPS:
        return
    for k in ("CLAUDE_API_KEY", "EMERGENT_LLM_KEY", "API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS"):
        os.environ.pop(k, None)
    os.environ.update(TEST_ENV)
    sys.modules.pop("server", None)
    import server as _server  # noqa: E402
    from fastapi.testclient import TestClient
    server = _server
    client = TestClient(server.app)


def tearDownModule():
    global server, client
    mc = getattr(server, "mongo_client", None)
    if mc is not None:
        mc.close()
    server = client = None


def run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    """Fake DNS + recording HTTP transport patched into outbound_guard."""
    DNS = {"api.example.com": [PUBLIC_IP], "docs.example.com": [PUBLIC_IP], "evil.example": [EVIL_IP],
           "rebind.example.com": [PUBLIC_IP], "internal-alias.example.com": ["172.20.0.2"],
           "metadata-alias.example.com": ["169.254.169.254"], "mixed.example.com": [PUBLIC_IP, "10.0.0.5"],
           "v6meta.example.com": ["fd00:ec2::254"], "loop6.example.com": ["::1"]}

    def setUp(self):
        import outbound_guard
        self.g = outbound_guard
        self.requests = []
        self.resolved = []
        self.handler = lambda req: self.json_response(200, {"ok": True})
        dns = dict(self.DNS)

        async def fake_resolve(host, port):
            self.resolved.append(host)
            if host not in dns:
                raise OSError("nxdomain")
            return list(dns[host])

        import httpx

        def record(request):
            self.requests.append(request)
            return self.handler(request)

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(record), timeout=timeout,
                                     follow_redirects=False, trust_env=False)
        for target, value in ((outbound_guard, ("_system_resolve", fake_resolve)),
                              (outbound_guard, ("_default_client", factory))):
            p = mock.patch.object(target, value[0], value[1])
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def json_response(status, body, headers=None):
        import httpx
        return httpx.Response(status, json=body, headers=headers or {})

    def safe_request(self, url, headers=None, params=None, **kw):
        import api_integrations
        return run(api_integrations.safe_api_request("GET", url, headers=headers, params=params, **kw))

    def hosts_contacted(self):
        return [r.headers.get("host") for r in self.requests]


# ══════════════════════════ guarded request behaviour ══════════════════════════

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class GuardedRequestTests(_Base):
    def assert_blocked_without_contact(self, url, **kw):
        r = self.safe_request(url, **kw)
        self.assertFalse(r["success"], url)
        self.assertEqual(r["status"], 0, url)
        self.assertTrue(r["error"].startswith("URL blocked"), r)
        self.assertEqual(self.requests, [], f"{url} was contacted")
        return r

    def test_legitimate_public_https_api(self):
        self.handler = lambda req: self.json_response(200, {"pools": [1, 2]})
        r = self.safe_request("https://api.example.com/v1/pools", headers={"Authorization": f"Bearer {SECRET}"},
                              params={"limit": "5"})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["data"], {"pools": [1, 2]})
        req = self.requests[0]
        self.assertEqual((req.url.scheme, req.url.host, req.url.path), ("https", PUBLIC_IP, "/v1/pools"))
        self.assertEqual(req.url.params.get("limit"), "5")
        self.assertEqual(req.headers["host"], "api.example.com")
        self.assertEqual(req.headers["authorization"], f"Bearer {SECRET}")
        self.assertEqual(req.extensions.get("sni_hostname"), "api.example.com")

    def test_redirects_are_never_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.requests.clear()
                self.handler = lambda req, s=status: self.json_response(
                    s, {"x": INTERNAL}, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
                r = self.safe_request("https://api.example.com/v1/x", headers={"Authorization": f"Bearer {SECRET}"})
                self.assertFalse(r["success"])
                self.assertEqual(r["status"], status)
                self.assertIn("Redirect not followed", r["error"])
                self.assertNotIn("data", r)
                self.assertEqual(len(self.requests), 1)            # the Location was not requested
                self.assertNotIn(INTERNAL, json.dumps(r))

    def test_dns_rebinding_cannot_redirect_the_connection(self):
        answers = iter([[PUBLIC_IP], ["127.0.0.1"]])

        async def rebinding(host, port):
            self.resolved.append(host)
            return next(answers)
        with mock.patch.object(self.g, "_system_resolve", rebinding):
            r = self.safe_request("https://rebind.example.com/x")
        self.assertTrue(r["success"])
        self.assertEqual(self.requests[0].url.host, PUBLIC_IP)    # pinned to the checked answer
        self.assertEqual(self.resolved, ["rebind.example.com"])    # resolved exactly once

    def test_any_internal_dns_answer_blocks(self):
        for host in ("internal-alias.example.com", "metadata-alias.example.com", "mixed.example.com",
                     "v6meta.example.com", "loop6.example.com", "unknown.example.com"):
            with self.subTest(host=host):
                self.assert_blocked_without_contact(f"https://{host}/x")

    def test_alternative_ip_encodings(self):
        for host in ("2130706433", "0x7f000001", "0x7f.1", "0177.0.0.1", "127.1", "0", "[::ffff:127.0.0.1]",
                     "[::1]", "[::ffff:a9fe:a9fe]", "[64:ff9b::a9fe:a9fe]", "[2002:a9fe:a9fe::]", "127.0.0.1",
                     "10.0.0.1", "0.0.0.0", "[::]"):
            with self.subTest(host=host):
                self.assert_blocked_without_contact(f"https://{host}/x")

    def test_internal_docker_hostnames(self):
        for url in ("https://bitbotpro-mongo/", "https://bitbotpro-mongo:27017/", "https://bitbotpro-postgres:5432/",
                    "https://bitbotpro-api:8000/api/health", "https://localhost/", "https://api.internal/",
                    "http://bitbotpro-mongo:27017/"):
            with self.subTest(url=url):
                self.assert_blocked_without_contact(url)
        self.assertEqual(self.resolved, [])       # rejected before any DNS lookup

    def test_cloud_metadata_endpoints(self):
        for url in ("https://169.254.169.254/latest/meta-data/", "http://169.254.169.254/",
                    "https://[fd00:ec2::254]/", "https://100.100.100.200/latest/meta-data/",
                    "https://169.254.170.2/v2/credentials", "https://metadata.google.internal/computeMetadata/v1/",
                    "https://metadata-alias.example.com/"):
            with self.subTest(url=url):
                self.assert_blocked_without_contact(url)

    def test_malicious_host_and_header_injection(self):
        for headers in ({"Host": "bitbotpro-mongo"}, {"host": "169.254.169.254"}, {"X-Forwarded-Host": "evil.example"},
                        {"X-API-Key": "a\r\nHost: evil.example"}, {"Transfer-Encoding": "chunked"},
                        {"Proxy-Authorization": "x"}):
            with self.subTest(headers=headers):
                self.assert_blocked_without_contact("https://api.example.com/x", headers=headers)
        r = self.safe_request("https://api.example.com/x", headers={"X-API-Key": SECRET})
        self.assertTrue(r["success"])
        self.assertEqual(self.requests[0].headers.get_list("host"), ["api.example.com"])

    def test_credentials_in_url_are_rejected(self):
        for url in ("https://user:pass@api.example.com/", "https://api.example.com@evil.example/",
                    "https://api.example.com#@evil.example/", "https://api.example.com\\@evil.example/"):
            with self.subTest(url=url):
                self.assert_blocked_without_contact(url)

    def test_http_is_rejected_in_production(self):
        self.assert_blocked_without_contact("http://api.example.com/x")

    def test_upstream_errors_do_not_expose_content(self):
        import httpx
        cases = [
            (lambda req: httpx.Response(500, text=INTERNAL), "HTTP 500 Internal Server Error"),
            (lambda req: httpx.Response(404, json={"error": INTERNAL}), "HTTP 404 Not Found"),
        ]
        for handler, expected in cases:
            with self.subTest(expected=expected):
                self.handler = handler
                r = self.safe_request("https://api.example.com/x")
                self.assertEqual(r["error"], expected)
                self.assertNotIn(INTERNAL, json.dumps(r))
        for exc, expected in ((httpx.ConnectError(f"[Errno 111] {INTERNAL} 10.0.0.5"), "Could not connect to the destination."),
                              (httpx.ConnectError(f"[SSL: CERTIFICATE_VERIFY_FAILED] {INTERNAL}"), "TLS certificate verification failed."),
                              (httpx.ReadTimeout(INTERNAL), "The request timed out."),
                              (httpx.RemoteProtocolError(INTERNAL), "The request failed.")):
            with self.subTest(exc=type(exc).__name__):
                def boom(req, e=exc):
                    raise e
                self.handler = boom
                r = self.safe_request("https://api.example.com/x")
                self.assertEqual(r["error"], expected)
                self.assertNotIn(INTERNAL, json.dumps(r))

    def test_response_size_is_capped(self):
        import httpx
        self.handler = lambda req: httpx.Response(200, content=b"x" * 5000)
        r = self.safe_request("https://api.example.com/x", max_size=1000)
        self.assertFalse(r["success"])
        self.assertIn("too large", r["error"])
        self.assertNotIn("data", r)


# ══════════════════════════ route-level behaviour ══════════════════════════

@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class IntegrationRouteTests(_Base):
    def setUp(self):
        super().setUp()
        from tests.unit._fake_motor import patch_motor
        import api_integrations
        self.store = patch_motor(self)
        self.store.col("users").append({"id": "alice", "email": "alice@example.org", "username": "alice",
                                        "role": "user", "is_verified": True, "token_version": 0, "hashed_password": "x"})
        self.store.col("organisations").append({"id": "org-a", "name": "A", "workspace_id": "ws-a",
                                                "owner_id": "alice", "member_ids": ["alice"]})
        self.store.col("bots").append({"id": "bot-a", "org_id": "org-a", "name": "Bot"})
        self.h = {"Authorization": f"Bearer {server.create_access_token('alice', 'alice@example.org', 'user', 0)}",
                  "X-Instance-ID": "bot-a"}
        self.store.col("api_integrations").append({
            "id": "i1", "instance_id": "bot-a", "name": "Pools", "base_url": "https://api.example.com/v1",
            "auth_type": "bearer", "auth_header_name": "", "auth_query_param": "",
            "auth_encrypted_value": api_integrations.encrypt_value(SECRET), "auth_value_set": True,
            "doc_source_type": "openapi", "doc_source_url": "https://docs.example.com/openapi.json", "enabled": True})
        self.store.col("api_endpoints").append({
            "id": "ep1", "integration_id": "i1", "instance_id": "bot-a", "method": "GET", "enabled": True,
            "path": "/pools/{pool_id}", "parameters": [
                {"name": "pool_id", "in": "path", "required": True}, {"name": "limit", "in": "query"}]})

    def execute(self, params, path=None):
        import api_integrations_routes as r
        ep = copy.deepcopy(self.store.col("api_endpoints")[0])
        if path is not None:
            ep["path"] = path
        intg = self.store.col("api_integrations")[0]
        return run(r.execute_endpoint(intg, ep, params))

    def test_execute_legitimate_call(self):
        r = client.post("/api/integrations/test-endpoint", headers=self.h,
                        json={"integration_id": "i1", "endpoint_id": "ep1", "parameters": {"pool_id": "pool1xyz", "limit": 3}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["success"], r.json())
        req = self.requests[0]
        self.assertEqual(req.headers["host"], "api.example.com")
        self.assertEqual(req.url.path, "/v1/pools/pool1xyz")
        self.assertEqual(req.headers["authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, r.text)

    def test_path_parameters_cannot_change_destination(self):
        for value in ("@evil.example", "//evil.example/x", "../../admin", "x?y=1#z", "x\r\nHost: evil.example",
                      "http://169.254.169.254/", "%2e%2e/"):
            with self.subTest(value=value):
                self.requests.clear()
                r = self.execute({"pool_id": value})
                self.assertTrue(r["success"], r)
                self.assertEqual(self.hosts_contacted(), ["api.example.com"])
                self.assertTrue(self.requests[0].url.raw_path.startswith(b"/v1/pools/"))
                self.assertNotIn(b"/", self.requests[0].url.raw_path[len(b"/v1/pools/"):].split(b"?")[0])

    def test_malicious_documented_paths_cannot_change_destination(self):
        for path in ("evil.example/x", "@evil.example/x", ".evil.example/x"):
            with self.subTest(path=path):
                self.requests.clear()
                r = self.execute({"pool_id": "p"}, path=path)
                if self.requests:
                    self.assertEqual(self.hosts_contacted(), ["api.example.com"])
                else:
                    self.assertTrue(r["error"].startswith("URL blocked"), r)

    def test_legacy_stored_host_header_name_is_blocked(self):
        intg = self.store.col("api_integrations")[0]
        intg.update({"auth_type": "api_key_header", "auth_header_name": "Host"})
        r = self.execute({"pool_id": "p"})
        self.assertFalse(r["success"])
        self.assertIn("URL blocked", r["error"])
        self.assertEqual(self.requests, [])

    def test_credentials_never_sent_to_documentation_on_another_origin(self):
        spec = {"openapi": "3.0.0", "paths": {"/pools": {"get": {"summary": "List pools"}}}}
        self.handler = lambda req: self.json_response(200, spec)
        r = client.post("/api/integrations/i1/discover", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.hosts_contacted(), ["docs.example.com"])
        self.assertNotIn("authorization", self.requests[0].headers)

    def test_credentials_sent_to_documentation_on_the_same_origin(self):
        self.store.col("api_integrations")[0]["doc_source_url"] = "https://api.example.com/openapi.json"
        self.handler = lambda req: self.json_response(200, {"openapi": "3.0.0", "paths": {}})
        client.post("/api/integrations/i1/discover", headers=self.h)
        self.assertEqual(self.requests[0].headers.get("authorization"), f"Bearer {SECRET}")

    def test_discover_does_not_follow_redirects_or_leak(self):
        self.handler = lambda req: self.json_response(302, {"x": INTERNAL}, headers={"Location": "http://10.0.0.1/"})
        r = client.post("/api/integrations/i1/discover", headers=self.h)
        self.assertEqual(r.status_code, 502)
        self.assertIn("Redirect not followed", r.json()["detail"])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(INTERNAL, r.text)

    def test_discover_to_internal_doc_url_is_blocked(self):
        for doc in ("https://bitbotpro-mongo:27017/", "https://169.254.169.254/latest/meta-data/",
                    "https://internal-alias.example.com/openapi.json"):
            with self.subTest(doc=doc):
                self.requests.clear()
                self.store.col("api_integrations")[0]["doc_source_url"] = doc
                r = client.post("/api/integrations/i1/discover", headers=self.h)
                self.assertEqual(r.status_code, 502, r.text)
                self.assertEqual(self.requests, [])

    def test_test_connection_sends_credentials_only_to_base_origin(self):
        self.store.col("api_endpoints").clear()       # probe the docs URL and health paths
        self.handler = lambda req: self.json_response(404, {})
        r = client.post("/api/integrations/i1/test-connection", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        for req in self.requests:
            has_auth = "authorization" in req.headers
            self.assertEqual(has_auth, req.headers["host"] == "api.example.com", req.headers["host"])
        self.assertIn("docs.example.com", self.hosts_contacted())

    def create(self, **body):
        base = {"name": "X", "base_url": "https://api.example.com/v1"}
        base.update(body)
        return client.post("/api/integrations", headers=self.h, json=base)

    def test_create_and_update_validation(self):
        bad = [
            {"base_url": "http://api.example.com"}, {"base_url": "https://bitbotpro-mongo:27017"},
            {"base_url": "https://169.254.169.254"}, {"base_url": "https://2130706433/"},
            {"base_url": "https://user:pw@api.example.com"}, {"base_url": "https://api.example.com/v1?x=1"},
            {"base_url": "https://api.example.com#x"}, {"doc_source_url": "https://localhost/openapi.json"},
            {"doc_source_url": "file:///etc/passwd"}, {"auth_type": "api_key_header", "auth_header_name": "Host"},
            {"auth_type": "api_key_header", "auth_header_name": "X-Forwarded-For"},
            {"auth_type": "api_key_header", "auth_header_name": "X-Key\r\nHost: evil"},
            {"auth_type": "oauth-magic"}, {"auth_type": "api_key_query", "auth_query_param": "a&b=c"},
        ]
        n = len(self.store.col("api_integrations"))
        for body in bad:
            with self.subTest(body=body):
                r = self.create(**body)
                self.assertEqual(r.status_code, 400, r.text)
                r = client.put("/api/integrations/i1", headers=self.h, json=body)
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(len(self.store.col("api_integrations")), n)
        self.assertEqual(self.store.col("api_integrations")[0]["base_url"], "https://api.example.com/v1")
        r = self.create(base_url="https://API.example.com/v1/", auth_type="api_key_header", auth_header_name="X-API-Key")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["base_url"], "https://api.example.com/v1")


# ══════════════════════════ TLS verification with IP pinning ══════════════════════════

def _make_certs(directory: Path, hostname: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (directory / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))


@unittest.skipUnless(HAVE_DEPS, "backend dependencies not installed")
class TLSPinningTests(unittest.TestCase):
    """A real TLS server on 127.0.0.1 presenting a certificate for legit.test."""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        _make_certs(d, "legit.test")
        cls.ca = str(d / "ca.pem")

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"host": self.headers.get("Host")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(d / "leaf.pem"), str(d / "leaf.key"))
        cls.httpd.socket = ctx.wrap_socket(cls.httpd.socket, server_side=True)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, host):
        import httpx
        import outbound_guard as g
        env = {"APP_ENV": "development", "API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS": "1"}

        async def resolver(h, port):
            return ["127.0.0.1"]

        def factory(timeout):
            return httpx.AsyncClient(verify=ssl.create_default_context(cafile=self.ca), timeout=timeout,
                                     follow_redirects=False, trust_env=False)
        return run(g.guarded_request("GET", f"https://{host}:{self.port}/x", env=env,
                                     resolver=resolver, client_factory=factory, timeout=5.0))

    def test_certificate_for_the_requested_host_is_accepted(self):
        r = self.request("legit.test")
        self.assertEqual(r["status"], 200)
        self.assertEqual(json.loads(r["content"]), {"host": f"legit.test:{self.port}"})

    def test_certificate_for_another_host_is_rejected_even_on_the_pinned_ip(self):
        import outbound_guard as g
        with self.assertRaises(g.OutboundError) as ctx:
            self.request("other.test")
        self.assertEqual(str(ctx.exception), "TLS certificate verification failed.")


class SourceGuardTests(unittest.TestCase):
    """No integration module may open its own HTTP clients (all traffic via outbound_guard)."""

    def test_integration_modules_have_no_raw_http_clients(self):
        for name in ("api_integrations.py", "api_integrations_routes.py", "api_diagnostics.py", "api_diagnostics_routes.py"):
            with self.subTest(module=name):
                text = (Path(BACKEND_DIR) / name).read_text()
                for needle in ("httpx.", "aiohttp", "requests.get", "requests.post", "urlopen", "follow_redirects=True"):
                    self.assertNotIn(needle, text, f"{name} uses {needle}")


if __name__ == "__main__":
    unittest.main()
