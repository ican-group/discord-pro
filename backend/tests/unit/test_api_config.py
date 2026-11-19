"""Configuration inheritance and policy through the API (spec 012 §4, §19.2)."""
import unittest
from unittest import mock

from tests.unit._api_fixtures import HAVE_API, World

STRICT = "strict_knowledge_only"


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class ConfigApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = await World().build()
        self.E, self.O, self.B, self.I = self.w.eA.id, self.w.oA.id, self.w.bA.id, self.w.iA_web.id

    async def config(self, path, who="owner"):
        r = await self.w.call("GET", f"{path}/config", who)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def patch(self, path, changes, who="owner", kind="config"):
        return await self.w.call("PATCH", f"{path}/{kind}", who, {"changes": changes})

    async def test_inheritance_provenance_and_removal(self):
        r = await self.patch(f"/enterprises/{self.E}", {"behaviour.language": "de"})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = await self.config(f"/instances/{self.I}")
        self.assertEqual(cfg["values"]["behaviour.language"], "de")
        self.assertEqual(cfg["sources"]["behaviour.language"], {"kind": "override", "level": "enterprise",
                                                                "object_id": self.E})
        await self.patch(f"/bots/{self.B}", {"behaviour.language": "fr"})
        self.assertEqual((await self.config(f"/instances/{self.I}"))["values"]["behaviour.language"], "fr")
        await self.patch(f"/bots/{self.B}", {"behaviour.language": None})        # remove the override
        cfg = await self.config(f"/instances/{self.I}")
        self.assertEqual(cfg["values"]["behaviour.language"], "de")
        bot = (await self.w.call("GET", f"/bots/{self.B}", "owner")).json()
        self.assertEqual(bot["overrides"], {})                                    # nothing copied down

    async def test_strict_minimum_through_policy_and_config_writes(self):
        r = await self.patch(f"/enterprises/{self.E}", {"knowledge.minimum_mode": STRICT}, kind="policy")
        self.assertEqual(r.status_code, 200, r.text)
        for path in (f"/organisations/{self.O}", f"/bots/{self.B}", f"/instances/{self.I}"):
            for weaker in ("general", "knowledge_preferred"):
                with self.subTest(path=path, mode=weaker):
                    r = await self.patch(path, {"knowledge.mode": weaker})
                    self.assertEqual(r.status_code, 422)
        r = await self.patch(f"/organisations/{self.O}", {"knowledge.minimum_mode": "general"}, kind="policy")
        self.assertEqual(r.status_code, 422)
        r = await self.patch(f"/organisations/{self.O}", {"knowledge.general_fallback_allowed": True}, kind="policy")
        self.assertEqual(r.status_code, 422)
        cfg = await self.config(f"/instances/{self.I}")
        self.assertEqual(cfg["values"]["knowledge.mode"], STRICT)

    async def test_policy_writes_only_tighten(self):
        r = await self.patch(f"/enterprises/{self.E}", {"limits.max_end_user_msgs_per_minute": 500}, kind="policy")
        self.assertEqual(r.status_code, 422)                                      # above the platform maximum
        r = await self.patch(f"/enterprises/{self.E}", {"limits.max_end_user_msgs_per_minute": 30}, kind="policy")
        self.assertEqual(r.status_code, 200)
        r = await self.patch(f"/organisations/{self.O}", {"limits.max_end_user_msgs_per_minute": 40}, kind="policy")
        self.assertEqual(r.status_code, 422)
        r = await self.patch(f"/bots/{self.B}", {"limits.end_user_msgs_per_minute": 31})
        self.assertEqual(r.status_code, 422)
        self.assertEqual((await self.w.call("GET", f"/bots/{self.B}/policy", "owner")).status_code, 404)

    async def test_per_property_role_and_unknown_keys(self):
        r = await self.patch(f"/bots/{self.B}", {"behaviour.persona": "Helpful"}, who="editor")
        self.assertEqual(r.status_code, 200)
        r = await self.patch(f"/bots/{self.B}", {"knowledge.mode": "knowledge_preferred"}, who="editor")
        self.assertEqual(r.status_code, 403)                                      # admin-only property
        r = await self.patch(f"/bots/{self.B}", {"no.such.key": 1})
        self.assertEqual(r.status_code, 422)
        r = await self.patch(f"/bots/{self.B}", {"behaviour.language": "de", "limits.end_user_msgs_per_minute": 0})
        self.assertEqual(r.status_code, 422)                                      # all-or-nothing
        self.assertEqual((await self.w.call("GET", f"/bots/{self.B}", "owner")).json()["overrides"],
                         {"behaviour.persona": "Helpful"})

    async def test_cross_tenant_credential_reference_is_refused(self):
        r = await self.patch(f"/bots/{self.B}", {"ai.credential_id": self.w.cB_tg.id})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], "The selected credential is not available here.")
        r = await self.patch(f"/bots/{self.B}", {"ai.credential_id": self.w.cA_ai.id})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = r.json()
        self.assertEqual(cfg["credentials"]["ai.credential_id"]["id"], self.w.cA_ai.id)
        self.assertNotIn("secret_enc", str(cfg))

    async def test_api_reads_go_through_the_single_resolver(self):
        import core.resolver as resolver_module
        real = resolver_module.resolve
        calls = []

        def spy(*a, **kw):
            calls.append(a[0].target.id)
            return real(*a, **kw)
        with mock.patch.object(resolver_module, "resolve", spy):
            await self.config(f"/instances/{self.I}")
        self.assertIn(self.I, calls)

    async def test_stale_version_is_a_conflict_not_a_lost_update(self):
        from core.hierarchy import Level
        chain = await self.w.S.hierarchy.load_chain(Level.BOT, self.B)
        await self.w.S.hierarchy.set_overrides(Level.BOT, self.B, {"behaviour.language": "de"},
                                               expect_version=chain.bot.version)
        from core.hierarchy import ConcurrentModification
        with self.assertRaises(ConcurrentModification):
            await self.w.S.hierarchy.set_overrides(Level.BOT, self.B, {"behaviour.language": "fr"},
                                                   expect_version=chain.bot.version)


if __name__ == "__main__":
    unittest.main()
