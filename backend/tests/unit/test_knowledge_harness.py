"""The Phase 6A evaluation harness runs reproducibly in scripted mode and
refuses live providers unless explicitly enabled (no live call in Phase 6A)."""
import os
import unittest
from unittest import mock

from tests.unit._api_fixtures import HAVE_API


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def run_mode(self, *argv):
        import argparse
        from tests.knowledge_eval import harness as p
        ns = argparse.Namespace(mode=argv[0], set=argv[1], provider="scripted", model="claude-sonnet-4-6", kh=False,
                                scripted_verifier=argv[2] if len(argv) > 2 else "approve", price_in=1.0, price_out=5.0,
                                out=None, live_ack=False)
        return await p.run(ns, [])

    async def test_deterministic_holdout_matches_the_phase5_measurement(self):
        r = await self.run_mode("deterministic", "holdout")
        m = r["metrics"]
        self.assertEqual((m["unsupported_accepted"], m["unsupported_total"], m["correct_refused"], m["answerable"]), (9, 25, 0, 12))
        self.assertEqual((m["must_refuse"]["refused"], m["must_refuse"]["total"]), (2, 2))
        self.assertTrue(r["measurement"].startswith("SCRIPTED"))
        self.assertEqual(r["policy"]["knowledge.minimum_mode"], "strict_knowledge_only")
        self.assertGreater(r["provider_calls"]["calls"], 0)
        self.assertIsNotNone(r["provider_calls"]["estimated_cost"])

    async def test_verifier_mode_reports_far_and_frr(self):
        approve = (await self.run_mode("verifier", "corpus", "approve"))["metrics"]
        reject = (await self.run_mode("verifier", "corpus", "reject"))["metrics"]
        self.assertEqual((approve["false_acceptance_rate"], approve["false_refusal_rate"]), (1.0, 0.0))
        self.assertEqual((reject["false_acceptance_rate"], reject["false_refusal_rate"]), (0.0, 1.0))
        self.assertEqual(approve["wrong_total"], 40)
        self.assertEqual(approve["gold_total"], 16)

    async def test_end_to_end_mode_runs(self):
        m = (await self.run_mode("end_to_end", "holdout"))["metrics"]
        self.assertEqual(m["answerable"], 12)
        self.assertEqual(m["must_refuse"]["total"], 2)

    def test_live_provider_is_refused_without_every_gate(self):
        from tests.knowledge_eval import harness
        for env, argv in (({}, [harness.LIVE_FLAG]), ({"KNOWLEDGE_EVAL_LIVE": "1"}, []),
                          ({"KNOWLEDGE_EVAL_LIVE": "1"}, [harness.LIVE_FLAG])):
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    harness.live_provider("anthropic", argv)


if __name__ == "__main__":
    unittest.main()
