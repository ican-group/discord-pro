"""Email delivery with fail-safe production behaviour (spec 004, Phase 2b fix).

Rules:
- Production never prints or logs verification codes, reset codes, invitation
  tokens/links or any other credential, and never logs message bodies or the
  provider's error text (only the exception type).
- If email cannot be delivered in production, EmailDeliveryError is raised; the
  API turns it into a generic 503 and never claims an email was sent.
- The console fallback exists ONLY when APP_ENV=development and is labelled so.

Standard library only; the provider send function is injected (resend in server.py).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import runtime_config as _rc

UNAVAILABLE_DETAIL = "Email delivery is temporarily unavailable. Please try again later."


class EmailDeliveryError(Exception):
    """Email could not be delivered. Deliberately carries no detail."""


class EmailService:
    def __init__(self, *, app_env: str, api_key_fn: Callable[[], str],
                 send_fn: Callable[[dict], object], from_email: str,
                 logger: Optional[logging.Logger] = None,
                 console: Callable[[str], None] = print):
        self.app_env = app_env
        self._api_key_fn = api_key_fn
        self._send_fn = send_fn
        self.from_email = from_email
        self._log = logger or logging.getLogger(__name__)
        self._console = console

    @property
    def dev_fallback_enabled(self) -> bool:
        return self.app_env == _rc.APP_ENV_DEVELOPMENT

    def configured(self) -> bool:
        return bool((self._api_key_fn() or "").strip())

    def require_available(self) -> None:
        """Fail before any state change when email cannot possibly be sent."""
        if not self.configured() and not self.dev_fallback_enabled:
            raise EmailDeliveryError()

    async def deliver(self, kind: str, to_email: str, subject: str, html: str) -> None:
        if not self.configured():
            raise EmailDeliveryError()
        params = {"from": self.from_email, "to": [to_email], "subject": subject, "html": html}
        try:
            await asyncio.to_thread(self._send_fn, params)
        except Exception as e:  # provider text may echo content: log the type only
            self._log.error(f"Email delivery failed (kind={kind}, error={type(e).__name__})")
            raise EmailDeliveryError() from None
        self._log.info(f"Email sent (kind={kind}) to {to_email}")

    async def send_or_dev_fallback(self, kind: str, to_email: str, subject: str, html: str,
                                   label: str, value: str) -> None:
        try:
            await self.deliver(kind, to_email, subject, html)
        except EmailDeliveryError:
            if not self.dev_fallback_enabled:
                raise
            self._log.warning(f"Email not delivered (kind={kind}); DEVELOPMENT console fallback used")
            self._dev_console(kind, to_email, label, value)

    def _dev_console(self, kind: str, to_email: str, label: str, value: str) -> None:
        """DEVELOPMENT ONLY. Hard-disabled outside APP_ENV=development."""
        if not self.dev_fallback_enabled:
            return
        banner = "=" * 60
        self._console(f"\n{banner}\nDEVELOPMENT ONLY (APP_ENV=development) - email NOT delivered\n"
                      f"Kind: {kind}\nTo: {to_email}\n{label}: {value}\n{banner}\n")
