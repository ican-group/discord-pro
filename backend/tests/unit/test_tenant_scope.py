"""tenant_scope (spec 008, N2): login and /auth/me list only bots the user may access.
Uses the in-memory FakeDB; no MongoDB."""
import asyncio
import unittest

from tests.unit._support import FakeDB
import tenant_scope as ts


def run(coro):
    return asyncio.run(coro)


def seeded():
    db = FakeDB()
    db.workspaces.docs = [{"id": "ws-a"}, {"id": "ws-b"}]
    db.organisations.docs = [
        {"id": "org-a1", "workspace_id": "ws-a", "member_ids": ["alice"]},
        {"id": "org-a2", "workspace_id": "ws-a", "member_ids": []},
        {"id": "org-b1", "workspace_id": "ws-b", "member_ids": ["bob"]},
    ]
    db.workspace_members.docs = [
        {"workspace_id": "ws-a", "user_id": "wsadmin", "role": "admin"},
        {"workspace_id": "ws-a", "user_id": "wsowner", "role": "owner"},
        {"workspace_id": "ws-a", "user_id": "wsmember", "role": "member"},
        {"workspace_id": "ws-b", "user_id": "bob", "role": "member"},
    ]
    db.bots.docs = [
        {"id": "bot-a1", "name": "A1", "org_id": "org-a1"},
        {"id": "bot-a2", "name": "A2", "org_id": "org-a2"},
        {"id": "bot-b1", "name": "B1 secret tenant", "org_id": "org-b1"},
        {"id": "bot-orphan", "name": "orphan", "org_id": "org-missing"},
    ]
    return db


def ids(db, user):
    return sorted(i["id"] for i in run(ts.accessible_instances(db, user)))


class AccessibleInstancesTests(unittest.TestCase):
    def setUp(self):
        self.db = seeded()

    def test_org_member_sees_only_own_org(self):
        self.assertEqual(ids(self.db, {"id": "alice", "role": "user"}), ["bot-a1"])

    def test_other_tenant_member_sees_only_their_bots(self):
        got = run(ts.accessible_instances(self.db, {"id": "bob", "role": "user"}))
        self.assertEqual(got, [{"id": "bot-b1", "name": "B1 secret tenant"}])

    def test_workspace_admin_and_owner_see_workspace_orgs(self):
        for uid in ("wsadmin", "wsowner"):
            with self.subTest(uid=uid):
                self.assertEqual(ids(self.db, {"id": uid, "role": "user"}), ["bot-a1", "bot-a2"])

    def test_plain_workspace_member_without_org_membership_sees_nothing(self):
        self.assertEqual(ids(self.db, {"id": "wsmember", "role": "user"}), [])

    def test_user_with_no_memberships_sees_nothing(self):
        self.assertEqual(ids(self.db, {"id": "stranger", "role": "user"}), [])

    def test_missing_user_id_sees_nothing(self):
        self.assertEqual(ids(self.db, {"role": "user"}), [])

    def test_superadmin_sees_all(self):
        everything = ["bot-a1", "bot-a2", "bot-b1", "bot-orphan"]
        self.assertEqual(ids(self.db, {"id": "root", "role": "superadmin"}), everything)
        self.assertEqual(ids(self.db, {"id": "root", "role": "user", "platform_role": "superadmin"}), everything)

    def test_never_returns_org_ids_or_extra_fields(self):
        for item in run(ts.accessible_instances(self.db, {"id": "root", "role": "superadmin"})):
            self.assertEqual(set(item), {"id", "name"})

    def test_cross_tenant_isolation_both_directions(self):
        a = set(ids(self.db, {"id": "alice", "role": "user"}))
        b = set(ids(self.db, {"id": "bob", "role": "user"}))
        self.assertFalse(a & b)
        self.assertNotIn("bot-b1", a)
        self.assertNotIn("bot-a1", b)

    def test_read_only(self):
        run(ts.accessible_instances(self.db, {"id": "alice", "role": "user"}))
        self.assertEqual(self.db.writes, [])

    def test_never_broader_than_instance_access_rules(self):
        """Each returned bot satisfies the V2 rule of server._verify_instance_access."""
        for uid in ("alice", "bob", "wsadmin", "wsowner", "wsmember", "stranger"):
            for bot_id in ids(self.db, {"id": uid, "role": "user"}):
                bot = next(b for b in self.db.bots.docs if b["id"] == bot_id)
                org = next(o for o in self.db.organisations.docs if o["id"] == bot["org_id"])
                member = uid in org["member_ids"]
                ws_admin = any(m["user_id"] == uid and m["workspace_id"] == org["workspace_id"]
                               and m["role"] in ("admin", "owner") for m in self.db.workspace_members.docs)
                self.assertTrue(member or ws_admin, (uid, bot_id))


if __name__ == "__main__":
    unittest.main()
