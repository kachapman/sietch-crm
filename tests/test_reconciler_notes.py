#!/usr/bin/env python3
"""Tests for the reconciler draft-note text (Phase 0).

Information only - the reconciler never posts a note anywhere.  These tests
only check the generated text.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reconciler.notes import (  # noqa: E402
    NO_PP_MARKER,
    billing_summary_lines,
    claim_increase_note,
    note_text,
)


class TestClaimIncreaseNote(unittest.TestCase):
    def test_basic_format(self):
        text = claim_increase_note(100_000, 150_000)
        self.assertEqual(text, "Claim increased by $500.00 ($1,000.00 to $1,500.00, no PP)")

    def test_no_pp_marker_present(self):
        text = claim_increase_note(100_000, 150_000)
        self.assertIn(NO_PP_MARKER, text)

    def test_personal_property_can_be_included(self):
        text = claim_increase_note(100_000, 150_000, exclude_personal_property=False)
        self.assertNotIn(NO_PP_MARKER, text)
        self.assertEqual(text, "Claim increased by $500.00 ($1,000.00 to $1,500.00)")

    def test_already_noted_reduces_the_increase(self):
        text = claim_increase_note(100_000, 150_000, already_noted=20_000)
        self.assertIn("$300.00", text)
        # the original and new totals are still reported in full
        self.assertIn("$1,000.00 to $1,500.00", text)

    def test_zero_increase(self):
        text = claim_increase_note(100_000, 100_000)
        self.assertIn("$0.00", text)

    def test_decrease_is_reported_as_negative(self):
        text = claim_increase_note(150_000, 100_000)
        self.assertIn("-$500.00", text)

    def test_extra_lines_are_appended(self):
        text = claim_increase_note(100_000, 150_000, extra_lines=["Driver: new flashing", ""])
        self.assertTrue(text.startswith("Claim increased by"))
        self.assertIn("\nDriver: new flashing", text)
        self.assertNotIn("\n\n", text)

    def test_note_text_is_an_alias(self):
        self.assertEqual(
            note_text(100_000, 150_000),
            claim_increase_note(100_000, 150_000),
        )

    def test_no_names_or_claims_in_the_note(self):
        """The note text must not invent a name, job id or invoice number."""
        text = claim_increase_note(100_000, 150_000)
        self.assertNotIn("@", text)
        self.assertNotIn("Invoice", text)
        self.assertNotIn("invoice", text)


class TestBillingSummaryLines(unittest.TestCase):
    def test_lines_report_the_increase(self):
        lines = billing_summary_lines(
            {"rcv": 100_000, "line_item_total": 90_000},
            {"rcv": 150_000, "line_item_total": 130_000},
        )
        self.assertTrue(lines)
        self.assertTrue(any("$500.00" in ln for ln in lines))
        self.assertTrue(any("$400.00" in ln for ln in lines))

    def test_commission_is_included_when_given(self):
        lines = billing_summary_lines(
            {"rcv": 0, "line_item_total": 0},
            {"rcv": 100_000, "line_item_total": 100_000},
            {"commission": 7_500},
        )
        self.assertTrue(any("Commission" in ln for ln in lines))
        self.assertTrue(any("$75.00" in ln for ln in lines))

    def test_note_is_included_when_given(self):
        lines = billing_summary_lines(
            {"rcv": 0}, {"rcv": 0}, {"commission": 0, "note": "NO increase - nothing to bill"}
        )
        self.assertTrue(any("NO increase" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
