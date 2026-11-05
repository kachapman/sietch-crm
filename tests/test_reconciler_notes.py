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
    draft_note,
    line_diff_mismatch,
    needs_review_note,
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


class TestDraftNoteGate(unittest.TestCase):
    """A failed tie-out must never produce an "increased by $X" sentence."""

    def _gate(self, allowed=True, blockers=(), old_rcv=0, new_rcv=0):
        return {
            "allowed": allowed,
            "blockers": list(blockers),
            "old_rcv": old_rcv,
            "new_rcv": new_rcv,
            "estimate_old": {"results": []},
            "estimate_new": {"results": []},
        }

    def test_allowed_draft_states_the_increase(self):
        text = draft_note(gate=self._gate(True, old_rcv=100_000, new_rcv=150_000))
        self.assertTrue(text.startswith("Claim increased by $500.00"))

    def test_blocked_draft_states_no_increase(self):
        text = draft_note(
            gate=self._gate(False, ["old estimate failed tie-out"], old_rcv=100_000, new_rcv=150_000)
        )
        self.assertNotIn("increased by", text.lower())
        self.assertIn("NEEDS REVIEW", text)
        self.assertIn("old estimate failed tie-out", text)

    def test_stated_increase_comes_from_the_gated_rcvs(self):
        """Caller-supplied RCVs cannot override the tied-out ones."""
        text = draft_note(
            1, 2, gate=self._gate(True, old_rcv=100_000, new_rcv=150_000)
        )
        self.assertIn("$1,000.00 to $1,500.00", text)
        self.assertIn("Claim increased by $500.00", text)

    def test_line_diff_disagreement_is_flagged(self):
        text = draft_note(
            gate=self._gate(True, old_rcv=100_000, new_rcv=200_000),
            diff={"row_deltas": {"rcv": 50_000}},
        )
        self.assertIn("WARNING: the line-item diff totals +$500.00", text)
        self.assertIn("RCV change is +$1,000.00", text)

    def test_matching_line_diff_is_not_flagged(self):
        text = draft_note(
            gate=self._gate(True, old_rcv=100_000, new_rcv=200_000),
            diff={"row_deltas": {"rcv": 100_000}},
        )
        self.assertNotIn("WARNING", text)

    def test_line_diff_mismatch_helper(self):
        mismatch = line_diff_mismatch(-20_353, 4_549_110, {"row_deltas": {"rcv": 2_577_290}})
        self.assertEqual(mismatch["stated"], 4_569_463)
        self.assertEqual(mismatch["diff_total"], 2_577_290)
        self.assertIsNone(line_diff_mismatch(0, 100, {"row_deltas": {"rcv": 100}}))
        self.assertIsNone(line_diff_mismatch(0, 100, {}))

    def test_needs_review_note_reports_both_numbers(self):
        """K1 shape: RCV delta +$45,694.63 vs line diff +$25,772.90."""
        text = needs_review_note(
            gate={
                "allowed": False,
                "blockers": ["old estimate failed tie-out"],
                "estimate_old": {"results": []},
                "estimate_new": {"results": []},
            },
            diff={"row_deltas": {"rcv": 2_577_290}},
            old_rcv=-20_353,
            new_rcv=4_549_110,
        )
        self.assertIn("$45,694.63", text)
        self.assertIn("+$25,772.90", text)
        self.assertIn("disagrees", text)
        self.assertNotIn("increased by", text.lower())

    def test_needs_review_lists_failing_checks(self):
        class R:
            def __init__(self, name, passed):
                self.name = name
                self.passed = passed

        text = needs_review_note(
            gate={
                "allowed": False,
                "blockers": ["old estimate failed tie-out"],
                "estimate_old": {"results": [R("items vs printed", False), R("ok", True)]},
                "estimate_new": {"results": []},
            }
        )
        self.assertIn("items vs printed", text)
        self.assertNotIn("ok,", text)


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
