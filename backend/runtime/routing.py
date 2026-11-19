"""Channel binding → Bot Instance resolution (spec 012 §5, §10; D-Q5).

The binding claims (``core.bindings``) are the routing table. An event routes
to an instance only when exactly one claim holder matches and the instance's
stored binding agrees with that claim; anything else fails closed:

* Webchat: ``webchat:<public_id>``.
* Telegram: ``telegram:bot:<bot_id>``; a webhook path hint must name the same
  instance.
* Discord guild message: holders of ``discord:<guild>`` whose scopes contain
  the channel (or ``*``).
* Discord DM (D-Q5): only instances with ``dm_enabled`` on a guild the author
  shares with the bot. Exactly one candidate routes; none is silent; more than
  one is ambiguous and refused, never "first tenant found".
"""
from __future__ import annotations

from typing import List, Optional

from core.bindings import BindingRegistry, WHOLE, claims_for
from core.hierarchy import COLLECTION, ChannelType, Level, from_doc, is_valid_id
from runtime.errors import AmbiguousRoute, BindingMismatch, NoRoute
from runtime.interfaces import ChannelAddress


async def _holders_for(reg: BindingRegistry, key: str, channel_id: Optional[str]) -> List[str]:
    holders = await reg.holders(key)
    out = []
    for iid, h in holders.items():
        scopes = h.get("scopes") or [WHOLE]
        if channel_id is None or WHOLE in scopes or channel_id in scopes:
            out.append(iid)
    return sorted(out)


async def resolve_instance(store, address: ChannelAddress) -> str:
    reg = BindingRegistry(store)
    if address.channel is ChannelType.WEBCHAT:
        if not address.public_id:
            raise NoRoute()
        key = f"webchat:{address.public_id}"
        candidates = await _holders_for(reg, key, None)
    elif address.channel is ChannelType.TELEGRAM:
        if not address.telegram_bot_id:
            raise NoRoute()
        key = f"telegram:bot:{address.telegram_bot_id}"
        candidates = await _holders_for(reg, key, None)
        if address.instance_hint is not None and candidates and candidates != [address.instance_hint]:
            raise BindingMismatch()
    elif address.channel is ChannelType.DISCORD and address.is_dm:
        if not address.application_id:
            raise NoRoute()
        candidates = []
        for guild in sorted(set(address.dm_shared_guilds)):
            for iid in await _holders_for(reg, f"discord:{address.application_id}:{guild}", None):
                doc = await store.get(COLLECTION[Level.BOT_INSTANCE], iid)
                if doc and (doc.get("channel") or {}).get("dm_enabled") is True and iid not in candidates:
                    candidates.append(iid)
        key = None
    elif address.channel is ChannelType.DISCORD:
        if not address.application_id or not address.guild_id or not address.channel_id:
            raise NoRoute()
        key = f"discord:{address.application_id}:{address.guild_id}"
        candidates = await _holders_for(reg, key, address.channel_id)
    else:
        raise NoRoute()
    if not candidates:
        raise NoRoute()
    if len(candidates) > 1:
        raise AmbiguousRoute()
    iid = candidates[0]
    await _verify(store, iid, address, key)
    return iid


async def _verify(store, instance_id: str, address: ChannelAddress, key: Optional[str]) -> None:
    """The instance document must agree with the claim that routed to it."""
    if not is_valid_id(instance_id):
        raise BindingMismatch()
    doc = await store.get(COLLECTION[Level.BOT_INSTANCE], instance_id)
    if doc is None:
        raise BindingMismatch()
    inst = from_doc(Level.BOT_INSTANCE, doc)
    if inst.channel.type is not address.channel:
        raise BindingMismatch()
    if address.channel is ChannelType.DISCORD and inst.channel.application_id != address.application_id:
        raise BindingMismatch()                          # an event of application A never reaches B's instance
    if key is not None and key not in claims_for(inst.channel, telegram_bot_id=inst.telegram_bot_id):
        raise BindingMismatch()


__all__ = ["resolve_instance"]
