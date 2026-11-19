"""Enterprise -> Organisation -> Bot -> Bot Instance (spec 012 §1-2).

Explicit models, server-assigned immutable ancestry, channel bindings, a small
async store interface (in-memory implementation here; MongoDB in Phase 3) and
:class:`HierarchyService`, the only way objects are created or changed.

Ownership rules enforced here:
* parents are loaded from the store by id; ancestry is copied from the loaded
  parent and never accepted from callers;
* ancestry, ids, channel type and creation metadata are immutable;
* a child can only be created under an existing, non-deleted parent whose
  lifecycle allows children (no orphans);
* :meth:`HierarchyService.load_chain` re-verifies the whole ancestry on every
  load and fails closed on any inconsistency;
* channel bindings are unique platform-wide (a Discord guild/channel scope, a
  Telegram bot credential, a web ``public_id`` can serve only one instance).
"""
from __future__ import annotations

import copy
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from core.lifecycle import (Lifecycle, LifecycleAction, LifecycleState, Authority, apply_action,
                            availability, Availability, can_create_child_under, is_writable)


# ═════════════════════════════════ errors ═════════════════════════════════

class HierarchyError(ValueError):
    """Invalid request. ``str(exc)`` is safe to show to the caller."""


class NotFound(HierarchyError):
    """Unknown id — also used for objects the caller may not see (non-disclosing)."""


class IntegrityError(HierarchyError):
    """Stored ancestry is inconsistent: fail closed."""


class ConcurrentModification(HierarchyError):
    """A compare-and-swap write lost a race. Safe to show; the caller may retry."""

    def __init__(self, message: str = "This object was changed by another request. Reload and try again."):
        super().__init__(message)


class DuplicateKey(HierarchyError):
    """A unique key (id or unique field) already exists."""


# ═════════════════════════════════ levels ═════════════════════════════════

class Level(str, Enum):
    ENTERPRISE = "enterprise"
    ORGANISATION = "organisation"
    BOT = "bot"
    BOT_INSTANCE = "bot_instance"


LEVEL_ORDER = (Level.ENTERPRISE, Level.ORGANISATION, Level.BOT, Level.BOT_INSTANCE)
PARENT: Dict[Level, Optional[Level]] = {
    Level.ENTERPRISE: None, Level.ORGANISATION: Level.ENTERPRISE,
    Level.BOT: Level.ORGANISATION, Level.BOT_INSTANCE: Level.BOT,
}
COLLECTION: Dict[Level, str] = {
    Level.ENTERPRISE: "enterprises", Level.ORGANISATION: "organisations",
    Level.BOT: "bots", Level.BOT_INSTANCE: "bot_instances",
}
ANCESTRY_FIELDS: Dict[Level, Tuple[str, ...]] = {
    Level.ENTERPRISE: (),
    Level.ORGANISATION: ("enterprise_id",),
    Level.BOT: ("enterprise_id", "organisation_id"),
    Level.BOT_INSTANCE: ("enterprise_id", "organisation_id", "bot_id"),
}
PARENT_ID_FIELD: Dict[Level, Optional[str]] = {
    Level.ENTERPRISE: None, Level.ORGANISATION: "enterprise_id",
    Level.BOT: "organisation_id", Level.BOT_INSTANCE: "bot_id",
}
# Fields no update may ever change.
IMMUTABLE_FIELDS = frozenset({"id", "level", "enterprise_id", "organisation_id", "bot_id",
                              "created_at", "created_by", "lifecycle", "overrides", "policy",
                              "version", "telegram_bot_id"})
# Fields a plain update may change, per level (overrides/policy/lifecycle have their own paths).
MUTABLE_FIELDS: Dict[Level, frozenset] = {
    Level.ENTERPRISE: frozenset({"name", "plan"}),
    Level.ORGANISATION: frozenset({"name", "description"}),
    Level.BOT: frozenset({"name", "description"}),
    Level.BOT_INSTANCE: frozenset({"name", "channel"}),
}

_NAME_MAX = 120
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?")
_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
PLANS = ("free", "pro", "enterprise")


def new_id() -> str:
    return str(uuid.uuid4())


def is_valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _NAME_MAX:
        raise HierarchyError(f"Name is required (1-{_NAME_MAX} characters).")
    if any(ord(c) < 0x20 for c in value):
        raise HierarchyError("Name contains control characters.")
    return value.strip()


def _text(value: Any, limit: int, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise HierarchyError(f"{label} must be text of at most {limit} characters.")
    return value.strip()


# ═════════════════════════════════ channel bindings ═════════════════════════════════

class ChannelType(str, Enum):
    DISCORD = "discord"
    TELEGRAM = "telegram"
    WEBCHAT = "webchat"


_SNOWFLAKE = re.compile(r"[1-9][0-9]{15,20}")
_TG_CHAT = re.compile(r"-?[1-9][0-9]{0,19}")
_ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")
_PUBLIC_ID = re.compile(r"pk_[A-Za-z0-9_-]{22,64}")

DISCORD_LISTEN_MODES = ("mention_only", "all_channels", "specific_channels")
DISCORD_REPLY_STYLES = ("natural", "with_mention")
TELEGRAM_LISTEN_MODES = ("mention_or_command", "dm_only", "all")


@dataclass(frozen=True)
class DiscordBinding:
    """Object-specific Discord settings (parity D3-D12; scripted mode retired, D-Q6)."""
    guild_id: Optional[str] = None                 # set by the OAuth install flow (Phase 4D)
    application_id: Optional[str] = None           # the Enterprise's Discord application (D-Q14), server-set
    channel_ids: Tuple[str, ...] = ()              # empty = whole guild
    listen_mode: str = "mention_only"
    mention_overrides_channels: bool = True
    reply_style: str = "natural"
    passive_mode: bool = False
    staff_role_name: str = ""
    handoff_cooldown_minutes: int = 15
    handoff_followup_message: str = "Is there anything else I can help with?"
    dm_enabled: bool = False                       # D-Q5: off by default

    type = ChannelType.DISCORD

    def validated(self) -> "DiscordBinding":
        if self.guild_id is not None and not _SNOWFLAKE.fullmatch(str(self.guild_id)):
            raise HierarchyError("Invalid Discord guild id.")
        if self.application_id is not None and not _SNOWFLAKE.fullmatch(str(self.application_id)):
            raise HierarchyError("Invalid Discord application id.")
        if bool(self.guild_id) != bool(self.application_id):
            raise HierarchyError("A Discord guild binding needs its Enterprise application.")
        chans = tuple(dict.fromkeys(str(c) for c in self.channel_ids))
        if len(chans) > 50 or not all(_SNOWFLAKE.fullmatch(c) for c in chans):
            raise HierarchyError("Invalid Discord channel ids (at most 50).")
        if self.listen_mode not in DISCORD_LISTEN_MODES:
            raise HierarchyError(f"listen_mode must be one of {', '.join(DISCORD_LISTEN_MODES)}.")
        if self.listen_mode == "specific_channels" and not chans:
            raise HierarchyError("specific_channels requires at least one channel id.")
        if self.reply_style not in DISCORD_REPLY_STYLES:
            raise HierarchyError(f"reply_style must be one of {', '.join(DISCORD_REPLY_STYLES)}.")
        if not isinstance(self.handoff_cooldown_minutes, int) or not 0 <= self.handoff_cooldown_minutes <= 1440:
            raise HierarchyError("handoff_cooldown_minutes must be 0-1440.")
        if self.dm_enabled and not self.guild_id:
            raise HierarchyError("Direct messages can only be enabled for an instance bound to a guild.")
        return DiscordBinding(
            guild_id=self.guild_id, application_id=self.application_id, channel_ids=chans, listen_mode=self.listen_mode,
            mention_overrides_channels=bool(self.mention_overrides_channels), reply_style=self.reply_style,
            passive_mode=bool(self.passive_mode), staff_role_name=_text(self.staff_role_name, 100, "staff_role_name"),
            handoff_cooldown_minutes=self.handoff_cooldown_minutes,
            handoff_followup_message=_text(self.handoff_followup_message, 500, "handoff_followup_message"),
            dm_enabled=bool(self.dm_enabled))

    def scopes(self) -> List[Tuple[str, ...]]:
        """Routing keys. A whole-guild binding conflicts with any binding in that guild."""
        if not self.guild_id:
            return []
        if not self.channel_ids:
            return [("discord", self.guild_id, "*")]
        return [("discord", self.guild_id, c) for c in self.channel_ids]


@dataclass(frozen=True)
class TelegramBinding:
    credential_id: Optional[str] = None            # Organisation/Enterprise credential (bot token)
    listen_mode: str = "mention_or_command"
    chat_ids: Tuple[str, ...] = ()                 # optional allowlist (enforced, parity T4)

    type = ChannelType.TELEGRAM

    def validated(self) -> "TelegramBinding":
        if self.credential_id is not None and not is_valid_id(self.credential_id):
            raise HierarchyError("Invalid credential reference.")
        if self.listen_mode not in TELEGRAM_LISTEN_MODES:
            raise HierarchyError(f"listen_mode must be one of {', '.join(TELEGRAM_LISTEN_MODES)}.")
        chats = tuple(dict.fromkeys(str(c) for c in self.chat_ids))
        if len(chats) > 100 or not all(_TG_CHAT.fullmatch(c) for c in chats):
            raise HierarchyError("Invalid Telegram chat ids (at most 100).")
        return TelegramBinding(self.credential_id, self.listen_mode, chats)

    def scopes(self) -> List[Tuple[str, ...]]:
        return [("telegram", self.credential_id)] if self.credential_id else []


@dataclass(frozen=True)
class WebchatBinding:
    public_id: str = ""                            # assigned by the server
    allowed_origins: Tuple[str, ...] = ()          # empty = any (subject to policy)

    type = ChannelType.WEBCHAT

    def validated(self) -> "WebchatBinding":
        if not _PUBLIC_ID.fullmatch(self.public_id or ""):
            raise HierarchyError("Invalid public id.")
        origins = tuple(dict.fromkeys(o.strip().lower().rstrip("/") for o in self.allowed_origins))
        if len(origins) > 50 or not all(_ORIGIN.fullmatch(o) for o in origins):
            raise HierarchyError("allowed_origins must be https origins (scheme://host[:port]).")
        return WebchatBinding(self.public_id, origins)

    def scopes(self) -> List[Tuple[str, ...]]:
        return [("webchat", self.public_id)]


ChannelBinding = Any  # DiscordBinding | TelegramBinding | WebchatBinding
_BINDING_CLASSES = {ChannelType.DISCORD: DiscordBinding, ChannelType.TELEGRAM: TelegramBinding,
                    ChannelType.WEBCHAT: WebchatBinding}


def new_public_id() -> str:
    return "pk_" + secrets.token_urlsafe(24)


def binding_from_doc(doc: dict) -> ChannelBinding:
    try:
        ctype = ChannelType(doc.get("type"))
    except ValueError:
        raise HierarchyError("Unknown channel type.")
    cls = _BINDING_CLASSES[ctype]
    fields = {k: v for k, v in doc.items() if k != "type" and k in cls.__dataclass_fields__}
    for k in ("channel_ids", "chat_ids", "allowed_origins"):
        if k in fields:
            if not isinstance(fields[k], (list, tuple)):
                raise HierarchyError(f"{k} must be a list.")
            fields[k] = tuple(fields[k])
    try:
        return cls(**fields)
    except TypeError:
        raise HierarchyError("Invalid channel settings.")


def binding_to_doc(binding: ChannelBinding) -> dict:
    doc = {"type": binding.type.value}
    for k in binding.__dataclass_fields__:
        v = getattr(binding, k)
        doc[k] = list(v) if isinstance(v, tuple) else v
    return doc


# ═════════════════════════════════ logos (D-Q8) ═════════════════════════════════

LOGO_MAX_BYTES = 256 * 1024
_LOGO_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def sniff_logo(data: bytes) -> str:
    """Content type from magic bytes (client-declared types are ignored)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise HierarchyError("Logo is empty.")
    if len(data) > LOGO_MAX_BYTES:
        raise HierarchyError(f"Logo must be at most {LOGO_MAX_BYTES // 1024} KB.")
    for magic, ctype in _LOGO_MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise HierarchyError("Logo must be a PNG, JPEG, GIF or WebP image.")


# ═════════════════════════════════ models ═════════════════════════════════

@dataclass(frozen=True)
class Node:
    """Fields shared by all four hierarchy objects."""
    id: str
    name: str
    lifecycle: Lifecycle
    overrides: Dict[str, Any]
    created_at: str
    created_by: str
    updated_at: str
    version: int = 0           # optimistic-concurrency version; every write is compare-and-swap

    level = None  # set by subclasses

    @property
    def ancestry(self) -> Dict[str, str]:
        return {f: getattr(self, f) for f in ANCESTRY_FIELDS[self.level]}


@dataclass(frozen=True)
class Enterprise(Node):
    slug: str = ""
    plan: str = "free"
    policy: Dict[str, Any] = field(default_factory=dict)
    level = Level.ENTERPRISE


@dataclass(frozen=True)
class Organisation(Node):
    enterprise_id: str = ""
    description: str = ""
    policy: Dict[str, Any] = field(default_factory=dict)
    level = Level.ORGANISATION


@dataclass(frozen=True)
class Bot(Node):
    enterprise_id: str = ""
    organisation_id: str = ""
    description: str = ""
    logo: Optional[Dict[str, Any]] = None          # {content_type, size, sha256}; bytes stored separately
    level = Level.BOT


@dataclass(frozen=True)
class BotInstance(Node):
    enterprise_id: str = ""
    organisation_id: str = ""
    bot_id: str = ""
    channel: ChannelBinding = None
    telegram_bot_id: Optional[str] = None          # server-derived from the Telegram credential (binding key)
    level = Level.BOT_INSTANCE


MODEL: Dict[Level, type] = {Level.ENTERPRISE: Enterprise, Level.ORGANISATION: Organisation,
                            Level.BOT: Bot, Level.BOT_INSTANCE: BotInstance}


def to_doc(obj: Node) -> dict:
    doc = {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    doc["level"] = obj.level.value
    doc["lifecycle"] = obj.lifecycle.to_doc()
    if isinstance(obj, BotInstance):
        doc["channel"] = binding_to_doc(obj.channel)
    return copy.deepcopy(doc)


def from_doc(level: Level, doc: dict) -> Node:
    cls = MODEL[level]
    if doc.get("level") != level.value:
        raise IntegrityError("Stored object has an unexpected type.")
    data = {k: copy.deepcopy(doc[k]) for k in cls.__dataclass_fields__ if k in doc}
    data["lifecycle"] = Lifecycle.from_doc(doc.get("lifecycle"))
    data.setdefault("overrides", {})
    if level is Level.BOT_INSTANCE:
        data["channel"] = binding_from_doc(doc.get("channel") or {})
    return cls(**data)


@dataclass(frozen=True)
class Chain:
    """A verified ancestry chain, root first. Missing lower levels are None."""
    enterprise: Enterprise
    organisation: Optional[Organisation] = None
    bot: Optional[Bot] = None
    instance: Optional[BotInstance] = None

    def nodes(self) -> List[Node]:
        return [n for n in (self.enterprise, self.organisation, self.bot, self.instance) if n is not None]

    @property
    def target(self) -> Node:
        return self.nodes()[-1]

    @property
    def enterprise_id(self) -> str:
        return self.enterprise.id

    def availability(self) -> Availability:
        return availability((n.level.value, n.id, n.lifecycle) for n in self.nodes())

    def ids(self) -> Dict[str, str]:
        return {f"{n.level.value}_id": n.id for n in self.nodes()}


# ═════════════════════════════════ store ═════════════════════════════════

class Store(Protocol):
    """Persistence primitives. Every multi-step invariant in Spec 012 is built from
    these single-document atomic operations, so the same code is safe on a
    standalone MongoDB (no multi-document transactions):

    * ``insert`` fails with :class:`DuplicateKey` on an existing id or unique field;
    * ``update`` with ``expect_version`` is a compare-and-swap: it applies only if
      the stored ``version`` still equals ``expect_version`` (then increments it),
      otherwise :class:`ConcurrentModification` (or :class:`NotFound`);
    * ``delete`` with ``expect_version`` is a conditional delete;
    * ``take`` atomically reads and deletes one document (single-use tokens).

    Filters are equality-only (server-built, never client-supplied); a list value
    means "one of"; a dotted path matches inside arrays like MongoDB.
    """
    async def get(self, collection: str, object_id: str) -> Optional[dict]: ...
    async def insert(self, collection: str, doc: dict) -> None: ...
    async def update(self, collection: str, object_id: str, fields: dict, *,
                     expect_version: Optional[int] = None) -> None: ...
    async def find(self, collection: str, flt: dict, *, limit: int = 0, sort: Optional[str] = None,
                   descending: bool = False) -> List[dict]: ...
    async def count(self, collection: str, flt: dict) -> int: ...
    async def delete(self, collection: str, object_id: str, *, expect_version: Optional[int] = None) -> bool: ...
    async def delete_many(self, collection: str, flt: dict) -> int: ...
    async def take(self, collection: str, object_id: str) -> Optional[dict]: ...
    async def increment(self, collection: str, object_id: str, field: str, amount: int, *,
                        initial: dict) -> int: ...      # atomic counter (upsert); returns the new value
    # Phase 6B (encryption-key rotation): ordered id-cursor scan and a field-level compare-and-set
    async def scan(self, collection: str, *, after: Optional[str] = None, limit: int = 100) -> List[dict]: ...
    async def set_field_if(self, collection: str, object_id: str, path: str, expected: str, value: str) -> bool: ...


# Unique fields per collection (besides ``id``). The MongoDB adapter builds unique
# indexes from the same table, so both stores enforce identical constraints.
UNIQUE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "enterprises": ("slug",),
    "users": ("email",),
    "invitations": ("token_hash",),
    "discord_applications": ("application_id",),     # D-Q14: one Enterprise per Discord application
    "discord_share_links": ("token_hash",),
}


def _path_values(doc: Any, parts: List[str]) -> List[Any]:
    """Values at a dotted path, traversing arrays like MongoDB."""
    if not parts:
        return [doc]
    if isinstance(doc, list):
        out: List[Any] = []
        for item in doc:
            out.extend(_path_values(item, parts))
        return out
    if not isinstance(doc, dict) or parts[0] not in doc:
        return [None] if not isinstance(doc, list) else []
    return _path_values(doc[parts[0]], parts[1:])


def match_filter(doc: dict, flt: dict) -> bool:
    for key, want in (flt or {}).items():
        values = _path_values(doc, key.split("."))
        flat = []
        for v in values:
            flat.extend(v if isinstance(v, list) else [v])
        wanted = want if isinstance(want, list) else [want]
        if not any(v in wanted for v in flat):
            return False
    return True


def _check_prefix_args(field, prefix, after, limit) -> None:
    if not isinstance(field, str) or not field.isidentifier():
        raise HierarchyError("Invalid filter.")
    if not isinstance(prefix, str) or len(prefix) > 254 or (after is not None and (not isinstance(after, str) or len(after) > 254)):
        raise HierarchyError("Invalid filter.")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise HierarchyError("Invalid filter.")


ENC_PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}_enc$")


def _check_enc_path(path) -> None:
    """``set_field_if`` may only touch an encrypted field (``…_enc``, at most four
    levels deep): server-built paths only, never an operator or an id."""
    if not isinstance(path, str) or not ENC_PATH.match(path):
        raise HierarchyError("Invalid field path.")


def _check_scan_args(after, limit) -> None:
    if after is not None and (not isinstance(after, str) or len(after) > 254):
        raise HierarchyError("Invalid cursor.")
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise HierarchyError("Invalid limit.")


class InMemoryStore:
    """Store for tests with the same semantics as the MongoDB adapter.

    Each operation yields to the event loop first (``asyncio.sleep(0)``), so
    concurrent coroutines genuinely interleave between operations, while each
    operation itself is atomic — exactly the guarantee MongoDB gives for single
    documents. Concurrency tests therefore exercise the real race windows.
    """

    def __init__(self, unique: Optional[Dict[str, Tuple[str, ...]]] = None):
        self.collections: Dict[str, Dict[str, dict]] = {}
        self.unique = dict(UNIQUE_FIELDS if unique is None else unique)

    @staticmethod
    async def _yield():
        import asyncio
        await asyncio.sleep(0)

    async def get(self, collection, object_id):
        await self._yield()
        doc = self.collections.get(collection, {}).get(object_id)
        return copy.deepcopy(doc) if doc is not None else None

    async def insert(self, collection, doc):
        await self._yield()
        col = self.collections.setdefault(collection, {})
        if doc["id"] in col:
            raise DuplicateKey("Duplicate id.")
        for f in self.unique.get(collection, ()):
            if doc.get(f) is not None and any(d.get(f) == doc.get(f) for d in col.values()):
                raise DuplicateKey(f"Duplicate {f}.")
        col[doc["id"]] = copy.deepcopy(doc)

    async def update(self, collection, object_id, fields, *, expect_version=None):
        await self._yield()
        col = self.collections.get(collection, {})
        if object_id not in col:
            raise NotFound("Not found.")
        cur = col[object_id]
        if expect_version is not None and cur.get("version", 0) != expect_version:
            raise ConcurrentModification()
        for f in self.unique.get(collection, ()):
            if f in fields and fields[f] is not None and any(
                    k != object_id and d.get(f) == fields[f] for k, d in col.items()):
                raise DuplicateKey(f"Duplicate {f}.")
        for k, v in fields.items():
            cur[k] = copy.deepcopy(v)
        if expect_version is not None:
            cur["version"] = expect_version + 1

    async def find(self, collection, flt, *, limit=0, sort=None, descending=False):
        await self._yield()
        docs = [copy.deepcopy(d) for d in self.collections.get(collection, {}).values() if match_filter(d, flt)]
        if sort:
            docs.sort(key=lambda d: (d.get(sort) is None, d.get(sort)), reverse=descending)
        return docs[:limit] if limit else docs

    async def find_prefix(self, collection, field, prefix, *, after=None, limit=50, flt=None):
        """Bounded, ordered prefix search on one string field (Phase 6A F2).
        Only server-built strings; ``after`` is the exclusive cursor."""
        await self._yield()
        _check_prefix_args(field, prefix, after, limit)
        docs = [copy.deepcopy(d) for d in self.collections.get(collection, {}).values()
                if isinstance(d.get(field), str) and d[field].startswith(prefix)
                and (after is None or d[field] > after) and match_filter(d, flt or {})]
        docs.sort(key=lambda d: d[field])
        return docs[:limit]

    async def scan(self, collection, *, after=None, limit=100):
        """Every document of one collection in id order, ``after`` exclusive (Phase 6B)."""
        await self._yield()
        _check_scan_args(after, limit)
        ids = sorted(i for i in self.collections.get(collection, {}) if after is None or i > after)[:limit]
        return [copy.deepcopy(self.collections[collection][i]) for i in ids]

    async def set_field_if(self, collection, object_id, path, expected, value):
        """Atomically set one encrypted field only if it still holds ``expected``
        (Phase 6B key rotation). The document version is not changed: the stored
        secret is the same, only the key that encrypts it."""
        await self._yield()
        _check_enc_path(path)
        doc = self.collections.get(collection, {}).get(object_id)
        if doc is None:
            return False
        *parents, leaf = path.split(".")
        cur = doc
        for p in parents:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if not isinstance(cur, dict) or cur.get(leaf) != expected:
            return False
        cur[leaf] = value
        return True

    async def count(self, collection, flt):
        await self._yield()
        return sum(1 for d in self.collections.get(collection, {}).values() if match_filter(d, flt))

    async def delete(self, collection, object_id, *, expect_version=None):
        await self._yield()
        col = self.collections.get(collection, {})
        cur = col.get(object_id)
        if cur is None:
            return False
        if expect_version is not None and cur.get("version", 0) != expect_version:
            return False
        del col[object_id]
        return True

    async def delete_many(self, collection, flt):
        await self._yield()
        col = self.collections.get(collection, {})
        doomed = [k for k, d in col.items() if match_filter(d, flt)]
        for k in doomed:
            del col[k]
        return len(doomed)

    async def increment(self, collection, object_id, field, amount, *, initial):
        await self._yield()
        col = self.collections.setdefault(collection, {})
        doc = col.get(object_id)
        if doc is None:
            doc = col[object_id] = {**copy.deepcopy(initial), "id": object_id, field: 0, "version": 1}
        doc[field] = int(doc.get(field, 0)) + int(amount)
        return doc[field]

    async def take(self, collection, object_id):
        await self._yield()
        return self.collections.get(collection, {}).pop(object_id, None)


# MongoDB secondary indexes (``persistence.mongo`` adds ``_id`` = ``id`` and the
# UNIQUE_FIELDS indexes). Every tenant-owned query starts with ``enterprise_id``.
INDEXES = {
    "organisations": [[("enterprise_id", 1), ("id", 1)]],
    "bots": [[("enterprise_id", 1), ("organisation_id", 1)]],
    "bot_instances": [[("enterprise_id", 1), ("bot_id", 1)], [("organisation_id", 1)],
                      [("binding_scopes", 1)]],
}


# ═════════════════════════════════ service ═════════════════════════════════

class HierarchyService:
    """Creates, updates and loads hierarchy objects. Authorisation is the caller's
    job (``core.authz``); this service enforces structure and ownership.

    Concurrency (spec 012 Phase 3): every update is a compare-and-swap on the
    object's ``version``; channel bindings are claimed atomically through
    :class:`core.bindings.BindingRegistry` before an instance stores them; a
    child created while its parent is being deleted is removed again (the
    post-insert check), and the deletion job sweeps until no descendant remains.
    """

    def __init__(self, store: Store, *, clock: Callable[[], str] = _now, ids: Callable[[], str] = new_id):
        from core.bindings import BindingRegistry       # local import: bindings imports this module
        self.store = store
        self.clock = clock
        self.ids = ids
        self.bindings = BindingRegistry(store, clock=clock)

    # ── loading ──
    async def _get(self, level: Level, object_id: str) -> Node:
        if not is_valid_id(object_id):
            raise NotFound("Not found.")
        doc = await self.store.get(COLLECTION[level], object_id)
        if doc is None:
            raise NotFound("Not found.")
        obj = from_doc(level, doc)
        if obj.id != object_id:
            raise IntegrityError("Stored id mismatch.")
        return obj

    async def load_chain(self, level: Level, object_id: str) -> Chain:
        """Load the target and every ancestor and verify the ancestry is consistent."""
        target = await self._get(level, object_id)
        nodes: Dict[Level, Node] = {level: target}
        cur = target
        while PARENT[cur.level] is not None:
            parent_level = PARENT[cur.level]
            parent = await self._get(parent_level, getattr(cur, PARENT_ID_FIELD[cur.level]))
            for f in ANCESTRY_FIELDS[parent_level]:
                if getattr(parent, f) != getattr(cur, f):
                    raise IntegrityError("Ancestry mismatch.")
            nodes[parent_level] = parent
            cur = parent
        # every node must carry the same enterprise id
        ent = nodes[Level.ENTERPRISE].id
        if any(getattr(n, "enterprise_id", ent) != ent for n in nodes.values()):
            raise IntegrityError("Ancestry mismatch.")
        return Chain(enterprise=nodes[Level.ENTERPRISE], organisation=nodes.get(Level.ORGANISATION),
                     bot=nodes.get(Level.BOT), instance=nodes.get(Level.BOT_INSTANCE))

    async def children(self, level: Level, parent_id: str) -> List[Node]:
        """Direct children of ``parent_id`` (``level`` is the child level)."""
        docs = await self.store.find(COLLECTION[level], {PARENT_ID_FIELD[level]: parent_id})
        return [from_doc(level, d) for d in sorted(docs, key=lambda d: (d.get("created_at", ""), d["id"]))]

    # ── creation ──
    def _base(self, name: str, actor_id: str, object_id: Optional[str] = None) -> dict:
        now = self.clock()
        return {"id": object_id or self.ids(), "name": _name(name),
                "lifecycle": Lifecycle(changed_at=now, changed_by=actor_id),
                "overrides": {}, "created_at": now, "created_by": actor_id, "updated_at": now, "version": 1}

    async def _parent_for_child(self, child: Level, parent_id: str) -> Chain:
        chain = await self.load_chain(PARENT[child], parent_id)
        if any(n.lifecycle.state is LifecycleState.DELETED for n in chain.nodes()):
            raise NotFound("Not found.")
        if not can_create_child_under(chain.target.lifecycle):
            raise HierarchyError("Children cannot be created under a suspended or archived parent.")
        return chain

    async def _insert_child(self, level: Level, obj: Node, *, on_abort=None) -> None:
        """Insert, then re-check the parent chain. A parent whose deletion began
        concurrently wins: the new child is removed again (no orphans)."""
        await self.store.insert(COLLECTION[level], to_doc(obj) if level is not Level.BOT_INSTANCE else _instance_doc(obj))
        try:
            chain = await self.load_chain(PARENT[level], getattr(obj, PARENT_ID_FIELD[level]))
            ok = not any(n.lifecycle.state is LifecycleState.DELETED for n in chain.nodes())
        except HierarchyError:
            ok = False
        if not ok:
            await self.store.delete(COLLECTION[level], obj.id)
            if on_abort is not None:
                await on_abort()
            raise NotFound("Not found.")

    async def create_enterprise(self, *, name: str, slug: str, actor_id: str, plan: str = "free",
                                enterprise_id: Optional[str] = None) -> Enterprise:
        """``enterprise_id`` lets server code pre-allocate the id (e.g. to create
        the owner roster first); it is never taken from a client."""
        slug = (slug or "").strip().lower()
        if not _SLUG_RE.fullmatch(slug):
            raise HierarchyError("Slug must be 1-50 lowercase letters, digits or hyphens.")
        if plan not in PLANS:
            raise HierarchyError("Unknown plan.")
        if enterprise_id is not None and not is_valid_id(enterprise_id):
            raise HierarchyError("Invalid id.")
        obj = Enterprise(**self._base(name, actor_id, enterprise_id), slug=slug, plan=plan, policy={})
        try:
            await self.store.insert(COLLECTION[Level.ENTERPRISE], to_doc(obj))   # unique slug index decides
        except DuplicateKey:
            raise HierarchyError("This slug is not available.")
        return obj

    async def create_organisation(self, *, enterprise_id: str, name: str, actor_id: str,
                                  description: str = "") -> Organisation:
        parent = (await self._parent_for_child(Level.ORGANISATION, enterprise_id)).target
        obj = Organisation(**self._base(name, actor_id), enterprise_id=parent.id,
                           description=_text(description, 1000, "Description"), policy={})
        await self._insert_child(Level.ORGANISATION, obj)
        return obj

    async def create_bot(self, *, organisation_id: str, name: str, actor_id: str, description: str = "") -> Bot:
        parent = (await self._parent_for_child(Level.BOT, organisation_id)).target
        obj = Bot(**self._base(name, actor_id), enterprise_id=parent.enterprise_id, organisation_id=parent.id,
                  description=_text(description, 1000, "Description"))
        await self._insert_child(Level.BOT, obj)
        return obj

    async def create_instance(self, *, bot_id: str, name: str, channel: dict, actor_id: str,
                              telegram_bot_id: Optional[str] = None) -> BotInstance:
        """``telegram_bot_id`` comes from the (server-loaded) Telegram credential."""
        from core.bindings import claims_for
        parent = (await self._parent_for_child(Level.BOT_INSTANCE, bot_id)).target
        channel = dict(channel or {}) if isinstance(channel, dict) else {}
        if channel.get("type") == ChannelType.WEBCHAT.value:
            channel["public_id"] = new_public_id()        # never accepted from callers
        if channel.get("type") == ChannelType.DISCORD.value and (channel.get("guild_id") is not None or
                                                                 channel.get("application_id") is not None):
            raise HierarchyError("The Discord guild is set by the install flow.")
        binding = binding_from_doc(channel).validated()
        obj = BotInstance(**self._base(name, actor_id), enterprise_id=parent.enterprise_id,
                          organisation_id=parent.organisation_id, bot_id=parent.id, channel=binding,
                          telegram_bot_id=telegram_bot_id if binding.type is ChannelType.TELEGRAM else None)
        claims = claims_for(binding, telegram_bot_id=obj.telegram_bot_id)
        previous = await self.bindings.claim(obj.id, obj.enterprise_id, claims)     # atomic, before the insert

        async def undo():
            await self.bindings.restore(obj.id, obj.enterprise_id, previous)
        try:
            await self._insert_child(Level.BOT_INSTANCE, obj, on_abort=undo)
        except BaseException:
            await undo()                                   # idempotent; the claim never outlives a failed create
            raise
        return obj

    # ── updates ──
    async def _cas(self, level: Level, obj: Node, fields: dict) -> None:
        fields = {**fields, "updated_at": self.clock()}
        await self.store.update(COLLECTION[level], obj.id, fields, expect_version=obj.version)

    async def _rebind(self, inst: BotInstance, binding: ChannelBinding, extra_fields: dict, *,
                      telegram_bot_id: Optional[str] = None) -> None:
        """Change an instance's binding safely: claim old ∪ new, write the
        instance (compare-and-swap), then shrink the claim to the new binding.
        On failure the claim is restored to what the instance held before."""
        from core.bindings import claims_for
        if binding.type is not ChannelType.TELEGRAM:
            telegram_bot_id = None
        elif binding.credential_id == inst.channel.credential_id and telegram_bot_id is None:
            telegram_bot_id = inst.telegram_bot_id
        old = claims_for(inst.channel, telegram_bot_id=inst.telegram_bot_id)
        new = claims_for(binding, telegram_bot_id=telegram_bot_id)
        union = {k: tuple(dict.fromkeys(old.get(k, ()) + new.get(k, ()))) for k in set(old) | set(new)}
        previous = await self.bindings.claim(inst.id, inst.enterprise_id,
                                             {k: union[k] for k in new})    # only keys we need to add/grow
        try:
            await self._cas(Level.BOT_INSTANCE, inst, {"channel": binding_to_doc(binding),
                                                       "binding_scopes": _scope_strings(binding),
                                                       "telegram_bot_id": telegram_bot_id, **extra_fields})
        except BaseException:
            await self.bindings.restore(inst.id, inst.enterprise_id, previous)
            raise
        # shrink: new scopes are a subset of what we hold, so no check is needed
        await self.bindings.restore(inst.id, inst.enterprise_id, {k: v for k, v in new.items()})
        await self.bindings.release(inst.id, [k for k in old if k not in new])

    async def update(self, level: Level, object_id: str, changes: dict, *, actor_id: str,
                     telegram_bot_id: Optional[str] = None) -> Node:
        chain = await self.load_chain(level, object_id)
        obj = chain.target
        if not is_writable(obj.lifecycle):
            raise HierarchyError("Archived or deleted objects are read-only.")
        if not isinstance(changes, dict) or not changes:
            raise HierarchyError("Nothing to update.")
        illegal = set(changes) - MUTABLE_FIELDS[level]
        if illegal:
            raise HierarchyError(f"These fields cannot be changed: {', '.join(sorted(illegal))}.")
        fields: Dict[str, Any] = {}
        if "name" in changes:
            fields["name"] = _name(changes["name"])
        if "description" in changes:
            fields["description"] = _text(changes["description"], 1000, "Description")
        if "plan" in changes:
            if changes["plan"] not in PLANS:
                raise HierarchyError("Unknown plan.")
            fields["plan"] = changes["plan"]
        if "channel" in changes:
            if not isinstance(changes["channel"], dict):
                raise HierarchyError("Invalid channel settings.")
            new = dict(changes["channel"])
            if new.get("type", obj.channel.type.value) != obj.channel.type.value:
                raise HierarchyError("The channel type of an instance cannot be changed.")
            new["type"] = obj.channel.type.value
            if obj.channel.type is ChannelType.WEBCHAT:
                new["public_id"] = obj.channel.public_id          # rotated only by a dedicated operation
            if obj.channel.type is ChannelType.DISCORD and (
                    new.get("guild_id", obj.channel.guild_id) != obj.channel.guild_id or
                    new.get("application_id", obj.channel.application_id) != obj.channel.application_id):
                raise HierarchyError("The Discord guild is set by the install flow.")
            merged = {**binding_to_doc(obj.channel), **new}
            binding = binding_from_doc(merged).validated()
            await self._rebind(obj, binding, fields, telegram_bot_id=telegram_bot_id)
        else:
            await self._cas(level, obj, fields)
        return (await self.load_chain(level, object_id)).target

    async def bind_discord_guild(self, instance_id: str, guild_id: str, *, application_id: str,
                                 actor_id: str, channel_ids: Optional[List[str]] = None) -> BotInstance:
        """Called by the OAuth install callback (Phase 4D) for the issuing instance
        only. ``application_id`` is the instance's Enterprise application, loaded
        server-side, never client-supplied."""
        chain = await self.load_chain(Level.BOT_INSTANCE, instance_id)
        inst = chain.target
        if inst.channel.type is not ChannelType.DISCORD:
            raise HierarchyError("Not a Discord instance.")
        if not is_writable(inst.lifecycle):
            raise HierarchyError("Archived or deleted objects are read-only.")
        merged = {**binding_to_doc(inst.channel), "guild_id": guild_id, "application_id": application_id}
        if channel_ids is not None:
            merged["channel_ids"] = list(channel_ids)
        elif inst.channel.guild_id and (inst.channel.guild_id != guild_id or
                                        inst.channel.application_id != application_id):
            merged["channel_ids"] = []                 # channels of another server never carry over
        binding = binding_from_doc(merged).validated()
        await self._rebind(inst, binding, {})
        return (await self.load_chain(Level.BOT_INSTANCE, instance_id)).target

    async def unbind_discord(self, instance_id: str, *, actor_id: str) -> BotInstance:
        """Disconnect: remove only this instance's guild/channel binding and claims."""
        chain = await self.load_chain(Level.BOT_INSTANCE, instance_id)
        inst = chain.target
        if inst.channel.type is not ChannelType.DISCORD:
            raise HierarchyError("Not a Discord instance.")
        merged = {**binding_to_doc(inst.channel), "guild_id": None, "application_id": None, "channel_ids": [],
                  "dm_enabled": False}
        if merged.get("listen_mode") == "specific_channels":
            merged["listen_mode"] = "mention_only"
        await self._rebind(inst, binding_from_doc(merged).validated(), {})
        return (await self.load_chain(Level.BOT_INSTANCE, instance_id)).target

    async def set_bot_logo(self, bot_id: str, meta: Optional[dict], *, expect_version: Optional[int] = None) -> None:
        """Logo metadata on the Bot (the bytes live in ``bot_logos``; D-Q8)."""
        if meta is not None and set(meta) != {"content_type", "size", "sha256"}:
            raise HierarchyError("Invalid logo metadata.")
        await self.store.update(COLLECTION[Level.BOT], bot_id, {"logo": meta, "updated_at": self.clock()},
                                expect_version=expect_version)

    async def set_overrides(self, level: Level, object_id: str, overrides: dict, *,
                            expect_version: Optional[int] = None) -> None:
        """Persist an already-validated override map (validation: core.resolver).
        ``expect_version`` is the version the validation was computed against."""
        await self.store.update(COLLECTION[level], object_id, {"overrides": overrides, "updated_at": self.clock()},
                                expect_version=expect_version)

    async def set_policy(self, level: Level, object_id: str, policy: dict, *,
                         expect_version: Optional[int] = None) -> None:
        if level not in (Level.ENTERPRISE, Level.ORGANISATION):
            raise HierarchyError("Policies can only be set on Enterprises and Organisations.")
        await self.store.update(COLLECTION[level], object_id, {"policy": policy, "updated_at": self.clock()},
                                expect_version=expect_version)

    async def change_lifecycle(self, level: Level, object_id: str, action: LifecycleAction, *,
                               authority: Authority, actor_id: str, reason: str = "",
                               expect_version: Optional[int] = None) -> Node:
        """Compare-and-swap state transition: two concurrent transitions cannot
        both apply to the same version. Beginning deletion releases an
        instance's channel bindings immediately (runtime off)."""
        chain = await self.load_chain(level, object_id)
        target = chain.target
        if expect_version is not None and target.version != expect_version:
            raise ConcurrentModification()
        new = apply_action(target.lifecycle, action, authority=authority, actor_id=actor_id,
                           now=self.clock(), reason=reason)
        await self._cas(level, target, {"lifecycle": new.to_doc()})
        if new.state is LifecycleState.DELETED and level is Level.BOT_INSTANCE:
            await self.bindings.release(target.id, await self.bindings.keys_held_by(target.id))
        return (await self.load_chain(level, object_id)).target


def _scope_strings(binding: ChannelBinding) -> List[str]:
    return ["|".join(s) for s in binding.scopes()]


def _instance_doc(obj: BotInstance) -> dict:
    doc = to_doc(obj)
    doc["binding_scopes"] = _scope_strings(obj.channel)
    return doc
