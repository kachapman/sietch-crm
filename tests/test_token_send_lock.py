#!/usr/bin/env python3
"""Offline tests: API-token (Bearer) mail sends obey the recipient lock.

2026-11-10, Ken 8:43 PM ET. Covers:
  * the shared lock helper (every non-empty lock must pass; conflicting locks
    refuse rather than resolve by precedence)
  * source guards: the send handler AND the compose preflight both call the
    shared enforcement, the token lock column is read at auth time, the final
    send helper re-checks the lock, and there is still exactly one send route
  * the admin script is dry-run by default
  * DB-gated: a Bearer token cannot send to a non-locked recipient, can send to
    crm@vanguardadj.online, an unlocked token is unaffected, and a reply /
    forward / draft send (same endpoint) cannot slip past the lock. The owner's
    own users.mail_send_locked_recipient applies to token requests too.

Pure + source-guard tests need no database. Handler tests skip without DB_HOST
(mirrors tests/test_mail_send_recipient_lock.py).

Run with: python3 tests/test_token_send_lock.py
"""

import contextlib
import importlib.util
import io
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import network_guard  # noqa: E402

network_guard.install()

import mail_recipient_lock as L  # noqa: E402

SCRIPT = ROOT / "scripts" / "set_token_mail_send_lock.py"
LOCKED = "crm@vanguardadj.online"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestPure(unittest.TestCase):
    def test_no_locks_allows_anyone(self):
        self.assertEqual(L.check_locks([], ["x@example.invalid"]), (True, ""))

    def test_blank_locks_ignored(self):
        self.assertEqual(L.check_locks(["", "  "], ["x@example.invalid"]), (True, ""))

    def test_single_token_lock_allows_locked_address(self):
        self.assertEqual(L.check_locks([LOCKED], [LOCKED]), (True, ""))

    def test_single_token_lock_refuses_other(self):
        ok, why = L.check_locks([LOCKED], ["x@example.invalid"])
        self.assertFalse(ok)
        self.assertIn("not the locked", why)

    def test_single_token_lock_refuses_extra_recipient(self):
        self.assertFalse(L.check_locks([LOCKED], [LOCKED, "x@example.invalid"])[0])

    def test_same_lock_twice_is_not_conflicting(self):
        self.assertEqual(L.check_locks([LOCKED, LOCKED.upper()], [LOCKED]), (True, ""))

    def test_two_different_locks_always_refuse(self):
        # Neither lock may be weakened by the other.
        ok, why = L.check_locks([LOCKED, "billing@example.invalid"], [LOCKED])
        self.assertFalse(ok)
        self.assertIn("conflicting locks", why)

    def test_display_name_wrapper_accepted(self):
        recips = L.parse_recipients(f"Vanguard CRM <{LOCKED}>")
        self.assertTrue(L.check_locks([LOCKED], recips)[0])


class TestSourceGuards(unittest.TestCase):
    """The rules must live in the shared helpers, not be duplicated per path."""

    @classmethod
    def setUpClass(cls):
        cls.server = _read(ROOT / "server.py")
        cls.tokens = _read(ROOT / "api_tokens.py")
        cls.send = _read(ROOT / "mail_send.py")

    def test_send_handler_uses_shared_enforcement(self):
        start = self.server.index("def _handle_mail_send(self)")
        end = self.server.index("def _handle_mail_send_undo(self)")
        body = self.server[start:end]
        self.assertIn("_enforce_mail_send_lock(", body)
        self.assertNotIn("SELECT mail_send_locked_recipient FROM users", body)

    def test_preflight_uses_shared_enforcement(self):
        start = self.server.index("def _handle_mail_send_preflight(self)")
        end = self.server.index("def _handle_mail_send(self)")
        self.assertIn("_enforce_mail_send_lock(", self.server[start:end])

    def test_lock_helper_reads_user_and_token_locks(self):
        start = self.server.index("def _mail_send_locks(")
        end = self.server.index("def _enforce_mail_send_lock(")
        body = self.server[start:end]
        self.assertIn("SELECT mail_send_locked_recipient FROM users", body)
        self.assertIn("api_token_mail_send_locked_recipient", body)

    def test_only_one_send_route_exists(self):
        self.assertEqual(self.server.count('api_path == "/api/v2/mail/send" and'),
                         1)
        self.assertNotIn('"/api/v2/mail/send/now"', self.server)

    def test_token_auth_reads_the_token_lock(self):
        self.assertIn("t.mail_send_locked_recipient", self.tokens)
        self.assertIn('"api_token_mail_send_locked_recipient"', self.tokens)

    def test_token_create_stores_the_lock(self):
        self.assertIn("mail_send_locked_recipient", self.tokens)
        self.assertIn("mail_send_locked_recipient=payload.get(\"mail_send_locked_recipient\")",
                      self.server)

    def test_inline_ddl_mirrors_migration(self):
        self.assertIn(
            "ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS mail_send_locked_recipient TEXT",
            self.server)
        mig = ROOT / "migrations" / "053_api_token_mail_send_lock.sql"
        self.assertTrue(mig.exists())
        self.assertIn("mail_send_locked_recipient", _read(mig))

    def test_send_helper_rechecks_lock_before_any_network(self):
        start = self.send.index("def try_send_for_account(")
        guard = self.send.index("if recipient_locks:", start)
        graph = self.send.index("send_email_via_graph", start)
        smtp = self.send.index("send_email_from_account(", start)
        self.assertLess(guard, graph)
        self.assertLess(guard, smtp)
        self.assertIn('return False, f"Recipient lock: {_lock_why}. Nothing was sent.", "blocked"',
                      self.send)

    def test_mcp_has_no_send_tool(self):
        tools = _read(ROOT / "mcp_tools.py")
        self.assertIn('FORBIDDEN_PATH_MARKERS = ("/mail/send", "/mail/send/undo")', tools)


class _FakeDb:
    """Minimal db stand-in for the admin script."""

    def __init__(self, row):
        self.row = row
        self.executed = []

    def init_db(self):
        pass

    def query_one(self, sql, params=None):
        return self.row

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return 1


class TestAdminScript(unittest.TestCase):
    def _load(self, fake):
        spec = importlib.util.spec_from_file_location("set_token_lock_script", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        saved = sys.modules.get("db")
        sys.modules["db"] = fake
        try:
            spec.loader.exec_module(mod)
        finally:
            if saved is None:
                sys.modules.pop("db", None)
            else:
                sys.modules["db"] = saved
        return mod

    def _row(self):
        return {"id": 7, "name": "acceptance", "token_prefix": "sietch_ab12",
                "created_by": 1, "revoked_at": None,
                "mail_send_locked_recipient": None}

    def test_dry_run_by_default(self):
        fake = _FakeDb(self._row())
        mod = self._load(fake)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.main(["--token-id", "7", "--set", LOCKED])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.executed, [])
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn(LOCKED, out.getvalue())

    def test_apply_writes_the_lock(self):
        fake = _FakeDb(self._row())
        mod = self._load(fake)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mod.main(["--token-id", "7", "--set", LOCKED, "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.executed), 1)
        self.assertIn(LOCKED, fake.executed[0][1])

    def test_clear_writes_null(self):
        row = self._row()
        row["mail_send_locked_recipient"] = LOCKED
        fake = _FakeDb(row)
        mod = self._load(fake)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mod.main(["--token-id", "7", "--clear", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIsNone(fake.executed[0][1][0])

    def test_unknown_token_is_an_error_not_a_write(self):
        fake = _FakeDb(None)
        mod = self._load(fake)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mod.main(["--token-id", "999", "--set", LOCKED, "--apply"])
        self.assertEqual(rc, 2)
        self.assertEqual(fake.executed, [])


@unittest.skipUnless(os.environ.get("DB_HOST"), "no database")
class TestTokenSends(unittest.TestCase):
    """Live handler checks (needs PostgreSQL). Nothing is ever sent."""

    @classmethod
    def setUpClass(cls):
        import db
        import auth
        import api_tokens
        import mail_recipient_lock

        cls.db = db
        cls.auth = auth
        cls.tokens = api_tokens
        cls.lock = mail_recipient_lock
        db.init_db()
        if db.query_one(
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_name='api_tokens' AND column_name='mail_send_locked_recipient'"
        ) is None:
            raise unittest.SkipTest("migration 053 not applied")
        tag = uuid.uuid4().hex[:8]
        cls.tag = tag
        cls.user = db.query_one(
            """INSERT INTO users (email, display_name, password_hash, password_salt,
                                  is_admin, is_active, role)
               VALUES (%s,'TokenOwner','x','x',FALSE,TRUE,'member') RETURNING id""",
            (f"token-owner-{tag}@example.invalid",))
        cls.acct = db.query_one(
            """INSERT INTO mail_accounts (email, display_name, imap_host,
                                          password_encrypted, owner_user_id)
               VALUES (%s,'Token Acct','imap.example.invalid','x',%s) RETURNING id""",
            (f"token-acct-{tag}@example.invalid", cls.user["id"]))
        cls.locked_token = api_tokens.create_api_token(
            f"locked-{tag}", cls.user["id"], "full",
            mail_send_locked_recipient=LOCKED)["token"]
        cls.open_token = api_tokens.create_api_token(
            f"open-{tag}", cls.user["id"], "full")["token"]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.execute("DELETE FROM mail_outgoing WHERE account_id=%s", (cls.acct["id"],))
            cls.db.execute("DELETE FROM mail_messages WHERE account_id=%s", (cls.acct["id"],))
            cls.db.execute("DELETE FROM api_token_audit_log WHERE token_id IN "
                           "(SELECT id FROM api_tokens WHERE name LIKE %s)",
                           (f"%-{cls.tag}",))
            cls.db.execute("DELETE FROM api_tokens WHERE name LIKE %s", (f"%-{cls.tag}",))
            cls.db.execute("DELETE FROM mail_accounts WHERE id=%s", (cls.acct["id"],))
            cls.db.execute("DELETE FROM users WHERE id=%s", (cls.user["id"],))
        except Exception:
            pass

    def _send(self, token, **extra):
        import mcp_local_api
        body = {"account_id": self.acct["id"], "to": LOCKED,
                "subject": "s", "body": "<p>b</p>"}
        body.update(extra)
        return mcp_local_api.call(token, "POST", "/api/v2/mail/send", body)

    def _patched(self):
        from unittest.mock import patch
        import server
        return (
            patch.object(server.KanbanHandler, "_try_send_for_account",
                         return_value=(True, None, "smtp_password")),
            patch.object(server.KanbanHandler, "_create_email_history_event",
                         return_value=True),
            patch.object(server.mail_sender_guard, "evaluate",
                         return_value={"allowed": True, "message": "", "reason": "",
                                       "deal_id": None, "original_account_id": None}),
        )

    def _with_patches(self, fn):
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            return fn()

    def test_locked_address_allowed(self):
        r = self._with_patches(lambda: self._send(self.locked_token))
        self.assertEqual(r.status, 200, r.text)

    def test_other_recipient_refused(self):
        r = self._with_patches(
            lambda: self._send(self.locked_token, to="x@example.invalid"))
        self.assertEqual(r.status, 403, r.text)
        self.assertIn("Recipient lock", r.text)

    def test_cc_and_bcc_refused(self):
        for key in ("cc", "bcc"):
            r = self._with_patches(
                lambda key=key: self._send(self.locked_token, **{key: "x@example.invalid"}))
            self.assertEqual(r.status, 403, r.text)
            self.assertIn("Recipient lock", r.text)

    def test_unlocked_token_unaffected(self):
        r = self._with_patches(
            lambda: self._send(self.open_token, to="x@example.invalid"))
        self.assertEqual(r.status, 200, r.text)

    def test_reply_and_forward_cannot_bypass(self):
        # reply_to_message_id / in-reply-to both land on the same locked handler.
        r = self._with_patches(lambda: self._send(
            self.locked_token, to="x@example.invalid", reply_to_message_id=999999))
        self.assertEqual(r.status, 403, r.text)
        self.assertIn("Recipient lock", r.text)

    def test_draft_send_cannot_bypass(self):
        r = self._with_patches(lambda: self._send(
            self.locked_token, to="x@example.invalid", draft_id=999999))
        self.assertEqual(r.status, 403, r.text)
        self.assertIn("Recipient lock", r.text)

    def test_owner_user_lock_applies_to_token_requests(self):
        self.db.execute("UPDATE users SET mail_send_locked_recipient=%s WHERE id=%s",
                        (LOCKED, self.user["id"]))
        try:
            r = self._with_patches(
                lambda: self._send(self.open_token, to="x@example.invalid"))
            self.assertEqual(r.status, 403, r.text)
            self.assertIn("Recipient lock", r.text)
            ok = self._with_patches(lambda: self._send(self.open_token))
            self.assertEqual(ok.status, 200, ok.text)
        finally:
            self.db.execute("UPDATE users SET mail_send_locked_recipient=NULL WHERE id=%s",
                            (self.user["id"],))

    def test_preflight_refuses_locked_recipient(self):
        import mcp_local_api
        r = mcp_local_api.call(
            self.locked_token, "POST", "/api/v2/mail/send/preflight",
            {"account_id": self.acct["id"], "to": "x@example.invalid"})
        self.assertEqual(r.status, 403, r.text)
        self.assertIn("Recipient lock", r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
