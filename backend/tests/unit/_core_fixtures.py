"""Shared helpers for spec 012 core tests (pure Python, no database)."""
import asyncio
import dataclasses

from tests.unit._support import BACKEND_DIR  # noqa: F401  (adds backend/ to sys.path)

from core.hierarchy import (Chain, HierarchyService, InMemoryStore, Level, new_id)
from core.lifecycle import Lifecycle


def run(coro):
    return asyncio.run(coro)


class Tree:
    """Builds hierarchies through the real HierarchyService on an in-memory store."""

    def __init__(self):
        self.store = InMemoryStore()
        self.svc = HierarchyService(self.store, clock=lambda: "2026-11-18T00:00:00+00:00")
        self.actor = new_id()

    def enterprise(self, name="Acme", slug=None):
        return run(self.svc.create_enterprise(name=name, slug=slug or f"e{new_id()[:8]}", actor_id=self.actor))

    def organisation(self, enterprise_id, name="Org"):
        return run(self.svc.create_organisation(enterprise_id=enterprise_id, name=name, actor_id=self.actor))

    def bot(self, organisation_id, name="Bot"):
        return run(self.svc.create_bot(organisation_id=organisation_id, name=name, actor_id=self.actor))

    def instance(self, bot_id, channel=None, name="Instance"):
        return run(self.svc.create_instance(bot_id=bot_id, name=name, actor_id=self.actor,
                                            channel=channel or {"type": "webchat"}))

    def full(self):
        """Enterprise -> Organisation -> Bot -> web Instance; returns the instance chain."""
        e = self.enterprise()
        o = self.organisation(e.id)
        b = self.bot(o.id)
        i = self.instance(b.id)
        return self.chain(Level.BOT_INSTANCE, i.id)

    def chain(self, level, object_id) -> Chain:
        return run(self.svc.load_chain(level, object_id))


def with_overrides(chain: Chain, **per_level):
    """Return a copy of ``chain`` with overrides/policy replaced per level, e.g.
    with_overrides(c, enterprise={"overrides": {...}, "policy": {...}}, bot={...})."""
    nodes = {}
    for attr in ("enterprise", "organisation", "bot", "instance"):
        node = getattr(chain, attr)
        changes = per_level.get(attr)
        if node is not None and changes:
            node = dataclasses.replace(node, **changes)
        nodes[attr] = node
    return Chain(**nodes)


def with_state(chain: Chain, attr: str, lifecycle: Lifecycle) -> Chain:
    return with_overrides(chain, **{attr: {"lifecycle": lifecycle}})
