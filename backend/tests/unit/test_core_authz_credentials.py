"""Spec 012 §8-9: memberships, roles, permissions and credential ownership."""
import unittest

from tests.unit._core_fixtures import Tree, with_overrides
from core.authz import (Action, Actor, AuthzError, Membership, Role, can, check_grant, effective_role,
                        ensure_owner_remains, ensure_superadmin_remains, lifecycle_authority)
from core.credentials import (CredentialError, CredentialStatus, Provider, SecretBox, check_manage,
                              check_manage_platform, check_reference_permission, check_use, new_credential,
                              replace_secret, revoke)
from core.hierarchy import Chain, Level, new_id
from core.lifecycle import Authority
from core.resolver import resolve

KEY = "dW5pdC10ZXN0LWZlcm5ldC1rZXktbm90LXJlYWwtMDA="
SECRET = "sk-ant-UNITTEST-secret-value-0000"


class Base(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.c = self.t.full()
        self.other = self.t.full()                    # another Enterprise
        self.alice, self.bob, self.carol, self.dave = (Actor(new_id()) for _ in range(4))
        e, o, b = self.c.enterprise.id, self.c.organisation.id, self.c.bot.id
        self.ms = [
            Membership(self.alice.user_id, e, Level.ENTERPRISE, e, Role.OWNER),
            Membership(self.bob.user_id, e, Level.ENTERPRISE, e, Role.VIEWER),
            Membership(self.bob.user_id, e, Level.ORGANISATION, o, Role.ADMIN),
            Membership(self.carol.user_id, e, Level.ENTERPRISE, e, Role.VIEWER),
            Membership(self.carol.user_id, e, Level.BOT, b, Role.EDITOR),
        ]
        self.policy = resolve(self.c).policy

    def chain(self, level):
        return {"enterprise": Chain(self.c.enterprise), "organisation": Chain(self.c.enterprise, self.c.organisation),
                "bot": Chain(self.c.enterprise, self.c.organisation, self.c.bot), "instance": self.c}[level]

    def allowed(self, actor, action, level, policy=None):
        return can(actor, action, self.chain(level), self.ms, policy or self.policy).allowed


class RoleTests(Base):
    def test_roles_apply_downwards(self):
        self.assertEqual(effective_role(self.bob, self.chain("enterprise"), self.ms).role, Role.VIEWER)
        self.assertEqual(effective_role(self.bob, self.chain("organisation"), self.ms).role, Role.ADMIN)
        self.assertEqual(effective_role(self.bob, self.chain("instance"), self.ms).role, Role.ADMIN)
        self.assertEqual(effective_role(self.carol, self.chain("organisation"), self.ms).role, Role.VIEWER)
        self.assertEqual(effective_role(self.carol, self.chain("bot"), self.ms).role, Role.EDITOR)
        self.assertEqual(effective_role(self.alice, self.chain("instance"), self.ms).role, Role.OWNER)
        self.assertIsNone(effective_role(self.dave, self.chain("bot"), self.ms).role)

    def test_role_restrictions(self):
        self.assertTrue(self.allowed(self.carol, Action.EDIT_CONFIG, "bot"))
        self.assertFalse(self.allowed(self.carol, Action.CREATE_CHILD, "bot"))
        self.assertFalse(self.allowed(self.carol, Action.EDIT_CONFIG, "organisation"))
        self.assertTrue(self.allowed(self.bob, Action.MANAGE_CHANNEL, "instance"))
        self.assertFalse(self.allowed(self.bob, Action.EDIT_POLICY, "enterprise"))
        self.assertTrue(self.allowed(self.bob, Action.EDIT_POLICY, "organisation"))
        self.assertFalse(self.allowed(self.bob, Action.EDIT_POLICY, "bot"))          # policies only at E/O
        self.assertFalse(self.allowed(self.bob, Action.MANAGE_BILLING, "enterprise"))
        self.assertTrue(self.allowed(self.alice, Action.MANAGE_BILLING, "enterprise"))
        self.assertFalse(self.allowed(self.alice, Action.MANAGE_BILLING, "organisation"))

    def test_delete_needs_authority_above_the_object(self):
        self.assertFalse(self.allowed(self.bob, Action.DELETE, "organisation"))    # own org
        self.assertTrue(self.allowed(self.bob, Action.DELETE, "bot"))
        self.assertTrue(self.allowed(self.alice, Action.DELETE, "organisation"))
        self.assertTrue(self.allowed(self.alice, Action.DELETE, "enterprise"))
        admin = Actor(new_id())
        self.ms.append(Membership(admin.user_id, self.c.enterprise.id, Level.ENTERPRISE, self.c.enterprise.id, Role.ADMIN))
        self.assertFalse(self.allowed(admin, Action.DELETE, "enterprise"))         # owner only
        # X7 (approved 2026-11-18): only Enterprise Owners delete Organisations
        self.assertFalse(self.allowed(admin, Action.DELETE, "organisation"))
        self.assertTrue(self.allowed(admin, Action.DELETE, "bot"))
        self.assertTrue(self.allowed(admin, Action.DELETE, "instance"))

    def test_cross_tenant_memberships_never_apply(self):
        foreign_owner = Actor(new_id())
        oe = self.other.enterprise.id
        ms = self.ms + [Membership(foreign_owner.user_id, oe, Level.ENTERPRISE, oe, Role.OWNER),
                        # forged membership: right scope id, wrong enterprise
                        Membership(foreign_owner.user_id, oe, Level.BOT, self.c.bot.id, Role.ADMIN)]
        for level in ("enterprise", "organisation", "bot", "instance"):
            with self.subTest(level=level):
                self.assertIsNone(effective_role(foreign_owner, self.chain(level), ms).role)
                self.assertFalse(can(foreign_owner, Action.READ, self.chain(level), ms, self.policy).allowed)

    def test_superadmin(self):
        root = Actor(new_id(), is_platform_superadmin=True)
        self.assertTrue(all(can(root, a, self.chain("enterprise"), [], self.policy).allowed for a in Action))
        self.assertEqual(lifecycle_authority(root, self.chain("bot"), []), Authority.PLATFORM)
        self.assertEqual(lifecycle_authority(self.alice, self.chain("bot"), self.ms), Authority.ENTERPRISE)
        self.assertEqual(lifecycle_authority(self.bob, self.chain("bot"), self.ms), Authority.OBJECT)

    def test_membership_validation(self):
        e, o = self.c.enterprise.id, self.c.organisation.id
        with self.assertRaises(AuthzError):
            Membership(new_id(), e, Level.ORGANISATION, o, Role.OWNER).validated()
        with self.assertRaises(AuthzError):
            Membership(new_id(), e, Level.BOT_INSTANCE, self.c.instance.id, Role.ADMIN).validated()
        with self.assertRaises(AuthzError):
            Membership(new_id(), e, Level.ENTERPRISE, self.other.enterprise.id, Role.ADMIN).validated()

    def test_resolver_reports_permissions_and_overridability(self):
        eff = resolve(self.chain("bot"), actor=self.carol, memberships=self.ms)
        self.assertEqual(eff.role, "editor")
        self.assertTrue(eff.permissions["edit_config"])
        self.assertFalse(eff.permissions["manage_members"])
        self.assertTrue(eff.overridable["behaviour.persona"])
        self.assertFalse(eff.overridable["knowledge.mode"])            # admin-level setting
        # Stage 0 (2026-11-20): editors select the AI provider/model/credential (bounded by policy)
        self.assertTrue(eff.overridable["ai.provider"])
        self.assertTrue(eff.overridable["ai.credential_id"])
        eff = resolve(self.chain("bot"), actor=self.bob, memberships=self.ms)
        self.assertTrue(eff.overridable["knowledge.mode"])


class GrantTests(Base):
    def test_cannot_grant_above_own_role(self):
        org = self.chain("organisation")
        member = Actor(new_id())
        self.ms.append(Membership(member.user_id, self.c.enterprise.id, Level.ENTERPRISE, self.c.enterprise.id, Role.VIEWER))
        check_grant(self.bob, org, self.ms, self.policy, target_user_id=member.user_id, new_role=Role.ADMIN)
        with self.assertRaises(AuthzError):
            check_grant(self.bob, self.chain("enterprise"), self.ms, self.policy,
                        target_user_id=member.user_id, new_role=Role.ADMIN)       # bob is only viewer there
        with self.assertRaises(AuthzError):
            check_grant(self.carol, self.chain("bot"), self.ms, self.policy,
                        target_user_id=member.user_id, new_role=Role.EDITOR)      # editors cannot manage members
        with self.assertRaises(AuthzError):
            check_grant(self.bob, org, self.ms, self.policy, target_user_id=member.user_id, new_role=Role.OWNER)

    def test_owner_only_at_enterprise_and_only_by_owner(self):
        target = Actor(new_id())
        e = self.chain("enterprise")
        self.ms.append(Membership(target.user_id, self.c.enterprise.id, Level.ENTERPRISE, self.c.enterprise.id, Role.VIEWER))
        check_grant(self.alice, e, self.ms, self.policy, target_user_id=target.user_id, new_role=Role.OWNER,
                    existing_role=Role.VIEWER)
        admin = Actor(new_id())
        self.ms.append(Membership(admin.user_id, self.c.enterprise.id, Level.ENTERPRISE, self.c.enterprise.id, Role.ADMIN))
        with self.assertRaises(AuthzError):
            check_grant(admin, e, self.ms, self.policy, target_user_id=target.user_id, new_role=Role.OWNER,
                        existing_role=Role.VIEWER)
        with self.assertRaises(AuthzError):                # admin cannot change the owner
            check_grant(admin, e, self.ms, self.policy, target_user_id=self.alice.user_id, new_role=Role.VIEWER,
                        existing_role=Role.OWNER)

    def test_members_must_already_belong_to_the_enterprise(self):
        outsider = Actor(new_id())
        with self.assertRaises(AuthzError):
            check_grant(self.bob, self.chain("organisation"), self.ms, self.policy,
                        target_user_id=outsider.user_id, new_role=Role.VIEWER)
        foreign = Actor(new_id())
        oe = self.other.enterprise.id
        self.ms.append(Membership(foreign.user_id, oe, Level.ENTERPRISE, oe, Role.OWNER))
        with self.assertRaises(AuthzError):
            check_grant(self.bob, self.chain("organisation"), self.ms, self.policy,
                        target_user_id=foreign.user_id, new_role=Role.VIEWER)

    def test_cannot_raise_own_role(self):
        with self.assertRaises(AuthzError):
            check_grant(self.bob, self.chain("organisation"), self.ms, self.policy,
                        target_user_id=self.bob.user_id, new_role=Role.OWNER, existing_role=Role.ADMIN)

    def test_delegation_policy_limits_lower_level_admins(self):
        member = Actor(new_id())
        self.ms.append(Membership(member.user_id, self.c.enterprise.id, Level.ENTERPRISE, self.c.enterprise.id, Role.VIEWER))
        restricted = resolve(with_overrides(self.c, enterprise={"policy": {"delegation.org_admins_manage_members": False,
                                                                          "delegation.org_admins_manage_credentials": False}})).policy
        with self.assertRaises(AuthzError):
            check_grant(self.bob, self.chain("organisation"), self.ms, restricted,
                        target_user_id=member.user_id, new_role=Role.VIEWER)
        self.assertFalse(can(self.bob, Action.MANAGE_CREDENTIALS, self.chain("organisation"), self.ms, restricted).allowed)
        # enterprise-level authority is unaffected
        check_grant(self.alice, self.chain("organisation"), self.ms, restricted,
                    target_user_id=member.user_id, new_role=Role.VIEWER)

    def test_last_owner_and_last_superadmin(self):
        with self.assertRaises(AuthzError):
            ensure_owner_remains(self.ms, self.c.enterprise.id, removing_user_id=self.alice.user_id)
        second = Membership(new_id(), self.c.enterprise.id, Level.ENTERPRISE, self.c.enterprise.id, Role.OWNER)
        ensure_owner_remains(self.ms + [second], self.c.enterprise.id, removing_user_id=self.alice.user_id)
        ensure_owner_remains(self.ms, self.c.enterprise.id, removing_user_id=self.bob.user_id)
        with self.assertRaises(AuthzError):
            ensure_superadmin_remains(["root"], removing_user_id="root")
        ensure_superadmin_remains(["root", "root2"], removing_user_id="root")


class CredentialTests(Base):
    def setUp(self):
        super().setUp()
        self.box = SecretBox(KEY)

    def make(self, owner_chain, provider=Provider.ANTHROPIC):
        return new_credential(credential_id=new_id(), chain=owner_chain, provider=provider, name="Prod key",
                              secret=SECRET, box=self.box, actor_id=self.alice.user_id, now="t")

    def test_encrypted_at_rest_and_never_exposed(self):
        cred = self.make(self.chain("organisation"))
        self.assertNotIn(SECRET, cred.secret_enc)
        self.assertEqual(self.box.decrypt(cred.secret_enc), SECRET)
        for text in (repr(cred), str(cred.public_view()), cred.fingerprint):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SECRET[-6:], text)
        self.assertNotIn("secret_enc", cred.public_view())
        with self.assertRaises(CredentialError):
            SecretBox(KEY).decrypt("tampered")

    def test_owner_scope(self):
        with self.assertRaises(CredentialError):
            self.make(self.chain("bot"))
        self.assertEqual(self.make(self.chain("enterprise")).owner_level, Level.ENTERPRISE)
        self.assertEqual(self.make(self.chain("organisation")).owner_level, Level.ORGANISATION)

    def test_use_vs_manage_permissions(self):
        org_chain = self.chain("organisation")
        check_reference_permission(self.carol, self.chain("bot"), self.ms, self.policy)      # editor may use
        with self.assertRaises(AuthzError):
            check_manage(self.carol, org_chain, self.ms, self.policy)                         # but not manage
        check_manage(self.bob, org_chain, self.ms, self.policy)
        with self.assertRaises(AuthzError):
            check_manage(self.bob, self.chain("enterprise"), self.ms, self.policy)
        viewer = Actor(self.carol.user_id)
        with self.assertRaises(AuthzError):
            check_reference_permission(viewer, self.chain("organisation"), self.ms, self.policy)

    def test_cross_tenant_and_scope_rules(self):
        foreign = self.make(Chain(self.other.enterprise))
        sibling_org = self.t.organisation(self.c.enterprise.id)
        sibling = self.make(Chain(self.c.enterprise, sibling_org))
        for cred in (foreign, sibling, None):
            with self.subTest(cred=getattr(cred, "id", None)):
                with self.assertRaises(CredentialError) as ctx:
                    check_use(cred, referencing_chain=self.chain("bot"), slot="ai.anthropic", policy=self.policy)
                self.assertEqual(str(ctx.exception), "The selected credential is not available here.")
        own = self.make(self.chain("organisation"))
        check_use(own, referencing_chain=self.c, slot="ai.anthropic", policy=self.policy)
        with self.assertRaises(CredentialError):
            check_use(own, referencing_chain=self.c, slot="channel.telegram", policy=self.policy)

    def test_rotation_keeps_id_and_revocation_fails_closed(self):
        cred = self.make(self.chain("organisation"))
        rotated = replace_secret(cred, secret="sk-ant-UNITTEST-new-0000", box=self.box, now="t2")
        self.assertEqual(rotated.id, cred.id)
        self.assertNotEqual(rotated.fingerprint, cred.fingerprint)
        self.assertEqual(self.box.decrypt(rotated.secret_enc), "sk-ant-UNITTEST-new-0000")
        gone = revoke(rotated, now="t3")
        self.assertEqual((gone.status, gone.secret_enc), (CredentialStatus.REVOKED, ""))
        with self.assertRaises(CredentialError):
            check_use(gone, referencing_chain=self.c, slot="ai.anthropic", policy=self.policy)
        with self.assertRaises(CredentialError):
            replace_secret(gone, secret="x", box=self.box, now="t4")

    def test_platform_credentials_are_superadmin_only(self):
        check_manage_platform(Actor(new_id(), is_platform_superadmin=True))
        for actor in (self.alice, self.bob):
            with self.assertRaises(AuthzError):
                check_manage_platform(actor)


if __name__ == "__main__":
    unittest.main()
