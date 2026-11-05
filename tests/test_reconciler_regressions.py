#!/usr/bin/env python3
"""Regression tests for O&P-only noise and REVISED lines.

Neutral ids only (L1 = the three-revision pair; the fixture paths come from the
gitignored golden table, so this file names no customer).  Both fixtures are
carrier revisions of the same claim:

* the earliest revision has no GC O&P;
* the middle revision adds O&P to many lines (price-only movement) and prints
  three lines as ``REVISED`` (value superseded elsewhere, not removed);
* the newest adds one more ``REVISED`` line.

The pair therefore pins both behaviours on real data: O&P-only rows collapse
to one driver instead of N "changed" rows, and REVISED rows are labelled
superseded rather than counted as scope removals.

Run with: python3 tests/test_reconciler_regressions.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from reconciler import diff as D  # noqa: E402
from reconciler import explain as E  # noqa: E402
from reconciler import notes as N  # noqa: E402
from reconciler import parse as P  # noqa: E402
from reconciler import tieout as T  # noqa: E402

import test_reconciler_parse as PT  # noqa: E402


def load(case_id):
    spec = PT.GOLDEN[case_id]
    path = PT.require(spec["file"])
    return P.parse_estimate(str(path))


class TestOpOnlyNoiseAndRevisedLines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = load("lanc_og_pdf")
        cls.new = load("lanc_813_pdf")
        cls.diff = D.diff_items(cls.old["items"], cls.new["items"])
        cls.summary = D.diff_summary(cls.diff)

    def test_both_sides_parse_and_tie_out(self):
        for name, est in (("old", self.old), ("new", self.new)):
            with self.subTest(side=name):
                self.assertEqual(est.get("status"), "ok", est.get("reason"))
                to = T.tieout_estimate(est)
                self.assertTrue(to["passed"], to["summary"][:1200])

    # --- O&P-only noise ---------------------------------------------------
    def test_price_only_rows_are_flagged(self):
        rows = [m for m in self.diff["matches"] if m.get("only_op_or_tax")]
        self.assertGreater(len(rows), 0, "this pair must exercise O&P-only movement")
        for m in rows:
            with self.subTest(desc=(m.get("b") or [{}])[0].get("desc")):
                o, n = m["a"][0], m["b"][0]
                # the pre-tax amount is identical: only tax/O&P/RCV moved
                self.assertEqual(int(o.get("pretax") or 0), int(n.get("pretax") or 0))

    def test_changed_rows_split_into_scope_op_only_and_revised(self):
        """Every changed row is accounted for exactly once."""
        self.assertEqual(
            self.summary["op_only_count"]
            + self.summary["revised_count"]
            + self.summary["scope_changed_count"],
            self.summary["changed_count"],
        )
        # the revision in this pair is almost entirely an O&P restatement
        self.assertGreater(self.summary["op_only_count"], 0)

    def test_op_only_total_matches_the_rows(self):
        rows = [m for m in self.diff["matches"] if m.get("only_op_or_tax")]
        expected = sum(
            sum(int(x.get("rcv") or 0) for x in m["b"])
            - sum(int(x.get("rcv") or 0) for x in m["a"])
            for m in rows
        )
        self.assertEqual(self.summary["op_only_total"], expected)

    def test_drivers_collapse_op_only_rows_into_one_line(self):
        drivers = N.change_drivers(self.diff, limit=100)
        group = [d for d in drivers if d["description"].startswith("O&P / tax only on")]
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0]["how"], "O&P/tax only")
        self.assertEqual(group[0]["delta"], self.summary["op_only_total"])
        self.assertIn(str(self.summary["op_only_count"]), group[0]["description"])

        # none of the individual O&P-only rows leak into the drivers
        op_only_descs = {
            str((m.get("b") or m.get("a") or [{}])[0].get("desc") or "")
            for m in self.diff["matches"]
            if m.get("only_op_or_tax")
        }
        leaked = [d for d in drivers if d["description"] in op_only_descs]
        self.assertEqual(leaked, [])

    def test_driver_lines_are_readable(self):
        lines = N.change_driver_lines(self.diff, limit=10)
        self.assertTrue(any("O&P / tax only" in ln for ln in lines))

    # --- REVISED lines ----------------------------------------------------
    def test_revised_rows_carry_zero_and_are_kept(self):
        revised = [it for it in self.new["items"] if it.get("revised")]
        self.assertGreater(len(revised), 0, "this pair must exercise REVISED rows")
        for it in revised:
            self.assertEqual(it.get("rcv"), 0)
            self.assertIsNone(it.get("unit_price"))
            self.assertTrue(it.get("status") or it.get("desc"))

    def test_revised_rows_are_matched_not_removed(self):
        revised_matches = [m for m in self.diff["matches"] if m.get("revised")]
        self.assertEqual(len(revised_matches), self.summary["revised_count"])
        self.assertGreater(len(revised_matches), 0)
        # a REVISED row is a superseded line, never an unmatched removal
        for m in revised_matches:
            self.assertTrue(m.get("a"), "a REVISED line should pair with its priced old row")
            self.assertIsInstance(m.get("how"), str)
            self.assertNotEqual(m.get("how"), "only in old")

    def test_no_revised_item_is_reported_as_removed(self):
        revised_descs = {
            str(it.get("desc") or "") for it in self.new["items"] if it.get("revised")
        }
        removed = {str(it.get("desc") or "") for it in self.diff["removed"]}
        self.assertEqual(revised_descs & removed, set())

    def test_attribution_labels_revised_and_accounts_for_every_dollar(self):
        att = E.attribute_changes(self.diff)
        self.assertEqual(len(att["revised_rows"]), self.summary["revised_count"])
        for row in att["revised_rows"]:
            self.assertIsNone(row["buildable"])
            self.assertIn("REVISED", row["reason"])
        attributed = (
            att["buildable_total"]
            + att["non_buildable_total"]
            + sum(r["delta"] for r in att["revised_rows"])
        )
        self.assertEqual(attributed, self.summary["row_deltas"]["rcv"])

    def test_op_only_rows_are_not_buildable_scope(self):
        att = E.attribute_changes(self.diff)
        op_rows = [r for r in att["rows"] if r["kind"] == "op_only"]
        self.assertEqual(len(op_rows), self.summary["op_only_count"])
        self.assertTrue(all(r["buildable"] is False for r in op_rows))
        self.assertTrue(all("O&P" in r["reason"] for r in op_rows))

    def test_net_delta_ties_to_the_printed_revisions(self):
        old_rcv = int((self.old.get("totals") or {}).get("rcv") or 0)
        new_rcv = int((self.new.get("totals") or {}).get("rcv") or 0)
        self.assertEqual(self.summary["net_rcv"], new_rcv - old_rcv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
