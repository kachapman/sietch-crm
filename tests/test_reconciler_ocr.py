#!/usr/bin/env python3
"""Tests for the PP-OCRv6 bridge: layout reconstruction, engine discovery and
the clean-refusal contract.  PaddleOCR itself is not needed for these tests -
the heavy end-to-end OCR run is gated in ``test_reconciler_g1.py``.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reconciler import ocr as O  # noqa: E402
from reconciler import pdftext as PT  # noqa: E402


def box(x0, y0, x1, y1, text, score=0.99):
    return [x0, y0, x1, y1, text, score]


class TestBoxesToText(unittest.TestCase):
    def test_one_row_is_one_line(self):
        page = {
            "page": 1,
            "boxes": [
                box(10, 10, 100, 30, "Drip edge"),
                box(200, 10, 260, 30, "275.14"),
                box(270, 10, 300, 30, "LF"),
            ],
        }
        text = O.boxes_to_text([page])
        self.assertEqual(text.count("\n"), 0)
        self.assertTrue(text.startswith("Drip edge"))
        self.assertIn("275.14", text)

    def test_wide_gap_becomes_column_separator(self):
        page = {
            "page": 1,
            "boxes": [
                box(10, 10, 100, 30, "Line Item Total"),
                box(600, 10, 700, 30, "614,027.56"),
            ],
        }
        line = O.boxes_to_text([page])
        self.assertIn("   ", line)
        # the parser's summary regex keys on the 2+ space column
        self.assertRegex(line, r"Line Item Total\s{2,}614,027\.56$")

    def test_vertical_jitter_stays_on_one_row(self):
        page = {
            "page": 1,
            "boxes": [
                box(10, 10, 100, 30, "Roof Surface"),
                box(200, 13, 260, 32, "1.00"),
            ],
        }
        self.assertEqual(O.boxes_to_text([page]).count("\n"), 0)

    def test_rows_are_ordered_top_to_bottom(self):
        page = {
            "page": 1,
            "boxes": [
                box(10, 100, 100, 120, "second"),
                box(10, 10, 100, 30, "first"),
            ],
        }
        self.assertEqual(O.boxes_to_text([page]), "first\nsecond")

    def test_wrapped_description_becomes_a_continuation_line(self):
        page = {
            "page": 1,
            "boxes": [
                box(10, 10, 300, 30, "1. Tear off, haul and dispose of comp. shingles -"),
                box(10, 34, 120, 54, "Laminated"),
                box(200, 34, 260, 54, "16.05"),
                box(270, 34, 300, 54, "SQ"),
            ],
        }
        lines = O.boxes_to_text([page]).splitlines()
        self.assertEqual(lines[0], "1. Tear off, haul and dispose of comp. shingles -")
        self.assertIn("Laminated", lines[1])

    def test_pages_are_form_feed_separated(self):
        pages = [
            {"page": 1, "boxes": [box(10, 10, 100, 30, "page one")]},
            {"page": 2, "boxes": [box(10, 10, 100, 30, "page two")]},
        ]
        text = O.boxes_to_text(pages)
        self.assertEqual(text, "page one\fpage two")

    def test_empty_page_does_not_crash(self):
        self.assertEqual(O.boxes_to_text([{"page": 1, "boxes": []}]), "")

    def test_blank_boxes_are_dropped(self):
        page = {"page": 1, "boxes": [box(10, 10, 20, 20, "   "), box(30, 10, 90, 20, "real")]}
        self.assertEqual(O.boxes_to_text([page]), "real")


class TestEngineDiscovery(unittest.TestCase):
    def tearDown(self):
        O._AVAILABLE_CACHE = None
        os.environ.pop("RECONCILER_OCR_PYTHON", None)
        os.environ.pop("RECONCILER_OCR", None)

    def test_bad_configured_python_is_unavailable_not_a_crash(self):
        os.environ["RECONCILER_OCR_PYTHON"] = "/nonexistent/python3"
        self.assertIsNone(O.ocr_python())
        ok, reason = O.ocr_available(refresh=True)
        self.assertFalse(ok)
        self.assertIn("PaddleOCR python", reason)

    def test_ocr_is_opt_in_by_default(self):
        os.environ.pop("RECONCILER_OCR", None)
        self.assertFalse(O.ocr_enabled())
        self.assertTrue(O.ocr_enabled(True))
        os.environ["RECONCILER_OCR"] = "1"
        self.assertTrue(O.ocr_enabled())

    def test_unavailable_engine_gives_a_clean_result(self):
        os.environ["RECONCILER_OCR_PYTHON"] = "/nonexistent/python3"
        O._AVAILABLE_CACHE = None
        res = O.ocr_pdf(__file__)
        self.assertFalse(res.ok)
        self.assertIn(res.status, ("unavailable", "error"))
        self.assertTrue(res.reason)

    def test_missing_file_is_an_error(self):
        res = O.ocr_pdf("/nonexistent/file.pdf")
        self.assertEqual(res.status, "error")
        self.assertIn("not found", res.reason)


class TestStructuredTextOverrulesGarbageHeuristic(unittest.TestCase):
    """Our own contractor estimate is full of drawing labels; it is not a scan."""

    HEADER = (
        "DESCRIPTION QUANTITY UNIT PRICE TAX O&P RCV DEPREC. ACV\n"
        "1. Tear off, haul and dispose rubber roofing 184.87 SQ 118.99 0.00 "
        "4,399.54 26,397.22 (0.00) 26,397.22\n"
    )

    def test_structure_detected(self):
        self.assertTrue(PT.has_line_item_structure(self.HEADER + "Line Item Totals:"))
        self.assertTrue(PT.has_line_item_structure(self.HEADER + "Replacement Cost Value"))

    def test_header_without_totals_is_not_enough(self):
        self.assertFalse(PT.has_line_item_structure(self.HEADER))

    def test_garbage_without_structure_is_still_garbage(self):
        noise = "0124250672489 50 " + "x" * 500
        self.assertFalse(PT.has_line_item_structure(noise))
        self.assertIsNotNone(PT.looks_garbled(noise))

    def test_structured_text_survives_the_letter_ratio_check(self):
        # 300 drawing-label characters, 100 letters: below the 20% ratio, but
        # the line-item structure proves the page is readable.
        body = self.HEADER + "Line Item Totals:\n" + ("1 2 3 . 4 5 " * 40)
        self.assertIsNotNone(PT.looks_garbled("1 2 3 . " * 200))
        self.assertIsNone(PT.looks_garbled(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
