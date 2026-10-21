#!/usr/bin/env python3
"""Unit tests for UTC timestamp serialization (history +4h EDT bug).

Live bug 2026-10-21: droplet Postgres runs UTC; history `created_at` is
`timestamp without time zone`. Inline `r[n].isoformat()` emitted naive ISO
("2026-10-21T20:01:21"), which browsers parse as LOCAL time — Ken's 4:01 PM
EDT note rendered "8:01 PM EDT". Dev-server audit passed only because dev PG
runs in local time (naive == wall clock by coincidence).

Fix: every DB datetime routes through `_ts()` (naive assumed UTC, offset
attached); frontend `parseFeedDate` treats naive "YYYY-MM-DD HH:mm:ss" as UTC.

Run with: python3 tests/test_timestamp_utc.py
"""
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


class TestTsHelper(unittest.TestCase):
    def test_naive_assumed_utc(self):
        out = _server._ts(datetime(2026, 10, 21, 20, 1, 21))
        self.assertIn("+00:00", out)
        self.assertTrue(out.startswith("2026-10-21T20:01:21"))

    def test_aware_converted_to_utc(self):
        from datetime import timedelta
        edt = timezone(timedelta(hours=-4))
        out = _server._ts(datetime(2026, 10, 21, 16, 1, 21, tzinfo=edt))
        self.assertIn("+00:00", out)
        self.assertTrue(out.startswith("2026-10-21T20:01:21"))

    def test_none_and_plain_values(self):
        self.assertIsNone(_server._ts(None))
        self.assertEqual(_server._ts("x"), "x")


class TestHistoryEmitsOffset(unittest.TestCase):
    def test_history_get_created_has_offset(self):
        import json as _json
        responses = []
        naive_utc = datetime(2026, 10, 21, 20, 1, 21, 790132)  # droplet PG value
        rows = [(49616, 1, "Note", "t", "c", 5, naive_utc, None, "Ken Chapman")]

        class Shim:
            headers = {"Cookie": ""}
        mock_db = MagicMock()
        mock_db.query.side_effect = [rows, []]
        with patch.object(_server, "_require_auth", lambda h: ADMIN), \
            patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"):
            _server.KanbanHandler._handle_project_history_get(Shim(), 46, {"count": ["10"], "startIndex": ["0"]})
        self.assertEqual(responses[0][0], 200)
        created = responses[0][1][0]["created"]
        # Must carry an explicit offset so browsers never parse as local.
        self.assertRegex(created, r"\+00:00$")
        # And it must still denote 20:01 UTC (not shifted).
        parsed = datetime.fromisoformat(created)
        self.assertEqual((parsed.hour, parsed.minute), (20, 1))
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_no_bare_isoformat_on_db_values(self):
        import re
        src = open("/home/chapman-server/Projects/new-crm/server.py", encoding="utf-8").read()
        bare = [ln for ln in src.splitlines()
                if re.search(r'\.isoformat\(\) if ', ln)]
        self.assertEqual(bare, [], f"bare .isoformat() emitters remain: {bare[:5]}")

    def test_frontend_parses_naive_as_utc(self):
        js = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        at = js.index("function parseFeedDate(value)")
        region = js[at:at + 1200]
        self.assertIn("T${m[2]}Z", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
