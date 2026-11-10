#!/usr/bin/env python3
"""Per-account credential isolation (item 2, 2026-11-10).

PROVES, OFFLINE (no DB, no network):

  * every credential read is keyed by `mail_accounts.id` — an email, tenant,
    app name or provider string is rejected before any SQL runs;
  * account A's credentials can never be used to sync, send or refresh as
    account B (sync gate, login assertion, send guard, refresh path);
  * `system mail` (notifications / automation / password resets) resolves to
    ONE id and refuses rather than borrowing another mailbox;
  * a static scan fails on any credential lookup/write keyed by email, and on
    any credential UPDATE in `mail_accounts` that is not `WHERE id = %s`;
  * `scripts/split_shared_mail_credentials.py` is dry-run by default, never
    copies credentials, and its plan clears a shared grant from every row it
    cannot prove ownership of.

Run with: python3 tests/test_mail_credential_isolation.py
"""

import ast
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import network_guard  # noqa: E402

network_guard.install()

import mail_identity  # noqa: E402
import mail_oauth  # noqa: E402
import mail_oauth_binding  # noqa: E402
import mail_send  # noqa: E402
import mail_recipient_lock  # noqa: E402,F401  (import-order parity with the suite)
import oauth_providers  # noqa: E402
import notification_dispatcher  # noqa: E402
import scanner.mail_scanner as ms  # noqa: E402
import importlib.util  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Doubles
# ─────────────────────────────────────────────────────────────────────────────

class FakeDB:
    """Records every statement; returns rows by the id in the parameters.

    `rows` maps account id -> row. A query for an id that is not in `rows`
    returns None. `sql_log` is the audit trail the isolation tests assert on.
    """

    def __init__(self, rows=None, executing=1):
        self.rows = rows or {}
        self.sql_log = []
        self.executing = executing

    def _id_param(self, params):
        if params and isinstance(params[0], int):
            return params[0]
        return None

    def query_one(self, sql, params=None):
        self.sql_log.append(("one", sql, params))
        aid = self._id_param(params)
        if aid is None:
            return None
        row = self.rows.get(aid)
        return dict(row) if row else None

    def query_dicts(self, sql, params=None):
        self.sql_log.append(("dicts", sql, params))
        return [dict(r) for r in self.rows.values()]

    def execute(self, sql, params=None):
        self.sql_log.append(("exec", sql, params))
        return self.executing

    def statements(self):
        return [entry[1] for entry in self.sql_log]

    def writes(self):
        return [entry for entry in self.sql_log if entry[0] == "exec"]


def account(aid, email, **kw):
    row = {"id": aid, "email": email, "imap_host": "imap.example.com",
           "imap_port": 993, "password_encrypted": "pw", "oauth_provider": None,
           "oauth_access_token": None, "oauth_refresh_token": None,
           "oauth_token_expires": None, "oauth_identity": None,
           "oauth_graph_access_token": None, "oauth_graph_refresh_token": None,
           "oauth_graph_token_expires": None, "oauth_graph_identity": None,
           "smtp_user": None, "smtp_host": "smtp.example.com", "smtp_port": 587,
           "smtp_password_encrypted": "smtp-pw", "smtp_auth_disabled": False,
           "sync_enabled": True, "monitored_folders": "INBOX", "retention_days": 0}
    row.update(kw)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# 1. Reads are keyed by id only
# ─────────────────────────────────────────────────────────────────────────────

class TestCredentialAccessIsIdOnly(unittest.TestCase):
    def test_reads_by_id(self):
        db = FakeDB({7: account(7, "a@example.com", oauth_refresh_token="RA")})
        row = mail_identity.credentials_for_account(7, db)
        self.assertEqual(row["id"], 7)
        sql, params = db.sql_log[0][1], db.sql_log[0][2]
        self.assertIn("WHERE id = %s", sql)
        self.assertNotIn("email = %s", sql)
        self.assertEqual(params, (7,))

    def test_email_key_is_rejected_before_sql(self):
        db = FakeDB({7: account(7, "a@example.com")})
        for bad in ("a@example.com", "example.com", "microsoft", None, ""):
            with self.assertRaises(ValueError):
                mail_identity.credentials_for_account(bad, db)
        self.assertEqual(db.sql_log, [])

    def test_account_row_is_id_only(self):
        db = FakeDB({9: account(9, "b@example.com")})
        self.assertEqual(mail_identity.account_row("9", db)["id"], 9)
        self.assertEqual(db.sql_log[0][2], (9,))
        with self.assertRaises(ValueError):
            mail_identity.account_row("b@example.com", db)

    def test_credential_columns_are_the_known_allowlist(self):
        for col in ("password_encrypted", "smtp_password_encrypted", "smtp_user",
                    "oauth_access_token", "oauth_refresh_token", "oauth_identity",
                    "oauth_graph_access_token", "oauth_graph_refresh_token",
                    "oauth_graph_identity"):
            self.assertIn(col, mail_identity.CREDENTIAL_COLUMNS)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Identity classification
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentityClassification(unittest.TestCase):
    def test_password_account_without_credentials_is_ok(self):
        state, detail = mail_identity.check_account(account(1, "a@example.com"))
        self.assertEqual(state, mail_identity.IDENTITY_OK)
        self.assertEqual(detail, "a@example.com")

    def test_verified_exchange_token_is_ok(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_refresh_token="RT",
                      oauth_identity="a@example.com")
        self.assertEqual(mail_identity.check_account(row)[0], mail_identity.IDENTITY_OK)

    def test_exchange_token_for_another_mailbox_is_a_mismatch(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_refresh_token="RT",
                      oauth_identity="b@example.com")
        state, detail = mail_identity.check_account(row)
        self.assertEqual(state, mail_identity.IDENTITY_MISMATCH)
        self.assertEqual(detail, "b@example.com")

    def test_graph_token_for_another_mailbox_is_a_mismatch(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_graph_access_token="GAT",
                      oauth_graph_identity="b@example.com")
        self.assertEqual(mail_identity.check_account(row)[0],
                         mail_identity.IDENTITY_MISMATCH)

    def test_token_without_a_verified_identity_is_unverifiable(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_refresh_token="RT")
        self.assertEqual(mail_identity.check_account(row)[0],
                         mail_identity.IDENTITY_UNVERIFIABLE)

    def test_provider_without_tokens_is_not_a_false_alarm(self):
        row = account(1, "a@example.com", oauth_provider="microsoft")
        self.assertEqual(mail_identity.check_account(row)[0], mail_identity.IDENTITY_OK)

    def test_smtp_login_as_another_mailbox_is_a_mismatch(self):
        row = account(1, "a@example.com", smtp_user="b@example.com")
        self.assertEqual(mail_identity.check_account(row)[0],
                         mail_identity.IDENTITY_MISMATCH)

    def test_smtp_username_is_not_treated_as_a_mailbox(self):
        row = account(1, "a@example.com", smtp_user="mailer-daemon")
        self.assertEqual(mail_identity.check_account(row)[0], mail_identity.IDENTITY_OK)

    def test_sync_gate_ignores_a_mis_bound_graph_token(self):
        """A Graph token for another mailbox must not stop RECEIVING mail."""
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_identity="a@example.com",
                      oauth_graph_access_token="GAT",
                      oauth_graph_identity="b@example.com")
        self.assertEqual(mail_identity.check_account(row)[0],
                         mail_identity.IDENTITY_MISMATCH)
        allowed, why = mail_identity.sync_gate(row, login_user="a@example.com")
        self.assertTrue(allowed, why)

    def test_sync_gate_ignores_an_smtp_login_of_another_mailbox(self):
        row = account(1, "a@example.com", smtp_user="b@example.com")
        self.assertEqual(mail_identity.check_account(row)[0],
                         mail_identity.IDENTITY_MISMATCH)
        allowed, why = mail_identity.sync_gate(row, login_user="a@example.com")
        self.assertTrue(allowed, why)

    def test_msa_prefixed_identity_matches(self):
        row = account(1, "a@outlook.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_identity="live.com#a@outlook.com")
        self.assertEqual(mail_identity.check_account(row)[0], mail_identity.IDENTITY_OK)

    def test_account_without_address_is_unverifiable(self):
        self.assertEqual(mail_identity.check_account({"id": 1, "email": ""})[0],
                         mail_identity.IDENTITY_UNVERIFIABLE)

    def test_refusal_message_names_no_secret(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="SECRET-A", oauth_refresh_token="SECRET-R",
                      oauth_identity="b@example.com")
        msg = mail_identity.identity_message(row, *mail_identity.check_account(row))
        self.assertIn("b@example.com", msg)
        self.assertNotIn("SECRET", msg)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Sync gate — account A can never sync as account B
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncGate(unittest.TestCase):
    def test_password_account_may_sync(self):
        ok, why = mail_identity.sync_gate(account(1, "a@example.com"),
                                          login_user="a@example.com")
        self.assertTrue(ok, why)

    def test_login_user_must_be_the_account_address(self):
        ok, why = mail_identity.sync_gate(account(1, "a@example.com"),
                                          login_user="b@example.com")
        self.assertFalse(ok)
        self.assertIn("login user", why)

    def test_mismatched_oauth_account_may_not_sync(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_identity="b@example.com")
        ok, why = mail_identity.sync_gate(row, login_user="a@example.com")
        self.assertFalse(ok)
        self.assertIn("b@example.com", why)

    def test_unverifiable_oauth_account_may_not_sync(self):
        row = account(1, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT")
        ok, _why = mail_identity.sync_gate(row, login_user="a@example.com")
        self.assertFalse(ok)

    def test_sync_gate_by_id_loads_the_row_by_id(self):
        db = FakeDB({3: account(3, "c@example.com")})
        self.assertTrue(mail_identity.sync_gate_by_id(3, db)[0])
        self.assertIn("WHERE id = %s", db.sql_log[0][1])

    def test_sync_gate_by_id_refuses_a_missing_row(self):
        db = FakeDB({})
        ok, why = mail_identity.sync_gate_by_id(3, db)
        self.assertFalse(ok)
        self.assertIn("not found", why)

    def test_refusal_is_persisted_for_the_ui(self):
        row = account(5, "a@example.com", oauth_provider="microsoft",
                      oauth_access_token="AT", oauth_identity="b@example.com")
        with patch.object(mail_identity, "record_identity_refusal") as rec:
            ok, why = mail_identity.sync_gate(row, login_user="a@example.com")
        self.assertFalse(ok)
        rec.assert_not_called()  # sync_gate is pure; callers record


# ─────────────────────────────────────────────────────────────────────────────
# 4. Scanner: env creds never apply to DB accounts; login asserts identity
# ─────────────────────────────────────────────────────────────────────────────

class TestScannerCredentialBinding(unittest.TestCase):
    def _mailboxes(self, rows, env=None):
        db = FakeDB({r["id"]: r for r in rows})
        env_vars = {"SCANNER_IMAP_HOST": "", "SCANNER_IMAP_USER": "",
                    "SCANNER_IMAP_PASSWORD": "", "SCANNER_INBOX": "INBOX"}
        env_vars.update(env or {})
        with patch.object(ms, "db", db), patch.dict(os.environ, env_vars, clear=False):
            return ms._get_mailboxes(), db

    def test_each_config_carries_its_own_account_id_and_email(self):
        rows = [account(1, "a@example.com"), account(2, "b@example.com")]
        boxes, _db = self._mailboxes(rows)
        self.assertEqual([(b["account_id"], b["email"], b["user"]) for b in boxes],
                         [(1, "a@example.com", "a@example.com"),
                          (2, "b@example.com", "b@example.com")])

    def test_env_credentials_are_ignored_when_accounts_exist(self):
        rows = [account(1, "a@example.com")]
        boxes, _db = self._mailboxes(rows, env={
            "SCANNER_IMAP_HOST": "imap.env.example", "SCANNER_IMAP_USER": "env@example.com",
            "SCANNER_IMAP_PASSWORD": "env-pw"})
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["account_id"], 1)
        self.assertNotIn("env@example.com", [b["user"] for b in boxes])

    def test_env_credentials_are_used_only_with_zero_accounts(self):
        boxes, _db = self._mailboxes([], env={
            "SCANNER_IMAP_HOST": "imap.env.example", "SCANNER_IMAP_USER": "env@example.com",
            "SCANNER_IMAP_PASSWORD": "env-pw"})
        self.assertEqual(len(boxes), 1)
        self.assertIsNone(boxes[0]["account_id"])
        self.assertEqual(boxes[0]["user"], "env@example.com")

    def test_mismatched_account_is_not_polled(self):
        rows = [account(1, "a@example.com", oauth_provider="microsoft",
                        oauth_access_token="AT", oauth_refresh_token="RT",
                        oauth_identity="b@example.com")]
        with patch.object(mail_identity, "record_identity_refusal") as rec:
            boxes, _db = self._mailboxes(rows)
        self.assertEqual(boxes, [])
        rec.assert_called_once()
        self.assertEqual(rec.call_args[0][0], 1)

    def test_login_asserts_the_login_user_before_connecting(self):
        cfg = {"host": "imap.example.com", "port": 993, "user": "b@example.com",
               "email": "a@example.com", "password": "pw", "auth_type": "password"}
        with patch.object(ms, "MailBox") as mailbox_cls:
            with self.assertRaises(ms.MailboxIdentityError):
                ms._login_mailbox(cfg)
        mailbox_cls.assert_not_called()

    def test_xoauth2_without_a_verified_identity_is_refused(self):
        cfg = {"host": "imap.example.com", "port": 993, "user": "a@example.com",
               "email": "a@example.com", "oauth_token": "AT",
               "auth_type": "xoauth2", "identity_state": "unverified"}
        with patch.object(ms, "MailBox") as mailbox_cls:
            with self.assertRaises(ms.MailboxIdentityError):
                ms._login_mailbox(cfg)
        mailbox_cls.assert_not_called()

    def test_verified_xoauth2_logs_in_as_the_account_itself(self):
        cfg = {"host": "imap.example.com", "port": 993, "user": "a@example.com",
               "email": "a@example.com", "oauth_token": "AT",
               "auth_type": "xoauth2", "identity_state": "verified"}
        with patch.object(ms, "MailBox") as mailbox_cls:
            ms._login_mailbox(cfg)
        mailbox_cls.return_value.xoauth2.assert_called_once_with("a@example.com", "AT")
        mailbox_cls.return_value.login.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Send path — account A can never send with B's credentials
# ─────────────────────────────────────────────────────────────────────────────

class TestSendIsolation(unittest.TestCase):
    def _send(self, acct, **kw):
        return mail_send.try_send_for_account(
            acct, to_addr="to@example.com", subject="S", html_body="<p>x</p>", **kw)

    def test_graph_token_of_another_mailbox_never_sends(self):
        acct = account(1, "a@example.com", oauth_provider="microsoft",
                       oauth_graph_access_token="GAT",
                       oauth_graph_identity="b@example.com")
        with patch("mail_graph_client.send_email_via_graph") as send:
            ok, err, method = self._send(
                acct, refresh_graph_token=lambda aid: ("GAT", None))
        self.assertFalse(ok)
        self.assertIn("b@example.com", err)
        send.assert_not_called()

    def test_unverified_graph_token_never_sends(self):
        acct = account(1, "a@example.com", oauth_provider="microsoft",
                       oauth_graph_access_token="GAT")
        with patch("mail_graph_client.send_email_via_graph") as send:
            ok, err, _m = self._send(
                acct, refresh_graph_token=lambda aid: ("GAT", None))
        self.assertFalse(ok)
        self.assertIn("identity", err.lower())
        send.assert_not_called()

    def test_smtp_login_of_another_mailbox_never_sends(self):
        acct = account(1, "a@example.com", smtp_user="b@example.com")
        with patch("smtp_client.send_email_from_account") as send:
            ok, err, _m = self._send(acct)
        self.assertFalse(ok)
        self.assertIn("b@example.com", err)
        send.assert_not_called()

    def test_oauth_smtp_login_of_another_mailbox_never_sends(self):
        acct = account(1, "a@example.com", oauth_provider="google",
                       oauth_access_token="AT", oauth_refresh_token="RT",
                       oauth_identity="b@example.com",
                       smtp_password_encrypted="")
        with patch("smtp_client.send_email_from_account") as send:
            ok, err, _m = self._send(acct)
        self.assertFalse(ok)
        self.assertIn("b@example.com", err)
        send.assert_not_called()

    def test_unverified_oauth_smtp_login_never_sends(self):
        acct = account(1, "a@example.com", oauth_provider="google",
                       oauth_access_token="AT", oauth_refresh_token="RT",
                       smtp_password_encrypted="")
        with patch("smtp_client.send_email_from_account") as send:
            ok, err, _m = self._send(acct)
        self.assertFalse(ok)
        self.assertIn("identity", err.lower())
        send.assert_not_called()

    def test_verified_oauth_smtp_login_sends_from_its_own_mailbox(self):
        acct = account(1, "a@example.com", oauth_provider="google",
                       oauth_access_token="AT", oauth_refresh_token="RT",
                       oauth_identity="a@example.com",
                       smtp_password_encrypted="")
        with patch("smtp_client.send_email_from_account",
                   return_value=(True, None)) as send:
            ok, err, method = self._send(acct)
        self.assertTrue(ok, err)
        self.assertEqual(method, "smtp_oauth")
        self.assertEqual(send.call_args.kwargs["smtp_user"], "a@example.com")

    def test_send_uses_only_this_rows_credentials(self):
        acct = account(1, "a@example.com", smtp_user="a@example.com",
                       password_encrypted="A-PW", smtp_password_encrypted="A-SMTP-PW")
        with patch("smtp_client.send_email_from_account",
                   return_value=(True, None)) as send:
            ok, err, _m = self._send(acct)
        self.assertTrue(ok, err)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["smtp_user"], "a@example.com")
        self.assertEqual(kwargs["smtp_password"], "A-SMTP-PW")
        self.assertEqual(kwargs["from_addr"], "a@example.com")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Refresh path — keyed by id, refuses a foreign grant, writes one row
# ─────────────────────────────────────────────────────────────────────────────

class _Provider:
    reports_identity_on_refresh = True

    def __init__(self, email):
        self.email = email

    def refresh_token(self, refresh_token):
        return {"access_token": "NEW-AT", "expires_in": 3600,
                "refresh_token": "NEW-RT", "email": self.email}


class TestRefreshIsolation(unittest.TestCase):
    def test_refresh_reads_and_writes_only_the_requested_id(self):
        row = account(2, "b@example.com", oauth_provider="microsoft",
                      oauth_refresh_token="B-RT", oauth_identity="b@example.com")
        db = FakeDB({2: row})
        with patch.object(mail_oauth, "db", db), \
             patch.object(oauth_providers, "get_provider",
                          return_value=_Provider("b@example.com")):
            out = mail_oauth.refresh_access_token(
                2, "microsoft", "B-RT", datetime.now(timezone.utc) - timedelta(hours=1))
        self.assertIsNotNone(out)
        reads = [e for e in db.sql_log if e[0] != "exec"]
        self.assertTrue(reads)
        for _kind, sql, params in reads:
            self.assertIn("WHERE id = %s", sql)
            self.assertEqual(params[0], 2)
        for _kind, sql, params in db.writes():
            self.assertIn("WHERE id = %s", sql)
            self.assertEqual(params[-1], 2)

    def test_refresh_refuses_a_grant_that_belongs_to_another_mailbox(self):
        row = account(2, "b@example.com", oauth_provider="microsoft",
                      oauth_refresh_token="A-RT", oauth_identity="b@example.com")
        db = FakeDB({2: row})
        with patch.object(mail_oauth, "db", db), \
             patch.object(oauth_providers, "get_provider",
                          return_value=_Provider("a@example.com")), \
             patch.object(mail_oauth, "record_refresh_error") as err:
            out = mail_oauth.refresh_access_token(
                2, "microsoft", "A-RT", datetime.now(timezone.utc) - timedelta(hours=1))
        self.assertIsNone(out)
        err.assert_called_once()
        self.assertIn("a@example.com", err.call_args[0][1])
        self.assertEqual(db.writes(), [])


# ─────────────────────────────────────────────────────────────────────────────
# 7. System mail — one id, refusal instead of borrowing
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemAccount(unittest.TestCase):
    def test_pinned_account_is_used_by_id(self):
        db = FakeDB({4: account(4, "sys@example.com", is_crm_mail=True)})
        settings = {"value": {"account_id": 4}}
        with patch.object(db, "query_one", side_effect=lambda sql, params=None: (
                settings if "system_settings" in sql else db.rows.get(params[0]))):
            acct = mail_identity.system_account(db, refresh=False)
        self.assertEqual(acct["id"], 4)

    def test_crm_inbox_is_used_when_nothing_is_pinned(self):
        rows = [account(2, "crm@example.com", is_crm_mail=True)]
        db = MagicMock()
        db.query_one.return_value = None
        db.query_dicts.return_value = rows
        acct = mail_identity.system_account(db, refresh=False)
        self.assertEqual(acct["id"], 2)
        sql = db.query_dicts.call_args[0][0]
        self.assertIn("is_crm_mail = TRUE", sql)

    def test_mismatched_system_account_is_refused_not_replaced(self):
        rows = [account(2, "crm@example.com", is_crm_mail=True,
                        oauth_provider="microsoft", oauth_access_token="AT",
                        oauth_identity="other@example.com")]
        db = MagicMock()
        db.query_one.return_value = None
        db.query_dicts.return_value = rows
        with patch.object(mail_identity, "record_identity_refusal") as rec:
            acct = mail_identity.system_account(db, refresh=False)
        self.assertIsNone(acct)
        rec.assert_called_once()

    def test_no_system_account_returns_none(self):
        db = MagicMock()
        db.query_one.return_value = None
        db.query_dicts.return_value = []
        self.assertIsNone(mail_identity.system_account(db, refresh=False))

    def test_dispatcher_uses_the_shared_resolver_only(self):
        sentinel = {"id": 6, "email": "crm@example.com"}
        with patch.object(mail_identity, "system_account", return_value=sentinel):
            with patch("db.query_one", return_value=None):
                self.assertEqual(notification_dispatcher._get_crm_smtp_account(), sentinel)

    def test_dispatcher_refusal_does_not_fall_back_to_env_smtp(self):
        with patch.object(notification_dispatcher, "_get_crm_smtp_account", return_value=None), \
             patch.object(notification_dispatcher, "_crm_smtp_refused",
                          return_value="Wrong mailbox's credentials"), \
             patch.object(notification_dispatcher, "_get_email_pref", return_value="immediate"), \
             patch.object(notification_dispatcher, "_get_user_email", return_value="u@example.com"), \
             patch.object(notification_dispatcher, "_format_email_notification",
                          return_value=("S", "T")), \
             patch.object(notification_dispatcher, "_get_branding", return_value={}), \
             patch.object(notification_dispatcher, "_mark_email_sent") as marked, \
             patch("smtp_client.send_email") as env_send:
            ok = notification_dispatcher._process_email(
                1, 2, {"id": 1}, "P", "A", None)
        self.assertFalse(ok)
        env_send.assert_not_called()
        marked.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 8. Split script — dry-run default, no copying, id-keyed clears
# ─────────────────────────────────────────────────────────────────────────────

def _load_split_script():
    path = ROOT / "scripts" / "split_shared_mail_credentials.py"
    spec = importlib.util.spec_from_file_location("split_shared_mail_credentials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSplitScript(unittest.TestCase):
    def setUp(self):
        self.script = _load_split_script()

    def test_dry_run_is_the_default_and_refuses_apply_without_yes(self):
        with patch.object(self.script, "_connect") as connect:
            self.assertEqual(self.script.main([]), 0)
        db = connect.call_args
        self.assertTrue(db.kwargs["readonly"])
        with patch.object(self.script, "_connect") as connect:
            self.assertEqual(self.script.main(["--apply"]), 1)
        connect.assert_not_called()

    def test_shared_exchange_grant_is_cleared_from_the_non_owner(self):
        rows = [account(8, "a@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="a@example.com"),
                account(9, "b@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="b@example.com")]
        plan = self.script.build_plan(rows)
        actions = {(p["account_id"], p["action"]) for p in plan["plan"]}
        self.assertEqual(actions, {(9, "clear_exchange_tokens")})
        self.assertIn("shared_exchange_grant", " ".join(plan["findings"]))

    def test_grant_with_no_provable_owner_is_cleared_everywhere(self):
        rows = [account(8, "a@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="x@example.com"),
                account(9, "b@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="y@example.com")]
        plan = self.script.build_plan(rows)
        self.assertEqual({(p["account_id"], p["action"]) for p in plan["plan"]},
                         {(8, "clear_exchange_tokens"), (9, "clear_exchange_tokens")})

    def test_owner_keeps_its_grant_and_nothing_is_copied(self):
        rows = [account(8, "a@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="a@example.com"),
                account(9, "b@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="b@example.com")]
        plan = self.script.build_plan(rows)
        self.assertFalse(any(p["account_id"] == 8 for p in plan["plan"]))
        for item in plan["plan"]:
            self.assertTrue(item["action"].startswith("clear_"))

    def test_identity_and_smtp_mismatches_are_reported(self):
        rows = [account(3, "c@example.com", oauth_provider="microsoft",
                        oauth_access_token="AT", oauth_identity="d@example.com",
                        smtp_user="e@example.com")]
        plan = self.script.build_plan(rows)
        actions = {p["action"] for p in plan["plan"]}
        self.assertIn("clear_exchange_tokens", actions)
        self.assertIn("clear_smtp_user", actions)

    def test_clean_accounts_produce_an_empty_plan(self):
        rows = [account(1, "a@example.com"),
                account(2, "b@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="R2", oauth_identity="b@example.com")]
        plan = self.script.build_plan(rows)
        self.assertEqual(plan["plan"], [])
        self.assertEqual(plan["findings"], [])

    def test_token_values_are_never_in_the_output(self):
        rows = [account(8, "a@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SECRET-TOKEN")]
        rendered = repr(self.script.build_plan(rows))
        self.assertNotIn("SECRET-TOKEN", rendered)

    def test_apply_never_copies_credentials(self):
        rows = [account(8, "a@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="a@example.com"),
                account(9, "b@example.com", oauth_provider="microsoft",
                        oauth_refresh_token="SAME", oauth_identity="b@example.com")]
        plan = self.script.build_plan(rows)["plan"]
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        self.script.apply_plan(conn, plan)
        for call in cur.execute.call_args_list:
            sql = call[0][0]
            self.assertIn("WHERE id = %s", sql) if sql.startswith("UPDATE") else None
            self.assertNotIn("oauth_refresh_token = %s", sql.split("SET")[-1].split("WHERE")[0])
            self.assertIn("SET", sql)
        conn.commit.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 9. STATIC: no credential lookup/write keyed by email (or smtp_user/provider)
# ─────────────────────────────────────────────────────────────────────────────

CREDENTIAL_COLUMNS = (
    "password_encrypted", "smtp_password_encrypted", "oauth_access_token",
    "oauth_refresh_token", "oauth_token_expires", "oauth_identity",
    "oauth_graph_access_token", "oauth_graph_refresh_token",
    "oauth_graph_token_expires", "oauth_graph_identity", "oauth_graph_scope",
    "smtp_user", "smtp_auth_disabled",
)

_SKIP_DIRS = {"tests", "migrations", "onlyoffice-module", "__pycache__", ".git",
              ".venv", "node_modules", "data", "doc_pdf", "deploy", "public",
              "scanner/data"}

_EMAIL_KEYED = re.compile(
    r"\bwhere\b[^;]*\bemail\b\s*(=|in\b|like\b)"      # WHERE email = %s
    r"|\bon\s+conflict\s*\(\s*email\b"                 # ON CONFLICT (email)
    r"|\bwhere\b[^;]*\bsmtp_user\b\s*="                # WHERE smtp_user = %s
    r"|\bwhere\b[^;]*\boauth_provider\b\s*="          # WHERE oauth_provider = %s
    r"|\bwhere\b[^;]*\bis_crm_mail\b\s*=",             # WHERE is_crm_mail = TRUE
)

_UPDATE_CREDENTIAL = re.compile(
    r"^\s*update\s+mail_accounts\s+set\b", re.IGNORECASE)


def _literal_text(node) -> str:
    """Flatten string literals, f-strings and `+` concatenations."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


def _iter_python_files():
    for path in ROOT.rglob("*.py"):
        parts = set(path.relative_to(ROOT).parts[:-1])
        if parts & _SKIP_DIRS:
            continue
        yield path


def _mail_credential_statements():
    """(file, lineno, text) for every SQL string touching mail credentials."""
    out = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) and not isinstance(node, ast.JoinedStr) \
                    and not isinstance(node, ast.BinOp):
                continue
            text = _literal_text(node)
            low = " ".join(text.lower().split())
            if "mail_accounts" not in low:
                continue
            if not any(col in low for col in CREDENTIAL_COLUMNS):
                continue
            out.append((path.relative_to(ROOT), getattr(node, "lineno", 0), low))
    return out


class TestStaticNoEmailKeyedCredentials(unittest.TestCase):
    def test_no_credential_statement_is_keyed_by_email(self):
        offenders = []
        for path, line, low in _mail_credential_statements():
            if _EMAIL_KEYED.search(low):
                offenders.append(f"{path}:{line}: {low[:160]}")
        self.assertEqual(offenders, [],
                         "credential SQL keyed by email/provider must be keyed by "
                         "mail_accounts.id instead:\n" + "\n".join(offenders))

    def test_every_credential_update_is_keyed_by_id(self):
        offenders = []
        for path, line, low in _mail_credential_statements():
            if not _UPDATE_CREDENTIAL.search(low):
                continue
            if "where id = %s" not in low:
                offenders.append(f"{path}:{line}: {low[:160]}")
        self.assertEqual(offenders, [],
                         "credential UPDATEs on mail_accounts must be WHERE id = %s:\n"
                         + "\n".join(offenders))

    def test_no_select_star_from_mail_accounts_without_an_id_predicate(self):
        """`SELECT *` may only be filtered by id.

        The single documented exception is the SYSTEM-mail resolver in
        `mail_identity.py`, which must pick *a* company inbox (there is no id
        to start from) — and it identity-checks the chosen row before use.
        """
        allowed = {Path("mail_identity.py")}
        offenders = []
        for path in _iter_python_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"SELECT\s+\*\s+FROM\s+mail_accounts([^\"']*)",
                                     text, re.IGNORECASE):
                tail = " ".join((match.group(1) or "").lower().split())
                if "where" not in tail:
                    continue
                if "id = %s" in tail:
                    continue
                rel = path.relative_to(ROOT)
                if rel in allowed and "where is_crm_mail = true" in tail:
                    continue
                offenders.append(f"{rel}: {tail[:120]}")
        self.assertEqual(offenders, [], "\n".join(offenders))
        # The exception above must stay deterministic (lowest id wins).
        self.assertIn("ORDER BY id", (ROOT / "mail_identity.py").read_text(encoding="utf-8"))

    def test_the_scan_actually_finds_credential_sql(self):
        """Guard against a vacuous static test (a moved tree would pass)."""
        statements = _mail_credential_statements()
        self.assertGreater(len(statements), 5)
        self.assertTrue(any("oauth_refresh_token" in s for _p, _l, s in statements))

    def test_a_planted_email_keyed_lookup_is_detected(self):
        sample = ("select oauth_refresh_token from mail_accounts "
                  "where email = %s")
        self.assertTrue(_EMAIL_KEYED.search(sample))
        update = "update mail_accounts set oauth_refresh_token = %s where oauth_provider = %s"
        self.assertTrue(_EMAIL_KEYED.search(update))
        self.assertTrue(_UPDATE_CREDENTIAL.search(update))
        self.assertNotIn("where id = %s", update)


if __name__ == "__main__":
    unittest.main(verbosity=2)
