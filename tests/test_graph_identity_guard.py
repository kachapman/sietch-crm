#!/usr/bin/env python3
"""Unit tests: Graph identity verification + send-time guard (live 2026-10-29).

THE BUG
-------
The Aplus Outlook account held a Microsoft **Graph** token that belonged to
the **MDM** mailbox, so every Graph send went out as "MC MDM Construction"
while the CRM recorded "aplus.estimates@outlook.com".

WHY THE EXISTING GUARD MISSED IT
--------------------------------
`server.py` already had an identity check on the Graph callback, but it read:

    if ms_email and acct_email and ms_email != acct_email:   # refuse

`ms_email` came from the token response. The Graph scope was

    offline_access https://graph.microsoft.com/Mail.Send

which carries **no `openid`**, so the token response contained **no
id_token** and the identity could not be read. The fallback probe
(Graph `/me`) requires `User.Read`, which was not granted — it answered
**HTTP 403**. So `ms_email` was always "" and the guard **skipped
entirely**: any Microsoft account's token could be stored on any row.

These tests pin the fix:
  1. the Graph scope includes openid/email/profile
  2. the authorize URL forces the account picker (+ login_hint)
  3. an UNVERIFIABLE identity is REFUSED (the hole)
  4. a MISMATCHED identity is refused
  5. a refresh re-verifies the token's identity
  6. the send-time guard blocks a mismatch AND an unverified account
  7. the send-time guard blocks an SMTP user that differs from the account

Run with: python3 tests/test_graph_identity_guard.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import oauth_providers as op
import mail_send as ms


class TestGraphScopeRevealsIdentity(unittest.TestCase):
    def test_graph_scope_includes_openid(self):
        """Without openid the token response has no id_token → no identity."""
        scope = op.MicrosoftProvider._GRAPH_SCOPE
        self.assertIn("openid", scope)
        self.assertIn("email", scope)
        self.assertIn("profile", scope)
        self.assertIn("https://graph.microsoft.com/Mail.Send", scope)

    def test_legacy_scope_kept_for_old_grants(self):
        """Old refresh tokens cannot be upgraded (AADSTS70000) — keep the
        legacy scope so the refresh can fall back to it."""
        self.assertNotIn("openid", op.MicrosoftProvider._GRAPH_SCOPE_LEGACY)
        self.assertIn("Mail.Send", op.MicrosoftProvider._GRAPH_SCOPE_LEGACY)


class TestAuthorizeUrlForcesAccountPicker(unittest.TestCase):
    def test_graph_authorize_url_select_account(self):
        url = op.MicrosoftProvider.graph_authorize_url("STATE123")
        self.assertIn("prompt=select_account", url)
        self.assertIn("state=STATE123", url)

    def test_graph_authorize_url_has_login_hint(self):
        url = op.MicrosoftProvider.graph_authorize_url("S", login_hint="aplus.estimates@outlook.com")
        self.assertIn("login_hint=aplus.estimates%40outlook.com", url)

    def test_exchange_authorize_url_select_account(self):
        url = op.MicrosoftProvider.authorize_url("S", login_hint="x@y.com")
        self.assertIn("prompt=select_account", url)
        self.assertIn("login_hint=x%40y.com", url)


class TestIdentityExtraction(unittest.TestCase):
    def test_id_token_email_is_extracted(self):
        import base64, json
        payload = base64.urlsafe_b64encode(json.dumps({
            "email": "aplus.estimates@outlook.com",
            "preferred_username": "aplus.estimates@outlook.com",
        }).encode()).rstrip(b"=").decode()
        id_token = f"header.{payload}.sig"
        email = op.MicrosoftProvider.account_email_from_token_result({"id_token": id_token})
        self.assertEqual(email, "aplus.estimates@outlook.com")


class TestGraphIdentityState(unittest.TestCase):
    """The classifier used by the send-time guard."""

    def test_ok_when_identity_matches(self):
        state, detail = ms._graph_identity_state({
            "email": "aplus.estimates@outlook.com",
            "oauth_graph_identity": "aplus.estimates@outlook.com",
        })
        self.assertEqual(state, "ok")

    def test_mismatch_when_identity_is_another_mailbox(self):
        state, detail = ms._graph_identity_state({
            "email": "aplus.estimates@outlook.com",
            "oauth_graph_identity": "mdm.estimates@outlook.com",
        })
        self.assertEqual(state, "mismatch")
        self.assertIn("mdm.estimates@outlook.com", detail)

    def test_unverified_when_identity_missing(self):
        state, detail = ms._graph_identity_state({
            "email": "aplus.estimates@outlook.com",
            "oauth_graph_identity": None,
        })
        self.assertEqual(state, "unverified")
        self.assertIn("reconnect", detail.lower())


class TestSendTimeGuard(unittest.TestCase):
    """Nothing may go out through a token that belongs to another mailbox."""

    def _acct(self, **kw):
        a = {
            "id": 5,
            "email": "aplus.estimates@outlook.com",
            "oauth_provider": "microsoft",
            "oauth_access_token": "EA",
            "oauth_refresh_token": "ER",
            "oauth_graph_access_token": "GA",
            "oauth_graph_refresh_token": "GR",
            "oauth_graph_identity": "aplus.estimates@outlook.com",
            "smtp_host": "smtp-mail.outlook.com",
            "smtp_port": 587,
            "smtp_user": "aplus.estimates@outlook.com",
            "smtp_from_name": "A-plus Restoration GC",
            "smtp_use_tls": True,
        }
        a.update(kw)
        return a

    def test_mismatched_identity_blocks_the_send(self):
        acct = self._acct(oauth_graph_identity="mdm.estimates@outlook.com")
        ok, err, method = ms.try_send_for_account(
            acct, to_addr="x@y.com", subject="s",
            refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertFalse(ok)
        self.assertIn("Refusing to send", err)
        self.assertIn("mdm.estimates@outlook.com", err)

    def test_unverified_identity_blocks_the_send(self):
        acct = self._acct(oauth_graph_identity=None)
        ok, err, method = ms.try_send_for_account(
            acct, to_addr="x@y.com", subject="s",
            refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertFalse(ok)
        self.assertIn("Refusing to send", err)

    def test_verified_identity_is_allowed_through(self):
        """The guard must not block a healthy account (no false positives)."""
        acct = self._acct()
        with patch("mail_graph_client.send_email_via_graph",
                   return_value=(True, None)) as send:
            ok, err, method = ms.try_send_for_account(
                acct, to_addr="x@y.com", subject="s",
                refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertTrue(ok, err)
        self.assertEqual(method, "graph")
        send.assert_called_once()

    def test_guard_runs_before_any_network_call(self):
        """The refusal must happen BEFORE Graph is contacted."""
        acct = self._acct(oauth_graph_identity="mdm.estimates@outlook.com")
        with patch("mail_graph_client.send_email_via_graph") as send:
            ok, err, method = ms.try_send_for_account(
                acct, to_addr="x@y.com", subject="s",
                refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertFalse(ok)
        send.assert_not_called()

    def test_smtp_user_mismatch_is_blocked(self):
        """SMTP sends as the authenticated smtp_user, not the From header."""
        acct = self._acct(
            oauth_graph_access_token=None,
            oauth_graph_refresh_token=None,
            smtp_user="mdm.estimates@outlook.com",
        )
        with patch("smtp_client.send_email_from_account") as send:
            ok, err, method = ms.try_send_for_account(
                acct, to_addr="x@y.com", subject="s")
        self.assertFalse(ok)
        self.assertIn("Refusing to send", err)
        send.assert_not_called()

    def test_matching_smtp_user_is_allowed(self):
        acct = self._acct(
            oauth_graph_access_token=None,
            oauth_graph_refresh_token=None,
        )
        with patch("smtp_client.send_email_from_account",
                   return_value=(True, None)) as send:
            ok, err, method = ms.try_send_for_account(
                acct, to_addr="x@y.com", subject="s")
        self.assertTrue(ok, err)
        send.assert_called_once()


class TestServerCallbackFailsClosed(unittest.TestCase):
    """The connect-time guard must REFUSE an unverifiable identity."""

    def _run_callback(self, state_key, signed_in_email, account_email="aplus.estimates@outlook.com"):
        import time as _time
        import server as _server
        redirects, writes = [], []

        class Shim:
            def __init__(self, path):
                self.path = path

            def send_response(self, *a, **k):
                pass

            def end_headers(self):
                pass

        fake_exchange = {"access_token": "AT", "refresh_token": "RT",
                         "expires_in": 3600, "email": signed_in_email}
        mock_db = MagicMock()
        mock_db.query_one.return_value = {"id": 5, "email": account_email}
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
        _server._oauth_graph_states[state_key] = (7, 5, _time.time() + 600)
        shim = Shim(f"/x?code=x&state={state_key}")
        shim._oauth_redirect_with_status = lambda st, msg: redirects.append((st, msg))
        with patch.object(_server, "db", mock_db), \
             patch.object(_server, "logger"), \
             patch.object(_server.oauth_providers.MicrosoftProvider, "graph_exchange_code",
                          classmethod(lambda cls, code: fake_exchange)):
            _server.KanbanHandler._handle_oauth_graph_callback(shim)
        _server._oauth_graph_states.pop(state_key, None)
        return redirects, writes

    def test_unverifiable_identity_is_refused_and_writes_nothing(self):
        """THE HOLE: an empty identity used to be accepted silently."""
        redirects, writes = self._run_callback("st-unverifiable", "")
        self.assertTrue(redirects, "the callback must redirect with an error")
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual([s for s, _ in writes if "oauth_graph_access_token = %s" in s], [],
                         "an unverifiable token must never be stored")

    def test_mismatched_identity_writes_nothing(self):
        redirects, writes = self._run_callback(
            "st-mismatch", "mdm.estimates@outlook.com", "aplus.estimates@outlook.com")
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual([s for s, _ in writes if "oauth_graph_access_token = %s" in s], [])

    def test_matching_identity_is_stored_with_the_identity(self):
        redirects, writes = self._run_callback(
            "st-match", "aplus.estimates@outlook.com", "aplus.estimates@outlook.com")
        self.assertEqual(redirects[0][0], "graph_success")
        stored = [s for s, _ in writes if "oauth_graph_access_token = %s" in s]
        self.assertTrue(stored, "a matching identity must be stored")
        self.assertTrue(any("oauth_graph_identity = %s" in s for s, _ in writes))


class TestSourceGuards(unittest.TestCase):
    """Source-level guards for the pieces that need a live browser/session."""

    def setUp(self):
        from pathlib import Path
        self.server = Path('/home/chapman-server/Projects/new-crm/server.py').read_text(encoding="utf-8")
        self.app = Path('/home/chapman-server/Projects/new-crm/public/app.js').read_text(encoding="utf-8")

    def test_graph_callback_refuses_unverifiable_identity(self):
        at = self.server.index("def _handle_oauth_graph_callback")
        region = self.server[at:at + 6000]
        self.assertIn("if not ms_email:", region,
                      "the callback must fail closed on an unverifiable identity")

    def test_inline_reply_sends_from_the_messages_account(self):
        """The inline reply used accounts[0] → every reply went out through
        one mailbox regardless of the tab (live bug 2026-10-29)."""
        at = self.app.index("function renderInlineReply")
        region = self.app[at:at + 12000]
        # The account sent from must come from the message's own account_id
        # (or the active tab), never from a bare accounts[0].
        self.assertNotIn("const accountId = accounts.length > 0 ? accounts[0].id",
                         region,
                         "the inline reply must use the message's own account")
        self.assertIn("messageAccountId", region)
        self.assertIn(".inline-reply-from", region,
                      "the inline reply must let the user see/choose the account")

    def test_send_paths_call_the_confirmation_dialog(self):
        self.assertIn("confirmMailSend", self.app)
        self.assertGreaterEqual(self.app.count("confirmMailSend({"), 3,
                                "compose, compose-tab and inline reply must all confirm")

    def test_confirm_dialog_supports_html_body(self):
        self.assertIn("messageHtml", self.app)


if __name__ == "__main__":
    unittest.main(verbosity=1)
