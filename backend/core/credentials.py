"""Credential ownership and references (spec 012 §8).

* Tenant credentials belong to an Enterprise or an Organisation; platform
  credentials (the shared Discord application, the Zendesk OAuth client) belong
  to the platform and are managed by superadmins only.
* Configuration never contains secrets — only credential ids
  (``SecurityClass.CREDENTIAL_REF``). Secrets are encrypted at rest and are
  write-only: :meth:`Credential.public_view` is the only representation that
  leaves this module, and ``repr`` never includes the secret.
* *Use* (referencing a credential from a bot/instance/integration) and *manage*
  (create, replace, revoke, delete) are separate permissions.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, Optional, Tuple

from core.authz import Action, Actor, AuthzError, Membership, can
from core.hierarchy import Chain, Level, is_valid_id


class CredentialError(ValueError):
    """Credential cannot be used. Messages never reveal other tenants' data."""


class Provider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    TELEGRAM_BOT = "telegram_bot"
    ZENDESK_OAUTH = "zendesk_oauth"          # tokens obtained through the platform OAuth client
    ZENDESK_API_TOKEN = "zendesk_api_token"  # email + API token (D-Q9)
    API_INTEGRATION = "api_integration"      # spec 011 integrations


class PlatformCredentialKind(str, Enum):
    # D-Q14 (2026-11-18): there is no global Discord application. Each Enterprise has
    # its own, administered only by platform superadmins (``runtime.discord`` app store).
    ZENDESK_OAUTH_CLIENT = "zendesk_oauth_client"


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


# Which credential providers a configuration/binding slot accepts.
SLOT_PROVIDERS = {
    "ai.anthropic": (Provider.ANTHROPIC,),
    "ai.openai": (Provider.OPENAI,),
    "channel.telegram": (Provider.TELEGRAM_BOT,),
    "integration.zendesk": (Provider.ZENDESK_OAUTH, Provider.ZENDESK_API_TOKEN),
    "integration.api": (Provider.API_INTEGRATION,),
}


# ═════════════════════════════════ encryption ═════════════════════════════════

KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CIPHERTEXT_PREFIX = "enc1:"          # enc1:<key id>:<Fernet token>
DEFAULT_KEY_ID = "k1"                # the key id of a single ENCRYPTION_KEY (Phase 6B)
MAX_KEYS = 8


class KeyRingError(ValueError):
    """Invalid key configuration. Messages never contain key material."""


class KeyRing:
    """Versioned encryption keys (Phase 6B).

    * every key has an explicit id (``KEY_ID_PATTERN``); exactly one is *active*
      and encrypts all new values;
    * every key in the ring can decrypt, so values written under an older key
      stay readable during a rotation (backward-compatible decryption);
    * ciphertexts name their key: ``enc1:<key id>:<Fernet token>``. Values written
      before Phase 6B (a bare Fernet token) are tried against every key; Fernet
      authenticates, so a wrong key fails cleanly and never yields garbage;
    * key material never appears in ``repr``, errors or logs.
    """

    def __init__(self, keys: Dict[str, str], active: str):
        from cryptography.fernet import Fernet   # optional dependency, imported lazily
        if not isinstance(keys, dict) or not keys:
            raise KeyRingError("At least one encryption key is required.")
        if len(keys) > MAX_KEYS:
            raise KeyRingError(f"At most {MAX_KEYS} encryption keys may be loaded.")
        self._fernets: Dict[str, object] = {}
        seen = set()
        for kid, key in keys.items():
            if not isinstance(kid, str) or not KEY_ID_PATTERN.match(kid):
                raise KeyRingError("Invalid encryption key id (lower-case letters, digits and '-', at most 32).")
            raw = key.encode() if isinstance(key, str) else key
            try:
                f = Fernet(raw)
            except (ValueError, TypeError):
                raise KeyRingError(f"Encryption key '{kid}' is not a valid Fernet key.") from None
            if raw in seen:
                raise KeyRingError("Two key ids share the same key material.")
            seen.add(raw)
            self._fernets[kid] = f
        if active not in self._fernets:
            raise KeyRingError("The active encryption key id is not in the key ring.")
        self.active = active

    @property
    def key_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._fernets))

    def __repr__(self) -> str:          # never the key material
        return f"KeyRing(active={self.active!r}, key_ids={list(self.key_ids)!r})"

    @classmethod
    def from_environment(cls, env) -> "KeyRing":
        """Secure key loading. ``ENCRYPTION_KEYS`` (normally provided through
        ``ENCRYPTION_KEYS_FILE`` by the container entrypoint) holds one
        ``<key id>=<key>`` per line; ``ENCRYPTION_ACTIVE_KEY_ID`` selects the
        active key. Without a key ring, the single ``ENCRYPTION_KEY`` is key id
        ``ENCRYPTION_KEY_ID`` (default ``k1``). Both forms at once are refused."""
        ring_text = env.get("ENCRYPTION_KEYS") or ""
        single = env.get("ENCRYPTION_KEY") or ""
        if ring_text and single:
            raise KeyRingError("Set either ENCRYPTION_KEYS or ENCRYPTION_KEY, not both.")
        if ring_text:
            keys: Dict[str, str] = {}
            for n, line in enumerate(ring_text.splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                kid, sep, key = line.partition("=")
                if not sep:
                    raise KeyRingError(f"ENCRYPTION_KEYS line {n} is not '<key id>=<key>'.")
                kid = kid.strip()
                if kid in keys:
                    raise KeyRingError(f"Duplicate encryption key id '{kid}'." if KEY_ID_PATTERN.match(kid)
                                       else "Duplicate encryption key id.")
                keys[kid] = key.strip()
            active = (env.get("ENCRYPTION_ACTIVE_KEY_ID") or "").strip()
            if not active:
                raise KeyRingError("ENCRYPTION_ACTIVE_KEY_ID is required with ENCRYPTION_KEYS.")
            return cls(keys, active)
        if single:
            kid = (env.get("ENCRYPTION_KEY_ID") or DEFAULT_KEY_ID).strip()
            return cls({kid: single}, kid)
        raise KeyRingError("No encryption key configured (ENCRYPTION_KEYS or ENCRYPTION_KEY).")

    def encrypt(self, plaintext: str) -> str:
        token = self._fernets[self.active].encrypt(plaintext.encode()).decode()
        return f"{CIPHERTEXT_PREFIX}{self.active}:{token}"

    def key_id_of(self, ciphertext: str) -> Optional[str]:
        """The key id a ciphertext names, or ``None`` for a pre-6B bare token."""
        if isinstance(ciphertext, str) and ciphertext.startswith(CIPHERTEXT_PREFIX):
            kid, sep, _ = ciphertext[len(CIPHERTEXT_PREFIX):].partition(":")
            return kid if sep else ""
        return None

    def decrypt(self, ciphertext: str) -> str:
        from cryptography.fernet import InvalidToken
        if not isinstance(ciphertext, str) or not ciphertext:
            raise CredentialError("The credential could not be decrypted.")
        kid = self.key_id_of(ciphertext)
        if kid is not None:
            f = self._fernets.get(kid)
            token = ciphertext[len(CIPHERTEXT_PREFIX) + len(kid) + 1:]
            candidates = [f] if f is not None else []
        else:
            token = ciphertext
            candidates = [self._fernets[self.active]] + [v for k, v in sorted(self._fernets.items()) if k != self.active]
        for f in candidates:
            try:
                return f.decrypt(token.encode()).decode()
            except (InvalidToken, ValueError, AttributeError):
                continue
        raise CredentialError("The credential could not be decrypted.")

    def needs_rotation(self, ciphertext: str) -> bool:
        return self.key_id_of(ciphertext) != self.active


class SecretBox:
    """Encryption of every stored secret (provider keys, bot tokens, webhook and
    OAuth secrets). Backed by a :class:`KeyRing`; ``SecretBox(key)`` is a ring with
    one key (id ``k1``)."""

    def __init__(self, key, *, key_id: str = DEFAULT_KEY_ID):
        self.ring = key if isinstance(key, KeyRing) else KeyRing({key_id: key}, key_id)

    def __repr__(self) -> str:
        return f"SecretBox({self.ring!r})"

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise CredentialError("A secret value is required.")
        return self.ring.encrypt(plaintext)

    def decrypt(self, token: str) -> str:
        return self.ring.decrypt(token)

    def key_id_of(self, token: str) -> Optional[str]:
        return self.ring.key_id_of(token)

    def needs_rotation(self, token: str) -> bool:
        return self.ring.needs_rotation(token)

    def reencrypt(self, token: str) -> str:
        """Decrypt with whichever key wrote ``token`` and encrypt with the active key."""
        return self.ring.encrypt(self.ring.decrypt(token))


def fingerprint(secret: str) -> str:
    """Non-reversible identifier for display: short hash, never part of the secret."""
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:10]


# ═════════════════════════════════ model ═════════════════════════════════

@dataclass(frozen=True)
class Credential:
    id: str
    enterprise_id: str
    organisation_id: Optional[str]        # None = Enterprise-owned
    provider: Provider
    name: str
    fingerprint: str
    status: CredentialStatus
    created_by: str
    created_at: str
    rotated_at: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)      # non-secret (e.g. Zendesk subdomain)
    secret_enc: str = field(default="", repr=False)              # never shown, never logged
    # X6: optional usage scope inside the owner's subtree: ((level, id), ...). Empty = whole owner subtree.
    usable_by: Tuple[Tuple[str, str], ...] = ()

    @property
    def owner_level(self) -> Level:
        return Level.ORGANISATION if self.organisation_id else Level.ENTERPRISE

    def public_view(self) -> Dict[str, object]:
        return {"id": self.id, "provider": self.provider.value, "name": self.name,
                "owner": self.owner_level.value, "enterprise_id": self.enterprise_id,
                "organisation_id": self.organisation_id, "fingerprint": self.fingerprint,
                "status": self.status.value, "rotated_at": self.rotated_at,
                "metadata": dict(self.metadata),
                "usable_by": [{"level": lvl, "id": oid} for lvl, oid in self.usable_by]}


def new_credential(*, credential_id: str, chain: Chain, provider: Provider, name: str, secret: str,
                   box: SecretBox, actor_id: str, now: str, metadata: Optional[Dict[str, str]] = None,
                   usable_by: Tuple[Tuple[str, str], ...] = ()) -> Credential:
    """Create a credential owned by ``chain.target`` (an Enterprise or Organisation)."""
    owner = chain.target
    if owner.level not in (Level.ENTERPRISE, Level.ORGANISATION):
        raise CredentialError("Credentials belong to an Enterprise or an Organisation.")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise CredentialError("A credential name (1-120 characters) is required.")
    meta = dict(metadata or {})
    if any(not isinstance(v, str) or len(v) > 200 for v in meta.values()) or len(meta) > 10:
        raise CredentialError("Invalid credential metadata.")
    return Credential(id=credential_id, enterprise_id=chain.enterprise_id,
                      organisation_id=owner.id if owner.level is Level.ORGANISATION else None,
                      provider=provider, name=name.strip(), fingerprint=fingerprint(secret),
                      status=CredentialStatus.ACTIVE, created_by=actor_id, created_at=now,
                      metadata=meta, secret_enc=box.encrypt(secret), usable_by=tuple(usable_by))


def replace_secret(cred: Credential, *, secret: str, box: SecretBox, now: str) -> Credential:
    """Rotation keeps the id, so every reference keeps working."""
    if cred.status is not CredentialStatus.ACTIVE:
        raise CredentialError("A revoked credential cannot be replaced; create a new one.")
    return replace(cred, secret_enc=box.encrypt(secret), fingerprint=fingerprint(secret), rotated_at=now)


def revoke(cred: Credential, *, now: str) -> Credential:
    """Revocation is immediate and permanent; runtime use fails closed."""
    return replace(cred, status=CredentialStatus.REVOKED, rotated_at=now, secret_enc="")


# ═════════════════════════════════ use and manage ═════════════════════════════════

def check_use(cred: Optional[Credential], *, referencing_chain: Chain, slot: str,
              policy: Dict[str, object]) -> Credential:
    """May an object (``referencing_chain.target``) use ``cred`` in ``slot``?

    Structural rule shared by configuration writes and runtime resolution:
    same Enterprise; Organisation credentials only inside that Organisation;
    Enterprise credentials only if ``credentials.allow_enterprise_credentials``;
    active; provider accepted by the slot. All failures use the same
    non-disclosing message.
    """
    denied = CredentialError("The selected credential is not available here.")
    if cred is None or not is_valid_id(cred.id):
        raise denied
    if cred.enterprise_id != referencing_chain.enterprise_id:
        raise denied
    if cred.organisation_id is not None:
        org = referencing_chain.organisation
        if org is None or org.id != cred.organisation_id:
            raise denied
    elif policy.get("credentials.allow_enterprise_credentials") is False and \
            referencing_chain.target.level not in (Level.ENTERPRISE,):
        raise denied
    if cred.usable_by:
        # X6: restricted to selected Organisations/Bots inside the owner subtree
        here = {(n.level.value, n.id) for n in referencing_chain.nodes()
                if n.level in (Level.ORGANISATION, Level.BOT)}
        if not here & set(cred.usable_by):
            raise denied
    if cred.status is not CredentialStatus.ACTIVE:
        raise CredentialError("The selected credential has been revoked.")
    if cred.provider not in SLOT_PROVIDERS.get(slot, ()):
        raise CredentialError("The selected credential is for a different service.")
    return cred


def check_reference_permission(actor: Actor, chain: Chain, memberships, policy: Dict[str, object]) -> None:
    """Using a credential needs Editor (or above) on the referencing object."""
    d = can(actor, Action.USE_CREDENTIAL, chain, memberships, policy)
    if not d.allowed:
        raise AuthzError(d.reason)


def check_manage(actor: Actor, owner_chain: Chain, memberships, policy: Dict[str, object]) -> None:
    """Managing (create/replace/revoke/delete) needs Admin on the owning scope,
    subject to ``delegation.org_admins_manage_credentials`` for Organisation-level
    authority. ``owner_chain.target`` is the credential's owner."""
    if owner_chain.target.level not in (Level.ENTERPRISE, Level.ORGANISATION):
        raise AuthzError("Credentials belong to an Enterprise or an Organisation.")
    d = can(actor, Action.MANAGE_CREDENTIALS, owner_chain, memberships, policy)
    if not d.allowed:
        raise AuthzError(d.reason)


def check_manage_platform(actor: Actor) -> None:
    if not actor.is_platform_superadmin:
        raise AuthzError("Platform credentials are managed by platform superadmins only.")


# ═════════════════════════════════ persistence helpers (Phase 3) ═════════════════════════════════

COLLECTION = "credentials"
PLATFORM_COLLECTION = "platform_credentials"
import re as _re
_TELEGRAM_TOKEN = _re.compile(r"([1-9][0-9]{2,19}):[A-Za-z0-9_-]{20,128}")


def telegram_bot_id(token: str) -> str:
    """The numeric bot id that prefixes a Telegram bot token (not secret)."""
    m = _TELEGRAM_TOKEN.fullmatch(token.strip()) if isinstance(token, str) else None
    if not m:
        raise CredentialError("This is not a valid Telegram bot token.")
    return m.group(1)


USABLE_BY_MAX = 200


async def normalise_usable_by(store, *, enterprise_id: str, organisation_id: Optional[str],
                              entries) -> Tuple[Tuple[str, str], ...]:
    """X6: validate a usage scope against the owner hierarchy (server-side
    lookups). Enterprise credentials may name Organisations or Bots of that
    Enterprise; Organisation credentials may name Bots of that Organisation.
    Unknown, deleted, foreign or out-of-subtree ids are refused with one
    non-disclosing message."""
    bad = CredentialError("The usage scope contains an object that is not available here.")
    if entries is None:
        return ()
    if not isinstance(entries, (list, tuple)) or len(entries) > USABLE_BY_MAX:
        raise CredentialError(f"The usage scope must be a list of at most {USABLE_BY_MAX} entries.")
    out = []
    for e in entries:
        level, oid = (e.get("level"), e.get("id")) if isinstance(e, dict) else (None, None)
        if level not in ("organisation", "bot") or not is_valid_id(oid):
            raise bad
        if organisation_id is not None and level != "bot":
            raise bad                                    # Organisation credentials: Bots only
        doc = await store.get("organisations" if level == "organisation" else "bots", oid)
        if not doc or doc.get("enterprise_id") != enterprise_id or \
                (doc.get("lifecycle") or {}).get("state") == "deleted":
            raise bad
        if organisation_id is not None and doc.get("organisation_id") != organisation_id:
            raise bad
        out.append((level, oid))
    return tuple(sorted(set(out)))


def credential_to_doc(cred: Credential, version: int) -> Dict[str, object]:
    return {"id": cred.id, "enterprise_id": cred.enterprise_id, "organisation_id": cred.organisation_id,
            "provider": cred.provider.value, "name": cred.name, "fingerprint": cred.fingerprint,
            "status": cred.status.value, "created_by": cred.created_by, "created_at": cred.created_at,
            "rotated_at": cred.rotated_at, "metadata": dict(cred.metadata), "secret_enc": cred.secret_enc,
            "usable_by": [{"level": lvl, "id": oid} for lvl, oid in cred.usable_by], "version": version}


def credential_from_doc(doc: Dict[str, object]) -> Credential:
    return Credential(id=doc["id"], enterprise_id=doc["enterprise_id"], organisation_id=doc.get("organisation_id"),
                      provider=Provider(doc["provider"]), name=doc.get("name", ""),
                      fingerprint=doc.get("fingerprint", ""), status=CredentialStatus(doc.get("status", "revoked")),
                      created_by=doc.get("created_by", ""), created_at=doc.get("created_at", ""),
                      rotated_at=doc.get("rotated_at"), metadata=dict(doc.get("metadata") or {}),
                      secret_enc=doc.get("secret_enc", ""),
                      usable_by=tuple((u["level"], u["id"]) for u in doc.get("usable_by") or []
                                      if isinstance(u, dict) and "level" in u and "id" in u))


async def load_credential(store, credential_id) -> "Optional[Tuple[Credential, int]]":
    if not is_valid_id(credential_id):
        return None
    doc = await store.get(COLLECTION, credential_id)
    return (credential_from_doc(doc), int(doc.get("version", 0))) if doc else None


async def enterprise_credentials(store, enterprise_id: str) -> Dict[str, Credential]:
    """All credentials of one Enterprise (the resolver filters by scope)."""
    docs = await store.find(COLLECTION, {"enterprise_id": enterprise_id})
    return {d["id"]: credential_from_doc(d) for d in docs if d.get("enterprise_id") == enterprise_id}


__all__ = ["CredentialError", "Provider", "PlatformCredentialKind", "CredentialStatus", "SLOT_PROVIDERS",
           "SecretBox", "KeyRing", "KeyRingError", "CIPHERTEXT_PREFIX", "DEFAULT_KEY_ID", "fingerprint", "Credential", "new_credential", "replace_secret", "revoke",
           "check_use", "check_reference_permission", "check_manage", "check_manage_platform", "Membership",
           "COLLECTION", "PLATFORM_COLLECTION", "telegram_bot_id", "credential_to_doc", "credential_from_doc",
           "load_credential", "enterprise_credentials", "normalise_usable_by", "USABLE_BY_MAX"]
