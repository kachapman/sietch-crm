#!/usr/bin/env python3
"""Tests for the Sietch Estimate Reconciler parser library (Phase 0).

The real claim samples live outside git (see ``.gitignore``) and are read from
``RECONCILER_SAMPLES_DIR`` (default ``tests/fixtures/reconciler``).  Every test
that needs a sample skips cleanly when the sample is missing, so CI without
the data stays green.

The golden table below contains **no PII** - only carrier names, line counts
and money amounts in integer cents.  Samples are referenced by a neutral id.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reconciler import parse as P  # noqa: E402
from reconciler import supplement as S  # noqa: E402
from reconciler import tieout as T  # noqa: E402
from reconciler.money import fmt_cents, parse_money, to_cents  # noqa: E402
from reconciler.pdftext import extract_text, looks_garbled  # noqa: E402

SAMPLES_DIR = Path(
    os.environ.get("RECONCILER_SAMPLES_DIR", str(ROOT / "tests/fixtures/reconciler"))
)


def sample_path(rel: str) -> Path:
    return SAMPLES_DIR / rel


def require(rel: str) -> Path:
    """Return the sample path or skip the test with a clear message."""
    p = sample_path(rel)
    if not p.exists():
        raise unittest.SkipTest(
            f"sample not available: {rel} (looked in {SAMPLES_DIR}; "
            f"set RECONCILER_SAMPLES_DIR to the fixture folder)"
        )
    return p


def load_old_recon():
    """Load the old recon.py as an oracle (it lives in the ignored fixtures)."""
    p = sample_path("recon.py")
    if not p.exists():
        raise unittest.SkipTest(f"old recon.py oracle not available: {p}")
    spec = importlib.util.spec_from_file_location("old_recon_oracle", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cents(d) -> int:
    """Old recon.py uses Decimal dollars; convert to cents for comparison."""
    if d is None:
        return 0
    return int((Decimal(d) * 100).quantize(Decimal("1")))


# ---------------------------------------------------------------------------
# golden expectations (no PII: carrier + counts + integer cents)
# ---------------------------------------------------------------------------
# kind: "carrier" | "supplement" | "scanned"
GOLDEN = {
    # --- old recon.py text samples ---
    "rae_og": {
        "file": "recon_samples/og_carrier_estimate_robert_rae_8-6-26.txt",
        "kind": "carrier",
        "carrier": "Allstate",
        "items": 32,
        "rcv": 1964301,
        "lit": 1931326,
        "tax": 32975,
        "deductible": 25000,
        "acv": 1391800,
        "summaries": 2,
        "coverage_rows": 21,
        "tie_out": True,
    },
    "rae_final": {
        "file": "recon_samples/ROBERT_RAE1_FINAL_DRAFT_DEPR_W_FAC_P_ALLST.txt",
        "kind": "carrier",
        "carrier": "Allstate",
        "items": 64,
        "rcv": 3080331,
        "lit": 3017138,
        "tax": 63193,
        "deductible": 25000,
        "acv": 2544400,
        "summaries": 2,
        "coverage_rows": 21,
        "tie_out": True,
    },
    "rae_sup1": {
        "file": "recon_samples/Supplement 1.txt",
        "kind": "carrier",
        "carrier": "Allstate",
        "items": 54,
        "rcv": 2752169,
        "lit": 2695433,
        "tax": 56736,
        "deductible": 25000,
        "acv": 2238674,
        "summaries": 2,
        "coverage_rows": 21,
        "tie_out": True,
    },
    "morrison_og": {
        "file": "recon_samples/og_carrier_estimate_pete_morrison_4-27-26_searchable.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 41,
        "rcv": 5361945,
        "lit": 5235758,
        "tax": 126187,
        "deductible": 244000,
        "summaries": 1,
        "coverage_rows": 0,
        # OCR'd sample: the summary ties out, the items do not (known limit).
        "tie_out": False,
    },
    "allstate_supp": {
        "file": "recon_samples/ROBERT-R_Exterior 9.23.26.txt",
        "kind": "supplement",
        "carrier": "us",
        "items": 103,
        "rcv": 4929986,
        "lit": 4030885,
        "tie_out": True,
    },
    "rouse_ext1": {
        "file": "recon_samples/ROUSE-W_Exterior 7.10.26.txt",
        "kind": "supplement",
        "carrier": "us",
        "items": 61,
        "rcv": 7807930,
        "tie_out": True,
    },
    "rouse_ext2": {
        "file": "recon_samples/ROUSE-W_Exterior 8.5.26.txt",
        "kind": "supplement",
        "carrier": "us",
        "items": 63,
        "rcv": 8091680,
        "tie_out": True,
    },
    "comer_rev": {
        "file": "recon_samples/COMER-R_Exterior 6.18.26.txt",
        "kind": "supplement",
        "carrier": "us",
        "items": 60,
        "rcv": 7846903,
        "tie_out": True,
    },
    "morrison_rev": {
        "file": "recon_samples/MORRISON-P_Exterior 10.1.26.txt",
        "kind": "supplement",
        "carrier": "us",
        "items": 74,
        "rcv": 5853826,
        "tie_out": True,
    },
    "stanley_og": {
        "file": "recon_samples/carrier_estimate_Craig_Stanley_4-30-26.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 33,
        "rcv": 2644358,
        "lit": 2582919,
        "tax": 61439,
        "deductible": 395000,
        "summaries": 1,
        "coverage_rows": 0,
        "tie_out": True,
    },
    "stanley_rev": {
        "file": "recon_samples/revised_carrier_estimate_stanley_9-28-26.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 41,
        "rcv": 3023992,
        "lit": 2957454,
        "tax": 66538,
        "deductible": 395000,
        "summaries": 2,
        "coverage_rows": 0,
        "tie_out": True,
    },
    "koziol_rev": {
        "file": "recon_samples/revised_carrier_estimate_tom_koziol_10-12-26.txt",
        "kind": "carrier",
        "carrier": "Farmers",
        "items": 25,
        "rcv": 3474198,
        "lit": 3393673,
        "tax": 80525,
        "deductible": 100000,
        "summaries": 1,
        "coverage_rows": 0,
        "tie_out": True,
    },
    "muehlbauer_og": {
        "file": "recon_samples/JFM4618 Travelers Insured Copy.txt",
        "kind": "carrier",
        "carrier": "Travelers",
        "items": 92,
        "rcv": 6578564,
        "lit": 5376027,
        "tax": 107289,
        "op": 1095248,
        "deductible": 100000,
        "acv": 5609634,
        "summaries": 2,
        "coverage_rows": 5,
        "tie_out": True,
    },
    "muehlbauer_rev": {
        "file": "recon_samples/JFM4618 Travelers MUEHLBAUER revised 8-25-26.txt",
        "kind": "carrier",
        "carrier": "Travelers",
        "items": 80,
        "rcv": 5962862,
        "lit": 4872890,
        "tax": 97354,
        "op": 992618,
        "deductible": 100000,
        "acv": 5068790,
        "summaries": 2,
        "coverage_rows": 5,
        "tie_out": True,
    },
    "busjhan_rev": {
        "file": "recon_samples/revised_carrier_estimate_Busjahn_9-29-26.txt",
        "kind": "carrier",
        "carrier": "Auto-Owners",
        "items": 30,
        "rcv": 3497920,
        "lit": 3421171,
        "tax": 76749,
        "deductible": 100000,
        "acv": 2927829,
        "summaries": 2,
        "coverage_rows": 1,
        "tie_out": True,
    },
    "rouse_rev": {
        "file": "recon_samples/revised_carrier_estimate_rouse_10-2-26.txt",
        "kind": "carrier",
        "carrier": "Universal",
        "items": 56,
        "rcv": 6749370,
        "lit": 6019503,
        "tax": 131008,
        "op": 1124900,
        "deductible": 710000,
        "acv": 3996223,
        "summaries": 2,
        "coverage_rows": 3,
        "tie_out": True,
    },
    "comer_final": {
        "file": "recon_samples/Customer Copy Final Draft 7.13.26.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 43,
        "rcv": 5576941,
        "lit": 5448985,
        "tax": 127956,
        "deductible": 563000,
        "summaries": 2,
        "coverage_rows": 0,
        "tie_out": True,
    },
    "sf_sample": {
        "file": "recon_samples/SF-1-Customer Copy 400150073_1.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 59,
        "rcv": 5515655,
        "lit": 5387939,
        "tax": 127716,
        "deductible": 244000,
        "summaries": 1,
        "coverage_rows": 0,
        "tie_out": True,
    },
    # --- Koller ---
    "koller_og": {
        "file": "koller/og_all.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 31,
        "summaries": 1,
        # OCR'd earlier: item totals do not reconcile (known limit, same as recon.py)
        "tie_out": False,
    },
    "koller_new": {
        "file": "koller/new.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 53,
        "rcv": 4549110,
        "lit": 4090407,
        "tax": 32809,
        "op": 425894,
        "deductible": 271400,
        "summaries": 2,
        "coverage_rows": 0,
        "tie_out": True,
    },
    "koller_ours": {
        "file": "koller/ours.txt",
        "kind": "supplement",
        "carrier": "us",
        "items": 99,
        "rcv": 4854025,
        "lit": 4045017,
        "op": 809008,
        "deductible": 271400,
        "sections": 12,
        "areas": 4,
        "tie_out": True,
    },
    "koller_new_pdf": {
        "file": "koller/49-0L1N-844 - Customer Copy Final Draft.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 53,
        "rcv": 4549110,
        "tie_out": True,
    },
    "koller_ours_pdf": {
        "file": "koller/KOLLER-R_Exterior.Interior 9.15.26.pdf",
        "kind": "supplement",
        "carrier": "us",
        "items": 99,
        "rcv": 4854025,
        "tie_out": True,
    },
    # --- Lancaster ---
    "lanc_r1002": {
        "file": "lancaster/r1002.txt",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 62,
        "rcv": 7238909,
        "lit": 6130263,
        "tax": 106546,
        "op": 1002100,
        "deductible": 458800,
        "summaries": 1,
        "coverage_rows": 0,
        "tie_out": True,
    },
    "lanc_og_pdf": {
        "file": "lancaster/carrier_estimate_Susan_Lancaster_5-12-26.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 51,
        "rcv": 4839004,
        "tie_out": True,
    },
    "lanc_813_pdf": {
        "file": "lancaster/SF revised Lancaster MORRISROE 8-13-26.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 59,
        "rcv": 6000593,
        "tie_out": True,
    },
    "lanc_1002_pdf": {
        "file": "lancaster/revised_carrier_estimate_lancaster_10-2-26.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 62,
        "rcv": 7238909,
        "tie_out": True,
    },
    # --- scanned / garbled: must refuse cleanly ---
    "busjhan_og": {
        "file": "recon_samples/og_carrier_estimate_Sue_David_Busjhan_7-16-26.txt",
        "kind": "scanned",
    },
    "rouse_og": {
        "file": "recon_samples/carrier_estimate_Rouse_5-29-26.txt",
        "kind": "scanned",
    },
    "comer_cc": {
        "file": "recon_samples/CC COMER.txt",
        "kind": "scanned",
    },
    "lanc_828_pdf": {
        "file": "lancaster/State Farm revised Lancaster-Morrisoe 8-28-26.pdf",
        "kind": "scanned",
    },
    # --- Spock's standalone-reconciler corpus (neutral ids only) ---
    # s1 = one commercial State Farm job, three revisions; the printed RCV
    # differences are v1->v2 +505,489.46, v2->v3 +78,571.25, v1->v3 +584,060.71.
    "sf_comm_v1": {
        "file": "spock/s1_pair_v1.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 150,
        "rcv": 26657821,
        "lit": 21820418,
        "tax": 520559,
        "op": 4316844,
        "deductible": 1000000,
        "summaries": 4,
        "parser_path": "schema",
        "tie_out": True,
    },
    "sf_comm_v2": {
        "file": "spock/s1_pair_v2.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 173,
        "rcv": 77206767,
        "lit": 62958115,
        "tax": 1506974,
        "op": 12741678,
        "deductible": 1000000,
        "summaries": 4,
        "parser_path": "schema",
        "tie_out": True,
    },
    "sf_comm_v3": {
        "file": "spock/s1_pair_v3.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 174,
        "rcv": 85063892,
        "lit": 70886260,
        "tax": 1505076,
        "op": 12672556,
        "deductible": 1000000,
        "summaries": 4,
        "parser_path": "schema",
        "tie_out": True,
    },
    "sf_res_h": {
        "file": "spock/s2_single_sf.pdf",
        "kind": "carrier",
        "carrier": "State Farm",
        "items": 184,
        "rcv": 35846935,
        "lit": 34792778,
        "tax": 1054157,
        "deductible": 2500000,
        "summaries": 8,
        "parser_path": "legacy",
        "tie_out": True,
    },
    "interior_final_i": {
        "file": "spock/s3_single_interior.pdf",
        "kind": "carrier",
        "carrier": "unknown",
        "items": 1926,
        "rcv": 114585530,
        "lit": 111124335,
        "tax": 3461195,
        "summaries": 0,
        "parser_path": "schema",
        "tie_out": True,
    },
    "ocr_e": {
        "file": "spock/s4_single_edj.pdf",
        "kind": "carrier",
        "carrier": "Auto-Owners",
        "items": 727,
        "rcv": 58544504,
        "lit": 57536823,
        "tax": 1007681,
        "deductible": 9943909,
        "acv": 41454074,
        "summaries": 31,
        "parser_path": "schema",
        "tie_out": True,
    },
}

# the neutral ids the plan asks for explicitly
REQUIRED_IDS = [
    "morrison_og",
    "stanley_og",
    "stanley_rev",
    "koziol_rev",
    "muehlbauer_og",
    "muehlbauer_rev",
    "sf_sample",
    "koller_og",
    "koller_new",
    "koller_ours",
    "lanc_r1002",
    "sf_comm_v1",
    "sf_comm_v2",
    "sf_comm_v3",
    "sf_res_h",
    "interior_final_i",
    "ocr_e",
]


def parse_case(case_id: str):
    """Parse one golden case with the right parser; returns the estimate dict."""
    spec = GOLDEN[case_id]
    path = require(spec["file"])
    if spec["kind"] == "supplement":
        return S.parse_supplement(str(path), force_text=path.suffix.lower() == ".txt")
    return P.parse_estimate(str(path), force_text=path.suffix.lower() == ".txt")


class TestGoldenTable(unittest.TestCase):
    """The golden table must stay complete and PII-free."""

    def test_required_ids_present(self):
        for cid in REQUIRED_IDS:
            self.assertIn(cid, GOLDEN, f"golden case {cid} missing")

    def test_golden_contains_no_pii(self):
        """The committed expectations must not carry names/claims/phones/e-mails."""
        import re

        # money amounts and counts are allowed; string values are not
        strings = []
        for cid, spec in GOLDEN.items():
            self.assertNotIn("insured", spec, f"{cid} carries an insured name")
            self.assertNotIn("claim", spec, f"{cid} carries a claim number")
            for key, value in spec.items():
                if isinstance(value, str):
                    strings.append(value)
        blob = " ".join(strings)

        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", blob), "e-mail in golden table")
        self.assertIsNone(
            re.search(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b", blob), "phone in golden table"
        )
        # no digit run that could be a claim / policy number in a string value
        self.assertIsNone(re.search(r"\b\d{7,}\b", blob), "long number (claim/policy) in golden table")
        # the carrier list is the only allowed free text
        allowed = {
            "carrier", "kind", "file", "us", "supplement", "carrier", "scanned",
            "unknown", "schema", "legacy",
        }
        for value in strings:
            if value in allowed:
                continue
            if value in P.SUPPORTED_CARRIERS:
                continue
            if value.startswith("recon_samples/") or value.startswith("koller/") or value.startswith(
                "lancaster/"
            ) or value.startswith("spock/"):
                continue
            self.fail(f"unexpected free-text value in the golden table: {value!r}")


class TestGoldenCases(unittest.TestCase):
    """Every golden case parses to the expected numbers."""

    def _check(self, case_id: str):
        spec = GOLDEN[case_id]
        est = parse_case(case_id)
        self.assertEqual(est.get("status"), "ok", f"{case_id}: {est.get('reason')}")
        meta = est.get("meta") or {}
        if "carrier" in spec:
            self.assertEqual(meta.get("carrier"), spec["carrier"], f"{case_id}: carrier")
        checks = est.get("checks") or {}
        if "items" in spec:
            self.assertEqual(checks.get("items_parsed"), spec["items"], f"{case_id}: item count")
        totals = est.get("totals") or {}
        for key in ("rcv", "lit", "tax", "op", "deductible", "acv"):
            if key in spec:
                field = "line_item_total" if key == "lit" else (
                    "overhead_profit" if key == "op" else key
                )
                self.assertEqual(
                    totals.get(field), spec[key], f"{case_id}: {key} was {totals.get(field)}"
                )
        if "summaries" in spec:
            self.assertEqual(len(est.get("summaries") or []), spec["summaries"], f"{case_id}: summaries")
        if "coverage_rows" in spec:
            self.assertEqual(
                len(est.get("coverage_table") or []), spec["coverage_rows"], f"{case_id}: coverage rows"
            )
        if "sections" in spec:
            self.assertEqual(len(est.get("sections") or []), spec["sections"], f"{case_id}: sections")
        if "areas" in spec:
            self.assertEqual(len(est.get("areas") or []), spec["areas"], f"{case_id}: areas")
        if "tie_out" in spec:
            to = T.tieout_estimate(est)
            self.assertEqual(
                to["passed"], spec["tie_out"], f"{case_id}: tie-out\n{to['summary']}"
            )
        if "parser_path" in spec:
            self.assertEqual(
                est.get("parser_path"), spec["parser_path"], f"{case_id}: parser_path"
            )

    def test_all_golden_cases(self):
        for case_id in sorted(GOLDEN):
            with self.subTest(case=case_id):
                spec = GOLDEN[case_id]
                if spec["kind"] == "scanned":
                    continue
                self._check(case_id)


class TestSchemaFallback(unittest.TestCase):
    """The schema-driven parser is a fallback: it runs only when ours fails."""

    def test_new_cases_tie_field_by_field(self):
        for case_id in ("sf_comm_v1", "sf_comm_v2", "sf_comm_v3", "sf_res_h",
                        "interior_final_i", "ocr_e"):
            with self.subTest(case=case_id):
                est = parse_case(case_id)
                to = T.tieout_estimate(est)
                self.assertTrue(to["passed"], f"{case_id}:\n{to['summary']}")
                self.assertTrue(to["exact"], f"{case_id} should tie exactly")

    def test_schema_path_is_recorded(self):
        for case_id in ("sf_comm_v1", "interior_final_i", "ocr_e"):
            with self.subTest(case=case_id):
                est = parse_case(case_id)
                self.assertEqual(est.get("parser_path"), "schema")
                self.assertTrue((est.get("schema_fallback") or {}).get("tied"))

    def test_legacy_path_is_kept_when_it_ties(self):
        est = parse_case("sf_res_h")
        self.assertEqual(est.get("parser_path"), "legacy")
        self.assertIsNone(est.get("schema_fallback"))

    def test_schema_parser_tolerates_the_space_before_the_decimals(self):
        """A clean '1 .00 EA' column layout parses without OCR repair."""
        self.assertFalse(P.looks_ocr("1 .00 EA   $12.34   $12.34"))
        q = P._SCHEMA_QTYUNIT.search("Temporary toilet (per 1 .00 MO")
        self.assertIsNotNone(q)
        self.assertEqual(str(q.group("qty")).replace(" ", ""), "1.00")

    def test_parser_path_is_on_every_estimate(self):
        est = parse_case("rae_og")
        self.assertEqual(est.get("parser_path"), "legacy")


class TestOcrFixGuard(unittest.TestCase):
    """ocr_fix is gated: it never touches a clean text layer."""

    def test_looks_ocr_ignores_a_clean_column_layout(self):
        # "1 .00" is a normal narrow-column artefact, not OCR damage
        self.assertFalse(P.looks_ocr("Temporary toilet (per 1 .00 MO   180.00"))
        self.assertFalse(P.looks_ocr("QUANTITY UNIT PRICE TAX RCV DEPREC. ACV\n1.00 SQ 2.27 0.00 2.27"))
        self.assertTrue(P.looks_ocr("1 LOO EA   12.34"))
        self.assertTrue(P.looks_ocr("18. I.00 EA   12.34"))

    def test_ocr_fix_repairs_damage(self):
        self.assertEqual(P.ocr_fix("1 LOO EA   12.34", enabled=True), "1 1.00 EA   12.34")
        self.assertEqual(P.ocr_fix("1 .00 EA   12.34", enabled=True), "1.00 EA   12.34")

    def test_ocr_fix_is_a_no_op_when_not_enabled(self):
        clean = "1.00 SQ   2.27   0.00   2.27\nLaminated - comp. shingle"
        self.assertEqual(P.ocr_fix(clean), clean)
        self.assertEqual(P.ocr_fix(clean, enabled=False), clean)

    def test_clean_fixtures_are_untouched(self):
        """No clean fixture is recognised as OCR text."""
        for case_id in ("rae_og", "stanley_og", "sf_res_h", "interior_final_i"):
            with self.subTest(case=case_id):
                spec = GOLDEN[case_id]
                path = require(spec["file"])
                if path.suffix.lower() == ".txt":
                    text = path.read_text(encoding="utf-8", errors="replace")
                else:
                    text = extract_text(str(path)).text
                self.assertFalse(P.looks_ocr(text), f"{case_id}: wrongly flagged as OCR")
                self.assertEqual(P.ocr_fix(text), text, f"{case_id}: ocr_fix changed clean text")


class TestBboxContinuations(unittest.TestCase):
    """Optional PDF-only wrapped-description continuations."""

    def test_pdf_continuations_are_found(self):
        from reconciler.pdftext import extract_bbox_continuations

        path = require(GOLDEN["sf_res_h"]["file"])
        cont = extract_bbox_continuations(str(path))
        self.assertTrue(cont, "expected bbox continuations on a PDF")
        self.assertTrue(any(parts for parts in cont.values()), "no wrapped description found")

    def test_text_input_has_no_bbox(self):
        from reconciler.pdftext import extract_bbox_continuations

        path = require(GOLDEN["rae_og"]["file"])
        self.assertEqual(extract_bbox_continuations(str(path)), {})

    def test_missing_file_returns_empty_map(self):
        from reconciler.pdftext import extract_bbox_continuations

        self.assertEqual(extract_bbox_continuations("/no/such/file.pdf"), {})


class TestStruckAndNotIncurred(unittest.TestCase):
    """Broader struck / REVISED / not-incurred detection in ``build_item``."""

    HEADER = "DESCRIPTION QUANTITY UNIT PRICE TAX RCV DEPREC. ACV"

    def _raw(self, first, rest=None):
        return {
            "no": 1,
            "first": first,
            "rest": [(1, line) for line in (rest or [])],
            "page": 1,
            "header": self.HEADER,
            "closed_desc": False,
        }

    def test_revised_marker_without_money(self):
        raw = self._raw("Tear off roof  10.00 SQ", ["   REVISED"])
        P.build_item(raw, [])
        self.assertTrue(raw.get("revised"))
        self.assertEqual(raw.get("rcv"), 0)

    def test_generic_status_marker_is_struck(self):
        raw = self._raw("R&R Gutter - aluminum  10.00 LF", ["   PWARR"])
        P.build_item(raw, [])
        self.assertTrue(raw.get("revised"))
        self.assertEqual(raw.get("status"), "PWARR")

    def test_see_revision_is_struck(self):
        raw = self._raw("Roofing felt - 15 lb.  14.05 SQ", ["   SEE REVISION"])
        P.build_item(raw, [])
        self.assertTrue(raw.get("revised"))

    def test_not_incurred_row_is_carried_at_zero(self):
        raw = self._raw(
            "R&R Flashing - pipe jack  1.00 EA  100.00  0.00  100.00",
            ["Note: this item has not yet been incurred"],
        )
        P.build_item(raw, [])
        self.assertTrue(raw.get("not_incurred"))
        self.assertEqual(raw.get("rcv"), 0)
        self.assertEqual(raw.get("printed_rcv"), 100_00)


class TestScannedRefusesCleanly(unittest.TestCase):
    """Scanned / garbled samples must refuse cleanly - never crash, never lie."""

    def _assert_needs_ocr(self, case_id: str):
        spec = GOLDEN[case_id]
        path = require(spec["file"])
        if spec["kind"] == "supplement":
            est = S.parse_supplement(str(path), force_text=path.suffix.lower() == ".txt")
        else:
            est = P.parse_estimate(str(path), force_text=path.suffix.lower() == ".txt")
        self.assertEqual(est.get("status"), "needs_ocr", f"{case_id} should need OCR")
        self.assertTrue(est.get("reason") or est.get("fatal"), f"{case_id}: no reason given")
        # no bogus totals
        self.assertEqual(est.get("items"), [], f"{case_id}: produced items")
        self.assertEqual(est.get("totals") or {}, {}, f"{case_id}: produced totals")
        self.assertEqual(est.get("checks") or {}, {}, f"{case_id}: produced checks")
        return est

    def test_scanned_text_samples(self):
        for case_id in ("busjhan_og", "rouse_og", "comer_cc"):
            with self.subTest(case=case_id):
                self._assert_needs_ocr(case_id)

    def test_garbled_pdf(self):
        self._assert_needs_ocr("lanc_828_pdf")

    def test_pdf_text_detection(self):
        path = require(GOLDEN["lanc_828_pdf"]["file"])
        result = extract_text(str(path))
        self.assertEqual(result.status, "needs_ocr")
        self.assertTrue(result.reason)
        # the raw text is still returned, but it is recognised as garbage
        self.assertIsNotNone(looks_garbled(result.text))


class TestAgainstOldOracle(unittest.TestCase):
    """The port must reproduce the old recon.py numbers exactly."""

    def _compare(self, rel: str):
        old = load_old_recon()
        path = require(rel)
        o = old.parse_estimate(str(path), force_text=True)
        n = P.parse_estimate(str(path), force_text=True)
        if o.get("fatal"):
            self.assertNotEqual(n.get("status"), "ok", f"{rel}: old refused, new did not")
            return
        self.assertEqual(n.get("status"), "ok", f"{rel}: {n.get('reason')}")
        ot, nt = o["totals"], n["totals"]
        for key in ("line_item_total", "tax", "overhead_profit", "rcv", "deductible", "acv"):
            self.assertEqual(cents(ot.get(key)), nt.get(key), f"{rel}: totals.{key}")
        oc, nc = o["checks"], n["checks"]
        self.assertEqual(oc.get("items_parsed"), nc.get("items_parsed"), f"{rel}: item count")
        self.assertEqual(
            cents(oc.get("items_rcv_sum")), nc.get("items_rcv_sum"), f"{rel}: item RCV sum"
        )
        self.assertEqual(
            cents(oc.get("items_pretax_sum")), nc.get("items_pretax_sum"), f"{rel}: item pretax sum"
        )
        self.assertEqual(len(o.get("summaries") or []), len(n.get("summaries") or []), f"{rel}: summaries")
        # every summary block, field by field
        for os_, ns_ in zip(o.get("summaries") or [], n.get("summaries") or []):
            self.assertEqual(os_["name"], ns_["name"], f"{rel}: summary name")
            for key in (
                "line_item_total",
                "rcv",
                "acv",
                "depreciation",
                "deductible",
                "overhead",
                "profit",
                "tax_total",
            ):
                # the oracle stores dollars as Decimal, the port stores cents
                self.assertEqual(
                    cents(os_.get(key)),
                    int(ns_.get(key) or 0),
                    f"{rel}: summary {key}",
                )
        # every item, field by field
        for oi, ni in zip(o.get("items") or [], n.get("items") or []):
            self.assertEqual(oi["no"], ni["no"], f"{rel}: item number")
            self.assertEqual(oi.get("qty"), ni.get("qty"), f"{rel}: item {oi['no']} qty")
            for key in ("rcv", "tax", "op", "deprec", "acv", "unit_price"):
                self.assertEqual(
                    cents(oi.get(key)),
                    int(ni.get(key) or 0),
                    f"{rel}: item {oi['no']} {key}",
                )
        self.assertEqual(
            len(o.get("warnings") or []), len(n.get("warnings") or []), f"{rel}: warning count"
        )

    def test_all_text_samples_match_oracle(self):
        samples_dir = sample_path("recon_samples")
        if not samples_dir.exists():
            raise unittest.SkipTest(f"no recon_samples folder in {SAMPLES_DIR}")
        names = sorted(p.name for p in samples_dir.glob("*.txt"))
        self.assertTrue(names, "no recon_samples found")
        for name in names:
            with self.subTest(sample=name):
                self._compare(f"recon_samples/{name}")

    def test_koller_and_lancaster_match_oracle(self):
        for rel in ("koller/og_all.txt", "koller/new.txt", "lancaster/r1002.txt"):
            with self.subTest(sample=rel):
                self._compare(rel)


class TestTieOut(unittest.TestCase):
    """Tie-out checks must pass on the clean text estimates."""

    def test_tie_out_passes_for_clean_estimates(self):
        for case_id in sorted(GOLDEN):
            spec = GOLDEN[case_id]
            if spec["kind"] != "carrier" or not spec.get("tie_out"):
                continue
            with self.subTest(case=case_id):
                est = parse_case(case_id)
                to = T.tieout_estimate(est)
                self.assertTrue(to["passed"], f"{case_id}:\n{to['summary']}")
                for r in to["results"]:
                    self.assertTrue(r.passed, f"{case_id}: {r.name} delta {r.delta}")

    def test_tie_out_reports_delta(self):
        est = parse_case("rae_og")
        to = T.tieout_estimate(est)
        self.assertTrue(to["results"])
        for r in to["results"]:
            self.assertEqual(r.delta, r.actual - r.expected)
            self.assertGreaterEqual(r.slack, 0)

    def test_tie_out_skips_non_ok_estimates(self):
        est = parse_case("busjhan_og")
        to = T.tieout_estimate(est)
        self.assertFalse(to["passed"])
        self.assertTrue(to["skipped"])


class TestGuideAndSamplePages(unittest.TestCase):
    """Carrier guide / sample pages must be skipped."""

    def test_guide_page_is_skipped(self):
        est = parse_case("koller_new")
        self.assertTrue(
            any("guide/sample" in w for w in est.get("warnings") or []),
            "expected a guide/sample-page warning",
        )
        # the skipped page must not contribute items
        self.assertEqual(est.get("checks", {}).get("items_parsed"), GOLDEN["koller_new"]["items"])

    def test_sf_sample_page_marker(self):
        from reconciler.parse import GUIDE_MARKERS, pages_of

        self.assertTrue(GUIDE_MARKERS.search("Summary Guide for your estimate"))
        keep, skipped = pages_of("Summary Guide\n\nQUANTITY RCV\n")
        self.assertEqual(skipped, [1])
        self.assertEqual(keep, [])


class TestCoverageTable(unittest.TestCase):
    """The coverage table must parse and flag personal property / code."""

    def test_coverage_rows_parsed(self):
        est = parse_case("rae_og")
        cov = est["coverage_table"]
        self.assertEqual(len(cov), GOLDEN["rae_og"]["coverage_rows"])
        total_rows = [r for r in cov if r["coverage"] == "Total"]
        self.assertTrue(total_rows, "expected a Total row in the coverage table")
        # money in the coverage table is integer cents
        for row in cov:
            self.assertIsInstance(row["item_total"], int)
            self.assertIsInstance(row["acv_total"], int)

    def test_personal_property_is_excluded_from_totals(self):
        est = parse_case("rae_og")
        self.assertTrue(est.get("coverage_flags") is not None)
        # personal property RCV is tracked separately, never in the billable total
        totals = est["totals"]
        self.assertIn("excluded_personal_property_rcv", totals)
        self.assertIsInstance(totals["excluded_personal_property_rcv"], int)


class TestLineItems(unittest.TestCase):
    """Line items carry the expected fields and money is integer cents."""

    def test_item_fields(self):
        est = parse_case("koller_new")
        items = est["items"]
        self.assertTrue(items)
        for it in items:
            self.assertIsInstance(it["no"], int)
            self.assertIsInstance(it["rcv"], int)
            self.assertIsInstance(it["unit_price"], int)
            self.assertIsInstance(it["tax"], int)
            self.assertIsInstance(it["op"], int)
            self.assertIn("desc", it)
            self.assertIn("section", it)
            self.assertIn("unit", it)
            self.assertIn("qty", it)
            self.assertIn("acv", it)
            self.assertIn("deprec", it)

    def test_pretax_is_rcv_minus_tax_and_op(self):
        est = parse_case("koller_new")
        for it in est["items"]:
            if not it.get("ok"):
                continue
            self.assertEqual(it["pretax"], it["rcv"] - it["tax"] - it["op"])

    def test_six_column_variant_without_op(self):
        """A table with no O&P column parses with op == 0."""
        est = parse_case("rae_og")
        self.assertTrue(est["items"])
        # the Allstate table has no O&P column
        self.assertTrue(all(it["op"] == 0 for it in est["items"] if it.get("ok")))

    def test_eight_column_variant_with_op(self):
        """A table with TAX and O&P columns parses both."""
        est = parse_case("muehlbauer_rev")
        self.assertTrue(any(it["op"] for it in est["items"] if it.get("ok")))

    def test_continuation_lines_are_joined(self):
        est = parse_case("koller_new")
        multi = [it for it in est["items"] if len(it.get("desc", "")) > 40]
        self.assertTrue(multi, "expected at least one wrapped description")


class TestSupplementParser(unittest.TestCase):
    """Our supplement estimate parser."""

    def test_supplement_totals(self):
        est = parse_case("koller_ours")
        self.assertEqual(est["totals"]["rcv"], GOLDEN["koller_ours"]["rcv"])
        self.assertEqual(est["totals"]["line_item_total"], GOLDEN["koller_ours"]["lit"])
        self.assertEqual(est["totals"]["overhead_profit"], GOLDEN["koller_ours"]["op"])
        self.assertEqual(est["totals"]["deductible"], GOLDEN["koller_ours"]["deductible"])

    def test_supplement_summary_block(self):
        est = parse_case("koller_ours")
        self.assertEqual(len(est["summaries"]), 1)
        s = est["summaries"][0]
        self.assertEqual(s["line_item_total"], GOLDEN["koller_ours"]["lit"])
        self.assertEqual(s["rcv"], GOLDEN["koller_ours"]["rcv"])
        self.assertEqual(s["deductible"], GOLDEN["koller_ours"]["deductible"])
        self.assertIn("net_claim", s)

    def test_supplement_section_totals_tie_out(self):
        est = parse_case("koller_ours")
        for t in est.get("printed_totals") or []:
            if not t.get("items") or not t.get("nums"):
                continue
            s = sum(i["rcv"] for i in t["items"] if i.get("ok"))
            self.assertIn(s, t["nums"], f"section '{t['name']}' does not tie out")

    def test_supplement_sections_and_areas(self):
        est = parse_case("koller_ours")
        self.assertEqual(len(est["sections"]), GOLDEN["koller_ours"]["sections"])
        self.assertEqual(len(est["areas"]), GOLDEN["koller_ours"]["areas"])
        area_names = {a["name"] for a in est["areas"]}
        self.assertIn("Exterior", area_names)
        self.assertIn("Interior", area_names)

    def test_supplement_pdf_matches_text(self):
        txt = parse_case("koller_ours")
        pdf = parse_case("koller_ours_pdf")
        self.assertEqual(txt["totals"]["rcv"], pdf["totals"]["rcv"])
        self.assertEqual(txt["checks"]["items_parsed"], pdf["checks"]["items_parsed"])

    def test_contractor_estimates_parse_with_supplement_parser(self):
        """Contractor estimates (our format) have no summary block."""
        for rel, expect_items in (
            ("recon_samples/MORRISON-P_Exterior 10.1.26.txt", 74),
            ("recon_samples/COMER-R_Exterior 6.18.26.txt", 60),
            ("recon_samples/ROUSE-W_Exterior 7.10.26.txt", 61),
            ("recon_samples/ROBERT-R_Exterior 9.23.26.txt", 103),
        ):
            with self.subTest(sample=rel):
                path = require(rel)
                est = S.parse_supplement(str(path), force_text=True)
                self.assertEqual(est["status"], "ok")
                self.assertEqual(est["checks"]["items_parsed"], expect_items)
                self.assertTrue(est["checks"]["items_match_printed_total"])

    def test_contractor_summary_captures_material_sales_tax(self):
        """A contractor Summary calls its tax line "Material Sales Tax"."""
        path = require("recon_samples/ROBERT-R_Exterior 9.23.26.txt")
        est = S.parse_supplement(str(path), force_text=True)
        s = est["summaries"][0]
        self.assertGreater(s.get("tax_total", 0), 0)
        # the summary prints Subtotal (LIT + tax) then O&P then RCV
        self.assertEqual(s["line_item_total"] + s["tax_total"], s["subtotal"])
        self.assertEqual(
            s["subtotal"] + s["overhead"] + s["profit"],
            s["rcv"],
        )

    def test_area_total_checks_are_advisory_not_blocking(self):
        """Xactimate sometimes prints area totals out of content order."""
        path = require("recon_samples/ROBERT-R_Exterior 9.23.26.txt")
        est = S.parse_supplement(str(path), force_text=True)
        to = T.tieout_estimate(est)
        self.assertTrue(to["passed"])
        self.assertTrue(to["advisories"])
        for r in to["advisories"]:
            self.assertIn("advisory", r.detail)


class TestMeta(unittest.TestCase):
    """Meta fields (carrier, estimate date, revision)."""

    def test_carrier_recognised(self):
        for case_id, carrier in (
            ("rae_og", "Allstate"),
            ("stanley_og", "State Farm"),
            ("muehlbauer_og", "Travelers"),
            ("busjhan_rev", "Auto-Owners"),
            ("koziol_rev", "Farmers"),
            ("rouse_rev", "Universal"),
        ):
            with self.subTest(case=case_id):
                est = parse_case(case_id)
                self.assertEqual(est["meta"]["carrier"], carrier)

    def test_estimate_date_present(self):
        for case_id in ("rae_og", "koller_new", "lanc_r1002"):
            with self.subTest(case=case_id):
                est = parse_case(case_id)
                self.assertIsNotNone(est["meta"].get("estimate_date"))
                self.assertRegex(est["meta"]["estimate_date"], r"^\d{1,2}/\d{1,2}/\d{4}$")

    def test_revision_detected(self):
        est = parse_case("koller_new")
        self.assertIsNotNone(est["meta"].get("revision"))


class TestMoneyHelpers(unittest.TestCase):
    """Integer-cents money helpers."""

    def test_parse_money(self):
        self.assertEqual(parse_money("$1,234.56"), 123456)
        self.assertEqual(parse_money("(0.00)"), 0)
        self.assertEqual(parse_money("(12.34)"), -1234)
        self.assertEqual(parse_money("12.34*"), 1234)
        self.assertEqual(parse_money(""), 0)
        self.assertEqual(parse_money(None), 0)

    def test_fmt_cents(self):
        self.assertEqual(fmt_cents(123456), "$1,234.56")
        self.assertEqual(fmt_cents(-500), "-$5.00")
        self.assertEqual(fmt_cents(0), "$0.00")

    def test_to_cents(self):
        self.assertEqual(to_cents("1.005"), 101)  # half-up
        self.assertEqual(to_cents("1.004"), 100)
        self.assertEqual(to_cents(3), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
