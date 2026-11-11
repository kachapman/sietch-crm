#!/usr/bin/env python3
"""A successful reconnect/refresh must clear a stale `auth_error` (live 2026-11-10).

THE BUG
-------
Ken reconnected accounts 8/9/10 at ~20:1x ET. The OAuth callbacks wrote fresh
tokens and the verified identity, the scanner's sync gate then passed — but the
red "Mail account needs reconnecting" banner stayed up. Cause: nothing on any
success path cleared `mail_accounts.auth_error` / `auth_error_at`
(`_handle_mail_accounts` maps `auth_error` to `authError`, the banner text).

Pinned here (offline, synthetic data, network guard installed):
  1. the Exchange and Graph connect callbacks clear a stale auth_error when the
     identity is verified and the token is written;
  2. the routine CAS refresh and both Graph refreshers (server + scanner) clear
     it on a successful write;
  3. a FAILED refresh never clears it;
  4. an unchanged-address Save still keeps a real auth_error (and the identity);
  5. the scanner clears a stale auth_error only when EVERY connected credential
     family verifies — never when one is unverified or mismatched;
  6. `record_auth_error` does not re-stamp `auth_error_at` for the same message
     (the scanner calls it every poll cycle);
  7. the accounts payload reports `identityStatus=unverifiable` for
     tokens-present/identity-empty (matching the sync gate) and never selects or
     returns a token value.

Run with: python3 tests/test_reconnect_clears_auth_error.py
"""

import json
import sys
import time
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
import oauth_providers  # noqa: E402
import oauth_state  # noqa: E402
import scanner.mail_scanner as ms  # noqa: E402

try:
    import server as srv  # noqa: E402  (needs a reachable DB to import)
except Exception as exc:  # pragma: no cover - environment dependent
    srv = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

EMAIL = "estimates.masterx@outlook.com"
OTHER = "mdm.estimates@outlook.com"


class FakeDB:
    """Records every statement; returns one synthetic account row by id."""

    def __init__(self, row=None, rows=None, executing=1):
        self.rows = rows or ({row.get("id"): row} if row else {})
        self.executing = executing
        self.writes = []

    def query_one(self, sql, params=None):
        params = params or ()
        if params and isinstance(params[0], int):
            return self.rows.get(params[0])
        return None

    def query_dicts(self, sql, params=None):
        return []

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return self.executing

    def clearing_writes(self):
        return [w for w in self.writes if "auth_error = NULL" in w[0]]


def _account_row(**over):
    row = {
        "id": 8,
        "email": EMAIL,
        "owner_user_id": 7,
        "oauth_provider": "microsoft",
        "oauth_refresh_token": "OLD-RT",
        "oauth_graph_refresh_token": "OLD-GRT",
        "oauth_identity": EMAIL,
        "oauth_graph_identity": EMAIL,
        "auth_error": "Credentials for <email> could not be verified",
        "auth_error_at": datetime(2026, 11, 10, 20, 16, 57),
    }
    row.update(over)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# 1. Connect callbacks
# ─────────────────────────────────────────────────────────────────────────────

class _Handler:
    """Minimal KanbanHandler stand-in for the OAuth callback tests."""

    def __init__(self, path):
        self.path = path
        self.redirects = []

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass

    def _oauth_redirect_with_status(self, status, message):
        self.redirects.append((status, message))


class _CallbackDB:
    def __init__(self, account):
        self.account = account
        self.writes = []

    def query_one(self, sql, params=None):
        if "FROM users" in sql:
            return {"id": 7, "is_admin": False}
        if "WHERE email = %s" in sql:
            return None  # new-account duplicate probe: no existing row
        if "AND id <> %s" in sql:
            return None  # no other account holds this grant
        if "FROM mail_accounts" in sql:
            return self.account
        return None

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return 1


@unittest.skipIf(srv is None, "needs importable server (live DB)")
class TestConnectCallbacksClearAuthError(unittest.TestCase):
    def setUp(self):
        oauth_state.reset_used_nonces()
        oauth_state.reset_secret_cache()

    def test_exchange_reconnect_clears_stale_auth_error(self):
        db = _CallbackDB(_account_row())
        handler = _Handler("/x")
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        handler.path = f"/api/v2/mail/oauth/callback?code=C&state={state}"
        token = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "email": EMAIL}
        with patch.object(srv, "db", db), \
             patch.object(srv, "logger"), \
             patch.object(srv.oauth_providers.MicrosoftProvider, "exchange_code",
                          classmethod(lambda cls, code: token)):
            srv.KanbanHandler._handle_oauth_callback(handler)
        self.assertEqual(handler.redirects[0][0], "success")
        writes = [w for w in db.writes if "oauth_access_token = %s" in w[0]]
        self.assertEqual(len(writes), 1)
        self.assertIn("auth_error = NULL", writes[0][0])
        self.assertIn("auth_error_at = NULL", writes[0][0])

    def test_graph_reconnect_clears_stale_auth_error(self):
        db = _CallbackDB(_account_row())
        handler = _Handler("/x")
        state = oauth_state.sign_state(oauth_state.KIND_GRAPH, 7, "microsoft", account_id=8)
        handler.path = f"/x?code=C&state={state}"
        token = {"access_token": "GAT", "refresh_token": "GRT",
                 "expires_in": 3600, "email": EMAIL}
        with patch.object(srv, "db", db), \
             patch.object(srv, "logger"), \
             patch.object(srv.oauth_providers.MicrosoftProvider, "graph_exchange_code",
                          classmethod(lambda cls, code: token)):
            srv.KanbanHandler._handle_oauth_graph_callback(handler)
        self.assertEqual(handler.redirects[0][0], "graph_success")
        writes = [w for w in db.writes if "oauth_graph_access_token = %s" in w[0]]
        self.assertEqual(len(writes), 1)
        self.assertIn("auth_error = NULL", writes[0][0])
        self.assertIn("auth_error_at = NULL", writes[0][0])


# ─────────────────────────────────────────────────────────────────────────────
# 2 + 3. Refresh success clears, failure does not
# ─────────────────────────────────────────────────────────────────────────────

class _Provider:
    reports_identity_on_refresh = True

    def __init__(self, token=None, raises=None):
        self._token = token
        self._raises = raises

    def refresh_token(self, refresh):
        if self._raises:
            raise self._raises
        return dict(self._token or {})


class TestRefreshPaths(unittest.TestCase):
    def test_cas_refresh_write_clears_auth_error(self):
        db = FakeDB()
        with patch.object(mail_oauth, "db", db):
            saved = mail_oauth._save_refreshed_token(
                5, "NEW", "NR", time.time() + 3600, "OLD")
        self.assertTrue(saved)
        sql, params = db.writes[0]
        self.assertIn("auth_error = NULL", sql)
        self.assertIn("auth_error_at = NULL", sql)
        self.assertIn("WHERE id = %s AND oauth_access_token = %s", sql)
        self.assertEqual(params[-1], "OLD")

    def test_successful_refresh_clears_auth_error(self):
        db = FakeDB()
        provider = _Provider({"access_token": "NEW", "refresh_token": "NR",
                              "expires_in": 3600, "email": EMAIL})
        with patch.object(mail_oauth, "db", db), \
             patch.object(oauth_providers, "get_provider", return_value=provider):
            out = mail_oauth.refresh_access_token(
                5, "microsoft", "OLD-RT", None, expected_access="OLD")
        self.assertIsNotNone(out)
        self.assertTrue(db.clearing_writes(), "a successful refresh must clear auth_error")

    def test_failed_refresh_does_not_clear_auth_error(self):
        db = FakeDB()
        provider = _Provider(raises=RuntimeError("invalid_grant"))
        with patch.object(mail_oauth, "db", db), \
             patch.object(oauth_providers, "get_provider", return_value=provider), \
             patch.object(mail_oauth, "_notify_admins_auth_error"):
            out = mail_oauth.refresh_access_token(
                5, "microsoft", "OLD-RT", None, expected_access="OLD")
        self.assertIsNone(out)
        self.assertEqual(db.clearing_writes(), [])
        self.assertTrue(any("auth_error = %s" in sql for sql, _ in db.writes))

    def test_server_graph_refresh_success_clears_auth_error(self):
        expired = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = {"id": 8, "email": EMAIL, "oauth_graph_access_token": "GAT-OLD",
               "oauth_graph_refresh_token": "GRT", "oauth_graph_token_expires": expired,
               "oauth_graph_identity": EMAIL}
        db = FakeDB(row)
        token = {"access_token": "GAT-NEW", "refresh_token": "GRT2",
                 "expires_in": 3600, "email": EMAIL}
        with patch.object(srv, "db", db), \
             patch.object(srv.oauth_providers.MicrosoftProvider, "graph_refresh_token",
                          staticmethod(lambda rt: token)):
            new_token, err = srv.KanbanHandler._refresh_graph_token_if_needed(MagicMock(), 8)
        self.assertIsNone(err)
        self.assertEqual(new_token, "GAT-NEW")
        self.assertTrue(db.clearing_writes())

    def test_server_graph_refresh_failure_does_not_clear(self):
        expired = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = {"id": 8, "email": EMAIL, "oauth_graph_access_token": "GAT-OLD",
               "oauth_graph_refresh_token": "GRT", "oauth_graph_token_expires": expired,
               "oauth_graph_identity": EMAIL}
        db = FakeDB(row)

        def boom(rt):
            raise RuntimeError("invalid_grant")

        with patch.object(srv, "db", db), \
             patch.object(srv.oauth_providers.MicrosoftProvider, "graph_refresh_token",
                          staticmethod(boom)), \
             patch.object(srv, "logger"):
            new_token, err = srv.KanbanHandler._refresh_graph_token_if_needed(MagicMock(), 8)
        self.assertIsNone(new_token)
        self.assertIn("invalid_grant", err)
        self.assertEqual(db.clearing_writes(), [])

    def test_scanner_graph_refresh_success_clears_auth_error(self):
        expired = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = {"id": 8, "email": EMAIL, "oauth_graph_access_token": "GAT-OLD",
               "oauth_graph_refresh_token": "GRT", "oauth_graph_token_expires": expired}
        db = FakeDB(row)
        token = {"access_token": "GAT-NEW", "refresh_token": "GRT2",
                 "expires_in": 3600, "email": EMAIL}
        with patch.object(ms, "db", db), \
             patch.object(oauth_providers.MicrosoftProvider, "graph_refresh_token",
                          staticmethod(lambda rt: token)):
            new_token, err = ms._build_graph_token_refresher()(8)
        self.assertIsNone(err)
        self.assertEqual(new_token, "GAT-NEW")
        self.assertTrue(db.clearing_writes())

    def test_scanner_graph_refresh_identity_mismatch_does_not_clear(self):
        expired = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = {"id": 8, "email": EMAIL, "oauth_graph_access_token": "GAT-OLD",
               "oauth_graph_refresh_token": "GRT", "oauth_graph_token_expires": expired}
        db = FakeDB(row)
        token = {"access_token": "GAT-NEW", "refresh_token": "GRT2",
                 "expires_in": 3600, "email": OTHER}
        with patch.object(ms, "db", db), \
             patch.object(oauth_providers.MicrosoftProvider, "graph_refresh_token",
                          staticmethod(lambda rt: token)), \
             patch.object(ms, "logger"):
            new_token, err = ms._build_graph_token_refresher()(8)
        self.assertIsNone(new_token)
        self.assertTrue(err)
        self.assertEqual(db.clearing_writes(), [])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Save keeps a real auth_error (v2.5.6 behaviour stays)
# ─────────────────────────────────────────────────────────────────────────────

@unittest.skipIf(srv is None, "needs importable server (live DB)")
class TestSaveKeepsAuthError(unittest.TestCase):
    def _run_save(self, body):
        db = FakeDB(_account_row(oauth_access_token="AT-SECRET",
                                 oauth_refresh_token="RT-SECRET",
                                 oauth_graph_access_token="GAT-SECRET"))
        responses = []
        with patch.object(srv, "_require_auth", return_value={"id": 7, "is_admin": True}), \
             patch.object(srv, "_mail_account_manageable", return_value=True), \
             patch.object(srv, "_read_body", return_value=json.dumps(body).encode()), \
             patch.object(srv, "_json_response",
                          lambda h, status, payload: responses.append((status, payload))), \
             patch.object(srv, "db", db), \
             patch.object(srv, "logger", MagicMock()):
            srv.KanbanHandler._handle_mail_account_update(MagicMock(), 8)
        return [w for w in db.writes if "UPDATE mail_accounts" in w[0]], responses

    def test_unchanged_address_save_keeps_real_auth_error_and_identity(self):
        updates, responses = self._run_save({"email": EMAIL, "display_name": "Estimates"})
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(len(updates), 1)
        sql = updates[0][0]
        for col in ("auth_error", "auth_error_at", "oauth_identity", "oauth_graph_identity"):
            self.assertNotIn(col, sql, f"{col} must survive an unchanged-address Save")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Scanner: clear only when every connected family verifies
# ─────────────────────────────────────────────────────────────────────────────

def _scanner_row(**over):
    row = {
        "id": 8,
        "email": EMAIL,
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "password_encrypted": "",
        "oauth_provider": "microsoft",
        "oauth_access_token": "AT",
        "oauth_refresh_token": "RT",
        "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
        "oauth_identity": EMAIL,
        "sync_enabled": True,
        "monitored_folders": "INBOX",
        "retention_days": 0,
        "smtp_user": EMAIL,
        "oauth_graph_identity": EMAIL,
        "has_exchange_token": True,
        "has_exchange_refresh": True,
        "has_graph_token": True,
    }
    row.update(over)
    return row


class TestScannerClearsStaleAuthError(unittest.TestCase):
    def _build(self, row):
        db = FakeDB()
        with patch.object(ms, "_refresh_oauth_token_if_needed", return_value=("AT", None)), \
             patch.object(mail_oauth, "db", db), \
             patch.object(ms, "logger", MagicMock()):
            cfg = ms._mailbox_cfg_from_row(row)
        return cfg, db

    def test_clears_when_every_connected_family_is_verified(self):
        cfg, db = self._build(_scanner_row())
        self.assertIsNotNone(cfg)
        writes = db.clearing_writes()
        self.assertEqual(len(writes), 1)
        self.assertIn("WHERE id = %s AND auth_error IS NOT NULL", writes[0][0])
        self.assertEqual(writes[0][1], (8,))

    def test_does_not_clear_when_graph_is_unverified(self):
        row = _scanner_row(has_graph_token=True, oauth_graph_identity="")
        cfg, db = self._build(row)
        self.assertIsNotNone(cfg)  # the IMAP gate only cares about exchange
        self.assertEqual(db.clearing_writes(), [])

    def test_does_not_clear_when_graph_belongs_to_another_mailbox(self):
        row = _scanner_row(oauth_graph_identity=OTHER)
        cfg, db = self._build(row)
        self.assertIsNotNone(cfg)
        self.assertEqual(db.clearing_writes(), [])

    def test_does_not_clear_when_smtp_login_belongs_to_another_mailbox(self):
        row = _scanner_row(smtp_user=OTHER)
        cfg, db = self._build(row)
        self.assertIsNotNone(cfg)
        self.assertEqual(db.clearing_writes(), [])

    def test_does_not_clear_when_the_gate_refuses(self):
        row = _scanner_row(oauth_identity="")
        db = FakeDB()
        with patch.object(ms, "_refresh_oauth_token_if_needed", return_value=("AT", None)), \
             patch.object(mail_oauth, "db", db), \
             patch.object(ms, "logger", MagicMock()):
            cfg = ms._mailbox_cfg_from_row(row)
        self.assertIsNone(cfg)
        self.assertEqual(db.clearing_writes(), [])
        self.assertTrue(any("auth_error = %s" in sql for sql, _ in db.writes))

    def test_password_account_error_is_not_cleared(self):
        row = _scanner_row(oauth_provider=None, oauth_access_token="", oauth_refresh_token="",
                           oauth_identity="", password_encrypted="enc", smtp_user=EMAIL,
                           has_exchange_token=False, has_exchange_refresh=False,
                           has_graph_token=False, oauth_graph_identity="")
        cfg, db = self._build(row)
        self.assertIsNotNone(cfg)
        self.assertEqual(db.clearing_writes(), [])


# ─────────────────────────────────────────────────────────────────────────────
# 6. record_auth_error must not re-stamp an unchanged message
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordAuthErrorDoesNotRestamp(unittest.TestCase):
    def test_same_message_is_not_rewritten(self):
        db = FakeDB(rows={5: {"auth_error": "already here"}})
        with patch.object(mail_oauth, "db", db), \
             patch.object(mail_oauth, "_notify_admins_auth_error") as notify:
            mail_oauth.record_auth_error(5, "already here")
        self.assertEqual(db.writes, [])
        notify.assert_not_called()

    def test_changed_message_is_stamped(self):
        db = FakeDB(rows={5: {"auth_error": "old reason"}})
        with patch.object(mail_oauth, "db", db), \
             patch.object(mail_oauth, "_notify_admins_auth_error") as notify:
            mail_oauth.record_auth_error(5, "new reason")
        self.assertEqual(len(db.writes), 1)
        self.assertIn("auth_error = %s", db.writes[0][0])
        self.assertIn("auth_error_at = NOW()", db.writes[0][0])
        notify.assert_not_called()

    def test_first_set_stamps_and_notifies(self):
        db = FakeDB(rows={5: {"auth_error": None}})
        with patch.object(mail_oauth, "db", db), \
             patch.object(mail_oauth, "_notify_admins_auth_error") as notify:
            mail_oauth.record_auth_error(5, "first reason")
        self.assertEqual(len(db.writes), 1)
        notify.assert_called_once()

    def test_clear_auth_error_only_when_set(self):
        db = FakeDB(executing=0)
        with patch.object(mail_oauth, "db", db):
            self.assertFalse(mail_oauth.clear_auth_error(5))
        self.assertIn("auth_error IS NOT NULL", db.writes[0][0])


# ─────────────────────────────────────────────────────────────────────────────
# 7. Payload: identityStatus matches the sync gate, no token values
# ─────────────────────────────────────────────────────────────────────────────

@unittest.skipIf(srv is None, "needs importable server (live DB)")
class TestMailAccountsPayloadIdentity(unittest.TestCase):
    def _payload(self, row):
        captured = []
        sql_seen = []

        class DB:
            def query_dicts(self, sql, params=None):
                if "FROM mail_accounts" in sql:
                    sql_seen.append(sql)
                    return [dict(row)]
                return []

            def query_one(self, sql, params=None):
                return None

            def execute(self, sql, params=None):
                return 1

        with patch.object(srv, "_require_auth", return_value={"id": 7, "is_admin": False}), \
             patch.object(srv, "_is_limited", return_value=False), \
             patch.object(srv, "_mail_account_manageable", return_value=True), \
             patch.object(srv, "_json_response",
                          lambda h, status, payload: captured.append((status, payload))), \
             patch.object(srv, "db", DB()), \
             patch.object(srv, "logger", MagicMock()):
            srv.KanbanHandler._handle_mail_accounts(MagicMock())
        self.assertEqual(captured[0][0], 200)
        return captured[0][1], sql_seen[0]

    def _row(self, **over):
        row = {
            "id": 8,
            "email": EMAIL,
            "display_name": "Estimates",
            "oauth_provider": "microsoft",
            "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
            "oauth_scopes": "",
            "sync_enabled": True,
            "monitored_folders": "INBOX",
            "is_crm_mail": True,
            "retention_days": 0,
            "owner_user_id": 7,
            "last_sync": None,
            "created_at": None,
            "oauth_identity": None,
            "oauth_last_error": None,
            "oauth_last_error_at": None,
            "auth_error": None,
            "has_exchange_token": True,
            "has_exchange_refresh": True,
            "has_graph_token": False,
            "oauth_graph_token_expires": None,
            "has_graph_refresh": False,
            "oauth_graph_identity": None,
            "oauth_graph_last_error": None,
            "oauth_graph_last_error_at": None,
            "smtp_auth_disabled": False,
            "smtp_user": EMAIL,
            "tab_icon": "",
            "tab_color": "",
            "auto_bcc_addr": "",
            "smtp_from_name": "",
            "smtp_host": "",
            "smtp_port": 587,
            "imap_host": "outlook.office365.com",
            "imap_port": 993,
        }
        row.update(over)
        return row

    def test_tokens_present_identity_empty_is_unverifiable(self):
        payload, sql = self._payload(self._row())
        acct = payload["accounts"][0]
        self.assertEqual(acct["identityStatus"], mail_identity.IDENTITY_UNVERIFIABLE)
        self.assertTrue(acct["identityError"])
        self.assertEqual(acct["authError"], acct["identityError"])
        # presence is selected as a boolean; the token value is never selected
        self.assertIn("oauth_access_token IS NOT NULL AS has_exchange_token", sql)
        self.assertNotIn("oauth_access_token,", sql)
        self.assertEqual(acct["oauth_access_token"], "")
        self.assertNotIn("has_exchange_token", acct)

    def test_verified_identity_is_ok_and_has_no_banner(self):
        payload, _sql = self._payload(self._row(oauth_identity=EMAIL))
        acct = payload["accounts"][0]
        self.assertEqual(acct["identityStatus"], mail_identity.IDENTITY_OK)
        self.assertEqual(acct["authError"], "")

    def test_graph_mismatch_is_reported(self):
        payload, _sql = self._payload(self._row(
            oauth_identity=EMAIL, has_graph_token=True, has_graph_refresh=True,
            oauth_graph_identity=OTHER))
        acct = payload["accounts"][0]
        self.assertEqual(acct["identityStatus"], mail_identity.IDENTITY_MISMATCH)
        self.assertIn(OTHER, acct["identityError"])

    def test_payload_never_contains_a_token_value(self):
        payload, _sql = self._payload(self._row())
        blob = json.dumps(payload, default=str)
        for secret in ("AT-SECRET", "RT-SECRET", "GAT-SECRET", "GRT-SECRET"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
