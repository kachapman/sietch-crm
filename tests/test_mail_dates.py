#!/usr/bin/env python3
"""Regression tests: mail timestamps must be tz-aware UTC, parsed unambiguously.

THE BUG (live, 2026-11-09)
--------------------------
An email Outlook shows as received Mon 11/9/2026 1:20 PM (EST = 18:20:03Z)
appeared in the Sietch email view as "11/9/2026, 2:20:03 AM".

Two independent defects:

  1. STORAGE — `mail_messages.date_received` was `TIMESTAMP` (no time zone).
     The scanner wrote `msg.date.isoformat()` (an ISO string that still
     carried the sender's offset) into it, so Postgres converted it using the
     DB *session* timezone. With TimeZone=UTC the value is UTC wall clock
     (correct); with any other session timezone every row silently becomes
     local wall clock while every reader keeps assuming UTC. That is why the
     error kept coming back.
  2. DISPLAY — the frontend fed offset-less strings to `new Date()`, which
     parses them in the BROWSER's timezone, and rendered with whatever
     timezone the browser had. There was no single formatter (~40 ad-hoc
     `new Date(...).toLocaleString()` calls).

These tests pin the canonical rules: parse to aware UTC, never drop tzinfo,
serialize with an explicit offset, and render in one timezone.

Run with: python3 tests/test_mail_dates.py
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mail_dates

NY = ZoneInfo("America/New_York")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class TestParseEmailDate(unittest.TestCase):
    """The IMAP `Date:` header / stored ISO value → aware UTC."""

    def test_rfc2822_with_negative_offset(self):
        """THE EXACT CASE: 1:20 PM EST is 18:20:03 UTC."""
        dt = mail_dates.parse_email_date("Mon, 9 Nov 2026 13:20:03 -0500")
        self.assertEqual(dt, _utc("2026-11-09T18:20:03Z"))

    def test_rfc2822_gmt(self):
        dt = mail_dates.parse_email_date("Mon, 09 Nov 2026 18:20:03 GMT")
        self.assertEqual(dt, _utc("2026-11-09T18:20:03Z"))

    def test_rfc2822_half_hour_offset(self):
        dt = mail_dates.parse_email_date("Mon, 9 Nov 2026 23:50:03 +0530")
        self.assertEqual(dt, _utc("2026-11-09T18:20:03Z"))

    def test_rfc2822_with_timezone_comment(self):
        dt = mail_dates.parse_email_date("Mon, 9 Nov 2026 13:20:03 -0500 (EST)")
        self.assertEqual(dt, _utc("2026-11-09T18:20:03Z"))

    def test_iso_with_offset(self):
        self.assertEqual(mail_dates.parse_email_date("2026-11-09T13:20:03-05:00"),
                         _utc("2026-11-09T18:20:03Z"))

    def test_iso_with_z(self):
        self.assertEqual(mail_dates.parse_email_date("2026-11-09T18:20:03Z"),
                         _utc("2026-11-09T18:20:03Z"))

    def test_naive_iso_is_utc_not_local(self):
        """Never treat an offset-less value as server/browser local time."""
        self.assertEqual(mail_dates.parse_email_date("2026-11-09T18:20:03"),
                         _utc("2026-11-09T18:20:03Z"))
        self.assertEqual(mail_dates.parse_email_date("2026-11-09 18:20:03"),
                         _utc("2026-11-09T18:20:03Z"))

    def test_naive_datetime_is_utc(self):
        self.assertEqual(mail_dates.parse_email_date(datetime(2026, 11, 9, 18, 20, 3)),
                         _utc("2026-11-09T18:20:03Z"))

    def test_aware_datetime_is_converted(self):
        aware = datetime(2026, 11, 9, 13, 20, 3, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(mail_dates.parse_email_date(aware), _utc("2026-11-09T18:20:03Z"))

    def test_epoch_seconds_and_milliseconds(self):
        epoch = _utc("2026-11-09T18:20:03Z").timestamp()
        self.assertEqual(mail_dates.parse_email_date(epoch), _utc("2026-11-09T18:20:03Z"))
        self.assertEqual(mail_dates.parse_email_date(epoch * 1000), _utc("2026-11-09T18:20:03Z"))

    def test_empty_and_garbage(self):
        for value in (None, "", "   ", "not a date", True, object()):
            self.assertIsNone(mail_dates.parse_email_date(value))

    def test_result_is_always_aware_utc(self):
        dt = mail_dates.parse_email_date("2026-11-09T13:20:03-05:00")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.utcoffset(), timedelta(0))


class TestDst(unittest.TestCase):
    """DST boundaries must not shift the instant or the wall clock."""

    def test_spring_forward_gap(self):
        # 2026-03-08 02:00 EST → 03:00 EDT in America/New_York.
        before = mail_dates.parse_email_date("Sun, 8 Mar 2026 01:59:59 -0500")
        after = mail_dates.parse_email_date("Sun, 8 Mar 2026 03:00:00 -0400")
        self.assertEqual(before, _utc("2026-03-08T06:59:59Z"))
        self.assertEqual(after, _utc("2026-03-08T07:00:00Z"))
        self.assertEqual(before.astimezone(NY).strftime("%-I:%M:%S %p"), "1:59:59 AM")
        self.assertEqual(after.astimezone(NY).strftime("%-I:%M:%S %p"), "3:00:00 AM")

    def test_fall_back_repeated_hour(self):
        # 2026-11-01 01:30 happens twice; the offset disambiguates.
        edt = mail_dates.parse_email_date("Sun, 1 Nov 2026 01:30:00 -0400")
        est = mail_dates.parse_email_date("Sun, 1 Nov 2026 01:30:00 -0500")
        self.assertEqual(edt, _utc("2026-11-01T05:30:00Z"))
        self.assertEqual(est, _utc("2026-11-01T06:30:00Z"))
        self.assertEqual(edt.astimezone(NY).strftime("%-I:%M %p"), "1:30 AM")
        self.assertEqual(est.astimezone(NY).strftime("%-I:%M %p"), "1:30 AM")
        self.assertNotEqual(edt, est)

    def test_offset_depends_on_the_date_not_the_current_dst_state(self):
        summer = mail_dates.parse_email_date("Mon, 13 Jul 2026 12:00:00 -0400")
        winter = mail_dates.parse_email_date("Mon, 12 Jan 2026 12:00:00 -0500")
        self.assertEqual(summer.astimezone(NY).hour, 12)
        self.assertEqual(winter.astimezone(NY).hour, 12)
        self.assertEqual(summer, _utc("2026-07-13T16:00:00Z"))
        self.assertEqual(winter, _utc("2026-01-12T17:00:00Z"))


class TestGraphReceivedDateTime(unittest.TestCase):
    """Graph returns ISO-8601 UTC with up to 7 fractional digits."""

    def test_z(self):
        self.assertEqual(mail_dates.parse_graph_received_datetime("2026-11-09T18:20:03Z"),
                         _utc("2026-11-09T18:20:03Z"))

    def test_seven_fractional_digits(self):
        self.assertEqual(
            mail_dates.parse_graph_received_datetime("2026-11-09T18:20:03.0000000Z"),
            _utc("2026-11-09T18:20:03Z"))
        self.assertEqual(
            mail_dates.parse_graph_received_datetime("2026-11-09T18:20:03.1234567Z"),
            _utc("2026-11-09T18:20:03.123456Z"))

    def test_explicit_offset(self):
        self.assertEqual(
            mail_dates.parse_graph_received_datetime("2026-11-09T13:20:03-05:00"),
            _utc("2026-11-09T18:20:03Z"))

    def test_no_offset_is_utc(self):
        """Graph documents these properties as UTC — not local time."""
        self.assertEqual(
            mail_dates.parse_graph_received_datetime("2026-11-09T18:20:03"),
            _utc("2026-11-09T18:20:03Z"))

    def test_space_separator(self):
        self.assertEqual(
            mail_dates.parse_graph_received_datetime("2026-11-09 18:20:03"),
            _utc("2026-11-09T18:20:03Z"))

    def test_empty(self):
        self.assertIsNone(mail_dates.parse_graph_received_datetime(None))
        self.assertIsNone(mail_dates.parse_graph_received_datetime(""))


class TestSerialization(unittest.TestCase):
    def test_iso_utc_has_an_explicit_offset(self):
        """A browser must never be able to parse it as local time."""
        self.assertEqual(mail_dates.iso_utc(datetime(2026, 11, 9, 18, 20, 3)),
                         "2026-11-09T18:20:03+00:00")
        self.assertTrue(mail_dates.iso_utc("2026-11-09T13:20:03-05:00").endswith("+00:00"))

    def test_iso_utc_z(self):
        self.assertEqual(mail_dates.iso_utc_z("2026-11-09T18:20:03+00:00"),
                         "2026-11-09T18:20:03Z")

    def test_iso_utc_of_none_is_empty(self):
        self.assertEqual(mail_dates.iso_utc(None), "")

    def test_has_explicit_offset(self):
        self.assertTrue(mail_dates.has_explicit_offset("2026-11-09T18:20:03Z"))
        self.assertTrue(mail_dates.has_explicit_offset("2026-11-09T13:20:03-05:00"))
        self.assertFalse(mail_dates.has_explicit_offset("2026-11-09 18:20:03"))


# ─────────────────────────────────────────────────────────────────────────────
# Backfill classification (synthetic rows; no DB, no email content)
# ─────────────────────────────────────────────────────────────────────────────

def _load_backfill():
    import importlib.util
    path = ROOT / "scripts" / "backfill_mail_dates.py"
    spec = importlib.util.spec_from_file_location("backfill_mail_dates", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.bf = _load_backfill()

    def _row(self, row_id, stored, header=None):
        return {
            "id": row_id,
            "account_id": 8,
            "date_received": stored,
            "raw_headers": f"From: a@b.c\nDate: {header}\nSubject: x" if header else "From: a@b.c",
        }

    def test_extracts_the_date_header(self):
        raw = "from: a@b.c\r\nDATE:   Mon, 9 Nov 2026 13:20:03 -0500\r\nSubject: hi"
        self.assertEqual(self.bf.extract_date_header(raw), "Mon, 9 Nov 2026 13:20:03 -0500")

    def test_correct_row_is_ok(self):
        rows = [self._row(1, _utc("2026-11-09T18:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500")]
        report = self.bf.classify_rows(rows)
        self.assertEqual(report["ok"], 1)
        self.assertEqual(report["fixable_from_header"], 0)

    def test_five_hour_offset_is_recoverable(self):
        """A local-wall-clock row: stored 13:20, header says 18:20Z."""
        rows = [self._row(2, _utc("2026-11-09T13:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500")]
        report = self.bf.classify_rows(rows)
        self.assertEqual(report["fixable_from_header"], 1)
        self.assertEqual(report["delta_histogram"], {"-18000": 1})
        self.assertEqual(report["fixes"][0]["target"],
                         _utc("2026-11-09T18:20:03Z").timestamp())

    def test_row_without_header_is_not_guessed(self):
        rows = [self._row(3, _utc("2026-11-09T13:20:03Z"))]
        report = self.bf.classify_rows(rows)
        self.assertEqual(report["no_header"], 1)
        self.assertEqual(report["fixable_from_header"], 0)
        self.assertEqual(report["inferred_fixes"], [])

    def test_inferred_offset_is_applied_only_to_headerless_rows(self):
        rows = [
            self._row(4, _utc("2026-11-09T13:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500"),
            self._row(5, _utc("2026-11-09T13:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500"),
            self._row(6, _utc("2026-11-09T13:20:03Z")),
        ]
        report = self.bf.classify_rows(rows)
        self.assertEqual(report["inferred_offset_seconds"], -18000)
        self.assertEqual(report["inferred_rows"], 1)
        self.assertEqual(report["inferred_fixes"][0]["id"], 6)
        self.assertEqual(report["inferred_fixes"][0]["target"],
                         _utc("2026-11-09T18:20:03Z").timestamp())

    def test_null_stored_value_is_filled_from_the_header(self):
        rows = [self._row(7, None, "Mon, 9 Nov 2026 13:20:03 -0500")]
        report = self.bf.classify_rows(rows)
        self.assertEqual(report["fixable_from_header"], 1)
        self.assertEqual(report["fixes"][0]["target"],
                         _utc("2026-11-09T18:20:03Z").timestamp())

    def test_apply_is_idempotent(self):
        row = self._row(8, _utc("2026-11-09T13:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500")
        first = self.bf.classify_rows([row])
        self.assertEqual(first["fixable_from_header"], 1)
        # Simulate the write, then re-classify the corrected row.
        row["date_received"] = datetime.fromtimestamp(
            first["fixes"][0]["target"], tz=timezone.utc)
        second = self.bf.classify_rows([row])
        self.assertEqual(second["fixable_from_header"], 0)
        self.assertEqual(second["ok"], 1)

    def test_apply_writes_only_the_fixable_rows(self):
        writes = []

        class FakeDB:
            def execute(self, sql, params=None):
                writes.append((sql, params))
                return 1

        fake_db = FakeDB()
        rows = [
            self._row(9, _utc("2026-11-09T13:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500"),
            self._row(10, _utc("2026-11-09T18:20:03Z"), "Mon, 9 Nov 2026 13:20:03 -0500"),
            self._row(11, _utc("2026-11-09T13:20:03Z")),
        ]
        with patch.dict(sys.modules, {"db": fake_db}):
            changed = self.bf.apply_fixes(self.bf.classify_rows(rows)["fixes"])
        self.assertEqual(changed, 1)
        self.assertEqual(len(writes), 1)
        self.assertIn("UPDATE mail_messages SET date_received", writes[0][0])
        self.assertEqual(writes[0][1][1], 9)

    def test_dry_run_does_not_write(self):
        writes = []

        class FakeDB:
            def query_dicts(self, sql, params=None):
                return [{
                    "id": 12, "account_id": 8,
                    "date_received": _utc("2026-11-09T13:20:03Z"),
                    "raw_headers": "Date: Mon, 9 Nov 2026 13:20:03 -0500",
                }]

            def execute(self, sql, params=None):
                writes.append((sql, params))
                return 1

        with patch.dict(sys.modules, {"db": FakeDB()}):
            self.bf.main([])
        self.assertEqual(writes, [], "the default mode must be a dry run")

    def test_report_prints_no_content(self):
        rows = [self._row(13, _utc("2026-11-09T13:20:03Z"),
                          "Mon, 9 Nov 2026 13:20:03 -0500")]
        text = self.bf.render(self.bf.classify_rows(rows), sample=5)
        self.assertNotIn("a@b.c", text)
        self.assertNotIn("Subject", text)
        self.assertIn("#13", text)


# ─────────────────────────────────────────────────────────────────────────────
# Source guards for the pieces that need a live DB / browser
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceGuards(unittest.TestCase):
    def setUp(self):
        self.server_src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.scanner_src = (ROOT / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")
        self.app_src = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.dates_js = (ROOT / "public" / "mail-dates.js").read_text(encoding="utf-8")

    def test_scanner_stores_an_aware_datetime(self):
        at = self.scanner_src.index("def _store_message")
        region = self.scanner_src[at:at + 3000]
        self.assertIn('mail_dates.parse_email_date(msg.get("date"))', region)
        self.assertNotIn('msg.get("date") or None', region)

    def test_scanner_emits_iso_z(self):
        self.assertIn('mail_dates.iso_utc_z(msg.date)', self.scanner_src)

    def test_no_raw_str_of_a_mail_timestamp_reaches_the_api(self):
        self.assertNotIn('str(r.get("date_received")', self.server_src)
        self.assertNotIn('str(msg.get("date_received"', self.server_src)
        self.assertNotIn('str(row["date_received"])', self.server_src)

    def test_migration_declares_existing_values_as_utc(self):
        sql = (ROOT / "migrations" / "048_mail_dates_utc.sql").read_text(encoding="utf-8")
        self.assertIn("TYPE timestamptz", sql)
        self.assertIn("USING date_received AT TIME ZONE 'UTC'", sql)
        self.assertNotIn("DROP ", sql.upper())

    def test_frontend_uses_the_shared_formatter(self):
        self.assertIn('from "./mail-dates.js', self.app_src)
        # The mail list and preview must not build dates by hand any more.
        self.assertNotIn("new Date(date).toLocaleDateString('en-US')", self.app_src)
        self.assertIn("formatMailDate(date, { mode: \"list\"", self.app_src)
        self.assertIn("formatMailDateOrRaw(raw, { mode: \"datetime\"", self.app_src)

    def test_shared_formatter_treats_naive_as_utc(self):
        self.assertIn("NAIVE_ISO", self.dates_js)
        self.assertIn("Z`", self.dates_js)
        self.assertIn("MAIL_DEFAULT_TIMEZONE", self.dates_js)
        self.assertIn("America/New_York", self.dates_js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
