#!/usr/bin/env python3
"""Tests for the reconciler commission calculation (Phase 0).

Information only - the reconciler never bills anything.  All amounts are
integer cents.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reconciler.commission import (  # noqa: E402
    COMMISSION_RATE_PCT,
    ESTIMATE_FEE_CENTS,
    commission,
)
from reconciler.diff import diff_items, diff_summary  # noqa: E402
from reconciler.money import fmt_cents  # noqa: E402


def totals(rcv: int, lit: int = 0) -> dict:
    return {"rcv": rcv, "line_item_total": lit}


class TestCommission(unittest.TestCase):
    def test_later_increase_is_ten_percent(self):
        c = commission(totals(100_000), totals(200_000), already_noted=0, first=False)
        self.assertEqual(c["increase_rcv_with_tax"], 100_000)
        self.assertEqual(c["commission"], 10_000)  # 10% of $1,000
        self.assertEqual(c["mode"], "later increase")

    def test_first_increase_subtracts_the_estimate_fee(self):
        # $10,000 increase -> 10% = $1,000 - $250 fee = $750 (above the floor)
        c = commission(totals(0), totals(1_000_000), already_noted=0, first=True)
        self.assertEqual(c["ten_percent"], 100_000)
        self.assertEqual(c["commission"], 75_000)
        self.assertFalse(c["minimum_applied"])

    def test_first_increase_floor_is_the_estimate_fee(self):
        # $1,000 increase -> 10% = $100 - $250 = -$150 -> floor at $250
        c = commission(totals(0), totals(100_000), already_noted=0, first=True)
        self.assertEqual(c["ten_percent"], 10_000)
        self.assertEqual(c["commission"], ESTIMATE_FEE_CENTS)
        self.assertTrue(c["minimum_applied"])

    def test_first_increase_exactly_at_the_crossover(self):
        # 10% x X - 250 == 250  ->  X == $5,000
        c = commission(totals(0), totals(500_000), already_noted=0, first=True)
        self.assertEqual(c["commission"], 25_000)
        self.assertFalse(c["minimum_applied"])

    def test_already_noted_is_subtracted(self):
        c = commission(totals(100_000), totals(200_000), already_noted=5_000, first=False)
        self.assertEqual(c["gross_commission"], 10_000)
        self.assertEqual(c["commission"], 5_000)

    def test_already_noted_can_zero_the_commission(self):
        c = commission(totals(100_000), totals(200_000), already_noted=10_000, first=False)
        self.assertEqual(c["commission"], 0)
        self.assertIn("already", c["note"])

    def test_already_noted_above_the_commission_yields_zero(self):
        c = commission(totals(100_000), totals(200_000), already_noted=25_000, first=False)
        self.assertEqual(c["commission"], 0)
        self.assertTrue(c["note"])

    def test_no_increase_yields_nothing(self):
        c = commission(totals(100_000), totals(100_000), already_noted=0, first=True)
        self.assertEqual(c["increase_rcv_with_tax"], 0)
        self.assertEqual(c["commission"], 0)
        self.assertIn("NO increase", c["note"])

    def test_negative_increase_yields_nothing(self):
        c = commission(totals(200_000), totals(100_000), already_noted=0, first=False)
        self.assertEqual(c["commission"], 0)
        self.assertIn("NO increase", c["note"])

    def test_tax_component_is_reported(self):
        c = commission(totals(100_000, 90_000), totals(200_000, 170_000))
        self.assertEqual(c["increase_rcv_with_tax"], 100_000)
        self.assertEqual(c["increase_pretax"], 80_000)
        self.assertEqual(c["tax_component"], 20_000)

    def test_constants(self):
        self.assertEqual(ESTIMATE_FEE_CENTS, 25_000)
        self.assertEqual(COMMISSION_RATE_PCT, 10)

    def test_all_values_are_integer_cents(self):
        c = commission(totals(123_456, 100_000), totals(234_567, 200_000), already_noted=1_234)
        for key in (
            "old_rcv",
            "new_rcv",
            "increase_rcv_with_tax",
            "increase_pretax",
            "tax_component",
            "already_noted",
            "gross_commission",
            "commission",
        ):
            self.assertIsInstance(c[key], int, f"{key} is not an int")
            self.assertEqual(c[key], int(c[key]))


class TestDiff(unittest.TestCase):
    """The line-item diff (pure function)."""

    @staticmethod
    def item(no, desc, rcv, section="Roof", qty=1, price=100):
        return {
            "no": no,
            "desc": desc,
            "desc_first": desc,
            "rcv": rcv,
            "pretax": rcv,
            "section": section,
            "qty": qty,
            "unit_price": price,
        }

    def test_identical_estimates_have_no_changes(self):
        a = [self.item(1, "Tear off roof", 100_00)]
        d = diff_items(a, list(a))
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])
        self.assertEqual(d["changed"], [])
        self.assertEqual(len(d["unchanged"]), 1)

    def test_added_item(self):
        old = [self.item(1, "Tear off roof", 100_00)]
        new = old + [self.item(2, "Ice and water barrier", 250_00)]
        d = diff_items(old, new)
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(d["added"][0]["no"], 2)

    def test_removed_item(self):
        old = [self.item(1, "Tear off roof", 100_00), self.item(2, "Drip edge", 50_00)]
        new = [self.item(1, "Tear off roof", 100_00)]
        d = diff_items(old, new)
        self.assertEqual(len(d["removed"]), 1)
        self.assertEqual(d["removed"][0]["no"], 2)

    def test_changed_price(self):
        old = [self.item(1, "Tear off roof", 100_00)]
        new = [self.item(1, "Tear off roof", 120_00)]
        d = diff_items(old, new)
        self.assertEqual(len(d["changed"]), 1)
        self.assertEqual(d["changed"][0][0]["rcv"], 100_00)
        self.assertEqual(d["changed"][0][1]["rcv"], 120_00)

    def test_moved_between_sections(self):
        # both sections exist on both sides, so no rename is detected and the
        # item is reported as "moved"
        old = [
            self.item(1, "Tear off roof", 100_00, section="Roof"),
            self.item(2, "Drip edge", 50_00, section="Gutters"),
        ]
        new = [
            self.item(1, "Tear off roof", 100_00, section="Gutters"),
            self.item(2, "Drip edge", 50_00, section="Gutters"),
        ]
        d = diff_items(old, new)
        self.assertEqual(len(d["moved"]), 1)
        self.assertEqual(d["moved"][0][0]["section"], "Roof")
        self.assertEqual(d["moved"][0][1]["section"], "Gutters")
        self.assertEqual(d["sections"]["roof"]["old"], 100_00)
        self.assertEqual(d["sections"]["gutters"]["new"], 150_00)

    def test_section_rename_is_detected(self):
        old = [self.item(1, "Tear off roof", 100_00, section="ROOF1"),
               self.item(2, "Drip edge", 50_00, section="ROOF1")]
        new = [self.item(1, "Tear off roof", 100_00, section="Dwelling Roof"),
               self.item(2, "Drip edge", 50_00, section="Dwelling Roof")]
        d = diff_items(old, new)
        self.assertIn("roof1", d["alias"])
        self.assertEqual(d["alias"]["roof1"], "dwelling roof")
        self.assertEqual(len(d["unchanged"]), 2)

    def test_diff_summary(self):
        old = [self.item(1, "Tear off roof", 100_00)]
        new = [self.item(1, "Tear off roof", 120_00)]
        d = diff_items(old, new)
        s = diff_summary(d)
        self.assertEqual(s["changed_count"], 1)
        self.assertEqual(s["net_rcv"], 20_00)
        self.assertEqual(s["net_pretax"], 20_00)

    def test_diff_is_pure(self):
        """diff_items must not mutate its inputs."""
        old = [self.item(1, "Tear off roof", 100_00)]
        new = [self.item(1, "Tear off roof", 120_00)]
        old_copy = [dict(i) for i in old]
        new_copy = [dict(i) for i in new]
        diff_items(old, new)
        self.assertEqual(old, old_copy)
        self.assertEqual(new, new_copy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
