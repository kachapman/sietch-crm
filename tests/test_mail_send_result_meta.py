#!/usr/bin/env python3
"""Regression: the real HTTP /api/v2/mail/send handler path, transport mocked.

These tests call the production KanbanHandler send path end-to-end (compose,
reply, forward, API-key Bearer send). They mock `mail_send.try_send_for_account`
(the shared helper / transport), NOT `KanbanHandler._try_send_for_account`.
That is the point: the 2026-11-11 prod outage was a wrapper signature drift
(`result_meta=send_meta` was passed to a wrapper that did not accept it),
which existing tests hid by patching the wrapper itself.

On the broken wrapper these tests fail with HTTP 500:
  unexpected keyword argument 'result_meta'
Nothing is sent: network_guard is installed and the helper is mocked.

Also: a missing account_id must return 400, not 500.

Run with: python3 tests/test_mail_send_result_meta.py
"""

from __future__ import annotations

import inspect
import os
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
    import api_tokens
    import mail_send
    import mcp_local_api
    import server
    HAVE_DB = True
except Exception as exc:  # pragma: no cover
    print(f"SKIP import: {exc}")
    HAVE_DB = False


def _fake_transport(acct, **kwargs):
    """Stand-in for mail_send.try_send_for_account. No network."""
    meta = kwargs.get("result_meta")
    if meta is not None:
        meta["fallback_reason"] = None
        meta["sent_as"] = (acct.get("email") or "")
    return (True, None, "smtp_password")


class TestWrapperSignature(unittest.TestCase):
    """Pure: would have failed on the 0906d3b-era wrapper."""

    def test_wrapper_accepts_and_forwards_result_meta(self):
        sig = inspect.signature(server.KanbanHandler._try_send_for_account)
        self.assertIn("result_meta", sig.parameters)
        self.assertIn("recipient_locks", sig.parameters)
        helper = inspect.signature(mail_send.try_send_for_account)
        for name in ("result_meta", "recipient_locks", "refresh_graph_token"):
            self.assertIn(name, helper.parameters)

    def test_handle_mail_send_passes_result_meta(self):
        src = Path(server.__file__).read_text()
        start = src.index("def _handle_mail_send(self)")
        end = src.index("def _handle_mail_send_undo(self)")
        body = src[start:end]
        self.assertIn("result_meta=send_meta", body)
        self.assertIn("recipient_locks=locks", body)

    def test_wrapper_source_forwards_result_meta(self):
        src = Path(server.__file__).read_text()
        start = src.index("def _try_send_for_account(")
        end = src.index("# ── Mail Scanner Handlers")
        body = src[start:end]
        self.assertIn("result_meta: dict | None = None", body)
        self.assertIn("result_meta=result_meta", body)

    def test_scanner_calls_shared_helper_not_the_wrapper(self):
        src = (ROOT / "scanner" / "mail_scanner.py").read_text()
        self.assertIn("from mail_send import try_send_for_account", src)
        self.assertNotIn("_try_send_for_account(", src)

    def test_mcp_still_has_no_send_tool(self):
        tools = (ROOT / "mcp_tools.py").read_text()
        self.assertIn('FORBIDDEN_PATH_MARKERS = ("/mail/send", "/mail/send/undo")', tools)

    def test_undo_does_not_call_try_send(self):
        src = Path(server.__file__).read_text()
        start = src.index("def _handle_mail_send_undo(self)")
        # next def
        nxt = src.index("    def _handle_mail_message_star", start)
        body = src[start:nxt]
        self.assertNotIn("try_send_for_account", body)


@unittest.skipUnless(HAVE_DB and os.environ.get("DB_HOST"), "no database")
class TestHttpSendPath(unittest.TestCase):
    """Real handler, mocked transport. Compose / reply / forward / API key."""

    @classmethod
    def setUpClass(cls):
        db.init_db()
        if db.query_one(
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_name='mail_outgoing' AND column_name='error'"
        ) is None:
            raise unittest.SkipTest("migration 050 not applied")
        cls.tag = uuid.uuid4().hex[:8]
        cls.user = db.query_one(
            """INSERT INTO users (email, display_name, password_hash, password_salt,
                                  is_admin, is_active, role)
               VALUES (%s, 'Send Meta Test', 'x', 'x', TRUE, TRUE, 'admin') RETURNING id""",
            (f"sendmeta-{cls.tag}@example.invalid",))
        cls.acct = db.query_one(
            """INSERT INTO mail_accounts (email, display_name, smtp_from_name, imap_host,
                   password_encrypted, owner_user_id)
               VALUES (%s, 'Meta Acct', 'Meta Brand', 'imap.example.invalid', 'x', %s)
               RETURNING id""",
            (f"meta-acct-{cls.tag}@example.invalid", cls.user["id"]))
        cls.deal = db.query_one(
            """INSERT INTO opportunities (title, responsible_user_id, created_by)
               VALUES (%s, %s, %s) RETURNING id""",
            (f"Send meta job {cls.tag}", cls.user["id"], cls.user["id"]))
        cls.message = db.query_one(
            """INSERT INTO mail_messages (account_id, imap_uid, message_id, subject)
               VALUES (%s, %s, %s, 'original') RETURNING id""",
            (cls.acct["id"], f"uid-{cls.tag}", f"orig-{cls.tag}@example.invalid"))
        cls.session = auth.create_session(cls.user["id"], ip_address="test")
        cls.token = api_tokens.create_api_token(
            f"open-{cls.tag}", cls.user["id"], "full")["token"]

    @classmethod
    def tearDownClass(cls):
        try:
            db.execute("DELETE FROM mail_outgoing WHERE account_id = %s", (cls.acct["id"],))
            db.execute("DELETE FROM mail_messages WHERE account_id = %s", (cls.acct["id"],))
            db.execute("DELETE FROM api_token_audit_log WHERE token_id IN "
                       "(SELECT id FROM api_tokens WHERE name LIKE %s)",
                       (f"%-{cls.tag}",))
            db.execute("DELETE FROM api_tokens WHERE name LIKE %s", (f"%-{cls.tag}",))
            db.execute("DELETE FROM sessions WHERE user_id = %s", (cls.user["id"],))
            db.execute("DELETE FROM opportunities WHERE id = %s", (cls.deal["id"],))
            db.execute("DELETE FROM mail_accounts WHERE id = %s", (cls.acct["id"],))
            db.execute("DELETE FROM users WHERE id = %s", (cls.user["id"],))
        except Exception as exc:  # pragma: no cover
            print(f"cleanup warning: {exc}")

    def _session_send(self, **extra):
        body = {"account_id": self.acct["id"], "to": "someone@example.invalid",
                "subject": "s", "body": "<p>b</p>", "deal_id": self.deal["id"]}
        body.update(extra)
        return mcp_local_api.call_session(self.session, "POST", "/api/v2/mail/send", body)

    def _token_send(self, **extra):
        body = {"account_id": self.acct["id"], "to": "someone@example.invalid",
                "subject": "s", "body": "<p>b</p>"}
        body.update(extra)
        return mcp_local_api.call(self.token, "POST", "/api/v2/mail/send", body)

    def _transport_patches(self):
        return (
            patch.object(mail_send, "try_send_for_account", side_effect=_fake_transport),
            patch.object(server.KanbanHandler, "_create_email_history_event",
                         return_value=True),
        )

    def test_missing_account_id_returns_400_not_500(self):
        p1, p2 = self._transport_patches()
        with p1, p2:
            resp = mcp_local_api.call_session(
                self.session, "POST", "/api/v2/mail/send",
                {"to": "someone@example.invalid", "subject": "s", "body": "<p>b</p>"})
        self.assertEqual(resp.status, 400, resp.text)
        self.assertIn("account_id is required", resp.text)
        self.assertNotIn("result_meta", resp.text)

    def test_empty_account_id_returns_400(self):
        p1, p2 = self._transport_patches()
        with p1, p2:
            resp = self._session_send(account_id="")
        self.assertEqual(resp.status, 400, resp.text)
        self.assertIn("account_id is required", resp.text)

    def test_compose_send_succeeds_through_real_wrapper(self):
        p1, p2 = self._transport_patches()
        with p1 as mocked, p2:
            resp = self._session_send(subject="compose")
            self.assertTrue(mocked.called)
            kwargs = mocked.call_args.kwargs
            self.assertIn("result_meta", kwargs)
            self.assertIsInstance(kwargs["result_meta"], dict)
        self.assertEqual(resp.status, 200, resp.text)
        self.assertTrue(resp.payload.get("ok"))
        self.assertEqual(resp.payload.get("sentAs"),
                         f"meta-acct-{self.tag}@example.invalid")

    def test_reply_send_succeeds_through_real_wrapper(self):
        p1, p2 = self._transport_patches()
        with p1, p2:
            resp = self._session_send(subject="Re: original",
                                      reply_to_message_id=self.message["id"])
        self.assertEqual(resp.status, 200, resp.text)
        self.assertTrue(resp.payload.get("ok"))

    def test_forward_send_succeeds_through_real_wrapper(self):
        # Forward uses the same HTTP handler with reply_to_message_id (the
        # inline/compose UI sets that for both reply and forward).
        p1, p2 = self._transport_patches()
        with p1, p2:
            resp = self._session_send(subject="Fwd: original",
                                      reply_to_message_id=self.message["id"])
        self.assertEqual(resp.status, 200, resp.text)
        self.assertTrue(resp.payload.get("ok"))

    def test_api_key_send_succeeds_through_real_wrapper(self):
        p1, p2 = self._transport_patches()
        with p1 as mocked, p2:
            resp = self._token_send(subject="api-key")
            self.assertTrue(mocked.called)
            self.assertIn("result_meta", mocked.call_args.kwargs)
        self.assertEqual(resp.status, 200, resp.text)
        self.assertTrue(resp.payload.get("ok"))


if __name__ == "__main__":
    unittest.main()
