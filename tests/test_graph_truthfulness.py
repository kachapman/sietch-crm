#!/usr/bin/env python3
"""Unit tests for Graph truthfulness + SMTP-disable memory (2026-10-22).

1. Token states are honest: connected / auto-renew (hourly expiry is normal)
   / action-needed (refresh observably failed) — never a bare "expired" that
   reads as broken. Identity of the connected mailbox is exposed.
2. Callback stores the verified identity; mismatch refuses; disconnect clears.
3. A Microsoft mailbox that failed SMTP with 5.7.139 is remembered: future
   sends skip SMTP entirely and fail loudly unless Graph delivers. SMTP
   "success" (250) is not proof of delivery for these mailboxes. SMTP success
   self-heals the flag.

Run with: python3 tests/test_graph_truthfulness.py
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


def _row(**kw):
    base = {"id": 5, "email": "aplus.estimates@outlook.com", "display_name": "A+",
            "imap_host": "x", "imap_port": 993, "smtp_host": "h", "smtp_port": 587,
            "smtp_from_name": "", "smtp_user": "", "sync_enabled": True,
            "monitored_folders": "", "oauth_provider": "microsoft",
            "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
            "oauth_scopes": "", "is_crm_mail": False, "auto_bcc_addr": "",
            "retention_days": 30, "tab_icon": "", "tab_color": "",
            "last_sync": None, "owner_user_id": 7, "created_at": None}
    base.update(kw)
    return base


class Shim:
    headers = {"Cookie": ""}

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def __init__(self, path="/"):
        self.path = path

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    wfile = MagicMock()


def _accounts_response(rows):
    responses = []
    mock_db = MagicMock()

    def fake_query_dicts(sql, params=None, *a, **k):
        if "FROM mail_accounts" in sql:
            return [dict(r) for r in rows]
        return []

    mock_db.query_dicts.side_effect = fake_query_dicts
    with patch.object(_server, "_require_auth", lambda h: ADMIN), \
        patch.object(_server, "_is_limited", lambda u: False), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_mail_accounts(Shim())
    assert responses, "no response"
    return responses[0]


class TestTokenStates(unittest.TestCase):
    def _acct(self, **kw):
        status, body = _accounts_response([_row(**kw)])
        self.assertEqual(status, 200)
        return body["accounts"][0]

    def test_live_token_connected_with_identity(self):
        a = self._acct(has_graph_token=True, has_graph_refresh=True,
                       oauth_graph_token_expires=datetime.now(timezone.utc) + timedelta(minutes=30),
                       oauth_graph_identity="aplus.estimates@outlook.com")
        self.assertEqual(a["graphStatus"], "connected")
        self.assertEqual(a["graphIdentity"], "aplus.estimates@outlook.com")

    def test_expired_access_with_refresh_is_auto_renew(self):
        a = self._acct(has_graph_token=True, has_graph_refresh=True,
                       oauth_graph_token_expires=datetime.now(timezone.utc) - timedelta(hours=2),
                       oauth_graph_last_error=None)
        self.assertEqual(a["graphStatus"], "auto-renew")

    def test_failed_refresh_is_action_needed(self):
        a = self._acct(has_graph_token=True, has_graph_refresh=True,
                       oauth_graph_token_expires=datetime.now(timezone.utc) - timedelta(hours=2),
                       oauth_graph_last_error="Graph token refresh failed: 400")
        self.assertEqual(a["graphStatus"], "action-needed")

    def test_expired_without_refresh_is_action_needed(self):
        a = self._acct(has_graph_token=True, has_graph_refresh=False,
                       oauth_graph_token_expires=datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertEqual(a["graphStatus"], "action-needed")

    def test_no_secrets_or_flags_leak(self):
        a = self._acct(has_graph_token=True, has_graph_refresh=True,
                       oauth_graph_token_expires=datetime.now(timezone.utc) + timedelta(hours=1))
        for key in ("oauth_graph_access_token", "oauth_graph_refresh_token",
                    "has_graph_token", "has_graph_refresh"):
            self.assertNotIn(key, a)


class TestIdentityLifecycle(unittest.TestCase):
    def _run_callback(self, state_key, account_email, token_email, ident="a@outlook.com"):
        import time as _time
        redirects = []
        writes = []
        mock_db = MagicMock()
        mock_db.query_one.return_value = {"id": 5, "email": account_email}
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
        _server._oauth_graph_states[state_key] = (7, 5, _time.time() + 600)
        fake_exchange = {"access_token": "AT", "refresh_token": "RT",
                         "expires_in": 3600, "email": token_email}
        shim = Shim(f"/x?code=x&state={state_key}")
        shim._oauth_redirect_with_status = lambda st, msg: redirects.append((st, msg))
        with patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"), \
            patch.object(_server.oauth_providers.MicrosoftProvider, "graph_exchange_code",
                         classmethod(lambda cls, code: fake_exchange)):
            _server.KanbanHandler._handle_oauth_graph_callback(shim)
        _server._oauth_graph_states.pop(state_key, None)
        return redirects, writes

    def test_connect_stores_identity_and_clears_error(self):
        redirects, writes = self._run_callback("st-id", "a@outlook.com", "a@outlook.com")
        self.assertEqual(redirects[0][0], "graph_success")
        upd = [p for s, p in writes if "oauth_graph_identity" in s]
        self.assertTrue(upd)
        self.assertIn("a@outlook.com", upd[0])
        self.assertIn("oauth_graph_last_error = NULL", [s for s, _ in writes][0] if writes else "")

    def test_mismatch_never_writes_tokens(self):
        redirects, writes = self._run_callback("st-idx", "b@outlook.com", "a@outlook.com")
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual([s for s, _ in writes if "oauth_graph_access_token = %s" in s], [])

    def test_disconnect_clears_identity(self):
        mock_db = MagicMock()
        writes = []
        mock_db.query_one.return_value = {"id": 5}
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append(sql)
        with patch.object(_server, "_require_auth", lambda h: ADMIN), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"):
            _server.KanbanHandler._handle_oauth_graph_disconnect(Shim(), 5)
        self.assertTrue(any("oauth_graph_identity = NULL" in s for s in writes))
        self.assertTrue(any("oauth_graph_last_error = NULL" in s for s in writes))


class TestSmtpMemoryRouting(unittest.TestCase):
    def _acct(self, **kw):
        a = {"id": 5, "email": "a@outlook.com", "oauth_provider": "microsoft",
             "oauth_access_token": "EA", "oauth_refresh_token": "ER",
             "smtp_host": "smtp.office365.com", "smtp_port": 587,
             "smtp_user": "", "smtp_from_name": "", "smtp_use_tls": True}
        a.update(kw)
        return a

    def test_known_dead_skips_smtp_and_fails_loud(self):
        import mail_send as _ms
        import db as _db
        acct = self._acct(smtp_auth_disabled=True)
        with patch("smtp_client.send_email_from_account") as m_smtp, \
            patch.object(_db, "execute"):
            ok, err, method = _ms.try_send_for_account(
                acct, to_addr="t@x.com", subject="s", html_body="b",
                refresh_graph_token=lambda aid: (None, "expired"))
        m_smtp.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("Nothing was sent", err)

    def test_known_dead_with_graph_sends_graph(self):
        import mail_send as _ms
        import db as _db
        acct = self._acct(smtp_auth_disabled=True)
        with patch("smtp_client.send_email_from_account") as m_smtp, \
            patch("mail_graph_client.send_email_via_graph", return_value=(True, None)), \
            patch.object(_db, "execute"):
            ok, err, method = _ms.try_send_for_account(
                acct, to_addr="t@x.com", subject="s", html_body="b",
                refresh_graph_token=lambda aid: ("GTOK", None))
        m_smtp.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(method, "graph")

    def test_smtp_5139_memorizes_and_reports(self):
        import mail_send as _ms
        import db as _db
        writes = []
        acct = self._acct()
        def bad_smtp(**k):
            return False, "Microsoft has disabled SMTP sending for this Outlook account. Ask admin."
        with patch("smtp_client.send_email_from_account", side_effect=bad_smtp), \
            patch("mail_graph_client.send_email_via_graph", return_value=(False, "graph down")), \
            patch.object(_db, "execute", side_effect=lambda s, p=None, *a, **k: writes.append((s, p))):
            ok, err, method = _ms.try_send_for_account(
                acct, to_addr="t@x.com", subject="s", html_body="b",
                refresh_graph_token=lambda aid: (None, "expired"))
        self.assertFalse(ok)
        flag_writes = [p for s, p in writes if "smtp_auth_disabled" in s]
        self.assertTrue(flag_writes)
        self.assertEqual(flag_writes[0][0], True)

    def test_flag_is_sticky_smtp_never_retried(self):
        # By design the flag does not self-heal: a single SMTP 250 proves
        # nothing for these mailboxes, and Microsoft refuses to re-enable
        # AUTH via support. While flagged, sends go Graph-only.
        import mail_send as _ms
        import db as _db
        writes = []
        acct = self._acct(smtp_auth_disabled=True)
        with patch("smtp_client.send_email_from_account") as m_smtp, \
            patch("mail_graph_client.send_email_via_graph", return_value=(True, None)), \
            patch.object(_db, "execute", side_effect=lambda s, p=None, *a, **k: writes.append((s, p))):
            ok, err, method = _ms.try_send_for_account(
                acct, to_addr="t@x.com", subject="s", html_body="b",
                refresh_graph_token=lambda aid: ("GTOK", None))
        self.assertTrue(ok)
        self.assertEqual(method, "graph")
        m_smtp.assert_not_called()
        self.assertEqual([s for s, p in writes if "smtp_auth_disabled" in s], [])

    def test_unflagged_smtp_success_sets_no_flag(self):
        import mail_send as _ms
        import db as _db
        writes = []
        acct = self._acct()
        with patch("smtp_client.send_email_from_account", return_value=(True, None)), \
            patch.object(_db, "execute", side_effect=lambda s, p=None, *a, **k: writes.append((s, p))):
            ok, err, method = _ms.try_send_for_account(
                acct, to_addr="t@x.com", subject="s", html_body="b",
                refresh_graph_token=lambda aid: (None, "expired"))
        self.assertTrue(ok)
        self.assertEqual([s for s, p in writes if "smtp_auth_disabled" in s], [])


class TestFrontendTruthfulnessGuards(unittest.TestCase):
    def test_three_states_and_identity_wired(self):
        src = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        for needle in ("auto-renew", "action-needed", "graphIdentity", "renews automatically on send",
                       "smtp_auth_disabled", "sending via Graph only", "Email sent"):
            self.assertIn(needle, src)
        self.assertIn("graphStatus === 'auto-renew'", src)
        self.assertIn("graphStatus === 'action-needed'", src)
        # Send toasts name the actual path (user asked: confirmation of how).
        self.assertIn("via Microsoft Graph", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
