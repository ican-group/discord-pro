"""Architectural guard tests (spec 012; CLAUDE.md §1). Each test exercises
behaviour of the real modules rather than searching for words:

* no parallel Workspace model can be expressed;
* strict knowledge inheritance cannot be weakened (writes and stored data);
* cross-tenant credential references fail closed;
* ancestry is immutable through every write path;
* platform credentials cannot be managed by tenants;
* channel bindings cannot be duplicated;
* D-Q6/D-Q7: binding schemas keep handoff/passive mode and carry no scripted
  or suggestion settings;
* the kept root modules never import the new code; server.py/worker.py are wiring only;
* the runtime image is the Spec 012 release candidate (Phase 6B).
"""
import ast
import re
import unittest
from pathlib import Path

from tests.unit._core_fixtures import Tree, run, with_overrides
from tests.unit._support import BACKEND_DIR

from core import config_registry as reg
from core import deletion as dl
from core import resolver
from core.authz import Actor, AuthzError, Membership, Role
from core.config_registry import ConfigError
from core.credentials import (CredentialError, Provider, SecretBox, check_manage_platform, check_use,
                              new_credential)
from core.hierarchy import (COLLECTION, DiscordBinding, HierarchyError, Level, TelegramBinding, WebchatBinding,
                            new_id)

STRICT = reg.KnowledgeMode.STRICT_KNOWLEDGE_ONLY.value
FERNET = "c3BlYzAxMi10ZXN0LW9ubHktZmVybmV0LWtleS0zMmI="


class NoWorkspaceModelTests(unittest.TestCase):
    def test_hierarchy_has_exactly_four_levels(self):
        self.assertEqual([l.value for l in Level], ["enterprise", "organisation", "bot", "bot_instance"])
        self.assertEqual(set(COLLECTION.values()), {"enterprises", "organisations", "bots", "bot_instances"})
        with self.assertRaises(ValueError):
            Level("workspace")

    def test_memberships_cannot_target_another_kind_of_scope(self):
        uid, eid = new_id(), new_id()
        for level in (Level.BOT_INSTANCE,):
            with self.assertRaises(AuthzError):
                Membership(uid, eid, level, new_id(), Role.VIEWER).validated()

    def test_deletion_and_storage_know_only_the_four_levels(self):
        self.assertEqual(set(dl.OWNER_FIELD), set(Level))
        self.assertFalse(any("workspace" in c for c in COLLECTION.values()))


class StrictKnowledgeInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.chain = self.t.full()
        self.strict = with_overrides(self.chain, enterprise={"policy": {"knowledge.minimum_mode": STRICT}})

    def test_descendants_cannot_select_a_weaker_mode(self):
        for attr in ("organisation", "bot", "instance"):
            for weaker in ("general", "knowledge_preferred"):
                with self.subTest(level=attr, mode=weaker):
                    target = {"organisation": resolver.Chain(self.strict.enterprise, self.strict.organisation),
                              "bot": resolver.Chain(self.strict.enterprise, self.strict.organisation, self.strict.bot),
                              "instance": self.strict}[attr]
                    with self.assertRaises(ConfigError):
                        resolver.apply_override_patch(target, {"knowledge.mode": weaker})

    def test_organisation_policy_cannot_lower_the_minimum_or_grant_fallback(self):
        org_chain = resolver.Chain(self.strict.enterprise, self.strict.organisation)
        with self.assertRaises(ConfigError):
            resolver.apply_policy_patch(org_chain, {"knowledge.minimum_mode": "general"})
        with self.assertRaises(ConfigError):
            resolver.apply_policy_patch(org_chain, {"knowledge.general_fallback_allowed": True})

    def test_stored_weaker_overrides_are_clamped_and_reported(self):
        c = with_overrides(self.strict, bot={"overrides": {"knowledge.mode": "general"}},
                           instance={"overrides": {"knowledge.mode": "knowledge_preferred"}})
        eff = resolver.resolve(c)
        self.assertEqual(reg.KnowledgeMode(eff.values["knowledge.mode"]).value, STRICT)
        self.assertIn("knowledge.mode", {x.key for x in eff.conflicts})
        from core.knowledge import plan_for
        plan = plan_for(eff)
        self.assertTrue(plan.strict)
        self.assertTrue(plan.require_citations)              # strict mode always requires citations
        self.assertFalse(plan.general_fallback_allowed)


class CredentialScopeTests(unittest.TestCase):
    def test_cross_tenant_and_sibling_references_fail_closed(self):
        t = Tree()
        a = t.full()
        b = t.full()
        sibling_org = t.organisation(a.enterprise.id)
        box = SecretBox(FERNET)

        def cred(chain):
            return new_credential(credential_id=new_id(), chain=chain, provider=Provider.TELEGRAM_BOT, name="t",
                                  secret="123:abc", box=box, actor_id="u", now="t")
        foreign = cred(resolver.Chain(b.enterprise, b.organisation))
        sibling = cred(t.chain(Level.ORGANISATION, sibling_org.id))
        own = cred(resolver.Chain(a.enterprise, a.organisation))
        for bad in (foreign, sibling):
            with self.assertRaises(CredentialError) as ctx:
                check_use(bad, referencing_chain=a, slot="channel.telegram", policy={})
            self.assertEqual(str(ctx.exception), "The selected credential is not available here.")
        self.assertIs(check_use(own, referencing_chain=a, slot="channel.telegram", policy={}), own)

    def test_platform_credentials_are_never_manageable_by_tenants(self):
        for actor in (Actor(new_id()), Actor(new_id(), is_platform_superadmin=False)):
            with self.assertRaises(AuthzError):
                check_manage_platform(actor)
        check_manage_platform(Actor(new_id(), is_platform_superadmin=True))


class ImmutableAncestryTests(unittest.TestCase):
    def test_no_write_path_changes_ancestry(self):
        t = Tree()
        c = t.full()
        other = t.full()
        for level, obj in ((Level.ORGANISATION, c.organisation), (Level.BOT, c.bot), (Level.BOT_INSTANCE, c.instance)):
            for field in ("enterprise_id", "organisation_id", "bot_id", "id", "level", "created_by"):
                with self.subTest(level=level, field=field):
                    with self.assertRaises(HierarchyError):
                        run(t.svc.update(level, obj.id, {field: getattr(other, level.value if level is not
                                                                         Level.BOT_INSTANCE else "instance").id},
                                         actor_id="u"))
        after = t.chain(Level.BOT_INSTANCE, c.instance.id)
        self.assertEqual(after.ids(), c.ids())

    def test_deletion_filters_are_always_tenant_pinned(self):
        for level in Level:
            flt = dl._scoped(level, "target", "enterprise")
            self.assertEqual(flt["enterprise_id"], "enterprise")


class BindingSchemaTests(unittest.TestCase):
    def test_discord_keeps_handoff_and_passive_mode_without_scripted_or_suggestions(self):
        fields = set(DiscordBinding.__dataclass_fields__)
        self.assertTrue({"passive_mode", "staff_role_name", "handoff_cooldown_minutes",
                         "handoff_followup_message", "dm_enabled"} <= fields)
        for binding in (DiscordBinding, TelegramBinding, WebchatBinding):
            names = " ".join(binding.__dataclass_fields__)
            self.assertNotRegex(names, r"script|suggest|reply_mode|agent_assist")

    def test_retired_settings_are_not_stored(self):
        t = Tree()
        c = t.full()
        inst = t.instance(c.bot.id, {"type": "discord", "scripted_mode": True, "suggestions": True})
        stored = run(t.store.get("bot_instances", inst.id))["channel"]
        self.assertFalse({"scripted_mode", "suggestions"} & set(stored))
        self.assertFalse(stored["dm_enabled"])

    def test_duplicate_bindings_are_refused(self):
        t = Tree()
        c = t.full()
        public_key = f"webchat:{c.instance.channel.public_id}"
        with self.assertRaises(HierarchyError):
            run(t.svc.bindings.claim(new_id(), new_id(), {public_key: ("*",)}))
        cred = new_id()
        t.instance(c.bot.id, {"type": "telegram", "credential_id": cred})
        with self.assertRaises(HierarchyError):
            t.instance(c.bot.id, {"type": "telegram", "credential_id": cred})


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


class IsolationFromProductionTests(unittest.TestCase):
    WIRING = ("server.py", "worker.py", "healthcheck.py")      # Phase 6B: app wiring only (spec §2)

    def test_root_modules_import_none_of_the_new_packages_except_the_wiring(self):
        """Kept security modules and the retained (unmounted) Spec 011 sources never import
        the new packages; the three wiring files import only ``api`` (the composition root)."""
        offenders = []
        for f in sorted(Path(BACKEND_DIR).glob("*.py")):
            for m in _imports(f):
                if m in ("core", "persistence", "runtime") or (m == "api" and f.name not in self.WIRING):
                    offenders.append(f"{f.name} -> {m}")
        self.assertEqual(offenders, [])

    def test_the_wiring_files_are_wiring_only(self):
        for name in self.WIRING:
            text = (Path(BACKEND_DIR) / name).read_text(encoding="utf-8")
            self.assertLess(len(text.splitlines()), 80, name)
            self.assertNotIn("@app.", text)               # no routes outside api/
            self.assertNotIn("APIRouter", text)

    def test_persistence_depends_only_on_core_and_the_driver(self):
        import sys
        allowed = set(sys.stdlib_module_names) | {"core", "pymongo", "motor", "__future__", "persistence"}
        offenders = [f"{f.name} -> {m}" for f in sorted((Path(BACKEND_DIR) / "persistence").glob("*.py"))
                     for m in _imports(f) if m not in allowed]
        self.assertEqual(offenders, [])

    def test_api_does_not_import_the_legacy_application(self):
        import sys
        # Spec 012 §2 "kept" security modules may be reused; the legacy application may not.
        allowed = set(sys.stdlib_module_names) | {"core", "api", "persistence", "runtime", "fastapi", "pydantic",
                                                   "starlette", "jwt", "bcrypt", "motor", "__future__",
                                                   "password_policy", "client_ip",
                                                   # Phase 6B production wiring (api/production.py)
                                                   "email_delivery", "log_redaction", "resend",
                                                   # Spec 009-011 protections reused by Zendesk (Phase 4E)
                                                   "outbound_guard", "webhook_auth"}
        offenders = [f"{f.name} -> {m}" for f in sorted((Path(BACKEND_DIR) / "api").glob("*.py"))
                     for m in _imports(f) if m not in allowed]
        self.assertEqual(offenders, [])

    def test_runtime_depends_only_on_core(self):
        import sys
        allowed = set(sys.stdlib_module_names) | {"core", "runtime", "__future__"}
        # Only the provider adapters talk HTTP (fixed provider hosts; Phase 4B).
        extra = {"providers.py": {"httpx"}, "telegram.py": {"httpx"}, "discord.py": {"httpx"},
                 # Zendesk reuses the kept Spec 010 outbound guard (never a weaker copy)
                 "zendesk.py": {"httpx", "outbound_guard"},
                 # isolated child process only (resource-limited, network disabled)
                 "pdf_extract_worker.py": {"pypdf"},
                 # real Discord gateway protocol adapter (Phase 4F)
                 "discord_gateway_ws.py": {"websockets"}}
        offenders = [f"{f.name} -> {m}" for f in sorted((Path(BACKEND_DIR) / "runtime").glob("*.py"))
                     for m in _imports(f) if m not in allowed | extra.get(f.name, set())]
        self.assertEqual(offenders, [])

    @unittest.skipUnless((Path(BACKEND_DIR) / "Dockerfile").exists(), "Dockerfile is not inside the test image")
    def test_runtime_image_is_the_spec012_release_candidate(self):
        """Phase 6B: the runtime image ships the Spec 012 packages and only the kept modules
        they import (explicit list); never the retained Spec 011 sources, manage.py or tests."""
        text = (Path(BACKEND_DIR) / "Dockerfile").read_text(encoding="utf-8")
        runtime = text[text.index("AS runtime"):]
        copies = " ".join(re.findall(r"^COPY\s+((?:.|\\\n)+?)$", runtime, flags=re.M)).replace("\\\n", " ")
        for pkg in ("core", "api", "persistence", "runtime"):
            self.assertRegex(copies, rf"\b{pkg} \./{pkg}\b")
        for forbidden in ("*.py", "api_integrations", "manage.py", "tests", "migrate_", "_routes.py"):
            self.assertNotIn(forbidden, copies)
        self.assertRegex(runtime, r"(?m)^USER 10001:10001$")
        self.assertRegex(runtime, r"(?m)^HEALTHCHECK .*healthcheck\.py")
        self.assertIn("io.bitbotpro.source-tree", runtime)
        self.assertIn("--no-proxy-headers", runtime)
        for line in re.findall(r"(?m)^(?:ARG|ENV)\s+(.+)$", runtime):    # no secret baked into the image
            self.assertNotRegex(line, r"(?i)(secret|password|key|token)\s*=")

if __name__ == "__main__":
    unittest.main()
