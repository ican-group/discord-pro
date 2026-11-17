"""spec 008 Stage 10: regression tests for the WireGuard/Caddy operator scripts.

Every script runs against MOCKED wg, wg-quick, caddy, systemctl, dpkg-query and
apt-get inside a TemporaryDirectory (WG_DIR / CADDY_DIR overrides): no root, no
real WireGuard, firewall, package or Caddy change. Host-only (deploy/ is not in
the Docker test image).
"""
import base64
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[3]
WG = REPO / "deploy" / "wireguard"
CADDY = REPO / "deploy" / "caddy"
SCRIPTS = {
    "create_key": WG / "bbp-create-key.sh",
    "install_conf": WG / "bbp-install-conf.sh",
    "add_peer": WG / "vps-add-peer.sh",
    "remove_peer": WG / "vps-remove-peer.sh",
    "check_peer": WG / "vps-check-peer.sh",
    "caddy_site": CADDY / "vps-caddy-site.sh",
}
HAVE = shutil.which("bash") is not None and all(p.is_file() for p in SCRIPTS.values())

MOCK_WG = r'''#!/usr/bin/env python3
import base64, hashlib, os, sys
st = os.environ["WG_STATE"]
open(os.environ["MOCK_LOG"], "a").write("wg " + " ".join(sys.argv[1:]) + "\n")
def load():
    return [l.rstrip("\n").split("\t") for l in open(st) if l.strip()]
def save(peers):
    open(st, "w").write("".join(f"{k}\t{v}\n" for k, v in peers))
a = sys.argv[1:]
if a == ["genkey"]:
    print(base64.b64encode(os.urandom(32)).decode())
elif a == ["pubkey"]:
    try:
        raw = base64.b64decode(sys.stdin.read().strip(), validate=True)
    except Exception:
        sys.exit(1)
    if len(raw) != 32:
        sys.exit(1)
    print(base64.b64encode(hashlib.sha256(raw).digest()).decode())
elif a[:2] == ["show", "wg0"] and a[2] == "public-key":
    print(os.environ["WG_OWN"])
elif a[:2] == ["show", "wg0"] and a[2] == "peers":
    for k, _ in load(): print(k)
elif a[:2] == ["show", "wg0"] and a[2] == "allowed-ips":
    for k, v in load(): print(f"{k}\t{v or '(none)'}")
elif a[:3] == ["set", "wg0", "peer"] and a[4:] == ["remove"]:
    save([(k, v) for k, v in load() if k != a[3]])
elif a[:3] == ["set", "wg0", "peer"] and a[4] == "allowed-ips":
    if os.environ.get("MOCK_WG_SET_FAIL") == "1":
        sys.exit(1)
    if os.environ.get("MOCK_WG_SET_HUP") == "1":       # SSH disconnect while the peer is applied
        import signal, time
        os.kill(os.getppid(), signal.SIGHUP)
        time.sleep(0.2)
    new = a[5].split(",")
    if os.environ.get("MOCK_WG_SET_EXTRA") == "1":     # new peer gets an unexpected extra prefix
        new = new + ["10.9.9.9/32"]
    peers = []
    for k, v in load():      # real semantics: identical prefixes move to the new peer
        if k != a[3]:
            peers.append((k, " ".join(p for p in v.split() if p not in new)))
    peers.append((a[3], " ".join(new)))
    if os.environ.get("MOCK_WG_SET_CORRUPT") == "1":   # simulate an unexpected side effect
        peers[0] = (peers[0][0], peers[0][1] + " 10.50.0.99/32")
    save(peers)
else:
    sys.exit(2)
'''
MOCK_WG_QUICK = r'''#!/usr/bin/env python3
import os, re, sys
open(os.environ["MOCK_LOG"], "a").write("wg-quick " + " ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if len(a) != 2 or a[0] != "strip" or not re.fullmatch(r"[a-zA-Z0-9_=+.-]{1,15}\.conf", os.path.basename(a[1])):
    sys.exit(2)
deny_all = os.environ.get("MOCK_WG_QUICK_DENY") == "1"
confined = os.environ.get("MOCK_WG_QUICK_CONFINE")          # AppArmor-like: only this dir readable
if deny_all or (confined and not os.path.realpath(a[1]).startswith(os.path.realpath(confined) + os.sep)):
    sys.stderr.write(f"/usr/bin/wg-quick: line 40: {a[1]}: Permission denied\n")
    sys.exit(1)
text = open(a[1]).read()
if "INVALID" in text:
    sys.stderr.write("wg-quick: configuration problem near PrivateKey = " + text.split("PrivateKey = ")[-1][:44] + "\n")
    sys.exit(1)
sys.stdout.write(text)
'''
MOCK_CADDY = r'''#!/usr/bin/env python3
import os, re, sys
open(os.environ["MOCK_LOG"], "a").write("caddy " + " ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:3] != ["validate", "--adapter", "caddyfile"] or a[3] != "--config":
    sys.exit(2)
def expand(path):
    out = []
    for line in open(path):
        m = re.match(r"\s*import\s+(\S+)\s*$", line)
        out += expand(m.group(1)) if m else [line]
    return out
lines = expand(a[4])
sites = [m.group(1) for l in lines if (m := re.match(r"^([A-Za-z0-9.*:-]+) \{$", l.rstrip("\n")))]
if any("BROKEN" in l for l in lines) or len(sites) != len(set(sites)):
    sys.exit(1)
'''
MOCK_LOGGER = '#!/usr/bin/env bash\necho "{name} $*" >> "$MOCK_LOG"\n'
MOCK_DPKG = ('#!/usr/bin/env bash\necho "dpkg-query $*" >> "$MOCK_LOG"\n'
             '[ "${MOCK_WG_INSTALLED:-0}" = 1 ] && printf "install ok installed" || exit 1\n')


def fake_key():
    return base64.b64encode(os.urandom(32)).decode()


@unittest.skipUnless(HAVE, "deploy/ scripts or bash not available (host-only test)")
class _Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        mocks = {"wg": MOCK_WG, "wg-quick": MOCK_WG_QUICK, "caddy": MOCK_CADDY,
                 "dpkg-query": MOCK_DPKG}
        for name in ("systemctl", "apt-get"):
            mocks[name] = MOCK_LOGGER.format(name=name)
        for name, body in mocks.items():
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.wg_dir = self.root / "etc-wireguard"
        self.caddy_dir = self.root / "etc-caddy"
        self.log = self.root / "mock.log"
        self.log.write_text("")
        self.state = self.root / "wg-state"
        self.state.write_text("")
        self.own = fake_key()
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root), "LANG": "C",
            "WG_DIR": str(self.wg_dir), "CADDY_DIR": str(self.caddy_dir),
            "WG_STATE": str(self.state), "WG_OWN": self.own, "MOCK_LOG": str(self.log),
        }

    def run_script(self, name, *args, **env):
        r = subprocess.run(["bash", str(SCRIPTS[name]), *args], cwd=self.root, capture_output=True,
                           text=True, env={**self.env, **env}, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def calls(self, prefix):
        return [l for l in self.log.read_text().splitlines() if l == prefix or l.startswith(prefix + " ")]


class CreateKeyTests(_Sandbox):
    def test_creates_key_pair_without_printing_the_private_key(self):
        rc, out = self.run_script("create_key")
        self.assertEqual(rc, 0, out)
        key = self.wg_dir / "wg-bbp.key"
        priv = key.read_text().strip()
        pub = (self.wg_dir / "wg-bbp.pub").read_text().strip()
        self.assertEqual(stat.S_IMODE(self.wg_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.wg_dir / "wg-bbp.pub").stat().st_mode), 0o644)
        self.assertNotIn(priv, out)
        self.assertIn(f"BBP1 WireGuard PUBLIC key: {pub}", out)
        self.assertEqual(len(self.calls("apt-get")), 1)
        self.assertEqual(sorted(p.name for p in self.wg_dir.iterdir()), ["wg-bbp.key", "wg-bbp.pub"])
        self.assertEqual(self.calls("systemctl"), [])           # nothing started

    def test_rerun_never_overwrites_the_key_and_skips_apt_when_installed(self):
        self.run_script("create_key")
        before = (self.wg_dir / "wg-bbp.key").read_bytes()
        rc, out = self.run_script("create_key", MOCK_WG_INSTALLED="1")
        self.assertEqual(rc, 0, out)
        self.assertIn("EXISTS", out)
        self.assertEqual((self.wg_dir / "wg-bbp.key").read_bytes(), before)
        self.assertEqual(len(self.calls("apt-get")), 1)          # only the first run
        self.assertEqual(len(self.calls("wg genkey")), 1)

    def test_invalid_existing_key_stops_and_is_left_unchanged(self):
        self.wg_dir.mkdir(mode=0o700)
        (self.wg_dir / "wg-bbp.key").write_text("")
        rc, out = self.run_script("create_key", MOCK_WG_INSTALLED="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("STOP", out)
        self.assertEqual((self.wg_dir / "wg-bbp.key").read_text(), "")
        self.assertFalse((self.wg_dir / "wg-bbp.pub").exists())


class InstallConfTests(_Sandbox):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_script("create_key", MOCK_WG_INSTALLED="1")[0], 0)
        self.vps = fake_key()
        self.dest = self.wg_dir / "wg-bbp.conf"

    def leftovers(self):
        return [p.name for p in self.wg_dir.iterdir() if p.name.startswith(".")]

    def test_installs_rendered_config_once(self):
        rc, out = self.run_script("install_conf", self.vps)
        self.assertEqual(rc, 0, out)
        text = self.dest.read_text()
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)
        self.assertIn(f"\nPublicKey = {self.vps}\n", text)
        self.assertNotIn("<VPS_WG0_PUBLIC_KEY>", text)
        self.assertNotRegex(text, r"(?im)^\s*PrivateKey\s*=")
        self.assertEqual(text, (WG / "wg-bbp.conf.template").read_text().replace("<VPS_WG0_PUBLIC_KEY>", self.vps))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.calls("systemctl"), [])

    def test_rerun_identical_is_a_no_op(self):
        self.run_script("install_conf", self.vps)
        inode = self.dest.stat().st_ino
        rc, out = self.run_script("install_conf", self.vps)
        self.assertEqual(rc, 0, out)
        self.assertIn("already installed (identical)", out)
        self.assertEqual(self.dest.stat().st_ino, inode)
        self.assertEqual(self.leftovers(), [])

    def test_different_existing_config_is_never_overwritten(self):
        self.run_script("install_conf", self.vps)
        before = self.dest.read_bytes()
        rc, out = self.run_script("install_conf", fake_key())
        self.assertNotEqual(rc, 0)
        self.assertIn("exists and differs", out)
        self.assertEqual(self.dest.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_guards(self):
        own_pub = (self.wg_dir / "wg-bbp.pub").read_text().strip()
        for label, args in {"malformed": ["not-a-key"], "own key": [own_pub], "no args": [],
                            "two args": [self.vps, self.vps]}.items():
            with self.subTest(label):
                rc, out = self.run_script("install_conf", *args)
                self.assertNotEqual(rc, 0)
                self.assertIn("STOP", out)
                self.assertFalse(self.dest.exists())
                self.assertEqual(self.leftovers(), [])

    def test_destination_is_created_only_by_an_atomic_hard_link(self):
        # `ln` fails if the destination appeared after the existence check (no overwrite race).
        import re
        src = SCRIPTS["install_conf"].read_text()
        writes = [l.strip() for l in src.splitlines() if re.search(r'"\$DEST"', l) and not l.lstrip().startswith("#")]
        self.assertIn('ln "$TMP" "$DEST"', writes)
        self.assertFalse([w for w in writes if re.match(r"(cp|mv|install|cat|tee)\b", w)])
        self.assertNotRegex(src, r'>\s*"\$DEST"')

    def test_requires_the_private_key_with_mode_0600(self):
        (self.wg_dir / "wg-bbp.key").chmod(0o644)
        rc, out = self.run_script("install_conf", self.vps)
        self.assertNotEqual(rc, 0)
        self.assertIn("mode 0600", out)
        (self.wg_dir / "wg-bbp.key").unlink()
        rc, out = self.run_script("install_conf", self.vps)
        self.assertIn("run bbp-create-key.sh first", out)
        self.assertFalse(self.dest.exists())


class VpsPeerTests(_Sandbox):
    def setUp(self):
        super().setUp()
        self.wg_dir.mkdir(mode=0o700)
        self.ga, self.gb, self.new = fake_key(), fake_key(), fake_key()
        self.vps_priv = fake_key()
        self.conf = self.wg_dir / "wg0.conf"
        self.original = textwrap.dedent(f"""\
            [Interface]
            Address = 10.50.0.1/24
            ListenPort = 51820
            PrivateKey = {self.vps_priv}

            [Peer]
            # gridscan-1
            PublicKey = {self.ga}
            AllowedIPs = 10.50.0.11/32

            [Peer]
            PublicKey = {self.gb}
            AllowedIPs = 10.50.0.12/32
            PersistentKeepalive = 25
            """)
        self.conf.write_text(self.original)
        self.conf.chmod(0o600)                      # as on the VPS; the script preserves the mode
        self.live = f"{self.ga}\t10.50.0.11/32\n{self.gb}\t10.50.0.12/32\n"
        self.state.write_text(self.live)

    def gridscan_live(self):
        return [l for l in self.state.read_text().splitlines() if not l.startswith(self.new)]

    def assert_untouched(self, out):
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.state.read_text(), self.live)
        self.assertEqual(self.calls("wg set"), [])
        self.assertEqual([p.name for p in self.wg_dir.iterdir() if p.name != "wg0.conf"], [])
        self.assertNotIn(self.vps_priv, out)

    def test_add_appends_one_block_and_leaves_gridscan_unchanged(self):
        rc, out = self.run_script("add_peer", self.new)
        self.assertEqual(rc, 0, out)
        text = self.conf.read_text()
        self.assertTrue(text.startswith(self.original))
        self.assertEqual(text[len(self.original):],
                         f"\n[Peer]\n# BitBotPro Backend 1 (PVE1 VM BBP1)\nPublicKey = {self.new}\nAllowedIPs = 10.50.0.20/32\n")
        self.assertEqual(self.gridscan_live(), self.live.splitlines())
        self.assertIn(f"{self.new}\t10.50.0.20/32", self.state.read_text())
        self.assertEqual(len(list(self.wg_dir.glob("wg0.conf.bak-*"))), 1)
        self.assertEqual([p.name for p in self.wg_dir.iterdir() if p.name.startswith(".")], [])
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)
        self.assertNotIn(self.vps_priv, out)
        self.assertIn("Existing peers unchanged", out)

    def _refusal_cases(self):
        """Built from the CURRENT sandbox's keys (each case gets a fresh sandbox)."""
        o = self.original
        return {
            "gridscan key A": (self.ga, None, None),
            "gridscan key B": (self.gb, None, None),
            "wg0's own key": (self.own, None, None),
            "malformed key": ("abc", None, None),
            "10.50.0.20 already live on a gridscan peer": (
                self.new, None, f"{self.ga}\t10.50.0.11/32 10.50.0.20/32\n{self.gb}\t10.50.0.12/32\n"),
            "10.50.0.20 inside a persisted /28": (
                self.new, o.replace("AllowedIPs = 10.50.0.12/32", "AllowedIPs = 10.50.0.12/32, 10.50.0.16/28"), None),
            "10.50.0.20 inside a live /24": (
                self.new, None, f"{self.ga}\t10.50.0.0/24\n{self.gb}\t10.50.0.12/32\n"),
            "SaveConfig = true": (self.new, o.replace("ListenPort", "SaveConfig = true\nListenPort"), None),
            "key already in the file": (self.new, o + f"\n[Peer]\nPublicKey = {self.new}\nAllowedIPs = 10.50.0.30/32\n", None),
            "new file would not parse": (self.new, o.replace("# gridscan-1", "# INVALID"), None),
            # only the live-peer guard can catch this: key live but not persisted
            "key is a live-only peer": (self.new, None, self.live + f"{self.new}\t10.50.0.30/32\n"),
        }

    def test_add_refuses_anything_that_could_touch_an_existing_peer(self):
        for label in list(self._refusal_cases()):
            with self.subTest(label):
                self.setUp()                                   # fresh sandbox and keys per case
                key, conf, live = self._refusal_cases()[label]
                if conf is not None:
                    self.original = conf
                    self.conf.write_text(conf)
                if live is not None:
                    self.live = live
                    self.state.write_text(live)
                rc, out = self.run_script("add_peer", key)
                self.assertNotEqual(rc, 0, out)
                self.assertIn("STOP", out)
                self.assert_untouched(out)

    def test_failed_live_set_restores_the_file(self):
        rc, out = self.run_script("add_peer", self.new, MOCK_WG_SET_FAIL="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("restored from", out)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)
        self.assertEqual(self.state.read_text(), self.live)
        self.assertEqual([p.name for p in self.wg_dir.iterdir() if p.name.startswith(".")], [])
        self.assertNotIn(self.vps_priv, out)

    def test_failed_post_check_removes_the_peer_and_restores_the_file(self):
        rc, out = self.run_script("add_peer", self.new, MOCK_WG_SET_CORRUPT="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("BBP1 peer removed again", out)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertNotIn(self.new, self.state.read_text())              # live peer removed again
        self.assertIn(f"{self.ga}\t10.50.0.11/32", out)                # BEFORE snapshot shown for manual restore
        self.assertIn("wg set wg0 peer <public key> allowed-ips", out)
        self.assertNotIn(self.vps_priv, out)
        self.assertEqual([p.name for p in self.wg_dir.iterdir() if p.name.startswith(".")], [])

    def test_post_check_rejects_extra_allowed_ips_on_the_new_peer(self):
        rc, out = self.run_script("add_peer", self.new, MOCK_WG_SET_EXTRA="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("BBP1 peer removed again", out)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.state.read_text(), self.live)

    def test_ssh_hangup_during_live_apply_does_not_interrupt_the_run(self):
        rc, out = self.run_script("add_peer", self.new, MOCK_WG_SET_HUP="1")
        self.assertEqual(rc, 0, out)                                   # HUP ignored in the critical section
        self.assertIn("Existing peers unchanged", out)
        self.assertIn(f"{self.new}\t10.50.0.20/32", self.state.read_text())
        self.assertIn(f"PublicKey = {self.new}", self.conf.read_text())

    # ---- vps-check-peer.sh (read-only verification) ----
    def snapshot(self):
        rc, out = self.run_script("check_peer", "snapshot")
        self.assertEqual(rc, 0, out)
        snap = self.root / "before-wg0.txt"
        snap.write_text(out)
        return str(snap)

    def check(self, mode, snap):
        conf, state = self.conf.read_bytes(), self.state.read_bytes()
        rc, out = self.run_script("check_peer", mode, self.new, snap)
        self.assertEqual((self.conf.read_bytes(), self.state.read_bytes()), (conf, state))   # read-only
        self.assertNotIn(self.vps_priv, out)
        return rc, out

    def test_check_added_passes_only_for_a_complete_strict_addition(self):
        snap = self.snapshot()
        self.assertTrue(pathlib.Path(snap).read_text().startswith("peers-in-file\t2\n"))
        self.assertNotIn(self.vps_priv, pathlib.Path(snap).read_text())
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        rc, out = self.check("added", snap)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        self.assertIn("COMPLETE (live and persisted)", out)
        good = self.state.read_text()
        tampered = {
            "gridscan allowed-ips changed": good.replace("10.50.0.12/32", "10.50.0.12/32 10.50.0.99/32"),
            "gridscan peer removed": "".join(l + "\n" for l in good.splitlines() if not l.startswith(self.gb)),
            "a second new peer": good + f"{fake_key()}\t10.50.0.21/32\n",
            "extra allowed-ip on BBP1": good.replace("10.50.0.20/32", "10.50.0.20/32 10.9.9.9/32"),
        }
        for label, content in tampered.items():
            with self.subTest(label):
                self.state.write_text(content)
                rc, out = self.check("added", snap)
                self.assertEqual(rc, 1, out)
                self.assertIn("FAIL", out)
                self.assertNotIn("PASS", out)
        self.state.write_text(good)

    def test_check_reports_incomplete_states(self):
        snap = self.snapshot()
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        # persisted but not live (run killed between file replacement and wg set)
        self.state.write_text(self.live)
        rc, out = self.check("added", snap)
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE — persisted", out)
        # documented recovery: remove, then add again
        self.assertEqual(self.run_script("remove_peer", self.new)[0], 0)
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        self.assertEqual(self.check("added", snap)[0], 0)
        # live but not persisted
        self.conf.write_text(self.original)
        rc, out = self.check("added", snap)
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE — live but NOT persisted", out)
        self.assertEqual(self.run_script("remove_peer", self.new)[0], 0)
        self.assertEqual(self.state.read_text(), self.live)
        self.assertEqual(self.check("removed", snap)[0], 0)

    def test_check_removed_after_rollback(self):
        snap = self.snapshot()
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        rc, out = self.check("removed", snap)
        self.assertEqual(rc, 1)                                        # still present
        self.assertEqual(self.run_script("remove_peer", self.new)[0], 0)
        rc, out = self.check("removed", snap)
        self.assertEqual(rc, 0, out)
        self.assertIn("ABSENT", out)
        self.assertIn("PASS: wg0 is exactly as in the snapshot", out)

    def test_check_added_fails_when_only_the_persisted_block_is_wrong(self):
        snap = self.snapshot()
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        good = self.conf.read_text()
        for label, content in {
            "extra line in the BBP1 block": good.replace("AllowedIPs = 10.50.0.20/32", "AllowedIPs = 10.50.0.20/32\nEndpoint = 1.2.3.4:51820"),
            "different allowed-ips persisted": good.replace("AllowedIPs = 10.50.0.20/32", "AllowedIPs = 10.50.0.21/32"),
        }.items():
            with self.subTest(label):
                self.conf.write_text(content)
                rc, out = self.check("added", snap)                   # live state is still correct
                self.assertEqual(rc, 1, out)
                self.assertIn("expected [Peer] block", out)
        self.conf.write_text(good)

    def test_check_removed_fails_if_another_peer_appeared(self):
        snap = self.snapshot()
        self.state.write_text(self.live + f"{fake_key()}\t10.50.0.21/32\n")
        rc, out = self.check("removed", snap)
        self.assertEqual(rc, 1, out)
        self.assertIn("not identical to the snapshot", out)

    def test_check_refuses_a_snapshot_taken_after_adding(self):
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        snap = self.snapshot()
        rc, out = self.check("added", snap)
        self.assertEqual(rc, 2)
        self.assertIn("take it BEFORE adding", out)

    # ---- wg-quick read denial (the Approval 2 failure) ----
    def wg_quick_paths(self):
        return [l.split()[-1] for l in self.calls("wg-quick")]

    def test_wg_quick_only_reads_files_inside_the_wireguard_dir(self):
        confine = {"MOCK_WG_QUICK_CONFINE": str(self.wg_dir)}
        snap = self.snapshot()
        rc, out = self.run_script("add_peer", self.new, **confine)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_script("check_peer", "added", self.new, snap, **confine)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_script("remove_peer", self.new, **confine)
        self.assertEqual(rc, 0, out)
        paths = self.wg_quick_paths()
        self.assertGreaterEqual(len(paths), 5)
        for path in paths:
            self.assertEqual(pathlib.Path(path).parent, self.wg_dir, path)
        self.assertEqual([p.name for p in self.wg_dir.iterdir() if p.name.startswith(("wgchk.", "."))], [])

    def test_read_denial_is_reported_as_permission_not_configuration(self):
        rc, out = self.run_script("add_peer", self.new, MOCK_WG_QUICK_DENY="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("DENIED READ access", out)
        self.assertIn("NOT a WireGuard configuration error", out)
        self.assertNotIn("configuration problem, see message", out)
        self.assert_untouched(out)
        rc, out = self.run_script("check_peer", "status", self.new)
        snap = self.snapshot()
        rc, out = self.run_script("check_peer", "removed", self.new, snap, MOCK_WG_QUICK_DENY="1")
        self.assertEqual(rc, 1)
        self.assertIn("DENIED READ access", out)
        self.assertIn("not a configuration error", out)

    def test_configuration_failure_is_reported_as_configuration_and_keys_redacted(self):
        self.original = self.original.replace("# gridscan-1", "# INVALID")
        self.conf.write_text(self.original)
        rc, out = self.run_script("add_peer", self.new)
        self.assertNotEqual(rc, 0)
        self.assertIn("configuration problem", out)
        self.assertNotIn("DENIED", out)
        self.assertIn("<key redacted>", out)
        self.assertNotIn(self.vps_priv, out)
        self.assert_untouched(out)

    def test_add_then_remove_restores_file_and_peers_exactly(self):
        for label, original in {"trailing newline": self.original,
                                "no trailing newline": self.original.rstrip("\n")}.items():
            with self.subTest(label):
                self.conf.write_text(original)
                self.state.write_text(self.live)
                self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
                rc, out = self.run_script("remove_peer", self.new)
                self.assertEqual(rc, 0, out)
                expected = original if original.endswith("\n") else original + "\n"
                self.assertEqual(self.conf.read_text(), expected)
                self.assertEqual(self.state.read_text(), self.live)
                self.assertIn("Remaining peers unchanged", out)

    def test_remove_refuses_gridscan_keys_and_modified_blocks(self):
        self.assertEqual(self.run_script("add_peer", self.new)[0], 0)
        added_conf, added_state = self.conf.read_text(), self.state.read_text()
        for key in (self.ga, self.gb):
            with self.subTest(key="gridscan"):
                rc, out = self.run_script("remove_peer", key)
                self.assertNotEqual(rc, 0)
                self.assertIn("STOP", out)
                self.assertEqual(self.conf.read_text(), added_conf)
                self.assertEqual(self.state.read_text(), added_state)
        edited = added_conf.replace("AllowedIPs = 10.50.0.20/32", "AllowedIPs = 10.50.0.20/32\nPersistentKeepalive = 25")
        self.conf.write_text(edited)
        rc, out = self.run_script("remove_peer", self.new)
        self.assertNotEqual(rc, 0)
        self.assertIn("not exactly as vps-add-peer.sh wrote it", out)
        self.assertEqual(self.conf.read_text(), edited)
        self.assertEqual(self.state.read_text(), added_state)       # live peer kept too

    def test_remove_refuses_a_live_only_gridscan_peer(self):
        # Key not in the file: only the AllowedIPs identity guard protects the peer.
        other = fake_key()
        self.live += f"{other}\t10.50.0.13/32\n"
        self.state.write_text(self.live)
        rc, out = self.run_script("remove_peer", other)
        self.assertNotEqual(rc, 0)
        self.assertIn("not the BBP1 peer", out)
        self.assert_untouched(out)

    def test_remove_when_absent_is_a_no_op(self):
        rc, out = self.run_script("remove_peer", self.new)
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to do", out)
        self.assert_untouched(out)


class CaddySiteTests(_Sandbox):
    def setUp(self):
        super().setUp()
        self.caddy_dir.mkdir()
        self.caddyfile = self.caddy_dir / "Caddyfile"
        self.original = textwrap.dedent("""\
            {
            \temail ops@example.invalid
            }

            ada.api.gridscan.io {
            \treverse_proxy 10.50.0.12:8000 10.50.0.11:8000 {
            \t\tlb_policy first
            \t}
            }
            """)
        self.caddyfile.write_text(self.original)
        self.site_src = self.root / "api.bitbotpro.io.caddy"
        shutil.copy(CADDY / "api.bitbotpro.io.caddy", self.site_src)
        self.site = self.caddy_dir / "sites" / "api.bitbotpro.io.caddy"
        self.import_line = f"import {self.site}\n"

    def assert_untouched(self):
        self.assertEqual(self.caddyfile.read_text(), self.original)
        self.assertEqual(self.calls("systemctl"), [])
        self.assertEqual([p.name for p in self.caddy_dir.iterdir() if p.name.startswith(".")], [])

    def test_install_adds_one_import_line_and_reloads_gracefully(self):
        rc, out = self.run_script("caddy_site", "install", str(self.site_src))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.caddyfile.read_text(), self.original + self.import_line)
        self.assertEqual(self.site.read_bytes(), self.site_src.read_bytes())
        self.assertEqual(self.calls("systemctl"), ["systemctl reload caddy"])
        self.assertEqual(len(list(self.caddy_dir.glob("Caddyfile.bak-*"))), 1)

    def test_install_then_remove_restores_the_caddyfile_exactly(self):
        self.run_script("caddy_site", "install", str(self.site_src))
        rc, out = self.run_script("caddy_site", "remove")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.caddyfile.read_text(), self.original)
        self.assertEqual(self.calls("systemctl"), ["systemctl reload caddy"] * 2)
        rc, out = self.run_script("caddy_site", "install", str(self.site_src))   # reinstall after rollback
        self.assertEqual(rc, 0, out)

    def test_invalid_site_changes_nothing_live(self):
        self.site_src.write_text(self.site_src.read_text() + "\n# BROKEN\n")
        rc, out = self.run_script("caddy_site", "install", str(self.site_src))
        self.assertNotEqual(rc, 0)
        self.assertIn("does not validate", out)
        self.assert_untouched()
        self.assertFalse(self.site.exists())                       # created file removed again

    def test_install_guards(self):
        cases = {
            "invalid current config": self.original + "BROKEN\n",
            "sites glob already imported": self.original + "import sites/*\n",
            "bitbotpro already configured": self.original + "api.bitbotpro.io {\n}\n",
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.original = content
                self.caddyfile.write_text(content)
                rc, out = self.run_script("caddy_site", "install", str(self.site_src))
                self.assertNotEqual(rc, 0)
                self.assertIn("STOP", out)
                self.assert_untouched()
                self.assertFalse(self.site.exists())

    def test_second_install_and_remove_when_absent(self):
        self.run_script("caddy_site", "install", str(self.site_src))
        installed = self.caddyfile.read_text()
        rc, out = self.run_script("caddy_site", "install", str(self.site_src))
        self.assertNotEqual(rc, 0)
        self.assertIn("already imported", out)
        self.assertEqual(self.caddyfile.read_text(), installed)
        self.run_script("caddy_site", "remove")
        rc, out = self.run_script("caddy_site", "remove")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.caddyfile.read_text(), self.original)


LANDLOCK_SHIM = r"""import ctypes, os, struct, sys
libc = ctypes.CDLL(None, use_errno=True)
H = 1 | 4 | 8                      # LANDLOCK_ACCESS_FS_EXECUTE | READ_FILE | READ_DIR
attr = ctypes.create_string_buffer(struct.pack("Q", H), 8)
rfd = libc.syscall(444, attr, ctypes.c_size_t(8), ctypes.c_uint32(0))      # landlock_create_ruleset
if rfd < 0: sys.exit(97)
for p in os.environ["LL_ALLOW"].split(":"):
    if not os.path.exists(p): continue
    fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
    rule = ctypes.create_string_buffer(struct.pack("=Qi", H, fd), 12)        # landlock_path_beneath_attr
    if libc.syscall(445, rfd, 1, rule, 0) != 0: sys.exit(98)                 # landlock_add_rule
    os.close(fd)
if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, rfd, 0) != 0: sys.exit(99)   # no_new_privs, restrict_self
os.execv("/bin/bash", ["bash", os.environ["WGQ_COPY"], *sys.argv[1:]])
"""
REAL_WG_QUICK = pathlib.Path("/usr/bin/wg-quick")


@unittest.skipUnless(HAVE and REAL_WG_QUICK.is_file() and os.uname().machine == "x86_64",
                     "needs the real /usr/bin/wg-quick (wireguard-tools) on x86_64 — host-only")
class RealWgQuickConfinementTests(VpsPeerTests):
    """Reproduces the Approval 2 failure with the REAL wg-quick parser under a REAL kernel
    path restriction (Landlock, unprivileged): wg-quick may read only the WireGuard dir,
    as an AppArmor profile confining wg-quick to /etc/wireguard would. No root, no sudo:
    the test copy of wg-quick has only its root re-exec line (auto_su) disabled, which
    `strip` does not need; a failing `sudo` stub guards against any sudo call."""

    def setUp(self):
        super().setUp()
        import re
        src = REAL_WG_QUICK.read_text()
        patched, n = re.subn(r"(?m)^\t\[\[ \$UID == 0 \]\] \|\| exec sudo .*$", "\t:  # test copy: root re-exec disabled", src)
        if n != 1 or "exec sudo" in patched:
            self.skipTest("unexpected wg-quick version (auto_su line not found)")
        (self.bin / "wg-quick.real").write_text(patched)
        (self.bin / "landlock_shim.py").write_text(LANDLOCK_SHIM)
        (self.bin / "sudo").write_text("#!/bin/sh\necho 'sudo must not be called' >&2\nexit 99\n")
        (self.bin / "sudo").chmod(0o755)
        self.allow = ["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc", "/dev", str(self.bin)]
        self.env["WGQ_COPY"] = str(self.bin / "wg-quick.real")
        self.env["LL_ALLOW"] = ":".join(self.allow + [str(self.wg_dir)])
        # replace the mock with the real parser behind the Landlock shim
        (self.bin / "wg-quick").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{self.bin}/landlock_shim.py" "$@"\n')
        probe = subprocess.run([str(self.bin / "wg-quick"), "strip", str(self.conf)], env=self.env,
                               capture_output=True, text=True)
        if probe.returncode in (97, 98, 99):
            self.skipTest("Landlock not available")
        self.assertEqual(probe.returncode, 0, probe.stderr)

    # Mock-only behaviours (the real parser has no INVALID marker, no deny switches and no call log):
    def test_add_refuses_anything_that_could_touch_an_existing_peer(self):
        cases = self._refusal_cases()
        cases.pop("new file would not parse")
        for label in cases:
            with self.subTest(label):
                self.setUp()
                key, conf, live = self._refusal_cases()[label]
                if conf is not None:
                    self.original = conf
                    self.conf.write_text(conf)
                if live is not None:
                    self.live = live
                    self.state.write_text(live)
                rc, out = self.run_script("add_peer", key)
                self.assertNotEqual(rc, 0, out)
                self.assertIn("STOP", out)
                self.assert_untouched(out)

    def test_wg_quick_only_reads_files_inside_the_wireguard_dir(self):
        self.skipTest("covered for real by the Landlock confinement in this class")

    def test_read_denial_is_reported_as_permission_not_configuration(self):
        self.skipTest("mock switch; see test_real_denial_is_classified_as_permission_and_changes_nothing")

    def test_configuration_failure_is_reported_as_configuration_and_keys_redacted(self):
        self.skipTest("mock-only INVALID marker")

    def test_reproduces_the_vps_error_for_a_copy_outside_the_wireguard_dir(self):
        outside = self.root / "tmp-copy"
        outside.mkdir(mode=0o700)
        shutil.copy(self.conf, outside / "wg0.conf")
        r = subprocess.run([str(self.bin / "wg-quick"), "strip", str(outside / "wg0.conf")], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stderr, r"wg-quick\.real: line \d+: .*/tmp-copy/wg0\.conf: Permission denied")
        self.assertNotIn(self.vps_priv, r.stdout + r.stderr)

    def test_add_check_remove_succeed_with_the_real_confined_parser(self):
        snap = self.snapshot()
        rc, out = self.run_script("add_peer", self.new, TMPDIR=str(self.root))
        self.assertEqual(rc, 0, out)
        rc, out = self.run_script("check_peer", "added", self.new, snap)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        rc, out = self.run_script("remove_peer", self.new, TMPDIR=str(self.root))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.conf.read_text(), self.original)
        self.assertEqual(self.state.read_text(), self.live)
        self.assertNotIn("sudo must not be called", out)

    def test_real_denial_is_classified_as_permission_and_changes_nothing(self):
        self.env["LL_ALLOW"] = ":".join(self.allow)            # WireGuard dir NOT readable for wg-quick
        rc, out = self.run_script("add_peer", self.new)
        self.assertNotEqual(rc, 0)
        self.assertIn("DENIED READ access", out)
        self.assertIn("Permission denied", out)                 # wg-quick's own message, shown
        self.assert_untouched(out)


@unittest.skipUnless(HAVE, "deploy/ scripts or bash not available (host-only test)")
class ScriptSyntaxTests(unittest.TestCase):
    def test_bash_syntax_and_strict_mode(self):
        for name, path in SCRIPTS.items():
            with self.subTest(script=name):
                r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("set -euo pipefail", path.read_text())

    def test_readme_shell_blocks_parse(self):
        import re
        text = (WG / "README.md").read_text()
        blocks = re.findall(r"```(?:bash)?\n(.*?)```", text, re.S)
        self.assertGreater(len(blocks), 5)
        for i, block in enumerate(blocks):
            if block.lstrip().startswith(("Client ", "[")):
                continue                                        # diagram / config excerpt
            with self.subTest(block=i):
                r = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr + block)


if __name__ == "__main__":
    unittest.main()
