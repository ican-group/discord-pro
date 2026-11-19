"""Phase 4F knowledge evaluation over a synthetic corpus (Part C).

Runs every case through the real API, ingestion, retrieval, gate and
validator with a scripted model, once with lexical validation only (layer 1)
and once with sentence alignment (layers 1+2). Reports *measured* counts
(written to ``KNOWLEDGE_EVAL_REPORT`` if set); asserts only invariants, never
a target rate. These numbers describe this synthetic corpus, not production
behaviour with a live model."""
import json
import os
import unittest

from tests.unit._api_fixtures import HAVE_API, World
from tests.unit import knowledge_holdout_corpus as HOLD
from tests.unit.knowledge_eval_corpus import CASES, DOCUMENTS, EXPIRED, REFUSALS, RESTRICTED

FALLBACK = "Please contact our support team."


class ScriptedProvider:
    """Answers with a given claim, citing the evidence that contains the
    accurate quote (as a well-behaved model would)."""
    name = "anthropic"

    def __init__(self):
        self.answer = None                  # (claim_text, quote)
        self.calls = []

    async def generate(self, request):
        from core.knowledge import Claim, normalise_text
        from runtime.interfaces import GenerationResult
        self.calls.append(request)
        if not request.structured_claims_required or not request.evidence or self.answer is None:
            return GenerationResult(claims=())
        text, quote = self.answer
        ev = next((e for e in request.evidence if normalise_text(quote) in normalise_text(e.text)),
                  request.evidence[0])
        return GenerationResult(claims=(Claim(text, frozenset({ev.evidence_id}), quote=quote),))


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class KnowledgeEvaluation(unittest.IsolatedAsyncioTestCase):
    DOCS, EXP, RESTR = DOCUMENTS, EXPIRED, RESTRICTED

    async def asyncSetUp(self):
        import httpx
        self.w = await World().build()
        self.p = ScriptedProvider()
        self.w.S.providers = {"anthropic": self.p}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        src = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/knowledge/sources", "admin",
                                 {"name": "Company documents"})).json()["id"]
        o_r = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/organisations", "owner",
                                 {"name": "Executive office"})).json()["id"]
        self.doc_ids = {}
        for name, text in self.DOCS.items():
            h = {**self.w.headers("admin"), "Content-Type": "text/plain", "X-Requested-With": "BitBotPro"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
                r = await c.put(f"/api/knowledge/sources/{src}/documents/{name}", content=text.encode(), headers=h)
            assert r.status_code == 200, r.text
            self.doc_ids[name] = r.json()["id"]
        for name, until in self.EXP.items():
            r = await self.w.call("PUT", f"/knowledge/documents/{self.doc_ids[name]}/validity", "admin",
                                  {"valid_until": until})
            assert r.status_code == 200, r.text
        for name in self.RESTR:
            r = await self.w.call("PUT", f"/knowledge/documents/{self.doc_ids[name]}/acl", "owner",
                                  {"organisations": [o_r]})
            assert r.status_code == 200, r.text
        self.name_of = {v: k for k, v in self.doc_ids.items()}
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.allowed_sources": [src],
                                       "knowledge.minimum_mode": "strict_knowledge_only"}})
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"knowledge.approved_sources": [src], "knowledge.mode": "strict_knowledge_only",
                                       "knowledge.insufficient_fallback": FALLBACK}})
        self.src = src

    async def ask(self, question):
        import httpx
        self.n = getattr(self, "n", 0) + 1                  # distinct synthetic clients (per-IP throttle unchanged)
        client = (f"198.51.{self.n // 250}.{self.n % 250 + 1}", 40000)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app, client=client),
                                     base_url="http://test") as c:
            r = await c.post(f"/api/public/instances/{self.w.iA_web.channel.public_id}/chat",
                             json={"message": question}, headers={"Origin": "https://shop.example"})
        assert r.status_code == 200, r.text
        return r.json()

    async def run_suite(self, alignment: bool, cases=None):
        from core import knowledge
        cases = cases if cases is not None else CASES
        knowledge.ALIGNMENT_CHECKS = alignment
        res = {"answerable": len(cases), "correct_refused": 0, "correct_accepted": 0, "citation_correct": 0,
               "unsupported_total": 0, "unsupported_accepted": 0, "by_category": {}, "accepted_examples": [],
               "refused_correct_examples": []}
        for question, doc, gold, wrong in cases:
            self.p.answer = (gold + ".", gold)
            out = await self.ask(question)
            if out["reply"] == FALLBACK:
                res["correct_refused"] += 1
                res["refused_correct_examples"].append(question)
            else:
                res["correct_accepted"] += 1
                ev = self.p.calls[-1].evidence
                cited = {self.name_of.get(e.document_id) for e in ev if gold.lower() in e.text.lower()}
                res["citation_correct"] += int(doc in cited and out.get("citations") == [self.src])
            for category, claim in wrong.items():
                self.p.answer = (claim, gold)
                out = await self.ask(question)
                cat = res["by_category"].setdefault(category, {"total": 0, "accepted": 0})
                cat["total"] += 1
                res["unsupported_total"] += 1
                if out["reply"] != FALLBACK:
                    cat["accepted"] += 1
                    res["unsupported_accepted"] += 1
                    res["accepted_examples"].append({"category": category, "question": question, "claim": claim})
        knowledge.ALIGNMENT_CHECKS = True
        return res

    async def retrieval_metrics(self):
        """Recall/precision of the evidence actually given to the model, and the
        score distribution used to calibrate the threshold (K-B)."""
        from core.hierarchy import Level
        from core.knowledge import plan_for
        from runtime.knowledge_store import StoreRetriever

        class P:
            min_evidence_score = 0.0
        chain = await self.w.S.hierarchy.load_chain(Level.BOT_INSTANCE, self.w.iA_web.id)
        retr = StoreRetriever(self.w.store, clock=self.w.S.clock)
        hits = relevant_returned = returned = 0
        gold_scores, other_scores = [], []
        for question, doc, gold, _ in CASES:
            r = await retr.retrieve(question, chain=chain, plan=P(), authorised=frozenset({self.src}))
            for e in r.evidence:
                (gold_scores if self.name_of.get(e.document_id) == doc else other_scores).append(e.score)
            usable = [e for e in r.evidence if e.score >= 0.5]
            returned += len(usable)
            rel = [e for e in usable if self.name_of.get(e.document_id) == doc]
            relevant_returned += len(rel)
            hits += int(bool(rel))
        thresholds = {}
        for t in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            thresholds[str(t)] = {"gold_kept": sum(s >= t for s in gold_scores),
                                  "gold_total": len(gold_scores),
                                  "other_kept": sum(s >= t for s in other_scores),
                                  "other_total": len(other_scores)}
        return {"recall_at_threshold_0.5": f"{hits}/{len(CASES)}",
                "precision_at_threshold_0.5": f"{relevant_returned}/{returned}", "thresholds": thresholds}

    async def refusal_metrics(self):
        out = {}
        self.p.answer = None
        for question, reason in REFUSALS:
            calls = len(self.p.calls)
            reply = (await self.ask(question))["reply"]
            used = [self.name_of.get(e.document_id) for c in self.p.calls[calls:] for e in c.evidence]
            out[question] = {"expected": reason, "refused": reply == FALLBACK,
                             "restricted_evidence_seen": any(n in RESTRICTED for n in used)}
        # expired version never used as evidence
        calls = len(self.p.calls)
        self.p.answer = ("Customers may request a refund within 30 days of delivery.",
                         "Customers may request a refund within 30 days of delivery")
        await self.ask("How many days do customers have to request a refund?")
        out["expired_policy_used"] = any(self.name_of.get(e.document_id) in EXPIRED
                                         for c in self.p.calls[calls:] for e in c.evidence)
        return out

    async def test_evaluation(self):
        layer1 = await self.run_suite(alignment=False)
        layer2 = await self.run_suite(alignment=True)
        retrieval = await self.retrieval_metrics()
        refusals = await self.refusal_metrics()
        report = {"layer1_lexical_only": layer1, "layer1_plus_sentence_alignment": layer2,
                  "retrieval": retrieval, "refusals": refusals}
        path = os.environ.get("KNOWLEDGE_EVAL_REPORT")
        if path:
            with open(path, "w") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
        # invariants only (no target rates)
        self.assertLessEqual(layer2["unsupported_accepted"], layer1["unsupported_accepted"])
        self.assertTrue(all(not v["restricted_evidence_seen"] for k, v in refusals.items() if isinstance(v, dict)))
        self.assertFalse(refusals["expired_policy_used"])
        self.assertTrue(refusals["Is standard shipping free on all orders?"]["refused"])



@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class HoldoutEvaluation(KnowledgeEvaluation):
    """Deterministic layers on the HOLDOUT set (not used for tuning). K-H
    rates are not measured here: they need a live model (Phase 6)."""
    DOCS, EXP, RESTR = HOLD.DOCUMENTS, {}, set()

    async def test_evaluation(self):                       # replaces the parent's corpus run
        layer1 = await self.run_suite(alignment=False, cases=HOLD.CASES)
        layer2 = await self.run_suite(alignment=True, cases=HOLD.CASES)
        refusals = {}
        self.p.answer = None
        for q, reason in HOLD.REFUSALS:
            refusals[q] = {"expected": reason, "refused": (await self.ask(q))["reply"] == FALLBACK}
        report = {"holdout_layer1": layer1, "holdout_layer1_plus_2": layer2, "holdout_refusals": refusals}
        path = os.environ.get("KNOWLEDGE_HOLDOUT_REPORT")
        if path:
            with open(path, "w") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
        self.assertLessEqual(layer2["unsupported_accepted"], layer1["unsupported_accepted"])
        self.assertTrue(all(v["refused"] for v in refusals.values()))


if __name__ == "__main__":
    unittest.main()
