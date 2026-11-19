"""Typed runtime errors and the only texts end users ever see (spec 012 §10).

Provider, storage and adapter exceptions never reach an end user: they are
mapped to a fixed category and a fixed message; details stay in redacted logs.
"""
from __future__ import annotations


class RuntimeFailure(Exception):
    category = "internal"


class NoRoute(RuntimeFailure):
    category = "no_route"


class AmbiguousRoute(RuntimeFailure):
    """More than one instance could own the event: fail closed (D-Q5)."""
    category = "ambiguous_binding"


class BindingMismatch(RuntimeFailure):
    """Claim and instance document disagree: fail closed."""
    category = "binding_mismatch"


class ProviderError(RuntimeFailure):
    category = "provider_error"


class ProviderAuthError(ProviderError):
    category = "provider_auth"


class ProviderRateLimited(ProviderError):
    category = "provider_rate_limited"


class ProviderUnavailable(ProviderError):
    """The provider definitely did not produce a generation (connection refused,
    5xx/overloaded response, request rejected)."""
    category = "provider_unavailable"


class ProviderUncertain(ProviderError):
    """Part C (Phase 4K review): the request may have reached the provider and a
    generation may have happened (read timeout after sending, connection lost
    mid-response, an unusable 200 response). Never retried automatically and
    never released as "not billed"."""
    category = "provider_uncertain"


class QuotaExceeded(RuntimeFailure):
    category = "quota_exceeded"


MESSAGES = {
    "busy": "I'm receiving a lot of messages right now. Please try again in a moment.",
    "rate_limited": "You're sending messages too quickly. Please wait a moment and try again.",
    "quota_exceeded": "This assistant has reached its usage limit for now. Please try again later.",
}


__all__ = ["RuntimeFailure", "NoRoute", "AmbiguousRoute", "BindingMismatch", "ProviderError", "ProviderAuthError",
           "ProviderRateLimited", "ProviderUnavailable", "QuotaExceeded", "MESSAGES"]
