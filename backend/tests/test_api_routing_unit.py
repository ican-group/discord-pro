"""Unit tests for api_routing.py (spec 018). No server or network needed:
    cd backend && python -m pytest tests/test_api_routing_unit.py -q

Fixtures: a subset of GridScan's public OpenAPI document, and a Blockfrost-style mock document
(Cardano preprod) written for these tests.
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import api_integrations as ai  # noqa: E402
import api_routing as r  # noqa: E402

GRIDSCAN = json.load(open(os.path.join(HERE, "fixtures", "gridscan-openapi-subset.json")))
MOCK = json.load(open(os.path.join(HERE, "fixtures", "blockfrost-style-mock.json")))
GS_EPS = [e for e in ai.parse_document(GRIDSCAN)[1] if e["method"] == "GET"]
MOCK_EPS = [e for e in ai.parse_document(MOCK)[1] if e["method"] == "GET"]
GS = {"integration": {"id": "gs", "name": "GridScan", "network": "mainnet"}, "endpoints": GS_EPS}
BF = {"integration": {"id": "bf", "name": "Preprod mock", "network": "testnet"}, "endpoints": MOCK_EPS}


def example(path):
    return GRIDSCAN["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["example"]


ADDR = example("/api/addresses/{address}")["address"]                 # real mainnet base address
STAKE = example("/api/addresses/{address}")["stake_address"]          # real mainnet stake address
TX = example("/api/txs/{hash}")["hash"]
# CIP-19 test vectors (https://cips.cardano.org/cip/CIP-0019)
CIP19_ADDR_TEST = "addr_test1qz2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer3n0d3vllmyqwsx5wktcd8cc3sq835lu7drv2xwl2wywfgs68faae"
CIP19_STAKE = "stake1uyehkck0lajq8gr28t9uxnuvgcqrc6070x3k9r8048z8y5gh6ffgw"
CIP19_STAKE_TEST = "stake_test1uqehkck0lajq8gr28t9uxnuvgcqrc6070x3k9r8048z8y5gssrtvn"
CIP19_ENTERPRISE = "addr1vx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzers66hrl8"


def run(coro):
    return asyncio.run(coro)


# ── Cardano address parser ──

@pytest.mark.parametrize("value,kind,network,atype", [
    (ADDR, "payment_address", "mainnet", "base"),
    (CIP19_ENTERPRISE, "payment_address", "mainnet", "enterprise"),
    (CIP19_ADDR_TEST, "payment_address", "testnet", "base"),
    (STAKE, "stake_address", "mainnet", "stake key"),
    (CIP19_STAKE, "stake_address", "mainnet", "stake key"),
    (CIP19_STAKE_TEST, "stake_address", "testnet", "stake key"),
    (ADDR.upper(), "payment_address", "mainnet", "base"),               # bech32 allows all upper case
])
def test_valid_addresses(value, kind, network, atype):
    a = r.parse_cardano_address(value)
    assert (a["kind"], a["network"], a["address_type"]) == (kind, network, atype)


def _swap_hrp(addr, hrp):
    _, payload = r.bech32_decode(addr)
    return r.bech32_encode(hrp, payload)


@pytest.mark.parametrize("value,reason", [
    (ADDR[:-1] + ("q" if ADDR[-1] != "q" else "p"), "checksum"),         # one character changed
    (ADDR[:20] + ADDR[21:], "checksum"),                                 # one character dropped
    (_swap_hrp(ADDR, "addr_test"), "network id"),                         # mainnet header, testnet prefix
    (_swap_hrp(STAKE, "addr"), "not a payment address"),                  # stake header under addr
    (r.bech32_encode("stake", bytes([0xE1]) + b"\x00" * 10), "wrong length"),
    (r.bech32_encode("pool", b"\x00" * 28), "unknown address prefix"),
    ("addr1" + ADDR[5:].replace(ADDR[10], "B", 1), "mixed upper and lower case"),
])
def test_invalid_addresses(value, reason):
    with pytest.raises(ValueError, match=reason):
        r.parse_cardano_address(value)


# ── input detection ──

def test_detects_kinds_and_hash_intent():
    d = r.detect_inputs(f"balance of {ADDR} and rewards for {STAKE}")
    assert [i["kind"] for i in d["inputs"]] == ["payment_address", "stake_address"]
    assert r.detect_inputs(f"fee of transaction {TX}")["inputs"][0]["kind"] == "tx_hash"
    assert r.detect_inputs(f"show block {TX}")["inputs"][0]["kind"] == "block_hash"
    assert r.detect_inputs(f"which block holds tx {TX}")["inputs"][0]["kind"] == "tx_hash"
    assert r.detect_inputs(TX)["inputs"][0]["kind"] == "hash"
    # an explicit block hash stays a block hash even when the question also talks about transactions
    for q in (f"which transactions are in block {TX}?", f"how many txs in block {TX}",
              f"what is the tx count of block hash {TX}"):
        assert r.detect_inputs(q)["inputs"][0]["kind"] == "block_hash", q
    for q in (f"what block is transaction {TX} in?", f"block containing tx {TX}", f"fees {TX}"):
        assert r.detect_inputs(q)["inputs"][0]["kind"] == "tx_hash", q
    assert r.detect_inputs("a" * 65)["inputs"] == []                    # longer hex run isn't a hash
    bad = r.detect_inputs(ADDR[:-1] + "x")
    assert bad["inputs"] == [] and "checksum" in bad["invalid"][0]["reason"]


def test_parameter_kinds_from_documentation():
    eps = {e["id"]: e for e in GS_EPS + MOCK_EPS}
    kinds = lambda i: [k for req in r.endpoint_requirements(eps[i]) for k in req["kinds"]]
    assert kinds("GET /api/txs/{hash}") == ["tx_hash"]
    assert kinds("GET /api/blocks/{hash_or_number}") == ["block_hash", "block_number"]
    assert kinds("GET /api/addresses/{address}") == ["payment_address"]
    assert kinds("GET /api/accounts/{stake_address}/rewards") == ["stake_address"]
    assert kinds("GET /txs/{hash}") == ["tx_hash"] and kinds("GET /blocks/{hash_or_number}") == ["block_hash", "block_number"]
    assert kinds("GET /api/pools/{pool_id}") == ["pool_id"]                          # spec 019
    assert kinds("GET /api/epochs/{number}") == ["epoch_number"]
    assert kinds("GET /api/governance/proposals/{tx_hash}/{cert_index}/votes") == ["tx_hash", "proposal_index"]
    assert kinds("GET /api/governance/committee/members/{credential}") == ["committee_credential"]
    assert r.endpoint_requirements(eps["GET /api/network"]) == []                    # no input


# ── routing on the GridScan document ──

def decide(q, providers=(GS,)):
    d = r.route(q, providers)
    return d["action"], d["endpoint"]["id"] if d["action"] == "call" else d.get("question") or d.get("message")


@pytest.mark.parametrize("question,endpoint", [
    (f"What was the fee for transaction {TX}?", "GET /api/txs/{hash}"),
    (f"Which block contains tx {TX}?", "GET /api/txs/{hash}"),
    (f"show block {TX}", "GET /api/blocks/{hash_or_number}"),
    (f"What's the balance of {ADDR}?", "GET /api/addresses/{address}"),
    (ADDR, "GET /api/addresses/{address}"),
    (f"utxos of {ADDR}", "GET /api/addresses/{address}/utxos"),
    (f"how many transactions has {ADDR} made", "GET /api/addresses/{address}/tx_count"),
    (f"What are the staking rewards for {STAKE}?", "GET /api/accounts/{stake_address}/rewards"),
    (f"rewards {STAKE}", "GET /api/accounts/{stake_address}/rewards"),
    (f"which pool is {STAKE} delegated to?", "GET /api/accounts/{stake_address}/delegations"),
    (f"balance of {STAKE}", "GET /api/accounts/{stake_address}"),
    ("What's the network status?", "GET /api/network"),
    ("What is the current epoch?", "GET /api/epochs/latest"),
    ("latest block", "GET /api/blocks/latest"),
    ("network stats", "GET /api/network/stats"),
])
def test_routes_to_the_endpoint_that_answers(question, endpoint):
    assert decide(question) == ("call", endpoint)


@pytest.mark.parametrize("question,endpoint", [
    (f"block hash {TX}", "GET /api/blocks/{hash_or_number}"),
    (f"which transactions are in block {TX}?", "GET /api/blocks/{hash_or_number}/txs"),
    (f"how many txs in block {TX}", "GET /api/blocks/{hash_or_number}/txs"),
])
def test_explicit_block_hash_skips_saved_lookup_and_reaches_block_endpoint(question, endpoint):
    assert r.saved_lookup_hash(r.detect_inputs(question)) is None
    assert decide(question) == ("call", endpoint)


def test_saved_lookup_keeps_transaction_and_bare_hashes():
    for q in (TX, f"fee of transaction {TX}", f"what block is transaction {TX} in?", f"fees {TX}"):
        assert r.saved_lookup_hash(r.detect_inputs(q)) == TX.lower(), q


@pytest.mark.parametrize("question", [
    "hello there", "How do I stake my ADA?", "How do staking rewards work?", "tell me about cardano governance",
    "What is an epoch?", "What are the fees on Cardano?", "why did my transaction fail?", "My wallet won't sync, can you help?",
])
def test_knowledge_questions_make_no_call(question):
    assert decide(question)[0] == "none"


@pytest.mark.parametrize("question,ask", [
    (TX, "transaction hash or a block hash"),
    ("What are my staking rewards?", "Which stake address (stake1…)"),
    ("What's my balance?", "Which stake address (stake1…) or address (addr1…)"),
])
def test_asks_one_short_question(question, ask):
    action, text = decide(question)
    assert action == "ask" and ask in text and text.count("?") == 1 and len(text) < 120


def test_invalid_and_wrong_network_are_rejected_without_a_call():
    assert decide(f"balance of {ADDR[:-1]}x")[0] == "reject"
    action, msg = decide(f"rewards for {CIP19_STAKE_TEST}")
    assert action == "reject" and "testnet stake address" in msg and "GridScan serves Cardano mainnet" in msg and "(stake1…)" in msg
    action, msg = decide(f"balance {CIP19_ADDR_TEST}")
    assert action == "reject" and "(addr1…)" in msg


def test_testnet_address_goes_to_the_testnet_provider():
    d = r.route(f"rewards for {CIP19_STAKE_TEST}", [GS, BF])
    assert d["action"] == "call" and d["integration"]["name"] == "Preprod mock" and d["endpoint"]["id"] == "GET /accounts/{stake_address}/rewards"
    d = r.route(f"rewards for {STAKE}", [GS, BF])
    assert d["integration"]["name"] == "GridScan"


def test_second_provider_routes_by_its_own_documentation():
    for q, ep in [(f"fee for tx {TX}", "GET /txs/{hash}"), (f"block {TX}", "GET /blocks/{hash_or_number}"),
                  (f"balance of {CIP19_ADDR_TEST}", "GET /addresses/{address}"), ("network status", "GET /network"),
                  ("latest block", "GET /blocks/latest")]:
        assert decide(q, (BF,)) == ("call", ep), q


def test_disabled_endpoint_is_not_used():
    without = {**GS, "endpoints": [e for e in GS_EPS if e["id"] != "GET /api/accounts/{stake_address}/rewards"]}
    action, ep = decide(f"rewards {STAKE}", (without,))
    assert ep != "GET /api/accounts/{stake_address}/rewards"


def test_schema_constraints_are_checked_before_calling():
    eps = json.loads(json.dumps(MOCK_EPS))
    for e in eps:
        if e["path"] == "/txs/{hash}":
            e["parameters"][0]["pattern"] = "^[0-9a-f]{64}$"
            e["parameters"][0]["max_length"] = 10
    assert r.route(f"fee for tx {TX}", [{**BF, "endpoints": eps}])["action"] == "none"


def test_network_inferred_from_the_documents():
    assert ai.infer_network(GRIDSCAN) == "mainnet" and ai.infer_network(MOCK) == "testnet"
    assert ai.infer_network({"paths": {}}) is None


# ── the call ──

class FakeApi:
    def __init__(self, handler):
        self.handler, self.requests = handler, []

    async def resolve(self, host, port):
        return ["104.21.1.1"]

    def _respond(self, request):
        self.requests.append(request)
        status, body = self.handler(request)
        return httpx.Response(status, content=json.dumps(body).encode() if not isinstance(body, bytes) else body)

    def __enter__(self):
        ai.NETWORK.update(resolver=self.resolve, client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(self._respond), follow_redirects=False))
        return self

    def __exit__(self, *exc):
        ai.NETWORK.update(resolver=None, client_factory=None)


INTEG = {"base_url": "https://ada.api.gridscan.io", "auth_type": "header", "auth_name": "x-project-id"}
EPS = {e["id"]: e for e in GS_EPS}


@pytest.mark.parametrize("ep,status,body,expect,reason", [
    ("GET /api/txs/{hash}", 200, {"fees": "0"}, "ok", ""),
    ("GET /api/txs/{hash}", 404, {"detail": "x"}, "not_found", "http_404"),         # 404 documented: no such tx
    ("GET /api/addresses/{address}", 404, {"detail": "x"}, "not_found", "http_404"),  # described in the text
    ("GET /api/network", 404, {"detail": "x"}, "unavailable", "endpoint"),          # not documented: broken setup
    ("GET /api/txs/{hash}", 200, {}, "not_found", "empty"),
    ("GET /api/accounts/{stake_address}/rewards", 200, [], "ok", ""),              # an empty list is a real answer
    ("GET /api/txs/{hash}", 401, {}, "unavailable", "auth"),
    ("GET /api/txs/{hash}", 500, {}, "unavailable", "server"),
    ("GET /api/txs/{hash}", 429, {}, "unavailable", "rate_limited"),
    ("GET /api/txs/{hash}", 200, b"<html>", "unavailable", "not_json"),
])
def test_call_endpoint_statuses(ep, status, body, expect, reason):
    values = {"GET /api/txs/{hash}": {"hash": TX}, "GET /api/addresses/{address}": {"address": ADDR},
              "GET /api/accounts/{stake_address}/rewards": {"stake_address": STAKE}}.get(ep, {})
    with FakeApi(lambda req: (status, body)):
        res = run(r.call_endpoint(INTEG, EPS[ep], values, "k"))
    assert (res["status"], res["reason"]) == (expect, reason)


def test_call_sends_key_only_to_the_api_newest_first_and_short_pages():
    with FakeApi(lambda req: (200, [])) as net:
        res = run(r.call_endpoint(INTEG, EPS["GET /api/accounts/{stake_address}/rewards"], {"stake_address": STAKE}, "secret-key"))
    req = net.requests[0]
    assert req.url.path == f"/api/accounts/{STAKE}/rewards" and req.url.params["order"] == "desc" and req.url.params["count"] == "10"
    assert req.headers["x-project-id"] == "secret-key" and "secret-key" not in res["request_url"]
    assert req.headers["host"] == "ada.api.gridscan.io" and req.url.host == "104.21.1.1"


def test_call_blocks_private_destinations():
    ai.NETWORK.update(resolver=lambda h, p: asyncio.sleep(0, result=["10.1.2.3"]), client_factory=None)
    try:
        res = run(r.call_endpoint(INTEG, EPS["GET /api/network"], {}, None))
    finally:
        ai.NETWORK.update(resolver=None, client_factory=None)
    assert res["status"] == "unavailable" and res["reason"] == "blocked"


# ── formatting ──

def fmt(path, data, channel="web", fields=None, target=None, status="ok", question=""):
    return r.format_result({"status": status, "data": data, "message": "The API had a server error (HTTP 500)."},
                           provider="GridScan", endpoint=EPS["GET " + path], channel=channel, fields=fields, target=target,
                           question=question)


def test_single_result_labelled_list_with_documented_units():
    out = fmt("/api/txs/{hash}", example("/api/txs/{hash}"), target={"kind": "tx_hash", "value": TX})
    t = out["text"]
    assert out["kind"] == "single" and t.startswith("GridScan — Tx Info for ed3551406390…837ae125")
    assert "Fees: 0.172365 ADA" in t                                                  # lovelace -> ADA
    assert "Block time: 2026-11-05 19:47:43 UTC" in t                                 # UNIX time documented
    assert "Hash: " not in t                                                          # the value asked about
    assert "…and" not in t and len(t.splitlines()) <= r.MAX_SHOWN + 1                 # short, no "more fields" line
    # amounts and times first; empty values and zero counts nobody asked about are left out
    body = t.splitlines()[1:]
    assert body[0].startswith(("Block time", "Fees", "Output amount")) and body[1].startswith(("Block time", "Fees", "Output amount"))
    assert "Deposit" not in t and "Delegation count" not in t and "Invalid before" not in t
    # ...but every other field is kept for the bot's follow-up answers
    assert "Input count: " in out["details"] and "Size: " in out["details"]


def test_single_result_puts_what_was_asked_first_and_keeps_a_real_zero():
    out = fmt("/api/txs/{hash}", example("/api/txs/{hash}"), target={"kind": "tx_hash", "value": TX},
              question=f"what was the deposit and fee of transaction {TX}?")
    body = out["text"].splitlines()[1:]
    assert {body[0], body[1]} == {"Fees: 0.172365 ADA", "Deposit: 0 ADA"}              # asked: first, and a real 0 is shown


def test_nested_lists_shown_as_tables():
    data = example("/api/txs/{hash}/utxos")
    out = fmt("/api/txs/{hash}/utxos", data, channel="discord", target={"kind": "tx_hash", "value": TX},
              question=f"show me the inputs for transaction {TX}")
    t = out["text"]
    assert "Inputs:" in t and "Outputs:" not in t and "```" in t                        # the list asked about, as a table
    assert "Amount" in t and "ADA" in t and "Address" in t
    assert f"{len(data['inputs'])} in total." in t or "Showing" in t
    both = fmt("/api/txs/{hash}/utxos", data, target={"kind": "tx_hash", "value": TX})["text"]
    assert "Inputs:" in both and "Outputs:" in both


def test_amount_lists_and_nested_objects():
    t = fmt("/api/addresses/{address}", example("/api/addresses/{address}"), target={"kind": "payment_address", "value": ADDR})["text"]
    assert "Amount: 404.551473 ADA + 1 other token" in t and ADDR not in t
    t = fmt("/api/network", example("/api/network"))["text"]
    assert "Tip epoch: 665" in t and "Tip time: 2026-11-05" in t


def test_repeated_results_per_channel():
    rows = example("/api/accounts/{stake_address}/rewards")
    web = fmt("/api/accounts/{stake_address}/rewards", rows)["text"]
    assert "• Amount: 0.234492 ADA · Epoch: 414 · Type: member" in web and "```" not in web and "|" not in web
    discord = fmt("/api/accounts/{stake_address}/rewards", rows, "discord")["text"]
    assert discord.startswith("**GridScan — Account Rewards**") and "```\nAmount" in discord and "0.39653 ADA   415" in discord
    api = fmt("/api/accounts/{stake_address}/rewards", rows, "api")["text"]
    assert "| Amount | Epoch | Type | Pool id |" in api and "| 0.234492 ADA | 414 | member |" in api
    many = fmt("/api/accounts/{stake_address}/rewards", rows * 8)["text"]
    assert "Showing 10 of 16." in many


def test_zero_empty_no_match_and_unavailable_are_distinct():
    zero = fmt("/api/accounts/{stake_address}/rewards", [{"amount": "0", "epoch": 500, "type": "member", "pool_id": "pool1x"}])
    assert zero["kind"] == "list" and "Amount: 0 ADA" in zero["text"]
    empty = fmt("/api/accounts/{stake_address}/rewards", [], target={"kind": "stake_address", "value": STAKE})
    assert empty["kind"] == "empty" and "No account rewards yet: GridScan answered with an empty list for this stake address." in empty["text"]
    nf = fmt("/api/txs/{hash}", None, status="not_found", target={"kind": "tx_hash", "value": TX})
    assert nf["kind"] == "not_found" and "No match: GridScan has no record of this transaction hash." in nf["text"]
    un = fmt("/api/txs/{hash}", None, status="unavailable")
    assert un["kind"] == "unavailable" and "Lookup unavailable" in un["text"] and "says nothing about whether it exists" in un["text"]


def test_overrides_set_labels_order_units_and_precision():
    fields = ai.validate_fields([{"path": "fees", "label": "Fee", "unit": "ADA", "scale": 6, "decimals": 2},
                                 {"path": "block_height", "label": "Block"}])
    t = fmt("/api/txs/{hash}", example("/api/txs/{hash}"), "discord", fields=fields)["text"]
    assert t.splitlines()[1:] == ["• **Fee:** 0.17 ADA", "• **Block:** 14,162,625"]
    rows = fmt("/api/accounts/{stake_address}/rewards", example("/api/accounts/{stake_address}/rewards"),
               fields=ai.validate_fields([{"path": "epoch", "label": "Epoch"}, {"path": "amount", "label": "Reward", "unit": "ADA", "scale": 6, "decimals": 3}]))["text"]
    assert "• Epoch: 414 · Reward: 0.234 ADA" in rows


def test_override_validation():
    with pytest.raises(ai.ConfigError, match="precision"):
        ai.validate_fields([{"path": "fees", "decimals": 40}])


def test_documented_fields_for_the_editor():
    f = {x["path"]: x for x in r.documented_fields(EPS["GET /api/accounts/{stake_address}/rewards"])}
    assert f["amount"]["unit"] == "ADA" and f["amount"]["scale"] == 6 and "epoch" in f
    f = {x["path"]: x for x in r.documented_fields(EPS["GET /api/network"])}
    assert "tip.epoch" in f and f["tip.time"]["format"] == "timestamp"


def test_providers_on_different_networks():
    assert decide("What's the network status?", (GS, BF))[1] == "Do you mean Cardano mainnet (GridScan) or testnet (Preprod mock)?"
    assert decide("What's the mainnet network status?", (GS, BF)) == ("call", "GET /api/network")
    assert decide("preprod network status", (GS, BF)) == ("call", "GET /network")
    assert decide(f"show block {TX}", (GS, BF))[0] == "ask"
    assert decide(f"show mainnet block {TX}", (GS, BF)) == ("call", "GET /api/blocks/{hash_or_number}")
    same_net = {**BF, "integration": {**BF["integration"], "network": "mainnet"}}
    assert decide("What's the network status?", (GS, same_net)) == ("call", "GET /api/network")   # first provider


def test_blockfrost_style_lovelaces_plural():
    eps = {e["id"]: e for e in MOCK_EPS}
    rows = MOCK["paths"]["/accounts/{stake_address}/rewards"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    t = r.format_result({"status": "ok", "data": rows}, provider="Preprod mock", endpoint=eps["GET /accounts/{stake_address}/rewards"])["text"]
    assert "Amount: 1.25 ADA" in t and "Amount: 0 ADA" in t


# ── imported vs usable by the bot ──

def test_bot_usability_explains_endpoints_the_bot_cannot_fill():
    by_id = {e["id"]: r.bot_usability(e) for e in GS_EPS}
    assert by_id["GET /api/addresses/{address}"] == {"usable": True, "accepts": ["payment_address"], "needs": [["payment_address"]], "reason": None}
    assert by_id["GET /api/network"]["usable"] is True and by_id["GET /api/network"]["accepts"] == []
    assert by_id["GET /api/governance/proposals/{tx_hash}/{cert_index}/votes"]["needs"] == [["tx_hash"], ["proposal_index"]]
    # spec 019: pools, epochs, slots, committee credentials and proposals are now usable
    for ep_id in ("GET /api/pools/{pool_id}", "GET /api/epochs/{number}", "GET /api/blocks/slot/{slot_number}",
                  "GET /api/blocks/epoch/{epoch_number}/slot/{slot_number}", "GET /api/governance/committee/members/{credential}",
                  "GET /api/governance/proposals/{tx_hash}/{cert_index}/votes", "GET /api/governance/dreps/{drep_id}",
                  "GET /api/metadata/txs/labels/{label}"):
        assert by_id[ep_id]["usable"], ep_id
    asset = {"method": "GET", "path": "/api/assets/{asset}", "parameters": [{"name": "asset", "in": "path", "required": True,
             "description": "Native-asset unit: the 56-character policy id immediately followed by the hex-encoded asset name"}]}
    assert r.bot_usability(asset)["reason"]["category"] == "asset"
    # the same rule as the router: usable exactly when the requirements can be filled
    for e in GS_EPS:
        reqs = r.endpoint_requirements(e)
        assert r.bot_usability(e)["usable"] == (reqs is not None and len(reqs) <= r.MAX_REQUIRED), e["id"]
    assert r.bot_usability({"method": "POST", "path": "/x"})["reason"]["category"] == "method"
    four = {"method": "GET", "path": "/txs/{hash}/{address}/{stake}/{pool}", "parameters": [
        {"name": "hash", "in": "path", "required": True, "description": "Transaction hash"},
        {"name": "address", "in": "path", "required": True, "description": "Payment address"},
        {"name": "stake", "in": "path", "required": True, "description": "Stake address"},
        {"name": "pool", "in": "path", "required": True, "description": "Pool id"}]}
    assert r.bot_usability(four)["reason"]["category"] == "multiple"


# ── spec 019: pool, DRep, committee and governance ids; numbers; follow-up answers ──

import re  # noqa: E402

_RAW = json.dumps(GRIDSCAN)
POOL = re.findall(r"pool1[02-9ac-hj-np-z]{50,}", _RAW)[0]            # ids from GridScan's documented examples
CC_COLD = re.findall(r"cc_cold1[02-9ac-hj-np-z]{50,}", _RAW)[0]
CC_HOT = re.findall(r"cc_hot1[02-9ac-hj-np-z]{50,}", _RAW)[0]
GOV = re.findall(r"gov_action1[02-9ac-hj-np-z]{50,}", _RAW)[0]
DREP = r.bech32_encode("drep", bytes([0x22]) + bytes(range(28)))   # CIP-129 key-hash DRep
DREP_LEGACY = r.bech32_encode("drep", bytes(range(28)))             # CIP-105


def test_parse_cardano_ids():
    assert r.parse_cardano_id(POOL)["kind"] == "pool_id"
    assert r.parse_cardano_id(CC_COLD)["kind"] == r.parse_cardano_id(CC_HOT)["kind"] == "committee_credential"
    assert r.parse_cardano_id(DREP)["kind"] == r.parse_cardano_id(DREP_LEGACY)["kind"] == "drep_id"
    g = r.parse_cardano_id(GOV)
    hrp, payload = r.bech32_decode(GOV)
    assert g["kind"] == "gov_action" and g["tx_hash"] == payload[:32].hex() and g["index"] == payload[32]
    for bad, reason in ((POOL[:-1] + ("q" if POOL[-1] != "q" else "p"), "checksum"),
                        (r.bech32_encode("pool", bytes(27)), "wrong length"),
                        (r.bech32_encode("drep", bytes([0x12]) + bytes(28)), "header"),     # a CC cold header on a DRep
                        (r.bech32_encode("gov_action", bytes(32)), "wrong length")):
        with pytest.raises(ValueError, match=reason):
            r.parse_cardano_id(bad)


def test_detects_new_inputs():
    kinds = lambda q: [(i["kind"], i["value"]) for i in r.detect_inputs(q)["inputs"]]
    assert kinds(f"pool {POOL}") == [("pool_id", POOL)]
    hrp, payload = r.bech32_decode(GOV)
    assert kinds(f"votes on {GOV}") == [("tx_hash", payload[:32].hex()), ("proposal_index", str(payload[32]))]
    assert kinds(f"tally {TX}#2") == [("tx_hash", TX.lower()), ("proposal_index", "2")]
    assert kinds("epoch 512") == [("epoch_number", "512")] and kinds("epoch #512") == [("epoch_number", "512")]
    assert kinds("block in epoch 512 slot 1000") == [("epoch_number", "512"), ("slot_number", "1000")]
    assert kinds("block 11000000") == [("block_number", "11000000")] and kinds("metadata label 674") == [("metadata_label", "674")]
    for q in ("top 10 pools", "send 2 ADA", "epoch 5.5", "my 3 wallets"):     # numbers need their cue word
        assert kinds(q) == [], q
    assert kinds(f"block {TX}") == [("block_hash", TX.lower())]                 # a 64-hex hash isn't a block number
    bad = r.detect_inputs(f"pool {POOL[:-1]}x")
    assert bad["inputs"] == [] and bad["invalid"][0]["kind"] == "pool_id"


def test_proposal_hashes_skip_saved_lookup():
    for q in (f"votes on proposal {TX}", f"tally {TX}#0", f"votes on {GOV}"):
        assert r.saved_lookup_hash(r.detect_inputs(q)) is None, q
    assert r.saved_lookup_hash(r.detect_inputs(TX)) == TX.lower()


@pytest.mark.parametrize("question,endpoint", [
    (f"pool info {POOL}", "GET /api/pools/{pool_id}"),
    (f"history of {POOL}", "GET /api/pools/{pool_id}/history"),
    (f"how did pool {POOL} vote", "GET /api/governance/pools/{pool_id}/votes"),
    (f"voting power of {POOL}", "GET /api/governance/pools/{pool_id}/voting_power"),
    (f"drep {DREP}", "GET /api/governance/dreps/{drep_id}"),
    (f"who delegates to {DREP}", "GET /api/governance/dreps/{drep_id}/delegators"),
    (f"votes by {DREP_LEGACY}", "GET /api/governance/dreps/{drep_id}/votes"),
    (f"committee member {CC_COLD}", "GET /api/governance/committee/members/{credential}"),
    (f"votes of {CC_HOT}", "GET /api/governance/committee/members/{credential}/votes"),
    (f"proposal {GOV}", "GET /api/governance/proposals/{tx_hash}/{cert_index}"),
    (f"votes on {GOV}", "GET /api/governance/proposals/{tx_hash}/{cert_index}/votes"),
    (f"tally for {GOV}", "GET /api/governance/proposals/{tx_hash}/{cert_index}/tally"),
    (f"timeline of {GOV}", "GET /api/governance/proposals/{tx_hash}/{cert_index}/timeline"),
    (f"votes on proposal {TX}#0", "GET /api/governance/proposals/{tx_hash}/{cert_index}/votes"),
    (f"tally of proposal {TX} index 0", "GET /api/governance/proposals/{tx_hash}/{cert_index}/tally"),
    ("epoch 512", "GET /api/epochs/{number}"),
    ("blocks in epoch 512", "GET /api/epochs/{number}/blocks"),
    ("slot 123456789", "GET /api/blocks/slot/{slot_number}"),
    ("block in epoch 512 slot 1000", "GET /api/blocks/epoch/{epoch_number}/slot/{slot_number}"),
    ("block 11000000", "GET /api/blocks/{hash_or_number}"),
    ("transactions with metadata label 674", "GET /api/metadata/txs/labels/{label}"),
    (f"fee for tx {TX}", "GET /api/txs/{hash}"),                                 # unchanged
])
def test_routes_new_inputs(question, endpoint):
    assert decide(question) == ("call", endpoint)


def test_multi_value_call_fills_every_parameter():
    d = r.route("block in epoch 512 slot 1000", [GS])
    assert d["values"] == {"epoch_number": "512", "slot_number": "1000"}
    hrp, payload = r.bech32_decode(GOV)
    d = r.route(f"votes on {GOV}", [GS])
    assert d["values"] == {"tx_hash": payload[:32].hex(), "cert_index": str(payload[32])}


@pytest.mark.parametrize("question,ask,expect", [
    (f"votes on proposal {TX}", "Which proposal index in that transaction? It's usually 0.", "proposal_index"),
    ("What are my staking rewards?", "Which stake address (stake1…) should I look up?", "stake_address"),
    ("who is my drep?", "Which stake address (stake1…) should I look up?", "stake_address"),   # never "which DRep"
])
def test_asks_for_the_missing_value(question, ask, expect):
    d = r.route(question, [GS])
    assert (d["action"], d["question"], d["expect"]) == ("ask", ask, expect)


@pytest.mark.parametrize("question", ["top 10 pools", "How do DReps work?", "what is a stake pool?", "tell me about epoch boundaries"])
def test_new_kinds_leave_knowledge_questions_alone(question):
    assert decide(question)[0] == "none"


def test_follow_up_answers_complete_the_lookup():
    # bare number gets the cue of the kind asked for
    q = f"votes on proposal {TX}"
    combined = r.follow_up_text(q, "0", "proposal_index")
    assert combined == f"{q} index 0" and decide(combined) == ("call", "GET /api/governance/proposals/{tx_hash}/{cert_index}/votes")
    combined = r.follow_up_text("What are my staking rewards?", STAKE, "stake_address")
    assert decide(combined) == ("call", "GET /api/accounts/{stake_address}/rewards")
    assert decide(r.follow_up_text(TX, "block", None)) == ("call", "GET /api/blocks/{hash_or_number}")
    assert decide(r.follow_up_text(TX, "the transaction", None)) == ("call", "GET /api/txs/{hash}")
    # not answers
    assert r.follow_up_text(q, "thanks!", "proposal_index") is None
    assert r.follow_up_text(q, "512", None) is None                     # a bare number needs a number question
    assert r.follow_up_text(q, " ".join(["word"] * 13), None) is None    # too long to be an answer
    assert r.answers_expected("0", "proposal_index") and r.answers_expected(STAKE, "stake_address")
    assert not r.answers_expected(ADDR, "stake_address") and not r.answers_expected("0", None)


# ── what the bot can look up, for its prompt ──

def test_capability_note_lists_inputs_and_live_data():
    note = r.capability_note([GS])
    assert note.startswith("You can look up live data from GridScan (Cardano mainnet).")
    for part in ("a stake address (stake1…)", "a Cardano address (addr1…)", "a transaction hash", "a block hash",
                 "the network status", '"What\'s the network status?"'):
        assert part in note, part
    off = {**GS, "endpoints": [e for e in GS_EPS if r.bot_usability(e)["accepts"]]}       # no no-input endpoints
    assert "network status" not in r.capability_note([off])


def test_capability_note_saved_lookup_only_and_nothing():
    assert r.capability_note([]) == ""
    note = r.capability_note([], saved_tx_lookup=True)
    assert "saved lookup" in note and "a transaction hash" in note and "stake address" not in note


# ── spec 020: follow-ups about the last thing looked up ──

def test_subject_text_round_trips_through_the_router():
    d = r.route(f"what was the fee for transaction {TX}?", [GS])
    subj = r.subject_text(d["inputs"], d["values"])
    assert subj == f"transaction {TX.lower()}"
    assert r.detect_inputs(subj)["inputs"][0]["kind"] == "tx_hash"
    d = r.route(f"votes on {GOV}", [GS])
    assert r.subject_text(d["inputs"], d["values"]).startswith("proposal ")


@pytest.mark.parametrize("question,endpoint", [
    ("Can you show me the inputs for this transaction?", "GET /api/txs/{hash}/utxos"),
    ("and the outputs?", "GET /api/txs/{hash}/utxos"),
    ("what about its metadata?", "GET /api/txs/{hash}/metadata"),
    ("how big was it in bytes?", "GET /api/txs/{hash}"),                      # no concept word: the record again
])
def test_follow_up_about_the_last_transaction(question, endpoint):
    assert r.about_subject(question)
    assert decide(f"{question} transaction {TX}") == ("call", endpoint)


@pytest.mark.parametrize("question", [
    "What is an epoch?", "thanks!", "how do I stake my ADA?", f"fee of transaction {TX}", "hello there", "What are fees on Cardano?",
    "thanks, that's great", "why did it fail?", "How does this work?",
])
def test_not_about_the_last_subject(question):
    assert not r.about_subject(question)
