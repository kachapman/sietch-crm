#!/usr/bin/env python3
"""Unit tests for partial project updates (2026-10-22).

The server PUT only writes keys present in the payload — a PATCH-style
partial update. This pins that contract: omitting responsibleUserId must
leave ownership untouched (the frontend theft bug was client-side), and
including it must write.

Run with: python3 tests/test_project_update_partial.py (mocked db, no Postgres)
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}
OLD = {"stage_id": 3, "bid_value": 100.0, "contact_id": 4,
       "responsible_user_id": 5, "expected_close_date": None,
       "is_private": False, "description": "d"}


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


def _put(payload):
    import json as _json
    responses = []
    writes = []
    mock_db = MagicMock()
    mock_db.query_one.side_effect = lambda sql, params=None, *a, **k: dict(OLD) if "SELECT stage_id" in sql else None
    mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
    with patch.object(_server, "_require_auth", lambda h: ADMIN), \
        patch.object(_server, "_read_body", lambda h: _json.dumps(payload).encode()), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"), \
        patch.object(_server, "event_bus"):
        _server.KanbanHandler._handle_project_update(Shim(), 42)
    assert responses, "no response"
    assert responses[0][0] == 200, responses[0]
    updates = [w for w in writes if w[0].startswith("UPDATE opportunities SET")]
    assert len(updates) == 1
    return updates[0]


class TestPartialUpdate(unittest.TestCase):
    def test_absent_responsible_left_untouched(self):
        sql, params = _put({"title": "New name"})
        self.assertNotIn("responsible_user_id", sql)

    def test_present_responsible_written(self):
        sql, params = _put({"title": "New name", "responsibleUserId": 9})
        self.assertIn("responsible_user_id = %s", sql)
        self.assertIn(9, params)

    def test_absent_fields_left_untouched(self):
        sql, params = _put({"stageId": 4})
        self.assertNotIn("bid_value", sql)
        self.assertNotIn("contact_id", sql)
        self.assertIn("stage_id = %s", sql)


if __name__ == "__main__":
    unittest.main(verbosity=1)
