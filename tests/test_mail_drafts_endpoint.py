#!/usr/bin/env python3
"""Unit tests for the drafts endpoint's cross-account hint data.

The Drafts folder is account-scoped with no all-accounts view, so an empty
list is ambiguous (no drafts vs wrong tab). The endpoint returns
other_accounts (draft counts grouped by visible account) so the UI can point
at the tab that actually holds them.

Harness mirrors tests/test_mail_link_history.py (mocked db, no Postgres).

Run with: python3 tests/test_mail_drafts_endpoint.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


class Shim:
    path = "/api/v2/mail/drafts"
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


def _call(path, drafts_rows, group_rows, user=ADMIN):
    responses = []
    seen_sql = []

    def fake_query_dicts(sql, params=None, *a, **k):
        seen_sql.append(sql)
        if "GROUP BY" in sql:
            return group_rows
        return drafts_rows

    mock_db = MagicMock()
    mock_db.query_dicts.side_effect = fake_query_dicts
    shim = Shim()
    shim.path = path
    with patch.object(_server, "_require_auth", lambda h: user), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_mail_drafts_get(shim)
    assert responses, "no response"
    return responses[0], seen_sql


class TestOtherAccounts(unittest.TestCase):
    DRAFTS = [{"id": 12, "subject": "x", "account_id": 5}]
    GROUPS = [
        {"id": 5, "email": "aplus@x.com", "crm": False, "n": 9},
        {"id": 2, "email": "crm@x.com", "crm": True, "n": 0},
    ]

    def test_returns_rows_plus_grouped_counts(self):
        (status, body), _sql = _call("/api/v2/mail/drafts?account_id=5",
                                          self.DRAFTS, self.GROUPS)
        self.assertEqual(status, 200)
        self.assertEqual(body["drafts"], self.DRAFTS)
        by_id = {g["id"]: g for g in body["other_accounts"]}
        self.assertEqual(by_id[5]["n"], 9)
        self.assertEqual(by_id[2]["email"], "crm@x.com")
        self.assertTrue(by_id[2]["crm"])

    def test_account_filter_applied_to_rows(self):
        (_status, _body), seen = _call("/api/v2/mail/drafts?account_id=5",
                                            self.DRAFTS, self.GROUPS)
        row_sql = [s for s in seen if "GROUP BY" not in s][0]
        self.assertIn("account_id = %s", row_sql)

    def test_crm_tab_filters_to_crm_accounts(self):
        (_status, _body), seen = _call("/api/v2/mail/drafts?account_id=crm",
                                            [], self.GROUPS)
        row_sql = [s for s in seen if "GROUP BY" not in s][0]
        self.assertIn("is_crm_mail", row_sql)


if __name__ == "__main__":
    unittest.main(verbosity=1)
