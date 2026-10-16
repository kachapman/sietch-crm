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


class TestRecurrenceRetention(unittest.TestCase):
    """EXDATE/RDATE/RECURRENCE-ID/DURATION/VALARM survive parsing."""

    ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:recur@example.com
DTSTART:20261005T090000Z
DURATION:PT1H
SUMMARY:Daily sync
RRULE:FREQ=DAILY;COUNT=5
EXDATE:20261007T090000Z
EXDATE:20261008T090000Z,20261009T090000Z
RDATE:20261020T090000Z
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:recur@example.com
RECURRENCE-ID:20261006T090000Z
DTSTART:20261006T100000Z
DTEND:20261006T110000Z
SUMMARY:Daily sync (late)
END:VEVENT
END:VCALENDAR
"""

    def setUp(self):
        parsed = parse_ics_calendar(self.ICS)
        self.masters = [e for e in parsed["events"] if not e.get("recurrenceIdRaw")]
        self.overrides = [e for e in parsed["events"] if e.get("recurrenceIdRaw")]
        self.master = self.masters[0]

    def test_master_and_override_split(self):
        self.assertEqual(len(self.masters), 1)
        self.assertEqual(len(self.overrides), 1)
        self.assertEqual(self.overrides[0]["recurrenceIdRaw"], "20261006T090000Z")

    def test_exdates_collected(self):
        self.assertEqual(len(self.master["exdates"]), 3)

    def test_rdates_collected(self):
        self.assertEqual(len(self.master["rdates"]), 1)

    def test_alarm_minutes(self):
        self.assertEqual(self.master["alarmMinutes"], 15)

    def test_duration_retained(self):
        self.assertEqual(self.master["duration"], "PT1H")


class TestExpansion(unittest.TestCase):
    def _expand(self, ics_text, start, end, **kw):
        from datetime import date as _date

        parsed = parse_ics_calendar(ics_text)
        return ics.expand_calendar_events(parsed["events"], start, end, **kw)

    def test_daily_count_with_exdate_rdate_override(self):
        from datetime import date

        exp = self._expand(TestRecurrenceRetention.ICS, date(2026, 10, 1), date(2026, 10, 31))
        uids = [e["uid"] for e in exp]
        # COUNT=5 → Oct 5,6,7,8,9 minus 3 EXDATEs = Oct 5,6; Oct 6 override
        # applied in place; RDATE adds Oct 20 → 3 occurrences total.
        self.assertEqual(len(exp), 3)
        by_uid = {e["uid"]: e for e in exp}
        self.assertIn("recur@example.com#20261005T090000", by_uid)
        late = by_uid["recur@example.com#20261006T100000"]
        self.assertEqual(late["summary"], "Daily sync (late)")
        self.assertTrue(all(e["isOccurrence"] for e in exp))
        self.assertTrue(all(e["recurrenceId"] == "recur@example.com" for e in exp))

    def test_weekly_byday(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:w\nDTSTART:20261005T090000Z\nDTEND:20261005T100000Z\n"
            "SUMMARY:W\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 10, 1), date(2026, 10, 31),
        )
        days = sorted(e["start"]["day"] for e in exp)
        # Oct 2026 from Mon Oct 5: Mon 5,12,19,26 + Wed 7,14,21,28 + Fri 9,16,23,30 = 12.
        self.assertEqual(len(exp), 12)
        self.assertEqual(days[0], 5)

    def test_monthly_skips_short_month(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:m\nDTSTART:20260131T090000Z\nDTEND:20260131T100000Z\n"
            "SUMMARY:M\nRRULE:FREQ=MONTHLY\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 1, 1), date(2026, 4, 30),
        )
        months = sorted((e["start"]["month"], e["start"]["day"]) for e in exp)
        # Jan 31, (Feb 31 invalid → skipped), Mar 31, Apr 30? No — BYMONTHDAY defaults
        # to DTSTART day 31; April has 30 days → skipped too.
        self.assertEqual(months, [(1, 31), (3, 31)])

    def test_yearly(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:y\nDTSTART:20240115T090000Z\nDTEND:20240115T100000Z\n"
            "SUMMARY:Y\nRRULE:FREQ=YEARLY\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 1, 1), date(2028, 12, 31),
        )
        self.assertEqual([e["start"]["year"] for e in exp], [2026, 2027, 2028])

    def test_until_and_interval(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:d\nDTSTART:20261001T090000Z\nDTEND:20261001T100000Z\n"
            "SUMMARY:D\nRRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261007T090000Z\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 10, 1), date(2026, 10, 31),
        )
        self.assertEqual(sorted(e["start"]["day"] for e in exp), [1, 3, 5, 7])

    def test_infinite_rule_capped(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:inf\nDTSTART:20261001T090000Z\nDTEND:20261001T100000Z\n"
            "SUMMARY:I\nRRULE:FREQ=DAILY\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2020, 1, 1), date(2035, 12, 31),
            max_occurrences=50,
        )
        self.assertEqual(len(exp), 50)

    def test_non_recurring_passthrough(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:s\nDTSTART:20261005T090000Z\nSUMMARY:S\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 10, 1), date(2026, 10, 31),
        )
        self.assertEqual(len(exp), 1)
        self.assertEqual(exp[0]["uid"], "s")
        self.assertNotIn("isOccurrence", exp[0])

    def test_occurrence_uid_format(self):
        from datetime import datetime

        self.assertEqual(
            ics.occurrence_uid("abc", datetime(2026, 10, 5, 9, 0), False), "abc#20261005T090000"
        )
        self.assertEqual(
            ics.occurrence_uid("abc", datetime(2026, 10, 5), True), "abc#20261005"
        )

    def test_monthly_nth_weekday(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:n\nDTSTART:20261020T090000Z\nDTEND:20261020T100000Z\n"
            "SUMMARY:N\nRRULE:FREQ=MONTHLY;BYDAY=3TU\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 10, 1), date(2026, 12, 31),
        )
        # 3rd Tuesdays: Oct 20, Nov 17, Dec 15 (2026).
        self.assertEqual(
            [(e["start"]["month"], e["start"]["day"]) for e in exp],
            [(10, 20), (11, 17), (12, 15)],
        )

    def test_duration_sets_end(self):
        from datetime import date

        exp = self._expand(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:du\nDTSTART:20261005T090000Z\nDURATION:PT1H\n"
            "SUMMARY:DU\nRRULE:FREQ=DAILY;COUNT=2\nEND:VEVENT\nEND:VCALENDAR\n",
            date(2026, 10, 1), date(2026, 10, 31),
        )
        # No DTEND in feed → parser applies DURATION (RFC 5545 3.6.1).
        self.assertEqual(len(exp), 2)
        self.assertEqual(exp[0]["end"]["hour"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
