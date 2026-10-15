"""Unit tests for timesheet_overdue_checker.scan_once().

Mock the db module so the test doesn't touch a real database. Verifies:
  1. An 11h open shift produces an INSERT for threshold=10.
  2. A 15h open shift produces INSERTs for BOTH threshold=10 AND threshold=14.
  3. The INSERT uses ON CONFLICT DO NOTHING (idempotency).
  4. The headline ("message") and snippet ("payload.snippet") are
     different — headline = time, snippet = question.
  5. A 5h open shift (under both thresholds) produces no INSERTs.
  6. The headline includes the project title when opportunity_id is set.
  7. Calling scan_once() twice with the same data does NOT error (the
     ON CONFLICT clause is the safety net, and we just verify the SQL
     has it — actual de-dup is enforced by the unique index in PG).
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Make the project root importable so we can import the checker module.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class _FakeRow(dict):
    """Pass through dict with attribute access for db.query_dicts output."""
    def __getattr__(self, k):
        return self[k]


class TestScanOnce(unittest.TestCase):
    def _setup_db(self, open_shifts):
        """Wire up the db module mocks so query_dicts returns open_shifts
        and execute captures every INSERT for later inspection."""
        inserted = []

        def fake_query_dicts(sql, params=()):
            sql_norm = " ".join(sql.split())
            if "FROM time_entries te" in sql_norm and "clock_out IS NULL" in sql_norm:
                return [_FakeRow(r) for r in open_shifts]
            return []

        def fake_execute(sql, params=()):
            sql_norm = " ".join(sql.split())
            if "INSERT INTO notifications" in sql_norm:
                inserted.append({"sql": sql_norm, "params": params})
            # RETURNING queries (admin close) would go here if tested.
            return None

        return fake_query_dicts, fake_execute, inserted

    def test_11h_shift_produces_10h_insert(self):
        from timesheet_overdue_checker import scan_once
        rows = [{
            "id": 1, "user_id": 100, "clock_in": "2026-10-15T00:00:00+00:00",
            "opportunity_id": None, "user_name": "Alice",
            "opportunity_title": None,
        }]
        # Patch time.time so the elapsed calculation is deterministic:
        # if we pretend now is 2026-10-15T11:30:00+00:00, then 11.5h
        # have elapsed for entry id=1.
        fake_now = 1763136600.0  # 2025-11-13 ish; just need a fixed delta
        clock_in_ts = fake_now - (11.5 * 3600)
        rows[0]["clock_in"] = _FakeRow(rows[0])["clock_in"]
        # Convert clock_in to a real datetime so scan_once's `hasattr(ts, 'timestamp')` works.
        from datetime import datetime, timezone
        rows[0]["clock_in"] = datetime.fromtimestamp(clock_in_ts, tz=timezone.utc)

        with patch("timesheet_overdue_checker.db") as mdb, \
             patch("timesheet_overdue_checker.time") as mtime:
            mdb.query_dicts.side_effect = lambda sql, params=(): [_FakeRow(r) for r in rows]
            mdb.execute.side_effect = lambda sql, params=(): None
            mtime.time.return_value = fake_now
            summary = scan_once()

        # 1 INSERT for threshold=10; threshold=14 should NOT fire (only 11.5h).
        inserts = [c for c in mdb.execute.call_args_list if "INSERT INTO notifications" in c.args[0]]
        self.assertEqual(len(inserts), 1, f"expected 1 insert, got {len(inserts)}: {inserts}")
        sql = inserts[0].args[0]
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        params = inserts[0].args[1]
        # params = (user_id, opportunity_id, headline, entry_id, threshold, elapsed, snippet)
        self.assertEqual(params[0], 100)  # user_id
        self.assertEqual(params[3], "1")  # entry_id (as string for JSONB)
        self.assertEqual(params[4], 10)  # threshold
        self.assertEqual(params[6], "Did you forget to log out?")  # snippet
        # The headline should NOT contain the snippet question
        self.assertNotIn("Did you forget", params[2])
        self.assertIn("Clocked in for", params[2])  # headline = time

    def test_15h_shift_produces_both_inserts(self):
        from timesheet_overdue_checker import scan_once
        from datetime import datetime, timezone
        fake_now = 1763136600.0
        clock_in_ts = fake_now - (15.5 * 3600)
        rows = [{
            "id": 7, "user_id": 200, "clock_in": datetime.fromtimestamp(clock_in_ts, tz=timezone.utc),
            "opportunity_id": None, "user_name": "Bob",
            "opportunity_title": None,
        }]

        with patch("timesheet_overdue_checker.db") as mdb, \
             patch("timesheet_overdue_checker.time") as mtime:
            mdb.query_dicts.side_effect = lambda sql, params=(): [_FakeRow(r) for r in rows]
            mdb.execute.side_effect = lambda sql, params=(): None
            mtime.time.return_value = fake_now
            scan_once()

        inserts = [c for c in mdb.execute.call_args_list if "INSERT INTO notifications" in c.args[0]]
        self.assertEqual(len(inserts), 2, f"expected 2 inserts (10h + 14h), got {len(inserts)}")
        thresholds = sorted(c.args[1][4] for c in inserts)
        self.assertEqual(thresholds, [10, 14])
        snippets = sorted(c.args[1][6] for c in inserts)
        self.assertEqual(snippets, [
            "Did you forget to log out?",
            "Please verify your timesheet.",
        ])

    def test_5h_shift_produces_no_inserts(self):
        from timesheet_overdue_checker import scan_once
        from datetime import datetime, timezone
        fake_now = 1763136600.0
        clock_in_ts = fake_now - (5 * 3600)
        rows = [{
            "id": 3, "user_id": 300, "clock_in": datetime.fromtimestamp(clock_in_ts, tz=timezone.utc),
            "opportunity_id": None, "user_name": "Carol",
            "opportunity_title": None,
        }]

        with patch("timesheet_overdue_checker.db") as mdb, \
             patch("timesheet_overdue_checker.time") as mtime:
            mdb.query_dicts.side_effect = lambda sql, params=(): [_FakeRow(r) for r in rows]
            mdb.execute.side_effect = lambda sql, params=(): None
            mtime.time.return_value = fake_now
            summary = scan_once()

        inserts = [c for c in mdb.execute.call_args_list if "INSERT INTO notifications" in c.args[0]]
        self.assertEqual(len(inserts), 0, f"5h shift should produce no inserts, got {len(inserts)}")
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["inserted_10h"], 0)
        self.assertEqual(summary["inserted_14h"], 0)

    def test_11h_shift_with_project_includes_title_in_headline(self):
        from timesheet_overdue_checker import scan_once
        from datetime import datetime, timezone
        fake_now = 1763136600.0
        clock_in_ts = fake_now - (11 * 3600)
        rows = [{
            "id": 9, "user_id": 400, "clock_in": datetime.fromtimestamp(clock_in_ts, tz=timezone.utc),
            "opportunity_id": 42, "user_name": "Dan",
            "opportunity_title": "Roof repair — Vance St",
        }]

        with patch("timesheet_overdue_checker.db") as mdb, \
             patch("timesheet_overdue_checker.time") as mtime:
            mdb.query_dicts.side_effect = lambda sql, params=(): [_FakeRow(r) for r in rows]
            mdb.execute.side_effect = lambda sql, params=(): None
            mtime.time.return_value = fake_now
            scan_once()

        inserts = [c for c in mdb.execute.call_args_list if "INSERT INTO notifications" in c.args[0]]
        self.assertEqual(len(inserts), 1)
        headline = inserts[0].args[1][2]  # message column
        self.assertIn("Roof repair", headline)
        self.assertIn("Clocked in for", headline)

    def test_idempotent_sql_has_on_conflict(self):
        """Verify the actual SQL string contains ON CONFLICT DO NOTHING
        so the unique index can do its job on retries."""
        from timesheet_overdue_checker import scan_once
        from datetime import datetime, timezone
        fake_now = 1763136600.0
        clock_in_ts = fake_now - (11 * 3600)
        rows = [{
            "id": 11, "user_id": 500, "clock_in": datetime.fromtimestamp(clock_in_ts, tz=timezone.utc),
            "opportunity_id": None, "user_name": "Eve",
            "opportunity_title": None,
        }]

        with patch("timesheet_overdue_checker.db") as mdb, \
             patch("timesheet_overdue_checker.time") as mtime:
            mdb.query_dicts.side_effect = lambda sql, params=(): [_FakeRow(r) for r in rows]
            mdb.execute.side_effect = lambda sql, params=(): None
            mtime.time.return_value = fake_now
            # Run twice — the second call must not error and must re-issue
            # the same ON CONFLICT DO NOTHING INSERT.
            scan_once()
            scan_once()

        inserts = [c for c in mdb.execute.call_args_list if "INSERT INTO notifications" in c.args[0]]
        # 1 row × 1 threshold × 2 calls = 2 inserts issued; the unique
        # index would make the second a no-op at the DB level.
        self.assertEqual(len(inserts), 2)
        for c in inserts:
            self.assertIn("ON CONFLICT DO NOTHING", c.args[0])


if __name__ == "__main__":
    unittest.main()
