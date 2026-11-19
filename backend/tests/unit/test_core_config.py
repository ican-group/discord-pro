"""Spec 012: configuration registry, sparse overrides, central resolver, policies."""
import unittest

from tests.unit._core_fixtures import Tree, run, with_overrides
from core import config_registry as reg
from core.config_registry import Combine, ConfigError, KnowledgeMode, SecurityClass
from core.credentials import Provider, SecretBox, new_credential, revoke
from core.hierarchy import Chain, Level, new_id
from core.resolver import (apply_override_patch, apply_policy_patch, resolve, validate_override,
                           validate_policy_write)

KEY = "dW5pdC10ZXN0LWZlcm5ldC1rZXktbm90LXJlYWwtMDA="   # synthetic Fernet key


def ov(**kv):
    return {k.replace("__", "."): v for k, v in kv.items()}


class RegistryTests(unittest.TestCase):
    def test_every_property_is_fully_specified(self):
        for name, p in reg.PROPERTIES.items():
            with self.subTest(name=name):
                self.assertEqual(p.name, name)
                self.assertTrue(p.levels)
                self.assertIsInstance(p.security, SecurityClass)
                self.assertIn(p.min_role, reg.ROLE_ORDER)
                if p.default is not None:
                    p.validator(p.default if not isinstance(p.default, KnowledgeMode) else p.default.value)

    def test_required_knowledge_properties_exist(self):
        for name in ("knowledge.mode", "knowledge.approved_sources", "knowledge.min_evidence_score",
                     "knowledge.require_citations", "knowledge.insufficient_fallback", "knowledge.human_handoff"):
            self.assertIn(name, reg.PROPERTIES)
        for name in ("knowledge.minimum_mode", "knowledge.allowed_sources", "knowledge.access_scope",
                     "knowledge.document_acl_required", "knowledge.general_fallback_allowed",
                     "knowledge.min_evidence_floor", "knowledge.require_citations"):
            self.assertIn(name, reg.POLICIES)

    def test_no_secret_valued_settings(self):
        """Credentials are references only; no property may hold a raw secret."""
        for name, p in reg.PROPERTIES.items():
            self.assertNotRegex(name, r"(api_key|token|secret|password)$")
            if "credential" in name:
                self.assertIs(p.security, SecurityClass.CREDENTIAL_REF)

    def test_registry_table_is_documentable(self):
        rows = reg.registry_table()
        self.assertEqual(len(rows), len(reg.PROPERTIES))
        self.assertTrue(all({"name", "default", "overridable_at", "security"} <= set(r) for r in rows))


class InheritanceTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()

    def eff(self, chain=None, **kw):
        return resolve(chain or self.c, **kw)

    def test_defaults_with_default_provenance(self):
        e = self.eff()
        self.assertEqual(e.values["behaviour.language"], "en")
        self.assertEqual(e.sources["behaviour.language"].kind, "default")

    def test_inheritance_and_provenance(self):
        c = with_overrides(self.c, enterprise={"overrides": ov(behaviour__language="de")})
        e = self.eff(c)
        self.assertEqual(e.values["behaviour.language"], "de")
        self.assertEqual(e.sources["behaviour.language"].to_dict(),
                         {"kind": "override", "level": "enterprise", "object_id": c.enterprise.id})

    def test_nearest_valid_override_wins(self):
        c = with_overrides(self.c, enterprise={"overrides": ov(behaviour__language="de")},
                           organisation={"overrides": ov(behaviour__language="fr")},
                           instance={"overrides": ov(behaviour__language="es")})
        e = self.eff(c)
        self.assertEqual((e.values["behaviour.language"], e.sources["behaviour.language"].level), ("es", "bot_instance"))

    def test_removing_an_override_restores_the_inherited_value(self):
        t, c = self.t, self.c
        run(t.svc.set_overrides(Level.ORGANISATION, c.organisation.id, ov(behaviour__fallback_message="Org says hi")))
        run(t.svc.set_overrides(Level.BOT, c.bot.id, ov(behaviour__fallback_message="Bot says hi")))
        chain = t.chain(Level.BOT_INSTANCE, c.instance.id)
        self.assertEqual(self.eff(chain).values["behaviour.fallback_message"], "Bot says hi")
        new = apply_override_patch(t.chain(Level.BOT, c.bot.id), {"behaviour.fallback_message": None})
        run(t.svc.set_overrides(Level.BOT, c.bot.id, new))
        chain = t.chain(Level.BOT_INSTANCE, c.instance.id)
        e = self.eff(chain)
        self.assertEqual(e.values["behaviour.fallback_message"], "Org says hi")
        self.assertEqual(e.sources["behaviour.fallback_message"].level, "organisation")

    def test_parent_changes_propagate_without_copies(self):
        t, c = self.t, self.c
        run(t.svc.set_overrides(Level.ENTERPRISE, c.enterprise.id, ov(behaviour__language="nl")))
        self.assertEqual(self.eff(t.chain(Level.BOT_INSTANCE, c.instance.id)).values["behaviour.language"], "nl")
        run(t.svc.set_overrides(Level.ENTERPRISE, c.enterprise.id, ov(behaviour__language="it")))
        self.assertEqual(self.eff(t.chain(Level.BOT_INSTANCE, c.instance.id)).values["behaviour.language"], "it")
        for col, oid in (("organisations", c.organisation.id), ("bots", c.bot.id), ("bot_instances", c.instance.id)):
            self.assertEqual(run(t.store.get(col, oid))["overrides"], {})       # nothing copied down

    def test_valid_and_invalid_override_writes(self):
        bot_chain = Chain(self.c.enterprise, self.c.organisation, self.c.bot)
        self.assertEqual(validate_override(bot_chain, "behaviour.persona", "Be brief."), "Be brief.")
        bad = [("behaviour.persona", "x" * 4001), ("behaviour.language", "english!"),
               ("knowledge.mode", "creative"), ("knowledge.min_evidence_score", 2), ("widget.theme", {"accent": "red"}),
               ("widget.custom_css_url", "http://evil.example/x.css"), ("behaviour.tone_examples", [{"user": "x"}]),
               ("limits.end_user_msgs_per_minute", 0), ("no.such.key", 1)]
        for key, value in bad:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    validate_override(bot_chain, key, value)

    def test_level_restrictions(self):
        ent_chain = Chain(self.c.enterprise)
        with self.assertRaises(ConfigError):           # persona is Bot/Instance only
            validate_override(ent_chain, "behaviour.persona", "x")
        with self.assertRaises(ConfigError):           # AI provider not overridable per instance
            validate_override(self.c, "ai.provider", "openai")
        with self.assertRaises(ConfigError):           # tone examples are Bot-only
            validate_override(self.c, "behaviour.tone_examples", [])

    def test_invalid_stored_overrides_are_ignored_and_reported(self):
        c = with_overrides(self.c, enterprise={"overrides": ov(behaviour__language="de")},
                           bot={"overrides": {"behaviour.language": "!!", "behaviour.persona": 7,
                                              "unknown.key": 1, "ai.provider": "openai"}},
                           instance={"overrides": ov(ai__provider="openai")})
        e = self.eff(c)
        self.assertEqual(e.values["behaviour.language"], "de")
        self.assertEqual(e.values["behaviour.persona"], reg.PROPERTIES["behaviour.persona"].default)
        reasons = {(x.key, x.level) for x in e.conflicts}
        self.assertIn(("behaviour.language", "bot"), reasons)
        self.assertIn(("behaviour.persona", "bot"), reasons)
        self.assertIn(("unknown.key", "bot"), reasons)
        self.assertIn(("ai.provider", "bot_instance"), reasons)          # not settable on instances
        self.assertEqual(e.values["ai.provider"], "openai")               # bot-level value is valid

    def test_override_patch_is_all_or_nothing(self):
        bot_chain = Chain(self.c.enterprise, self.c.organisation, self.c.bot)
        with self.assertRaises(ConfigError):
            apply_override_patch(bot_chain, {"behaviour.persona": "ok", "behaviour.language": "!!"})
        new = apply_override_patch(bot_chain, {"behaviour.persona": "ok", "knowledge.mode": "knowledge_preferred"})
        self.assertEqual(new, {"behaviour.persona": "ok", "knowledge.mode": "knowledge_preferred"})


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()

    def test_allowed_sets_intersect(self):
        c = with_overrides(self.c, enterprise={"policy": {"channels.allowed": ["webchat", "telegram"]}},
                           organisation={"policy": {"channels.allowed": ["telegram", "discord"]}})
        e = resolve(c)
        self.assertEqual(e.policy["channels.allowed"], frozenset({"telegram"}))
        self.assertIn("channel webchat is not permitted by policy", e.runtime_blockers)
        self.assertEqual(e.policy_sources["channels.allowed"].level, "organisation")

    def test_maximum_limits_take_minimum(self):
        c = with_overrides(self.c, enterprise={"policy": {"limits.max_end_user_msgs_per_minute": 30}},
                           organisation={"policy": {"limits.max_end_user_msgs_per_minute": 50}},
                           bot={"overrides": ov(limits__end_user_msgs_per_minute=40)})
        e = resolve(c)
        self.assertEqual(e.policy["limits.max_end_user_msgs_per_minute"], 30)   # org cannot raise
        self.assertEqual(e.values["limits.end_user_msgs_per_minute"], 10)       # bot override non-compliant -> default
        self.assertTrue(any(x.key == "limits.max_end_user_msgs_per_minute" and x.level == "organisation"
                            for x in e.conflicts))

    def test_platform_policy_cannot_be_weakened(self):
        c = with_overrides(self.c, enterprise={"policy": {"limits.max_end_user_msgs_per_minute": 500,
                                                          "security.require_webhook_signatures": False,
                                                          "ai.allowed_providers": ["anthropic", "ollama"]}})
        e = resolve(c)
        self.assertEqual(e.policy["limits.max_end_user_msgs_per_minute"], 60)
        self.assertTrue(e.policy["security.require_webhook_signatures"])
        self.assertEqual(e.policy["ai.allowed_providers"], frozenset({"anthropic"}))   # ollama not platform-allowed
        ent = Chain(self.c.enterprise)
        for key, value in (("limits.max_end_user_msgs_per_minute", 500), ("security.require_webhook_signatures", False),
                           ("ai.allowed_providers", ["ollama"])):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    validate_policy_write(ent, key, value)

    def test_required_controls_stay_required(self):
        c = with_overrides(self.c, enterprise={"policy": {"knowledge.require_citations": True}},
                           organisation={"policy": {"knowledge.require_citations": False}})
        self.assertTrue(resolve(c).policy["knowledge.require_citations"])
        org = Chain(c.enterprise, c.organisation)
        with self.assertRaises(ConfigError):
            validate_policy_write(org, "knowledge.require_citations", False)

    def test_allow_flags_and_enterprise_authority(self):
        # Enterprise silent => KNOWLEDGE_PREFERRED general fallback is NOT permitted, and an
        # Organisation cannot grant it on its own.
        org = Chain(self.c.enterprise, self.c.organisation)
        with self.assertRaises(ConfigError):
            validate_policy_write(org, "knowledge.general_fallback_allowed", True)
        stored = with_overrides(self.c, organisation={"policy": {"knowledge.general_fallback_allowed": True}})
        self.assertFalse(resolve(stored).policy["knowledge.general_fallback_allowed"])
        ent = Chain(self.c.enterprise)
        self.assertTrue(validate_policy_write(ent, "knowledge.general_fallback_allowed", True))
        granted = with_overrides(self.c, enterprise={"policy": {"knowledge.general_fallback_allowed": True}},
                                 organisation={"policy": {"knowledge.general_fallback_allowed": False}})
        self.assertFalse(resolve(granted).policy["knowledge.general_fallback_allowed"])   # org may tighten

    def test_policy_only_at_enterprise_and_organisation(self):
        bot_chain = Chain(self.c.enterprise, self.c.organisation, self.c.bot)
        with self.assertRaises(ConfigError):
            validate_policy_write(bot_chain, "channels.allowed", ["webchat"])
        with self.assertRaises(ConfigError):     # delegation is Enterprise-only
            validate_policy_write(Chain(self.c.enterprise, self.c.organisation), "delegation.org_admins_manage_members", False)
        with self.assertRaises(Exception):
            run(self.t.svc.set_policy(Level.BOT, self.c.bot.id, {}))

    def test_policy_patch(self):
        ent = Chain(self.c.enterprise)
        new = apply_policy_patch(ent, {"channels.allowed": ["webchat"], "limits.max_monthly_ai_messages": 1000})
        self.assertEqual(new, {"channels.allowed": ["webchat"], "limits.max_monthly_ai_messages": 1000})
        with self.assertRaises(ConfigError):
            apply_policy_patch(ent, {"channels.allowed": ["smoke-signals"]})

    def test_each_combine_rule_is_monotonic(self):
        """combine(parent, child) is never weaker than parent for every policy key."""
        samples = {Combine.INTERSECT: [frozenset({"a", "b"}), frozenset({"b", "c"})], Combine.MIN: [5, 9],
                   Combine.MAX: [0.2, 0.7], Combine.ALLOW: [True, False], Combine.REQUIRE: [False, True]}
        for key in reg.POLICIES.values():
            vals = samples.get(key.combine)
            if not vals:
                continue
            for a in vals:
                for b in vals:
                    out = reg.combine(key, a, b)
                    self.assertEqual(reg.combine(key, out, a), out, key.name)    # tighter than a
                    self.assertEqual(reg.combine(key, out, b), out, key.name)    # tighter than b


class AiAndCredentialTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()
        self.box = SecretBox(KEY)

    def cred(self, owner_chain, provider=Provider.ANTHROPIC, secret="sk-ant-UNITTEST-0000"):
        return new_credential(credential_id=new_id(), chain=owner_chain, provider=provider, name="key",
                              secret=secret, box=self.box, actor_id="u", now="t")

    def test_ai_blocked_without_credential(self):
        e = resolve(self.c)
        self.assertEqual(e.blocked.get("ai"), "no AI credential is configured")
        self.assertFalse(e.runnable)

    def test_valid_credential_and_redaction(self):
        cred = self.cred(Chain(self.c.enterprise, self.c.organisation))
        c = with_overrides(self.c, organisation={"overrides": ov(ai__credential_id=cred.id)})
        e = resolve(c, credentials={cred.id: cred})
        self.assertNotIn("ai", e.blocked)
        self.assertTrue(e.runnable)
        view = e.credentials["ai.credential_id"]
        self.assertEqual(view["id"], cred.id)
        self.assertNotIn("secret_enc", view)
        self.assertNotIn("sk-ant-UNITTEST", repr(e))
        self.assertNotIn("sk-ant-UNITTEST", repr(cred))

    def test_cross_tenant_and_out_of_scope_credentials_are_refused(self):
        t2 = self.t
        other = t2.full()
        foreign = self.cred(Chain(other.enterprise))
        sibling_org = t2.organisation(self.c.enterprise.id)
        sibling = self.cred(Chain(self.c.enterprise, sibling_org))
        for cred in (foreign, sibling):
            with self.subTest(owner=cred.organisation_id or cred.enterprise_id):
                c = with_overrides(self.c, bot={"overrides": ov(ai__credential_id=cred.id)})
                e = resolve(c, credentials={cred.id: cred})
                self.assertEqual(e.blocked["ai"], "The selected credential is not available here.")
                with self.assertRaises(ConfigError):
                    validate_override(Chain(self.c.enterprise, self.c.organisation, self.c.bot),
                                      "ai.credential_id", cred.id, credentials={cred.id: cred})

    def test_enterprise_credentials_policy_and_revocation(self):
        cred = self.cred(Chain(self.c.enterprise))
        c = with_overrides(self.c, enterprise={"overrides": ov(ai__credential_id=cred.id)})
        self.assertNotIn("ai", resolve(c, credentials={cred.id: cred}).blocked)
        c2 = with_overrides(c, enterprise={"overrides": ov(ai__credential_id=cred.id),
                                           "policy": {"credentials.allow_enterprise_credentials": False}})
        self.assertIn("ai", resolve(c2, credentials={cred.id: cred}).blocked)
        revoked = revoke(cred, now="t2")
        self.assertEqual(resolve(c, credentials={cred.id: revoked}).blocked["ai"], "The selected credential has been revoked.")

    def test_provider_mismatch_and_model_rules(self):
        cred = self.cred(Chain(self.c.enterprise, self.c.organisation), provider=Provider.OPENAI)
        c = with_overrides(self.c, organisation={"overrides": ov(ai__credential_id=cred.id)})
        self.assertEqual(resolve(c, credentials={cred.id: cred}).blocked["ai"],
                         "The selected credential is for a different service.")
        c = with_overrides(self.c, enterprise={"overrides": ov(ai__provider="openai")},
                           bot={"overrides": ov(ai__model="claude-sonnet-4-6")})
        e = resolve(c)
        self.assertEqual(e.values["ai.model"], reg.DEFAULT_MODEL["openai"])
        self.assertTrue(any(x.key == "ai.model" for x in e.conflicts))

    def test_provider_policy(self):
        c = with_overrides(self.c, enterprise={"overrides": ov(ai__provider="openai"),
                                               "policy": {"ai.allowed_providers": ["anthropic"]}})
        e = resolve(c)
        self.assertEqual(e.values["ai.provider"], "anthropic")        # non-compliant override skipped
        c = with_overrides(self.c, enterprise={"policy": {"ai.allowed_providers": ["anthropic"]}},
                           organisation={"policy": {"ai.allowed_providers": ["openai"]}})
        e = resolve(c)
        self.assertIsNone(e.values["ai.provider"])
        self.assertEqual(e.blocked["ai"], "no AI provider is permitted by policy")


class ChannelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def test_embed_origins_restricted_by_policy(self):
        e = self.t.enterprise()
        o = self.t.organisation(e.id)
        b = self.t.bot(o.id)
        i = self.t.instance(b.id, {"type": "webchat", "allowed_origins": ["https://a.example", "https://evil.example"]})
        chain = self.t.chain(Level.BOT_INSTANCE, i.id)
        self.assertEqual(resolve(chain).values["channel.allowed_origins"],
                         frozenset({"https://a.example", "https://evil.example"}))
        chain = with_overrides(chain, enterprise={"policy": {"webchat.allowed_embed_origins": ["https://a.example"]}})
        eff = resolve(chain)
        self.assertEqual(eff.values["channel.allowed_origins"], frozenset({"https://a.example"}))
        self.assertTrue(any(x.key == "channel.allowed_origins" for x in eff.conflicts))
        chain = with_overrides(chain, organisation={"policy": {"webchat.public_access_allowed": False}})
        self.assertIn("public web chat is not permitted by policy", resolve(chain).runtime_blockers)

    def test_unbound_discord_and_telegram_without_token(self):
        e = self.t.enterprise()
        o = self.t.organisation(e.id)
        b = self.t.bot(o.id)
        d = self.t.instance(b.id, {"type": "discord"})
        tg = self.t.instance(b.id, {"type": "telegram"})
        self.assertTrue(resolve(self.t.chain(Level.BOT_INSTANCE, d.id)).runtime_blockers)
        self.assertIn("no Telegram bot token is configured",
                      resolve(self.t.chain(Level.BOT_INSTANCE, tg.id)).runtime_blockers)

    def test_public_values_exclude_internal_settings(self):
        c = self.t.full()
        c = with_overrides(c, bot={"overrides": ov(behaviour__persona="secret sauce", widget__title="Help")})
        pub = resolve(c).public_values()
        self.assertEqual(pub["widget.title"], "Help")
        self.assertNotIn("behaviour.persona", pub)
        self.assertNotIn("ai.credential_id", pub)


if __name__ == "__main__":
    unittest.main()
