"""Web chat responses report the GridScan Agent lookup (for Embed Code → Live Preview).
No server, database or network needed:
    cd backend && python -m pytest tests/test_web_chat_lookup_unit.py -q
server.py is imported with placeholder settings (it doesn't connect at import time)."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
for k, v in {"MONGO_URL": "mongodb://127.0.0.1:9/none", "DB_NAME": "none", "JWT_SECRET": "x",
             "ADMIN_EMAIL": "a@example.com", "ADMIN_PASSWORD": "x", "APP_MODE": "api"}.items():
    os.environ.setdefault(k, v)
import server  # noqa: E402

CALL = lambda ok=True: {"tool": "address_utxos", "status_code": 200 if ok else 400, "success": ok, "duration_ms": 5,
                        "arguments": {"address": "addr1secret"}}


class Coll:
    def __init__(self, doc=None):
        self.doc, self.inserted = doc, []

    async def insert_one(self, d):
        self.inserted.append(d)

    async def find_one(self, *a, **k):
        return self.doc


@pytest.fixture
def agent(monkeypatch):
    fake_db = SimpleNamespace(api_lookup_log=Coll(), bot_config=Coll({"instance_id": "i"}))
    monkeypatch.setattr(server, "db", fake_db)
    monkeypatch.setattr(server.agentconn, "agent_url", lambda: "http://agent")
    monkeypatch.setattr(server, "_api_lookup_allowed", lambda instance_id: True)
    state = {"result": {"ok": True, "reply": "1 UTxO.", "tool_calls": [CALL()], "duration_ms": 9}}

    async def chat(message, session, *a, **k):
        return state["result"]
    monkeypatch.setattr(server.agentconn, "chat", chat)
    return state


def lookup(source, text="utxos?"):
    return asyncio.run(server._agent_lookup_context("i", text, source, f"{source}:s1", {"id": "d"}))


@pytest.mark.parametrize("result,expected", [
    ({"ok": True, "tool_calls": [CALL()]}, {"status": "ok", "calls": 1}),
    ({"ok": True, "tool_calls": [CALL(), CALL(False)]}, {"status": "partial", "calls": 2}),
    ({"ok": True, "reply": "From memory.", "tool_calls": []}, {"status": "no_call", "calls": 0}),
    ({"ok": False, "error": "timed out", "tool_calls": []}, {"status": "unavailable", "calls": 0}),
])
def test_web_lookup_reports_the_agent_call(agent, result, expected):
    agent["result"] = result
    assert lookup("web")["lookup"] == {"provider": "GridScan Agent", **expected}


@pytest.mark.parametrize("source", ["discord", "api"])
def test_other_channels_dont_get_it(agent, source):
    assert "lookup" not in lookup(source)


def test_summary_has_no_arguments(agent):
    assert "addr1secret" not in str(lookup("web")["lookup"])


def test_raw_web_reply_also_reports_it(agent):
    agent["result"] = {"ok": True, "tool_calls": [CALL()], "raw_json": [{"tool": "address_utxos", "endpoint": "GET x", "data": []}]}
    out = lookup("web", "/raw utxos")
    assert out["reply"] and out["lookup"]["calls"] == 1


def _send(monkeypatch, looked):
    async def ret(value, *a, **k):
        return value
    monkeypatch.setattr(server, "db", SimpleNamespace(bot_config=Coll({"instance_id": "i"})))
    monkeypatch.setattr(server, "search_knowledge", lambda *a, **k: ret("kb"))
    monkeypatch.setattr(server, "_api_lookup_context", lambda *a, **k: ret(looked))
    monkeypatch.setattr(server, "get_tone_examples_text", lambda *a, **k: ret(""))
    monkeypatch.setattr(server, "call_claude", lambda *a, **k: ret("It has 1 UTxO."))
    monkeypatch.setattr(server, "save_conversation", lambda *a, **k: ret(None))
    return asyncio.run(server.send_chat_message(server.ChatMessage(message="utxos?", session_id="s1", instance_id="i")))


def test_chat_send_returns_the_lookup(monkeypatch):
    summary = {"provider": "GridScan Agent", "status": "ok", "calls": 1}
    res = _send(monkeypatch, {"context": "data", "reply": None, "hint": "", "lookup": summary})
    assert res == {"response": "It has 1 UTxO.", "session_id": "s1", "lookup": summary}


def test_chat_send_unchanged_without_an_agent_lookup(monkeypatch):
    res = _send(monkeypatch, {"context": "", "reply": None, "hint": ""})
    assert res == {"response": "It has 1 UTxO.", "session_id": "s1"}
