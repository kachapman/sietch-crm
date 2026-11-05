#!/usr/bin/env python3
"""Old-vs-new pair tests for the reconciler (Phase 0b).

Real claim samples live outside git (see ``.gitignore``); every test that
needs one skips cleanly when the fixture folder is missing.  The golden table
below uses neutral ids and integer cents only - no PII.

A pair is accepted only when the net RCV equals the printed difference and
both per-estimate tie-outs and the delta tie-out pass (see
``reconciler.tieout.draft_gate``).  Everything non-exact stays in
``needs_review``.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from reconciler import diff as D  # noqa: E402
from reconciler import parse as P  # noqa: E402
from reconciler import supplement as S  # noqa: E402
from reconciler import tieout as T  # noqa: E402
from reconciler.money import fmt_cents  # noqa: E402

import test_reconciler_parse as PT  # noqa: E402

GOLDEN = PT.GOLDEN
require = PT.require


def load(case_id: str) -> dict:
    spec = GOLDEN[case_id]
    path = require(spec["file"])
    if spec["kind"] == "supplement":
        return S.parse_supplement(str(path), force_text=path.suffix.lower() == ".txt")
    return P.parse_estimate(str(path), force_text=path.suffix.lower() == ".txt")


# case_a, case_b, printed net RCV (cents), counts, needs-review count
PAIRS = [
    # Spock's commercial State Farm job: one job, three revisions
    ("sf_comm_v1", "sf_comm_v2", 50_548_946, (144, 3, 0, 0, 26, 3), 3),
    ("sf_comm_v2", "sf_comm_v3", 7_857_125, (166, 7, 0, 0, 1, 0), 2),
    ("sf_comm_v1", "sf_comm_v3", 58_406_071, (138, 9, 0, 0, 27, 3), 5),
    # State Farm residential single -> self only (no pair delta printed)
    # our existing corpus
    ("rae_og", "rae_sup1", 764_107, (26, 5, 0, 0, 23, 1), 5),
    ("rae_sup1", "rae_final", 321_705, (53, 0, 0, 0, 11, 1), 0),
    ("rae_og", "rae_final", 1_085_812, (26, 5, 0, 0, 33, 1), 5),
    ("stanley_og", "stanley_rev", 379_634, (26, 7, 0, 0, 8, 0), 0),
    ("muehlbauer_og", "muehlbauer_rev", -615_702, (71, 6, 0, 0, 3, 15), 0),
    ("lanc_og_pdf", "lanc_813_pdf", 1_161_589, (0, 51, 0, 0, 8, 0), 0),
    ("lanc_813_pdf", "lanc_1002_pdf", 1_238_316, (58, 1, 0, 0, 3, 0), 0),
]


class TestPairDeltas(unittest.TestCase):
    """The net RCV of a pair equals the printed difference, and the gate passes."""

    @classmethod
    def setUpClass(cls):
        cls._cache = {}

    def _est(self, case_id: str) -> dict:
        if case_id not in self._cache:
            self._cache[case_id] = load(case_id)
        return self._cache[case_id]

    def test_golden_pairs(self):
        for a_id, b_id, net, counts, review in PAIRS:
            with self.subTest(pair=f"{a_id}->{b_id}"):
                a, b = self._est(a_id), self._est(b_id)
                d = D.diff_items(a["items"], b["items"])
                s = D.diff_summary(d)
                self.assertEqual(
                    s["net_rcv"],
                    net,
                    f"{a_id}->{b_id}: net {fmt_cents(s['net_rcv'])} != {fmt_cents(net)}",
                )
                actual = (
                    s["unchanged_count"],
                    s["changed_count"],
                    s["moved_count"],
                    s["split_count"],
                    s["added_count"],
                    s["removed_count"],
                )
                self.assertEqual(actual, counts, f"{a_id}->{b_id}: counts")
                self.assertEqual(
                    s["needs_review_count"], review, f"{a_id}->{b_id}: needs-review count"
                )
                # row deltas must equal the item grand delta
                self.assertEqual(s["row_deltas"]["rcv"], net)

    def test_good_pairs_pass_both_gates(self):
        for a_id, b_id, net, counts, review in PAIRS:
            with self.subTest(pair=f"{a_id}->{b_id}"):
                a, b = self._est(a_id), self._est(b_id)
                d = D.diff_items(a["items"], b["items"])
                td = T.tieout_diff(a, b, d)
                self.assertTrue(
                    td["passed"], f"{a_id}->{b_id} delta tie-out:\n{td['summary']}"
                )
                gate = T.draft_gate(a, b, d)
                self.assertTrue(gate["allowed"], f"{a_id}->{b_id}: {gate['blockers']}")

    def test_printed_delta_is_checked(self):
        """The three-way check really compares against the printed totals."""
        a, b = self._est("sf_comm_v1"), self._est("sf_comm_v2")
        d = D.diff_items(a["items"], b["items"])
        td = T.tieout_diff(a, b, d)
        self.assertEqual(td["fields"]["rcv"]["grand_delta"], 50_548_946)
        self.assertEqual(td["fields"]["rcv"]["section_sum"], 50_548_946)
        self.assertEqual(td["fields"]["rcv"]["row_sum"], 50_548_946)
        self.assertEqual(td["fields"]["rcv"]["printed_delta"], 50_548_946)
        self.assertTrue(td["exact"])

    def test_k1_is_a_known_parse_failure_and_blocks(self):
        """Both sides of the K1 pair fail their own tie-out (known limit)."""
        a, b = self._est("k1_og"), self._est("k1_new")
        self.assertFalse(T.tieout_estimate(a)["passed"])
        d = D.diff_items(a["items"], b["items"])
        gate = T.draft_gate(a, b, d)
        self.assertFalse(gate["allowed"])
        self.assertIn("old estimate failed tie-out", gate["blockers"])


class TestSelfDiff(unittest.TestCase):
    """A fixture diffed with itself is empty - for every parseable fixture."""

    def test_self_diff_is_empty_for_every_fixture(self):
        for case_id in sorted(GOLDEN):
            spec = GOLDEN[case_id]
            if spec["kind"] == "scanned":
                continue
            with self.subTest(case=case_id):
                est = load(case_id)
                d = D.diff_items(est["items"], est["items"])
                s = D.diff_summary(d)
                self.assertEqual(d["added"], [])
                self.assertEqual(d["removed"], [])
                self.assertEqual(d["changed"], [])
                self.assertEqual(d["moved"], [])
                self.assertEqual(d["split"], [])
                self.assertEqual(d["needs_review"], [])
                self.assertEqual(s["net_rcv"], 0)
                self.assertEqual(s["net_pretax"], 0)


class TestPairPerformance(unittest.TestCase):
    """The largest fixture must reconcile in under 2 seconds."""

    def test_pair_under_two_seconds_on_largest_fixture(self):
        path = require(GOLDEN["interior_final_i"]["file"])
        t0 = time.perf_counter()
        a = P.parse_estimate(str(path))
        b = P.parse_estimate(str(path))
        d = D.diff_items(a["items"], b["items"])
        T.tieout_diff(a, b, d)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(a["items"]), 1926)
        self.assertLess(elapsed, 2.0, f"pair took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
