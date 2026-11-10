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
    def test_mapped_account_allowed(self):
        dbx = FakeDB(one=lambda sql, p: (
            {"contact_id": 55} if "opportunities" in sql else {"id": 1}),
            rows=[{"id": 1, "email": "a@example.invalid"},
                  {"id": 2, "email": "b@example.invalid"}])
        out = guard.evaluate(dbx, account={"id": 1, "email": "a@example.invalid"}, deal_id=9)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["contact_id"], 55)

    def test_other_account_blocked(self):
        dbx = FakeDB(one=lambda sql, p: (
            {"contact_id": 55} if "opportunities" in sql else {"id": 1}),
            rows=[{"id": 1, "email": "a@example.invalid"}])
        out = guard.evaluate(dbx, account={"id": 2, "email": "b@example.invalid"}, deal_id=9)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], guard.REASON_MAPPED_MISMATCH)

    def test_no_mapping_allows_manual_pick(self):
        dbx = FakeDB(one=lambda sql, p: {"contact_id": 55}, rows=[])
        out = guard.evaluate(dbx, account={"id": 3, "email": "c@example.invalid"}, deal_id=9)
        self.assertTrue(out["allowed"])

    def test_reply_from_wrong_account_blocked(self):
        dbx = FakeDB(one=lambda sql, p: (
            {"account_id": 1} if "mail_messages" in sql else {"contact_id": None}),
            rows=[])
        out = guard.evaluate(dbx, account={"id": 2, "email": "b@example.invalid"},
                             reply_to_message_id=44)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], guard.REASON_ORIGINAL_MISMATCH)

    def test_thread_conflict_blocked(self):
        # thread received on account 2, job mapped to account 1
        def one(sql, p):
            if "mail_deal_links" in sql:
                return {"opportunity_id": 9}
            if "mail_messages" in sql:
                return {"account_id": 2}
            if "opportunities" in sql:
                return {"contact_id": 55}
            return {"id": 1}
        dbx = FakeDB(one=one, rows=[{"id": 1, "email": "a@example.invalid"}])
        out = guard.evaluate(dbx, account={"id": 1, "email": "a@example.invalid"},
                             reply_to_message_id=44)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], guard.REASON_THREAD_CONFLICT)

    def test_refusal_is_logged(self):
        dbx = FakeDB()
        guard.log_refusal(dbx, user_id=7, account_id=2, deal_id=9, reason="mapped_mismatch")
        self.assertIn("mail_send_refusals", dbx.writes[0][0])


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
        if db.query_one("SELECT 1 AS ok FROM information_schema.tables "
                        "WHERE table_name='mail_send_refusals'") is None:
            raise unittest.SkipTest("migration 050 not applied")
        cls.user = db.query_one(
            """INSERT INTO users (email, display_name, password_hash, password_salt,
                                  is_admin, is_active, role)
               VALUES (%s, 'Guard Test', 'x', 'x', TRUE, TRUE, 'admin') RETURNING id""",
            (f"guard-{S}@example.invalid",))
        cls.contact = db.query_one(
            "INSERT INTO contacts (company, is_company) VALUES (%s, TRUE) RETURNING id",
            (f"Guard Contractor {S}",))
        cls.other_contact = db.query_one(
            "INSERT INTO contacts (company, is_company) VALUES (%s, TRUE) RETURNING id",
            (f"Other Contractor {S}",))
        cls.acct = db.query_one(
            """INSERT INTO mail_accounts (email, display_name, smtp_from_name, imap_host,
                   password_encrypted, owner_user_id, contractor_contact_id)
               VALUES (%s, 'Mapped', 'Mapped Brand', 'imap.example.invalid', 'x', %s, %s)
               RETURNING id""",
            (f"mapped-{S}@example.invalid", cls.user["id"], cls.contact["id"]))
        cls.other_acct = db.query_one(
            """INSERT INTO mail_accounts (email, display_name, imap_host,
                   password_encrypted, owner_user_id, contractor_contact_id)
               VALUES (%s, 'Other', 'imap.example.invalid', 'x', %s, %s) RETURNING id""",
            (f"other-{S}@example.invalid", cls.user["id"], cls.other_contact["id"]))
        cls.deal = db.query_one(
            """INSERT INTO opportunities (title, contact_id, responsible_user_id, created_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (f"Guard job {S}", cls.contact["id"], cls.user["id"], cls.user["id"]))
        cls.session = auth.create_session(cls.user["id"], ip_address="test")

    @classmethod
    def tearDownClass(cls):
        try:
            db.execute("DELETE FROM mail_outgoing WHERE account_id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM mail_messages WHERE account_id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM mail_send_refusals WHERE account_id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM api_tokens WHERE created_by = %s", (cls.user["id"],))
            db.execute("DELETE FROM sessions WHERE user_id = %s", (cls.user["id"],))
            db.execute("DELETE FROM opportunities WHERE id = %s", (cls.deal["id"],))
            db.execute("DELETE FROM mail_accounts WHERE id IN (%s, %s)",
                       (cls.acct["id"], cls.other_acct["id"]))
            db.execute("DELETE FROM contacts WHERE id IN (%s, %s)",
                       (cls.contact["id"], cls.other_contact["id"]))
            db.execute("DELETE FROM users WHERE id = %s", (cls.user["id"],))
        except Exception as exc:  # pragma: no cover
            print(f"cleanup warning: {exc}")

    def _send(self, account_id, **extra):
        body = {"account_id": account_id, "to": "someone@example.invalid",
                "subject": "s", "body": "<p>b</p>", "deal_id": self.deal["id"]}
        body.update(extra)
        return mcp_local_api.call_session(self.session, "POST", "/api/v2/mail/send", body)

    def test_mismatch_returns_409_and_writes_nothing(self):
        before = db.query_one("SELECT count(*) AS n FROM mail_outgoing")
        resp = self._send(self.other_acct["id"])
        self.assertEqual(resp.status, 409, resp.text)
        self.assertIn("mapped", resp.text.lower())
        after = db.query_one("SELECT count(*) AS n FROM mail_outgoing")
        self.assertEqual(before["n"], after["n"])
        refusal = db.query_one(
            "SELECT reason FROM mail_send_refusals WHERE account_id = %s ORDER BY id DESC LIMIT 1",
            (self.other_acct["id"],))
        self.assertEqual(refusal["reason"], guard.REASON_MAPPED_MISMATCH)

    def test_mapped_account_sends_and_records_sent_as(self):
        with patch.object(server.KanbanHandler, "_try_send_for_account",
                          return_value=(True, None, "smtp_password")), \
             patch.object(server.KanbanHandler, "_create_email_history_event",
                          return_value=True):
            resp = self._send(self.acct["id"])
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

    def test_preflight_blocks_and_allows(self):
        bad = mcp_local_api.call_session(
            self.session, "POST", "/api/v2/mail/send/preflight",
            {"account_id": self.other_acct["id"], "deal_id": self.deal["id"]})
        self.assertEqual(bad.status, 409)
        good = mcp_local_api.call_session(
            self.session, "POST", "/api/v2/mail/send/preflight",
            {"account_id": self.acct["id"], "deal_id": self.deal["id"]})
        self.assertEqual(good.status, 200, good.text)
        self.assertEqual(good.payload["contactId"], self.contact["id"])

    def test_accounts_list_exposes_mapping(self):
        resp = mcp_local_api.call_session(self.session, "GET", "/api/v2/mail/accounts")
        self.assertEqual(resp.status, 200)
        row = next(a for a in resp.payload["accounts"] if a["id"] == self.acct["id"])
        self.assertEqual(row["contractorContactId"], self.contact["id"])
        self.assertTrue(row["contractorContactName"])

    def test_admin_can_set_and_clear_mapping(self):
        resp = mcp_local_api.call_session(
            self.session, "PUT", f"/api/v2/mail/accounts/{self.acct['id']}",
            {"contractor_contact_id": None})
        self.assertEqual(resp.status, 200, resp.text)
        row = db.query_one("SELECT contractor_contact_id FROM mail_accounts WHERE id = %s",
                           (self.acct["id"],))
        self.assertIsNone(row["contractor_contact_id"])
        db.execute("UPDATE mail_accounts SET contractor_contact_id = %s WHERE id = %s",
                   (self.contact["id"], self.acct["id"]))


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

    def test_auto_link_scoping(self):
        import scanner.mail_scanner as ms
        dbx = FakeDB(one={"contact_id": 55})
        with patch.object(ms, "db", dbx):
            self.assertTrue(ms._contractor_allows_link({}, 9))
            self.assertFalse(ms._contractor_allows_link({"contractor_contact_id": 66}, 9))
            self.assertTrue(ms._contractor_allows_link({"contractor_contact_id": 55}, 9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
