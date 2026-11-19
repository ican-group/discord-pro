"""Phase 6B: versioned encryption keys and crash-safe key rotation (test-only keys)."""
import asyncio
import copy
import json
import re
import unittest
from pathlib import Path

from tests.unit._support import BACKEND_DIR

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:          # pragma: no cover - host without the dependency
    HAVE_CRYPTO = False
try:
    from tests.unit._api_fixtures import HAVE_API
except Exception:            # pragma: no cover
    HAVE_API = False


def keys(n):
    from cryptography.fernet import Fernet
    return [Fernet.generate_key().decode() for _ in range(n)]


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed (runs in the Docker test target)")
class KeyRingTests(unittest.TestCase):
    def test_versioned_ciphertexts_name_their_key(self):
        from core.credentials import KeyRing, SecretBox
        k1, k2 = keys(2)
        box = SecretBox(KeyRing({"k1": k1, "k2": k2}, "k2"))
        c = box.encrypt("s3cret-value")
        self.assertTrue(c.startswith("enc1:k2:"))
        self.assertEqual(box.key_id_of(c), "k2")
        self.assertEqual(box.decrypt(c), "s3cret-value")
        self.assertFalse(box.needs_rotation(c))

    def test_old_keys_still_decrypt_and_unknown_keys_fail_closed(self):
        from core.credentials import CredentialError, KeyRing, SecretBox
        k1, k2, k3 = keys(3)
        old = SecretBox(KeyRing({"k1": k1}, "k1")).encrypt("value-one")
        both = SecretBox(KeyRing({"k1": k1, "k2": k2}, "k2"))
        self.assertEqual(both.decrypt(old), "value-one")               # backward compatible
        self.assertTrue(both.needs_rotation(old))
        only_new = SecretBox(KeyRing({"k2": k2}, "k2"))
        with self.assertRaises(CredentialError):
            only_new.decrypt(old)                                      # retired key: refused, never garbage
        forged = "enc1:k2:" + old.split(":", 2)[2]                     # relabelled to another key
        with self.assertRaises(CredentialError):
            both.decrypt(forged)
        for bad in ("", "enc1:", "enc1:k9:abc", "enc1:k1", "not-a-token", None):
            with self.assertRaises(CredentialError):
                SecretBox(KeyRing({"k1": k1, "k3": k3}, "k1")).decrypt(bad)

    def test_pre_6b_bare_fernet_tokens_are_readable_and_rotated(self):
        from cryptography.fernet import Fernet
        from core.credentials import KeyRing, SecretBox
        k1, k2 = keys(2)
        bare = Fernet(k1.encode()).encrypt(b"legacy").decode()
        box = SecretBox(KeyRing({"k1": k1, "k2": k2}, "k2"))
        self.assertIsNone(box.key_id_of(bare))
        self.assertEqual(box.decrypt(bare), "legacy")
        self.assertTrue(box.needs_rotation(bare))
        self.assertTrue(box.reencrypt(bare).startswith("enc1:k2:"))

    def test_single_key_box_is_key_id_k1(self):
        from core.credentials import SecretBox
        (k1,) = keys(1)
        self.assertTrue(SecretBox(k1).encrypt("x").startswith("enc1:k1:"))

    def test_key_loading_from_the_environment(self):
        from core.credentials import KeyRing, KeyRingError
        k1, k2 = keys(2)
        ring = KeyRing.from_environment({"ENCRYPTION_KEYS": f"# comment\nk1={k1}\n\nk2 = {k2}\n",
                                         "ENCRYPTION_ACTIVE_KEY_ID": "k2"})
        self.assertEqual((ring.active, ring.key_ids), ("k2", ("k1", "k2")))
        self.assertEqual(KeyRing.from_environment({"ENCRYPTION_KEY": k1}).key_ids, ("k1",))
        self.assertEqual(KeyRing.from_environment({"ENCRYPTION_KEY": k1, "ENCRYPTION_KEY_ID": "2026-11"}).active,
                         "2026-11")
        bad_envs = [
            {},
            {"ENCRYPTION_KEYS": f"k1={k1}", "ENCRYPTION_KEY": k1, "ENCRYPTION_ACTIVE_KEY_ID": "k1"},
            {"ENCRYPTION_KEYS": f"k1={k1}"},                                         # no active id
            {"ENCRYPTION_KEYS": f"k1={k1}", "ENCRYPTION_ACTIVE_KEY_ID": "k2"},       # active not loaded
            {"ENCRYPTION_KEYS": f"k1={k1}\nk1={k2}", "ENCRYPTION_ACTIVE_KEY_ID": "k1"},
            {"ENCRYPTION_KEYS": f"k1={k1}\nk2={k1}", "ENCRYPTION_ACTIVE_KEY_ID": "k1"},  # same material twice
            {"ENCRYPTION_KEYS": f"K1={k1}", "ENCRYPTION_ACTIVE_KEY_ID": "K1"},       # invalid id
            {"ENCRYPTION_KEYS": "k1=not-a-fernet-key", "ENCRYPTION_ACTIVE_KEY_ID": "k1"},
            {"ENCRYPTION_KEYS": f"k1 {k1}", "ENCRYPTION_ACTIVE_KEY_ID": "k1"},
            {"ENCRYPTION_KEYS": "\n".join(f"k{i}={k}" for i, k in enumerate(keys(9))), "ENCRYPTION_ACTIVE_KEY_ID": "k0"},
        ]
        for env in bad_envs:
            with self.assertRaises(KeyRingError) as cm:
                KeyRing.from_environment(env)
            for k in (k1, k2):
                self.assertNotIn(k, str(cm.exception))                 # errors never contain key material

    def test_repr_never_contains_key_material(self):
        from core.credentials import KeyRing, SecretBox
        k1, k2 = keys(2)
        box = SecretBox(KeyRing({"k1": k1, "k2": k2}, "k1"))
        for text in (repr(box), repr(box.ring), str(box.ring)):
            self.assertNotIn(k1, text)
            self.assertNotIn(k2, text)


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed (runs in the Docker test target)")
class StorePrimitiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_field_if_is_a_field_level_compare_and_set_on_enc_paths_only(self):
        from core.hierarchy import HierarchyError, InMemoryStore
        s = InMemoryStore()
        await s.insert("x", {"id": "a", "version": 3, "hook": {"path_enc": "old"}, "secret_enc": "s"})
        self.assertTrue(await s.set_field_if("x", "a", "hook.path_enc", "old", "new"))
        self.assertFalse(await s.set_field_if("x", "a", "hook.path_enc", "old", "newer"))   # lost race
        self.assertFalse(await s.set_field_if("x", "missing", "secret_enc", "s", "t"))
        doc = await s.get("x", "a")
        self.assertEqual((doc["hook"]["path_enc"], doc["version"]), ("new", 3))           # version unchanged
        for path in ("version", "id", "$set", "secret", "hook.$where_enc", "a.b.c.d.e_enc", "Secret_enc", ""):
            with self.assertRaises(HierarchyError):
                await s.set_field_if("x", "a", path, "s", "t")

    async def test_scan_is_ordered_and_bounded(self):
        from core.hierarchy import HierarchyError, InMemoryStore
        s = InMemoryStore()
        for i in (3, 1, 2):
            await s.insert("x", {"id": f"id{i}"})
        self.assertEqual([d["id"] for d in await s.scan("x", limit=2)], ["id1", "id2"])
        self.assertEqual([d["id"] for d in await s.scan("x", after="id2")], ["id3"])
        for bad in ({"limit": 0}, {"limit": 501}, {"after": 5}):
            with self.assertRaises(HierarchyError):
                await s.scan("x", **bad)


SOURCE_DIRS = ("api", "runtime", "core")
AUDIT = "audit_events"


def source_enc_fields():
    pattern = re.compile(r"""["']([a-z][a-z0-9_]*_enc)["']|\b([a-z][a-z0-9_]*_enc)=""")
    names = set()
    for d in SOURCE_DIRS:
        for f in (BACKEND_DIR / d).glob("*.py"):
            names |= {a or b for a, b in pattern.findall(f.read_text())}
    return names


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class RotationTests(unittest.IsolatedAsyncioTestCase):
    """A World with every kind of stored secret, then rotated k1 → k2 (and back)."""

    async def asyncSetUp(self):
        from core.credentials import KeyRing, SecretBox
        from tests.unit._api_fixtures import World, ZD_API_TOKEN, ZD_CLIENT_SECRET, FERNET_KEY
        self.k1 = FERNET_KEY
        (self.k2,) = keys(1)
        self.w = await World().build()
        self.w.S.box = SecretBox(KeyRing({"k1": self.k1}, "k1"))
        self.plaintexts = {ZD_API_TOKEN, ZD_CLIENT_SECRET}
        call = self.w.call
        r = await call("PUT", "/platform/credentials/zendesk_oauth_client", "root",
                       {"secret": ZD_CLIENT_SECRET, "metadata": {"client_id": "bitbotpro_global"}})
        assert r.status_code == 200, r.text
        r = await call("POST", f"/bots/{self.w.bA.id}/integrations", "admin",
                       {"type": "zendesk", "name": "Support", "settings": {}})
        assert r.status_code == 201, r.text
        self.Z = r.json()["id"]
        r = await call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                       {"provider": "zendesk_api_token", "name": "Zendesk", "secret": ZD_API_TOKEN,
                        "metadata": {"subdomain": "acme", "email": "agent@acme.example"}})
        assert r.status_code == 201, r.text
        r = await call("PUT", f"/integrations/{self.Z}/zendesk/signing-secret", "admin",
                       {"secret": "manual-signing-secret-0123456789"})
        assert r.status_code == 200, r.text
        r = await call("POST", f"/integrations/{self.Z}/zendesk/oauth/start", "admin", {"subdomain": "acme"})
        assert r.status_code == 200, r.text
        r = await call("POST", f"/instances/{self.w.iA_tg.id}/channel/connect", "admin")
        assert r.status_code == 200, r.text
        # values written by World.build() before the k1 ring was installed use the same key (FERNET_KEY):
        # they are pre-6B bare tokens here, which is exactly the upgrade case
        self.original = await self.decrypted_by_location(SecretBox(KeyRing({"k1": self.k1}, "k1")))

    async def decrypted_by_location(self, box):
        from core.key_rotation import ENCRYPTED_COLLECTIONS, encrypted_fields
        out = {}
        for coll in ENCRYPTED_COLLECTIONS:
            for doc in await self.w.store.scan(coll, limit=500):
                for path, value in encrypted_fields(doc):
                    if isinstance(value, str) and value:
                        out[(coll, doc["id"], path)] = box.decrypt(value)
        return out

    def ring(self, active, *ids):
        from core.credentials import KeyRing, SecretBox
        material = {"k1": self.k1, "k2": self.k2}
        return SecretBox(KeyRing({i: material[i] for i in ids}, active))

    def now(self):
        return self.w.S.clock()

    async def test_every_encrypted_field_is_covered(self):
        """Every collection holding a *_enc field is rotated, and every *_enc field
        name used by the source was produced by this World (nothing unexercised)."""
        from core.key_rotation import ENCRYPTED_COLLECTIONS, encrypted_fields
        found_colls, found_leaves = set(), set()
        for coll, docs in self.w.store.collections.items():
            for doc in docs.values():
                for path, value in encrypted_fields(doc):
                    found_colls.add(coll)
                    found_leaves.add(path.rsplit(".", 1)[-1])
        self.assertLessEqual(found_colls, set(ENCRYPTED_COLLECTIONS))
        self.assertEqual(found_colls, set(ENCRYPTED_COLLECTIONS))
        self.assertLessEqual(source_enc_fields(), found_leaves | {"secret_enc"})
        self.assertEqual(len(self.original), sum(1 for _ in self.original))
        self.assertGreaterEqual(len(self.original), 10)

    async def test_rotation_reencrypts_everything_and_the_api_keeps_working(self):
        from core import key_rotation as kr
        box = self.ring("k2", "k1", "k2")
        self.w.S.box = box
        before = await kr.verify(self.w.store, box)
        self.assertFalse(before["ok"])
        self.assertEqual(before["undecryptable"], [])
        report = await kr.rotate(self.w.store, box, now=self.now)
        self.assertEqual(sum(report["rotated"].values()), len(self.original))
        after = await kr.verify(self.w.store, box)
        self.assertTrue(after["ok"], after)
        self.assertEqual(await self.decrypted_by_location(box), self.original)   # same secret, same place
        # the old key can now be retired: a ring with k2 only reads everything
        only_k2 = self.ring("k2", "k2")
        self.assertEqual(await self.decrypted_by_location(only_k2), self.original)
        self.w.S.box = only_k2
        r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertNotEqual(r.status_code, 500)
        r = await self.w.call("POST", f"/instances/{self.w.iA_tg.id}/channel/test", "admin")
        self.assertEqual(r.status_code, 200, r.text)
        # re-running is a no-op
        again = await kr.rotate(self.w.store, only_k2, now=self.now)
        self.assertEqual(again["rotated"], {})

    async def test_new_values_use_the_active_key_while_old_ones_stay_readable(self):
        self.w.S.box = self.ring("k2", "k1", "k2")
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                              {"provider": "anthropic", "name": "AI 2", "secret": "sk-ant-test-" + "z" * 30})
        self.assertEqual(r.status_code, 201, r.text)
        doc = await self.w.store.get("credentials", r.json()["id"])
        self.assertTrue(doc["secret_enc"].startswith("enc1:k2:"))
        self.assertEqual(await self.decrypted_by_location(self.w.S.box) | {}, {
            **self.original, ("credentials", doc["id"], "secret_enc"): "sk-ant-test-" + "z" * 30})

    async def test_a_crash_mid_run_resumes_from_the_saved_cursor(self):
        from core import key_rotation as kr
        box = self.ring("k2", "k1", "k2")
        calls = {"n": 0}

        async def crash(coll, after):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("worker killed")

        with self.assertRaises(RuntimeError):
            await kr.rotate(self.w.store, box, now=self.now, batch=1, on_batch=crash)
        run = await self.w.store.get(kr.RUNS, "to-k2")
        self.assertEqual(run["status"], "running")
        partial = await kr.verify(self.w.store, box)
        self.assertFalse(partial["ok"])
        self.assertGreater(partial["on_active_key"], 0)
        await kr.rotate(self.w.store, box, now=self.now, batch=1)
        self.assertTrue((await kr.verify(self.w.store, box))["ok"])
        self.assertEqual(await self.decrypted_by_location(box), self.original)
        actions = [a["action"] for a in await self.w.store.find(AUDIT, {}) if a["action"].startswith("encryption.")]
        self.assertEqual(actions.count("encryption.rotation_started"), 1)
        self.assertEqual(actions.count("encryption.rotation_resumed"), 1)
        self.assertEqual(actions.count("encryption.rotation_completed"), 1)

    async def test_a_concurrent_write_always_wins(self):
        """An administrator replacing a secret during rotation keeps the new secret."""
        from core import key_rotation as kr
        box = self.ring("k2", "k1", "k2")
        self.w.S.box = box
        cred = self.w.cA_ai.id
        store = self.w.store
        real = store.set_field_if

        async def racing(coll, oid, path, expected, value):
            if coll == "credentials" and oid == cred:
                r = await self.w.call("PATCH", f"/credentials/{cred}", "owner", {"secret": "sk-ant-replaced-" + "y" * 30})
                assert r.status_code == 200, r.text
            return await real(coll, oid, path, expected, value)

        store.set_field_if = racing
        try:
            report = await kr.rotate(store, box, now=self.now)
        finally:
            store.set_field_if = real
        self.assertEqual(report["raced"], {"credentials": 1})
        self.assertEqual(box.decrypt((await store.get("credentials", cred))["secret_enc"]), "sk-ant-replaced-" + "y" * 30)
        self.assertTrue((await kr.verify(store, box))["ok"])

    async def test_rollback_moves_every_value_back_to_the_previous_key(self):
        from core import key_rotation as kr
        await kr.rotate(self.w.store, self.ring("k2", "k1", "k2"), now=self.now)
        back = self.ring("k1", "k1", "k2")
        await kr.rotate(self.w.store, back, now=self.now)
        self.assertTrue((await kr.verify(self.w.store, back))["ok"])
        self.assertEqual(await self.decrypted_by_location(self.ring("k1", "k1")), self.original)

    async def test_tenant_isolation_and_no_secret_in_reports_audit_or_run_records(self):
        from core import key_rotation as kr
        box = self.ring("k2", "k1", "k2")
        untouched = {c: copy.deepcopy(d) for c, d in self.w.store.collections.items()
                     if c not in kr.ENCRYPTED_COLLECTIONS and c not in (AUDIT, kr.RUNS)}
        before_docs = {c: copy.deepcopy(self.w.store.collections.get(c, {})) for c in kr.ENCRYPTED_COLLECTIONS}
        report = await kr.rotate(self.w.store, box, now=self.now)
        verify = await kr.verify(self.w.store, box)
        for c, d in untouched.items():
            self.assertEqual(self.w.store.collections[c], d, c)                  # nothing outside the allowlist
        for c, docs in before_docs.items():                                       # only *_enc fields changed
            for oid, doc in docs.items():
                now_doc = self.w.store.collections[c][oid]
                strip = lambda d: {k: (strip(v) if isinstance(v, dict) else v) for k, v in d.items()
                                   if not k.endswith("_enc")}
                self.assertEqual(strip(now_doc), strip(doc), (c, oid))
        ents = {d["id"] for d in self.w.store.collections["enterprises"].values()}
        run = await self.w.store.get(kr.RUNS, "to-k2")
        self.assertLessEqual(set(run["by_enterprise"]), ents)
        audit_text = json.dumps([a for a in self.w.store.collections[AUDIT].values()
                                 if a["action"].startswith("encryption.")])
        blob = json.dumps([report, verify, run, audit_text], default=str)
        for secret in set(self.original.values()) | self.plaintexts:
            self.assertNotIn(secret, blob)
        for k in (self.k1, self.k2):
            self.assertNotIn(k, blob)
        self.assertNotIn("enc1:", blob)
        self.assertNotIn("gAAAA", blob)                                           # no ciphertext either

    async def test_undecryptable_values_are_reported_with_location_never_fatal(self):
        from core import key_rotation as kr
        await self.w.store.set_field_if("credentials", self.w.cA_ai.id, "secret_enc",
                                        (await self.w.store.get("credentials", self.w.cA_ai.id))["secret_enc"],
                                        "enc1:k9:garbage")
        box = self.ring("k2", "k1", "k2")
        report = await kr.rotate(self.w.store, box, now=self.now)
        self.assertEqual(report["undecryptable"], {"credentials": 1})
        v = await kr.verify(self.w.store, box)
        self.assertFalse(v["ok"])
        self.assertEqual(v["undecryptable"], [{"collection": "credentials", "id": self.w.cA_ai.id, "field": "secret_enc"}])


class SourceTests(unittest.TestCase):
    def test_every_secret_goes_through_the_box(self):
        """No module builds a Fernet directly: the key ring is the only encryption path."""
        for d in SOURCE_DIRS:
            for f in (BACKEND_DIR / d).glob("*.py"):
                if f.name == "credentials.py" and d == "core":
                    continue
                self.assertNotIn("Fernet(", f.read_text(), f.name)

    def test_entrypoint_loads_the_key_ring_from_files(self):
        text = (BACKEND_DIR / "docker-entrypoint.sh").read_text()
        self.assertRegex(text, r"for name in [^\n]*\bENCRYPTION_KEYS\b")


if __name__ == "__main__":
    unittest.main()
