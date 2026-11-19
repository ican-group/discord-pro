"""Telegram channel (Phase 4C): parity T1–T12, webhook security, routing and
tenant isolation, credentials, lifecycle, knowledge modes, quotas, billing
idempotency and deletion cleanup. Fake Telegram Bot API and fake AI provider
only; no live service is contacted."""
import asyncio
import json
import logging
import unittest

from tests.unit._api_fixtures import HAVE_API, TELEGRAM_TOKEN_A, TELEGRAM_TOKEN_B, World
from tests.unit.test_api_webchat import FakeProvider

BOT_A = TELEGRAM_TOKEN_A.split(":")[0]
HEADER = "X-Telegram-Bot-Api-Secret-Token"


class CapturedLogs(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage())


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class TelegramBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = CapturedLogs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)
        self.w = await World().build()
        self.sleeps = []

        async def no_sleep(seconds):
            self.sleeps.append(seconds)
        self.w.S.telegram_sleep = no_sleep
        self.provider = FakeProvider(text="Telegram answer.")
        self.w.S.providers = {"anthropic": self.provider}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        self.I = self.w.iA_tg.id
        self.tg = self.w.telegram
        await self.connect()
        self.update_id = 1000

    async def asyncTearDown(self):
        logging.getLogger().removeHandler(self.logs)
        logging.getLogger().setLevel(logging.WARNING)
        joined = "\n".join(self.logs.text)
        self.assertNotIn(TELEGRAM_TOKEN_A, joined)                 # the bot token never reaches any log
        self.assertNotIn(TELEGRAM_TOKEN_A.split(":")[1], joined)

    async def connect(self, instance=None):
        r = await self.w.call("POST", f"/instances/{instance or self.I}/channel/connect", "admin")
        assert r.status_code == 200, r.text
        url, header = [(c[2]["url"], c[2]["secret_token"]) for c in self.tg.methods("setWebhook")][-1]
        self.path_secret, self.header_secret = url.rsplit("/", 1)[1], header
        self.hook_path = "/hooks" + url.split("/api/hooks", 1)[1]
        return r.json()

    async def hook(self, update, path=None, header="__default__", raw=None, headers=None):
        import httpx
        h = {HEADER: self.header_secret if header == "__default__" else header} if header is not None else {}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            if raw is not None:
                return await c.post("/api" + (path or self.hook_path), content=raw, headers=h)
            return await c.post("/api" + (path or self.hook_path), json=update, headers=h)

    def message(self, text, chat_type="private", chat_id=555, from_id=42, **extra):
        self.update_id += 1
        msg = {"message_id": self.update_id + 5000, "text": text, "chat": {"id": chat_id, "type": chat_type},
               "from": {"id": from_id, "is_bot": False, "first_name": "Ann"}, **extra}
        return {"update_id": self.update_id, "message": msg}

    def sent(self):
        return self.tg.methods("sendMessage")


class T1CredentialTests(TelegramBase):
    async def test_credential_validated_on_save_and_never_exposed(self):
        O = f"/organisations/{self.w.oA.id}/credentials"
        token = "111222333:" + "Q" * 35
        r = await self.w.call("POST", O, "admin", {"provider": "telegram_bot", "name": "New bot", "secret": token})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn(("111222333", "getMe", {}), self.tg.calls)
        self.assertNotIn(token, r.text)
        self.assertNotIn(token, json.dumps(await self.w.snapshot(), default=str))
        self.tg.fail["getMe"] = [(401, {"ok": False, "error_code": 401, "description": "Unauthorized " + token})]
        bad = await self.w.call("POST", O, "admin", {"provider": "telegram_bot", "name": "x",
                                                     "secret": "444555666:" + "R" * 35})
        self.assertEqual((bad.status_code, bad.json()["detail"]), (422, "Telegram did not accept this bot token."))

    async def test_bot_identity_uniqueness_and_credential_test(self):
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                              {"provider": "telegram_bot", "name": "dup", "secret": TELEGRAM_TOKEN_A})
        dup = r.json()["id"]
        r = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "admin",
                              {"name": "dup", "channel": {"type": "telegram", "credential_id": dup}})
        self.assertEqual(r.status_code, 409)                          # one Telegram bot serves one instance
        t = await self.w.call("POST", f"/credentials/{self.w.cA_tg.id}/test", "admin")
        self.assertEqual(t.json(), {"ok": True, "bot_username": f"bot{BOT_A}"})
        self.assertEqual((await self.w.call("POST", f"/credentials/{self.w.cA_tg.id}/test", "editor")).status_code, 403)
        self.assertEqual((await self.w.call("POST", f"/credentials/{self.w.cA_tg.id}/test", "owner_b")).status_code, 404)

    async def test_revocation_removes_the_webhook_and_fails_closed(self):
        before = len(self.tg.methods("deleteWebhook"))
        r = await self.w.call("POST", f"/credentials/{self.w.cA_tg.id}/revoke", "owner")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.tg.methods("deleteWebhook")), before + 1)
        status = (await self.w.call("GET", f"/instances/{self.I}/channel/status", "viewer")).json()
        self.assertEqual((status["connected"], status["registered"]), (False, False))
        r = await self.hook(self.message("hello"))
        self.assertEqual(r.status_code, 404)                          # no longer connected
        self.assertEqual(self.provider.calls, [])


class T2WebhookLifecycleTests(TelegramBase):
    async def test_connect_registers_with_secrets(self):
        reg = self.tg.methods("setWebhook")[-1]
        self.assertEqual(reg[0], BOT_A)
        self.assertTrue(reg[2]["url"].startswith(f"https://api.bitbotpro.io/api/hooks/telegram/{self.I}/"))
        self.assertEqual(reg[2]["allowed_updates"], ["message", "callback_query"])
        self.assertGreaterEqual(len(reg[2]["secret_token"]), 32)
        stored = json.dumps(await self.w.store.get("bot_instances", self.I), default=str)
        self.assertNotIn(self.path_secret, stored)                    # encrypted + hashed only
        self.assertNotIn(self.header_secret, stored)
        status = (await self.w.call("GET", f"/instances/{self.I}/channel/status", "viewer")).json()
        self.assertEqual((status["connected"], status["registered"], status["bot_username"]),
                         (True, True, f"bot{BOT_A}"))

    async def test_lifecycle_drives_the_webhook(self):
        n = len(self.tg.methods("deleteWebhook"))
        await self.w.call("POST", f"/organisations/{self.w.oA.id}/suspend", "owner", {})
        self.assertEqual(len(self.tg.methods("deleteWebhook")), n + 1)
        self.assertFalse((await self.w.call("GET", f"/instances/{self.I}/channel/status", "viewer")).json()["registered"])
        m = len(self.tg.methods("setWebhook"))
        await self.w.call("POST", f"/organisations/{self.w.oA.id}/resume", "owner", {})
        self.assertEqual(len(self.tg.methods("setWebhook")), m + 1)
        await self.w.call("POST", f"/instances/{self.I}/archive", "owner", {})
        self.assertEqual(len(self.tg.methods("deleteWebhook")), n + 2)

    async def test_failed_sync_is_recorded_and_retried_by_the_worker(self):
        from api.telegram import channel_for
        from runtime.worker import Worker
        self.tg.fail["deleteWebhook"] = [(502, {"ok": False})]
        await self.w.call("POST", f"/bots/{self.w.bA.id}/suspend", "owner", {})
        status = (await self.w.call("GET", f"/instances/{self.I}/channel/status", "viewer")).json()
        self.assertEqual((status["registered"], status["sync_error"]), (True, "server_error"))
        report = await Worker(self.w.store, clock=self.w.S.clock, worker_id="tg",
                              telegram=channel_for(self.w.S)).run_once()
        self.assertGreaterEqual(report.telegram_synced, 1)
        status = (await self.w.call("GET", f"/instances/{self.I}/channel/status", "viewer")).json()
        self.assertEqual((status["registered"], status["sync_error"]), (False, None))

    async def test_rotate_and_disconnect(self):
        old_path, old_header = self.path_secret, self.header_secret
        r = await self.w.call("POST", f"/instances/{self.I}/channel/rotate-secret", "admin")
        self.assertEqual(r.status_code, 200)
        url, header = [(c[2]["url"], c[2]["secret_token"]) for c in self.tg.methods("setWebhook")][-1]
        self.assertNotEqual((url.rsplit("/", 1)[1], header), (old_path, old_header))
        old = await self.hook(self.message("hi"), path=f"/hooks/telegram/{self.I}/{old_path}", header=old_header)
        self.assertEqual(old.status_code, 404)
        self.path_secret, self.header_secret, self.hook_path = url.rsplit("/", 1)[1], header, "/hooks" + url.split("/api/hooks", 1)[1]
        self.assertEqual((await self.hook(self.message("hi"))).status_code, 200)
        r = await self.w.call("POST", f"/instances/{self.I}/channel/disconnect", "admin")
        self.assertEqual((r.json()["connected"], r.json()["registered"]), (False, False))
        self.assertEqual((await self.hook(self.message("hi"))).status_code, 404)

    async def test_permissions_and_token_change_while_connected(self):
        for who, status in (("editor", 403), ("viewer", 403), ("owner_b", 404)):
            self.assertEqual((await self.w.call("POST", f"/instances/{self.I}/channel/connect", who)).status_code, status)
        r = await self.w.call("PATCH", f"/instances/{self.I}", "owner",
                              {"channel": {"credential_id": self.w.cA_ai.id}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual((await self.w.call("POST", f"/instances/{self.w.iA_web.id}/channel/connect",
                                            "owner")).status_code, 404)        # not a Telegram instance


class T3WebhookSecurityTests(TelegramBase):
    async def test_authentication(self):
        upd = self.message("hello")
        cases = [dict(path=f"/hooks/telegram/{self.I}/{'x' * 43}"), dict(header=None), dict(header="wrong" * 10),
                 dict(path=f"/hooks/telegram/00000000-0000-4000-8000-000000000000/{self.path_secret}"),
                 dict(path=f"/hooks/telegram/{self.w.iA_web.id}/{self.path_secret}")]
        for kw in cases:
            r = await self.hook(upd, **kw)
            self.assertEqual(r.status_code, 404, kw)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(await self.w.store.count("auth_throttle", {}), 0)   # nothing done before auth

    async def test_size_limit_and_malformed_payloads(self):
        big = await self.hook(None, raw=b"{" + b" " * 300_000 + b"}",
                              headers={"Content-Type": "application/json"})
        self.assertEqual(big.status_code, 413)
        for raw in (b"not json", b"[]", b'{"message": {}}', b'{"update_id": "1"}'):
            r = await self.hook(None, raw=raw, headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400, raw)

    async def test_duplicate_updates_are_processed_once(self):
        upd = self.message("hello")
        for _ in range(3):
            self.assertEqual((await self.hook(upd)).status_code, 200)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(await self.w.store.count("usage_events", {"kind": "llm_call"}), 1)

    async def test_body_bot_ids_are_never_trusted(self):
        # another tenant's instance connected to bot B; a forged body naming bot A changes nothing
        B = self.w.iB_web.id
        r = await self.w.call("POST", f"/organisations/{self.w.oB.id}/bots", "owner_b", {"name": "TG"})
        inst = await self.w.call("POST", f"/bots/{r.json()['id']}/instances", "owner_b",
                                 {"name": "TG B", "channel": {"type": "telegram", "credential_id": self.w.cB_tg.id}})
        self.assertEqual(inst.status_code, 201, inst.text)
        upd = self.message("hello")
        upd["message"]["via_bot"] = {"id": int(BOT_A), "is_bot": True}
        upd["bot_id"] = int(BOT_A)
        r = await self.hook(upd, path=f"/hooks/telegram/{inst.json()['id']}/{self.path_secret}")
        self.assertEqual(r.status_code, 404)                          # A's secrets never authenticate B
        self.assertEqual((await self.hook(upd)).status_code, 200)
        self.assertEqual({c[0] for c in self.sent()}, {BOT_A})         # replies only via A's own bot


class T4ListenModeTests(TelegramBase):
    async def set_channel(self, **ch):
        r = await self.w.call("PATCH", f"/instances/{self.I}", "owner", {"channel": ch})
        self.assertEqual(r.status_code, 200, r.text)

    async def test_mention_or_command_default(self):
        await self.hook(self.message("random chatter", chat_type="group", chat_id=-100))
        self.assertEqual(self.provider.calls, [])
        await self.hook(self.message(f"@bot{BOT_A} what time is it?", chat_type="group", chat_id=-100))
        await self.hook(self.message("/ask opening hours?", chat_type="group", chat_id=-100))
        await self.hook(self.message("follow up", chat_type="group", chat_id=-100,
                                     reply_to_message={"from": {"id": int(BOT_A), "is_bot": True}}))
        await self.hook(self.message("hello in private"))
        self.assertEqual([c.user_text for c in self.provider.calls],
                         ["what time is it?", "opening hours?", "follow up", "hello in private"])

    async def test_dm_only_all_and_allowlist(self):
        await self.set_channel(listen_mode="dm_only")
        await self.hook(self.message("/ask hi", chat_type="group", chat_id=-100))
        self.assertEqual(self.provider.calls, [])
        await self.set_channel(listen_mode="all")
        await self.hook(self.message("anything", chat_type="group", chat_id=-100))
        self.assertEqual(len(self.provider.calls), 1)
        await self.set_channel(chat_ids=["-200"])
        await self.hook(self.message("anything", chat_type="group", chat_id=-100))
        self.assertEqual(len(self.provider.calls), 1)                  # not on the allowlist
        await self.hook(self.message("anything", chat_type="group", chat_id=-200))
        self.assertEqual(len(self.provider.calls), 2)

    async def test_bots_and_empty_messages(self):
        upd = self.message("hi")
        upd["message"]["from"]["is_bot"] = True
        await self.hook(upd)
        await self.hook({"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "photo": []}})
        self.assertEqual(self.provider.calls, [])
        await self.hook(self.message("/ask"))
        self.assertEqual(self.provider.calls, [])
        self.assertIn("Send me a question", self.sent()[-1][2]["text"])  # help, no AI call


class T5SuggestionTests(TelegramBase):
    async def test_suggestion_buttons_round_trip(self):
        self.provider.text = "We open at 9.\n[SUGGESTIONS: When do you close? | Are you open on Sunday?]"
        await self.hook(self.message("When do you open?"))
        payload = self.sent()[-1][2]
        self.assertEqual(payload["text"], "We open at 9.")
        buttons = [row[0] for row in payload["reply_markup"]["inline_keyboard"]]
        self.assertEqual([b["text"] for b in buttons], ["When do you close?", "Are you open on Sunday?"])
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in buttons))
        self.assertTrue(all(b["callback_data"].startswith("s:") for b in buttons))
        self.assertIn("SUGGESTIONS", self.provider.calls[0].instructions)   # format hint for general answers
        self.provider.text = "We close at 5."
        self.update_id += 1
        await self.hook({"update_id": self.update_id, "callback_query": {
            "id": "cb1", "data": buttons[0]["callback_data"], "from": {"id": 42},
            "message": {"message_id": 1, "chat": {"id": 555, "type": "private"}}}})
        self.assertEqual(self.provider.calls[-1].user_text, "When do you close?")
        self.assertTrue(self.tg.methods("answerCallbackQuery"))
        stored = await self.w.store.find("conversations", {"instance_id": self.I})
        self.assertNotIn("[SUGGESTIONS", json.dumps(stored, default=str))

    async def test_forged_or_foreign_callbacks_are_ignored(self):
        self.provider.text = "A.\n[SUGGESTIONS: Next?]"
        await self.hook(self.message("Q"))
        data = self.sent()[-1][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        calls = len(self.provider.calls)
        for cq in ({"data": "s:forged", "chat": 555}, {"data": data, "chat": 999}, {"data": "x" * 70, "chat": 555}):
            self.update_id += 1
            await self.hook({"update_id": self.update_id, "callback_query": {
                "id": "cb", "data": cq["data"], "from": {"id": 42},
                "message": {"message_id": 1, "chat": {"id": cq["chat"], "type": "private"}}}})
        self.assertEqual(len(self.provider.calls), calls)


class T6SendingTests(TelegramBase):
    async def test_long_replies_are_chunked_on_boundaries(self):
        self.provider.text = ("word " * 2000).strip()                  # ~10k characters
        await self.hook(self.message("long please"))
        chunks = [c[2] for c in self.sent()]
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c["text"]) <= 4096 for c in chunks))
        self.assertTrue(all(not c["text"].endswith("wor") for c in chunks))
        self.assertIn("reply_to_message_id", chunks[0])
        self.assertNotIn("reply_to_message_id", chunks[1])
        self.assertEqual(" ".join(c["text"] for c in chunks).split(), self.provider.text.split())

    async def test_markdown_failure_retries_as_plain_text(self):
        self.tg.fail["sendMessage"] = [(400, {"ok": False, "error_code": 400,
                                              "description": "Bad Request: can't parse entities"})]
        await self.hook(self.message("hi"))
        attempts = self.sent()
        self.assertEqual(attempts[-2][2].get("parse_mode"), "Markdown")
        self.assertNotIn("parse_mode", attempts[-1][2])


class T7SessionTests(TelegramBase):
    async def test_one_conversation_per_chat(self):
        await self.hook(self.message("first", chat_id=555))
        await self.hook(self.message("second", chat_id=555, from_id=43))
        await self.hook(self.message("other chat", chat_id=777))
        self.assertEqual(self.provider.calls[1].history, (("user", "first"), ("assistant", "Telegram answer.")))
        self.assertEqual(self.provider.calls[2].history, ())
        convs = await self.w.store.find("conversations", {"instance_id": self.I})
        self.assertEqual(len(convs), 2)
        self.assertTrue(all(c["channel"] == "telegram" and c["enterprise_id"] == self.w.eA.id for c in convs))


class T8ConnectionTestTests(TelegramBase):
    async def test_channel_test(self):
        r = await self.w.call("POST", f"/instances/{self.I}/channel/test", "admin")
        self.assertEqual(r.json(), {"ok": True, "bot_username": f"bot{BOT_A}"})
        self.tg.fail["getMe"] = [(401, {"ok": False, "error_code": 401})]
        r = await self.w.call("POST", f"/instances/{self.I}/channel/test", "admin")
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "Telegram rejected the bot token."))


class T9ErrorHandlingTests(TelegramBase):
    async def test_provider_failures_get_a_generic_apology(self):
        self.provider.error = RuntimeError(f"upstream exploded {TELEGRAM_TOKEN_A}")
        await self.hook(self.message("hi"))
        text = self.sent()[-1][2]["text"]
        self.assertEqual(text, "Sorry, something went wrong. Please try again.")

    async def test_telegram_rate_limits_are_honoured_and_bounded(self):
        self.tg.fail["sendMessage"] = [(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}})]
        await self.hook(self.message("hi"))
        self.assertEqual(self.sleeps, [3])
        self.assertEqual(len(self.sent()), 2)                          # one retry
        self.tg.fail["sendMessage"] = [(429, {"ok": False, "parameters": {"retry_after": 600}})]
        await self.hook(self.message("again"))
        self.assertEqual(self.sleeps, [3])                              # too long: not waited for, dropped safely


class T10T11T12Tests(TelegramBase):
    async def test_start_is_unchanged_and_no_polling_or_suggestion_modes(self):
        await self.hook(self.message("/start"))
        self.assertEqual(self.provider.calls[0].user_text, "/start")     # T10: unchanged (sent as text in DMs)
        self.assertEqual(self.tg.methods("getUpdates"), [])              # T11: no polling
        from core.hierarchy import TelegramBinding
        self.assertEqual(set(TelegramBinding.__dataclass_fields__), {"credential_id", "listen_mode", "chat_ids"})  # T12


class KnowledgeAndLimitTests(TelegramBase):
    async def test_strict_mode_refuses_without_buttons_or_ai(self):
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"knowledge.minimum_mode": "strict_knowledge_only"}})
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"knowledge.insufficient_fallback": "Please ask our team."}})
        self.provider.text = "Invented.\n[SUGGESTIONS: More?]"
        await self.hook(self.message("What is the refund policy?"))
        payload = self.sent()[-1][2]
        self.assertEqual(payload["text"], "Please ask our team.")
        self.assertNotIn("reply_markup", payload)
        self.assertEqual(self.provider.calls, [])

    async def test_per_user_rate_limit_and_quota(self):
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"limits.end_user_msgs_per_minute": 1}})
        await self.hook(self.message("one"))
        await self.hook(self.message("two"))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIn("too quickly", self.sent()[-1][2]["text"])
        await self.hook(self.message("other user", from_id=99))
        self.assertEqual(len(self.provider.calls), 2)
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 2}})
        await self.hook(self.message("third user", from_id=100))
        self.assertEqual(len(self.provider.calls), 2)
        self.assertIn("usage limit", self.sent()[-1][2]["text"])


class CleanupTests(TelegramBase):
    async def delete_instance(self):
        pv = (await self.w.call("POST", f"/instances/{self.I}/deletion-preview", "owner")).json()
        r = await self.w.call("POST", f"/instances/{self.I}/delete", "owner",
                              {"preview_token": pv["preview_token"], "confirm_name": self.w.iA_tg.name})
        return r.json()["id"]

    async def worker(self):
        from runtime.telegram import telegram_cleanup_executors
        from runtime.worker import Worker
        return Worker(self.w.store, clock=self.w.S.clock, worker_id="cleanup",
                      executors=telegram_cleanup_executors(self.w.S.box, transport=self.w.S.telegram_transport))

    async def test_webhook_removal_with_retries(self):
        jid = await self.delete_instance()
        job = await self.w.store.get("deletion_jobs", jid)
        self.assertEqual(job["status"], "awaiting_external_cleanup")
        self.tg.fail["deleteWebhook"] = [(502, {"ok": False}), (502, {"ok": False})]
        n = len(self.tg.methods("deleteWebhook"))
        worker = await self.worker()
        await worker.run_once()
        job = await self.w.store.get("deletion_jobs", jid)
        self.assertEqual(job["external_actions"][0]["state"], "retry_wait")
        from datetime import datetime, timedelta
        for i in range(3):
            later = (datetime.fromisoformat(self.w.S.clock()) + timedelta(hours=2 * (i + 1))).isoformat()
            from runtime.telegram import telegram_cleanup_executors
            from runtime.worker import Worker
            await Worker(self.w.store, clock=lambda later=later: later, worker_id="cleanup",
                         executors=telegram_cleanup_executors(self.w.S.box, transport=self.w.S.telegram_transport)
                         ).run_once()
            job = await self.w.store.get("deletion_jobs", jid)
            if job["status"] == "completed":
                break
        self.assertEqual(job["status"], "completed")
        calls = self.tg.methods("deleteWebhook")[n:]
        self.assertTrue(all(c[0] == BOT_A for c in calls))              # only the deleted instance's bot

    async def test_never_removes_a_webhook_of_a_bot_that_moved_to_another_instance(self):
        jid = await self.delete_instance()
        new = await self.w.call("POST", f"/bots/{self.w.bA.id}/instances", "owner",
                                {"name": "TG again", "channel": {"type": "telegram", "credential_id": self.w.cA_tg.id}})
        self.assertEqual(new.status_code, 201, new.text)                # bot re-bound right away
        n = len(self.tg.methods("deleteWebhook"))
        await (await self.worker()).run_once()
        job = await self.w.store.get("deletion_jobs", jid)
        self.assertEqual(job["external_actions"][0]["state"], "not_required")
        self.assertEqual(len(self.tg.methods("deleteWebhook")), n)
        self.assertEqual(job["status"], "completed")


if __name__ == "__main__":
    unittest.main()
