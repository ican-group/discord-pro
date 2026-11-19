"""Spec 012: hierarchy models, server-assigned immutable ancestry, bindings."""
import unittest

from tests.unit._core_fixtures import Tree, run
from core.hierarchy import (COLLECTION, BotInstance, ChannelType, HierarchyError, IntegrityError, Level, NotFound,
                            new_id, sniff_logo, LOGO_MAX_BYTES, to_doc)
from core.lifecycle import Authority, LifecycleAction as A

GUILD = "123456789012345678"
GUILD2 = "223456789012345678"
CHAN = "323456789012345678"
CHAN2 = "423456789012345678"


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def test_valid_parent_child_creation_assigns_ancestry(self):
        e = self.t.enterprise()
        o = self.t.organisation(e.id)
        b = self.t.bot(o.id)
        i = self.t.instance(b.id)
        self.assertEqual(o.enterprise_id, e.id)
        self.assertEqual((b.enterprise_id, b.organisation_id), (e.id, o.id))
        self.assertEqual((i.enterprise_id, i.organisation_id, i.bot_id), (e.id, o.id, b.id))
        self.assertEqual(i.channel.type, ChannelType.WEBCHAT)
        self.assertTrue(i.channel.public_id.startswith("pk_"))

    def test_invalid_or_unknown_parent_rejected(self):
        for bad in (new_id(), "not-an-id", "", None):
            with self.subTest(bad=bad):
                with self.assertRaises(NotFound):
                    run(self.t.svc.create_organisation(enterprise_id=bad, name="x", actor_id="u"))
                with self.assertRaises(NotFound):
                    run(self.t.svc.create_bot(organisation_id=bad, name="x", actor_id="u"))
                with self.assertRaises(NotFound):
                    run(self.t.svc.create_instance(bot_id=bad, name="x", actor_id="u", channel={"type": "webchat"}))

    def test_wrong_parent_type_rejected(self):
        e = self.t.enterprise()
        o = self.t.organisation(e.id)
        with self.assertRaises(NotFound):          # an organisation id is not a bot id
            run(self.t.svc.create_instance(bot_id=o.id, name="x", actor_id="u", channel={"type": "webchat"}))
        with self.assertRaises(NotFound):          # an enterprise id is not an organisation id
            run(self.t.svc.create_bot(organisation_id=e.id, name="x", actor_id="u"))

    def test_ancestry_comes_from_the_parent_not_the_caller(self):
        e1, e2 = self.t.enterprise(), self.t.enterprise()
        o2 = self.t.organisation(e2.id)
        with self.assertRaises(TypeError):         # no way to pass ancestry ids
            run(self.t.svc.create_bot(organisation_id=o2.id, name="x", actor_id="u", enterprise_id=e1.id))
        b = self.t.bot(o2.id)
        self.assertEqual(b.enterprise_id, e2.id)

    def test_children_blocked_under_deleted_or_restricted_parents(self):
        e = self.t.enterprise()
        o = self.t.organisation(e.id)
        run(self.t.svc.change_lifecycle(Level.ORGANISATION, o.id, A.ARCHIVE, authority=Authority.OBJECT, actor_id="u"))
        with self.assertRaises(HierarchyError):
            self.t.bot(o.id)
        o2 = self.t.organisation(e.id)
        run(self.t.svc.change_lifecycle(Level.ORGANISATION, o2.id, A.BEGIN_DELETE, authority=Authority.OBJECT, actor_id="u"))
        with self.assertRaises(NotFound):
            self.t.bot(o2.id)
        o3 = self.t.organisation(e.id)
        run(self.t.svc.change_lifecycle(Level.ORGANISATION, o3.id, A.SUSPEND, authority=Authority.OBJECT, actor_id="u"))
        self.t.bot(o3.id)                          # disabled by own admins: preparation allowed

    def test_input_validation(self):
        e = self.t.enterprise()
        for name in ("", "   ", "x" * 121, "bad\x00name", None, 5):
            with self.subTest(name=name):
                with self.assertRaises(HierarchyError):
                    run(self.t.svc.create_organisation(enterprise_id=e.id, name=name, actor_id="u"))
        self.assertEqual(run(self.t.svc.create_enterprise(name="x", slug="MixedCase", actor_id="u")).slug, "mixedcase")
        for slug in ("", "-bad", "bad-", "x" * 51, "has space", "ünï"):
            with self.subTest(slug=slug):
                with self.assertRaises(HierarchyError):
                    run(self.t.svc.create_enterprise(name="x", slug=slug, actor_id="u"))
        with self.assertRaises(HierarchyError):
            run(self.t.svc.create_enterprise(name="x", slug=e.slug, actor_id="u"))      # slug unique


class ImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()

    def test_parent_and_ancestor_ids_cannot_be_changed(self):
        e2 = self.t.enterprise()
        o2 = self.t.organisation(e2.id)
        cases = [(Level.ORGANISATION, self.c.organisation.id, {"enterprise_id": e2.id}),
                 (Level.BOT, self.c.bot.id, {"organisation_id": o2.id}),
                 (Level.BOT, self.c.bot.id, {"enterprise_id": e2.id}),
                 (Level.BOT_INSTANCE, self.c.instance.id, {"bot_id": new_id()}),
                 (Level.BOT_INSTANCE, self.c.instance.id, {"id": new_id()}),
                 (Level.BOT, self.c.bot.id, {"lifecycle": {"state": "active"}}),
                 (Level.BOT, self.c.bot.id, {"overrides": {}}),
                 (Level.BOT, self.c.bot.id, {"created_by": "x"})]
        for level, oid, change in cases:
            with self.subTest(change=change):
                with self.assertRaises(HierarchyError):
                    run(self.t.svc.update(level, oid, change, actor_id="u"))
        chain = self.t.chain(Level.BOT_INSTANCE, self.c.instance.id)
        self.assertEqual(chain.ids(), self.c.ids())

    def test_channel_type_and_public_id_cannot_be_changed(self):
        with self.assertRaises(HierarchyError):
            run(self.t.svc.update(Level.BOT_INSTANCE, self.c.instance.id, {"channel": {"type": "telegram"}}, actor_id="u"))
        inst = run(self.t.svc.update(Level.BOT_INSTANCE, self.c.instance.id,
                                     {"channel": {"public_id": "pk_" + "A" * 30, "allowed_origins": ["https://a.example"]}},
                                     actor_id="u"))
        self.assertEqual(inst.channel.public_id, self.c.instance.channel.public_id)
        self.assertEqual(inst.channel.allowed_origins, ("https://a.example",))

    def test_permitted_updates(self):
        b = run(self.t.svc.update(Level.BOT, self.c.bot.id, {"name": "Renamed", "description": "d"}, actor_id="u"))
        self.assertEqual((b.name, b.description), ("Renamed", "d"))

    def test_archived_objects_are_read_only(self):
        run(self.t.svc.change_lifecycle(Level.BOT, self.c.bot.id, A.ARCHIVE, authority=Authority.OBJECT, actor_id="u"))
        with self.assertRaises(HierarchyError):
            run(self.t.svc.update(Level.BOT, self.c.bot.id, {"name": "x"}, actor_id="u"))


class ChainIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()

    def tamper(self, collection, oid, **fields):
        self.t.store.collections[collection][oid].update(fields)

    def test_chain_verifies_every_ancestor(self):
        e2 = self.t.enterprise()
        o2 = self.t.organisation(e2.id)
        # a bot claiming another enterprise than its organisation
        self.tamper("bots", self.c.bot.id, enterprise_id=e2.id)
        with self.assertRaises(IntegrityError):
            self.t.chain(Level.BOT_INSTANCE, self.c.instance.id)
        self.tamper("bots", self.c.bot.id, enterprise_id=self.c.enterprise.id)
        # an instance pointing to a bot in another tenant
        b2 = self.t.bot(o2.id)
        self.tamper("bot_instances", self.c.instance.id, bot_id=b2.id)
        with self.assertRaises(IntegrityError):
            self.t.chain(Level.BOT_INSTANCE, self.c.instance.id)

    def test_orphans_cannot_load(self):
        del self.t.store.collections["organisations"][self.c.organisation.id]
        with self.assertRaises(NotFound):
            self.t.chain(Level.BOT_INSTANCE, self.c.instance.id)

    def test_stored_type_mismatch_fails_closed(self):
        doc = self.t.store.collections["bots"][self.c.bot.id]
        self.t.store.collections["organisations"][self.c.bot.id] = dict(doc)
        with self.assertRaises(IntegrityError):
            self.t.chain(Level.ORGANISATION, self.c.bot.id)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        e = self.t.enterprise()
        o = self.t.organisation(e.id)
        self.b = self.t.bot(o.id)
        e2 = self.t.enterprise()
        o2 = self.t.organisation(e2.id)
        self.b2 = self.t.bot(o2.id)

    def discord(self, bot_id, **ch):
        inst = self.t.instance(bot_id, {"type": "discord", **ch})
        return inst

    def bind(self, inst, guild):
        return run(self.t.svc.bind_discord_guild(inst.id, guild, application_id="910000000000000001", actor_id="u"))

    def test_discord_whole_guild_binding_is_exclusive_across_tenants(self):
        i1 = self.bind(self.discord(self.b.id), GUILD)
        self.assertEqual(i1.channel.guild_id, GUILD)
        i2 = self.discord(self.b2.id)
        with self.assertRaises(HierarchyError) as ctx:
            self.bind(i2, GUILD)
        self.assertNotIn(self.b.id, str(ctx.exception))        # non-disclosing
        self.bind(i2, GUILD2)

    def test_discord_channel_scopes(self):
        i1 = self.bind(self.discord(self.b.id, listen_mode="specific_channels", channel_ids=[CHAN]), GUILD)
        i2 = self.bind(self.discord(self.b.id, listen_mode="specific_channels", channel_ids=[CHAN2]), GUILD)
        self.assertNotEqual(i1.id, i2.id)               # multiple instances, distinct bindings
        i3 = self.discord(self.b.id, listen_mode="specific_channels", channel_ids=[CHAN])
        with self.assertRaises(HierarchyError):
            self.bind(i3, GUILD)
        i4 = self.discord(self.b.id)                     # whole guild overlaps channel bindings
        with self.assertRaises(HierarchyError):
            self.bind(i4, GUILD)

    def test_guild_is_set_only_by_install_flow(self):
        i = self.bind(self.discord(self.b.id), GUILD)
        with self.assertRaises(HierarchyError):
            run(self.t.svc.update(Level.BOT_INSTANCE, i.id, {"channel": {"guild_id": GUILD2}}, actor_id="u"))

    def test_discord_validation_and_dm_rule(self):
        bad = [{"listen_mode": "everything"}, {"reply_style": "shout"}, {"channel_ids": ["abc"]},
               {"listen_mode": "specific_channels"}, {"handoff_cooldown_minutes": -1},
               {"dm_enabled": True}]                       # DMs need a bound guild (D-Q5)
        for ch in bad:
            with self.subTest(ch=ch):
                with self.assertRaises(HierarchyError):
                    self.discord(self.b.id, **ch)
        inst = self.discord(self.b.id)
        self.assertFalse(inst.channel.dm_enabled)        # off by default
        inst = self.bind(inst, GUILD)
        inst = run(self.t.svc.update(Level.BOT_INSTANCE, inst.id, {"channel": {"dm_enabled": True}}, actor_id="u"))
        self.assertTrue(inst.channel.dm_enabled)

    def test_telegram_token_serves_one_instance(self):
        cred = new_id()
        self.t.instance(self.b.id, {"type": "telegram", "credential_id": cred})
        with self.assertRaises(HierarchyError):
            self.t.instance(self.b2.id, {"type": "telegram", "credential_id": cred})
        for ch in ({"listen_mode": "loud"}, {"credential_id": "nope"}, {"chat_ids": ["abc"]}):
            with self.subTest(ch=ch):
                with self.assertRaises(HierarchyError):
                    self.t.instance(self.b.id, {"type": "telegram", **ch})

    def test_webchat_public_id_is_server_assigned_and_origins_validated(self):
        i = self.t.instance(self.b.id, {"type": "webchat", "public_id": "pk_" + "X" * 30})
        self.assertNotEqual(i.channel.public_id, "pk_" + "X" * 30)
        for origins in (["http://a.example"], ["https://a.example/path"], ["javascript:alert(1)"]):
            with self.subTest(origins=origins):
                with self.assertRaises(HierarchyError):
                    self.t.instance(self.b.id, {"type": "webchat", "allowed_origins": origins})

    def test_unknown_channel_rejected(self):
        for ch in ({"type": "zendesk"}, {"type": "email"}, {}, None):
            with self.subTest(ch=ch):
                with self.assertRaises(HierarchyError):
                    run(self.t.svc.create_instance(bot_id=self.b.id, name="x", actor_id="u", channel=ch))

    def test_deleted_instance_releases_binding(self):
        i1 = self.bind(self.discord(self.b.id), GUILD)
        run(self.t.svc.change_lifecycle(Level.BOT_INSTANCE, i1.id, A.BEGIN_DELETE, authority=Authority.OBJECT, actor_id="u"))
        self.bind(self.discord(self.b2.id), GUILD)

    def test_round_trip(self):
        i = self.bind(self.discord(self.b.id, staff_role_name="Support"), GUILD)
        stored = run(self.t.store.get(COLLECTION[Level.BOT_INSTANCE], i.id))
        self.assertEqual(stored["binding_scopes"], [f"discord|{GUILD}|*"])
        self.assertIsInstance(i, BotInstance)
        self.assertEqual(to_doc(i)["channel"]["staff_role_name"], "Support")


class LogoTests(unittest.TestCase):
    def test_sniffing(self):
        self.assertEqual(sniff_logo(b"\x89PNG\r\n\x1a\n" + b"0" * 10), "image/png")
        self.assertEqual(sniff_logo(b"\xff\xd8\xff\xe0" + b"0" * 10), "image/jpeg")
        self.assertEqual(sniff_logo(b"GIF89a" + b"0" * 10), "image/gif")
        self.assertEqual(sniff_logo(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        for bad in (b"", b"<svg onload=alert(1)>", b"%PDF-1.7", b"\x89PNG\r\n\x1a\n" + b"0" * LOGO_MAX_BYTES):
            with self.subTest(bad=bad[:10]):
                with self.assertRaises(HierarchyError):
                    sniff_logo(bad)


if __name__ == "__main__":
    unittest.main()
