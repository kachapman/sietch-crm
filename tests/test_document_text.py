#!/usr/bin/env python3
"""Offline tests for the shared text extractor (``document_text.py``).

Synthetic fixtures only (built by tests/fixtures/ocr/pdfgen.py). No database,
no network, no OCR engine, no real customer document.

Run with: python3 tests/test_document_text.py
"""

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "fixtures" / "ocr"))
sys.path.insert(0, str(HERE))

import network_guard  # noqa: E402

network_guard.install()

import document_text  # noqa: E402
from pdfgen import write_fixtures  # noqa: E402

FIXTURES: dict[str, Path] = {}
TMPDIR = None


def setUpModule():
    global TMPDIR
    TMPDIR = tempfile.mkdtemp(prefix="sietch-ocr-fixtures-")
    FIXTURES.update(write_fixtures(TMPDIR))


def tearDownModule():
    if TMPDIR:
        shutil.rmtree(TMPDIR, ignore_errors=True)


class TestDetectKind(unittest.TestCase):
    def test_extension_wins_over_octet_stream(self):
        self.assertEqual(document_text.detect_kind("a.PDF", "application/octet-stream"), "pdf")
        self.assertEqual(document_text.detect_kind("a.html", None), "html")
        self.assertEqual(document_text.detect_kind("a.eml", None), "eml")
        self.assertEqual(document_text.detect_kind("a.txt", None), "txt")

    def test_mime_fallback_for_extensionless(self):
        self.assertEqual(document_text.detect_kind("noext", "application/pdf"), "pdf")
        self.assertEqual(document_text.detect_kind("noext", "text/plain; charset=utf-8"), "txt")
        self.assertEqual(document_text.detect_kind("noext", "message/rfc822"), "eml")

    def test_images_and_binaries_are_unsupported(self):
        self.assertEqual(document_text.detect_kind("scan.png", "image/png"), "unsupported")
        self.assertEqual(document_text.detect_kind("x.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), "unsupported")
        self.assertEqual(document_text.detect_kind("x.zip", "application/zip"), "unsupported")


class TestPdfExtraction(unittest.TestCase):
    def test_digital_pdf_per_page(self):
        result = document_text.extract_document(
            FIXTURES["digital_pdf"], filename="synthetic-digital.pdf",
            mime_type="application/pdf")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["page_count"], 2)
        self.assertEqual((result["page_start"], result["page_end"]), (1, 2))
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["pages"]), 2)
        self.assertIn("Synthetic digital page one", result["pages"][0]["text"])
        self.assertIn("Synthetic digital page two", result["pages"][1]["text"])
        for page in result["pages"]:
            self.assertEqual(page["source"], "pdftotext")
            self.assertFalse(page["ocr_needed"])
            self.assertIsNone(page["ocr_status"])
            self.assertGreater(page["char_count"], 20)

    def test_scanned_pdf_marks_ocr_needed(self):
        result = document_text.extract_document(
            FIXTURES["scanned_pdf"], filename="synthetic-scanned.pdf",
            mime_type="application/pdf")
        self.assertEqual(result["status"], "ocr_pending")
        self.assertEqual(len(result["pages"]), 2)
        for page in result["pages"]:
            self.assertTrue(page["ocr_needed"])
            self.assertEqual(page["ocr_status"], "pending")
            self.assertLess(page["char_count"], document_text.OCR_NEEDED_MIN_PRINTABLE)

    def test_queue_state_is_reported_per_page(self):
        for state, expected in (("pending", "queued"),
                                ("in_progress", "in_progress")):
            result = document_text.extract_document(
                FIXTURES["scanned_pdf"], filename="synthetic-scanned.pdf",
                queue_status_lookup=lambda sha, s=state: s)
            self.assertEqual(result["pages"][0]["ocr_status"], expected)
            self.assertEqual(result["status"], "ocr_pending")

    def test_cache_text_wins_even_when_pdftotext_has_text(self):
        result = document_text.extract_document(
            FIXTURES["digital_pdf"], filename="synthetic-digital.pdf",
            cache_lookup=lambda sha, page: f"CACHED OCR PAGE {page}")
        self.assertEqual(result["status"], "ok")
        for page in result["pages"]:
            self.assertEqual(page["source"], "ocr_cache")
            self.assertEqual(page["ocr_status"], "cached")
            self.assertTrue(page["ocr_needed"])
        self.assertIn("CACHED OCR PAGE 1", result["text"])

    def test_page_range(self):
        result = document_text.extract_document(
            FIXTURES["digital_pdf"], filename="synthetic-digital.pdf",
            page_start=2, page_end=2)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual((result["page_start"], result["page_end"]), (2, 2))
        self.assertEqual([p["page"] for p in result["pages"]], [2])
        self.assertIn("page two", result["text"])

    def test_page_range_validation(self):
        with self.assertRaises(ValueError):
            document_text.extract_document(
                FIXTURES["digital_pdf"], filename="a.pdf", page_start=9, page_end=10)
        with self.assertRaises(ValueError):
            document_text.extract_document(
                FIXTURES["digital_pdf"], filename="a.pdf", page_start=2, page_end=1)

    def test_max_chars_truncates(self):
        result = document_text.extract_document(
            FIXTURES["digital_pdf"], filename="a.pdf", max_chars=10)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"]), 10)

    def test_sha256_is_of_file_bytes(self):
        sha = document_text.sha256_file(FIXTURES["digital_pdf"])
        self.assertEqual(len(sha), 64)
        self.assertEqual(sha, document_text.extract_document(
            FIXTURES["digital_pdf"], filename="a.pdf")["file_sha256"])

    def test_unusual_filename_is_passed_as_one_argument(self):
        # A shell-interpolated subprocess would break here; the list form
        # must not. This proves no user filename reaches a shell string.
        weird = Path(TMPDIR) / "weird; name $(touch pwned) `x`.pdf"
        shutil.copyfile(FIXTURES["digital_pdf"], weird)
        result = document_text.extract_document(weird, filename=weird.name)
        self.assertEqual(result["status"], "ok")
        self.assertIn("page one", result["text"].lower())
        self.assertFalse((Path(TMPDIR) / "pwned").exists())

    def test_missing_pdf_raises(self):
        with self.assertRaises(FileNotFoundError):
            document_text.extract_document(str(Path(TMPDIR) / "nope.pdf"), filename="nope.pdf")


class TestPlainTypes(unittest.TestCase):
    def test_txt(self):
        result = document_text.extract_document(FIXTURES["txt"], filename="note.txt")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["page_count"], 1)
        self.assertIn("Synthetic plain text attachment", result["text"])
        self.assertEqual(result["pages"][0]["source"], "text")

    def test_html_is_stripped(self):
        result = document_text.extract_document(FIXTURES["html"], filename="page.html")
        self.assertIn("Hello", result["text"])
        self.assertIn("synthetic", result["text"])
        self.assertNotIn("<b>", result["text"])
        self.assertNotIn("alert(1)", result["text"])
        self.assertNotIn("color:red", result["text"])

    def test_eml_prefers_plain_part(self):
        result = document_text.extract_document(FIXTURES["eml"], filename="mail.eml")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Plain synthetic body text", result["text"])
        self.assertNotIn("alert", result["text"])

    def test_text_truncation(self):
        result = document_text.extract_document(
            FIXTURES["txt"], filename="note.txt", max_chars=5)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"]), 5)
        self.assertGreater(result["pages"][0]["char_count"], 5)


class TestUnsupported(unittest.TestCase):
    def test_png_is_structured_error_without_body(self):
        result = document_text.extract_document(FIXTURES["png"], filename="image.png",
                                                mime_type="image/png")
        self.assertEqual(result["status"], "unsupported_type")
        self.assertEqual(result["error"], "unsupported_type")
        self.assertEqual(result["filename"], "image.png")
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["pages"], [])
        self.assertGreater(result["size_bytes"], 0)


class TestSafety(unittest.TestCase):
    def test_audit_helper_drops_text_and_tokens(self):
        import ocr_store
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        previous_level = ocr_store.logger.level
        ocr_store.logger.setLevel(logging.INFO)
        ocr_store.logger.addHandler(handler)
        try:
            ocr_store.audit("unit", job_id=7, file_sha256="abc", page=2,
                            status="pending", text="PATIENT ZERO SECRET",
                            token="sietch_do_not_log", filename="secret.pdf")
        finally:
            ocr_store.logger.removeHandler(handler)
            ocr_store.logger.setLevel(previous_level)
        joined = "\n".join(records)
        self.assertIn("job_id=7", joined)
        self.assertIn("file_sha256=abc", joined)
        self.assertNotIn("PATIENT ZERO SECRET", joined)
        self.assertNotIn("sietch_do_not_log", joined)
        self.assertNotIn("secret.pdf", joined)

    def test_server_modules_never_import_an_ocr_engine(self):
        for name in ("document_text.py", "document_read.py", "ocr_store.py",
                     "server.py", "mcp_tools.py"):
            source = (ROOT / name).read_text(encoding="utf-8").lower()
            with self.subTest(module=name):
                self.assertNotIn("import paddleocr", source)
                self.assertNotIn("from paddleocr", source)
                self.assertNotIn("import paddle\n", source)

    def test_extractor_never_logs_extracted_text(self):
        source = (ROOT / "document_text.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            if "logger." in line:
                self.assertNotIn("text", line.replace("pdftotext", "").lower(),
                                 f"possible text leak: {line.strip()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
