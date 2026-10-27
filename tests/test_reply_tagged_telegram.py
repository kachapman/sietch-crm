#!/usr/bin/env python3
"""Unit tests for reply_tagged Telegram delivery + bot mark-read (2026-10-26).

Live gaps:
1. Replies created `reply_tagged` notification rows (drawer worked) but
   notification_dispatcher._format_notification had no reply_tagged branch —
   returned None, so nothing ever reached Telegram.
2. Replying in Telegram never cleared the drawer badge.
   Fix: bot calls POST /api/bot/notification-read after a 200/201 reply post;
   the endpoint marks is_read for the mapped user only and emits
   notification:read over SSE.

Run with: python3 tests/test_reply_tagged_telegram.py (mocked db, no Postgres)
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import notification_dispatcher as _nd
import server as _server

ADMIN = {"id": 7, "is_admin": True}


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


def _reply_notif():
    return {"id": 99, "user_id": 5, "type": "reply_tagged",
            "message": "Ken Chapman replied on Storyboard on Ramada",
            "payload": {"event_id": 49503, "reply_id": 12,
                        "snippet": "What error did you get?"}}


class TestReplyTaggedFormat(unittest.TestCase):
    def test_telegram_format_emits(self):
        text = _nd._format_notification(_reply_notif(), "Storyboard on Ramada",
                                        "Ken Chapman", project_id=130, project_number=None)
        self.assertIsNotNone(text)
        self.assertIn("Ken Chapman", text)
        self.assertIn("What error did you get?", text)
        self.assertIn("Reply to add a note.", text)

    def test_email_format_emits(self):
        subject, body = _nd._format_email_notification(
            _reply_notif(), "Storyboard on Ramada", "Ken Chapman")
        self.assertIsNotNone(subject)
        self.assertIsNotNone(body)
        self.assertIn("Ken Chapman", subject)


class TestBotMarkReadEndpoint(unittest.TestCase):
    def _call(self, body_dict, mapping):
        import json as _json
        responses = []
        writes = []
        mock_db = MagicMock()
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))

        h = Shim()
        h.headers = {"Authorization": "Bearer sekret"}
        h.path = "/api/bot/notification-read"
        with patch.object(_server, "_get_bot_token", lambda: "sekret"), \
            patch.object(_server, "_read_body", lambda hh: _json.dumps(body_dict).encode()), \
            patch.object(_server, "_json_response", lambda hh, s, b: responses.append((s, b))), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"), \
            patch.object(_server, "event_bus"):
            import crm_bot_store as _store
            with patch.object(_store, "get_mapping_by_chat", return_value=mapping):
                _server.KanbanHandler._handle_bot_api_post(h, "POST")
        assert responses, "no response"
        return responses[0], writes

    def test_unmapped_chat_403_no_write(self):
        (status, body), writes = self._call(
            {"notificationId": 99, "chatId": 111}, mapping=None)
        self.assertEqual(status, 403)
        self.assertEqual([w for w in writes if "UPDATE notifications" in w[0]], [])

    def test_mapped_chat_marks_only_own_row(self):
        (status, body), writes = self._call(
            {"notificationId": 99, "chatId": 111}, mapping={"userId": 5})
        self.assertEqual(status, 200)
        upd = [w for w in writes if "UPDATE notifications SET is_read" in w[0]]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0][1], (99, 5))

    def test_missing_fields_400(self):
        (status, body), writes = self._call({"notificationId": 99}, mapping={"userId": 5})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=1)
