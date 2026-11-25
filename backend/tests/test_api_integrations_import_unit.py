"""Unit tests for spec 017 (URL import, documented schemas, change report, connection check,
observed outcomes, response quality). No server or network needed:
    cd backend && python -m pytest tests/test_api_integrations_import_unit.py -q
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api_integrations as ai  # noqa: E402

KEY = "gs-test-key-123"
HASH = "a" * 64
SPEC = {
    "openapi": "3.1.0",
    "components": {
        "securitySchemes": {"ProjectIdHeader": {"type": "apiKey", "in": "header", "name": "x-project-id"}},
        "schemas": {
            "Amount": {"type": "object", "required": ["unit", "quantity"],
                       "properties": {"unit": {"type": "string"}, "quantity": {"type": "string"}}},
            "Output": {"type": "object", "required": ["address", "amount"],
                       "properties": {"address": {"type": "string"},
                                      "amount": {"type": "array", "items": {"$ref": "#/components/schemas/Amount"}},
                                      "datum": {"anyOf": [{"type": "string"}, {"type": "null"}]}}},
            "Tx": {"type": "object", "required": ["hash", "fees"],
                   "properties": {"hash": {"type": "string"}, "fees": {"type": "string"},
                                  "block_height": {"type": ["integer", "null"]},
                                  "valid_contract": {"type": "boolean"},
                                  "outputs": {"type": "array", "items": {"$ref": "#/components/schemas/Output"}},
                                  "node": {"$ref": "#/components/schemas/Node"}}},
            "Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"},
                                                      "name": {"type": "string"}}},
        },
    },
    "paths": {
        "/api/health": {"get": {"summary": "Health", "security": [],
                                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/api/txs/{hash}": {"get": {
            "summary": "Tx info", "security": [{"ProjectIdHeader": []}],
            "parameters": [{"name": "hash", "in": "path", "required": True, "description": "Tx hash"}],
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Tx"}}}},
                          "401": {"description": "no key"}}}},
        "/api/open/{hash}": {"get": {"security": [], "parameters": [{"name": "hash", "in": "path", "required": True}],
                                     "responses": {"200": {"description": "ok"}}}},
        "/api/signup": {"post": {"summary": "Sign up", "security": []}},
    },
}
INTEGRATION = {"base_url": "https://ada.api.gridscan.io", "auth_type": "header", "auth_name": "x-project-id"}
TX = {"hash": HASH, "fees": "171000", "block_height": 1, "valid_contract": True,
      "outputs": [{"address": "addr1", "amount": [{"unit": "lovelace", "quantity": "1"}], "datum": None}]}


def run(coro):
    return asyncio.run(coro)


def endpoints():
    return {e["id"]: e for e in ai.parse_document(SPEC)[1]}


class FakeApi:
    """Fake DNS + HTTP: ``handler(request) -> (status, body)``; records every request."""

    def __init__(self, handler):
        self.handler, self.requests = handler, []

    async def resolve(self, host, port):
        return ["104.21.1.1"]

    def _respond(self, request):
        self.requests.append(request)
        status, body = self.handler(request)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode())

    def __enter__(self):
        ai.NETWORK.update(resolver=self.resolve, client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(self._respond), follow_redirects=False))
        return self

    def __exit__(self, *exc):
        ai.NETWORK.update(resolver=None, client_factory=None)


def gridscan(*, key_ok=True, health=200, tx_status=200, no_key_status=401):
    def handler(req):
        if req.url.path == "/api/health":
            return health, {"status": "ok"}
        if req.url.path == "/api/openapi.json":
            return 200, SPEC
        if req.url.path.startswith("/api/txs/"):
            key = req.headers.get("x-project-id")
            if not key:
                return no_key_status, {"detail": "missing key"} if no_key_status >= 400 else TX
            if key != KEY or not key_ok:
                return 401, {"detail": "invalid key"}
            return tx_status, TX if tx_status == 200 else {"detail": "x"}
        return 404, {"detail": "not found"}
    return handler


# ── documented response schemas ──

def test_schemas_kept_with_refs_nullable_and_auth():
    eps = endpoints()
    tx = eps["GET /api/txs/{hash}"]
    s = tx["response_schema"]
    assert tx["requires_auth"] is True and eps["GET /api/health"]["requires_auth"] is False
    assert s["types"] == ["object"] and s["required"] == ["hash", "fees"]
    assert s["properties"]["block_height"] == {"types": ["integer"], "nullable": True}
    out = s["properties"]["outputs"]["items"]
    assert out["properties"]["amount"]["items"]["required"] == ["unit", "quantity"]
    assert out["properties"]["datum"]["nullable"] is True and out["properties"]["datum"]["types"] == ["string"]
    # a recursive $ref stops instead of looping
    assert s["properties"]["node"]["properties"]["child"] == {"types": [], "nullable": False}
    assert eps["GET /api/open/{hash}"]["response_schema"] is None      # no JSON schema documented
    assert ai.schema_field_count(s) > 10


def test_swagger2_schema_and_document_security_default():
    spec = {"swagger": "2.0", "security": [{"k": []}], "paths": {"/x": {"get": {"responses": {"200": {
        "schema": {"type": "array", "items": {"type": "object", "properties": {"a": {"type": "integer", "x-nullable": True}}}}}}}}}}
    ep = ai.parse_document(spec)[1][0]
    assert ep["requires_auth"] is True
    assert ep["response_schema"]["items"]["properties"]["a"] == {"types": ["integer"], "nullable": True}


def test_reference_format_has_no_schema():
    ep = ai.parse_document([{"path": "/tx", "method": "GET"}])[1][0]
    assert ep["response_schema"] is None and ep["requires_auth"] is None


# ── import from URL and change report ──

@pytest.mark.parametrize("url", ["http://ada.api.gridscan.io/api/openapi.json", "https://localhost/api.json",
                                 "https://10.0.0.1/a.json", "https://u:p@ada.api.gridscan.io/a.json",
                                 "https://ada.api.gridscan.io/a/../b.json", "https://ada.api.gridscan.io/a.json#x", ""])
def test_doc_url_rejected(url):
    with pytest.raises(ai.OutboundBlocked):
        ai.parse_doc_url(url)


def test_doc_url_accepted_with_query():
    d = ai.parse_doc_url("https://ada.api.gridscan.io/api/openapi.json?v=2")
    assert d["url"] == "https://ada.api.gridscan.io/api/openapi.json?v=2" and d["target"] == "/api/openapi.json?v=2"


def test_fetch_document_sends_key_only_to_the_integration_origin():
    secret_integration = {**INTEGRATION}
    with FakeApi(gridscan()) as net:
        doc = run(ai.fetch_document(secret_integration, ai.parse_doc_url("https://ada.api.gridscan.io/api/openapi.json"), KEY))
        assert doc["openapi"] == "3.1.0" and net.requests[0].headers.get("x-project-id") == KEY
        other = {**INTEGRATION, "base_url": "https://other.example.com"}
        run(ai.fetch_document(other, ai.parse_doc_url("https://ada.api.gridscan.io/api/openapi.json"), KEY))
        assert "x-project-id" not in net.requests[1].headers


@pytest.mark.parametrize("status,body,needle", [
    (302, b"", "redirected"), (404, b"nope", "HTTP 404"), (200, b"openapi: 3.0.0", "not valid JSON"),
    (200, b"x" * (ai.MAX_DOC_SIZE + 10), "larger than 2 MB"),
])
def test_fetch_document_errors(status, body, needle):
    with FakeApi(lambda req: (status, body)):
        with pytest.raises(ai.ConfigError, match=needle):
            run(ai.fetch_document(INTEGRATION, ai.parse_doc_url("https://ada.api.gridscan.io/api/openapi.json"), None))


def test_diff_endpoints_reports_new_updated_removed():
    old = ai.parse_document(SPEC)[1]
    spec2 = json.loads(json.dumps(SPEC))
    del spec2["paths"]["/api/open/{hash}"]
    spec2["paths"]["/api/txs/{hash}"]["get"]["summary"] = "Transaction"
    spec2["paths"]["/api/blocks/latest"] = {"get": {"responses": {}}}
    d = ai.diff_endpoints(old, ai.parse_document(spec2)[1])
    assert d == {"new": ["GET /api/blocks/latest"], "updated": ["GET /api/txs/{hash}"],
                 "removed": ["GET /api/open/{hash}"], "unchanged": 2}


def test_doc_coverage_counts_only_documentation():
    c = ai.doc_coverage(ai.parse_document(SPEC)[1])
    assert c["get_endpoints"] == 3 and c["with_response_schema"] == 2 and c["parameters"] == 2 and c["parameters_described"] == 1


# ── connection check ──

def integration(**conn):
    return {**INTEGRATION, "connection": {"health_endpoint_id": "GET /api/health",
                                          "credential_endpoint_id": "GET /api/txs/{hash}",
                                          "credential_params": {"hash": HASH}, **conn}}


def test_connection_key_accepted_needs_rejection_without_key():
    with FakeApi(gridscan()) as net:
        r = run(ai.connection_check(integration(), endpoints(), KEY))
    assert r["ok"] and r["reachability"]["state"] == "reachable" and r["credentials"]["state"] == "accepted"
    assert [p["label"] for p in r["probes"]] == ["Health check: GET /api/health",
                                                 "Credential check: GET /api/txs/{hash} with the key",
                                                 "Credential check: GET /api/txs/{hash} without a key"]
    health, with_key, without = net.requests
    assert "x-project-id" not in health.headers and with_key.headers["x-project-id"] == KEY
    assert "x-project-id" not in without.headers
    assert all(KEY not in p["url"] for p in r["probes"])


def test_rejected_key_is_never_a_successful_connection():
    with FakeApi(gridscan(key_ok=False)):
        r = run(ai.connection_check(integration(), endpoints(), "wrong"))
    assert not r["ok"] and r["reachability"]["state"] == "reachable"
    assert r["credentials"]["state"] == "rejected" and "HTTP 401" in r["credentials"]["message"]


def test_query_key_is_not_shown_in_probe_urls():
    integ = {**integration(), "auth_type": "query", "auth_name": "api_key"}
    with FakeApi(lambda req: (200, TX) if "api_key" in str(req.url) or "health" in str(req.url) else (401, {})):
        r = run(ai.connection_check(integ, endpoints(), KEY))
    assert r["credentials"]["state"] == "accepted" and all(KEY not in json.dumps(p) for p in r["probes"])


@pytest.mark.parametrize("kw,state,needle", [
    ({"tx_status": 404}, "not_verified", "wasn't found"),
    ({"tx_status": 500}, "not_verified", "server error"),
    ({"tx_status": 429}, "not_verified", "rate limiting"),
    ({"tx_status": 400}, "not_verified", "sample input"),
    ({"no_key_status": 200}, "not_verified", "also answered without a key"),
])
def test_credentials_not_verified(kw, state, needle):
    with FakeApi(gridscan(**kw)):
        r = run(ai.connection_check(integration(), endpoints(), KEY))
    assert r["credentials"]["state"] == state and needle in r["credentials"]["message"] and not r["ok"]


def test_health_failure_and_unreachable_host():
    with FakeApi(gridscan(health=503)):
        r = run(ai.connection_check(integration(), endpoints(), KEY))
    assert r["reachability"]["state"] == "unhealthy" and r["credentials"]["state"] == "accepted" and not r["ok"]
    with FakeApi(lambda req: (200, httpx.ConnectError("refused"))):
        r = run(ai.connection_check(integration(), endpoints(), KEY))
    assert r["reachability"]["state"] == "unreachable" and r["credentials"]["state"] == "not_verified"


@pytest.mark.parametrize("integ,secret,state", [
    ({**integration(), "auth_type": "none"}, None, "not_required"),
    (integration(), None, "missing"),
    (integration(credential_endpoint_id=""), KEY, "not_checked"),
    (integration(credential_endpoint_id="GET /api/open/{hash}"), KEY, "not_checked"),    # documented public
    (integration(credential_endpoint_id="GET /gone"), KEY, "not_checked"),
    (integration(credential_params={}), KEY, "not_checked"),                             # no sample input
])
def test_credentials_not_checked_without_a_request(integ, secret, state):
    with FakeApi(gridscan()) as net:
        r = run(ai.connection_check(integ, endpoints(), secret))
    assert r["credentials"]["state"] == state
    assert all("txs" not in str(q.url) for q in net.requests)
    assert r["ok"] is (state == "not_required")


def test_without_health_endpoint_reachability_comes_from_the_credential_probe():
    with FakeApi(gridscan(key_ok=False)):
        r = run(ai.connection_check(integration(health_endpoint_id=""), endpoints(), "wrong"))
    assert r["reachability"]["state"] == "reachable" and "No health check" in r["reachability"]["message"]
    assert r["credentials"]["state"] == "rejected" and not r["ok"]


def test_blocked_destination():
    integ = integration()
    with FakeApi(gridscan()) as net:
        net.resolve = None
        ai.NETWORK["resolver"] = lambda host, port: asyncio.sleep(0, result=["10.0.0.5"])
        r = run(ai.connection_check(integ, endpoints(), KEY))
    assert r["reachability"]["state"] == "blocked" and not net.requests


# ── observed outcomes ──

@pytest.mark.parametrize("result,outcome", [
    ({"status": "ok", "http_status": 200}, "passed"),
    ({"status": "not_found", "reason": "empty", "http_status": 200}, "not_found"),
    ({"status": "not_found", "reason": "http_404", "http_status": 404}, "not_found"),
    ({"status": "unavailable", "reason": "auth", "http_status": 401}, "failed"),
    ({"status": "unavailable", "reason": "network", "http_status": None}, "failed"),
    ({"status": "unavailable", "reason": "rate_limited", "http_status": 429}, "failed"),
    ({"status": "unavailable", "reason": "configuration"}, None),
    ({"status": "unavailable", "reason": "rate_limited", "http_status": None}, None),   # BridgeBot's own limit
])
def test_observed_outcome(result, outcome):
    o = ai.observed_outcome(result)
    assert (o or {}).get("outcome") == outcome


# ── response quality ──

def tx_schema():
    return endpoints()["GET /api/txs/{hash}"]["response_schema"]


def statuses(q):
    return {f["path"]: (f["status"], f["count"], f["of"]) for f in q["fields"] if f["status"] != "ok"}


def test_quality_matching_response():
    q = ai.compare_response(TX, tx_schema())
    assert q["documented"] and q["summary"]["missing_required"] == q["summary"]["type_difference"] == 0
    assert statuses(q) == {"node": ("missing_optional", 1, 1)}


def test_quality_reports_required_optional_unexpected_and_types_in_nested_arrays():
    data = {"fees": 171000, "block_height": None, "extra": 1,
            "outputs": [{"address": "a", "amount": [{"unit": "lovelace", "quantity": "1"}, {"unit": "x", "quantity": 5}]},
                        {"address": "b", "amount": [{"unit": "lovelace"}], "datum": 7, "note": "?"},
                        {"amount": "not an array"}]}
    q = ai.compare_response(data, tx_schema())
    st = statuses(q)
    assert st["hash"] == ("missing_required", 1, 1)
    assert st["fees"] == ("type_difference", 1, 1)
    assert "block_height" not in st                                   # null is documented as allowed
    assert st["extra"] == ("unexpected", 1, 1)
    assert st["valid_contract"] == ("missing_optional", 1, 1)
    assert st["outputs[].address"] == ("missing_required", 1, 3)
    assert st["outputs[].amount"] == ("type_difference", 1, 3)
    assert st["outputs[].datum"] == ("type_difference", 1, 3)
    assert st["outputs[].note"] == ("unexpected", 1, 3)
    assert st["outputs[].amount[].quantity"] in (("type_difference", 1, 3), ("missing_required", 1, 3))
    s = q["summary"]
    assert s["missing_required"] >= 2 and s["type_difference"] >= 3 and s["unexpected"] == 2
    f = next(x for x in q["fields"] if x["path"] == "fees")
    assert f["expected"] == ["string"] and f["actual"] == ["integer"] and f["required"]


def test_quality_root_type_and_no_schema():
    q = ai.compare_response([1, 2], tx_schema())
    assert q["root_mismatch"] == {"expected": ["object"], "actual": ["array"]}
    assert ai.compare_response(TX, None)["documented"] is False


def test_quality_arrays_are_sampled_not_unbounded():
    schema = {"types": ["array"], "nullable": False, "items": {"types": ["integer"], "nullable": False}}
    q = ai.compare_response(list(range(ai.QUALITY_MAX_ITEMS + 20)) + ["x"], schema)
    assert q["sampled"] and q["summary"]["type_difference"] == 0      # item 71 isn't examined


def test_run_lookup_tester_includes_quality_bot_does_not():
    eps = endpoints()
    lookup = ai.validate_lookup({"name": "Tx", "endpoint_id": "GET /api/txs/{hash}",
                                 "inputs": [{"param": "hash", "source": "tx_hash", "template": "$input"}],
                                 "fields": [{"path": "fees", "label": "Fee"}]}, eps)
    with FakeApi(gridscan()):
        tester = run(ai.run_lookup(INTEGRATION, lookup, HASH, KEY, include_raw=True,
                                   schema=eps["GET /api/txs/{hash}"]["response_schema"]))
        bot = run(ai.run_lookup(INTEGRATION, lookup, HASH, KEY))
    assert tester["status"] == bot["status"] == "ok" and tester["text"] == bot["text"]
    assert tester["quality"]["documented"] and "quality" not in bot and "raw" not in bot


def test_base_path_repeated_by_documented_paths_is_flagged():
    eps = ai.parse_document(SPEC)[1]
    w = ai.base_path_warning("https://ada.api.gridscan.io/api", eps)
    assert "/api/api/" in w and "https://ada.api.gridscan.io." in w
    assert ai.base_path_warning("https://ada.api.gridscan.io", eps) is None
    assert ai.base_path_warning("https://api.koios.rest/api/v1", ai.parse_document([{"path": "/tx_info"}])[1]) is None
