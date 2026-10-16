"""Unit tests for Phase 6 (reminders, search, export, snooze wiring).

No DB required: server.py surface is pinned via source-level guards.
ics_dump is imported from server (pure helper).

Run with: python3 tests/test_calendar_phase6.py
"""

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


class TestIcsDump(unittest.TestCase):
    def setUp(self):
        self.evt = {
            "uid": "src-1#20261005T090000",
            "summary": "Standup",
            "description": "Daily 15-min sync",
            "location": "Room A",
            "dtstart": datetime(2026, 10, 5, 9, 0),
            "dtend": datetime(2026, 10, 5, 9, 15),
            "all_day": False,
        }

    def test_basic_event(self):
        out = server._ics_dump([self.evt])
        self.assertIn("BEGIN:VCALENDAR", out)
        self.assertIn("END:VCALENDAR", out)
        self.assertIn("UID:src-1#20261005T090000", out)
        self.assertIn("DTSTART:20261005T090000", out)
        self.assertIn("DTEND:20261005T091500", out)
        self.assertIn("SUMMARY:Standup", out)
        self.assertIn("LOCATION:Room A", out)
        # Description commas/semicolons/newlines escaped
        self.assertIn("DESCRIPTION:Daily 15-min sync", out)

    def test_all_day_event(self):
        self.evt.update({
            "all_day": True,
            "dtstart": date(2026, 10, 5),
            "dtend": date(2026, 10, 6),
        })
        out = server._ics_dump([self.evt])
        self.assertIn("DTSTART:20261005", out)
        self.assertIn("DTEND:20261006", out)
        # No T-suffix on all-day
        self.assertNotIn("DTSTART:20261005T", out)

    def test_escapes_commas_and_newlines(self):
        self.evt["summary"] = "a, b; c\nd"
        out = server._ics_dump([self.evt])
        self.assertIn("SUMMARY:a\\, b\\; c\\nd", out)

    def test_rrule_preserved(self):
        self.evt["rrule"] = "FREQ=WEEKLY;BYDAY=MO"
        out = server._ics_dump([self.evt])
        # RRULE property separators are escaped per RFC 5545
        self.assertIn("RRULE:FREQ=WEEKLY\\;BYDAY=MO", out)

    def test_empty_input(self):
        out = server._ics_dump([])
        self.assertIn("BEGIN:VCALENDAR", out)
        self.assertIn("END:VCALENDAR", out)
        # No events
        self.assertNotIn("BEGIN:VEVENT", out)

    def test_falls_back_when_end_missing(self):
        self.evt.pop("dtend")
        out = server._ics_dump([self.evt])
        self.assertIn("DTSTART:20261005T090000", out)
        # DTSTART used as DTEND when no end
        self.assertIn("DTEND:20261005T090000", out)

    def test_handles_none_dtstart(self):
        self.evt["dtstart"] = None
        out = server._ics_dump([self.evt])
        # No DTSTART line when start missing
        self.assertNotIn("\nDTSTART:", out)


class TestServerWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        cls.app = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")

    def test_snooze_endpoint(self):
        self.assertIn("/api/v2/calendar/events/(\\d+)/snooze", self.src)
        self.assertIn("_handle_calendar_event_snooze", self.src)

    def test_dismiss_endpoint(self):
        self.assertIn("/api/v2/calendar/events/(\\d+)/dismiss-reminder", self.src)
        self.assertIn("_handle_calendar_event_dismiss_reminder", self.src)

    def test_search_endpoint(self):
        self.assertIn("/api/v2/calendar/search", self.src)
        self.assertIn("_handle_calendar_search", self.src)

    def test_export_endpoint(self):
        self.assertIn("/api/v2/calendar/export", self.src)
        self.assertIn("_handle_calendar_export", self.src)
        self.assertIn('Content-Disposition', self.src)
        self.assertIn("text/calendar", self.src)

    def test_snooze_column_migrated(self):
        self.assertIn("snoozed_until", self.src)

    def test_sweep_skips_snoozed(self):
        sweep = (HERE.parent / "calendar_sync.py").read_text(encoding="utf-8")
        self.assertIn("snoozed_until", sweep)

    def test_telegram_key_corrected(self):
        # Old calendar_telegram was a no-op; new key routes through the dispatcher.
        self.assertIn("telegram_calendar_reminder", self.src)

    def test_drawer_wires_calendar_actions(self):
        self.assertIn("data-cal-snooze", self.app)
        self.assertIn("data-cal-dismiss", self.app)
        self.assertIn("/api/v2/calendar/events/", self.app)

    def test_print_block_in_stylesheet(self):
        styles = (HERE.parent / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media print", styles)
        self.assertIn("visibility: hidden", styles)


if __name__ == "__main__":
    unittest.main(verbosity=2)
