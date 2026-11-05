#!/usr/bin/env python3
"""Tests for the reconciliation explanation layer (Phase 1 G1 features).

All fixtures here are pure Python numbers shaped like the G1 golden case; the
real client PDFs live outside git.  The golden ids and the G1 amounts come from
the plan (no PII).  Every money value is integer cents.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reconciler import explain as E  # noqa: E402
from reconciler import notes as N  # noqa: E402

# G1 golden numbers, all cents.
G1_NEW_RCV = 95_508_988
G1_PRIOR_PAYMENTS = 84_063_892
G1_DEDUCTIBLE = 1_000_000
G1_PRIOR_RCV = 85_063_892
G1_PAYMENT = 10_445_096
G1_PWARR = 5_706_599
G1_ALREADY_NOTED = 90_770_491
G1_ADDITIONAL = 4_738_497

G1_OUR_RCV = 103_845_029
G1_OUR_HVAC = 16_576_985
G1_CARRIER_HVAC = 10_925_830
G1_GAP = 8_336_041
G1_HVAC_EXCESS = 5_651_155
G1_REALISTIC_GAP = 2_684_886


def item(desc, rcv=0, pretax=None, tax=0, op=0, notes=(), **kw):
    it = {
        "no": kw.get("no", 1),
        "desc": desc,
        "section": kw.get("section", "Roof"),
        "qty": kw.get("qty", 1),
        "unit": kw.get("unit", "EA"),
        "unit_price": kw.get("unit_price", rcv),
        "rcv": rcv,
        "tax": tax,
        "op": op,
        "pretax": rcv - tax - op if pretax is None else pretax,
        "deprec": 0,
        "acv": rcv,
        "notes": list(notes),
    }
    it.update({k: v for k, v in kw.items() if k not in it})
    return it


class TestPriorRcvFromPayments(unittest.TestCase):
    def test_g1_prior_rcv_from_eleven_payments_plus_deductible(self):
        # eleven cheques, 8/15/25 .. 9/11/26, that sum exactly
        payments = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, G1_PRIOR_PAYMENTS - 55]
        res = E.prior_rcv_from_payments(payments, G1_DEDUCTIBLE)
        self.assertEqual(res["payments_count"], 11)
        self.assertEqual(res["payments_total"], G1_PRIOR_PAYMENTS)
        self.assertEqual(res["prior_rcv"], G1_PRIOR_RCV)
        self.assertTrue(res["ties"])

    def test_printed_total_mismatch_is_reported(self):
        res = E.prior_rcv_from_payments([100], 10, printed_total=500)
        self.assertFalse(res["ties"])

    def test_empty_history(self):
        res = E.prior_rcv_from_payments([], 0)
        self.assertEqual(res["prior_rcv"], 0)


class TestPaymentExplanation(unittest.TestCase):
    def _g1(self, **kw):
        args = dict(
            prior_payments=G1_PRIOR_PAYMENTS,
            deductible=G1_DEDUCTIBLE,
            already_noted_rcv=G1_ALREADY_NOTED,
            releases=[{"rcv": G1_PWARR}],
        )
        args.update(kw)
        return E.payment_explanation(G1_NEW_RCV, G1_PAYMENT, **args)

    def test_g1_cheque_is_the_sf_prior_rcv_difference(self):
        res = self._g1()
        self.assertEqual(res["prior_rcv"], G1_PRIOR_RCV)
        self.assertEqual(res["stated_increase"], G1_PAYMENT)
        self.assertTrue(res["ties"], res["checks"])

    def test_g1_additional_increase_after_the_released_pwarr(self):
        res = self._g1()
        self.assertEqual(res["released_total"], G1_PWARR)
        self.assertEqual(res["payment_less_releases"], G1_ADDITIONAL)
        self.assertEqual(res["additional_increase"], G1_ADDITIONAL)

    def test_already_noted_ties_to_prior_plus_released(self):
        res = self._g1()
        check = next(c for c in res["checks"] if "already-noted" in c["name"])
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["expected"], G1_ALREADY_NOTED)

    def test_without_releases_the_cheque_is_the_increase(self):
        res = self._g1(releases=[])
        self.assertEqual(res["released_total"], 0)
        self.assertEqual(res["payment_less_releases"], G1_PAYMENT)

    def test_a_bad_cheque_breaks_the_tie(self):
        res = E.payment_explanation(
            G1_NEW_RCV,
            G1_PAYMENT + 100,
            prior_payments=G1_PRIOR_PAYMENTS,
            deductible=G1_DEDUCTIBLE,
            releases=[{"rcv": G1_PWARR}],
        )
        self.assertFalse(res["ties"])

    def test_summary_lines_quote_the_golden_numbers(self):
        text = "\n".join(self._g1()["summary"])
        self.assertIn("$850,638.92", text)
        self.assertIn("$104,450.96", text)
        self.assertIn("$57,065.99", text)
        self.assertIn("$47,384.97", text)


class TestHoldbackReleases(unittest.TestCase):
    def test_g1_pwarr_line_is_confirmed_from_its_own_note(self):
        new = [
            item(
                'Insulation - ISO board, 4"',
                rcv=G1_PWARR,
                notes=["Paid when applied to roof surfaces in amount of $57,065.99."],
                no=65,
            ),
            item(
                'Insulation - ISO board, 4" (Workers Comp)',
                rcv=11_974_788,
                notes=["Paid when applied to roof surfaces in amount of $12,695.73"],
                no=4,
            ),
        ]
        res = E.holdback_releases(new)
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["confirmed_total"], G1_PWARR)
        self.assertEqual(res["conditional_total"], G1_PWARR + 11_974_788)
        self.assertTrue(res["needs_prior_estimate"])
        # the Worker Comp line's note does not match its own RCV: reported, not guessed
        self.assertEqual(len(res["mismatches"]), 1)
        self.assertEqual(res["mismatches"][0]["note_amount"], 1_269_573)

    def test_with_a_prior_estimate_only_newly_paid_lines_count(self):
        new = [
            item("Roofing felt", rcv=100, notes=["Paid when applied to roof surfaces in amount of $1.00."]),
            item("Drip edge", rcv=200, notes=["Paid when applied to roof surfaces in amount of $2.00."]),
        ]
        old = [
            item("Roofing felt", rcv=0),
            item("Drip edge", rcv=200),
        ]
        res = E.holdback_releases(new, old)
        self.assertEqual(res["confirmed_total"], 100)
        self.assertFalse(res["needs_prior_estimate"])

    def test_code_line_without_a_note_is_not_a_confirmed_release(self):
        res = E.holdback_releases([item("Ordinance and law coverage", rcv=500)])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["confirmed_total"], 0)
        self.assertTrue(res["items"][0]["code_upgrade"])

    def test_plain_lines_are_ignored(self):
        self.assertEqual(E.holdback_releases([item("Drip edge", rcv=500)])["count"], 0)


class TestAttributeChanges(unittest.TestCase):
    def _diff(self):
        op_a = item("Laminated shingles", rcv=1_000, pretax=1_000, no=1)
        op_b = item("Laminated shingles", rcv=1_100, pretax=1_000, op=100, no=1)
        rev_a = item("Valley metal", rcv=900, pretax=900, no=2)
        rev_b = item("Valley metal", rcv=0, pretax=0, no=2, revised=True)
        return {
            "matches": [
                {"a": [op_a], "b": [op_b], "how": "same section", "what_changed": ["O&P", "RCV"]},
                {"a": [rev_a], "b": [rev_b], "how": "same section (qty/price changed)", "what_changed": ["RCV"]},
            ],
            "added": [
                item("Standing seam metal roof", rcv=1_410_930, pretax=1_410_930, no=45),
                item("Ordinance and law - ICC", rcv=500, pretax=500, no=46),
            ],
            "removed": [item("Tarp", rcv=200, pretax=200, no=3)],
        }

    def test_rows_are_labelled(self):
        res = E.attribute_changes(self._diff())
        by_desc = {r["description"]: r for r in res["rows"]}
        self.assertFalse(by_desc["Laminated shingles"]["buildable"])
        self.assertIn("O&P", by_desc["Laminated shingles"]["reason"])
        self.assertIsNone(by_desc["Valley metal"]["buildable"])
        self.assertIn("REVISED", by_desc["Valley metal"]["reason"])
        self.assertTrue(by_desc["Standing seam metal roof"]["buildable"])
        self.assertIn("conditional", by_desc["Ordinance and law - ICC"]["reason"])
        self.assertFalse(by_desc["Tarp"]["buildable"])

    def test_totals_leave_revised_out_of_buildable(self):
        res = E.attribute_changes(self._diff())
        self.assertEqual(res["buildable_total"], 1_410_930 + 500)
        self.assertEqual(res["conditional_total"], 500)
        # O&P-only (+100) and the removed tarp (-200)
        self.assertEqual(res["non_buildable_total"], 100 - 200)
        self.assertEqual(len(res["revised_rows"]), 1)
        self.assertEqual(len(res["op_only_groups"]), 1)

    def test_op_only_group_collapses_every_line(self):
        diff = {
            "matches": [
                {
                    "a": [item(f"Line {i}", rcv=1_000, pretax=1_000)],
                    "b": [item(f"Line {i}", rcv=1_120, pretax=1_000, op=120)],
                }
                for i in range(5)
            ]
        }
        groups = E.op_only_changes(diff)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["line_count"], 5)
        self.assertEqual(groups[0]["op_delta"], 600)
        self.assertEqual(E.attribute_changes(diff)["non_buildable_total"], 600)

    def test_empty_diff(self):
        res = E.attribute_changes({})
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["buildable_total"], 0)


class TestGapVsOurEstimate(unittest.TestCase):
    def _our(self):
        return {
            "totals": {"rcv": G1_OUR_RCV},
            "items": [
                item("R&R Air conditioning unit, 8 ton", rcv=G1_OUR_HVAC, no=39),
                item("Laminated - comp. shingle roofing", rcv=G1_OUR_RCV - G1_OUR_HVAC, no=1),
            ],
        }

    def _carrier(self):
        return {
            "totals": {"rcv": G1_NEW_RCV},
            "items": [
                item("HVACi bid - air conditioning", rcv=G1_CARRIER_HVAC, no=33),
                item("Laminated - comp. shingle roofing", rcv=G1_NEW_RCV - G1_CARRIER_HVAC, no=37),
            ],
        }

    def test_g1_total_gap(self):
        res = E.gap_vs_our_estimate(self._our(), self._carrier())
        self.assertEqual(res["gap"], G1_GAP)

    def test_g1_bid_superseded_hvac_is_excluded(self):
        res = E.gap_vs_our_estimate(self._our(), self._carrier())
        self.assertEqual(res["excluded_total"], G1_HVAC_EXCESS)
        self.assertEqual(res["realistic_gap"], G1_REALISTIC_GAP)

    def test_no_groups_means_the_whole_gap(self):
        res = E.gap_vs_our_estimate(self._our(), self._carrier(), groups=[])
        self.assertEqual(res["realistic_gap"], G1_GAP)

    def test_carrier_ahead_gives_a_negative_gap(self):
        carrier = {"totals": {"rcv": G1_OUR_RCV + 100}, "items": []}
        res = E.gap_vs_our_estimate({"totals": {"rcv": G1_OUR_RCV}, "items": []}, carrier)
        self.assertEqual(res["gap"], -100)


class TestPaymentLetter(unittest.TestCase):
    TEXT = (
        "October 28, 2026\n\nRE: Our Claim Number: 00-0000-000\n"
        "Policy Number: 93J8X2795\nDate of Loss: March 14, 2025\n\n"
        "Thank you for the opportunity to review your claim. Our payment in the amount of "
        "$104,450.96\nin settlement of your Dwelling claim will be mailed to you.\n"
    )

    def test_cheque_amount(self):
        res = E.parse_payment_letter(self.TEXT)
        self.assertEqual(res["payment"], G1_PAYMENT)
        self.assertEqual(res["claim_number"], "00-0000-000")
        self.assertEqual(res["date_of_loss"], "March 14, 2025")
        self.assertTrue(res["ok"])

    def test_missing_amount_is_not_invented(self):
        res = E.parse_payment_letter("no money here")
        self.assertIsNone(res["payment"])
        self.assertFalse(res["ok"])


class TestSummaryOfLoss(unittest.TestCase):
    TEXT = (
        "SUMMARY OF LOSS\n"
        "Replacement Cost Value                         955,089.88\n"
        "Less Deductible                                 10,000.00\n"
        "Less Prior Payments                            840,638.92\n"
        "Net Payment                                    104,450.96\n"
    )

    def test_fields(self):
        res = E.parse_summary_of_loss(self.TEXT)
        self.assertEqual(res["rcv"], G1_NEW_RCV)
        self.assertEqual(res["deductible"], G1_DEDUCTIBLE)
        self.assertEqual(res["prior_payments"], G1_PRIOR_PAYMENTS)
        self.assertEqual(res["net_payment"], G1_PAYMENT)
        self.assertTrue(res["ok"])

    def test_ocr_spacing_tolerated(self):
        text = "Replacement Cost Value    $955,089.88\nNet Payment $ 104,450.96"
        res = E.parse_summary_of_loss(text)
        self.assertEqual(res["rcv"], G1_NEW_RCV)
        self.assertEqual(res["net_payment"], G1_PAYMENT)


class TestOpOnlyNoiseInNoteDrivers(unittest.TestCase):
    def test_drivers_collapse_op_only_rows(self):
        diff = {
            "matches": [
                {
                    "a": [item(f"Line {i}", rcv=1_000, pretax=1_000)],
                    "b": [item(f"Line {i}", rcv=1_120, pretax=1_000, op=120)],
                    "how": "same section",
                    "only_op_or_tax": True,
                    "exact": True,
                }
                for i in range(4)
            ]
            + [
                {
                    "a": [item("Metal roof", rcv=0)],
                    "b": [item("Metal roof", rcv=1_410_930)],
                    "how": "same section (qty/price changed)",
                    "exact": True,
                }
            ],
            "added": [],
            "removed": [],
        }
        drivers = N.change_drivers(diff, limit=10)
        self.assertEqual(drivers[0]["description"], "O&P / tax only on 4 line(s)")
        self.assertEqual(drivers[0]["delta"], 480)
        # the scope change is still listed, the four O&P rows are not
        self.assertEqual(len(drivers), 2)
        self.assertEqual(drivers[1]["description"], "Metal roof")

    def test_collapse_can_be_disabled(self):
        diff = {
            "matches": [
                {
                    "a": [item("Line", rcv=1_000, pretax=1_000)],
                    "b": [item("Line", rcv=1_120, pretax=1_000, op=120)],
                    "only_op_or_tax": True,
                    "exact": True,
                }
            ]
        }
        self.assertEqual(len(N.change_drivers(diff, collapse_op_only=False)), 1)
        self.assertEqual(len(N.change_drivers(diff)), 1)  # group row


if __name__ == "__main__":
    unittest.main(verbosity=2)
