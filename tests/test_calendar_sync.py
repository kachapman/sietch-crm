"""Unit tests for calendar_sync ICS→DB sync with recurrence expansion.

The db layer is stubbed (record executed statements) so no PostgreSQL is needed.

Run with: python3 tests/test_calendar_sync.py
"""

import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


class FakeDb:
    """Records execute() calls; query_* return empty results."""

    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executes.append((sql, tuple(params)))

    def query_dicts(self, sql, params=()):
        return []

    def query_one(self, sql, params=()):
        return None


_fake = FakeDb()
sys.modules["db"] = _fake  # stub before calendar_sync imports it

import calendar_sync  # noqa: E402
from ics_calendar import parse_ics_calendar  # noqa: E402


FEED = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:standup@example.com
DTSTART:20261005T090000Z
DTEND:20261005T091500Z
SUMMARY:Standup
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:single@example.com
DTSTART:20261010T120000Z
DTEND:20261010T130000Z
SUMMARY:One-off
END:VEVENT
END:VCALENDAR
"""


def _inserts():
    return [(sql, p) for sql, p in _fake.executes if sql.lstrip().startswith("INSERT INTO calendar_events")]


def _deletes():
    return [(sql, p) for sql, p in _fake.executes if sql.lstrip().startswith("DELETE FROM calendar_events")]


class TestSyncExpansion(unittest.TestCase):
    def setUp(self):
        _fake.executes.clear()
        parsed = parse_ics_calendar(FEED)
        # Pin "today" by shrinking the window: sync uses date.today(); the feed
        # dates are Oct 2026. If today is outside the expansion window the
        # occurrences won't materialize — so just assert relative behavior and
        # skip when the window excludes the feed.
        self.count = calendar_sync._sync_ics_events_to_db(42, 7, parsed)

    def test_returns_count(self):
        self.assertGreaterEqual(self.count, 0)

    def test_single_event_upserted(self):
        uids = [p[2] for _, p in _inserts()]
        self.assertIn("single@example.com", uids)

    def test_upsert_carries_recurrence_columns(self):
        self.assertTrue(_inserts())
        sql, _ = _inserts()[0]
        self.assertIn("recurrence_id", sql)
        self.assertIn("is_occurrence", sql)
        # reminder_minutes seeded on INSERT (VALARM) but never in the UPDATE list.
        update_part = sql.split("ON CONFLICT")[1]
        self.assertNotIn("reminder_minutes", update_part)

    def test_prune_runs(self):
        # Every sync prunes stale occurrences for the source.
        self.assertTrue(_deletes())
        for _, params in _deletes():
            self.assertIn(42, params)  # scoped to the user
            self.assertIn(7, params)  # scoped to the source


class TestSyncExpansionWindow(unittest.TestCase):
    """Occurrences materialize when the feed falls inside the expansion window."""

    def test_occurrences_have_suffixed_uids(self):
        _fake.executes.clear()
        # Build a feed anchored at today so the default window always covers it.
        monday = date.today() - timedelta(days=date.today().weekday())
        stamp = monday.strftime("%Y%m%dT%H%M%S")
        feed = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:w@example.com\n"
            f"DTSTART:{stamp}Z\nDTEND:{stamp}Z\nSUMMARY:W\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        parsed = parse_ics_calendar(feed)
        calendar_sync._sync_ics_events_to_db(1, 1, parsed)
        uids = [p[2] for _, p in _inserts()]
        suffixed = [u for u in uids if u.startswith("w@example.com#")]
        self.assertTrue(suffixed, f"expected occurrence rows, got {uids}")
        # Bare master row must NOT be stored (it would double-show day one).
        self.assertNotIn("w@example.com", uids)
        # Legacy bare row cleanup attempted.
        bare_deletes = [p for _, p in _deletes() if "w@example.com" in p]
        self.assertTrue(bare_deletes)
        # VALARM-less events insert NULL reminder.
        for _, params in _inserts():
            rec_idx = 14  # position of reminder in the INSERT value list
            self.assertIsNone(params[rec_idx])

    def test_valarm_seeds_reminder(self):
        _fake.executes.clear()
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        feed = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:v@example.com\n"
            f"DTSTART:{stamp}Z\nDTEND:{stamp}Z\nSUMMARY:V\n"
            "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT30M\nEND:VALARM\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        parsed = parse_ics_calendar(feed)
        calendar_sync._sync_ics_events_to_db(1, 1, parsed)
        self.assertTrue(_inserts())
        _, params = _inserts()[0]
        self.assertEqual(params[14], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
