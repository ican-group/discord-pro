"""Zendesk adapter: guarded REST client, OAuth helpers, credential handling and
deletion cleanup (Phase 4E; parity Z1–Z18).

Every outbound request reuses the Spec 009–011 protections of the kept
``outbound_guard`` module, never a weaker copy:

* the URL is built only by ``outbound_guard.zendesk_url``: the subdomain is
  a single DNS label of ``zendesk.com`` (no scheme/host/port/credentials/
  path injection, no IP literals or obfuscated IPs) and the path must match
  a strict pattern (no ``//``, no ``..`` segments, checked here);
* the host is resolved **once**; *every* answer must be a public unicast
  address (private, loopback, link-local, metadata, reserved, mapped/6to4/
  Teredo/NAT64-embedded addresses are refused), and the connection is
  **pinned** to the checked address, so DNS rebinding cannot swap in an
  internal address between check and use;
* TLS SNI, certificate verification and the ``Host`` header use the real
  host name; callers cannot set ``Host`` or any other routing header;
* redirects are never followed (a 3xx is an error), proxies and environment
  configuration are ignored, response bodies are capped.

Tokens appear only in the ``Authorization`` header; errors are fixed
categories without upstream content; httpx request logging is held at
WARNING. Tests use ``httpx.MockTransport`` and a fake resolver only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

import outbound_guard as og

TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
MAX_RESPONSE = 2_000_000
USER_AGENT = "BitBotPro-Zendesk/1.0"
OAUTH_SCOPES = "read write"
REFRESH_WINDOW = timedelta(minutes=5)

for _name in ("httpx", "httpcore"):
    if logging.getLogger(_name).level < logging.WARNING:
        logging.getLogger(_name).setLevel(logging.WARNING)


class ZendeskError(Exception):
    """Safe category only (never tokens, URLs or Zendesk response text)."""

    def __init__(self, category: str, retry_after: int = 0):
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after


def _check_path(path: str) -> str:
    if not isinstance(path, str) or "/../" in path or path.endswith("/..") or "/./" in path or "\\" in path:
        raise ZendeskError("blocked")
    return path


async def guarded_call(subdomain: str, method: str, path: str, *, headers: Dict[str, str], json_body: Any = None,
                       form: Optional[Dict[str, str]] = None, transport=None, resolver=None) -> Tuple[int, Any]:
    """One policy-checked request. Returns (status, parsed JSON or None)."""
    try:
        url = og.zendesk_url(subdomain, _check_path(path))
        host = og.zendesk_host(subdomain)
        address = await og.resolve_pinned_address(host, 443, allow_private=False, resolver=resolver,
                                                   unsafe_error=og.DESTINATION_NOT_ALLOWED_ERROR)
    except og.OutboundBlocked:
        raise ZendeskError("blocked") from None
    pinned_url, host_header, extensions = og.pinned_request(f"https://{host}", address, url[len(f"https://{host}"):])
    # callers can never set routing/forwarding headers (Host, X-Forwarded-*, …), in any letter case
    safe = {k: v for k, v in headers.items() if k.lower() not in og.FORBIDDEN_HEADER_NAMES}
    send = {"User-Agent": USER_AGENT, "Accept": "application/json", **safe, **host_header}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False,
                                     trust_env=False, verify=True) as client:
            async with client.stream(method, pinned_url, headers=send, json=json_body, data=form,
                                     extensions=extensions) as resp:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise ZendeskError("response_too_large")
                status = resp.status_code
                retry_after = resp.headers.get("retry-after", "")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise ZendeskError("connect_failed") from None          # never reached Zendesk
    except (httpx.TimeoutException, httpx.TransportError):
        raise ZendeskError("uncertain") from None               # may have been processed
    if 300 <= status < 400:
        raise ZendeskError("redirect_blocked")
    try:
        data = json.loads(bytes(body)) if body else None
    except ValueError:
        data = None
    if 200 <= status < 300:
        return status, data
    if status == 401:
        raise ZendeskError("unauthorized")
    if status == 403:
        raise ZendeskError("forbidden")
    if status == 404:
        raise ZendeskError("not_found")
    if status == 409:
        raise ZendeskError("conflict")
    if status == 429:
        try:
            ra = int(float(retry_after or 60))
        except ValueError:
            ra = 60
        raise ZendeskError("rate_limited", retry_after=max(1, min(ra, 3600)))
    if status >= 500:
        raise ZendeskError("server_error")
    raise ZendeskError("bad_request")


class ZendeskClient:
    """API client for one Zendesk account (OAuth bearer or email/API token)."""

    def __init__(self, subdomain: str, *, bearer: Optional[str] = None, email: Optional[str] = None,
                 api_token: Optional[str] = None, transport=None, resolver=None, fence=None):
        try:
            self.subdomain = og.normalize_zendesk_subdomain(subdomain)
        except og.OutboundBlocked:
            raise ZendeskError("blocked") from None
        if bearer:
            self._auth = f"Bearer {bearer}"
        elif email and api_token:
            raw = f"{email}/token:{api_token}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        else:
            raise ZendeskError("credential_unavailable")
        self._transport, self._resolver = transport, resolver
        self._fence = fence           # Z-G: async context manager factory taken around EVERY request

    def __repr__(self) -> str:
        return f"ZendeskClient({self.subdomain}, ****)"

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        if self._fence is None:
            return await self._call(method, path, body)
        async with self._fence():
            return await self._call(method, path, body)

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        _, data = await guarded_call(self.subdomain, method, path,
                                     headers={"Authorization": self._auth, "Content-Type": "application/json"},
                                     json_body=body, transport=self._transport, resolver=self._resolver)
        return data

    async def me(self) -> Dict[str, Any]:
        data = await self.call("GET", "/api/v2/users/me.json")
        user = (data or {}).get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise ZendeskError("unexpected_response")
        return user

    async def ticket(self, ticket_id: str) -> Dict[str, Any]:
        data = await self.call("GET", f"/api/v2/tickets/{ticket_id}.json")
        t = (data or {}).get("ticket") if isinstance(data, dict) else None
        if not isinstance(t, dict):
            raise ZendeskError("unexpected_response")
        return t

    async def comments(self, ticket_id: str) -> List[Dict[str, Any]]:
        data = await self.call("GET", f"/api/v2/tickets/{ticket_id}/comments.json")
        items = (data or {}).get("comments") if isinstance(data, dict) else None
        return [c for c in items or [] if isinstance(c, dict)][-50:]

    async def add_comment(self, ticket_id: str, *, body: str, public: bool, tags: List[str],
                          updated_stamp: Optional[str]) -> Optional[str]:
        """Z10/Z11: one comment (+ tags, *added*, never replacing existing tags).
        ``safe_update`` + ``updated_stamp`` make a retry after an unknown outcome
        safe: if the first attempt applied, the stamp is stale and Zendesk
        answers 409 instead of posting a second comment."""
        ticket: Dict[str, Any] = {"comment": {"body": body, "public": bool(public)}}
        if tags:
            ticket["additional_tags"] = list(tags)
        if updated_stamp:
            ticket.update(safe_update=True, updated_stamp=updated_stamp)
        data = await self.call("PUT", f"/api/v2/tickets/{ticket_id}.json", {"ticket": ticket})
        audit = (data or {}).get("audit") if isinstance(data, dict) else None
        return str(audit.get("id")) if isinstance(audit, dict) and audit.get("id") is not None else None

    async def create_webhook(self, name: str, endpoint: str) -> str:
        data = await self.call("POST", "/api/v2/webhooks", {"webhook": {
            "name": name[:100], "endpoint": endpoint, "http_method": "POST", "request_format": "json",
            "status": "active", "subscriptions": ["conditional_ticket_events"]}})
        hook = (data or {}).get("webhook") if isinstance(data, dict) else None
        if not isinstance(hook, dict) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", str(hook.get("id", ""))):
            raise ZendeskError("unexpected_response")
        return str(hook["id"])

    async def webhook_signing_secret(self, webhook_id: str) -> str:
        data = await self.call("GET", f"/api/v2/webhooks/{webhook_id}/signing_secret")
        sec = ((data or {}).get("signing_secret") or {}).get("secret") if isinstance(data, dict) else None
        if not isinstance(sec, str) or not 16 <= len(sec) <= 256:
            raise ZendeskError("unexpected_response")
        return sec

    async def get_webhook(self, webhook_id: str) -> Dict[str, Any]:
        data = await self.call("GET", f"/api/v2/webhooks/{webhook_id}")
        hook = (data or {}).get("webhook") if isinstance(data, dict) else None
        if not isinstance(hook, dict):
            raise ZendeskError("unexpected_response")
        return hook

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.call("DELETE", f"/api/v2/webhooks/{webhook_id}")

    async def revoke_current_token(self) -> None:
        await self.call("DELETE", "/api/v2/oauth/tokens/current.json")


# ═════════════════════════════════ OAuth (Z2/Z3) ═════════════════════════════════

def pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(subdomain: str, *, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    from urllib.parse import urlencode
    base = og.zendesk_url(subdomain, "/oauth/authorizations/new")
    return base + "?" + urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                                   "scope": OAUTH_SCOPES, "state": state, "code_challenge": challenge,
                                   "code_challenge_method": "S256"})


async def exchange_code(subdomain: str, *, client_id: str, client_secret: str, code: str, redirect_uri: str,
                        verifier: str, transport=None, resolver=None) -> Dict[str, Any]:
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_\-.]{8,200}", code):
        raise ZendeskError("bad_request")
    _, data = await guarded_call(subdomain, "POST", "/oauth/tokens",
                                 headers={"Content-Type": "application/json"},
                                 json_body={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                            "client_secret": client_secret, "redirect_uri": redirect_uri,
                                            "scope": OAUTH_SCOPES, "code_verifier": verifier},
                                 transport=transport, resolver=resolver)
    return _token_set(data)


async def refresh_tokens(subdomain: str, *, client_id: str, client_secret: str, refresh_token: str,
                         transport=None, resolver=None) -> Dict[str, Any]:
    _, data = await guarded_call(subdomain, "POST", "/oauth/tokens",
                                 headers={"Content-Type": "application/json"},
                                 json_body={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                            "client_id": client_id, "client_secret": client_secret,
                                            "scope": OAUTH_SCOPES},
                                 transport=transport, resolver=resolver)
    return _token_set(data)


def _token_set(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise ZendeskError("oauth_failed")
    out = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or ""}
    if isinstance(data.get("expires_in"), int) and data["expires_in"] > 0:
        out["expires_in"] = data["expires_in"]
    return out


# ═════════════════════════════════ credentials ═════════════════════════════════

def oauth_secret(tokens: Dict[str, Any], now: str) -> str:
    expires = None
    if tokens.get("expires_in"):
        expires = (datetime.fromisoformat(now) + timedelta(seconds=int(tokens["expires_in"]))).isoformat()
    return json.dumps({"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or "",
                       "expires_at": expires})


def client_for(cred_doc: dict, box, *, transport=None, resolver=None, fence=None) -> ZendeskClient:
    """A client for a stored ``zendesk_oauth`` or ``zendesk_api_token`` credential
    (decrypted in memory only)."""
    if not cred_doc or cred_doc.get("status") != "active" or not cred_doc.get("secret_enc"):
        raise ZendeskError("credential_unavailable")
    meta = cred_doc.get("metadata") or {}
    try:
        secret = box.decrypt(cred_doc["secret_enc"])
    except Exception:
        raise ZendeskError("credential_unavailable") from None
    if cred_doc.get("provider") == "zendesk_oauth":
        try:
            token = json.loads(secret)["access_token"]
        except (ValueError, KeyError, TypeError):
            raise ZendeskError("credential_unavailable") from None
        return ZendeskClient(meta.get("subdomain", ""), bearer=token, transport=transport, resolver=resolver,
                             fence=fence)
    if cred_doc.get("provider") == "zendesk_api_token":
        return ZendeskClient(meta.get("subdomain", ""), email=meta.get("email"), api_token=secret,
                             transport=transport, resolver=resolver, fence=fence)
    raise ZendeskError("credential_unavailable")


async def ensure_fresh(store, box, cred_doc: dict, *, platform_client: Optional[dict], now: str, transport=None,
                       resolver=None) -> dict:
    """Z3: refresh an expiring OAuth token. The credential document's version
    is the lock: concurrent refreshers race on compare-and-swap, the loser
    re-reads the winner's tokens (one refresh per token, no lost update)."""
    if cred_doc.get("provider") != "zendesk_oauth":
        return cred_doc
    try:
        secret = json.loads(box.decrypt(cred_doc["secret_enc"]))
    except Exception:
        raise ZendeskError("credential_unavailable") from None
    exp = secret.get("expires_at")
    if not exp or datetime.fromisoformat(now) + REFRESH_WINDOW < datetime.fromisoformat(exp):
        return cred_doc
    if not secret.get("refresh_token") or not platform_client or platform_client.get("status") != "active":
        raise ZendeskError("reauth_required")
    tokens = await refresh_tokens((cred_doc.get("metadata") or {}).get("subdomain", ""),
                                  client_id=(platform_client.get("metadata") or {}).get("client_id", ""),
                                  client_secret=box.decrypt(platform_client["secret_enc"]),
                                  refresh_token=secret["refresh_token"], transport=transport, resolver=resolver)
    from core.hierarchy import ConcurrentModification
    new_secret = oauth_secret(tokens, now)
    try:
        await store.update("credentials", cred_doc["id"], {"secret_enc": box.encrypt(new_secret), "rotated_at": now},
                           expect_version=cred_doc["version"])
    except ConcurrentModification:
        pass                                     # another worker refreshed first: use its tokens
    return await store.get("credentials", cred_doc["id"])


# ═════════════════════════════════ deletion cleanup (D11, X12) ═════════════════════════════════

def zendesk_cleanup_executors(box, *, transport=None, resolver=None) -> Dict[str, Any]:
    from core.deletion import CleanupOutcome, CleanupStatus

    async def remove_webhook(action: Dict[str, Any], ctx) -> CleanupOutcome:
        """Removes only the Zendesk webhook this integration provisioned (Z-C):
        through the provisioning credential (held), only on the account recorded
        at provisioning, and only after a GET confirms it still points at this
        integration. Nothing provisioned ⇒ not required. Shared platform OAuth
        configuration is never touched."""
        t = action.get("target") or {}
        hook_id = t.get("webhook_id")
        if not hook_id:
            return CleanupOutcome(CleanupStatus.NOT_REQUIRED)
        doc = await ctx.credential_doc(action.get("credential_id"))
        if not doc or (doc.get("metadata") or {}).get("subdomain") != t.get("subdomain"):
            return CleanupOutcome(CleanupStatus.RETRY, "credential_unavailable")   # never another account
        try:
            client = client_for(doc, box, transport=transport, resolver=resolver)
            try:
                current = await client.get_webhook(str(hook_id))
            except ZendeskError as exc:
                if exc.category == "not_found":
                    return CleanupOutcome(CleanupStatus.SUCCEEDED)   # already gone: idempotent
                raise
            if f"/api/hooks/zendesk/{t.get('integration_id')}/" not in str(current.get("endpoint") or ""):
                return CleanupOutcome(CleanupStatus.RETRY, "ownership_mismatch")
            await client.delete_webhook(str(hook_id))
        except ZendeskError as exc:
            if exc.category == "not_found":
                return CleanupOutcome(CleanupStatus.SUCCEEDED)
            return CleanupOutcome(CleanupStatus.RETRY, exc.category)
        return CleanupOutcome(CleanupStatus.SUCCEEDED)

    async def revoke_token(action: Dict[str, Any], ctx) -> CleanupOutcome:
        doc = await ctx.credential_doc(action.get("credential_id"))
        if not doc or doc.get("provider") != "zendesk_oauth":
            return CleanupOutcome(CleanupStatus.RETRY, "credential_unavailable")
        try:
            await client_for(doc, box, transport=transport, resolver=resolver).revoke_current_token()
        except ZendeskError as exc:
            if exc.category in ("unauthorized", "not_found"):
                return CleanupOutcome(CleanupStatus.SUCCEEDED)       # token already invalid
            return CleanupOutcome(CleanupStatus.RETRY, exc.category)
        return CleanupOutcome(CleanupStatus.SUCCEEDED)

    return {"zendesk_webhook_subscription_removal": remove_webhook, "zendesk_token_revocation": revoke_token}


__all__ = ["ZendeskClient", "ZendeskError", "guarded_call", "pkce_pair", "authorize_url", "exchange_code",
           "refresh_tokens", "oauth_secret", "client_for", "ensure_fresh", "zendesk_cleanup_executors",
           "OAUTH_SCOPES"]
