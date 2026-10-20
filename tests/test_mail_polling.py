#!/usr/bin/env python3
"""Unit tests for manual mail polling + newest-first fetch + reply actor.

Covers, with stubs/mocks (no live IMAP, DB, or Telegram):
  - _fetch_messages returns newest-first so fresh mail lands before backfill.
  - request_manual_poll queues ids (set semantics, rejects garbage) for the
    scanner loop to consume.
  - POST /api/v2/mail/accounts/{id}/sync-now gates + queues (202), 404s
    unknown accounts.
  - Threaded bot replies attribute created_by to the chat's linked user and
    name the ACTOR (not the recipient) in the reply_tagged notification
    message (live case: Ken's reply showed "Claudiu Chereji replied").
  - The mail refresh button calls sync-now (source guard).

Run with: python3 tests/test_mail_polling.py
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

# Local .venv has every dependency (imap_tools included) and db connects
# lazily, so both modules import for real — no stubs needed (same as
# tests/test_mail_link_history.py, which imports server directly).
import scanner.mail_scanner as ms  # noqa: E402
import server as _server  # noqa: E402

ADMIN = {"id": 7, "is_admin": True}


class FakeMsg:
    def __init__(self, uid):
        self.uid = str(uid)
        self.attachments = []
        self.headers = {}
        self.from_values = None
        self.to_values = []
        self.subject = f"msg {uid}"
        self.date = None
        self.text = "body"
        self.html = ""
        self.flags = []


class FakeFolder:
    def set(self, folder):
        pass


class FakeMailbox:
    instances = []

    def __init__(self, host, port=None):
        self.host = host
        FakeMailbox.instances.append(self)
        self.folder = FakeFolder()

    def login(self, user, password):
        pass

    def xoauth2(self, user, token):
        pass

    def fetch(self, criteria=None, mark_seen=False, bulk=False):
        return [FakeMsg(uid) for uid in (101, 102, 103)]

    def logout(self):
        pass


class TestNewestFirstFetch(unittest.TestCase):
    def test_fetch_returns_descending_uid(self):
        from datetime import datetime, timezone
        cfg = {"host": "h", "port": 993, "user": "u", "password": "p",
               "inbox": "INBOX", "account_id": 6}
        with patch.object(ms, "MailBox", FakeMailbox):
            msgs = ms._fetch_messages(cfg, "INBOX",
                                      since=datetime.now(timezone.utc))
        self.assertEqual([m["uid"] for m in msgs], ["103", "102", "101"])


class TestManualPollQueue(unittest.TestCase):
    def setUp(self):
        ms._manual_poll_ids.clear()
        ms._wakeup.clear()

    def tearDown(self):
        ms._manual_poll_ids.clear()
        ms._wakeup.clear()

    def test_queues_and_wakes(self):
        self.assertTrue(ms.request_manual_poll(6))
        self.assertIn(6, ms._manual_poll_ids)
        self.assertTrue(ms._wakeup.is_set())

    def test_accumulates_multiple(self):
        ms.request_manual_poll(6)
        ms.request_manual_poll(7)
        self.assertEqual(ms._manual_poll_ids, {6, 7})

    def test_rejects_garbage(self):
        self.assertFalse(ms.request_manual_poll("nope"))
        self.assertFalse(ms.request_manual_poll(None))
        self.assertEqual(ms._manual_poll_ids, set())


class _Shim:
    class headers:
        @staticmethod
        def get(key, default=None):
            if key == "Authorization":
                return "Bearer tok"
            return default

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    wfile = MagicMock()


_Shim._link_email_to_deal = _server.KanbanHandler._link_email_to_deal


class TestSyncNowEndpoint(unittest.TestCase):
    def _call(self, account_id, user=ADMIN, row=None):
        import json as _json
        responses = []
        calls = []

        def fake_query_one(sql, params=None, *a, **k):
            return row

        mock_db = MagicMock()
        mock_db.query_one.side_effect = fake_query_one
        with patch.object(_server, "_require_auth", lambda h: user), \
            patch.object(_server, "_read_body", lambda h: b"{}"), \
            patch.object(_server, "_mail_account_manageable", lambda u, aid: True), \
            patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"), \
            patch.object(ms, "request_manual_poll", lambda aid: calls.append(aid) or True), \
            patch.object(_server, "mail_scanner", ms):
            _server.KanbanHandler._handle_mail_account_sync_now(_Shim(), account_id)
        return responses, calls

    def test_queues_existing_account(self):
        responses, calls = self._call(6, row={"id": 6})
        self.assertEqual(responses[0][0], 202)
        self.assertEqual(calls, [6])
        self.assertTrue(responses[0][1]["queued"])

    def test_unknown_account_404(self):
        responses, calls = self._call(999, row=None)
        self.assertEqual(responses[0][0], 404)
        self.assertEqual(calls, [])


class TestReplyActorAttribution(unittest.TestCase):
    """Ken (user 7) replies via bot to Claudiu's (user 2) note: the reply and
    its notification must name Ken, never Claudiu."""

    def _call(self):
        import json as _json
        responses = []
        executes = []
        returning = []

        def fake_execute(sql, params=None, *a, **k):
            executes.append((sql, params))
            return None

        def fake_returning(sql, params=None, *a, **k):
            returning.append((sql, params))
            return 5

        mock_db = MagicMock()
        mock_db.execute.side_effect = fake_execute
        mock_db.insert_returning.side_effect = fake_returning
        body = {"content": "What error did you get?", "chatId": 111, "notifyUserList": [2]}
        with patch.object(_server, "_require_auth", lambda h: (_ for _ in ()).throw(AssertionError("session auth must not run for bot Bearer"))), \
            patch.object(_server, "_get_bot_token", lambda: "tok"), \
            patch.object(_server, "_read_body", lambda h: _json.dumps(body).encode()), \
            patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
            patch.object(_server, "get_mapping_by_chat", lambda portal, cid: {"userId": 7}), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"):
            _server.KanbanHandler._handle_history_reply_create(_Shim(), 43, 900)
        return responses, executes, returning

    def test_reply_created_by_linked_user(self):
        responses, executes, returning = self._call()
        self.assertEqual(responses[0][0], 201)
        reply_inserts = [e for e in returning if "INSERT INTO history_replies" in e[0]]
        self.assertEqual(len(reply_inserts), 1)
        _sql, params = reply_inserts[0]
        self.assertEqual(params[3], 7)  # created_by = Ken, not hardcoded 1

    def test_notification_names_actor_not_recipient(self):
        _responses, executes, _returning = self._call()
        notif = [e for e in executes if "'reply_tagged'" in e[0]]
        self.assertEqual(len(notif), 1)
        _sql, params = notif[0]
        # (user_id, ..., actor_user_id, ..., u.id for display name)
        self.assertEqual(params[0], 2)   # recipient: Claudiu
        self.assertEqual(params[2], 7)   # actor: Ken
        self.assertEqual(params[-1], 7)  # display string joins the ACTOR
        tag_rows = [e for e in executes if "history_reply_notify_users" in e[0]]
        self.assertEqual(len(tag_rows), 1)


class TestRefreshButtonWiring(unittest.TestCase):
    def test_refresh_calls_sync_now(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(encoding="utf-8")
        start = js.index('$("#mail-refresh")')
        region = js[start:start + 2000]
        self.assertIn("sync-now", region)
        self.assertIn("loadMailMessagesForModal", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
