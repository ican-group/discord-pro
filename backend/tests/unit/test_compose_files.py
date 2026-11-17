"""Static checks of the API compose files (spec 004, R3/R6/R8, admin/resend overrides).

Parses the raw YAML (no interpolation, no Docker needed). Skipped if PyYAML or
the deploy/ directory is unavailable (e.g. inside the Docker test stage).
"""
import importlib.util
import unittest

from tests.unit._support import BACKEND_DIR

DEPLOY = BACKEND_DIR.parent / "deploy"
HAVE_YAML = importlib.util.find_spec("yaml") is not None
# deploy/ is outside the Docker build context, so these run on the host only.
HAVE_FILES = (DEPLOY / "docker-compose.api.yml").is_file()

SECRET_ENV_NAMES = {"MONGO_URL", "JWT_SECRET", "ENCRYPTION_KEY", "RESEND_API_KEY", "CODE_HMAC_KEY",
                    "ADMIN_PASSWORD", "ADMIN_EMAIL", "DISCORD_BOT_TOKEN", "DISCORD_CLIENT_SECRET",
                    "CLAUDE_API_KEY", "EMERGENT_LLM_KEY"}
HARDENING_KEYS = ("user", "read_only", "tmpfs", "cap_drop", "security_opt", "init", "pids_limit")


def load(name):
    import yaml
    return yaml.safe_load((DEPLOY / name).read_text(encoding="utf-8"))


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


@unittest.skipUnless(HAVE_YAML and HAVE_FILES, "PyYAML or deploy/ compose files not available")
class ComposeFileTests(unittest.TestCase):
    def setUp(self):
        self.base = load("docker-compose.api.yml")
        self.admin = load("docker-compose.admin.yml")
        self.resend = load("docker-compose.resend.yml")
        self.api = self.base["services"]["api"]

    def test_base_defines_only_api(self):
        self.assertEqual(set(self.base["services"]), {"api"})

    def test_base_never_references_admin_password(self):
        import json
        self.assertNotIn("admin_password", json.dumps(self.base))
        self.assertNotIn("admin_password", json.dumps(self.resend))

    def test_admin_password_only_in_admin_override(self):
        self.assertIn("admin_password", self.admin["secrets"])
        cli = self.admin["services"]["admin-cli"]
        self.assertIn("admin_password", cli["secrets"])
        self.assertEqual(cli["profiles"], ["admin"])
        self.assertEqual(cli["networks"], ["db"])
        self.assertEqual(cli.get("pull_policy"), "never")
        self.assertNotIn("api", self.admin["services"])

    def test_no_published_ports_anywhere(self):
        for name, doc in (("api", self.base), ("admin", self.admin), ("resend", self.resend)):
            for key in walk_keys(doc):
                with self.subTest(file=name):
                    self.assertNotIn(key, {"ports", "expose"})

    def test_public_origins(self):
        env = self.api["environment"]
        self.assertEqual(env["PUBLIC_APP_URL"], "https://bitbotpro.io")
        self.assertEqual(env["PUBLIC_API_URL"], "https://api.bitbotpro.io")
        self.assertNotIn("REACT_APP_BACKEND_URL", env)   # backend no longer reads it (spec 008, R-P6.3)
        self.assertEqual(env["REGISTRATION_MODE"], "invite_only")

    def test_production_cors_is_exactly_bitbotpro(self):
        env = self.api["environment"]
        self.assertEqual(env["CORS_ORIGINS"], "https://bitbotpro.io")
        self.assertEqual(env["APP_ENV"], "production")
        self.assertEqual(env["APP_MODE"], "api")

    def test_no_raw_secret_env_vars_only_file_paths(self):
        for svc_name, svc in (("api", self.api), ("admin-cli", self.admin["services"]["admin-cli"]),
                              ("resend-override", self.resend["services"]["api"])):
            env = svc.get("environment", {})
            with self.subTest(service=svc_name):
                self.assertFalse(SECRET_ENV_NAMES & set(env), set(env))
                for k, v in env.items():
                    if k.endswith("_FILE"):
                        self.assertTrue(str(v).startswith("/run/secrets/"), k)

    def test_api_gets_no_postgres_credentials(self):
        import json
        blob = json.dumps(self.api).lower() + json.dumps(self.resend).lower()
        for needle in ("postgres", "pg_", "kb_database_url", "kb_migrator", "5432"):
            self.assertNotIn(needle, blob)
        self.assertEqual(set(self.api["secrets"]), {"backend_mongo_url", "jwt_secret", "encryption_key",
                                                    "code_hmac_key"})
        self.assertEqual(self.api["environment"]["CODE_HMAC_KEY_FILE"], "/run/secrets/code_hmac_key")

    def test_networks(self):
        self.assertEqual(sorted(self.api["networks"]), ["db", "egress"])   # public edge only via an edge override
        self.assertNotIn("TRUSTED_PROXY_IPS", self.api["environment"])     # base trusts no proxy
        self.assertEqual(self.base["networks"]["db"], {"name": "bitbotpro_db", "external": True})
        self.assertEqual(self.base["networks"]["egress"]["name"], "bitbotpro_egress")

    def test_resend_override_is_minimal(self):
        api = self.resend["services"]["api"]
        self.assertEqual(api["environment"], {"RESEND_API_KEY_FILE": "/run/secrets/resend_api_key"})
        self.assertEqual(api["secrets"], ["resend_api_key"])
        self.assertEqual(set(self.resend["services"]), {"api"})

    def test_admin_cli_has_same_hardening_as_api(self):
        cli = self.admin["services"]["admin-cli"]
        for key in HARDENING_KEYS:
            with self.subTest(key=key):
                self.assertEqual(cli.get(key), self.api.get(key))
        self.assertEqual(self.api["user"], "10001:10001")
        self.assertTrue(self.api["read_only"])
        self.assertEqual(self.api["cap_drop"], ["ALL"])

    def test_database_compose_not_referenced_or_modified_by_api_files(self):
        import json
        for doc in (self.base, self.admin, self.resend):
            blob = json.dumps(doc)
            self.assertNotIn("postgres-data", blob)
            self.assertNotIn("mongo-data", blob)
            self.assertNotIn("volumes", doc)


DOCKERFILE = BACKEND_DIR / "Dockerfile"


@unittest.skipUnless(DOCKERFILE.is_file(), "Dockerfile not available (outside the Docker build context)")
class RuntimeCommandTests(unittest.TestCase):
    """spec 008 R-IP.3: uvicorn must never trust forwarded-IP headers."""

    def _cmd(self):
        import json
        import re
        text = DOCKERFILE.read_text(encoding="utf-8")
        runtime = text.split("AS runtime", 1)[1]
        m = re.search(r"^CMD\s+(\[.*?\])\s*$", runtime.replace("\\\n", " "), re.M | re.S)
        self.assertIsNotNone(m, "runtime CMD not found")
        return json.loads(m.group(1))

    def test_no_proxy_headers(self):
        cmd = self._cmd()
        self.assertEqual(cmd[:2], ["uvicorn", "server:app"])
        self.assertIn("--no-proxy-headers", cmd)
        self.assertNotIn("--proxy-headers", cmd)
        self.assertFalse(any(a.startswith("--forwarded-allow-ips") for a in cmd))

    def test_existing_runtime_flags_preserved(self):
        cmd = self._cmd()
        for flag in ("--no-server-header", "--timeout-graceful-shutdown"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--workers") + 1], "1")


TUNNEL = DEPLOY / "docker-compose.tunnel.yml"
CLOUDFLARED_CONFIG = DEPLOY / "cloudflared" / "config.yml"


@unittest.skipUnless(HAVE_YAML and HAVE_FILES and TUNNEL.is_file(), "deploy/ tunnel files not available")
class TunnelDeploymentTests(unittest.TestCase):
    """spec 008 Stage 10: Cloudflare Tunnel, trusted proxy, deployment safeguards."""

    def setUp(self):
        import yaml
        self.api_doc = load("docker-compose.edge-tunnel.yml")
        self.api = self.api_doc["services"]["api"]
        self.tunnel = load("docker-compose.tunnel.yml")
        self.cf = self.tunnel["services"]["cloudflared"]
        self.config = yaml.safe_load(CLOUDFLARED_CONFIG.read_text(encoding="utf-8"))

    def test_tunnel_is_profile_gated_hardened_and_portless(self):
        self.assertEqual(set(self.tunnel["services"]), {"cloudflared"})
        self.assertEqual(self.cf["profiles"], ["tunnel"])            # never started by a plain `up`
        for key in ("ports", "expose", "environment", "privileged", "network_mode"):
            self.assertNotIn(key, self.cf)
        self.assertTrue(self.cf["read_only"])
        self.assertEqual(self.cf["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", self.cf["security_opt"])
        self.assertEqual(self.cf["user"], "65532:65532")
        image = self.cf["image"]
        self.assertRegex(image, r"^cloudflare/cloudflared:\d{4}\.\d+\.\d+")   # pinned, never :latest
        self.assertEqual(self.cf["secrets"], ["cloudflared_credentials"])
        self.assertTrue(self.tunnel["secrets"]["cloudflared_credentials"]["file"].endswith("/cloudflared_credentials.json"))
        self.assertIn("./cloudflared/config.yml:/etc/cloudflared/config.yml:ro", self.cf["volumes"])
        self.assertNotIn("token", " ".join(self.cf["command"]).lower())

    def test_ingress_network_and_trusted_proxy_address(self):
        ingress = self.api_doc["networks"]["ingress"]
        self.assertEqual(ingress["name"], "bitbotpro_ingress")
        self.assertTrue(ingress["internal"])                         # no internet / LAN route
        self.assertEqual(ingress["ipam"]["config"][0]["subnet"], "10.250.250.0/29")
        api_ip = self.api["networks"]["ingress"]["ipv4_address"]
        cf_ip = self.cf["networks"]["ingress"]["ipv4_address"]
        self.assertEqual((api_ip, cf_ip), ("10.250.250.3", "10.250.250.2"))
        self.assertEqual(self.api["environment"]["TRUSTED_PROXY_IPS"], cf_ip)   # exactly cloudflared, nothing else
        self.assertEqual(self.tunnel["networks"]["ingress"], {"name": "bitbotpro_ingress", "external": True})
        self.assertNotIn("internal", self.tunnel["networks"]["tunnel_egress"])   # cloudflared needs outbound
        self.assertNotIn("ingress", self.cf["networks"]["tunnel_egress"] or {})

    def test_only_api_and_cloudflared_join_ingress(self):
        admin = load("docker-compose.admin.yml")
        self.assertNotIn("ingress", admin["services"]["admin-cli"]["networks"])
        self.assertEqual(sorted(self.api["networks"]), ["ingress"])            # added to db+egress by the override
        self.assertEqual(set(self.api_doc["services"]), {"api"})
        self.assertNotIn("db", self.cf["networks"])                  # tunnel never reaches the databases

    def test_cloudflared_routes_only_the_api_path_to_the_api(self):
        rules = self.config["ingress"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["hostname"], "api.bitbotpro.io")
        self.assertEqual(rules[0]["path"], "^/api/")
        self.assertEqual(rules[0]["service"], "http://10.250.250.3:8000")
        self.assertEqual(rules[-1], {"service": "http_status:404"})
        self.assertEqual(self.config["credentials-file"], "/run/secrets/cloudflared_credentials")
        self.assertTrue(self.config["no-autoupdate"])
        self.assertTrue(str(self.config["metrics"]).startswith("127.0.0.1:"))
        self.assertNotIn("tunnel", self.config)                      # ID comes from the command line

    def test_operational_scripts_never_put_secrets_in_argv(self):
        backup = (DEPLOY / "scripts" / "backup-restore-check.sh").read_text(encoding="utf-8")
        self.assertNotIn('--password "$(', backup)
        self.assertIn("--config /tmp/.bbp-dump.yml", backup)
        self.assertIn("--network \"$NET\"", backup)
        self.assertIn("docker network create --internal", backup)
        scanner = (DEPLOY / "scripts" / "scan-image-secrets.sh").read_text(encoding="utf-8")
        self.assertIn("grep -caF -f <(patterns)", scanner)            # patterns never written to disk
        self.assertIn("RESULT: INCOMPLETE", scanner)


EDGE_CADDY = DEPLOY / "docker-compose.edge-caddy.yml"
WG_TEMPLATE = DEPLOY / "wireguard" / "wg-bbp.conf.template"
CADDY_SITE = DEPLOY / "caddy" / "api.bitbotpro.io.caddy"


def _wg_values(text, key):
    import re
    return [m.group(1).strip() for m in re.finditer(rf"^{key}\s*=\s*(.+)$", text, re.M)]


@unittest.skipUnless(HAVE_YAML and HAVE_FILES and EDGE_CADDY.is_file(), "deploy/ edge files not available")
class CaddyWireGuardEdgeTests(unittest.TestCase):
    """spec 008 Stage 10 (selected edge): Caddy VPS -> WireGuard -> API."""

    def setUp(self):
        self.edge = load("docker-compose.edge-caddy.yml")
        self.api = self.edge["services"]["api"]
        self.wg = WG_TEMPLATE.read_text(encoding="utf-8")
        self.caddy = CADDY_SITE.read_text(encoding="utf-8")

    def test_api_published_only_on_the_wireguard_address(self):
        self.assertEqual(set(self.edge["services"]), {"api"})
        self.assertEqual(set(self.api), {"ports", "environment"})          # nothing else is changed
        self.assertEqual(self.api["ports"], ["10.50.0.20:8000:8000"])       # never 0.0.0.0, LAN or a DB port
        self.assertEqual(self.api["environment"], {"TRUSTED_PROXY_IPS": "10.50.0.1"})

    def test_wireguard_client_is_minimal_and_keyless(self):
        self.assertEqual(_wg_values(self.wg, "Address"), ["10.50.0.20/32"])
        self.assertEqual(_wg_values(self.wg, "AllowedIPs"), ["10.50.0.1/32"])     # no default route, no peers
        self.assertEqual(_wg_values(self.wg, "Endpoint"), ["37.27.153.47:51820"])
        self.assertEqual(_wg_values(self.wg, "PersistentKeepalive"), ["25"])
        self.assertEqual(_wg_values(self.wg, "PrivateKey"), [])                   # key file, loaded by PostUp
        self.assertIn("PostUp = wg set %i private-key /etc/wireguard/wg-bbp.key", self.wg)
        for forbidden in ("DNS", "ListenPort", "Table", "SaveConfig"):
            self.assertEqual(_wg_values(self.wg, forbidden), [], forbidden)
        self.assertNotIn("0.0.0.0/0", self.wg)

    TAG = "-m comment --comment bitbotpro-wg-bbp"

    def _rules(self, hook):
        """(chain, spec) for every firewall rule a hook installs/removes."""
        import re
        out = []
        for line in _wg_values(self.wg, hook):
            if "DOCKER-USER" not in line and "INPUT" not in line or line.startswith("iptables -N"):
                continue
            m = (re.search(r"\|\| iptables -I (\S+) 1 (.+)$", line) if hook == "PreUp"
                 else re.search(r"^while iptables -D (\S+) (.+?) 2>/dev/null; do :; done$", line))
            self.assertIsNotNone(m, line)
            out.append((m.group(1), m.group(2)))
        return out

    def test_firewall_rules_installed_before_address_and_removed_after(self):
        pre = _wg_values(self.wg, "PreUp")
        installed = self._rules("PreUp")
        self.assertEqual(len(installed), 4)
        for chain, spec in installed:
            with self.subTest(rule=spec):
                self.assertIn(self.TAG, spec)                     # countable / verifiable
                # idempotent install: only if absent (-C), before the address exists (PreUp)
                self.assertIn(f"iptables -C {chain} {spec} 2>/dev/null || iptables -I {chain} 1 {spec}", pre)
        self.assertEqual(sorted(installed), sorted(self._rules("PostDown")))   # every rule removed
        self.assertFalse(any("iptables" in r for r in _wg_values(self.wg, "PostUp")))
        self.assertEqual({c for c, _ in installed}, {"DOCKER-USER", "INPUT"})

    @staticmethod
    def _matches(spec, pkt):
        """Minimal iptables matcher for the options the template uses."""
        tok = spec.split()
        i, ok = 0, True
        while i < len(tok):
            neg = tok[i] == "!"
            if neg:
                i += 1
            opt = tok[i]
            if opt in ("-m",):
                i += 2
                continue
            if opt == "--comment":
                i += 2
                continue
            if opt == "-j":
                break
            val = tok[i + 1]
            got = {
                "-p": pkt["proto"], "-i": pkt["in"], "-s": pkt["src"],
                "--ctdir": pkt.get("dir"), "--ctorigdst": pkt["orig_dst"][0],
                "--ctorigdstport": str(pkt["orig_dst"][1]),
            }.get(opt)
            if opt == "--ctstate":
                hit = pkt["state"] in val.split(",")
            elif opt in ("-p", "-i", "-s", "--ctdir", "--ctorigdst", "--ctorigdstport"):
                hit = got == val.replace("%i", "wg-bbp")
            else:
                raise AssertionError(f"unsupported iptables option in template: {opt}")
            ok = ok and (hit != neg)
            i += 2
        return ok

    def _dropped(self, chain, pkt):
        return any(c == chain and self._matches(spec, pkt) for c, spec in self._rules("PreUp"))

    def test_firewall_rule_semantics(self):
        api = ("10.50.0.20", 8000)
        fwd = {   # DOCKER-USER: forwarded (DNATed) traffic
            "caddy request via tunnel": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": api, "src": "10.50.0.1", "in": "wg-bbp"}, False),
            "container reply to caddy": ({"proto": "tcp", "dir": "REPLY", "orig_dst": api, "src": "172.19.0.2", "in": "br-bitbotpro"}, False),
            "LAN host with a route": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": api, "src": "192.168.0.50", "in": "ens18"}, True),
            "LAN host spoofing caddy": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": api, "src": "10.50.0.1", "in": "ens18"}, True),
            "gridscan peer via tunnel": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": api, "src": "10.50.0.11", "in": "wg-bbp"}, True),
            "VPS pivot to home LAN": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": ("192.168.0.1", 80), "src": "10.50.0.1", "in": "wg-bbp"}, True),
            "VPS to a database container": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": ("172.19.0.3", 27017), "src": "10.50.0.1", "in": "wg-bbp"}, True),
            "VPS UDP via tunnel": ({"proto": "udp", "dir": "ORIGINAL", "orig_dst": ("192.168.0.1", 53), "src": "10.50.0.1", "in": "wg-bbp"}, True),
            "container egress to internet": ({"proto": "tcp", "dir": "ORIGINAL", "orig_dst": ("1.1.1.1", 443), "src": "172.19.0.2", "in": "br-bitbotpro"}, False),
        }
        for name, (pkt, expect) in fwd.items():
            with self.subTest(chain="DOCKER-USER", case=name):
                self.assertEqual(self._dropped("DOCKER-USER", pkt), expect)
        inp = {   # INPUT: traffic to the VM itself
            "VPS to VM ssh": ({"proto": "tcp", "state": "NEW", "orig_dst": ("10.50.0.20", 22), "src": "10.50.0.1", "in": "wg-bbp"}, True),
            "VPS to docker-proxy directly": ({"proto": "tcp", "state": "NEW", "orig_dst": api, "src": "10.50.0.1", "in": "wg-bbp"}, True),
            "VPS UDP to VM": ({"proto": "udp", "state": "NEW", "orig_dst": ("10.50.0.20", 53), "src": "10.50.0.1", "in": "wg-bbp"}, True),
            "VPS ping": ({"proto": "icmp", "state": "NEW", "orig_dst": ("10.50.0.20", 0), "src": "10.50.0.1", "in": "wg-bbp"}, False),
            "reply on tunnel": ({"proto": "tcp", "state": "ESTABLISHED", "orig_dst": ("10.50.0.1", 443), "src": "10.50.0.1", "in": "wg-bbp"}, False),
            "LAN to VM (unchanged)": ({"proto": "tcp", "state": "NEW", "orig_dst": ("192.168.0.32", 22), "src": "192.168.0.50", "in": "ens18"}, False),
        }
        for name, (pkt, expect) in inp.items():
            with self.subTest(chain="INPUT", case=name):
                self.assertEqual(self._dropped("INPUT", pkt), expect)

    def test_caddy_site_is_isolated_and_overwrites_client_ip(self):
        import re
        self.assertRegex(self.caddy, r"(?m)^api\.bitbotpro\.io \{$")
        sites = re.findall(r"(?m)^([A-Za-z0-9.*:-]+(?:, *[A-Za-z0-9.*:-]+)*) \{$", self.caddy)
        self.assertEqual(sites, ["api.bitbotpro.io"])                         # one site, no global options
        directives = "\n".join(l for l in self.caddy.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("gridscan", directives.lower())                       # comments may mention it
        self.assertIn("reverse_proxy 10.50.0.20:8000", self.caddy)
        self.assertIn("header_up CF-Connecting-IP {remote_host}", self.caddy)  # overwrite, never pass through
        self.assertIn("flush_interval -1", self.caddy)                         # SSE chat streaming
        self.assertIn("handle /api/* {", self.caddy)
        self.assertIn("respond 404", self.caddy)
        self.assertIsNone(re.search(r"(?m)^\s*log\b", self.caddy))           # paths carry tokens/secrets
        self.assertNotIn("encode", self.caddy)

    def test_edge_files_agree_with_each_other(self):
        wg_ip = _wg_values(self.wg, "Address")[0].split("/")[0]
        caddy_peer = _wg_values(self.wg, "AllowedIPs")[0].split("/")[0]
        bind_ip, host_port, _ = self.api["ports"][0].split(":")
        self.assertEqual(bind_ip, wg_ip)
        self.assertIn(f"reverse_proxy {wg_ip}:{host_port}", self.caddy)
        self.assertEqual(self.api["environment"]["TRUSTED_PROXY_IPS"], caddy_peer)
        self.assertIn(f"--ctorigdst {wg_ip} --ctorigdstport {host_port}", self.wg)

    def test_docker_waits_for_the_tunnel(self):
        dropin = (DEPLOY / "wireguard" / "docker-after-wg-bbp.conf").read_text(encoding="utf-8")
        self.assertIn("After=wg-quick@wg-bbp.service", dropin)
        self.assertIn("Wants=wg-quick@wg-bbp.service", dropin)


@unittest.skipUnless(HAVE_FILES and (DEPLOY / "scripts" / "api-compose.sh").is_file(), "deploy/ not available")
class ApiComposeWrapperTests(unittest.TestCase):
    """Every production API compose command must include both overrides (spec 008 Step 10)."""

    def test_wrapper_always_uses_both_overrides_in_order(self):
        import re
        src = (DEPLOY / "scripts" / "api-compose.sh").read_text(encoding="utf-8")
        files = re.findall(r"-f deploy/(docker-compose\.[a-z-]+\.yml)", src)
        self.assertEqual(files, ["docker-compose.api.yml", "docker-compose.edge-caddy.yml",
                                 "docker-compose.resend.yml"])
        self.assertIn("--env-file deploy/.env", src)
        self.assertIn("set -euo pipefail", src)
        self.assertIn("resend_api_key", src)                # refuses to run without the key file


if __name__ == "__main__":
    unittest.main()
