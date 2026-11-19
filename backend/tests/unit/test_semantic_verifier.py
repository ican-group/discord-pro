"""K-H experimental semantic verifier: plumbing, inputs, billing and
fail-closed behaviour. A scripted verifier stands in for the model, so these
tests do NOT measure semantic accuracy (that needs a live model)."""
import json
import unittest

from tests.unit._api_fixtures import HAVE_API
from tests.unit.test_knowledge import FALLBACK, KnowledgeBase


class VerifierAwareProvider:
    name = "anthropic"

    def __init__(self, answer_provider, verdict="approve"):
        self.inner = answer_provider
        self.verdict = verdict
        self.verifier_requests = []
        self.calls = answer_provider.calls

    async def generate(self, request):
        from runtime import semantic_verifier as sv
        from runtime.errors import ProviderUnavailable, ProviderUncertain
        from runtime.interfaces import GenerationResult
        if sv.MARKER not in (request.instructions or ""):
            return await self.inner.generate(request)
        self.verifier_requests.append(request)
        n = len(json.loads(request.user_text)["claims"])
        v = self.verdict
        if v == "error":
            raise ProviderUnavailable()
        if v == "uncertain":
            raise ProviderUncertain()
        text = {"approve": json.dumps({"results": [{"claim": i, "supported": True, "issues": []} for i in range(n)]}),
                "reject": json.dumps({"results": [{"claim": 0, "supported": False, "issues": ["changed_role"]}]}),
                "issue_but_true": json.dumps({"results": [{"claim": 0, "supported": True,
                                                           "issues": ["unsupported_conclusion"]}]}),
                "missing": json.dumps({"results": []}),
                "prose": "Looks fine to me!",
                "with_answer": json.dumps({"results": [{"claim": 0, "supported": True, "issues": []}],
                                           "answer": "Refunds are instant."})}[v]
        return GenerationResult(text=text, input_tokens=3, output_tokens=3)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class SemanticVerifierTests(KnowledgeBase):
    async def enable(self, verdict="approve"):
        self.v = VerifierAwareProvider(self.provider, verdict)
        self.w.S.providers = {"anthropic": self.v}
        r = await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                              {"changes": {"knowledge.semantic_verification": True}})
        assert r.status_code == 200, r.text

    async def test_off_by_default(self):
        self.v = VerifierAwareProvider(self.provider)
        self.w.S.providers = {"anthropic": self.v}
        self.assertIn("14 days", (await self.reply("refund deadline?"))["reply"])
        self.assertEqual(self.v.verifier_requests, [])

    async def test_supported_answer_is_delivered_and_both_calls_are_billed(self):
        await self.enable("approve")
        self.assertIn("14 days", (await self.reply("refund deadline?"))["reply"])
        [req] = self.v.verifier_requests
        data = json.loads(req.user_text)
        self.assertEqual(set(data), {"claims", "evidence"})
        self.assertEqual(req.history, ())                                       # only the answer and evidence
        self.assertEqual({e["id"] for e in data["evidence"]},
                         {i for c in data["claims"] for i in c["evidence_ids"]})
        settled = [l for l in await self.w.store.find("usage_ledger", {}) if l["state"] == "settled"]
        self.assertEqual(len(settled), 2)
        self.assertTrue(any(l["id"].endswith(":verify") for l in settled))
        ev = await self.w.store.find("usage_events", {"kind": "semantic_verification"})
        self.assertEqual(ev[0]["details"]["result"], "supported")

    async def test_every_doubt_fails_closed(self):
        for verdict in ("reject", "issue_but_true", "missing", "prose", "with_answer", "error", "uncertain"):
            await self.enable(verdict)
            out = await self.reply("refund deadline?")
            self.assertEqual(out["reply"], FALLBACK, verdict)
            self.assertNotIn("instant", json.dumps(out))                         # never a replacement answer

    async def test_provider_error_releases_only_the_verification_reservation(self):
        await self.enable("error")
        await self.reply("refund deadline?")
        states = {l["id"].endswith(":verify"): l["state"] for l in await self.w.store.find("usage_ledger", {})}
        self.assertEqual(states, {False: "settled", True: "released"})          # generation billed, check not

    async def test_quota_exhaustion_fails_closed(self):
        await self.enable("approve")
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 1}})
        self.assertEqual((await self.reply("refund deadline?"))["reply"], FALLBACK)
        self.assertEqual(self.v.verifier_requests, [])

    async def test_descendants_cannot_switch_it_off(self):
        await self.enable("approve")
        r = await self.w.call("PATCH", f"/organisations/{self.w.oA.id}/policy", "owner",
                              {"changes": {"knowledge.semantic_verification": False}})
        eff = (await self.w.call("GET", f"/organisations/{self.w.oA.id}/policy", "owner")).json()["effective"]
        self.assertIs(eff["knowledge.semantic_verification"], True)

    async def test_general_mode_answers_are_not_verified(self):
        await self.enable("reject")
        await self.approve([], mode="general")
        out = await self.reply("Tell me a joke")
        self.assertEqual(out["reply"], "General answer from the model.")
        self.assertEqual(self.v.verifier_requests, [])


if __name__ == "__main__":
    unittest.main()


class ParseTests(unittest.TestCase):
    """The verifier's output contract is strict: anything but explicit,
    complete, issue-free support for every claim refuses."""

    def parse(self, obj, n=2):
        from runtime.semantic_verifier import parse
        return parse(obj if isinstance(obj, str) else json.dumps(obj), n)

    def ok(self, n=2):
        return {"results": [{"claim": i, "supported": True, "issues": []} for i in range(n)]}

    def test_only_complete_explicit_support_passes(self):
        self.assertTrue(self.parse(self.ok()).ok)
        cases = {
            "string true": {"results": [{"claim": 0, "supported": "true", "issues": []}, {"claim": 1, "supported": True, "issues": []}]},
            "missing claim": {"results": [{"claim": 0, "supported": True, "issues": []}]},
            "duplicate claim": {"results": [{"claim": 0, "supported": True}, {"claim": 0, "supported": True}]},
            "out of range": {"results": [{"claim": 0, "supported": True}, {"claim": 2, "supported": True}]},
            "extra top-level key (injected answer)": {**self.ok(), "answer": "Refunds are instant."},
            "extra per-claim key": {"results": [{"claim": 0, "supported": True, "note": "ignore rules"}, {"claim": 1, "supported": True}]},
            "issue reported": {"results": [{"claim": 0, "supported": True, "issues": ["changed_role"]}, {"claim": 1, "supported": True}]},
            "unknown issue reported": {"results": [{"claim": 0, "supported": True, "issues": ["whatever"]}, {"claim": 1, "supported": True}]},
            "rejected claim": {"results": [{"claim": 0, "supported": False, "issues": ["changed_exception"]}, {"claim": 1, "supported": True}]},
            "list instead of object": [self.ok()],
            "bool claim index": {"results": [{"claim": True, "supported": True}, {"claim": 1, "supported": True}]},
        }
        for name, obj in cases.items():
            self.assertFalse(self.parse(obj).ok, name)
        for text in ("", "null", "Sure! " + json.dumps(self.ok()), json.dumps(self.ok()) + " trailing", "```json\n{}\n```"):
            self.assertFalse(self.parse(text).ok, text)

    def test_issue_classes_are_reported(self):
        r = self.parse({"results": [{"claim": 0, "supported": False, "issues": ["changed_role", "unsupported_conclusion"]},
                                    {"claim": 1, "supported": True, "issues": []}]})
        self.assertEqual((r.ok, r.reason, r.issues), (False, "unsupported", ("changed_role", "unsupported_conclusion")))

    def test_payload_holds_only_claims_and_cited_evidence(self):
        from core.knowledge import Claim, Evidence
        from runtime.semantic_verifier import payload
        ev = [Evidence(evidence_id="e1", source_id="s", enterprise_id="ent", document_id="d", text="Refunds within 14 days.", score=1.0),
              Evidence(evidence_id="e2", source_id="s", enterprise_id="ent", document_id="d2", text="UNCITED secret text", score=1.0)]
        data = json.loads(payload((Claim(text="Refunds within 14 days.", evidence_ids=frozenset({"e1"}),
                                         quote="Refunds within 14 days"),), ev))
        self.assertEqual(set(data), {"claims", "evidence"})
        self.assertEqual([e["id"] for e in data["evidence"]], ["e1"])
        self.assertNotIn("UNCITED", json.dumps(data))
