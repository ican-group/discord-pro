"""Email delivery fail-safe behaviour (spec 004 Phase 2b security fix).

Production: no credential is ever printed or logged; missing configuration and
provider failures raise EmailDeliveryError (-> generic 503), never a fake success.
Development: the labelled console fallback is used instead.
"""
import asyncio
import logging
import unittest

from tests.unit import _support  # noqa: F401
from email_delivery import EmailDeliveryError, EmailService, UNAVAILABLE_DETAIL

CODE = "482913"                                   # synthetic
LINK = "https://api.bitbotpro.io/invite/Zx9_synthetic_invite_token_value_0123456789"
HTML = f"<p>Your code is {CODE}</p><a href='{LINK}'>x</a>"


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def make(app_env, api_key="re_synthetic_key", send_exc=None):
    sent, printed = [], []
    log = logging.getLogger(f"test.email.{app_env}.{id(sent)}")
    log.propagate = False
    handler = _ListHandler()
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    def send_fn(params):
        if send_exc:
            raise send_exc
        sent.append(params)
        return {"id": "msg_1"}

    svc = EmailService(app_env=app_env, api_key_fn=lambda: api_key, send_fn=send_fn,
                       from_email="noreply@bitbotpro.io", logger=log, console=printed.append)
    return svc, sent, printed, handler.lines


def send(svc, label="Code", value=CODE):
    return asyncio.run(svc.send_or_dev_fallback("verification", "user@example.org",
                                                "Subject", HTML, label, value))


class ProductionTests(unittest.TestCase):
    def assertNoCredential(self, *texts):
        blob = "\n".join(texts)
        self.assertNotIn(CODE, blob)
        self.assertNotIn(LINK, blob)
        self.assertNotIn("invite_token", blob)

    def test_missing_api_key_fails_before_state_change(self):
        svc, sent, printed, logs = make("production", api_key="")
        with self.assertRaises(EmailDeliveryError):
            svc.require_available()

    def test_whitespace_api_key_counts_as_missing(self):
        svc, *_ = make("production", api_key="   ")
        with self.assertRaises(EmailDeliveryError):
            svc.require_available()

    def test_missing_api_key_send_raises_and_prints_nothing(self):
        svc, sent, printed, logs = make("production", api_key="")
        with self.assertRaises(EmailDeliveryError):
            send(svc)
        self.assertEqual(sent, [])
        self.assertEqual(printed, [])
        self.assertNoCredential(*logs)

    def test_provider_failure_raises_generic_error_and_logs_type_only(self):
        provider_msg = f"provider echoed body {CODE} {LINK}"
        svc, sent, printed, logs = make("production", send_exc=RuntimeError(provider_msg))
        with self.assertRaises(EmailDeliveryError) as ctx:
            send(svc)
        self.assertEqual(str(ctx.exception), "")
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertEqual(printed, [])
        self.assertTrue(any("error=RuntimeError" in line for line in logs))
        self.assertNoCredential(*logs)
        self.assertFalse(any(provider_msg in line for line in logs))

    def test_invitation_link_never_printed_or_logged(self):
        svc, sent, printed, logs = make("production", send_exc=ConnectionError("boom"))
        with self.assertRaises(EmailDeliveryError):
            send(svc, label="Link", value=LINK)
        self.assertEqual(printed, [])
        self.assertNoCredential(*logs)

    def test_success_sends_and_logs_no_credential(self):
        svc, sent, printed, logs = make("production")
        send(svc)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ["user@example.org"])
        self.assertIn(CODE, sent[0]["html"])  # the code goes only into the email itself
        self.assertEqual(printed, [])
        self.assertNoCredential(*logs)

    def test_dev_console_is_hard_disabled_in_production(self):
        svc, sent, printed, logs = make("production")
        svc._dev_console("verification", "user@example.org", "Code", CODE)
        self.assertEqual(printed, [])

    def test_generic_detail_does_not_claim_success(self):
        self.assertNotIn("sent", UNAVAILABLE_DETAIL.lower())
        self.assertIn("unavailable", UNAVAILABLE_DETAIL.lower())


class DevelopmentTests(unittest.TestCase):
    def test_missing_key_allowed_and_uses_labelled_console(self):
        svc, sent, printed, logs = make("development", api_key="")
        svc.require_available()  # does not raise in development
        send(svc)
        self.assertEqual(sent, [])
        self.assertEqual(len(printed), 1)
        self.assertIn("DEVELOPMENT ONLY", printed[0])
        self.assertIn(CODE, printed[0])
        self.assertFalse(any(CODE in line for line in logs))  # logs stay clean even in dev

    def test_provider_failure_falls_back_in_development(self):
        svc, sent, printed, logs = make("development", send_exc=RuntimeError("x"))
        send(svc)
        self.assertEqual(len(printed), 1)
        self.assertIn("DEVELOPMENT ONLY", printed[0])

    def test_success_in_development_does_not_print(self):
        svc, sent, printed, logs = make("development")
        send(svc)
        self.assertEqual(len(sent), 1)
        self.assertEqual(printed, [])


if __name__ == "__main__":
    unittest.main()
