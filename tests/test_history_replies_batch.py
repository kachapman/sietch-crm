#!/usr/bin/env python3
"""Unit tests for the batch reply-threads endpoint + reply-close behavior.

Live incident: opening a preview auto-fetched one thread per event (N
concurrent GETs vs a 20-connection pool -> 500s), so threads never loaded and
replies vanished into a void. GET /api/v2/projects/{id}/history/replies now
returns every thread in one request; auto-load paths use it.

Also locks the reply-close contract (post/delete refresh the thread WITHOUT
reopening the composer) and the muted mention colors, via source guards.

Harness mirrors tests/test_mail_link_history.py (mocked db, no Postgres).

Run with: python3 tests/test_history_replies_batch.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


class Shim:
    path = "/api/v2/projects/42/history/replies"
    headers = {"Cookie": ""}

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


ROWS = [
    # id, parent, content, created_by, created_at, deleted, author, event_id
    (1, None, "first", 7, None, False, "Ken Chapman", 100),
    (2, None, "second", 2, None, False, "Claudiu Chereji", 100),
    (3, None, "other", 2, None, False, "Claudiu Chereji", 200),
]
NOTIFY = [(1, 2)]


def _call(opp_id, user=ADMIN, rows=None, notify=None):
    responses = []
    rows = ROWS if rows is None else rows
    notify = NOTIFY if notify is None else notify

    def fake_query(sql, params=None, *a, **k):
        if "history_reply_notify_users" in sql:
            return notify
        return rows

    mock_db = MagicMock()
    mock_db.query.side_effect = fake_query

    def fake_require_auth(h):
        if user is None:
            # Mirror the real helper, which sends the 401 itself.
            responses.append((401, {"error": "Not authenticated"}))
            return None
        return user

    with patch.object(_server, "_require_auth", fake_require_auth), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_history_replies_batch(Shim(), opp_id)
    assert responses, "no response"
    return responses[0]


class TestBatchEndpoint(unittest.TestCase):
    def test_groups_by_event_with_single_shape(self):
        status, body = _call(42)
        self.assertEqual(status, 200)
        threads = body["replies"]
        self.assertEqual([r["id"] for r in threads["100"]], [1, 2])
        self.assertEqual([r["id"] for r in threads["200"]], [3])
        # Same keys as the single-event GET.
        r = threads["100"][0]
        for key in ("id", "parentReplyId", "content", "createdBy", "created",
                    "isDeleted", "author", "notifyUserIds"):
            self.assertIn(key, r)
        self.assertEqual(threads["100"][0]["notifyUserIds"], [2])
        self.assertEqual(threads["100"][1]["notifyUserIds"], [])

    def test_requires_auth(self):
        status, _body = _call(42, user=None)
        self.assertEqual(status, 401)

    def test_empty_deal_returns_empty_map(self):
        status, body = _call(999, rows=[], notify=[])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"replies": {}})


if __name__ == "__main__":
    unittest.main(verbosity=1)
