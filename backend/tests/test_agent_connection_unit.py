"""Spec 024: GridScan Agent connection helpers (no network, no server)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_connection as ac  # noqa: E402


@pytest.mark.parametrize("text", [
    "/raw show the utxos for addr1xyz",
    "show me the raw json",
    "Show me the raw JSON for that transaction",
    "give me the raw API response",
    "raw response please",
    "show me the json",
    "Show me the JSON of that",
    "can you display the api json",
    "I want the exact JSON the API returned",
    "the full API response for this address please",
    "post the complete payload",
    "dump json",
    "paste the API response verbatim",
])
def test_raw_requests(text):
    assert ac.wants_raw(text)


@pytest.mark.parametrize("text", [
    "Run a fresh UTxO lookup for addr1xyz. Reply with one JSON code block containing txHash, outputIndex, lovelace, and assets for each output.",
    "show me the details of that transaction",
    "utxos?",
    "what's the latest block?",
    "is the JSON format documented anywhere?",
    "how do I parse json from the api in python",
    "",
])
def test_not_raw_requests(text):
    assert not ac.wants_raw(text)


def test_agent_message_adds_raw_prefix_once():
    assert ac.agent_message("show me the raw json", True) == "/raw show me the raw json"
    assert ac.agent_message("/raw latest block", True) == "/raw latest block"
    assert ac.agent_message("utxos?", False) == "utxos?"
    again = ac.agent_message("/raw that transaction", True, refetch=True)
    assert again.startswith("/raw Call the API again now") and again.endswith("that transaction") and again.count("/raw") == 1


def test_session_is_scoped_to_bot_channel_and_user():
    a = ac.session_id("bot1", "discord:user1:chan1")
    assert a == ac.session_id("bot1", "discord:user1:chan1")          # stable
    assert len({a, ac.session_id("bot2", "discord:user1:chan1"), ac.session_id("bot1", "discord:user2:chan1"),
                ac.session_id("bot1", "discord:user1:chan2")}) == 4


def _joined(messages):
    """The JSON text of Discord messages, as a reader would copy it out of the code blocks."""
    out = []
    for m in messages:
        body = m.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        out.append(body)
    return "".join(out)


def test_small_payload_is_one_unmodified_message():
    data = [{"tx_hash": "a" * 64, "output_index": 1, "amount": [{"unit": "lovelace", "quantity": "1176630"}]}]
    d = ac.raw_discord([{"tool": "address_utxos", "data": data}], at="2026-11-25 22:00:00 UTC")
    assert d["files"] == [] and len(d["messages"]) == 1
    assert _joined(d["messages"]) == json.dumps(data, indent=2, ensure_ascii=False)
    assert "unmodified" in d["messages"][0] and "address_utxos" in d["messages"][0]


def test_long_payload_is_split_into_labelled_parts_with_nothing_lost():
    data = [{"tx_hash": f"{i:064x}", "output_index": i, "note": "x" * 60} for i in range(60)]
    d = ac.raw_discord([{"tool": "address_utxos", "data": data}], at="t")
    msgs = d["messages"]
    assert 1 < len(msgs) <= ac.DISCORD_INLINE_MAX_PARTS and not d["files"]
    assert all(len(m) <= 1990 - 40 for m in msgs)                         # room for a mention
    assert all(m.count("```") == 2 for m in msgs)                         # each part is its own code block
    assert f"Part 1/{len(msgs)}" in msgs[0] and f"Part {len(msgs)}/{len(msgs)}" in msgs[-1]
    assert json.loads(_joined(msgs)) == data
    assert _joined(msgs) == json.dumps(data, indent=2, ensure_ascii=False)


def test_backticks_cannot_break_the_code_block_and_parse_the_same():
    data = {"name": "a`b```c"}
    d = ac.raw_discord([{"tool": "asset", "data": data}], at="t")
    assert d["messages"][0].count("```") == 2
    assert json.loads(_joined(d["messages"])) == data


def test_very_long_payload_is_attached_complete_not_cut():
    data = [{"i": i, "pad": "y" * 200} for i in range(200)]
    d = ac.raw_discord([{"tool": "pools list", "data": data}], at="t")
    assert len(d["files"]) == 1 and d["files"][0]["filename"] == "pools-list.json"
    assert json.loads(d["files"][0]["content"]) == data
    assert "attached" in d["messages"][0] and len(d["messages"]) == 1


def test_agent_truncation_is_reported_not_silent():
    cut = json.dumps([{"i": i, "pad": "z" * 100} for i in range(1000)], indent=2)[:50000] + "\n... (truncated)"
    d = ac.raw_discord([{"tool": "big", "data": cut}], at="t")
    assert "cut this response at 50,000 characters" in d["messages"][0]
    text = ac.raw_text([{"tool": "big", "data": cut}], at="t", markdown=False)
    assert "incomplete" in text and text.endswith("... (truncated)")


def test_several_payloads_are_numbered():
    d = ac.raw_discord([{"tool": "tx", "data": {"a": 1}}, {"tool": "tx_utxos", "data": {"b": 2}}], at="t")
    assert "(1 of 2)" in d["messages"][0] and "(2 of 2)" in d["messages"][1]


def test_web_raw_text_is_complete_plain_json():
    data = {"hash": "b" * 64, "fees": "170000"}
    text = ac.raw_text([{"tool": "tx", "data": data}], at="t", markdown=False)
    assert "```" not in text and text.split("\n", 1)[1] == json.dumps(data, indent=2)


def test_no_payload_reply_never_passes_the_model_text_off_as_api_data():
    r = ac.no_payload_reply({"reply": "Which address do you mean?", "tool_calls": []})
    assert "didn't call the API" in r and "Which address do you mean?" in r
    written = ac.no_payload_reply({"reply": "Here it is:\n```json\n{\"hash\": \"abc\"}\n```", "tool_calls": []})
    assert "didn't call the API" in written and "abc" not in written and "```" not in written


def test_lookup_block_states_calls_and_failures():
    ok = {"ok": True, "reply": "The address holds 2 UTxOs.", "tool_calls": [
        {"tool": "address_utxos", "status_code": 200, "success": True, "duration_ms": 300, "arguments": {"address": "addr1"}}]}
    block = ac.lookup_block(ok, at="t", reply_format="Discord")
    assert "fresh lookup at t" in block and "address_utxos → HTTP 200" in block and "addr1" not in block.split("AGENT ANSWER")[0]
    assert "The address holds 2 UTxOs." in block
    none = ac.lookup_block({"ok": True, "reply": "It moved 224.3 ADA.", "tool_calls": []}, at="t", reply_format="x")
    assert "no fresh API call" in none and "earlier lookup" in none and "It moved 224.3 ADA." in none
    assert "fresh lookup at" not in none
    down = ac.lookup_block({"ok": False, "error": "timed out"}, at="t", reply_format="x")
    assert "lookup unavailable" in down


def test_call_summary_drops_arguments():
    s = ac.call_summary([{"tool": "address_utxos", "status_code": 200, "success": True, "duration_ms": 5,
                          "endpoint": "/api/addresses/addr1secret/utxos", "arguments": {"address": "addr1secret"}}])
    assert "addr1secret" not in json.dumps(s)


@pytest.mark.parametrize("text,fresh", [
    ("Look up transaction abc", True), ("check the latest block", True), ("run that lookup again", True),
    ("show me the details of that transaction", False), ("utxos?", False), ("thanks", False),
])
def test_wants_fresh(text, fresh):
    assert ac.wants_fresh(text) is fresh
