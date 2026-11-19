"""Zendesk corrections from the Phase 4K review: Z-G durable deletion, Z-C
webhook ownership, Z-D delivery guarantees, Part C generation/billing
idempotency. Fake Zendesk behind the real outbound guard; fake AI provider."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.unit._api_fixtures import HAVE_API, ZD_API_TOKEN, ZD_HOST
from tests.unit.test_api_zendesk import ZendeskBase


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class LifecycleBase(ZendeskBase):
    def svc(self, seconds=0):
        from api.integrations import service_for
        s = service_for(self.w.S)
        if seconds:
            t = (datetime.fromisoformat(self.w.S.clock()) + timedelta(seconds=seconds)).isoformat()
            s.clock = lambda: t
        return s

    async def integ(self):
        return await self.w.store.get("bot_integrations", self.Z)

    async def audits(self, action):
        return await self.w.store.find("audit_events", {"action": action, "target_id": self.Z})

    async def provision(self):
        r = await self.w.call("POST", f"/integrations/{self.Z}/zendesk/webhook/provision", "admin")
        assert r.status_code == 200, r.text
        return r.json()["provisioned_webhook_id"]

    def zd_calls(self, method, suffix):
        return [c for c in self.zd.calls if c[1] == method and c[2].endswith(suffix)]


# ═════════════════════ Z-G: durable deletion ═════════════════════

class DeletionStateMachineTests(LifecycleBase):
    async def test_failed_cleanup_leaves_it_disabled_audited_and_retryable(self):
        hid = await self.provision()
        self.zd.fail[("DELETE", f"/webhooks/{hid}")] = [(503, {}), (503, {})]
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        # a 5xx on DELETE is an unknown outcome: reconciled with a GET before any retry
        self.assertEqual((r.status_code, r.json()["state"]), (502, "cleanup_uncertain"))
        doc = await self.integ()
        self.assertEqual((doc["status"], doc["deletion"]["attempts"], doc["deletion"]["error"]),
                         ("disabled", 1, "server_error"))
        self.assertEqual(len(await self.audits("integration.delete_requested")), 1)
        self.assertEqual(len(await self.audits("integration.delete_cleanup_failed")), 1)
        # disabled: no inbound processing, no sidebar, no re-enable, no changes
        r = await self.webhook(secret=self.zd.webhooks[hid]["secret"])
        self.assertEqual(r.json().get("skipped"), "disabled")
        self.assertEqual(self.provider.calls, [])
        for body in ({"status": "enabled"}, {"name": "x"}, {"settings": {"reply_mode": "auto_reply"}}):
            self.assertEqual((await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", body)).status_code, 409)
        # retry through the API (second failure), then the worker after back-off succeeds
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual(r.status_code, 502)
        self.assertEqual((await self.integ())["deletion"]["attempts"], 2)
        await self.worker(3600)
        self.assertIsNone(await self.integ())
        self.assertNotIn(hid, self.zd.webhooks)
        self.assertEqual(len(await self.audits("integration.delete")), 1)
        self.assertEqual(len(await self.audits("integration.delete_requested")), 1)   # audit preserved
        self.assertEqual(await self.w.store.find("zendesk_app_keys", {"integration_id": self.Z}), [])

    async def test_queued_deliveries_are_never_sent_after_the_request(self):
        from api.integrations import service_for
        from runtime.zendesk_integration import ZendeskService
        await self.settings(reply_mode="auto_reply")
        original = ZendeskService.deliver

        async def crash(self, did):
            return None
        ZendeskService.deliver = crash
        try:
            await self.webhook()
        finally:
            ZendeskService.deliver = original
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual(d["status"], "pending")
        await self.svc().request_deletion(self.Z, actor_id=self.w.users["admin"].id)
        from runtime import delivery as dlv
        out = await dlv.run(self.w.store, d["id"], service_for(self.w.S).send_part, now=self.w.S.clock())
        self.assertEqual((out["status"], out["last_error"]), ("failed_before_send", "integration_unavailable"))
        self.assertEqual(self.zd.public_posts() + self.zd.notes(), [])

    async def test_in_flight_send_fences_the_transition(self):
        s = self.svc()
        ok, _ = await s._lease(self.Z, "sender-1")                      # a send already in progress
        self.assertTrue(ok)
        doc = await s.request_deletion(self.Z, actor_id=self.w.users["admin"].id)
        self.assertEqual((doc["deletion"]["state"], doc["status"]), ("requested", "enabled"))
        ok2, why = await s._lease(self.Z, "sender-2")                     # nothing new may start
        self.assertEqual((ok2, why), (False, "integration_unavailable"))
        self.assertEqual((await self.webhook()).json().get("skipped"), "disabled")
        state = await s.advance_deletion(self.Z)
        self.assertEqual(state, {"state": "requested", "waiting_for": "in_flight_send"})
        await s._unlease(self.Z, "sender-1")
        self.assertEqual((await s.advance_deletion(self.Z))["state"], "deleted")

    # ── Part E (Phase 4F): no Zendesk request may start after the deletion transition ──
    def puts(self):
        return [c for c in self.zd.calls if c[1] == "PUT"]

    async def test_deletion_during_generation_posts_nothing(self):
        await self.settings(reply_mode="auto_reply")
        provider, svc = self.provider, self.svc()
        original = provider.generate

        async def generate_then_delete(request):
            await svc.request_deletion(self.Z, actor_id=self.w.users["admin"].id)
            return await original(request)
        provider.generate = generate_then_delete
        await self.webhook()
        self.assertEqual(self.puts(), [])
        self.assertEqual(await self.suggestions(), [])                   # no new suggestion either
        [l] = await self.w.store.find("usage_ledger", {})
        self.assertEqual(l["state"], "settled")                           # the generation did happen

    async def test_deletion_immediately_before_transmission_wins(self):
        await self.settings(reply_mode="auto_reply")
        from runtime.zendesk_integration import ZendeskService
        original = ZendeskService._lease
        fired = {"n": 0}

        async def lease_races_deletion(self_, integ_id, holder):
            if fired["n"] == 0 and self.zd_calls("GET", "/tickets/101.json"):
                fired["n"] = 1                                            # the deletion lands first
                await self.svc().request_deletion(self.Z, actor_id=self.w.users["admin"].id)
            return await original(self_, integ_id, holder)
        ZendeskService._lease = lease_races_deletion
        try:
            await self.webhook()
        finally:
            ZendeskService._lease = original
        self.assertEqual(self.puts(), [])

    async def test_a_transmitted_request_finishes_but_nothing_new_starts(self):
        await self.settings(reply_mode="auto_reply")
        svc = self.svc()
        state = {"done": False}

        def delete_while_in_flight(request):
            if request.method == "PUT" and not state["done"]:
                state["done"] = True
                import asyncio as _a
                # the deletion is requested while this request is on the wire
                _a.get_event_loop().create_task(svc.request_deletion(self.Z, actor_id=self.w.users["admin"].id))
            return None
        self.zd.intercept = delete_while_in_flight
        await self.webhook()
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.puts()), 1)                            # the public comment already sent
        self.assertEqual(len(self.zd.public_posts()), 1)
        self.assertEqual(self.zd.notes(), [])                            # the audit note never starts
        doc = await self.integ()
        self.assertTrue(doc["deletion"])
        calls = len(self.zd.calls)
        await self.worker(10)                                             # transition, cleanup, finalise
        self.assertIsNone(await self.integ())
        self.assertFalse([c for c in self.zd.calls[calls:] if c[1] in ("PUT", "POST")])

    async def test_deletion_response_never_claims_completion_while_a_comment_is_in_flight(self):
        import httpx
        await self.settings(reply_mode="auto_reply")
        responses = []

        async def delete_on_the_wire(request):
            if request.method == "PUT" and not responses:
                # the comment request is on the wire (its lease is held) while the admin deletes
                responses.append(await self.w.call("DELETE", f"/integrations/{self.Z}", "admin"))
            return self.zd.handle(request)
        self.w.S.zendesk_transport = httpx.MockTransport(delete_on_the_wire)
        await self.webhook()
        r = responses[0]
        self.assertEqual(r.status_code, 202)                              # not "deleted"
        self.assertEqual(r.json()["state"], "requested")
        view = (await self.w.call("GET", f"/integrations/{self.Z}", "viewer")).json()
        self.assertEqual(view["deletion"]["state"], "requested")
        self.assertEqual(view["status"], "enabled")                       # transition waits for the lease
        self.assertEqual(len(self.zd.public_posts()), 1)
        self.assertEqual(self.zd.notes(), [])                             # nothing new began
        await self.worker(10)
        self.assertEqual((await self.w.call("GET", f"/integrations/{self.Z}", "viewer")).status_code, 404)
        self.assertEqual(self.zd.notes(), [])

    async def test_states_visible_to_the_ui(self):
        hid = await self.provision()
        self.zd.fail[("DELETE", f"/webhooks/{hid}")] = [(403, {})]
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual((r.status_code, r.json()["state"]), (502, "cleanup_failed"))
        view = (await self.w.call("GET", f"/integrations/{self.Z}", "viewer")).json()
        self.assertEqual((view["deletion"]["state"], view["deletion"]["error"], view["deletion"]["retryable"]),
                         ("cleanup_failed", "forbidden", True))
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")    # retry succeeds
        self.assertEqual(r.json(), {"deleted": True})

    async def test_crashed_request_lease_delays_the_transition_until_expiry(self):
        s = self.svc()
        ok, _ = await s._lease(self.Z, "crashed-worker")                  # the worker died holding it
        self.assertTrue(ok)
        await s.request_deletion(self.Z, actor_id=self.w.users["admin"].id)
        self.assertEqual((await s.advance_deletion(self.Z))["state"], "requested")
        self.assertEqual((await self.integ())["status"], "enabled")       # not yet confirmed
        later = self.svc(120)                                              # lease (60 s) expired
        self.assertEqual((await later.advance_deletion(self.Z))["state"], "deleted")

    async def test_after_the_transition_no_request_reaches_zendesk(self):
        from runtime.zendesk import ZendeskError
        s = self.svc()
        await s.request_deletion(self.Z, actor_id=self.w.users["admin"].id)
        integ, chain, eff, creds = await s.context(self.Z)
        before = len(self.zd.calls)
        with self.assertRaises(ZendeskError):
            await s.client(chain, integ, eff, creds)
        self.assertEqual(len(self.zd.calls), before)
        r = await self.w.call("POST", f"/integrations/{self.Z}/test", "admin")
        self.assertNotEqual(r.status_code, 200)
        self.assertEqual(len(self.zd.calls), before)

    async def test_crash_points_are_recoverable_and_audit_is_never_lost(self):
        from runtime.zendesk_integration import ZendeskService
        hid = await self.provision()
        s = self.svc()
        # 1. crash before the transition: nothing changed
        original = ZendeskService.request_deletion

        async def boom(self, *a, **k):
            raise RuntimeError("crash")
        ZendeskService.request_deletion = boom
        try:
            r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        except RuntimeError:
            pass
        finally:
            ZendeskService.request_deletion = original
        self.assertFalse((await self.integ()).get("deletion"))
        # 2. crash after disabling, before the audit write
        await s.request_deletion(self.Z, actor_id=self.w.users["admin"].id)
        self.assertEqual((await self.integ())["status"], "disabled")
        self.assertEqual(await self.audits("integration.delete_requested"), [])
        # 3. crash after the audit write, before cleanup (driver stops after the audit step)
        original_rm = ZendeskService._remove_webhook

        async def crash_cleanup(self, doc):
            raise RuntimeError("crash during cleanup")
        ZendeskService._remove_webhook = crash_cleanup
        try:
            await s.advance_deletion(self.Z)
        except RuntimeError:
            pass
        finally:
            ZendeskService._remove_webhook = original_rm
        self.assertEqual(len(await self.audits("integration.delete_requested")), 1)
        self.assertIn(hid, self.zd.webhooks)
        # 4. crash after external cleanup, before final deletion
        original_fin = ZendeskService._finalise

        async def crash_finalise(self, doc):
            raise RuntimeError("crash before final deletion")
        ZendeskService._finalise = crash_finalise
        try:
            await s.advance_deletion(self.Z)
        except RuntimeError:
            pass
        finally:
            ZendeskService._finalise = original_fin
        self.assertNotIn(hid, self.zd.webhooks)
        self.assertEqual((await self.integ())["deletion"]["state"], "finalising")
        # the worker completes it; every audit event exactly once
        await self.worker(10)
        self.assertIsNone(await self.integ())
        for action in ("integration.delete_requested", "integration.delete"):
            self.assertEqual(len(await self.audits(action)), 1, action)
        self.assertEqual(len(self.zd_calls("DELETE", f"/webhooks/{hid}")), 1)

    async def test_uncertain_cleanup_is_reconciled_before_any_retry(self):
        import httpx
        hid = await self.provision()
        state = {"n": 1}

        def lands_then_times_out(request):
            if request.method == "DELETE" and request.url.path.endswith(hid) and state["n"]:
                state["n"] -= 1
                self.zd.handle(request)                                    # Zendesk deleted it
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.zd.intercept = lands_then_times_out
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual(r.json()["state"], "cleanup_uncertain")
        await self.worker(3600)
        self.assertIsNone(await self.integ())
        self.assertEqual(len(self.zd_calls("DELETE", f"/webhooks/{hid}")), 1)   # GET proved it gone: no 2nd DELETE

    async def test_concurrent_deletion_requests_form_one_workflow(self):
        hid = await self.provision()
        results = await asyncio.gather(*[self.w.call("DELETE", f"/integrations/{self.Z}", "admin") for _ in range(5)])
        self.assertTrue(all(r.status_code in (200, 202) for r in results), [r.text for r in results])
        await self.worker(10)
        self.assertIsNone(await self.integ())
        self.assertEqual(len(await self.audits("integration.delete_requested")), 1)
        self.assertEqual(len(await self.audits("integration.delete")), 1)
        self.assertEqual(len(self.zd_calls("DELETE", f"/webhooks/{hid}")), 1)

    async def test_a_webhook_that_no_longer_points_here_is_never_deleted(self):
        hid = await self.provision()
        self.zd.webhooks[hid]["endpoint"] = "https://other.example/hook"     # changed in Zendesk by someone
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual((r.status_code, r.json()["error"]), (502, "ownership_mismatch"))
        self.assertIn(hid, self.zd.webhooks)
        self.assertEqual(self.zd_calls("DELETE", f"/webhooks/{hid}"), [])
        self.assertEqual((await self.integ())["status"], "disabled")


# ═════════════════════ Z-C: webhook ownership ═════════════════════

class WebhookOwnershipTests(LifecycleBase):
    async def other_account_credential(self):
        self.zd.basic.add(("agent@other.example", ZD_API_TOKEN))
        r = await self.w.call("POST", f"/organisations/{self.w.oA.id}/credentials", "admin",
                              {"provider": "zendesk_api_token", "name": "Other", "secret": ZD_API_TOKEN,
                               "metadata": {"subdomain": "other", "email": "agent@other.example"}})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def test_provisioning_records_authoritative_ownership(self):
        hid = await self.provision()
        prov = (await self.integ())["webhook"]["provision"]
        self.assertEqual((prov["enterprise_id"], prov["organisation_id"], prov["integration_id"], prov["subdomain"],
                          prov["credential_id"], prov["webhook_id"], prov["status"]),
                         (self.w.eA.id, self.w.oA.id, self.Z, "acme", self.cred, hid, "active"))

    async def test_credential_switch_is_blocked_while_provisioned(self):
        await self.provision()
        other = await self.other_account_credential()
        for body in ({"credential_id": other}, {"clear_credential": True}):
            r = await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", body)
            self.assertEqual(r.status_code, 409)
        r = await self.w.call("POST", f"/integrations/{self.Z}/zendesk/webhook/deprovision", "admin")
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.w.call("PATCH", f"/integrations/{self.Z}", "admin", {"credential_id": other})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await self.webhook()).status_code, 404)           # no signing secret any more

    async def test_revoked_owner_credential_is_never_replaced_silently(self):
        hid = await self.provision()
        other = await self.other_account_credential()
        await self.w.call("POST", f"/credentials/{self.cred}/revoke", "admin")
        before = len(self.zd.calls)
        r = await self.w.call("DELETE", f"/integrations/{self.Z}", "admin")
        self.assertEqual((r.status_code, r.json()["error"]), (502, "credential_unavailable"))
        self.assertFalse([c for c in self.zd.calls[before:] if c[0] == "other.zendesk.com"])
        self.assertIn(hid, self.zd.webhooks)
        # recovery: another account is refused; the same account is accepted
        r = await self.w.call("PUT", f"/integrations/{self.Z}/zendesk/webhook/owner-credential", "admin",
                              {"credential_id": other})
        self.assertEqual(r.status_code, 422)
        same = await self.api_token_credential()
        r = await self.w.call("PUT", f"/integrations/{self.Z}/zendesk/webhook/owner-credential", "admin",
                              {"credential_id": same})
        self.assertEqual(r.status_code, 200, r.text)
        await self.worker(3600)                                      # the preserved cleanup now succeeds
        self.assertIsNone(await self.integ())
        self.assertNotIn(hid, self.zd.webhooks)

    async def test_deletion_job_cleanup_uses_only_the_provisioning_account(self):
        from runtime.zendesk import zendesk_cleanup_executors
        hid = await self.provision()
        other = await self.other_account_credential()

        class Ctx:
            store = self.w.store

            async def credential_doc(ctx, cid):
                return await self.w.store.get("credentials", cid)
        ex = zendesk_cleanup_executors(self.w.S.box, transport=self.w.S.zendesk_transport,
                                       resolver=self.w.S.zendesk_resolver)["zendesk_webhook_subscription_removal"]
        before = len(self.zd.calls)
        out = await ex({"target": {"integration_id": self.Z, "webhook_id": hid, "subdomain": "acme"},
                        "credential_id": other}, Ctx())                      # wrong account's credential
        self.assertEqual((out.status.value, out.error_class), ("retry", "credential_unavailable"))
        self.assertEqual(self.zd.calls[before:], [])
        out = await ex({"target": {"integration_id": "8f7e6d5c-4b3a-4291-8a7b-6c5d4e3f2a1b", "webhook_id": hid,
                                   "subdomain": "acme"}, "credential_id": self.cred}, Ctx())   # not this integration's
        self.assertEqual(out.error_class, "ownership_mismatch")
        self.assertIn(hid, self.zd.webhooks)


# ═════════════════════ Z-D: delivery guarantees ═════════════════════

class DeliveryGuaranteeTests(LifecycleBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.settings(reply_mode="auto_reply")

    def ours(self, public=True):
        return [c for c in self.zd.comments["101"] if c["author_id"] == 77 and c["public"] == public]

    async def test_unknown_outcome_is_never_retried_and_is_reconciled_when_found(self):
        import httpx
        state = {"n": 1}

        def lands_then_times_out(request):
            if request.method == "PUT" and state["n"]:
                state["n"] -= 1
                self.zd.handle(request)
                raise httpx.ReadTimeout("t", request=request)
            return None
        self.zd.intercept = lands_then_times_out
        await self.webhook()
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual((d["status"], d["parts"][0]["state"], d["parts"][1]["state"]),
                         ("uncertain", "uncertain", "pending"))
        self.assertEqual(len(self.zd_calls("PUT", "/tickets/101.json")), 1)   # no automatic retry
        await self.worker(60)                                        # reconciliation finds the comment
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertTrue(d["parts"][0]["message_id"].startswith("comment:"))
        await self.worker(120)                                       # the never-attempted part is sent
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual(d["status"], "delivered")
        self.assertEqual((len(self.ours(True)), len(self.ours(False))), (1, 1))

    async def test_unknown_outcome_without_evidence_stays_uncertain(self):
        import httpx

        def times_out(request):
            if request.method == "PUT":
                raise httpx.ReadTimeout("t", request=request)                 # did not land
            return None
        self.zd.intercept = times_out
        await self.webhook()
        self.zd.intercept = None
        for t in (60, 600, 7200):
            await self.svc(t).maintain()
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual(d["status"], "uncertain")
        self.assertEqual(self.ours(True), [])                                 # never resent

    async def test_5xx_is_an_unknown_outcome_too(self):
        self.zd.fail[("PUT", "/tickets/101.json")] = [(502, {})]
        await self.webhook()
        [d] = await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
        self.assertEqual(d["status"], "uncertain")
        self.assertEqual(len(self.zd_calls("PUT", "/tickets/101.json")), 1)


# ═════════════════════ Part C: generation and billing idempotency ═════════════════════

class GenerationIdempotencyTests(LifecycleBase):
    async def event(self, event_id=None):
        from runtime.zendesk_integration import event_doc_id
        return await self.w.store.get("zendesk_events", event_doc_id(self.Z, event_id or "ticket:101:created"))

    async def ledger(self):
        return await self.w.store.find("usage_ledger", {})

    async def test_definite_failure_retries_under_the_same_identity_and_bills_once(self):
        from runtime.errors import ProviderUnavailable
        self.provider.error = ProviderUnavailable()
        await self.webhook()
        ev = await self.event()
        self.assertEqual((ev["status"], ev["generation"]["state"]), ("received", "failed_before_generation"))
        [l] = await self.ledger()
        self.assertEqual((l["id"], l["state"]), (f"zd:{ev['id']}", "released"))
        self.provider.error = None
        await self.worker(60)
        [l] = await self.ledger()
        self.assertEqual((l["state"], l["attempts"]), ("settled", 2))         # same key, one settlement
        self.assertEqual(len(await self.w.store.find("usage_events", {"kind": "llm_call"})), 1)
        self.assertEqual((await self.event())["generation"]["state"], "settled")

    async def test_uncertain_provider_outcome_is_never_regenerated(self):
        from runtime.errors import ProviderUncertain
        self.provider.error = ProviderUncertain()
        await self.webhook()
        self.provider.error = None
        for t in (60, 600, 7200):
            await self.svc(t).maintain()
        self.assertEqual(len(self.provider.calls), 1)
        ev = await self.event()
        self.assertEqual((ev["status"], ev["generation"]["state"]), ("uncertain", "uncertain"))
        [l] = await self.ledger()
        self.assertEqual(l["state"], "uncertain")
        self.assertEqual(self.zd.notes() + self.zd.public_posts(), [])
        r = await self.w.call("GET", f"/integrations/{self.Z}/events?status=uncertain", "admin")
        self.assertEqual([e["id"] for e in r.json()["events"]], [ev["id"]])
        self.assertEqual((await self.w.call("GET", f"/integrations/{self.Z}/events", "owner_b")).status_code, 404)

    async def test_operator_resolution_charge_or_waive(self):
        from runtime.errors import ProviderUncertain
        await self.w.call("PATCH", f"/enterprises/{self.w.eA.id}/policy", "owner",
                          {"changes": {"limits.max_monthly_ai_messages": 1}})
        self.provider.error = ProviderUncertain()
        await self.webhook({"ticket_id": 101, "event_id": "e1"})
        self.provider.error = None
        await self.webhook({"ticket_id": 101, "event_id": "e2"})              # quota held by the uncertain one
        self.assertEqual((await self.event("e2"))["label"], "quota_exceeded")
        path = f"/integrations/{self.Z}/events/{(await self.event('e1'))['id']}/resolve"
        self.assertEqual((await self.w.call("POST", path, "viewer", {"decision": "waive", "note": "x"})).status_code, 403)
        self.assertEqual((await self.w.call("POST", path, "owner_b", {"decision": "waive", "note": "x"})).status_code, 404)
        r = await self.w.call("POST", path, "admin", {"decision": "waive", "note": "provider confirmed no output"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.w.call("POST", path, "admin", {"decision": "charge", "note": "x"})).status_code, 409)
        await self.webhook({"ticket_id": 101, "event_id": "e3"})              # quota returned by the waiver
        self.assertEqual((await self.event("e3"))["label"], "answer")
        self.assertEqual(len(self.provider.calls), 2)

    async def test_charge_settles_exactly_once(self):
        from runtime.errors import ProviderUncertain
        self.provider.error = ProviderUncertain()
        await self.webhook()
        path = f"/integrations/{self.Z}/events/{(await self.event())['id']}/resolve"
        r = await self.w.call("POST", path, "admin", {"decision": "charge", "note": "assume generated"})
        self.assertEqual(r.status_code, 200)
        [l] = await self.ledger()
        self.assertEqual(l["state"], "settled")
        self.assertEqual(len(await self.w.store.find("usage_events", {"kind": "llm_call"})), 1)

    async def test_concurrent_workers_generate_once(self):
        from runtime.zendesk_integration import ZendeskService
        original = ZendeskService.process

        async def no_inline(self, eid):
            return "deferred"
        ZendeskService.process = no_inline
        try:
            await self.webhook()
        finally:
            ZendeskService.process = original
        await asyncio.gather(*[self.svc().maintain() for _ in range(5)])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([l["state"] for l in await self.ledger()], ["settled"])
        self.assertEqual(len(self.zd.notes()), 1)

    async def test_crash_after_generation_before_storage_becomes_uncertain(self):
        from runtime.zendesk_integration import ZendeskService
        original = ZendeskService._set

        async def lose_store(self, eid, from_states, **fields):
            if fields.get("status") == "generated":
                return False                                                # the store write is lost
            return await original(self, eid, from_states, **fields)
        ZendeskService._set = lose_store
        try:
            await self.webhook()
        finally:
            ZendeskService._set = original
        ev = await self.event()
        self.assertEqual(ev["status"], "uncertain")
        [l] = await self.ledger()
        self.assertEqual(l["state"], "uncertain")                            # never settled, never released
        await self.worker(3600)
        self.assertEqual(len(self.provider.calls), 1)

    async def test_crash_after_storage_before_settlement_settles_once_later(self):
        from runtime.usage import StoreUsageTracker
        original = StoreUsageTracker.settle

        async def down(self, *a, **k):
            raise RuntimeError("ledger unavailable")
        StoreUsageTracker.settle = down
        try:
            await self.webhook()
        except Exception:
            pass
        finally:
            StoreUsageTracker.settle = original
        self.assertEqual((await self.event())["status"], "generated")
        self.assertEqual(self.zd.notes(), [])
        await self.worker(10)
        await self.worker(20)
        self.assertEqual([l["state"] for l in await self.ledger()], ["settled"])
        self.assertEqual(len(await self.w.store.find("usage_events", {"kind": "llm_call"})), 1)
        self.assertEqual(len(self.zd.notes()), 1)
        self.assertEqual(len(self.provider.calls), 1)

    async def test_crash_before_and_during_outbound_delivery(self):
        from runtime import delivery as dlv
        from runtime.zendesk_integration import ZendeskService
        original = ZendeskService.deliver

        async def crash(self, did):
            return None
        ZendeskService.deliver = crash
        try:
            await self.webhook({"ticket_id": 101, "event_id": "before"})
        finally:
            ZendeskService.deliver = original
        await self.worker(10)                                         # sent once by the worker
        self.assertEqual(len(self.zd.notes()), 1)
        # during: a part left "sending" by a dead worker becomes uncertain, never blindly resent
        self.zd.add_ticket(102)
        ZendeskService.deliver = crash
        try:
            await self.webhook({"ticket_id": 102, "event_id": "during"})
        finally:
            ZendeskService.deliver = original
        d = next(x for x in await self.w.store.find("outbound_deliveries", {"integration_id": self.Z})
                 if x["status"] == "pending")
        parts = [dict(p, state="sending") if p["index"] == 0 else p for p in d["parts"]]
        await self.w.store.update("outbound_deliveries", d["id"], {"status": "sending", "parts": parts,
                                                                   "lease_until": "2020-01-01T00:00:00+00:00"})
        await self.worker(10)
        self.assertEqual((await self.w.store.get("outbound_deliveries", d["id"]))["status"], "uncertain")
        self.assertEqual([c for c in self.zd.comments["102"] if c["author_id"] == 77], [])

    async def test_replays_are_deduplicated_and_new_events_on_the_same_ticket_are_not(self):
        for event_id in ("audit-1", "audit-1", "audit-2", "audit-1"):
            await self.webhook({"ticket_id": 101, "event_id": event_id})
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(await self.w.store.find("usage_events", {"kind": "llm_call"})), 2)
        self.assertEqual(len(self.zd.notes()), 2)

    async def test_identities_are_tenant_scoped(self):
        from runtime.zendesk_integration import event_doc_id, generation_key
        bz = await self.integration(bot=self.w.bB.id, who="owner_b")
        self.assertNotEqual(generation_key(event_doc_id(self.Z, "e")), generation_key(event_doc_id(bz, "e")))
        await self.webhook({"ticket_id": 101, "event_id": "e"})
        [l] = await self.ledger()
        self.assertEqual(l["enterprise_id"], self.w.eA.id)


if __name__ == "__main__":
    unittest.main()
