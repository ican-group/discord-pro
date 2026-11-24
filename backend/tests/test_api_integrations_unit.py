"""Unit tests for api_integrations.py (spec 015). No server or network needed:
    cd backend && python -m pytest tests/test_api_integrations_unit.py -q
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api_integrations as ai  # noqa: E402

TX = "c429616148d242bd5f4191e02e5c539ab79142ad2834ba0c0aee5b89a4c7f374"
INTEGRATION = {"base_url": "https://api.koios.rest/api/v1", "auth_type": "none", "auth_name": ""}
ENDPOINTS = {e["id"]: e for e in ai.parse_document([
    {"path": "/tx_info", "method": "GET", "summary": "Transaction info",
     "parameters": [{"name": "_tx_hashes", "in": "query", "required": True}]},
    {"path": "/tx/{hash}", "method": "GET", "parameters": [{"name": "hash", "in": "path"}]},
    {"path": "/submit", "method": "POST"},
])[1]}
LOOKUP = ai.validate_lookup({
    "name": "Transaction", "endpoint_id": "GET /tx_info",
    "inputs": [{"param": "_tx_hashes", "source": "tx_hash", "template": "{$input}"}],
    "fields": [{"path": "0.fee", "label": "Fee", "unit": "ADA", "scale": 6},
               {"path": "0.block_height", "label": "Block"},
               {"path": "0.tx_timestamp", "label": "Time", "format": "timestamp"}],
}, ENDPOINTS)
KOIOS_TX = [{"tx_hash": TX, "block_height": 14244038, "tx_timestamp": 1795558237, "fee": "667335",
             "total_output": "49210962956", "outputs": [{"value": "1000000"}]}]


def run(coro):
    return asyncio.run(coro)


class FakeNet:
    """Fake DNS + HTTP: records every request; responses by path."""

    def __init__(self, answers=("104.21.1.1",)):
        self.answers = list(answers)
        self.requests = []
        self.routes = {}

    async def resolve(self, host, port):
        return self.answers

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        status, body, headers = self.routes.get(request.url.path, (200, json.dumps(KOIOS_TX), {}))
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode(), headers=headers)

    def __enter__(self):
        ai.NETWORK.update(resolver=self.resolve, client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), follow_redirects=False))
        return self

    def __exit__(self, *exc):
        ai.NETWORK.update(resolver=None, client_factory=None)


# ── extraction and validation of the input ──

@pytest.mark.parametrize("text,expected", [
    (f"what is {TX}?", [TX]),
    (f"tx:{TX.upper()}.", [TX]),
    (TX[:63], []),                       # 63 characters
    (TX + "a", []),                      # 65 characters
    (TX + TX, []),                       # 128-character hex run is not a transaction hash
    ("z" * 64, []),
    (f"{TX} and {TX}", [TX]),
    (f"{TX} {'a' * 64}", [TX, "a" * 64]),
])
def test_extract_tx_hashes(text, expected):
    assert ai.extract_tx_hashes(text) == expected


@pytest.mark.parametrize("text,error", [
    ("hello", "No transaction hash"), (TX[:-1] + "g", "No transaction hash"),
    (f"{TX} {'b' * 64}", "More than one"),
])
def test_tester_input_errors(text, error):
    with pytest.raises(ai.ConfigError, match=error):
        ai.tester_input(text)


# ── outbound URL policy ──

@pytest.mark.parametrize("url", [
    "http://api.koios.rest", "https://localhost/api", "https://10.0.0.5", "https://127.1", "https://2130706433",
    "https://[::1]/", "https://user:pw@api.koios.rest", "https://api.koios.rest/v1?x=1", "https://db.internal",
    "https://169.254.169.254/latest", "https://api.koios.rest/a/../b", "ftp://api.koios.rest", "https://intranet",
])
def test_base_url_rejected(url):
    with pytest.raises(ai.OutboundBlocked):
        ai.parse_base_url(url)


def test_base_url_accepted():
    assert ai.parse_base_url("https://API.koios.rest/api/v1/") == {
        "origin": "https://api.koios.rest", "base_path": "/api/v1", "host": "api.koios.rest", "port": 443}


@pytest.mark.parametrize("name", ["Host", "X-Forwarded-For", "Cookie", "Proxy-Authorization", "bad header"])
def test_forbidden_auth_header_names(name):
    with pytest.raises(ai.OutboundBlocked):
        ai.validate_auth("header", name)


# ── import ──

def test_parse_openapi_and_unsafe_paths():
    doc = {"openapi": "3.1.0", "paths": {
        "/tip": {"get": {"summary": "Tip"}},
        "/tx_info": {"post": {"summary": "Tx"}},
        "/account/{addr}": {"parameters": [{"$ref": "#/components/parameters/addr"}], "get": {}},
        "/../admin": {"get": {}}, "//evil.example/x": {"get": {}}, "/a b": {"get": {}},
    }, "components": {"parameters": {"addr": {"name": "addr", "in": "path", "required": True}}}}
    kind, eps = ai.parse_document(doc)
    assert kind == "openapi"
    assert [e["id"] for e in eps] == ["GET /tip", "POST /tx_info", "GET /account/{addr}"]
    assert eps[2]["parameters"] == [{"name": "addr", "in": "path", "required": True, "type": "string", "description": ""}]


def test_parse_reference_shapes_and_errors():
    assert ai.parse_document({"endpoints": [{"path": "tx/{h}"}]})[1][0]["parameters"] == []
    assert ai.parse_document({"endpoints": [{"path": "/tx/{h}", "params": [{"name": "h"}]}]})[1][0]["parameters"][0]["in"] == "path"
    for bad in ({}, [], {"x": 1}, [{"nopath": 1}], "text"):
        with pytest.raises(ai.ConfigError):
            ai.parse_document(bad)


# ── lookup configuration ──

@pytest.mark.parametrize("change,error", [
    ({"endpoint_id": "POST /submit"}, "Only GET"),
    ({"endpoint_id": "GET /nope"}, "Choose an imported endpoint"),
    ({"inputs": [{"param": "_tx_hashes", "source": "fixed", "value": "x"}]}, "Exactly one"),
    ({"inputs": [{"param": "_tx_hashes", "source": "tx_hash", "template": "{hash}"}]}, r"must contain \$input"),
    ({"fields": []}, "at least one response field"),
    ({"fields": [{"path": "0.fee", "scale": 30}]}, "between 0 and 18"),
    ({"fields": [{"path": "0.fee", "format": "html"}]}, "Unknown format"),
    ({"name": " "}, "name"),
])
def test_validate_lookup_errors(change, error):
    base = {"name": "T", "endpoint_id": "GET /tx_info",
            "inputs": [{"param": "_tx_hashes", "source": "tx_hash"}], "fields": [{"path": "0.fee"}]}
    with pytest.raises(ai.ConfigError, match=error):
        ai.validate_lookup({**base, **change}, ENDPOINTS)


def test_path_parameter_needs_a_value():
    with pytest.raises(ai.ConfigError, match="Exactly one|needs a value"):
        ai.validate_lookup({"name": "T", "endpoint_id": "GET /tx/{hash}", "inputs": [], "fields": [{"path": "a"}]}, ENDPOINTS)


# ── request building ──

def test_build_request_query_template_and_base_path():
    req = ai.build_request(INTEGRATION, LOOKUP, TX, None)
    assert req["origin"] == "https://api.koios.rest" and req["host"] == "api.koios.rest"
    assert req["target"] == f"/api/v1/tx_info?_tx_hashes=%7B{TX}%7D"
    assert "Authorization" not in req["headers"]


def test_build_request_path_values_are_encoded_and_secret_hidden():
    lookup = ai.validate_lookup({"name": "T", "endpoint_id": "GET /tx/{hash}",
                                 "inputs": [{"param": "hash", "source": "tx_hash", "template": "$input/../../x?y#"}],
                                 "fields": [{"path": "a"}]}, ENDPOINTS)
    req = ai.build_request({**INTEGRATION, "auth_type": "query", "auth_name": "key"}, lookup, TX, "s3cret")
    assert req["target"].startswith(f"/api/v1/tx/{TX}%2F..%2F..%2Fx%3Fy%23?key=s3cret")
    assert "s3cret" not in req["display_url"]
    bearer = ai.build_request({**INTEGRATION, "auth_type": "bearer"}, LOOKUP, TX, "tok")
    assert bearer["headers"]["Authorization"] == "Bearer tok"
    header = ai.build_request({**INTEGRATION, "auth_type": "header", "auth_name": "project_id"}, LOOKUP, TX, "pid")
    assert header["headers"]["project_id"] == "pid"
    with pytest.raises(ai.ConfigError):
        ai.build_request(INTEGRATION, LOOKUP, "not-a-hash", None)


# ── formatting ──

def test_format_fields():
    rows = ai.format_fields(KOIOS_TX, LOOKUP["fields"] + [
        {"path": "0.total_output", "label": "Total", "unit": "ADA", "scale": 6, "format": "auto"},
        {"path": "0.missing", "label": "Missing", "unit": "", "scale": 0, "format": "auto"},
        {"path": "[0].outputs[0].value", "label": "First output", "unit": "lovelace", "scale": 0, "format": "number"}])
    text = ai.fields_text(rows)
    assert text.splitlines() == ["Fee: 0.667335 ADA", "Block: 14,244,038", "Time: 2026-11-24 22:10:37 UTC",
                                 "Total: 49,210.962956 ADA", "Missing: not available", "First output: 1,000,000 lovelace"]


def test_leaf_paths():
    paths = [p["path"] for p in ai.leaf_paths(KOIOS_TX)]
    assert "0.fee" in paths and "0.outputs[0].value" in paths


# ── the lookup (shared by tester and bot) ──

def test_run_lookup_ok_sends_only_to_checked_address():
    with FakeNet() as net:
        r = run(ai.run_lookup(INTEGRATION, LOOKUP, TX, None, include_raw=True))
    assert r["status"] == "ok" and r["http_status"] == 200
    assert r["text"] == "Fee: 0.667335 ADA\nBlock: 14,244,038\nTime: 2026-11-24 22:10:37 UTC"
    assert "total_output" in r["raw"] and "total_output" not in r["text"]
    req = net.requests[0]
    assert req.url.host == "104.21.1.1" and req.headers["host"] == "api.koios.rest"
    assert req.extensions.get("sni_hostname") == "api.koios.rest"
    block = ai.bot_block("Transaction", r)
    assert "total_output" not in block and "koios" not in block and "Fee: 0.667335 ADA" in block


@pytest.mark.parametrize("route,status,needle", [
    ((200, "[]", {}), "not_found", "no data"),
    ((404, "missing", {}), "not_found", "404"),
    ((500, "secret upstream stack trace", {}), "api_error", "HTTP 500"),
    ((302, "", {"location": "http://169.254.169.254/"}), "api_error", "redirects are not followed"),
    ((200, "<html>not json</html>", {}), "api_error", "did not return JSON"),
    ((200, b"x" * (ai.MAX_RESPONSE_SIZE + 10), {}), "api_error", "larger than"),
    ((200, httpx.ReadTimeout("slow"), {}), "api_error", "did not respond in time"),
    ((200, httpx.ConnectError("refused"), {}), "api_error", "Could not connect"),
])
def test_run_lookup_errors(route, status, needle):
    with FakeNet() as net:
        net.routes["/api/v1/tx_info"] = route
        r = run(ai.run_lookup(INTEGRATION, LOOKUP, TX, None))
    assert r["status"] == status and needle in r["message"], r
    assert "upstream" not in r["message"] and r["text"] == ""
    assert len(net.requests) == 1                     # redirects are never followed


@pytest.mark.parametrize("answers", [["10.0.0.7"], ["104.21.1.1", "127.0.0.1"], ["169.254.169.254"], ["::ffff:10.1.1.1"], []])
def test_private_dns_answers_block_before_any_request(answers):
    with FakeNet(answers) as net:
        r = run(ai.run_lookup(INTEGRATION, LOOKUP, TX, "s3cret"))
    assert r["status"] in ("blocked", "api_error") and not net.requests
    assert "s3cret" not in json.dumps(r)
