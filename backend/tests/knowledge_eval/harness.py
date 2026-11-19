"""Reproducible knowledge evaluation harness (Phase 6A, Part G).

Runs the Phase 4F corpus and the independent holdout set through the real
ingestion, retrieval, strict gate, validators and (optionally) the K-H
verifier, in an in-memory Spec 012 World. Three modes:

* ``deterministic`` — a scripted model returns the gold claim or each wrong
  claim; measures what the deterministic layers accept (no model involved).
* ``verifier`` — K-H only: the verifier model judges every gold claim and
  every wrong claim against the cited evidence. Measures K-H's own
  false-acceptance and false-refusal rates.
* ``end_to_end`` — the model answers every question through the strict
  pipeline. Measures refusals of answerable questions, answers to must-refuse
  questions, citation correctness and a key-fact heuristic.

Every mode reports provider errors, latency (p50/p95/max) and token usage
(cost if prices are given). Policy is fixed by the harness and written into
the report: strict mode, the configured evidence threshold, K-H on/off — it
is never tuned per run to improve results.

LIVE PROVIDERS. ``--provider anthropic|openai`` calls a real AI provider and
is refused unless ALL of: environment ``KNOWLEDGE_EVAL_LIVE=1``, the flag
``--i-understand-this-calls-a-live-provider``, and the API key in the
environment variable ``KNOWLEDGE_EVAL_API_KEY`` (never printed or written to
the report). Phase 6A runs only ``--provider scripted``; scripted results are
plumbing checks, NOT accuracy measurements.

    python -m tests.knowledge_eval.harness --mode verifier --set holdout --provider scripted --out report.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional

FALLBACK = "Please contact our support team."
LIVE_FLAG = "--i-understand-this-calls-a-live-provider"


def corpus(name: str):
    if name == "corpus":
        from tests.unit import knowledge_eval_corpus as C
        return {"documents": C.DOCUMENTS, "expired": C.EXPIRED, "restricted": C.RESTRICTED, "cases": C.CASES,
                "refusals": C.REFUSALS}
    from tests.unit import knowledge_holdout_corpus as H
    return {"documents": H.DOCUMENTS, "expired": {}, "restricted": set(), "cases": H.CASES, "refusals": H.REFUSALS}


def corpus_digest(c) -> str:
    return hashlib.sha256(json.dumps({k: (sorted(v) if isinstance(v, set) else v) for k, v in c.items()},
                                     sort_keys=True, default=str).encode()).hexdigest()[:16]


class Meter:
    """Latency, tokens and errors of every provider call (wraps a provider)."""

    def __init__(self, inner):
        self.inner, self.name = inner, getattr(inner, "name", "anthropic")
        self.latencies: List[float] = []
        self.tokens_in = self.tokens_out = 0
        self.errors: Dict[str, int] = {}

    async def generate(self, request):
        t = time.perf_counter()
        try:
            out = await self.inner.generate(request)
        except Exception as exc:                     # counted, then re-raised: the pipeline fails closed
            self.errors[type(exc).__name__] = self.errors.get(type(exc).__name__, 0) + 1
            raise
        finally:
            self.latencies.append(time.perf_counter() - t)
        self.tokens_in += int(out.input_tokens or 0)
        self.tokens_out += int(out.output_tokens or 0)
        return out

    def summary(self, price_in: Optional[float], price_out: Optional[float]) -> Dict[str, Any]:
        lat = sorted(self.latencies)
        pct = (lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))] * 1000, 1)) if lat else (lambda q: None)
        cost = None
        if price_in is not None and price_out is not None:
            cost = round(self.tokens_in / 1e6 * price_in + self.tokens_out / 1e6 * price_out, 6)
        return {"calls": len(lat), "errors": self.errors, "latency_ms": {"p50": pct(0.5), "p95": pct(0.95),
                "max": round(lat[-1] * 1000, 1) if lat else None, "mean": round(statistics.mean(lat) * 1000, 1) if lat else None},
                "tokens": {"input": self.tokens_in, "output": self.tokens_out}, "estimated_cost": cost}


class ScriptedAnswerer:
    """Scripted model for ``deterministic``/dry runs: returns the configured claim
    citing the evidence that contains its quote. Also a scripted K-H verifier."""
    name = "anthropic"

    def __init__(self, verifier_policy: str = "approve"):
        self.answer = None
        self.verifier_policy = verifier_policy

    async def generate(self, request):
        from core.knowledge import Claim, normalise_text
        from runtime import semantic_verifier as sv
        from runtime.interfaces import GenerationResult
        if sv.MARKER in (request.instructions or ""):
            n = len(json.loads(request.user_text)["claims"])
            ok = self.verifier_policy == "approve"
            return GenerationResult(text=json.dumps({"results": [{"claim": i, "supported": ok, "issues": []}
                                                                 for i in range(n)]}), input_tokens=50, output_tokens=20)
        if not request.structured_claims_required or not request.evidence or self.answer is None:
            return GenerationResult(claims=(), input_tokens=40, output_tokens=5)
        text, quote = self.answer
        ev = next((e for e in request.evidence if normalise_text(quote) in normalise_text(e.text)), request.evidence[0])
        return GenerationResult(claims=(Claim(text, frozenset({ev.evidence_id}), quote=quote),), input_tokens=60,
                                output_tokens=15)


def live_provider(name: str, argv_flags: List[str]):
    if os.environ.get("KNOWLEDGE_EVAL_LIVE") != "1" or LIVE_FLAG not in argv_flags or \
            not os.environ.get("KNOWLEDGE_EVAL_API_KEY"):
        raise SystemExit("Live providers need KNOWLEDGE_EVAL_LIVE=1, " + LIVE_FLAG +
                         " and KNOWLEDGE_EVAL_API_KEY. Refusing.")
    from runtime.providers import build_providers
    return build_providers()[name]


class Harness:
    def __init__(self, *, set_name: str, provider, kh: bool, model: str):
        self.c = corpus(set_name)
        self.set_name, self.provider, self.kh, self.model = set_name, provider, kh, model

    async def setup(self, api_key: str):
        import httpx
        from core.credentials import Provider, credential_to_doc, new_credential
        from core.hierarchy import Level, new_id
        from tests.unit._api_fixtures import World
        self.w = w = await World().build()
        echain = await w.S.hierarchy.load_chain(Level.ENTERPRISE, w.eA.id)
        cred = new_credential(credential_id=new_id(), chain=echain, provider=Provider(self.provider.name),
                              name="Evaluation key", secret=api_key, box=w.S.box, actor_id=w.users["root"].id,
                              now=w.S.clock())
        await w.store.insert("credentials", credential_to_doc(cred, 1))
        w.S.providers = {self.provider.name: self.provider}
        src = (await w.call("POST", f"/enterprises/{w.eA.id}/knowledge/sources", "admin", {"name": "Evaluation"})).json()["id"]
        other_org = (await w.call("POST", f"/enterprises/{w.eA.id}/organisations", "owner", {"name": "Restricted"})).json()["id"]
        self.doc_ids = {}
        for name, text in self.c["documents"].items():
            h = {**w.headers("admin"), "Content-Type": "text/plain", "X-Requested-With": "BitBotPro"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as cl:
                r = await cl.put(f"/api/knowledge/sources/{src}/documents/{name}", content=text.encode(), headers=h)
            assert r.status_code == 200, r.text
            self.doc_ids[name] = r.json()["id"]
        for name, until in self.c["expired"].items():
            await w.call("PUT", f"/knowledge/documents/{self.doc_ids[name]}/validity", "admin", {"valid_until": until})
        for name in self.c["restricted"]:
            await w.call("PUT", f"/knowledge/documents/{self.doc_ids[name]}/acl", "owner", {"organisations": [other_org]})
        self.name_of = {v: k for k, v in self.doc_ids.items()}
        self.policy = {"knowledge.allowed_sources": [src], "knowledge.minimum_mode": "strict_knowledge_only",
                       "knowledge.semantic_verification": bool(self.kh)}
        await w.call("PATCH", f"/enterprises/{w.eA.id}/policy", "owner", {"changes": self.policy})
        self.config = {"ai.credential_id": cred.id, "ai.provider": self.provider.name, "ai.model": self.model,
                       "knowledge.approved_sources": [src], "knowledge.mode": "strict_knowledge_only",
                       "knowledge.insufficient_fallback": FALLBACK}
        r = await w.call("PATCH", f"/bots/{w.bA.id}/config", "owner", {"changes": self.config})
        assert r.status_code == 200, r.text
        self.src = src
        self.n = 0

    async def ask(self, question: str) -> Dict[str, Any]:
        import httpx
        self.n += 1
        client = (f"198.51.{self.n // 250}.{self.n % 250 + 1}", 40000)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app, client=client),
                                     base_url="http://test") as c:
            r = await c.post(f"/api/public/instances/{self.w.iA_web.channel.public_id}/chat",
                             json={"message": question}, headers={"Origin": "https://shop.example"})
        return r.json() if r.status_code == 200 else {"reply": FALLBACK, "http_status": r.status_code}

    # ── modes ──
    async def deterministic(self, inner: ScriptedAnswerer) -> Dict[str, Any]:
        res = {"answerable": 0, "correct_refused": 0, "citation_correct": 0, "unsupported_total": 0,
               "unsupported_accepted": 0, "by_category": {}}
        for question, doc, gold, wrong in self.c["cases"]:
            res["answerable"] += 1
            inner.answer = (gold + ".", gold)
            out = await self.ask(question)
            if out["reply"] == FALLBACK:
                res["correct_refused"] += 1
            else:
                res["citation_correct"] += int(out.get("citations") == [self.src])
            for cat, claim in wrong.items():
                inner.answer = (claim, gold)
                out = await self.ask(question)
                b = res["by_category"].setdefault(cat, {"total": 0, "accepted": 0})
                b["total"] += 1
                res["unsupported_total"] += 1
                if out["reply"] != FALLBACK:
                    b["accepted"] += 1
                    res["unsupported_accepted"] += 1
        res["must_refuse"] = await self.must_refuse(inner)
        return res

    async def must_refuse(self, inner=None) -> Dict[str, Any]:
        out = {"total": 0, "refused": 0, "answered_examples": []}
        for q, reason in self.c["refusals"]:
            if inner is not None:
                inner.answer = None
            r = await self.ask(q)
            out["total"] += 1
            if r["reply"] == FALLBACK:
                out["refused"] += 1
            else:
                out["answered_examples"].append({"question": q, "expected": reason})
        return out

    async def verifier(self) -> Dict[str, Any]:
        """K-H judges claims directly (no answer generation)."""
        from core.knowledge import Claim, normalise_text
        from runtime import semantic_verifier as sv
        from runtime.interfaces import GenerationRequest
        from runtime.knowledge_store import StoreRetriever
        from core.hierarchy import Level
        from runtime.interfaces import Secret
        chain = await self.w.S.hierarchy.load_chain(Level.BOT_INSTANCE, self.w.iA_web.id)
        retr = StoreRetriever(self.w.store, clock=self.w.S.clock)

        class P:
            min_evidence_score = 0.0
        res = {"gold_total": 0, "gold_refused": 0, "wrong_total": 0, "wrong_accepted": 0, "malformed": 0,
               "provider_errors": 0, "by_category": {}}
        for question, doc, gold, wrong in self.c["cases"]:
            r = await retr.retrieve(question, chain=chain, plan=P(), authorised=frozenset({self.src}))
            ev = next((e for e in r.evidence if normalise_text(gold) in normalise_text(e.text)), None)
            if ev is None:
                continue
            for label, text in [("gold", gold + ".")] + list(wrong.items()):
                claims = (Claim(text, frozenset({ev.evidence_id}), quote=gold),)
                req = GenerationRequest(provider=self.provider.name, model=self.model, api_key=Secret(self.api_key),
                                        persona="", instructions=sv.INSTRUCTIONS, tone_instructions="", language="en",
                                        user_text=sv.payload(claims, [ev]), knowledge_mode="verification",
                                        max_output_tokens=512)
                try:
                    out = await self.provider.generate(req)
                    check = sv.parse(out.text, 1)
                except Exception:
                    res["provider_errors"] += 1
                    check = sv.VerificationResult(False, "provider_error")
                if check.reason == "malformed":
                    res["malformed"] += 1
                if label == "gold":
                    res["gold_total"] += 1
                    res["gold_refused"] += int(not check.ok)
                else:
                    b = res["by_category"].setdefault(label, {"total": 0, "accepted": 0})
                    b["total"] += 1
                    res["wrong_total"] += 1
                    b["accepted"] += int(check.ok)
                    res["wrong_accepted"] += int(check.ok)
        res["false_acceptance_rate"] = round(res["wrong_accepted"] / res["wrong_total"], 4) if res["wrong_total"] else None
        res["false_refusal_rate"] = round(res["gold_refused"] / res["gold_total"], 4) if res["gold_total"] else None
        return res

    async def end_to_end(self) -> Dict[str, Any]:
        """The model answers. 'key_fact_present' is a lexical heuristic (numbers
        and the gold quote's rare words), not a judgement of correctness."""
        import re
        res = {"answerable": 0, "refused": 0, "answered": 0, "citation_correct": 0, "key_fact_present": 0}
        for question, doc, gold, _ in self.c["cases"]:
            res["answerable"] += 1
            out = await self.ask(question)
            if out["reply"] == FALLBACK:
                res["refused"] += 1
                continue
            res["answered"] += 1
            res["citation_correct"] += int(out.get("citations") == [self.src])
            facts = set(re.findall(r"[0-9]+(?:[.,][0-9]+)?%?", gold)) or {w for w in gold.lower().split() if len(w) > 6}
            res["key_fact_present"] += int(all(f.lower() in out["reply"].lower() for f in facts))
        res["false_refusal_rate"] = round(res["refused"] / res["answerable"], 4) if res["answerable"] else None
        res["must_refuse"] = await self.must_refuse()
        mr = res["must_refuse"]
        res["false_acceptance_rate_must_refuse"] = round(1 - mr["refused"] / mr["total"], 4) if mr["total"] else None
        return res


async def run(args, argv) -> Dict[str, Any]:
    from core import knowledge
    scripted = args.provider == "scripted"
    inner = ScriptedAnswerer(args.scripted_verifier) if scripted else live_provider(args.provider, argv)
    meter = Meter(inner)
    h = Harness(set_name=args.set, provider=meter, kh=args.kh or args.mode == "verifier", model=args.model)
    h.api_key = os.environ.get("KNOWLEDGE_EVAL_API_KEY", "") if not scripted else "sk-scripted-0000000000000000"
    await h.setup(h.api_key)
    knowledge.ALIGNMENT_CHECKS = True
    if args.mode == "deterministic":
        if not scripted:
            raise SystemExit("deterministic mode uses the scripted model only")
        metrics = await h.deterministic(inner)
    elif args.mode == "verifier":
        metrics = await h.verifier()
    else:
        metrics = await h.end_to_end()
    c = corpus(args.set)
    return {"mode": args.mode, "set": args.set, "corpus_digest": corpus_digest(c), "provider": args.provider,
            "model": args.model, "measurement": "LIVE" if not scripted else "SCRIPTED (plumbing only, not accuracy)",
            "policy": {**h.policy, "knowledge.allowed_sources": "<evaluation source>"},
            "config": {k: v for k, v in h.config.items() if k not in ("ai.credential_id", "knowledge.approved_sources")},
            "metrics": metrics, "provider_calls": meter.summary(args.price_in, args.price_out)}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(description="BitBotPro knowledge evaluation harness")
    p.add_argument("--mode", choices=("deterministic", "verifier", "end_to_end"), required=True)
    p.add_argument("--set", choices=("corpus", "holdout"), required=True)
    p.add_argument("--provider", choices=("scripted", "anthropic", "openai"), default="scripted")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--kh", action="store_true", help="enable K-H in end_to_end mode")
    p.add_argument("--scripted-verifier", choices=("approve", "reject"), default="approve")
    p.add_argument("--price-in", type=float, default=None, help="USD per million input tokens")
    p.add_argument("--price-out", type=float, default=None, help="USD per million output tokens")
    p.add_argument("--out", default=None)
    p.add_argument(LIVE_FLAG, action="store_true", dest="live_ack")
    args = p.parse_args(argv)
    report = asyncio.run(run(args, argv))
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
