#!/usr/bin/env python3
"""Unit tests for Microsoft Graph Send acknowledgement + identity check.

Live issues 2026-10-21/22:
1. Per-inbox "Microsoft Graph Send" status always showed "Not connected" and
   the compose banner never cleared: _handle_mail_accounts computed graphStatus
   from oauth_graph_* columns its SELECT never fetched (always None).
2. The status dot HTML was set via textContent, rendering literal markup.
3. The Graph callback stored tokens with no identity check: signing in as
   mailbox A while connecting account B silently reassigns B's sender to A
   (Graph /me/sendMail derives the sender from the token).

Run with: python3 tests/test_graph_oauth_status.py
"""
import base64
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server
import oauth_providers as _op

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


class TestGraphStatusAcknowledged(unittest.TestCase):
    def test_connected_token_reports_connected(self):
        status, body = _accounts_response([_row(has_graph_token=True,
            oauth_graph_token_expires=datetime.now(timezone.utc) + timedelta(hours=1))])
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"][0]["graphStatus"], "connected")

    def test_expired_token_without_refresh_reports_action_needed(self):
        # An expired token that cannot be renewed needs action; the server
        # reports "action-needed" (the UI shows "Reconnect needed"). The
        # old expectation of "expired" was stale (it failed on main before
        # the 2026-10-29 identity work).
        status, body = _accounts_response([_row(has_graph_token=True,
            oauth_graph_token_expires=datetime.now(timezone.utc) - timedelta(days=1))])
        self.assertEqual(body["accounts"][0]["graphStatus"], "action-needed")

    def test_absent_token_reports_none(self):
        status, body = _accounts_response([_row(has_graph_token=False,
            oauth_graph_token_expires=None)])
        self.assertIsNone(body["accounts"][0]["graphStatus"])

    def test_non_microsoft_never_graph(self):
        status, body = _accounts_response([_row(oauth_provider="password",
            has_graph_token=True,
            oauth_graph_token_expires=datetime.now(timezone.utc) + timedelta(hours=1))])
        self.assertIsNone(body["accounts"][0]["graphStatus"])

    def test_no_token_material_leaks(self):
        status, body = _accounts_response([_row(has_graph_token=True,
            oauth_graph_token_expires=datetime.now(timezone.utc) + timedelta(hours=1))])
        acct = body["accounts"][0]
        self.assertNotIn("oauth_graph_access_token", acct)
        self.assertNotIn("oauth_graph_refresh_token", acct)
        self.assertNotIn("has_graph_token", acct)


def _callback(state_key, account_email, token_email):
    """Run the Graph callback with a canned state + exchange result."""
    import time as _time
    redirects = []
    writes = []
    mock_db = MagicMock()
    mock_db.query_one.return_value = {"id": 5, "email": account_email}
    mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
    _server._oauth_graph_states[state_key] = (7, 5, _time.time() + 600)
    fake_exchange = {"access_token": "AT", "refresh_token": "RT",
                     "expires_in": 3600, "email": token_email}
    shim = Shim(f"/api/v2/mail/oauth/microsoft/graph/callback?code=x&state={state_key}")
    shim._oauth_redirect_with_status = lambda st, msg: redirects.append((st, msg))
    with patch.object(_op.MicrosoftProvider, "graph_exchange_code", classmethod(lambda cls, code: fake_exchange)), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_oauth_graph_callback(shim)
    _server._oauth_graph_states.pop(state_key, None)
    return redirects, writes


class TestGraphIdentityCheck(unittest.TestCase):
    def test_mismatch_refuses_without_writes(self):
        redirects, writes = _callback("st-mismatch", "b@outlook.com", "a@outlook.com")
        self.assertEqual(redirects[0][0], "error")
        self.assertIn("a@outlook.com", redirects[0][1])
        tok_writes = [s for s, _ in writes if "oauth_graph_access_token" in s]
        self.assertEqual(tok_writes, [])

    def test_match_stores_tokens(self):
        redirects, writes = _callback("st-match", "a@outlook.com", "A@outlook.com")
        self.assertEqual(redirects[0][0], "graph_success")
        self.assertTrue(any("oauth_graph_access_token" in s for s, _ in writes))

    def test_unverifiable_identity_is_refused(self):
        # CHANGED 2026-10-29. This used to assert that an unverifiable
        # identity still connects ("cannot refuse what cannot be verified").
        # That policy is exactly what let mailbox A's token be stored on
        # mailbox B: the Graph scope had no `openid` (no id_token) and the
        # /me fallback 403'd, so the identity was always "" and the guard
        # never ran. The Aplus account then held an MDM token and every
        # send went out as MDM Construction.
        #
        # An unverifiable identity is now REFUSED — fail closed. The Graph
        # scope includes `openid`, so a normal connect always carries an
        # id_token and this state only arises for a broken/unreadable
        # response, where storing the token would be reckless.
        redirects, writes = _callback("st-empty", "a@outlook.com", "")
        self.assertEqual(redirects[0][0], "error")
        tok_writes = [s for s, _ in writes if "oauth_graph_access_token" in s]
        self.assertEqual(tok_writes, [],
                         "a token whose mailbox cannot be verified must not be stored")


def _fake_id_token(payload_dict):
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"


class TestAccountEmailExtraction(unittest.TestCase):
    def test_msa_prefix_username_stripped(self):
        tok = _fake_id_token({"preferred_username": "live.com#user@outlook.com"})
        self.assertEqual(_op.MicrosoftProvider.account_email_from_token_result({"id_token": tok}),
                         "user@outlook.com")

    def test_verified_email_preferred(self):
        tok = _fake_id_token({"email": "a@outlook.com", "preferred_username": "live.com#b@outlook.com"})
        self.assertEqual(_op.MicrosoftProvider.account_email_from_token_result({"id_token": tok}),
                         "a@outlook.com")

    def test_empty_result_yields_empty_without_network(self):
        self.assertEqual(_op.MicrosoftProvider.account_email_from_token_result({}), "")


class TestStatusMarkupGuard(unittest.TestCase):
    def test_no_textcontent_with_markup(self):
        import re
        src = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        bad = [ln.strip()[:90] for ln in src.splitlines()
               if re.search(r"\btextContent\s*=\s*['\"`][^'\"`]*<span", ln)]
        self.assertEqual(bad, [])
        at = src.index("function updateGraphStatusBlock")
        self.assertIn("innerHTML", src[at:at + 2500])


if __name__ == "__main__":
    unittest.main(verbosity=1)
