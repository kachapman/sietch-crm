#!/usr/bin/env python3
"""Format detection for the document editor and download headers.

Synthetic bytes only. Display titles and MIME labels are the lie; the
assertions are about the bytes. Importing server (and therefore Postgres)
is not required for the behavior tests. A source guard checks that the
editor and download handlers call this code after their auth checks.

Run with: python3 tests/test_document_format.py
"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import document_format as df


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
WORD_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SHEET_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _office_zip(content_type: str, part: str, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types><Override PartName="/%s" ContentType="%s"/></Types>'
            % (part, content_type),
        )
        zf.writestr(part, b"<root/>")
        for name, payload in (extra or {}).items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _docx() -> bytes:
    return _office_zip(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "word/document.xml",
    )


def _xlsx() -> bytes:
    return _office_zip(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "xl/workbook.xml",
    )


def _pptx() -> bytes:
    return _office_zip(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "ppt/presentation.xml",
    )


class TestBytesWinOverLabels(unittest.TestCase):
    def test_extensionless_pdf_is_pdf_not_docx(self):
        fmt = df.detect_document_format(PDF, "Estimate Packet", "application/octet-stream")
        self.assertEqual(fmt["ext"], "pdf")
        self.assertEqual(fmt["document_type"], "pdf")
        self.assertTrue(fmt["onlyoffice"])
        self.assertEqual(fmt["mime"], "application/pdf")

    def test_uppercase_suffix_still_uses_bytes(self):
        fmt = df.detect_document_format(PDF, "PACKET.PDF", "application/octet-stream")
        self.assertEqual(fmt["ext"], "pdf")

    def test_misleading_docx_title_and_word_mime_are_still_pdf(self):
        fmt = df.detect_document_format(PDF, "Estimate.docx", WORD_MIME)
        self.assertEqual(fmt["ext"], "pdf")
        self.assertEqual(fmt["document_type"], "pdf")
        self.assertNotEqual(fmt["document_type"], "word")

    def test_double_suffix_pdf_docx_with_pdf_bytes(self):
        fmt = df.detect_document_format(PDF, "packet.pdf.docx", WORD_MIME)
        self.assertEqual(fmt["ext"], "pdf")
        self.assertEqual(df.safe_document_filename("packet.pdf.docx", fmt["ext"]), "packet.pdf")

    def test_double_suffix_docx_pdf_with_docx_bytes(self):
        fmt = df.detect_document_format(_docx(), "packet.docx.pdf", "application/pdf")
        self.assertEqual(fmt["ext"], "docx")
        self.assertEqual(fmt["document_type"], "word")
        self.assertEqual(df.safe_document_filename("packet.docx.pdf", fmt["ext"]), "packet.docx")

    def test_uppercase_double_suffix(self):
        fmt = df.detect_document_format(_docx(), "file.PDF.DOCX", "application/pdf")
        self.assertEqual(fmt["ext"], "docx")
        self.assertEqual(df.safe_document_filename("file.PDF.DOCX", "docx"), "file.docx")

    def test_pdf_preamble_within_1024_bytes(self):
        data = b"\n\n" + PDF
        fmt = df.detect_document_format(data, "no-name", None)
        self.assertEqual(fmt["ext"], "pdf")

    def test_pdf_marker_past_the_window_is_not_a_pdf(self):
        data = b"\x00" + (b"A" * 1100) + b"%PDF-1.4\n"
        fmt = df.detect_document_format(data, "late.pdf", "application/pdf")
        self.assertNotEqual(fmt["ext"], "pdf")
        self.assertFalse(fmt["onlyoffice"])

    def test_zip_whose_name_contains_pdf_marker_is_not_a_pdf(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x%PDF-.txt", b"not a pdf")
        fmt = df.detect_document_format(buf.getvalue(), "trick.pdf", "application/pdf")
        self.assertNotEqual(fmt["ext"], "pdf")
        self.assertFalse(fmt["onlyoffice"])

    def test_legitimate_docx_extensionless_and_mislabelled(self):
        data = _docx()
        for title, mime in (
            ("Contract.docx", WORD_MIME),
            ("Contract", "application/octet-stream"),
            ("Contract.PDF", "application/pdf"),
            ("CONTRACT.DOCX", "application/pdf"),
        ):
            fmt = df.detect_document_format(data, title, mime)
            self.assertEqual(fmt["ext"], "docx", title)
            self.assertEqual(fmt["document_type"], "word", title)
            self.assertTrue(fmt["onlyoffice"], title)

    def test_xlsx_and_pptx_ignore_pdf_labels(self):
        sheet = df.detect_document_format(_xlsx(), "grid.PDF", "application/pdf")
        self.assertEqual(sheet["ext"], "xlsx")
        self.assertEqual(sheet["document_type"], "cell")
        slides = df.detect_document_format(_pptx(), "deck", "application/octet-stream")
        self.assertEqual(slides["ext"], "pptx")
        self.assertEqual(slides["document_type"], "slide")

    def test_macro_enabled_word_is_docm_not_docx(self):
        data = _office_zip(
            "application/vnd.ms-word.document.macroEnabled.12",
            "word/document.xml",
        )
        fmt = df.detect_document_format(data, "macros.pdf", "application/pdf")
        self.assertEqual(fmt["ext"], "docm")
        self.assertEqual(fmt["document_type"], "word")

    def test_odt_mimetype(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", b"application/vnd.oasis.opendocument.text")
            zf.writestr("content.xml", b"<office/>")
        fmt = df.detect_document_format(buf.getvalue(), "note.docx", WORD_MIME)
        self.assertEqual(fmt["ext"], "odt")
        self.assertEqual(fmt["document_type"], "word")

    def test_ole_word_beats_excel_mime_and_xlsx_title(self):
        data = OLE + b"\x00" * 32 + "WordDocument".encode("utf-16le")
        fmt = df.detect_document_format(data, "sheet.xlsx", "application/vnd.ms-excel")
        self.assertEqual(fmt["ext"], "doc")
        self.assertEqual(fmt["document_type"], "word")

    def test_ole_workbook_beats_pdf_title(self):
        data = OLE + b"\x00" * 32 + "Workbook".encode("utf-16le")
        fmt = df.detect_document_format(data, "budget.PDF", "application/pdf")
        self.assertEqual(fmt["ext"], "xls")
        self.assertEqual(fmt["document_type"], "cell")

    def test_csv_text_with_lying_pdf_mime(self):
        fmt = df.detect_document_format(b"col_a,col_b\n1,2\n", "Export.CSV", "application/pdf")
        self.assertEqual(fmt["ext"], "csv")
        self.assertEqual(fmt["document_type"], "cell")
        self.assertEqual(fmt["mime"], "text/csv")

    def test_extensionless_plain_text_is_txt(self):
        fmt = df.detect_document_format(b"Hello synthetic note\n", "Field notes", "application/octet-stream")
        self.assertEqual(fmt["ext"], "txt")
        self.assertEqual(fmt["document_type"], "word")

    def test_text_labelled_pdf_is_not_opened_as_pdf(self):
        fmt = df.detect_document_format(b"not a pdf file\n", "x.pdf", "application/pdf")
        self.assertFalse(fmt["onlyoffice"])
        self.assertNotEqual(fmt["ext"], "pdf")

    def test_rtf_signature(self):
        fmt = df.detect_document_format(b"{\\rtf1\\ansi hi}", "letter.pdf", "application/pdf")
        self.assertEqual(fmt["ext"], "rtf")
        self.assertEqual(fmt["document_type"], "word")

    def test_file_on_disk_matches_bytes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            path.write_bytes(PDF)
            fmt = df.detect_document_format_file(path, "Estimate.docx", WORD_MIME)
        self.assertEqual(fmt["ext"], "pdf")


class TestUnsafeFormatsFailClearly(unittest.TestCase):
    def test_empty(self):
        fmt = df.detect_document_format(b"", "empty.pdf", "application/pdf")
        self.assertEqual(fmt["kind"], "empty")
        self.assertFalse(fmt["onlyoffice"])
        self.assertIn("empty", df.editor_format_error(fmt).lower())

    def test_executable_labelled_pdf(self):
        sentinel = "CLAIM-SECRET-9912"
        fmt = df.detect_document_format(b"MZ\x00\x01\x02", f"{sentinel}.pdf", "application/pdf")
        self.assertFalse(fmt["onlyoffice"])
        err = df.editor_format_error(fmt)
        self.assertNotIn(sentinel, err)
        self.assertIn("can't be opened", err)

    def test_html_script_is_not_an_office_document(self):
        data = b"<script>alert(1)</script>"
        fmt = df.detect_document_format(data, "notes.html", "text/html")
        self.assertFalse(fmt["onlyoffice"])
        self.assertNotIn("<script>", df.editor_format_error(fmt))
        # The saved name must not keep a dangerous suffix.
        self.assertEqual(df.safe_document_filename("notes.html", fmt.get("ext")), "notes")

    def test_png_is_an_image_not_a_word_file(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        fmt = df.detect_document_format(png, "scan.pdf", "application/pdf")
        self.assertEqual(fmt["kind"], "image")
        self.assertEqual(fmt["ext"], "png")
        self.assertFalse(fmt["onlyoffice"])
        self.assertIn("image", df.editor_format_error(fmt).lower())

    def test_encrypted_office_zip_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("EncryptionInfo", b"not-a-real-key")
            zf.writestr("EncryptedPackage", b"cipher")
        fmt = df.detect_document_format(buf.getvalue(), "secret.docx", WORD_MIME)
        self.assertEqual(fmt["kind"], "encrypted")
        self.assertFalse(fmt["onlyoffice"])
        self.assertIn("encrypted", df.editor_format_error(fmt).lower())

    def test_random_zip_is_not_guessed_as_docx(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", b"hello")
        fmt = df.detect_document_format(buf.getvalue(), "report.docx", WORD_MIME)
        self.assertFalse(fmt["onlyoffice"])
        self.assertNotEqual(fmt["ext"], "docx")

    def test_too_many_zip_entries_fail_closed(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(5001):
                zf.writestr(f"p{i}.txt", b"x")
        fmt = df.detect_document_format(buf.getvalue(), "a.docx", WORD_MIME)
        self.assertFalse(fmt["onlyoffice"])


class TestFilenamesAndHeaders(unittest.TestCase):
    def test_extensionless_title_gains_detected_suffix(self):
        self.assertEqual(df.safe_document_filename("Estimate Packet", "pdf"), "Estimate Packet.pdf")

    def test_path_and_header_injection_cannot_break_the_header(self):
        title = '../../etc/passwd.pdf"\r\nSet-Cookie: pwned=1'
        name = df.safe_document_filename(title, "pdf")
        self.assertEqual(name, "passwd.pdf")
        header = df.content_disposition_header("inline", name)
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertNotIn("Set-Cookie", header)
        self.assertNotIn("..", header)
        self.assertTrue(header.startswith("inline;"))
        self.assertIn('filename="passwd.pdf"', header)

    def test_attachment_flag_and_unicode_fallback(self):
        name = df.safe_document_filename("Éstimate", "pdf")
        self.assertTrue(name.endswith(".pdf"))
        header = df.content_disposition_header("attachment", name)
        self.assertTrue(header.startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", header)
        self.assertNotIn("É", header.split("filename=")[1].split(";")[0])
        self.assertIn("%C3%89", header)

    def test_disposition_injection_is_coerced(self):
        header = df.content_disposition_header("inline\r\nX-Evil: 1", "note.pdf")
        self.assertTrue(header.startswith("inline;"))
        self.assertNotIn("X-Evil", header)
        self.assertNotIn("\r", header)

    def test_download_flag(self):
        self.assertTrue(df.wants_attachment_download("1"))
        self.assertTrue(df.wants_attachment_download(" true "))
        self.assertFalse(df.wants_attachment_download("0"))
        self.assertFalse(df.wants_attachment_download(None))
        self.assertFalse(df.wants_attachment_download(""))


class TestSessionKey(unittest.TestCase):
    def test_type_is_part_of_the_key_and_repeats_are_stable(self):
        pdf_key = df.document_session_key(5, "pdf")
        self.assertEqual(pdf_key, "sietch-doc-5-pdf")
        self.assertEqual(df.document_session_key(5, "PDF"), pdf_key)
        self.assertEqual(df.document_session_key(5, "pdf"), pdf_key)
        self.assertNotEqual(pdf_key, df.document_session_key(5, "docx"))

    def test_refuses_an_unsafe_suffix(self):
        for bad in ("", "../pdf", "pdf.exe", "pdf/../../x", "has space", "x" * 9):
            with self.assertRaises(ValueError):
                df.document_session_key(1, bad)

    def test_callback_binding_accepts_legacy_and_typed_keys_for_this_id_only(self):
        self.assertTrue(df.document_key_bound_to("sietch-doc-1", 1))
        self.assertTrue(df.document_key_bound_to("sietch-doc-1-pdf", 1))
        self.assertTrue(df.document_key_bound_to("sietch-doc-1-docx", 1))
        self.assertFalse(df.document_key_bound_to("sietch-doc-1-pdf", 2))
        self.assertFalse(df.document_key_bound_to("sietch-doc-1-pdf", 10))
        self.assertFalse(df.document_key_bound_to("sietch-doc-10", 1))
        self.assertFalse(df.document_key_bound_to("sietch-doc-10-pdf", 1))
        self.assertFalse(df.document_key_bound_to("sietch-doc-1-pdf/../../x", 1))
        self.assertFalse(df.document_key_bound_to("sietch-doc-1-", 1))
        self.assertFalse(df.document_key_bound_to("sietch-doc-1-PDF", 1))
        self.assertFalse(df.document_key_bound_to(None, 1))
        self.assertFalse(df.document_key_bound_to(12, 1))


class TestHandlerWiring(unittest.TestCase):
    """The handlers live in server.py, which opens Postgres on import.

    These assertions read the source so they run without a database, and
    they pin the auth-before-bytes order plus the cookie-less token path.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (HERE.parent / "server.py").read_text(encoding="utf-8")

    def _slice(self, start_def: str, end_def: str) -> str:
        start = self.src.index(f"def {start_def}")
        end = self.src.index(f"def {end_def}", start)
        return self.src[start:end]

    def test_download_authorizes_before_sniff_or_read(self):
        body = self._slice("_handle_document_download", "_handle_document_editor_config")
        self.assertLess(body.index("_authorize_document_fetch"), body.index("detect_document_format"))
        self.assertLess(body.index("_authorize_document_fetch"), body.index("read_bytes"))
        self.assertIn("content_disposition_header", body)
        self.assertIn("wants_attachment_download", body)
        self.assertIn("nosniff", body)
        self.assertNotIn('filename="{title}"', body)

    def test_cookie_less_token_still_authorizes_the_fetch(self):
        body = self._slice("_authorize_document_fetch", "_verify_document_callback")
        self.assertIn("_verify_document_token", body)
        self.assertIn('qs.get("token")', body)
        # No session and no token still fails closed.
        self.assertIn("Not authenticated", body)

    def test_editor_config_uses_bytes_and_refuses_unknown(self):
        body = self._slice("_handle_document_editor_config", "_handle_document_activity")
        self.assertLess(body.index("_require_auth"), body.index("detect_document_format_file"))
        self.assertIn("document_session_key", body)
        self.assertIn("safe_document_filename", body)
        self.assertIn("415", body)
        self.assertNotIn('or "docx"', body)
        self.assertNotIn("Path(title)", body)

    def test_callback_binds_the_typed_key(self):
        body = self._slice("_verify_document_callback", "_insert_project_document_safely")
        self.assertIn("document_key_bound_to", body)
        self.assertNotIn("expected_key not in candidates", body)


if __name__ == "__main__":
    unittest.main(verbosity=1)
