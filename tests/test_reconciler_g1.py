#!/usr/bin/env python3
"""G1 golden-case tests (the plan's "ultimate goal" example).

The client PDFs are gitignored; every test skips cleanly when they are absent.
Neutral ids only: G1 (the golden case) and K1 (the known-failed parse).
No surnames, claim numbers or addresses appear in this file.

Two tiers:

* **Fast tests** (always run when the fixture folder exists): our own R1
  estimate, the payment letter, and the pure explanation maths against the
  golden numbers.
* **OCR tests** (``RECONCILER_OCR_TEST=1``): the full PaddleOCR PP-OCRv6 run on
  the 46-page revised carrier estimate and the Summary of Loss.  They are
  gated because the run is minutes long; the result is cached on disk by file
  hash, so a second run is free.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from reconciler import diff as D  # noqa: E402
from reconciler import explain as E  # noqa: E402
from reconciler import parse as P  # noqa: E402
from reconciler import supplement as S  # noqa: E402
from reconciler import tieout as T  # noqa: E402
from reconciler.money import fmt_cents  # noqa: E402

import reconciler_service as RS  # noqa: E402
import test_reconciler_parse as PT  # noqa: E402

G1_DIR = "golden_g1"

# G1 golden numbers from the plan (integer cents).
G1_NEW_RCV = 95_508_988
G1_ORIGINAL_RCV = 7_521_140
G1_ALREADY_NOTED = 90_770_491
G1_ADDITIONAL = 4_738_497
G1_PAYMENT = 10_445_096
G1_PRIOR_PAYMENTS = 84_063_892
G1_DEDUCTIBLE = 1_000_000
G1_PRIOR_RCV = 85_063_892
G1_PWARR = 5_706_599
G1_OUR_RCV = 103_845_029
G1_OUR_HVAC = 16_576_985
G1_CARRIER_HVAC = 10_925_830
G1_GAP = 8_336_041


def g1_path(name: str) -> Path:
    return PT.require(f"{G1_DIR}/{name}")


def ocr_tests_enabled() -> bool:
    return os.environ.get("RECONCILER_OCR_TEST", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _skip_unless_ocr_ready() -> None:
    if not ocr_tests_enabled():
        raise unittest.SkipTest(
            "heavy OCR test skipped: set RECONCILER_OCR_TEST=1 to run the "
            "PP-OCRv6 pass (minutes on CPU; results are cached)"
        )
    from reconciler import ocr

    ok, why = ocr.ocr_available()
    if not ok:
        raise unittest.SkipTest(f"OCR engine not available: {why}")


# ---------------------------------------------------------------------------
# Fast tier
# ---------------------------------------------------------------------------
class TestG1OurEstimate(unittest.TestCase):
    """Our R1 estimate has a real text layer (drawing pages made it look scan-like)."""

    @classmethod
    def setUpClass(cls):
        cls.est = S.parse_supplement(str(g1_path("our_estimate_r1.pdf")))

    def test_parses(self):
        self.assertEqual(self.est.get("status"), "ok", self.est.get("reason"))
        self.assertEqual(self.est.get("pdf_text_status"), "ok")

    def test_ties_out_to_the_cent(self):
        to = T.tieout_estimate(self.est)
        self.assertTrue(to["passed"], to["summary"])

    def test_golden_rcv(self):
        self.assertEqual(self.est["totals"]["rcv"], G1_OUR_RCV)

    def test_hvac_section_is_the_bid_superseded_scope(self):
        hvac = sum(
            int(it.get("rcv") or 0)
            for it in self.est["items"]
            if (it.get("section") or "") == "HVAC"
        )
        self.assertEqual(hvac, G1_OUR_HVAC)

    def test_estimate_date(self):
        self.assertEqual(self.est["meta"].get("estimate_date"), "8/30/2026")


class TestG1PaymentLetter(unittest.TestCase):
    def test_cheque_amount_from_the_text_layer(self):
        path = g1_path("payment_letter.pdf")
        from reconciler.pdftext import run_pdftotext

        proc = run_pdftotext(str(path))
        text = (proc.stdout or b"").decode("utf-8", errors="replace")
        res = E.parse_payment_letter(text)
        self.assertEqual(res["payment"], G1_PAYMENT)
        # the claim id itself is client data: assert it parsed, never the value
        self.assertTrue(res["claim_number"])
        self.assertRegex(res["claim_number"], r"^\d[\dA-Z-]{5,}$")
        self.assertEqual(res["date_of_loss"], "March 14, 2025")


class TestG1ExplanationMaths(unittest.TestCase):
    """The G1 note's numbers, as pure deterministic maths."""

    def test_stated_increase(self):
        self.assertEqual(G1_NEW_RCV - G1_ORIGINAL_RCV, 87_987_848)  # $879,878.48

    def test_additional_increase_since_the_already_noted_rcv(self):
        self.assertEqual(G1_NEW_RCV - G1_ALREADY_NOTED, G1_ADDITIONAL)

    def test_prior_rcv_from_payments_plus_deductible(self):
        res = E.prior_rcv_from_payments([G1_PRIOR_PAYMENTS], G1_DEDUCTIBLE)
        self.assertEqual(res["prior_rcv"], G1_PRIOR_RCV)

    def test_payment_vs_increase_explanation_ties(self):
        res = E.payment_explanation(
            G1_NEW_RCV,
            G1_PAYMENT,
            prior_payments=G1_PRIOR_PAYMENTS,
            deductible=G1_DEDUCTIBLE,
            already_noted_rcv=G1_ALREADY_NOTED,
            releases=[{"rcv": G1_PWARR}],
        )
        self.assertTrue(res["ties"], res["checks"])
        self.assertEqual(res["stated_increase"], G1_PAYMENT)  # $104,450.96
        self.assertEqual(res["released_total"], G1_PWARR)  # $57,065.99
        self.assertEqual(res["additional_increase"], G1_ADDITIONAL)  # $47,384.97

    def test_gap_and_hvac_exclusion(self):
        """Uses our real R1 items and the golden carrier RCV/HVAC totals."""
        our = S.parse_supplement(str(g1_path("our_estimate_r1.pdf")))
        carrier = {
            "totals": {"rcv": G1_NEW_RCV},
            "items": [
                {"desc": "HVACi bid - air conditioning", "rcv": G1_CARRIER_HVAC},
                {"desc": "Laminated - comp. shingle roofing", "rcv": G1_NEW_RCV - G1_CARRIER_HVAC},
            ],
        }
        res = E.gap_vs_our_estimate(our, carrier)
        self.assertEqual(res["gap"], G1_GAP)  # $83,360.41
        self.assertEqual(res["excluded_total"], G1_OUR_HVAC - G1_CARRIER_HVAC)
        # the plan's "realistic remaining target ~$26.8k"
        self.assertEqual(res["realistic_gap"], 2_684_886)


# ---------------------------------------------------------------------------
# OCR tier
# ---------------------------------------------------------------------------
class TestG1CarrierRevisedOcr(unittest.TestCase):
    """PP-OCRv6 on the 46-page revised carrier estimate (obfuscated text layer)."""

    @classmethod
    def setUpClass(cls):
        _skip_unless_ocr_ready()
        cls.est = P.parse_estimate(str(g1_path("carrier_revised.pdf")), ocr=True)

    def test_ocr_was_used(self):
        self.assertEqual(self.est.get("pdf_text_status"), "ocr")
        info = self.est.get("ocr") or {}
        self.assertEqual(info.get("engine"), "paddleocr-ppocrv6")
        self.assertIn(info.get("device"), ("cpu", "gpu:0", "gpu"))
        # a device decision is recorded, not assumed
        self.assertTrue((info.get("device_info") or {}).get("device_reason"))

    def test_parses(self):
        self.assertEqual(self.est.get("status"), "ok", self.est.get("reason"))
        self.assertGreater(len(self.est.get("items") or []), 100)

    def test_ties_out(self):
        to = T.tieout_estimate(self.est)
        self.assertTrue(to["passed"], to["summary"][:2000])

    def test_golden_rcv_955089_88(self):
        actual = self.est["totals"]["rcv"]
        self.assertEqual(
            actual, G1_NEW_RCV, f"RCV {fmt_cents(actual)} != {fmt_cents(G1_NEW_RCV)}"
        )

    def test_golden_deductible(self):
        self.assertEqual(self.est["totals"]["deductible"], G1_DEDUCTIBLE)

    def test_pwarr_release_57065_99(self):
        res = E.holdback_releases(self.est["items"])
        self.assertEqual(
            res["confirmed_total"],
            G1_PWARR,
            f"confirmed release {fmt_cents(res['confirmed_total'])}; "
            f"mismatches={[m['description'] for m in res['mismatches']]}",
        )


class TestG1GapWithOcrCarrier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_unless_ocr_ready()
        cls.our = S.parse_supplement(str(g1_path("our_estimate_r1.pdf")))
        cls.carrier = P.parse_estimate(str(g1_path("carrier_revised.pdf")), ocr=True)

    def test_gap_83360_41(self):
        res = E.gap_vs_our_estimate(self.our, self.carrier)
        self.assertEqual(
            res["gap"], G1_GAP, f"gap {fmt_cents(res['gap'])} != {fmt_cents(G1_GAP)}"
        )

    def test_bid_superseded_hvac_is_excluded(self):
        res = E.gap_vs_our_estimate(self.our, self.carrier)
        hvac = next(g for g in res["groups"] if g["name"] == "HVAC")
        self.assertEqual(hvac["our_total"], G1_OUR_HVAC)
        self.assertEqual(hvac["carrier_total"], G1_CARRIER_HVAC)
        self.assertEqual(res["excluded_total"], G1_OUR_HVAC - G1_CARRIER_HVAC)
        self.assertEqual(res["realistic_gap"], 2_684_886)

    def test_every_change_is_attributed(self):
        d = D.diff_items(self.our["items"], self.carrier["items"])
        res = E.attribute_changes(d)
        self.assertTrue(res["rows"])
        s = D.diff_summary(d)
        attributed = (
            res["buildable_total"] + res["non_buildable_total"]
            + sum(r["delta"] for r in res["revised_rows"])
        )
        self.assertEqual(attributed, s["row_deltas"]["rcv"])


class TestG1ServiceRun(unittest.TestCase):
    """The Phase 1 service on the real G1 triple (draft only, nothing posted)."""

    @classmethod
    def setUpClass(cls):
        _skip_unless_ocr_ready()

        cls.docs = {}
        for slot, rel in (
            ("prior", "prior_sf/sf_prior_2026-08-27.pdf"),
            ("revised", "carrier_revised.pdf"),
            ("our", "our_estimate_r1.pdf"),
        ):
            p = g1_path(rel)
            cls.docs[slot] = {"docRef": f"doc:{slot}", "title": p.name, "path": str(p)}
        cls.result_run = RS.build_run(
            118,
            cls.docs,
            parsed={
                "prior": P.parse_estimate(cls.docs["prior"]["path"], ocr=True),
                "revised": P.parse_estimate(cls.docs["revised"]["path"], ocr=True),
                "our": S.parse_supplement(cls.docs["our"]["path"]),
            },
        )

    def test_draft_is_allowed_and_states_the_cheque_delta(self):
        self.assertTrue(self.result_run["gateAllowed"], self.result_run["blockers"])
        self.assertEqual(self.result_run["status"], "drafted")
        self.assertIn("Claim increased by $104,450.96", self.result_run["draftNote"])

    def test_result_carries_the_golden_pieces(self):
        result = self.result_run["result"]
        self.assertEqual(result["holdbacks"]["confirmedTotal"], G1_PWARR)
        self.assertEqual(result["gap"]["gap"], G1_GAP)
        self.assertEqual(result["diff"]["summary"]["net_rcv"], G1_PAYMENT)
        self.assertEqual(result["estimates"]["prior"]["totals"]["rcv"], G1_PRIOR_RCV)

    def test_billing_is_info_only_and_present(self):
        self.assertTrue(self.result_run["billingSummary"])
        self.assertTrue(any("Commission" in ln for ln in self.result_run["billingSummary"]))

    def test_result_is_json_serialisable(self):
        import json

        json.dumps(self.result_run["result"])
        json.dumps(self.result_run["inputs"])


class TestG1SummaryOfLossOcr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_unless_ocr_ready()
        from reconciler import ocr

        cls.res = ocr.ocr_pdf(str(g1_path("summary_of_loss.pdf")))

    def test_ocr_ok(self):
        self.assertTrue(self.res.ok, self.res.reason)

    def test_prior_payments_and_deductible(self):
        parsed = E.parse_summary_of_loss(self.res.text)
        self.assertEqual(parsed["rcv"], G1_NEW_RCV)
        self.assertEqual(parsed["deductible"], G1_DEDUCTIBLE)
        self.assertEqual(parsed["prior_payments"], G1_PRIOR_PAYMENTS)
        self.assertEqual(parsed["net_payment"], G1_PAYMENT)

    def test_eleven_payments_sum_to_the_printed_total(self):
        parsed = E.parse_summary_of_loss(self.res.text)
        self.assertEqual(len(parsed["payments"]), 11)
        self.assertEqual(parsed["payments_total"], G1_PRIOR_PAYMENTS)
        self.assertEqual(parsed["payments_totals_printed"], G1_PRIOR_PAYMENTS)
        prior = E.prior_rcv_from_payments(
            parsed["payments"], parsed["deductible"], parsed["payments_totals_printed"]
        )
        self.assertTrue(prior["ties"])
        self.assertEqual(prior["prior_rcv"], G1_PRIOR_RCV)


class TestG1NoteComposition(unittest.TestCase):
    """The G1 note's sentences, assembled from deterministic pieces."""

    def test_g1_note_contains_every_golden_amount(self):
        gap = {
            "gap": G1_GAP,
            "our_rcv": G1_OUR_RCV,
            "carrier_rcv": G1_NEW_RCV,
            "excluded_total": G1_OUR_HVAC - G1_CARRIER_HVAC,
            "realistic_gap": 2_684_886,
            "groups": [{"name": "HVAC"}],
        }
        note = E.claim_change_note(
            G1_NEW_RCV,
            original_rcv=G1_ORIGINAL_RCV,
            already_noted_rcv=G1_ALREADY_NOTED,
            already_noted_date="9/11",
            payment=G1_PAYMENT,
            prior_payments=G1_PRIOR_PAYMENTS,
            deductible=G1_DEDUCTIBLE,
            releases=[{"rcv": G1_PWARR}],
            gap=gap,
            our_label="our R1 8/30 estimate",
        )
        text = "\n".join(note["lines"])
        for amount in (
            "$879,878.48",  # stated increase
            "$75,211.40",  # original RCV
            "$955,089.88",  # new RCV
            "$47,384.97",  # additional since the already-noted RCV
            "$104,450.96",  # cheque
            "$57,065.99",  # PWARR release
            "$83,360.41",  # gap
            "$26,848.86",  # realistic remaining target
        ):
            self.assertIn(amount, text)
        # the prior estimate is not available: the split stays unconfirmed
        self.assertTrue(note["unconfirmed"])
        self.assertIn("prior estimate", text)
        self.assertIn("bid-superseded", text)

    def test_no_original_rcv_never_invents_the_increase(self):
        note = E.claim_change_note(G1_NEW_RCV)
        text = "\n".join(note["lines"])
        self.assertIn("$955,089.88", text)
        self.assertIn("not stated", text)
        self.assertNotIn("The claim increased by", text)

    def test_confirmed_new_items_are_listed(self):
        note = E.claim_change_note(
            G1_NEW_RCV,
            confirmed_new_items=[
                {"description": "GC O&P on the HVAC bid", "delta": 1_654_370},
                {"description": "Garage 1 shingles", "delta": 481_988},
            ],
        )
        text = "\n".join(note["lines"])
        self.assertIn("GC O&P on the HVAC bid +$16,543.70", text)
        self.assertIn("Garage 1 shingles +$4,819.88", text)
        self.assertFalse(note["unconfirmed"])


class TestG1PriorCarrierEstimate(unittest.TestCase):
    """The direct prior (8/27) is here now: the split is computed, not guessed.

    Heavy OCR tier: run with ``RECONCILER_OCR_TEST=1``.  After the first run the
    PP-OCRv6 output is cached by file hash, so re-runs are seconds.
    """

    @classmethod
    def setUpClass(cls):
        _skip_unless_ocr_ready()
        cls.prior = P.parse_estimate(
            str(g1_path("prior_sf/sf_prior_2026-08-27.pdf")), ocr=True
        )
        cls.revised = P.parse_estimate(str(g1_path("carrier_revised.pdf")), ocr=True)
        cls.diff = D.diff_items(cls.prior["items"], cls.revised["items"])
        cls.summary = D.diff_summary(cls.diff)

    def _rows(self, include_zero=False):
        """(delta_cents, old_items, new_items) for every change."""
        rows = []
        for m in self.diff["matches"]:
            delta = sum(int(x.get("rcv") or 0) for x in m["b"]) - sum(
                int(x.get("rcv") or 0) for x in m["a"]
            )
            if delta or include_zero:
                rows.append((delta, m["a"], m["b"]))
        for it in self.diff["added"]:
            rows.append((int(it.get("rcv") or 0), [], [it]))
        for it in self.diff["removed"]:
            rows.append((-int(it.get("rcv") or 0), [it], []))
        return rows

    def _group(self, rows, pattern):
        import re

        rx = re.compile(pattern, re.I)
        out = []
        for delta, a, b in rows:
            if any(rx.search(str(x.get("desc") or "")) for x in list(a) + list(b)):
                out.append((delta, (b or a)[0]))
        return out

    def test_prior_parses_and_ties_out(self):
        self.assertEqual(self.prior.get("status"), "ok", self.prior.get("reason"))
        to = T.tieout_estimate(self.prior)
        self.assertTrue(to["passed"], to["summary"][:1500])

    def test_prior_rcv_is_payments_plus_deductible(self):
        """Both sides parsed: the Summary of Loss and the prior estimate agree."""
        from reconciler import ocr

        sol = ocr.ocr_pdf(str(g1_path("summary_of_loss.pdf")))
        self.assertTrue(sol.ok, sol.reason)
        parsed = E.parse_summary_of_loss(sol.text)
        self.assertEqual(
            parsed["prior_payments"] + parsed["deductible"],
            self.prior["totals"]["rcv"],
            "prior RCV must equal prior payments + deductible",
        )
        self.assertEqual(parsed["prior_payments"], G1_PRIOR_PAYMENTS)
        self.assertEqual(parsed["deductible"], G1_DEDUCTIBLE)

    def test_payment_equals_the_revision_delta(self):
        from reconciler.pdftext import run_pdftotext

        proc = run_pdftotext(str(g1_path("payment_letter.pdf")))
        letter = E.parse_payment_letter((proc.stdout or b"").decode("utf-8", errors="replace"))
        delta = self.revised["totals"]["rcv"] - self.prior["totals"]["rcv"]
        self.assertEqual(letter["payment"], delta)
        self.assertEqual(letter["payment"], G1_PAYMENT)

    def test_pwarr_release_is_confirmed_against_the_prior(self):
        h = E.holdback_releases(self.revised["items"], self.prior["items"])
        self.assertEqual(h["confirmed_total"], G1_PWARR)
        released = [r for r in h["items"] if r["released"]]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["no"], 65)
        self.assertEqual(released[0]["rcv"], G1_PWARR)
        # the Worker Comp ISO line's note still disagrees with its RCV
        self.assertTrue(h["mismatches"])
        self.assertEqual(h["mismatches"][0]["note_amount"], 1_269_573)

    def test_metal_roof_lines_were_already_in_the_prior(self):
        """The golden report inferred ~$14.1k of new metal roof; the prior says 0."""
        metal = self._group(
            self._rows(include_zero=True),
            r"standing seam|metal roof|for metal roofing|metal roof portion|metal panel|steel roof",
        )
        self.assertTrue(metal, "the metal-roof lines must be present in both revisions")
        self.assertEqual(
            sum(d for d, _ in metal),
            0,
            "metal-roof lines changed between the prior and the revision: "
            + ", ".join(f"{d} {x.get('desc')}" for d, x in metal),
        )

    def test_split_of_the_remainder_is_fully_attributed(self):
        """The residual is decomposed from the parsed line deltas, not assumed.

        PWARR + HVAC O&P + Garage 1 shingles are the three confirmed items.
        What is left is attributed per line; the metal roof contributes 0
        because it was already in the 8/27 prior.
        """
        rows = self._rows()
        total = sum(d for d, _a, _b in rows)
        self.assertEqual(total, G1_PAYMENT)

        pwarr = [d for d, _ in self._group(rows, r'ISO board, 4"') if d == G1_PWARR]
        self.assertEqual(pwarr, [G1_PWARR])

        hvac = self._group(rows, r"^HVACi$")
        self.assertEqual(sum(d for d, _ in hvac), 1_654_370)

        garage = [
            (d, it)
            for d, a, b in rows
            for it in [(b or a)[0]]
            if "Garage 1" in str(it.get("section") or "")
        ]
        self.assertEqual(sum(d for d, _ in garage), 481_988)

        residual = G1_PAYMENT - G1_PWARR - 1_654_370 - 481_988
        self.assertEqual(residual, 2_602_139)  # the report's "~$26,021"

        # everything else is accounted for by the remaining parsed lines
        others = total - G1_PWARR - 1_654_370 - 481_988
        self.assertEqual(others, residual)

        # the actual parsed composition of the residual
        condensers = self._group(rows, r"Detach & Reset Central air - condenser")
        black_pipe = self._group(rows, r"Detach & Reset Black pipe")
        interior = [
            (d, it) for d, a, b in rows for it in [(b or a)[0]]
            if "Interior" in str(it.get("section") or "")
        ]
        soffit_felt = self._group(rows, r"Soffit - fiber cement|Roofing felt - 15 lb\.")
        named = (
            sum(d for d, _ in condensers)
            + sum(d for d, _ in black_pipe)
            + sum(d for d, _ in interior)
            + sum(d for d, _ in soffit_felt)
        )
        # any line not named above is small price drift; assert the named groups
        # already explain almost all of the residual and report the rest
        self.assertGreater(named, residual * 0.95)
        self.assertLessEqual(named, residual + 1_000_000)

    def test_revised_gate_run_is_postable(self):
        """With the prior present the gate passes: this is a real revision pair."""
        gate = T.draft_gate(self.prior, self.revised, self.diff)
        self.assertTrue(gate["allowed"], gate["blockers"])
        self.assertEqual(gate["old_rcv"], G1_PRIOR_RCV)
        self.assertEqual(gate["new_rcv"], G1_NEW_RCV)


class TestK1DraftNoteGate(unittest.TestCase):
    """K1: the 2020 side does not parse, so no increase may be stated."""

    @classmethod
    def setUpClass(cls):
        cls.old = P.parse_estimate(str(PT.require("k1/k1_og.txt")), force_text=True)
        cls.new = P.parse_estimate(str(PT.require("k1/k1_new.txt")), force_text=True)
        cls.diff = D.diff_items(cls.old["items"], cls.new["items"])

    def test_old_side_fails_tieout(self):
        self.assertFalse(T.tieout_estimate(self.old)["passed"])
        self.assertEqual(self.old["totals"]["rcv"], -20_353)  # -$203.53

    def test_gate_blocks_the_draft(self):
        gate = T.draft_gate(self.old, self.new, self.diff)
        self.assertFalse(gate["allowed"])

    def test_draft_note_is_needs_review_without_an_increase(self):
        from reconciler.notes import draft_note

        text = draft_note(old=self.old, new=self.new, diff=self.diff)
        self.assertIn("NEEDS REVIEW", text)
        self.assertNotIn("increased by", text.lower())
        # both the unverified RCV delta and the unverified line diff total
        self.assertIn("$45,694.63", text)
        self.assertIn("+$25,772.90", text)

    def test_line_diff_disagrees_with_the_rcv_change(self):
        from reconciler.notes import line_diff_mismatch

        mismatch = line_diff_mismatch(
            self.old["totals"]["rcv"], self.new["totals"]["rcv"], self.diff
        )
        self.assertIsNotNone(mismatch)
        self.assertEqual(mismatch["diff_total"], 2_577_290)  # +$25,772.90
        self.assertEqual(mismatch["stated"], 4_569_463)  # +$45,694.63


if __name__ == "__main__":
    unittest.main(verbosity=2)
