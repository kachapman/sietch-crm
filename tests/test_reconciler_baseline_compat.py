#!/usr/bin/env python3
"""Baseline compatibility checks for the Sietch reconciler.

Synthetic fixtures only. No claim corpus, no network, no database.

The SHA-256 lock is the ee6e401 byte content of the parser modules. This
branch must not rewrite them. The tests named ``test_baseline_*`` describe
what that baseline does today, including gaps the Sherwood v30 patch owns.
They are not the target contract. When that patch is integrated, replace
those tests in the same commit instead of teaching them to keep the gap.

``pdftotext`` is not installed in this environment, so PDF extraction is
not executed. Text-layer behavior is what these tests run.
"""
from __future__ import annotations

import hashlib
import inspect
import shutil
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reconciler import __version__  # noqa: E402
from reconciler import diff as D  # noqa: E402
from reconciler import parse as P  # noqa: E402
from reconciler import pdftext  # noqa: E402
from reconciler import sietch_compat as C  # noqa: E402
from reconciler import supplement as S  # noqa: E402
from reconciler import tieout as T  # noqa: E402

# File bytes at ee6e40145c2329c81be3aa43c7b304d51428cd21. Not git blob ids.
BASELINE_SHA256 = {
    "reconciler/__init__.py": "40fa89294df10304bde4e406cf7b7fd1a3ed04888d862bbb0237a5f06caeb47b",
    "reconciler/commission.py": "fb74a82af39889925e5f3960cc1793b6fc739a891bcf075ad776d8524e064010",
    "reconciler/diff.py": "e8ad8c9435c08b4173eee772649366f48a70668f6ef8ddfb34cb6774e8844cc7",
    "reconciler/money.py": "4700737d1d3736649fa33c42e564b674fe3dea22f5c37c8b95154ae551507f45",
    "reconciler/notes.py": "f0f8af83c3d6f230b4b243f14055d59c1881530d14335693483c99c5bc15820f",
    "reconciler/parse.py": "5957c0d4026998a34fa89710a84e4a863e058147fc619d982755e40c2cf0e33c",
    "reconciler/pdftext.py": "3027c64851dd94be9e174f6648e52c447f9f6317c5337ed4ce9f9112c5486e73",
    "reconciler/supplement.py": "48fce70e6bcc40aed96ed97369537b9c217e8244114ba73f6bf42a5095d6e232",
    "reconciler/tieout.py": "f447a0053b704fc8c4c0f2ccd7734696f567cdd1a1fa52ed8aac5fc19aba60b5",
}

_PAD = "Synthetic fixture only. No claim data. " * 6


def _native_carrier() -> str:
    return f"""
Insured: SYNTHETIC PERSON
Claim Number: SYN-00001
Allstate
1/2/2026  Page: 1

DESCRIPTION                    QUANTITY    UNIT PRICE    TAX      RCV       DEPREC.     ACV
1. Synthetic shingle course    10.00 SQ    100.00        0.00     1,000.00  (0.00)      1,000.00
Totals: Roof                   0.00        1,000.00      0.00      1,000.00
Line Item Totals:              0.00        1,000.00      0.00      1,000.00

Summary for Dwelling
Line Item Total                                          1,000.00
Overhead                                                     0.00
Profit                                                       0.00
Replacement Cost Value                                   1,000.00
Actual Cash Value                                        1,000.00
{_PAD}
"""


def _alternate_header(with_summary: bool) -> str:
    summary = ""
    if with_summary:
        summary = """
Summary for Dwelling
Line Item Total                                          1,000.00
Replacement Cost Value                                   1,000.00
"""
    return f"""
Insured: SYNTHETIC PERSON
Claim Number: SYN-00002
1/2/2026  Page: 1

DESCRIPTION          QTY     REMOVE     REPLACE     TOTAL
1. Synthetic shingle 10.00   200.00     800.00      1,000.00
Line Item Totals:    1,000.00
{summary}
{_PAD}
"""


def _native_supplement() -> str:
    return f"""
Client: SYNTHETIC PERSON
File Number: SYN-00003
Estimate: SYN-EST
Type of Estimate: Supplement
Date Entered: 1/2/2026
1/2/2026 Page: 1

DESCRIPTION                    QUANTITY    UNIT PRICE    TAX    O&P     RCV      DEPREC.    ACV
Exterior
     Roof
1. Synthetic shingle course    10.00 SQ    100.00        0.00   0.00    1,000.00 (0.00)    1,000.00
Totals: Roof                   0.00  0.00  1,000.00  0.00  1,000.00
Line Item Totals: SYN          0.00  0.00  1,000.00  0.00  1,000.00

Summary
Line Item Total                                          1,000.00
Replacement Cost Value                                   1,000.00
Actual Cash Value                                        1,000.00
{_PAD}
"""


def _gapped_numbers() -> str:
    return f"""
Insured: SYNTHETIC PERSON
Claim Number: SYN-00004
Allstate
1/2/2026  Page: 1

DESCRIPTION                    QUANTITY    UNIT PRICE    TAX      RCV       DEPREC.     ACV
1. Synthetic shingle course    10.00 SQ    100.00        0.00     1,000.00  (0.00)      1,000.00
3. Synthetic drip edge         10.00 LF     10.00        0.00       100.00  (0.00)        100.00
Totals: Roof                   0.00        1,100.00
Line Item Totals:              0.00        1,100.00

Summary for Dwelling
Line Item Total                                          1,100.00
Replacement Cost Value                                   1,100.00
Actual Cash Value                                        1,100.00
{_PAD}
"""


def _printed_total_mismatch() -> str:
    return f"""
Insured: SYNTHETIC PERSON
Claim Number: SYN-00005
Allstate
1/2/2026  Page: 1

DESCRIPTION                    QUANTITY    UNIT PRICE    TAX      RCV       DEPREC.     ACV
1. Synthetic shingle course    10.00 SQ    100.00        0.00     1,000.00  (0.00)      1,000.00
Totals: Roof                   0.00        1,000.00
Line Item Totals:              0.00        1,500.00

Summary for Dwelling
Line Item Total                                          1,000.00
Replacement Cost Value                                   1,000.00
Actual Cash Value                                        1,000.00
{_PAD}
"""


class TestBaselineIdentity(unittest.TestCase):
    def test_package_version_and_waiting_flag(self):
        self.assertEqual(__version__, "0.2.0")
        rec = C.baseline_record()
        self.assertEqual(rec["version"], "0.2.0")
        self.assertEqual(rec["sietch_baseline_commit"], C.SIETCH_BASELINE_COMMIT)
        self.assertFalse(rec["sherwood_baseline_reviewed"])
        self.assertFalse(rec["ported_sherwood_delta"])
        self.assertEqual(rec["v30_status"], "waiting_on_tumnus_patch")
        self.assertIn("alternate-table header dispatch", " ".join(rec["v30_owned_work"]))
        self.assertEqual(rec["reported_divergent_files"], ["parse.py", "pdftext.py", "diff.py"])
        self.assertEqual(rec["reported_supplement"], "upstream-only")

    def test_parser_modules_match_ee6e401_bytes(self):
        for rel, expect in BASELINE_SHA256.items():
            digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
            self.assertEqual(digest, expect, rel)

    def test_sietch_compat_is_not_exported_from_package_all(self):
        # Keep the comparable public export list identical to ee6e401.
        import reconciler

        self.assertNotIn("sietch_compat", reconciler.__all__)
        self.assertNotIn("ESX", (ROOT / "reconciler" / "parse.py").read_text())
        self.assertNotIn("xml.etree", (ROOT / "reconciler" / "parse.py").read_text())
        self.assertNotIn("zipfile", (ROOT / "reconciler" / "parse.py").read_text())

    def test_public_signatures_are_unchanged(self):
        sigs = C.public_signatures()
        # Annotations stay strings because the modules use
        # ``from __future__ import annotations``.
        self.assertEqual(
            sigs["parse_estimate"],
            "(path: 'str', force_text: 'bool' = False) -> 'Dict'",
        )
        self.assertEqual(
            sigs["parse_text"],
            "(text: 'str', path: 'str' = '<text>', bbox: 'Optional[Dict]' = None, "
            "allow_schema: 'bool' = True) -> 'Dict'",
        )
        self.assertEqual(
            sigs["parse_supplement"],
            "(path: 'str', force_text: 'bool' = False) -> 'Dict'",
        )
        self.assertEqual(
            sigs["parse_supplement_text"],
            "(text: 'str', path: 'str' = '<supplement>') -> 'Dict'",
        )
        self.assertEqual(
            sigs["diff_items"],
            "(old: 'List[Dict]', new: 'List[Dict]') -> 'Dict'",
        )
        self.assertEqual(sigs["diff_summary"], "(d: 'Dict') -> 'Dict'")
        # The parameters are still positional-or-keyword, not keyword-only.
        for fn in (P.parse_estimate, P.parse_text, S.parse_supplement, D.diff_items):
            for param in inspect.signature(fn).parameters.values():
                self.assertIs(param.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_hook_does_not_decide_final_acceptance(self):
        signals = C.acceptance_signals({"status": "ok", "items": [{"ok": True}], "checks": {}})
        self.assertEqual(signals["final_acceptance"], "not_decided_waiting_on_v30")
        self.assertEqual(signals["completeness_gate"], "not_implemented")
        self.assertFalse(signals["sum_tie_detects_offsetting_omissions"])
        self.assertEqual(C.OCR_HANDOFF_SPEC["implemented"], False)
        self.assertIsNone(C.OCR_HANDOFF_SPEC["worker"])
        self.assertIsNone(C.OCR_HANDOFF_SPEC["queue"])
        self.assertFalse(C.OCR_HANDOFF_SPEC["cloud_fallback"])
        self.assertFalse(C.OCR_HANDOFF_SPEC["model_inference"])
        self.assertFalse(C.OCR_HANDOFF_SPEC["encrypted_xactdoc_decryption"])


class TestDeploymentGap(unittest.TestCase):
    def test_image_does_not_copy_reconciler(self):
        docker = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("COPY reconciler", docker)
        self.assertIn("poppler-utils", docker)
        gap = C.deployment_gap()
        self.assertFalse(gap["dockerfile_copies_reconciler"])
        self.assertTrue(gap["poppler_utils_installed_in_image"])
        self.assertFalse(gap["estimate_parser_route"])

    def test_server_full_reconcile_is_not_the_estimate_parser(self):
        server = (ROOT / "server.py").read_text()
        self.assertNotIn("reconciler", server)
        self.assertNotIn("parse_estimate", server)
        self.assertIn("/api/v2/admin/sync/full-reconcile", server)
        self.assertFalse(C.deployment_gap()["server_imports_reconciler"])
        self.assertFalse(C.deployment_gap()["crm_full_reconcile_is_estimate_parser"])


class TestSupplementCoupling(unittest.TestCase):
    def test_supplement_shares_carrier_header_objects(self):
        coupling = C.supplement_coupling()
        self.assertTrue(coupling["all_shared"], coupling["same_object"])
        self.assertIs(S.HEADER_RE, P.HEADER_RE)
        self.assertIs(S.QTY_RE, P.QTY_RE)

    def test_native_supplement_text_still_parses(self):
        est = S.parse_supplement_text(_native_supplement(), path="<synthetic-supplement>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["kind"], "supplement")
        self.assertEqual(est["checks"]["items_parsed"], 1)
        self.assertTrue(est["checks"]["items_match_printed_total"])
        self.assertEqual(est["items"][0]["rcv"], 100_000)
        self.assertTrue(T.tieout_estimate(est)["passed"])

    def test_baseline_alternate_header_drops_supplement_summary(self):
        """Missing QUANTITY/RCV returns before the Summary block is read.

        BASELINE GAP. A v30 header dispatcher that only wraps the carrier
        parser leaves this path in place unless ``HEADER_RE`` itself changes,
        because the supplement parser uses that same object and returns early.
        """
        text = _alternate_header(with_summary=True) + """
Summary
Line Item Total                                          1,000.00
Replacement Cost Value                                   1,000.00
"""
        est = S.parse_supplement_text(text, path="<synthetic-alt-supplement>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est.get("items"), [])
        self.assertEqual(est.get("summaries"), [])
        self.assertEqual(est.get("totals"), {})
        self.assertTrue(any("no line-item table header" in w for w in est["warnings"]))
        signals = C.acceptance_signals(est)
        self.assertTrue(signals["header_not_recognized"])
        self.assertEqual(signals["final_acceptance"], "not_decided_waiting_on_v30")


class TestNativeTextUntouched(unittest.TestCase):
    def test_standard_header_parses_and_ocr_fix_is_not_applied(self):
        text = _native_carrier()
        self.assertFalse(P.looks_ocr(text))
        self.assertEqual(P.ocr_fix(text), text)
        est = P.parse_text(text, path="<synthetic-native>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["parser_path"], "legacy")
        self.assertIsNone(est.get("schema_fallback"))
        self.assertFalse(est["ocr_fix_applied"])
        self.assertEqual(est["checks"]["items_parsed"], 1)
        self.assertTrue(est["checks"]["items_match_printed_total"])
        item = est["items"][0]
        self.assertEqual(item["desc"], "Synthetic shingle course")
        self.assertEqual(item["qty"], Decimal("10.00"))
        self.assertEqual(item["unit"], "SQ")
        self.assertEqual(item["unit_price"], 10_000)
        self.assertEqual(item["rcv"], 100_000)
        self.assertEqual(item["acv"], 100_000)
        self.assertTrue(T.tieout_estimate(est)["passed"])
        signals = C.acceptance_signals(est)
        self.assertEqual(signals["final_acceptance"], "not_decided_waiting_on_v30")


class TestBaselineAlternateHeader(unittest.TestCase):
    def test_qty_remove_replace_total_is_not_a_header(self):
        line = "DESCRIPTION          QTY     REMOVE     REPLACE     TOTAL"
        self.assertIsNone(P.HEADER_RE.search(line))
        self.assertIsNone(P.header_schema(line))
        self.assertIsNotNone(
            P.header_schema("DESCRIPTION QUANTITY UNIT PRICE TAX RCV DEPREC. ACV")
        )

    def test_baseline_alternate_header_without_summary_tieout_passes_empty(self):
        """No QUANTITY/RCV header, no summary: zero items, status ok, tie-out passes.

        BASELINE GAP. The empty row-math check is enough for tieout_estimate
        to pass, and the schema fallback therefore does not run. This is the
        false-accept shape. It is not a reproduction of any corpus count.
        """
        est = P.parse_text(_alternate_header(False), path="<synthetic-alt>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["checks"]["items_parsed"], 0)
        self.assertEqual(est["items"], [])
        self.assertIsNone(est.get("schema_fallback"))
        self.assertIsNone(est["checks"].get("items_match_printed_total"))
        self.assertTrue(T.tieout_estimate(est)["passed"])
        self.assertTrue(any("no line-item table header" in w for w in est["warnings"]))
        signals = C.acceptance_signals(est)
        self.assertTrue(signals["header_not_recognized"])
        self.assertEqual(signals["item_count"], 0)
        self.assertEqual(signals["completeness_gate"], "not_implemented")

    def test_baseline_alternate_header_with_summary_stays_status_ok(self):
        """A summary makes tie-out fail, schema runs, and status is still ok.

        BASELINE GAP. Failure to tie does not change ``status``.
        """
        est = P.parse_text(_alternate_header(True), path="<synthetic-alt-summary>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["items"], [])
        self.assertTrue((est.get("schema_fallback") or {}).get("attempted"))
        self.assertFalse((est.get("schema_fallback") or {}).get("tied"))
        self.assertFalse(T.tieout_estimate(est)["passed"])
        self.assertEqual(C.acceptance_signals(est)["final_acceptance"], "not_decided_waiting_on_v30")


class TestBaselineTotalsGate(unittest.TestCase):
    def test_baseline_line_number_gap_is_a_warning_only(self):
        """A hole in 1..max is warned. Status stays ok and tie-out still passes.

        BASELINE GAP. The warning is not a completeness gate.
        """
        est = P.parse_text(_gapped_numbers(), path="<synthetic-gap>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["checks"]["items_parsed"], 2)
        self.assertTrue(est["checks"]["items_match_printed_total"])
        self.assertTrue(any("line numbers not parsed: [2]" in w for w in est["warnings"]))
        self.assertTrue(T.tieout_estimate(est)["passed"])
        self.assertTrue(C.acceptance_signals(est)["missing_line_numbers_warned"])

    def test_baseline_schema_fallback_can_drop_a_printed_mismatch(self):
        """Legacy sees item RCV $1,000 against a printed $1,500 total.

        With the default schema fallback, that printed figure is no longer in
        ``checks`` and tie-out passes. ``allow_schema=False`` keeps the
        mismatch and fails tie-out. Status is ok on both paths.

        BASELINE GAP. Owned by the v30 completeness/tie-out work. This does
        not show that an arbitrary missing line can be detected from a total.
        """
        text = _printed_total_mismatch()
        legacy = P.parse_text(text, path="<synthetic-mismatch>", allow_schema=False)
        self.assertEqual(legacy["parser_path"], "legacy")
        self.assertEqual(legacy["status"], "ok")
        self.assertFalse(legacy["checks"]["items_match_printed_total"])
        self.assertIn(150_000, legacy["checks"]["line_item_totals_printed"])
        self.assertFalse(T.tieout_estimate(legacy)["passed"])

        default = P.parse_text(text, path="<synthetic-mismatch>")
        self.assertEqual(default["status"], "ok")
        self.assertEqual(default["parser_path"], "schema")
        self.assertTrue((default.get("schema_fallback") or {}).get("tied"))
        self.assertIsNone(default["checks"].get("line_item_totals_printed"))
        self.assertTrue(T.tieout_estimate(default)["passed"])
        diff = D.diff_items(default["items"], default["items"])
        gate = T.draft_gate(default, default, diff)
        self.assertTrue(gate["allowed"])
        self.assertEqual(C.acceptance_signals(default)["completeness_gate"], "not_implemented")

    def test_offsetting_omissions_are_not_detected_by_a_matching_sum(self):
        """A tied sum still passes when the caller knows two lines were absent.

        The parser is not given those lines. The assertion is only that
        tie-out cannot see them, and that this package does not pretend it can.
        """
        est = {
            "status": "ok",
            "items": [
                {
                    "ok": True,
                    "no": 1,
                    "rcv": 100_000,
                    "pretax": 100_000,
                    "qty": Decimal("1"),
                    "unit_price": 100_000,
                    "tax": 0,
                    "op": 0,
                    "deprec": 0,
                    "acv": 100_000,
                }
            ],
            "checks": {"line_item_totals_printed": [100_000]},
            "summaries": [
                {
                    "name": "Dwelling",
                    "line_item_total": 100_000,
                    "rcv": 100_000,
                    "tax_total": 0,
                }
            ],
            "printed_totals": [],
        }
        omitted_positive = 5_000
        omitted_negative = -5_000
        self.assertEqual(omitted_positive + omitted_negative, 0)
        self.assertTrue(T.tieout_estimate(est)["passed"])
        self.assertFalse(C.acceptance_signals(est)["sum_tie_detects_offsetting_omissions"])
        self.assertFalse(C.acceptance_signals(est)["missing_line_numbers_warned"])


class TestBaselineClassification(unittest.TestCase):
    def test_short_text_is_needs_ocr_and_invents_nothing(self):
        est = P.parse_text("scan", path="<synthetic-short>")
        self.assertEqual(est["status"], "needs_ocr")
        self.assertTrue(est.get("fatal"))
        self.assertEqual(est.get("items"), [])
        self.assertEqual(est.get("totals"), {})
        self.assertIn(est["status"], C.EXISTING_PARSE_STATUSES)

    def test_looks_garbled_has_one_bucket_for_blank_sparse_and_non_estimate(self):
        blank = pdftext.looks_garbled("")
        sparse = pdftext.looks_garbled("abc")
        prose = pdftext.looks_garbled("roof photo sketch blank page " * 40)
        self.assertIn("no text layer", blank)
        self.assertIn("sparse", sparse)
        self.assertIn("does not look like a Xactimate estimate", prose)
        # No separate photo / sketch / blank / scanned-estimate status code.
        self.assertIsNone(pdftext.looks_garbled(
            "QUANTITY Line Item Totals Replacement Cost Value " + ("shingle " * 30)
        ))

    def test_parse_text_does_not_classify_a_long_non_estimate(self):
        """``looks_garbled`` runs on the PDF extract path, not in ``parse_text``.

        BASELINE GAP. A long caption with no table is status ok and zero items.
        """
        prose = "This is a synthetic photo caption of a roof sketch with no estimate table. " * 8
        self.assertGreaterEqual(len(prose), 200)
        self.assertNotIn("looks_garbled", inspect.getsource(P.parse_text))
        est = P.parse_text(prose, path="<synthetic-prose>")
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["items"], [])
        self.assertEqual(C.acceptance_signals(est)["scan_class_distinctions"], "not_implemented")

    def test_garbled_detection_is_document_level(self):
        """A blank second page does not produce a per-page OCR disposition.

        BASELINE GAP. ``extract_text`` can mark the whole document
        ``needs_ocr`` when half the pages are sparse. That branch is not
        executed here: pdftotext is absent. The source still shows the
        decision is one document status, not a page list.
        """
        good = "QUANTITY Line Item Totals Replacement Cost Value " + ("shingle " * 40)
        self.assertIsNone(pdftext.looks_garbled(good + "\f \n"))
        src = inspect.getsource(pdftext.extract_text)
        self.assertIn("too_few >= max(1, (len(page_stats) // 2))", src)
        self.assertIn('result.status = "needs_ocr"', src)
        self.assertNotIn("page_disposition", src)
        self.assertNotIn("ocr_needed_pages", src)
        self.assertFalse(hasattr(pdftext.PdfTextResult, "page_disposition"))
        self.assertIsNone(shutil.which("pdftotext"))

    def test_missing_pdftotext_is_an_explicit_error(self):
        if shutil.which("pdftotext"):
            self.skipTest("pdftotext is installed; the missing-binary path is not the one under test")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.pdf"
            path.write_bytes(b"%PDF-1.4 synthetic\n")
            est = P.parse_estimate(str(path))
        self.assertEqual(est["status"], "error")
        self.assertIn("pdftotext", est.get("reason") or "")
        self.assertEqual(est.get("items"), [])
        self.assertEqual(est.get("totals"), {})


class TestStructuredSourceNotInvented(unittest.TestCase):
    def _xml(self) -> str:
        return (
            "<?xml version='1.0'?>"
            "<XACTDOC><ITEM id='1' desc='Synthetic shingle' qty='10.00'/></XACTDOC>"
        )

    def test_xml_and_esx_and_zip_do_not_invent_items(self):
        """There is no structured route. Untrusted names are not extracted.

        BASELINE GAP for the ESX/XML entry work. Today's result is an explicit
        error from the PDF path (pdftotext is absent) or, if that binary
        exists, still no invented rows. A zip member named with ``..`` is not
        written.
        """
        xml = self._xml()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel_parent = root / "outside"
            sentinel_parent.mkdir()
            xml_path = root / "synthetic.xml"
            esx_path = root / "synthetic.esx"
            zip_path = root / "synthetic.zip"
            xml_path.write_text(xml, encoding="utf-8")
            esx_path.write_text(xml, encoding="utf-8")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../outside/should-not-be-written.txt", "synthetic")
                zf.writestr("synthetic-inner.xml", xml)
            for path in (xml_path, esx_path, zip_path):
                est = P.parse_estimate(str(path))
                self.assertNotEqual(est.get("status"), "ok", path.name)
                self.assertIn(est.get("status"), ("error", "needs_ocr"))
                self.assertEqual(est.get("items") or [], [])
                self.assertFalse(est.get("totals"))
                self.assertEqual(
                    C.acceptance_signals(est)["structured_source_route"],
                    "not_implemented",
                )
            self.assertFalse((sentinel_parent / "should-not-be-written.txt").exists())
            listed = list(sentinel_parent.iterdir())
            self.assertEqual(listed, [])

    def test_baseline_force_text_xml_is_status_ok_with_no_items(self):
        """Reading XML as text does not build rows, and it also does not refuse.

        BASELINE GAP. ``force_text`` bypasses both the PDF classifier and any
        future structured-format status.
        """
        xml = self._xml() + ("<NOTE>synthetic</NOTE>" * 30)
        self.assertGreaterEqual(len(xml), 200)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.esx"
            path.write_text(xml, encoding="utf-8")
            est = P.parse_estimate(str(path), force_text=True)
        self.assertEqual(est["status"], "ok")
        self.assertEqual(est["items"], [])
        self.assertTrue(any("no line-item table header" in w for w in est["warnings"]))
        self.assertEqual(C.acceptance_signals(est)["structured_source_route"], "not_implemented")

    def test_diff_review_list_is_not_a_parse_status(self):
        diff = D.diff_items([], [])
        self.assertIn("needs_review", diff)
        self.assertEqual(diff["needs_review_count"], 0)
        self.assertNotIn("needs_review", C.EXISTING_PARSE_STATUSES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
