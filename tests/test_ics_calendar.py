"""Unit tests for ics_calendar (ICS feed parsing for dashboard calendar tiles).

No DB required — ics_calendar is stdlib-only.

Run with: python3 tests/test_ics_calendar.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ics_calendar as ics
from ics_calendar import parse_ics_calendar


SAMPLE_ICS = """BEGIN:VCALENDAR\r
X-WR-CALNAME:Test Calendar\r
X-WR-TIMEZONE:America/Chicago\r
BEGIN:VEVENT\r
UID:evt-1@example.com\r
DTSTAMP:20260101T120000Z\r
DTSTART:20261020T140000Z\r
DTEND:20261020T150000Z\r
SUMMARY:Team meeting\r
DESCRIPTION:Discuss Q4 goals\\, all hands\r
LOCATION:Conference Room A\r
ORGANIZER;CN=Alice:mailto:alice@example.com\r
ATTENDEE;CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.com\r
ATTENDEE;CN=Carol;PARTSTAT=TENTATIVE:mailto:carol@example.com\r
CATEGORIES:Work,Meeting\r
STATUS:CONFIRMED\r
URL:https://example.com/meet\r
RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=TU\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-2@example.com\r
DTSTART;VALUE=DATE:20261025\r
DTEND;VALUE=DATE:20261027\r
SUMMARY:Holiday weekend\r
STATUS:CONFIRMED\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-3@example.com\r
DTSTART:20261021T090000\r
DTEND:20261021T100000\r
SUMMARY:Cancelled standup\r
STATUS:CANCELLED\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-4@example.com\r
DTSTART:20261022T090000Z\r
SUMMARY:No end event\r
END:VEVENT\r
END:VCALENDAR\r
"""


class TestAllowedUrl(unittest.TestCase):
    def test_allows_https(self):
        self.assertTrue(ics.is_allowed_calendar_url("https://example.com/feed.ics"))

    def test_allows_http(self):
        self.assertTrue(ics.is_allowed_calendar_url("http://example.com/feed.ics"))

    def test_rejects_non_http(self):
        self.assertFalse(ics.is_allowed_calendar_url("ftp://example.com/f.ics"))
        self.assertFalse(ics.is_allowed_calendar_url("file:///etc/passwd"))
        self.assertFalse(ics.is_allowed_calendar_url("not a url"))

    def test_rejects_local_hosts(self):
        self.assertFalse(ics.is_allowed_calendar_url("http://localhost/feed.ics"))
        self.assertFalse(ics.is_allowed_calendar_url("http://127.0.0.1/feed.ics"))
        self.assertFalse(ics.is_allowed_calendar_url("http://printer.local/feed.ics"))


class TestUnfold(unittest.TestCase):
    def test_folded_line_joined(self):
        text = "DESCRIPTION:Long line\r\n continued here\r\nSUMMARY:X"
        lines = ics.unfold_ics_lines(text)
        self.assertIn("DESCRIPTION:Long linecontinued here", lines)

    def test_tab_continuation(self):
        text = "A:1\n\t2\nB:3"
        self.assertEqual(ics.unfold_ics_lines(text), ["A:12", "B:3"])


class TestParseProp(unittest.TestCase):
    def test_with_params(self):
        name, meta, value = ics._parse_prop("DTSTART;TZID=America/New_York:20261020T090000")
        self.assertEqual(name, "DTSTART")
        self.assertEqual(meta.get("TZID"), "America/New_York")
        self.assertEqual(value, "20261020T090000")

    def test_plain(self):
        name, meta, value = ics._parse_prop("SUMMARY:Hello")
        self.assertEqual((name, meta, value), ("SUMMARY", {}, "Hello"))


class TestUnescape(unittest.TestCase):
    def test_escapes(self):
        self.assertEqual(ics._unescape_ics_text("a\\,b\\;c\\\\d"), "a,b;c\\d")
        self.assertEqual(ics._unescape_ics_text("line1\\nline2"), "line1\nline2")


class TestOrganizerAttendee(unittest.TestCase):
    def test_organizer_cn(self):
        self.assertEqual(ics._parse_organizer("CN=Alice:mailto:alice@example.com"), "Alice")

    def test_organizer_bare_mailto(self):
        self.assertEqual(ics._parse_organizer("mailto:bob@example.com"), "bob@example.com")

    def test_attendee_accepted_has_plain_label(self):
        a = ics._parse_attendee("CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.com")
        self.assertEqual(a["name"], "Bob")
        self.assertEqual(a["label"], "Bob")

    def test_attendee_tentative_flagged(self):
        a = ics._parse_attendee("CN=Carol;PARTSTAT=TENTATIVE:mailto:carol@example.com")
        self.assertIn("TENTATIVE", a["label"])


class TestRrule(unittest.TestCase):
    def test_weekly_humanized(self):
        label = ics._humanize_rrule("FREQ=WEEKLY;COUNT=10;BYDAY=TU")
        self.assertIn("Weekly", label)
        self.assertIn("10 times", label)
        self.assertIn("TU", label)

    def test_empty(self):
        self.assertEqual(ics._humanize_rrule(""), "")

    def test_interval_until(self):
        label = ics._humanize_rrule("FREQ=DAILY;INTERVAL=2;UNTIL=20261231T000000Z")
        self.assertIn("every 2", label)
        self.assertIn("until", label)


class TestColor(unittest.TestCase):
    def test_explicit_hex(self):
        self.assertEqual(ics._color_from_props({"COLOR": "#AABBCC"}, "u"), "#aabbcc")

    def test_bare_hex(self):
        self.assertEqual(ics._color_from_props({"X-APPLE-CALENDAR-COLOR": "AABBCC"}, "u"), "#aabbcc")

    def test_fallback_deterministic(self):
        c1 = ics._color_from_props({}, "same-uid")
        c2 = ics._color_from_props({}, "same-uid")
        self.assertEqual(c1, c2)
        self.assertIn(c1, ics.PROTON_EVENT_COLORS)


class TestDatetime(unittest.TestCase):
    def test_utc(self):
        d = ics._parse_ics_datetime("20261020T140000Z", {}, "UTC")
        self.assertFalse(d["allDay"])
        self.assertEqual((d["year"], d["month"], d["day"]), (2026, 10, 20))
        self.assertTrue(d["iso"].endswith("Z"))

    def test_all_day(self):
        d = ics._parse_ics_datetime("20261025", {"VALUE": "DATE"}, "UTC")
        self.assertTrue(d["allDay"])
        self.assertEqual(d["iso"], "2026-10-25")

    def test_floating_keeps_tzid(self):
        d = ics._parse_ics_datetime("20261020T090000", {"TZID": "America/New_York"}, "UTC")
        self.assertEqual(d["tzid"], "America/New_York")
        self.assertEqual((d["hour"], d["minute"]), (9, 0))


class TestFullParse(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_ics_calendar(SAMPLE_ICS)

    def test_calendar_name_and_timezone(self):
        self.assertEqual(self.parsed["name"], "Test Calendar")
        self.assertEqual(self.parsed["timezone"], "America/Chicago")

    def test_event_count(self):
        self.assertEqual(len(self.parsed["events"]), 4)

    def test_timed_event_fields(self):
        ev = next(e for e in self.parsed["events"] if e["uid"] == "evt-1@example.com")
        self.assertEqual(ev["summary"], "Team meeting")
        self.assertEqual(ev["description"], "Discuss Q4 goals, all hands")
        self.assertEqual(ev["location"], "Conference Room A")
        self.assertEqual(ev["organizer"], "Alice")
        self.assertEqual(ev["url"], "https://example.com/meet")
        self.assertEqual(ev["categories"], ["Work", "Meeting"])
        self.assertEqual(ev["rrule"], "FREQ=WEEKLY;COUNT=10;BYDAY=TU")
        self.assertIn("Weekly", ev["recurrence"])
        self.assertEqual(len(ev["attendees"]), 2)
        # Tentative attendee label carries partstat; accepted does not.
        labels = [a["label"] for a in ev["attendees"]]
        self.assertTrue(any("TENTATIVE" in lb for lb in labels))

    def test_all_day_multi_day(self):
        ev = next(e for e in self.parsed["events"] if e["uid"] == "evt-2@example.com")
        self.assertTrue(ev["start"]["allDay"])
        self.assertEqual(ev["start"]["iso"], "2026-10-25")

    def test_missing_dtend_falls_back_to_dtstart(self):
        ev = next(e for e in self.parsed["events"] if e["uid"] == "evt-4@example.com")
        self.assertEqual(ev["end"]["iso"], ev["start"]["iso"])

    def test_occurs_in_month(self):
        ev = next(e for e in self.parsed["events"] if e["uid"] == "evt-1@example.com")
        self.assertTrue(ics._event_occurs_in_month(ev, 2026, 10))
        self.assertFalse(ics._event_occurs_in_month(ev, 2026, 11))

    def test_cancelled_excluded_from_month(self):
        ev = next(e for e in self.parsed["events"] if e["uid"] == "evt-3@example.com")
        self.assertEqual(ev["status"], "CANCELLED")
        self.assertFalse(ics._event_occurs_in_month(ev, 2026, 10))

    def test_no_title_fallback(self):
        parsed = parse_ics_calendar(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20261020T140000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.assertEqual(parsed["events"][0]["summary"], "(No title)")

    def test_event_without_dtstart_skipped(self):
        parsed = parse_ics_calendar(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:No date\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.assertEqual(parsed["events"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
