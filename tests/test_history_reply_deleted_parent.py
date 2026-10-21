#!/usr/bin/env python3
"""Unit tests for replying to a deleted parent note.

Live case: Claudiu replied to Rebeca's tag (notification event 49503), but
the note had been deleted — the INSERT died on the FK constraint and the bot
reported "Server error". The handler must 404 before inserting so every
client (bot, dashboard, project page) reports "deleted".

Harness mirrors tests/test_mail_link_history.py (mocked db, no Postgres).

Run with: python3 tests/test_history_reply_deleted_parent.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


class Shim:
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


def _call(opp_id, event_id, body_dict, parent_row, user=ADMIN):
    import json as _json
    responses = []
    writes = []

    def fake_query_one(sql, params=None, *a, **k):
        if "FROM history_events WHERE id" in sql:
            return parent_row
        return None

    def fake_execute(sql, params=None, *a, **k):
        writes.append((sql, params))
        return None

    def fake_returning(sql, params=None, *a, **k):
        writes.append((sql, params))
        return 9

    mock_db = MagicMock()
    mock_db.query_one.side_effect = fake_query_one
    mock_db.execute.side_effect = fake_execute
    mock_db.insert_returning.side_effect = fake_returning
    with patch.object(_server, "_require_auth", lambda h: user), \
        patch.object(_server, "_read_body", lambda h: _json.dumps(body_dict).encode()), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_history_reply_create(Shim(), opp_id, event_id)
    assert responses, "no response"
    return responses[0], writes


class TestDeletedParent(unittest.TestCase):
    def test_missing_event_returns_404_without_writes(self):
        (status, body), writes = _call(
            130, 49503, {"content": "What error did you get?"}, parent_row=None)
        self.assertEqual(status, 404)
        self.assertIn("deleted", body["error"].lower())
        self.assertEqual(writes, [])

    def test_existing_event_proceeds(self):
        (status, body), writes = _call(
            130, 900, {"content": "What error did you get?"}, parent_row={"id": 900})
        self.assertEqual(status, 201)
        self.assertTrue(any("INSERT INTO history_replies" in sql for sql, _ in writes))


if __name__ == "__main__":
    unittest.main(verbosity=1)
