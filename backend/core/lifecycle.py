"""One lifecycle model for every hierarchy object (spec 012 §10).

States: ACTIVE, SUSPENDED, ARCHIVED, DELETED. "Disabled" is SUSPENDED with
``authority = OBJECT`` (decision D-Q2). A suspension records the authority that
imposed it; only the same or a higher authority may lift it. DELETED means a
permanent deletion is in progress: it is irreversible and the records are
removed by the deletion job (Phase 3).

Availability is never stored on children: an object is available only when it
and every ancestor are ACTIVE (:func:`availability`).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable, Optional, Tuple


class LifecycleState(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"
    DELETED = "deleted"


class Authority(IntEnum):
    """Who imposed a suspension. Higher values outrank lower ones."""
    OBJECT = 1        # the object's own admins ("disabled")
    ENTERPRISE = 2    # an Enterprise owner acting on a descendant
    PLATFORM = 3      # a platform superadmin (enforcement)


class LifecycleAction(str, Enum):
    SUSPEND = "suspend"
    RESUME = "resume"
    ARCHIVE = "archive"
    UNARCHIVE = "unarchive"
    BEGIN_DELETE = "begin_delete"


class LifecycleError(ValueError):
    """A transition is not allowed. ``str(exc)`` is safe to show to admins."""


@dataclass(frozen=True)
class Lifecycle:
    state: LifecycleState = LifecycleState.ACTIVE
    authority: Optional[Authority] = None     # set only while SUSPENDED
    reason: str = ""
    changed_at: str = ""
    changed_by: str = ""

    def to_doc(self) -> dict:
        return {"state": self.state.value,
                "authority": int(self.authority) if self.authority is not None else None,
                "reason": self.reason, "changed_at": self.changed_at, "changed_by": self.changed_by}

    @classmethod
    def from_doc(cls, doc: Optional[dict]) -> "Lifecycle":
        doc = doc or {}
        auth = doc.get("authority")
        return cls(state=LifecycleState(doc.get("state", "active")),
                   authority=Authority(auth) if auth is not None else None,
                   reason=doc.get("reason", ""), changed_at=doc.get("changed_at", ""),
                   changed_by=doc.get("changed_by", ""))


def apply_action(current: Lifecycle, action: LifecycleAction, *, authority: Authority,
                 actor_id: str, now: str, reason: str = "") -> Lifecycle:
    """Return the new lifecycle or raise :class:`LifecycleError`.

    ``authority`` is the authority of the actor *for this object* (computed by
    ``core.authz.lifecycle_authority``).
    """
    state = current.state
    if state is LifecycleState.DELETED:
        raise LifecycleError("This object is being permanently deleted.")
    stamp = dict(changed_at=now, changed_by=actor_id, reason=reason[:500])

    if action is LifecycleAction.SUSPEND:
        if state is LifecycleState.ACTIVE:
            return Lifecycle(LifecycleState.SUSPENDED, authority, **stamp)
        if state is LifecycleState.SUSPENDED:
            if authority > (current.authority or Authority.OBJECT):
                return Lifecycle(LifecycleState.SUSPENDED, authority, **stamp)   # escalate
            raise LifecycleError("This object is already suspended.")
        raise LifecycleError("Only active objects can be suspended.")

    if action is LifecycleAction.RESUME:
        if state is not LifecycleState.SUSPENDED:
            raise LifecycleError("Only suspended objects can be resumed.")
        if authority < (current.authority or Authority.OBJECT):
            raise LifecycleError("This suspension can only be lifted by the authority that imposed it.")
        return Lifecycle(LifecycleState.ACTIVE, None, **stamp)

    if action is LifecycleAction.ARCHIVE:
        if state is LifecycleState.ACTIVE:
            return Lifecycle(LifecycleState.ARCHIVED, None, **stamp)
        if state is LifecycleState.SUSPENDED:
            if authority < (current.authority or Authority.OBJECT):
                raise LifecycleError("A suspension imposed by a higher authority prevents archiving.")
            return Lifecycle(LifecycleState.ARCHIVED, None, **stamp)
        raise LifecycleError("This object is already archived.")

    if action is LifecycleAction.UNARCHIVE:
        if state is not LifecycleState.ARCHIVED:
            raise LifecycleError("Only archived objects can be restored from the archive.")
        return Lifecycle(LifecycleState.ACTIVE, None, **stamp)

    if action is LifecycleAction.BEGIN_DELETE:
        return Lifecycle(LifecycleState.DELETED, None, **stamp)

    raise LifecycleError("Unknown lifecycle action.")


@dataclass(frozen=True)
class Availability:
    available: bool
    blocking_level: Optional[str] = None
    blocking_id: Optional[str] = None
    blocking_state: Optional[LifecycleState] = None

    @property
    def reason(self) -> str:
        if self.available:
            return ""
        return f"{self.blocking_level} {self.blocking_state.value}"


def availability(chain: Iterable[Tuple[str, str, Lifecycle]]) -> Availability:
    """``chain`` is ordered root first: ``(level, object_id, lifecycle)``.

    Returns the first (highest) non-ACTIVE object as the blocking reason.
    """
    for level, object_id, lc in chain:
        if lc.state is not LifecycleState.ACTIVE:
            return Availability(False, level, object_id, lc.state)
    return Availability(True)


def can_create_child_under(parent: Lifecycle) -> bool:
    """Children may be created under ACTIVE parents and parents disabled by
    their own admins (so a deployment can be prepared while paused)."""
    if parent.state is LifecycleState.ACTIVE:
        return True
    return parent.state is LifecycleState.SUSPENDED and parent.authority is Authority.OBJECT


def is_writable(lc: Lifecycle) -> bool:
    """Configuration of archived or deleted objects is read-only."""
    return lc.state in (LifecycleState.ACTIVE, LifecycleState.SUSPENDED)


__all__ = ["LifecycleState", "Authority", "LifecycleAction", "LifecycleError", "Lifecycle",
           "apply_action", "Availability", "availability", "can_create_child_under", "is_writable"]

