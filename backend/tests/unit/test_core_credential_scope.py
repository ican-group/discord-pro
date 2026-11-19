"""X6: per-credential usage scopes (Phase 4A)."""
import unittest

from tests.unit._core_fixtures import Tree, run, with_overrides

from core.config_registry import ConfigError
from core.credentials import (CredentialError, Provider, SecretBox, check_use, credential_to_doc, credential_from_doc,
                              new_credential, normalise_usable_by)
from core.hierarchy import Chain, Level, LifecycleAction, new_id
from core.lifecycle import Authority
from core.resolver import resolve, validate_override

FERNET = "c3BlYzAxMi10ZXN0LW9ubHktZmVybmV0LWtleS0zMmI="


class CredentialScopeTests(unittest.TestCase):
    def setUp(self):
        t = self.t = Tree()
        self.e = t.enterprise()
        self.o1, self.o2 = t.organisation(self.e.id), t.organisation(self.e.id)
        self.b1, self.b1b = t.bot(self.o1.id), t.bot(self.o1.id)
        self.b2 = t.bot(self.o2.id)
        self.i1 = t.instance(self.b1.id)
        self.other = t.full()
        self.box = SecretBox(FERNET)

    def norm(self, entries, org=None, enterprise=None):
        return run(normalise_usable_by(self.t.store, enterprise_id=enterprise or self.e.id, organisation_id=org,
                                       entries=entries))

    def cred(self, owner_level, owner_id, usable_by=()):
        return new_credential(credential_id=new_id(), chain=self.t.chain(owner_level, owner_id),
                              provider=Provider.ANTHROPIC, name="c", secret="not-a-real-key", box=self.box,
                              actor_id="u", now="t", usable_by=usable_by)

    def usable(self, cred, level, oid):
        try:
            check_use(cred, referencing_chain=self.t.chain(level, oid), slot="ai.anthropic", policy={})
            return True
        except CredentialError:
            return False

    def test_scope_validation_against_the_owner_hierarchy(self):
        self.assertEqual(self.norm([{"level": "organisation", "id": self.o1.id}, {"level": "bot", "id": self.b2.id}]),
                         tuple(sorted({("organisation", self.o1.id), ("bot", self.b2.id)})))
        self.assertEqual(self.norm([{"level": "bot", "id": self.b1.id}], org=self.o1.id), (("bot", self.b1.id),))
        bad = [([{"level": "organisation", "id": self.other.organisation.id}], None),   # foreign Enterprise
               ([{"level": "bot", "id": self.other.bot.id}], None),
               ([{"level": "organisation", "id": self.o1.id}], self.o1.id),             # org credential: bots only
               ([{"level": "bot", "id": self.b2.id}], self.o1.id),                      # bot of another org
               ([{"level": "bot_instance", "id": self.i1.id}], None),
               ([{"level": "bot", "id": "not-a-uuid"}], None), (["x"], None),
               ([{"level": "bot", "id": self.b1.id}] * 201, None)]
        for entries, org in bad:
            with self.subTest(entries=str(entries)[:80], org=org):
                with self.assertRaises(CredentialError):
                    self.norm(entries, org=org)
        run(self.t.svc.change_lifecycle(Level.BOT, self.b1b.id, LifecycleAction.BEGIN_DELETE,
                                        authority=Authority.ENTERPRISE, actor_id="u"))
        with self.assertRaises(CredentialError):
            self.norm([{"level": "bot", "id": self.b1b.id}])                            # deleted

    def test_enterprise_credential_restricted_to_an_organisation(self):
        c = self.cred(Level.ENTERPRISE, self.e.id, (("organisation", self.o1.id),))
        self.assertTrue(self.usable(c, Level.BOT, self.b1.id))
        self.assertTrue(self.usable(c, Level.BOT_INSTANCE, self.i1.id))
        self.assertTrue(self.usable(c, Level.ORGANISATION, self.o1.id))
        self.assertFalse(self.usable(c, Level.BOT, self.b2.id))
        self.assertFalse(self.usable(c, Level.ENTERPRISE, self.e.id))                  # would reach every org

    def test_organisation_credential_restricted_to_a_bot(self):
        c = self.cred(Level.ORGANISATION, self.o1.id, (("bot", self.b1.id),))
        self.assertTrue(self.usable(c, Level.BOT, self.b1.id))
        self.assertFalse(self.usable(c, Level.BOT, self.b1b.id))
        self.assertFalse(self.usable(c, Level.ORGANISATION, self.o1.id))

    def test_never_usable_outside_the_owner_hierarchy_even_if_forged(self):
        forged = self.cred(Level.ORGANISATION, self.o1.id, (("bot", self.b2.id), ("bot", self.other.bot.id)))
        self.assertFalse(self.usable(forged, Level.BOT, self.b2.id))                   # other org of same tenant
        self.assertFalse(self.usable(forged, Level.BOT, self.other.bot.id))            # other tenant

    def test_assignment_and_runtime_resolution_respect_the_scope(self):
        c = self.cred(Level.ENTERPRISE, self.e.id, (("bot", self.b1.id),))
        creds = {c.id: c}
        ok = validate_override(self.t.chain(Level.BOT, self.b1.id), "ai.credential_id", c.id, credentials=creds)
        self.assertEqual(ok, c.id)
        with self.assertRaises(ConfigError):
            validate_override(self.t.chain(Level.BOT, self.b2.id), "ai.credential_id", c.id, credentials=creds)
        # stored reference that falls outside a (later narrowed) scope: blocked at resolution
        chain = with_overrides(self.t.chain(Level.BOT, self.b2.id), bot={"overrides": {"ai.credential_id": c.id}})
        eff = resolve(chain, credentials=creds)
        self.assertIn("ai", eff.blocked)
        self.assertNotIn("ai.credential_id", eff.credentials)

    def test_scope_round_trips_without_secrets(self):
        c = self.cred(Level.ENTERPRISE, self.e.id, (("bot", self.b1.id),))
        doc = credential_to_doc(c, 1)
        self.assertEqual(credential_from_doc(doc).usable_by, c.usable_by)
        view = c.public_view()
        self.assertEqual(view["usable_by"], [{"level": "bot", "id": self.b1.id}])
        self.assertNotIn("secret", " ".join(view))


if __name__ == "__main__":
    unittest.main()
