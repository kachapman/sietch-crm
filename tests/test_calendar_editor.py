"""Unit tests for local calendar series builder + attendee serialization.

calendar_sync.build_local_series_rows powers create/update-with-series in
server.py. The db layer is stubbed (builder is pure) so no PG is needed.
Server-handler wiring is covered by source-level guards below.

Run with: python3 tests/test_calendar_editor.py
"""

import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


class FakeDb:
    def execute(self, sql, params=()):
        pass

    def query_dicts(self, sql, params=()):
        return []

    def query_one(self, sql, params=()):
        return None


sys.modules.setdefault("db", FakeDb())

import calendar_sync  # noqa: E402
from calendar_sync import (  # noqa: E402
    build_local_series_rows,
    parse_attendees_json,
    serialize_attendees,
)


def _fields(**kw):
    base = {
        "uid": "local-1-123",
        "summary": "Test",
        "description": "",
        "location": "",
        "dtstart": datetime(2026, 10, 5, 9, 0),
        "dtend": datetime(2026, 10, 5, 10, 0),
        "all_day": False,
        "rrule": "",
        "color": "",
        "status": "CONFIRMED",
        "reminder_minutes": None,
        "transparency": "",
        "url": "",
        "attendees_list": [],
        "timezone": "",
    }
    base.update(kw)
    return base


class TestBuildLocalSeriesRows(unittest.TestCase):
    def test_single_row_without_rrule(self):
        rows = build_local_series_rows(_fields())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uid"], "local-1-123")
        self.assertFalse(rows[0]["is_occurrence"])
        self.assertIsNone(rows[0]["recurrence_id"])
        self.assertEqual(rows[0]["dtstart"], datetime(2026, 10, 5, 9, 0))

    def test_recurring_expands_to_occurrences(self):
        rows = build_local_series_rows(_fields(rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=3"))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["is_occurrence"] for r in rows))
        self.assertTrue(all(r["recurrence_id"] == "local-1-123" for r in rows))
        self.assertTrue(all(r["uid"].startswith("local-1-123#") for r in rows))
        self.assertTrue(all(r["rrule"] == "FREQ=WEEKLY;BYDAY=MO;COUNT=3" for r in rows))
        # Bare master row is never stored (it would double-show day one).
        self.assertNotIn("local-1-123", [r["uid"] for r in rows])

    def test_new_fields_carried(self):
        rows = build_local_series_rows(_fields(
            transparency="TRANSPARENT", url="https://x.example",
            attendees_list=[{"name": "Jane", "email": "j@x.com"}],
            timezone="America/Chicago", reminder_minutes=30,
        ))
        self.assertEqual(rows[0]["transparency"], "TRANSPARENT")
        self.assertEqual(rows[0]["url"], "https://x.example")
        self.assertEqual(rows[0]["timezone"], "America/Chicago")
        self.assertEqual(rows[0]["reminder_minutes"], 30)
        att = parse_attendees_json(rows[0]["attendees"])
        self.assertEqual(att[0]["email"], "j@x.com")


class TestAttendees(unittest.TestCase):
    def test_list_round_trip(self):
        raw = serialize_attendees([{"name": "Jane", "email": "j@x.com", "partstat": "", "label": "Jane"}])
        self.assertEqual(parse_attendees_json(raw)[0]["name"], "Jane")

    def test_free_text_lines(self):
        raw = serialize_attendees("Jane\nbob@x.com")
        parsed = parse_attendees_json(raw)
        self.assertEqual(len(parsed), 2)

    def test_empty_is_none(self):
        self.assertIsNone(serialize_attendees([]))
        self.assertIsNone(serialize_attendees(""))
        self.assertIsNone(serialize_attendees(None))
        self.assertEqual(parse_attendees_json(None), [])
        self.assertEqual(parse_attendees_json(""), [])


class TestServerWiring(unittest.TestCase):
    """Source-level guards: server.py must expose the Phase 4 surface.

    These need a live socket/admin session end-to-end; the guards pin the
    wiring so refactors fail loudly instead of silently dropping scope.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (HERE.parent / "server.py").read_text(encoding="utf-8")

    def test_update_supports_series_scope(self):
        self.assertIn('edit_scope', self.src)
        self.assertIn('build_local_series_rows', self.src)

    def test_delete_supports_series_scope(self):
        app = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn("?scope=series", app)
        self.assertIn('qs.get("scope")', self.src)

    def test_new_columns_in_events_select(self):
        for col in ("transparency", "ce.url", "ce.attendees", "ce.timezone"):
            self.assertIn(col, self.src)

    def test_single_edit_detaches_occurrence(self):
        self.assertIn("recurrence_id = NULL", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
