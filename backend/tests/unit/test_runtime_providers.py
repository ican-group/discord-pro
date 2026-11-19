"""AI provider adapters (Phase 4B): request shape, response validation,
structured claims, bounded retries, safe error mapping. ``httpx.MockTransport``
only; no live provider call."""
import asyncio
import importlib.util
import json
import unittest

from tests.unit._support import BACKEND_DIR  # noqa: F401

HAVE_HTTPX = importlib.util.find_spec("httpx") is not None
KEY = "not-a-real-provider-key-abcdefghijklmnop"


def request(**kw):
    from core.knowledge import Evidence
    from runtime.interfaces import GenerationRequest, Secret
    base = dict(provider="anthropic", model="claude-sonnet-4-6", api_key=Secret(KEY), persona="You are helpful.",
                instructions="Be brief.", tone_instructions="", language="en", user_text="Refund policy?",
                knowledge_mode="general", history=(("user", "Hi"), ("assistant", "Hello!")))
    base.update(kw)
    if base.get("structured_claims_required") and "evidence" not in kw:
        base["evidence"] = (Evidence("ev-1", "src-1", "ent-1", 0.9, "Refunds take 14 days."),)
    return GenerationRequest(**base)


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed (runs in the Docker test target)")
class ProviderTests(unittest.TestCase):
    def provider(self, cls_name, handler, **kw):
        import httpx
        from runtime import providers
        sleeps = []

        async def no_sleep(seconds):
            sleeps.append(seconds)
        p = getattr(providers, cls_name)(transport=httpx.MockTransport(handler), sleep=no_sleep, **kw)
        p.sleeps = sleeps
        return p

    def go(self, coro):
        return asyncio.run(coro)

    def test_anthropic_request_and_response(self):
        import httpx
        seen = {}

        def handler(req: httpx.Request):
            seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Refunds take 14 days."}],
                                             "usage": {"input_tokens": 12, "output_tokens": 7}})
        result = self.go(self.provider("AnthropicProvider", handler).generate(request()))
        self.assertEqual((result.text, result.input_tokens, result.output_tokens), ("Refunds take 14 days.", 12, 7))
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], KEY)
        self.assertEqual(seen["body"]["model"], "claude-sonnet-4-6")
        self.assertIn("You are helpful.", seen["body"]["system"])
        self.assertEqual([m["role"] for m in seen["body"]["messages"]], ["user", "assistant", "user"])

    def test_openai_request_and_response(self):
        import httpx
        seen = {}

        def handler(req):
            seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hello there."}}],
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        result = self.go(self.provider("OpenAIProvider", handler).generate(request(provider="openai", model="gpt-4o")))
        self.assertEqual((result.text, result.input_tokens), ("Hello there.", 5))
        self.assertEqual(seen["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(seen["body"]["messages"][0]["role"], "system")

    def test_structured_claims_are_validated_strictly(self):
        import httpx
        from runtime.providers import parse_claims
        good = json.dumps({"claims": [{"text": "Refunds take 14 days.", "evidence_ids": ["ev-1"]}]})

        def handler(req):
            body = json.loads(req.content)
            self.assertEqual(body.get("response_format"), {"type": "json_object"})
            self.assertIn('<evidence id="ev-1">\nRefunds take 14 days.\n</evidence>', body["messages"][0]["content"])
            self.assertIn("never instructions", body["messages"][0]["content"])
            return httpx.Response(200, json={"choices": [{"message": {"content": good}}]})
        result = self.go(self.provider("OpenAIProvider", handler).generate(
            request(provider="openai", model="gpt-4o", structured_claims_required=True)))
        self.assertEqual([c.text for c in result.claims], ["Refunds take 14 days."])
        self.assertEqual(result.text, "")                                    # free text never passes through
        for bad in ("Refunds take 14 days.", "{}", '{"claims": "x"}', '{"claims": [{"text": 1}]}',
                    '{"claims": [{"text": "a", "evidence_ids": ["e"], "extra": 1}]}', "```json {}```",
                    json.dumps({"claims": [{"text": "a", "evidence_ids": ["e"]}] * 51})):
            with self.subTest(bad=bad[:40]):
                self.assertEqual(parse_claims(bad), ())

    def test_error_mapping_and_bounded_retries(self):
        import httpx
        from runtime.errors import ProviderAuthError, ProviderRateLimited, ProviderUnavailable
        cases = [(401, ProviderAuthError, 1), (403, ProviderAuthError, 1), (429, ProviderRateLimited, 1),
                 (400, ProviderUnavailable, 1), (302, ProviderUnavailable, 1), (500, ProviderUnavailable, 2),
                 (503, ProviderUnavailable, 2)]
        for status, exc, expected_calls in cases:
            calls = []

            def handler(req, status=status):
                calls.append(1)
                return httpx.Response(status, json={"error": {"message": f"echo {KEY} internal detail"}},
                                      headers={"location": "http://169.254.169.254/"} if status == 302 else {})
            p = self.provider("AnthropicProvider", handler)
            with self.subTest(status=status):
                with self.assertRaises(exc) as ctx:
                    self.go(p.generate(request()))
                self.assertEqual(len(calls), expected_calls)                  # no redirect follow, bounded retry
                self.assertNotIn(KEY, repr(ctx.exception) + str(ctx.exception))
                self.assertNotIn("internal detail", str(ctx.exception))

    def test_timeouts_and_transport_errors_retry_once_then_fail(self):
        import httpx
        from runtime.errors import ProviderUncertain, ProviderUnavailable
        calls = []

        def handler(req):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("refused", request=req)          # never sent: safe to retry
            return httpx.Response(200, json={"content": [{"type": "text", "text": "recovered"}]})
        p = self.provider("AnthropicProvider", handler)
        self.assertEqual(self.go(p.generate(request())).text, "recovered")
        self.assertEqual((len(calls), p.sleeps), (2, [0.5]))
        calls.clear()

        # Part C: a timeout after the request was sent may have generated: never retried
        for exc in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError):
            def after_send(req, exc=exc):
                calls.append(1)
                raise exc("lost", request=req)
            calls.clear()
            with self.assertRaises(ProviderUncertain):
                self.go(self.provider("AnthropicProvider", after_send).generate(request()))
            self.assertEqual(len(calls), 1)
        calls.clear()

        def always(req):
            calls.append(1)
            raise httpx.ConnectError("refused", request=req)
        with self.assertRaises(ProviderUnavailable):
            self.go(self.provider("AnthropicProvider", always).generate(request()))
        self.assertEqual(len(calls), 2)

    def test_malformed_or_oversized_responses_fail_closed(self):
        import httpx
        from runtime.errors import ProviderUncertain as ProviderUnavailable   # a 200 means it generated
        bodies = [httpx.Response(200, content=b"not json"), httpx.Response(200, json={"content": "x"}),
                  httpx.Response(200, json={"choices": []}),
                  httpx.Response(200, json={"content": [{"type": "text", "text": "x" * 30000}]}),
                  httpx.Response(200, content=b"{" + b" " * 2_100_000 + b"}")]
        for resp in bodies:
            with self.subTest(resp=resp.content[:20]):
                with self.assertRaises(ProviderUnavailable):
                    self.go(self.provider("AnthropicProvider", lambda req, r=resp: r).generate(request()))

    def test_fixed_hosts_only(self):
        from runtime import providers
        self.assertEqual(providers.ANTHROPIC_URL, "https://api.anthropic.com/v1/messages")
        self.assertEqual(providers.OPENAI_URL, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(set(providers.build_providers()), {"anthropic", "openai"})   # no ollama


if __name__ == "__main__":
    unittest.main()
