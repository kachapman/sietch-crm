#!/usr/bin/env python3
"""Sender guard + token/IMAP fixes (synthetic data, no network).

Run with: python3 tests/test_mail_sender_guard.py
"""

import json
import socket
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import network_guard  # noqa: E402

network_guard.install()

try:
    import db
    import auth
    import mail_sender_guard as guard
    import mcp_local_api
    import server
    import mail_oauth
    import mail_send
    HAVE_DB = True
except Exception as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    HAVE_DB = False

S = uuid.uuid4().hex[:8]
IDS = {}


class FakeDB:
    """Tiny DB double for pure-logic tests."""

    def __init__(self, one=None, rows=None, exec_result=1):
        self._one = one or {}
        self._rows = rows or []
        self.writes = []
        self.exec_result = exec_result

    def query_one(self, sql, params=None):
        if callable(self._one):
            return self._one(sql, params)
        return self._one

    def query_dicts(self, sql, params=None):
        if callable(self._rows):
            return self._rows(sql, params)
        return self._rows

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return self.exec_result


class TestGuardLogic(unittest.TestCase):
    def test_new_email_is_allowed_from_any_account(self):
        dbx = FakeDB(one={}, rows=[])
        out = guard.evaluate(dbx, account={"id": 3, "email": "c@example.invalid"}, deal_id=9)
        self.assertTrue(out["allowed"])
        self.assertIsNone(out["original_account_id"])

    def test_reply_from_wrong_account_blocked(self):
        dbx = FakeDB(one=lambda sql, p: {"account_id": 1})
        out = guard.evaluate(dbx, account={"id": 2, "email": "b@example.invalid"},
                             reply_to_message_id=44)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], guard.REASON_ORIGINAL_MISMATCH)
        self.assertEqual(out["original_account_id"], 1)

    def test_reply_from_original_account_allowed(self):
        dbx = FakeDB(one=lambda sql, p: {"account_id": 2})
        out = guard.evaluate(dbx, account={"id": 2, "email": "b@example.invalid"},
                             reply_to_message_id=44)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["original_account_id"], 2)


class TestNetworkGuard(unittest.TestCase):
    def test_non_localhost_connect_is_blocked(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(network_guard.NetworkBlocked):
                sock.connect(("203.0.113.1", 443))
        finally:
            sock.close()

    def test_localhost_is_allowed(self):
        # The test DB is localhost; a refused connection is fine, the guard
        # must not be what stops it.
        try:
            socket.create_connection(("127.0.0.1", 1), timeout=0.2)
        except network_guard.NetworkBlocked:
            self.fail("localhost must not be blocked")
        except OSError:
            pass


@unittest.skipUnless(HAVE_DB, "no database")
class TestHandlerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if db.query_one(
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_name='mail_outgoing' AND column_name='error'"
        ) is None:
            raise unittest.SkipTest("migration 050 not applied")
        cls.user = db.query_one(
            """INSERT INTO users (email, display_name, password_hash, password_salt,
                                  is_admin, is_active, role)
               VALUES (%s, 'Guard Test', 'x', 'x', TRUE, TRUE, 'admin') RETURNING id""",
            (f"guard-{S}@example.invalid",))
        cls.acct = db.query_one(
            """INSERT INTO mail_accounts (email, display_name, smtp_from_name, imap_host,
                   password_encrypted, owner_user_id)
               VALUES (%s, 'Mapped', 'Mapped Brand', 'imap.example.invalid', 'x', %s)
               RETURNING id""",
            (f"mapped-{S}@example.invalid", cls.user["id"]))
        cls.other_acct = db.query_one(
            """INSERT INTO mail_accounts (email, display_name, imap_host,
                   password_encrypted, owner_user_id)
               VALUES (%s, 'Other', 'imap.example.invalid', 'x', %s) RETURNING id""",
            (f"other-{S}@example.invalid", cls.user["id"]))
        cls.deal = db.query_one(
            """INSERT INTO opportunities (title, responsible_user_id, created_by)
               VALUES (%s, %s, %s) RETURNING id""",
            (f"Guard job {S}", cls.user["id"], cls.user["id"]))
        cls.message = db.query_one(
            """INSERT INTO mail_messages (account_id, imap_uid, message_id, subject)
               VALUES (%s, %s, %s, 'original') RETURNING id""",
            (cls.acct["id"], f"uid-{S}", f"orig-{S}@example.invalid"))
        cls.session = auth.create_session(cls.user["id"], ip_address="test")

    @classmethod
    def tearDownClass(cls):
        try:
            db.execute("DELETE FROM mail_outgoing WHERE account_id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM mail_messages WHERE account_id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM api_tokens WHERE created_by = %s", (cls.user["id"],))
            db.execute("DELETE FROM sessions WHERE user_id = %s", (cls.user["id"],))
            db.execute("DELETE FROM opportunities WHERE id = %s", (cls.deal["id"],))
            db.execute("DELETE FROM mail_accounts WHERE id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM users WHERE id = %s", (cls.user["id"],))
        except Exception as exc:  # pragma: no cover
            print(f"cleanup warning: {exc}")

    def _send(self, account_id, **extra):
        body = {"account_id": account_id, "to": "someone@example.invalid",
                "subject": "s", "body": "<p>b</p>", "deal_id": self.deal["id"]}
        body.update(extra)
        return mcp_local_api.call_session(self.session, "POST", "/api/v2/mail/send", body)

    def test_reply_from_wrong_account_returns_409_and_writes_nothing(self):
        before = db.query_one("SELECT count(*) AS n FROM mail_outgoing")
        resp = self._send(self.other_acct["id"],
                          reply_to_message_id=self.message["id"])
        self.assertEqual(resp.status, 409, resp.text)
        self.assertIn("reply/forward", resp.text.lower())
        after = db.query_one("SELECT count(*) AS n FROM mail_outgoing")
        self.assertEqual(before["n"], after["n"])

    def test_new_email_can_use_any_account(self):
        with patch.object(server.KanbanHandler, "_try_send_for_account",
                          return_value=(True, None, "smtp_password")), \
             patch.object(server.KanbanHandler, "_create_email_history_event",
                          return_value=True):
            resp = self._send(self.other_acct["id"])
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(resp.payload["sentAs"], f"other-{S}@example.invalid")

    def test_reply_from_original_account_sends_and_records_sent_as(self):
        with patch.object(server.KanbanHandler, "_try_send_for_account",
                          return_value=(True, None, "smtp_password")), \
             patch.object(server.KanbanHandler, "_create_email_history_event",
                          return_value=True):
            resp = self._send(self.acct["id"],
                              reply_to_message_id=self.message["id"])
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(resp.payload["sentAs"], f"mapped-{S}@example.invalid")

    def test_failed_send_records_error_and_fallback(self):
        with patch.object(server.KanbanHandler, "_try_send_for_account",
                          side_effect=lambda *a, **k: (
                              k["result_meta"].update(fallback_reason="graph: boom")
                              or (False, "boom", "smtp_password"))):
            resp = self._send(self.acct["id"])
        self.assertEqual(resp.status, 500)
        row = db.query_one(
            "SELECT error, fallback_reason FROM mail_outgoing WHERE account_id = %s "
            "ORDER BY id DESC LIMIT 1", (self.acct["id"],))
        self.assertEqual(row["error"], "boom")
        self.assertEqual(row["fallback_reason"], "graph: boom")

    def test_credential_identity_mismatch_is_refused(self):
        db.execute("UPDATE mail_accounts SET smtp_user = %s WHERE id = %s",
                   ("other@example.invalid", self.acct["id"]))
        try:
            before = db.query_one("SELECT count(*) AS n FROM mail_outgoing")
            resp = self._send(self.acct["id"])
            self.assertEqual(resp.status, 409, resp.text)
            self.assertIn("Refusing to send", resp.text)
            after = db.query_one("SELECT count(*) AS n FROM mail_outgoing")
            self.assertEqual(before["n"], after["n"])
        finally:
            db.execute("UPDATE mail_accounts SET smtp_user = NULL WHERE id = %s",
                       (self.acct["id"],))

    def test_preflight_blocks_and_allows(self):
        bad = mcp_local_api.call_session(
            self.session, "POST", "/api/v2/mail/send/preflight",
            {"account_id": self.other_acct["id"], "deal_id": self.deal["id"],
             "reply_to_message_id": self.message["id"]})
        self.assertEqual(bad.status, 409, bad.text)
        self.assertEqual(bad.payload["reason"], guard.REASON_ORIGINAL_MISMATCH)
        good = mcp_local_api.call_session(
            self.session, "POST", "/api/v2/mail/send/preflight",
            {"account_id": self.acct["id"], "deal_id": self.deal["id"],
             "reply_to_message_id": self.message["id"]})
        self.assertEqual(good.status, 200, good.text)
        self.assertEqual(good.payload["originalAccountId"], self.acct["id"])


class TestNoFallback(unittest.TestCase):
    """No failure may substitute another account, credential or method."""

    def _acct(self, **kw):
        a = {"id": 5, "email": "a@example.invalid", "oauth_provider": "microsoft",
             "oauth_access_token": "AT", "oauth_refresh_token": "RT",
             "oauth_token_expires": None,
             "oauth_graph_access_token": "GAT", "oauth_graph_refresh_token": "GRT",
             "oauth_graph_identity": "a@example.invalid",
             "smtp_host": "smtp.example.invalid", "smtp_port": 587,
             "smtp_user": "a@example.invalid", "smtp_from_name": "A",
             "smtp_use_tls": True}
        a.update(kw)
        return a

    def test_graph_error_does_not_fall_back_to_smtp(self):
        with patch("mail_graph_client.send_email_via_graph", return_value=(False, "graph boom")), \
             patch("smtp_client.send_email_from_account") as smtp:
            ok, err, method = mail_send.try_send_for_account(
                self._acct(), to_addr="x@example.invalid", subject="s",
                refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertFalse(ok)
        self.assertEqual(method, "graph")
        self.assertIn("graph boom", err)
        smtp.assert_not_called()

    def test_graph_identity_mismatch_never_sends(self):
        with patch("mail_graph_client.send_email_via_graph") as g, \
             patch("smtp_client.send_email_from_account") as smtp:
            ok, err, method = mail_send.try_send_for_account(
                self._acct(oauth_graph_identity="other@example.invalid"),
                to_addr="x@example.invalid", subject="s",
                refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertFalse(ok)
        g.assert_not_called()
        smtp.assert_not_called()

    def test_graph_refresh_failure_never_falls_back(self):
        with patch("mail_graph_client.send_email_via_graph") as g, \
             patch("smtp_client.send_email_from_account") as smtp:
            ok, err, method = mail_send.try_send_for_account(
                self._acct(), to_addr="x@example.invalid", subject="s",
                refresh_graph_token=lambda aid: (None, "refresh failed"))
        self.assertFalse(ok)
        self.assertIn("refresh failed", err)
        g.assert_not_called()
        smtp.assert_not_called()

    def test_smtp_only_account_never_uses_graph(self):
        with patch("mail_graph_client.send_email_via_graph") as g, \
             patch("smtp_client.send_email_from_account", return_value=(False, "535 auth")) as smtp:
            ok, err, method = mail_send.try_send_for_account(
                self._acct(oauth_graph_access_token=None, oauth_graph_refresh_token=None,
                           oauth_graph_identity=None),
                to_addr="x@example.invalid", subject="s",
                refresh_graph_token=lambda aid: (None, None))
        self.assertFalse(ok)
        g.assert_not_called()
        smtp.assert_called_once()
        self.assertEqual(smtp.call_args.kwargs["smtp_user"], "a@example.invalid")
        self.assertEqual(smtp.call_args.kwargs["from_addr"], "a@example.invalid")

    def test_smtp_auth_failure_is_not_retried_via_graph(self):
        with patch("mail_graph_client.send_email_via_graph") as g, \
             patch("mail_send._set_smtp_auth_disabled"), \
             patch("smtp_client.send_email_from_account",
                   return_value=(False, "535 5.7.139 SmtpClientAuthentication is disabled")):
            ok, err, method = mail_send.try_send_for_account(
                self._acct(oauth_graph_access_token=None, oauth_graph_refresh_token=None,
                           oauth_graph_identity=None),
                to_addr="x@example.invalid", subject="s",
                refresh_graph_token=lambda aid: (None, None))
        self.assertFalse(ok)
        g.assert_not_called()


class TestTokens(unittest.TestCase):
    def test_persist_failure_sets_auth_error(self):
        dbx = FakeDB(one={"auth_error": None, "id": 1}, rows=[{"id": 1}],
                     exec_result=1)
        dbx.execute = lambda sql, params=None: (
            dbx.writes.append((sql, params)) or (1 if "notifications" in sql else 1))
        with patch.object(mail_oauth, "db", dbx):
            mail_oauth.record_auth_error(5, "refresh failed")
        self.assertTrue(any("auth_error = %s" in sql for sql, _ in dbx.writes))

    def test_concurrent_refresh_keeps_one_token(self):
        dbx = FakeDB(one={"email": "a@example.invalid", "oauth_identity": None})
        dbx.execute = lambda sql, params=None: (
            dbx.writes.append((sql, params)) or (1 if "AND oauth_access_token" not in sql else 0))
        prov = type("P", (), {"reports_identity_on_refresh": True,
                              "refresh_token": staticmethod(
                                  lambda rt: {"access_token": "new", "refresh_token": "nr",
                                              "expires_in": 3600, "email": "a@example.invalid"})})()
        with patch.object(mail_oauth, "db", dbx), \
             patch("oauth_providers.get_provider", return_value=prov):
            out = mail_oauth.refresh_access_token(5, "microsoft", "old", None,
                                                  force=True, expected_access="old")
        self.assertIsNone(out, "a lost compare-and-swap must not report success")


class TestImapHelpers(unittest.TestCase):
    def test_message_id_reconciliation_skips_insert(self):
        import scanner.mail_scanner as ms
        dbx = FakeDB(one=lambda sql, p: (
            {"id": 77} if "message_id" in sql and "SELECT id" in sql else None))
        with patch.object(ms, "db", dbx):
            out = ms._store_message(
                {"uid": "9", "message_id": "abc@example.invalid", "folder": "INBOX",
                 "subject": "s", "from": "a@example.invalid", "to": "b@example.invalid"},
                {"account_id": 3, "inbox": "INBOX"})
        self.assertEqual(out, 77)
        self.assertFalse(any("INSERT INTO mail_messages" in sql for sql, _ in dbx.writes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
