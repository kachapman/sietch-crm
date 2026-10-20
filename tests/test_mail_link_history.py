#!/usr/bin/env python3
"""Unit tests for email→deal linking creating project history.

Regression: linking an email whose mail_deal_links row already existed
returned 200 "Already linked" WITHOUT creating the history event, so
nothing ever appeared in History & Notes (live case: message 1648 linked
to opp 43, both POSTs 200, zero timeline events).

Also locks EMAIL_HISTORY_CATEGORY_ID to the real "Email" category (was 16,
"Customer Update" — a stale value from the taxonomy reseed that mislabelled
linked emails and leaked them into the Telegram customer-updates feed).

Follows the harness style of tests/test_email_audit_345.py: stdlib
unittest + MagicMock, no live DB (server.db is patched out).

Run with: python3 tests/test_mail_link_history.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}

FULL_MSG = {
    "subject": "A00007611973",
    "from_addr": " adjuster@example.com",
    "to_addr": "a@example.com",
    "cc_addr": "",
    "body_html": "<p>estimate</p>",
    "body_text": "estimate",
    "date_received": "2026-10-20 06:08:00",
}


class _FakeHeaders:
    def get(self, key, default=None):
        return default


class _FakeRFile:
    def read(self, n):
        return b""


class Shim:
    headers = _FakeHeaders()
    rfile = _FakeRFile()

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    wfile = MagicMock()


# _handle_mail_link dispatches to sibling handler methods via self; the Shim
# is not a KanbanHandler, so bind the real (db-mocked) implementations.
Shim._link_email_to_deal = _server.KanbanHandler._link_email_to_deal
Shim._create_email_history_event = _server.KanbanHandler._create_email_history_event


def _call_link(message_id, body_dict, query_one_results, user=ADMIN):
    """Invoke KanbanHandler._handle_mail_link with a stubbed db layer.

    query_one_results: values returned by successive db.query_one() calls.
    Returns (responses, executes) where executes is [(sql, params), ...].
    """
    responses = []
    executes = []
    calls = {"i": 0}

    def fake_query_one(sql, params=None, *a, **k):
        if calls["i"] >= len(query_one_results):
            return None
        r = query_one_results[calls["i"]]
        calls["i"] += 1
        return r

    def fake_execute(sql, params=None, *a, **k):
        executes.append((sql, params))
        return None

    def fake_query_dicts(sql, params=None, *a, **k):
        return []

    import json as _json

    mock_db = MagicMock()
    mock_db.query_one.side_effect = fake_query_one
    mock_db.execute.side_effect = fake_execute
    mock_db.query_dicts.side_effect = fake_query_dicts

    with patch.object(_server, "_require_auth", lambda h: user), \
        patch.object(_server, "_read_body", lambda h: _json.dumps(body_dict).encode()), \
        patch.object(_server, "_mail_account_accessible", lambda u, aid: True), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_mail_link(Shim(), message_id)
    return responses, executes


def _history_inserts(executes):
    return [e for e in executes if "INSERT INTO history_events" in e[0]]


class TestAlreadyLinkedBackfillsHistory(unittest.TestCase):
    """The live 1648 case: link row exists, no timeline event."""

    def test_already_linked_without_event_creates_it(self):
        responses, executes = _call_link(
            1648, {"opportunityId": 43},
            [
                {"account_id": 1},   # accessible
                {"id": 43},          # opp exists
                {"id": 191},         # link exists -> not ok
                {"id": 191},         # existing re-check
                None,                # no history event yet
                dict(FULL_MSG),      # message row for snapshot
            ],
        )
        self.assertEqual(len(responses), 1)
        status, body = responses[0]
        self.assertEqual(status, 200)
        self.assertFalse(body["linked"])
        self.assertTrue(body["historyCreated"])
        inserts = _history_inserts(executes)
        self.assertEqual(len(inserts), 1)
        _sql, params = inserts[0]
        self.assertEqual(params[0], 43)  # opportunity_id
        self.assertEqual(params[1], 13)  # Email category, not Customer Update
        self.assertIn('"message_id": 1648', params[2])

    def test_already_linked_with_event_does_not_duplicate(self):
        responses, executes = _call_link(
            1648, {"opportunityId": 43},
            [
                {"account_id": 1},
                {"id": 43},
                {"id": 191},
                {"id": 191},
                {"id": 999},  # event already present
            ],
        )
        status, body = responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(body["historyCreated"])
        self.assertEqual(_history_inserts(executes), [])


class TestFreshLinkCreatesHistory(unittest.TestCase):
    def test_fresh_link_creates_link_row_and_event(self):
        responses, executes = _call_link(
            1649, {"opportunityId": 43},
            [
                {"account_id": 1},
                {"id": 43},
                None,  # no existing link -> INSERT
                None,  # no existing event
                dict(FULL_MSG),
            ],
        )
        status, body = responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(body["linked"])
        self.assertTrue(body["historyCreated"])
        self.assertTrue(any("INSERT INTO mail_deal_links" in e[0] for e in executes))
        self.assertEqual(len(_history_inserts(executes)), 1)


class TestEmailCategoryConstant(unittest.TestCase):
    def test_email_category_is_real_email(self):
        # Was 16 ("Customer Update") — a stale pre-reseed value.
        self.assertEqual(_server.EMAIL_HISTORY_CATEGORY_ID, 13)


if __name__ == "__main__":
    unittest.main(verbosity=1)
