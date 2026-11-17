"""manage.py: one-time superadmin bootstrap and explicit legacy migrations
(spec 004, R1, R2, C6). Uses an in-memory fake DB; no MongoDB required."""
import asyncio
import io
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._support import FakeDB
import manage

EMAIL = "admin@bitbotpro.io"
# Synthetic test value only (not a real credential).
STRONG = "Tq7#vLm2!pZx9&Rw"
FAKE_HASH = "fakehash$not-a-real-bcrypt-hash"


def run(coro):
    return asyncio.run(coro)


def bootstrap(db, *, confirm, email=EMAIL, password=STRONG):
    out = io.StringIO()
    code = run(manage.bootstrap_superadmin(
        db, email, password, confirm=confirm,
        hasher=lambda _pw: FAKE_HASH, now_fn=lambda: "2026-11-17T00:00:00+00:00", out=out))
    return code, out.getvalue()


class BootstrapTests(unittest.TestCase):
    def test_dry_run_creates_nothing(self):
        db = FakeDB()
        code, out = bootstrap(db, confirm=False)
        self.assertEqual(code, manage.EXIT_DRY_RUN)
        self.assertEqual(db.users.docs, [])
        self.assertEqual(db.mutating_writes(), [])
        self.assertIn("DRY RUN", out)

    def test_confirm_creates_exactly_one_superadmin_and_audit(self):
        db = FakeDB()
        code, out = bootstrap(db, confirm=True)
        self.assertEqual(code, manage.EXIT_OK)
        self.assertEqual(len(db.users.docs), 1)
        user = db.users.docs[0]
        self.assertEqual(user["email"], EMAIL)
        self.assertEqual(user["role"], "superadmin")
        self.assertEqual(user["platform_role"], "superadmin")
        self.assertTrue(user["is_verified"])
        self.assertEqual(user["hashed_password"], FAKE_HASH)
        self.assertNotIn(STRONG, repr(user))
        self.assertEqual(len(db.audit_log.docs), 1)
        self.assertEqual(db.audit_log.docs[0]["action"], "bootstrap_superadmin")
        self.assertIn(((("email", 1),), {"unique": True}), db.users.indexes)

    def test_second_bootstrap_refused(self):
        db = FakeDB()
        bootstrap(db, confirm=True)
        code, out = bootstrap(db, confirm=True, email="other@bitbotpro.io")
        self.assertEqual(code, manage.EXIT_REFUSED)
        self.assertEqual(len(db.users.docs), 1)
        self.assertIn("one-time", out)

    def test_refused_when_existing_platform_superadmin(self):
        db = FakeDB()
        db.users.docs.append({"email": "x@example.org", "role": "user", "platform_role": "superadmin"})
        code, _ = bootstrap(db, confirm=True)
        self.assertEqual(code, manage.EXIT_REFUSED)
        self.assertEqual(len(db.users.docs), 1)

    def test_refused_when_email_already_registered(self):
        db = FakeDB()
        db.users.docs.append({"email": EMAIL, "role": "user"})
        code, _ = bootstrap(db, confirm=True)
        self.assertEqual(code, manage.EXIT_REFUSED)
        self.assertEqual(len(db.users.docs), 1)

    def test_email_is_normalised(self):
        db = FakeDB()
        code, _ = bootstrap(db, confirm=True, email="  Admin@BitBotPro.io ")
        self.assertEqual(code, manage.EXIT_OK)
        self.assertEqual(db.users.docs[0]["email"], EMAIL)

    def test_weak_password_rejected_before_any_db_access(self):
        db = FakeDB()
        code, out = bootstrap(db, confirm=True, password="ChangeMe123!")
        self.assertEqual(code, manage.EXIT_INVALID)
        self.assertEqual(db.writes, [])
        self.assertNotIn("ChangeMe123!", out)

    def test_invalid_email(self):
        for bad in ("", "admin", "admin@", "@bitbotpro.io", "a b@x.io"):
            with self.subTest(bad=bad):
                db = FakeDB()
                code, _ = bootstrap(db, confirm=True, email=bad)
                self.assertEqual(code, manage.EXIT_INVALID)
                self.assertEqual(db.writes, [])

    def test_output_never_contains_password_or_hash(self):
        for confirm in (False, True):
            db = FakeDB()
            _, out = bootstrap(db, confirm=confirm)
            self.assertNotIn(STRONG, out)
            self.assertNotIn(FAKE_HASH, out)


class PasswordFileTests(unittest.TestCase):
    def _write(self, content):
        f = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_strips_single_trailing_newline(self):
        self.assertEqual(manage.read_password_file(self._write(STRONG + "\n")), STRONG)
        self.assertEqual(manage.read_password_file(self._write(STRONG + "\r\n")), STRONG)
        self.assertEqual(manage.read_password_file(self._write(STRONG)), STRONG)

    def test_rejects_multiline(self):
        with self.assertRaises(ValueError):
            manage.read_password_file(self._write("a\nb\n"))

    def test_cli_has_no_password_argument(self):
        parser = manage._parser()
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            parser.parse_args(["bootstrap-superadmin", "--email", EMAIL, "--password", STRONG])


class LegacyMigrationTests(unittest.TestCase):
    def _db_with_legacy_data(self):
        db = FakeDB()
        db.bot_instances.docs.append({"id": "inst-1"})
        db.bot_config.docs.append({"name": "x"})
        db.conversations.docs.append({"session_id": "s"})
        db.knowledge_sources.docs.append({"title": "t"})
        return db

    def _run(self, db, **kw):
        out = io.StringIO()
        params = dict(only=None, apply=False, confirm=False, allow_hardcoded_accounts=False)
        params.update(kw)
        code = run(manage.legacy_migrations(db, out=out, now_fn=lambda: "t", **params))
        return code, out.getvalue()

    def test_dry_run_by_default_changes_nothing(self):
        db = self._db_with_legacy_data()
        code, out = self._run(db)
        self.assertEqual(code, manage.EXIT_DRY_RUN)
        self.assertEqual(db.mutating_writes(), [])
        self.assertIn("DRY RUN", out)

    def test_apply_requires_confirm(self):
        db = self._db_with_legacy_data()
        code, _ = self._run(db, apply=True)
        self.assertEqual(code, manage.EXIT_DRY_RUN)
        self.assertEqual(db.mutating_writes(), [])

    def test_workspaces_requires_explicit_flag(self):
        db = self._db_with_legacy_data()
        code, out = self._run(db, apply=True, confirm=True)
        self.assertEqual(code, manage.EXIT_REFUSED)
        self.assertEqual(db.mutating_writes(), [])
        self.assertIn("hard-coded", out)

    def test_instance_backfill_skips_knowledge_sources_in_api_mode(self):
        db = self._db_with_legacy_data()
        with mock.patch.dict(os.environ, {"APP_MODE": "api"}):
            code, _ = self._run(db, only=["instance-backfill"], apply=True, confirm=True)
        self.assertEqual(code, manage.EXIT_OK)
        self.assertEqual(db.bot_config.docs[0]["instance_id"], "inst-1")
        self.assertEqual(db.conversations.docs[0]["instance_id"], "inst-1")
        self.assertNotIn("instance_id", db.knowledge_sources.docs[0])
        self.assertNotIn(("update_many", "knowledge_sources"), db.writes)
        self.assertEqual(db.audit_log.docs[0]["action"], "legacy_migration:instance-backfill")

    def test_instance_backfill_includes_knowledge_sources_in_all_mode(self):
        db = self._db_with_legacy_data()
        with mock.patch.dict(os.environ, {"APP_MODE": "all"}):
            self._run(db, only=["instance-backfill"], apply=True, confirm=True)
        self.assertEqual(db.knowledge_sources.docs[0]["instance_id"], "inst-1")

    def test_discord_metadata_noop_without_global_app(self):
        db = FakeDB()
        db.connected_accounts.docs.append({"id": "a1", "provider": "discord"})
        code, _ = self._run(db, only=["discord-metadata"], apply=True, confirm=True)
        self.assertEqual(code, manage.EXIT_OK)
        self.assertNotIn("metadata", db.connected_accounts.docs[0])


if __name__ == "__main__":
    unittest.main()
