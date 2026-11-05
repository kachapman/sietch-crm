#!/usr/bin/env python3
"""Synthetic, PII-free matcher unit tests (Phase 0b).

Every pass of :func:`reconciler.diff.diff_items` is exercised with hand-built
items, including the R&R split/merge path (untested in the standalone
reconciler) and an ambiguous near-tie.  Guards: non-exact matches always land
in ``needs_review`` and are never ``exact``; personal property stays out of
the billable totals even on the schema path; scanned fixtures still refuse.
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
from reconciler import parse as P  # noqa: E402
from reconciler import tieout as T  # noqa: E402

import test_reconciler_parse as PT  # noqa: E402


def item(
    no,
    desc,
    rcv,
    section="Roof",
    qty=1,
    price=None,
    unit="EA",
    tax=0,
    op=0,
    deprec=0,
    acv=None,
    section_label=None,
    area=None,
):
    price = rcv if price is None else price
    return {
        "no": no,
        "desc": desc,
        "desc_first": desc,
        "rcv": rcv,
        "pretax": rcv - tax - op,
        "tax": tax,
        "op": op,
        "deprec": deprec,
        "acv": acv,
        "section": section,
        "section_label": section_label,
        "area": area,
        "qty": qty,
        "unit": unit,
        "unit_price": price,
        "ok": True,
    }


class TestExactMatches(unittest.TestCase):
    def test_exact_unchanged_is_not_needs_review(self):
        old = [item(1, "Tear off roof", 100_00)]
        new = [item(1, "Tear off roof", 100_00)]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["unchanged"]), 1)
        self.assertEqual(d["needs_review"], [])
        self.assertTrue(d["matches"][0]["exact"])
        self.assertEqual(d["matches"][0]["sim"], 1.0)

    def test_exact_change_is_not_needs_review(self):
        old = [item(1, "Tear off roof", 100_00)]
        new = [item(1, "Tear off roof", 120_00)]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["changed"]), 1)
        self.assertEqual(d["needs_review"], [])
        self.assertEqual(d["matches"][0]["how"], "same section (qty/price changed)")


class TestFuzzyInSection(unittest.TestCase):
    def test_fuzzy_description_match(self):
        old = [item(1, "Laminated - comp. shingle rfg. - w/out felt", 100_00)]
        new = [item(1, "Laminated - composite shingle roofing - without felt", 100_00)]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["matches"]), 1)
        m = d["matches"][0]
        self.assertEqual(m["how"], "fuzzy in-section")
        self.assertLess(m["sim"], 1.0)
        self.assertFalse(m["exact"])
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])
        self.assertEqual(d["needs_review_count"], 1)
        self.assertIn(m, d["needs_review"])

    def test_ambiguous_near_tie_is_logged(self):
        old = [item(1, "Replace window vinyl insulated", 100_00)]
        new = [
            item(1, "Replace window vinyl insulated unit", 100_00),
            item(2, "Replace window vinyl insulated units", 100_00),
        ]
        d = D.diff_items(old, new)
        self.assertTrue(d["ambiguous"], "expected an ambiguous near-tie")
        amb = d["ambiguous"][0]
        self.assertEqual(amb["kind"], "ambiguous")
        self.assertIn(amb["chosen"]["no"], (1, 2))
        self.assertIn(amb["alternative"]["no"], (1, 2))
        self.assertLess(abs(amb["sim_chosen"] - amb["sim_alt"]), 0.02)
        self.assertTrue(any(e.get("kind") == "ambiguous" for e in d["needs_review"]))


class TestMoved(unittest.TestCase):
    def test_exact_item_in_another_section_is_moved(self):
        old = [item(1, "Tear off roof", 100_00, section="Roof"),
               item(2, "Drip edge", 50_00, section="Gutters")]
        new = [item(1, "Tear off roof", 100_00, section="Gutters"),
               item(2, "Drip edge", 50_00, section="Gutters")]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["moved"]), 1)
        self.assertEqual(d["moved"][0][0]["section"], "Roof")
        m = next(m for m in d["matches"] if m["a"][0]["no"] == 1)
        self.assertFalse(m["exact"])
        self.assertIn("moved", m["how"])
        self.assertIn(m, d["needs_review"])

    def test_fuzzy_moved_requires_same_qty(self):
        old = [item(1, "Remove laminated shingles", 100_00, section="Roof", qty=10)]
        new = [item(1, "Remove laminated shingle", 100_00, section="Gutters", qty=10)]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["moved"]), 1)
        self.assertEqual(d["needs_review_count"], 1)


class TestSectionMapping(unittest.TestCase):
    def test_rename_is_mapped(self):
        old = [item(1, "Tear off roof", 100_00, section="ROOF1")]
        new = [item(1, "Tear off roof", 100_00, section="Dwelling Roof")]
        d = D.diff_items(old, new)
        self.assertIn("roof1", d["alias"])
        self.assertEqual(d["alias"]["roof1"], "dwelling roof")
        self.assertEqual(len(d["unchanged"]), 1)

    def test_reparented_section_is_mapped(self):
        old = [
            item(1, "Tear off roof", 100_00, section="Roof",
                 section_label="Exterior / Roof", area="Exterior"),
            item(2, "Drip edge", 50_00, section="Roof",
                 section_label="Exterior / Roof", area="Exterior"),
        ]
        new = [
            item(1, "Tear off roof", 100_00, section="Roof",
                 section_label="Interior / Roof", area="Interior"),
            item(2, "Drip edge", 50_00, section="Roof",
                 section_label="Interior / Roof", area="Interior"),
        ]
        d = D.diff_items(old, new)
        self.assertEqual(d["alias"].get("exterior / roof"), "interior / roof")
        self.assertEqual(len(d["unchanged"]), 2)


class TestReplacement(unittest.TestCase):
    def test_possible_replacement(self):
        old = [item(1, "R&R Skylight - flat fixed, 12.1 - 15 sf", 569_540,
                    qty=4, unit="EA", price=142_385)]
        new = [item(1, "R&R Roof window (skylight), 12.1 - 15 sf", 790_189,
                    qty=4, unit="EA", price=197_547)]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["changed"]), 1)
        self.assertEqual(len(d["added"]), 0)
        self.assertEqual(len(d["removed"]), 0)
        m = d["matches"][0]
        self.assertEqual(m["how"], "possible replacement")
        self.assertFalse(m["exact"])
        self.assertIn(m, d["needs_review"])


class TestRandomReplaceSplitMerge(unittest.TestCase):
    """R&R X <-> Remove X + Replace X (both directions)."""

    def test_rr_splits_into_remove_and_replace(self):
        old = [item(1, "R&R Gutter - aluminum", 1_000_00, qty=10, price=100_00)]
        new = [
            item(1, "Remove Gutter - aluminum", 200_00, qty=10, price=20_00),
            item(2, "Replace Gutter - aluminum", 800_00, qty=10, price=80_00),
        ]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["split"]), 1)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])
        s = D.diff_summary(d)
        self.assertEqual(s["split_count"], 1)
        self.assertEqual(s["net_rcv"], 0)
        self.assertEqual(len(d["needs_review"]), 1)
        self.assertIn("split", d["matches"][0]["how"])

    def test_remove_and_replace_merge_into_rr(self):
        old = [
            item(1, "Remove Gutter - aluminum", 200_00, qty=10, price=20_00),
            item(2, "Replace Gutter - aluminum", 800_00, qty=10, price=80_00),
        ]
        new = [item(1, "R&R Gutter - aluminum", 1_000_00, qty=10, price=100_00)]
        d = D.diff_items(old, new)
        self.assertEqual(len(d["split"]), 1)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])
        self.assertIn("merged", d["matches"][0]["how"])
        self.assertEqual(D.diff_summary(d)["net_rcv"], 0)

    def test_qty_mismatch_does_not_split(self):
        old = [item(1, "R&R Gutter - aluminum", 1_000_00, qty=10, price=100_00)]
        new = [
            item(1, "Remove Gutter - aluminum", 200_00, qty=5, price=40_00),
            item(2, "Replace Gutter - aluminum", 800_00, qty=5, price=160_00),
        ]
        d = D.diff_items(old, new)
        self.assertEqual(d["split"], [])
        self.assertEqual(len(d["added"]), 2)
        self.assertEqual(len(d["removed"]), 1)


class TestGuards(unittest.TestCase):
    def test_every_non_exact_match_is_needs_review(self):
        old = [
            item(1, "Tear off roof", 100_00, section="Roof"),
            item(2, "Laminated - comp. shingle rfg. - w/out felt", 200_00, section="Roof"),
            item(3, "R&R Gutter - aluminum", 1_000_00, section="Gutters", qty=10, price=100_00),
            item(4, "R&R Skylight - flat fixed, 12.1 - 15 sf", 569_540,
                 section="Roof", qty=4, price=142_385),
        ]
        new = [
            item(1, "Tear off roof", 100_00, section="Gutters"),  # moved
            item(2, "Laminated - composite shingle roofing", 200_00, section="Roof"),  # fuzzy
            item(3, "Remove Gutter - aluminum", 200_00, section="Gutters", qty=10, price=20_00),
            item(4, "Replace Gutter - aluminum", 800_00, section="Gutters", qty=10, price=80_00),
            item(5, "R&R Roof window (skylight), 12.1 - 15 sf", 790_189,
                 section="Roof", qty=4, price=197_547),
        ]
        d = D.diff_items(old, new)
        review_ids = {id(m) for m in d["needs_review"]}
        for m in d["matches"]:
            if not m["exact"]:
                self.assertIn(id(m), review_ids, f"{m['how']} not in needs_review")
        self.assertTrue(any(not m["exact"] for m in d["matches"]))
        s = D.diff_summary(d)
        self.assertEqual(s["needs_review_count"], len(d["needs_review"]))

    def test_personal_property_is_excluded_on_the_schema_path(self):
        """Billable totals come from the coverage summaries, never Cov C."""
        caps = [
            {"name": "001 - Coverage A - Dwelling", "line_item_total": 10_000,
             "tax_total": 0, "rcv": 10_000, "taxes": {}, "adjustments": {}},
            {"name": "002 - Coverage C - Personal Property", "line_item_total": 5_000,
             "tax_total": 0, "rcv": 5_000, "taxes": {}, "adjustments": {}},
        ]
        items = [
            item(1, "Laminated - comp. shingle rfg.", 10_000),
            item(2, "Sofa - three cushion", 5_000, section="Personal Property"),
        ]
        schema = {
            "sections": [],
            "areas": [],
            "formats": ["sameline"],
            "line_item_totals": {"name": "synthetic", "printed": {"rcv": 15_000}},
        }
        cov = [
            {"coverage": "001 - Coverage A - Dwelling", "item_total": 10_000, "acv_total": 10_000},
            {"coverage": "002 - Coverage C - Personal Property", "item_total": 5_000,
             "acv_total": 5_000},
        ]
        est = P._finalize_estimate(
            [(1, "synthetic")], "<synthetic>", {"carrier": "State Farm"}, cov,
            caps, items, [], [], "schema", schema=schema,
        )
        self.assertEqual(est["parser_path"], "schema")
        self.assertEqual(est["totals"]["rcv"], 10_000)
        self.assertEqual(est["totals"]["excluded_personal_property_rcv"], 5_000)
        self.assertTrue(any("PERSONAL PROPERTY" in f for f in est["coverage_flags"]))

    def test_scan_fixtures_still_refuse_cleanly(self):
        for case_id in ("busjhan_og", "rouse_og", "comer_cc", "lanc_828_pdf"):
            with self.subTest(case=case_id):
                spec = PT.GOLDEN[case_id]
                path = PT.require(spec["file"])
                est = P.parse_estimate(str(path), force_text=path.suffix.lower() == ".txt")
                self.assertEqual(est["status"], "needs_ocr")
                self.assertEqual(est["items"], [])
                self.assertEqual(est["totals"], {})

    def test_draft_gate_blocks_a_failed_estimate(self):
        good = {
            "status": "ok",
            "items": [item(1, "Tear off roof", 100_00)],
            "totals": {},
            "checks": {},
        }
        bad = {
            "status": "ok",
            "items": [],
            "totals": {},
            "checks": {},
        }
        d = D.diff_items(good["items"], good["items"])
        gate = T.draft_gate(bad, good, d)
        self.assertFalse(gate["allowed"])
        self.assertTrue(gate["blockers"])


class TestOpOnlyAndRevisedNoise(unittest.TestCase):
    """O&P-only rows and REVISED rows are labelled, not reported as scope change."""

    def test_op_only_row_is_flagged_and_counted_once(self):
        old = [item(1, "Laminated shingles", 100_00, qty=10, price=100_00)]
        new = [item(1, "Laminated shingles", 110_00, qty=10, price=100_00, op=10_00)]
        d = D.diff_items(old, new)
        match = next(m for m in d["matches"] if m["a"])
        self.assertTrue(match["only_op_or_tax"])
        s = D.diff_summary(d)
        self.assertEqual(s["op_only_count"], 1)
        self.assertEqual(s["op_only_total"], 10_00)
        self.assertEqual(s["scope_changed_count"], 0)

    def test_a_real_scope_change_is_not_op_only(self):
        old = [item(1, "Laminated shingles", 100_00, qty=10)]
        new = [item(1, "Laminated shingles", 150_00, qty=15)]
        d = D.diff_items(old, new)
        match = next(m for m in d["matches"] if m["a"])
        self.assertFalse(match["only_op_or_tax"])
        self.assertEqual(D.diff_summary(d)["scope_changed_count"], 1)

    def test_revised_row_is_flagged_not_a_removal(self):
        old = [item(1, "Valley metal", 90_00, qty=10)]
        new = [item(1, "Valley metal", 0, qty=10, price=0)]
        new[0]["revised"] = True
        d = D.diff_items(old, new)
        match = next(m for m in d["matches"] if m["a"])
        self.assertTrue(match["revised"])
        self.assertEqual(len(d["removed"]), 0)
        s = D.diff_summary(d)
        self.assertEqual(s["revised_count"], 1)
        self.assertEqual(s["scope_changed_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
