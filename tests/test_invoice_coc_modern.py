#!/usr/bin/env python3
"""Unit tests for the modern (Sherwood-style) Invoice + Certificate of
Completion PDFs.

Covers:
  - the pure mapping layer (document_templates row + payload -> brand / job
    / items / opts),
  - the ported ReportLab builders (text content, page numbering, repeated
    header rows, accent readability, missing assets),
  - the server-side layout switch (payload `layout` field and the
    SIETCH_DOC_LAYOUT env var) plus the classic fallback still rendering,
  - the four HTTP handlers (project invoice, project COC, standalone
    invoice, standalone COC) with a mocked DB.

Unlike the older suites, this file resolves every path relative to its own
location so it can run from any checkout / worktree.

Run with: python3 tests/test_invoice_coc_modern.py
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

try:
    from doc_pdf import mapping as doc_mapping
    from doc_pdf.coc_pdf import build_coc
    from doc_pdf.invoice_pdf import build_invoice
    from doc_pdf.pdf_common import accent, company_lines, money, on_accent, text_accent
    _PDF_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    doc_mapping = None
    build_coc = None
    build_invoice = None
    _PDF_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


TEMPLATE = {
    "id": 7,
    "name": "Default",
    "company_name": "Public Adjustment Midwest LLC",
    "company_address": "1234 N Main St",
    "company_city_state_zip": "Columbus, OH 43215",
    "company_phone": "(614) 555-0100",
    "company_email": "info@example.com",
    # Branding extras (migration 045) -- empty by default so the
    # "omitted when empty" behaviour is what the base template exercises.
    "company_website": "",
    "company_license": "",
    "signature_title": "",
    "primary_color": "#1f4e79",
    "invoice_title": "INVOICE",
    "coc_title": "CERTIFICATE OF COMPLETION",
    "default_terms": "Due on receipt",
    "default_sales_rep": "Ken Chapman",
    "default_line_items": [],
    "show_signature": True,
    "signature_name": "Ken Chapman",
    "signature_path": None,
    "logo_path": None,
    "show_branding_footer": True,
    "is_default": True,
}

# The same template with every branding extra filled in.
TEMPLATE_WITH_EXTRAS = dict(
    TEMPLATE,
    company_website="www.publicadjustermidwest.com",
    company_license="OH LIC 104.8872",
    signature_title="Licensed Public Adjuster",
)

PAYLOAD = {
    "invoice_number": "VA-2041",
    "invoice_date": "11/03/2026",
    "terms": "Due on receipt",
    "sales_rep": "Ken Chapman",
    "claim_number": "CLM-9988",
    "project_number": "1042",
    "customer_name": "Jordan Ellis",
    "street": "1428 Maple Ridge Dr",
    "city_state_zip": "Columbus, OH 43215",
    "notes": "Payment by check or ACH.",
    "base_amount": "1000",
    "line_items": [
        {"description": "Roof replacement", "amount": "18450"},
        {"description": "Gutters", "amount": "2310.50"},
    ],
    "include_signature": True,
    "signature_name": "Ken Chapman",
}


def pdf_text(path):
    """Extract text from a PDF (pdftotext when available, else pypdf).

    poppler's pdftotext is an OS package (poppler-utils), not a Python
    dependency, so it is absent on a bare runner. subprocess.run raises
    FileNotFoundError when the binary is missing, which used to escape the
    fallback entirely.
    """
    path = str(path)
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return out.stdout
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def pdf_page_count(path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        try:
            out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True)
        except (FileNotFoundError, OSError):
            return 0
        m = re.search(r"Pages:\s+(\d+)", out.stdout or "")
        return int(m.group(1)) if m else 0


# ── Mapping layer ───────────────────────────────────────────────────────────

class TestMapping(unittest.TestCase):
    """document_templates row + payload -> brand / job / items / opts."""

    def setUp(self):
        if doc_mapping is None:
            self.skipTest(f"doc_pdf not importable: {_PDF_IMPORT_ERROR}")

    def test_brand_maps_template_columns(self):
        brand = doc_mapping.build_brand(TEMPLATE, root=REPO)
        self.assertEqual(brand["name"], "Public Adjustment Midwest LLC")
        self.assertEqual(brand["address"], "1234 N Main St")
        self.assertEqual(brand["city_state_zip"], "Columbus, OH 43215")
        self.assertEqual(brand["phone"], "(614) 555-0100")
        self.assertEqual(brand["email"], "info@example.com")
        self.assertEqual(brand["accent"], "#1f4e79")
        self.assertEqual(brand["invoice_title"], "INVOICE")
        self.assertEqual(brand["coc_title"], "CERTIFICATE OF COMPLETION")

    def test_brand_footer_follows_show_branding_footer(self):
        on = doc_mapping.build_brand(dict(TEMPLATE, show_branding_footer=True), root=REPO)
        off = doc_mapping.build_brand(dict(TEMPLATE, show_branding_footer=False), root=REPO)
        self.assertEqual(on["footer_text"], "Generated by Sietch CRM")
        self.assertEqual(off["footer_text"], "")

    def test_brand_website_and_license_are_blank_when_unset(self):
        brand = doc_mapping.build_brand(TEMPLATE, root=REPO)
        self.assertEqual(brand["website"], "")
        self.assertEqual(brand["license"], "")

    def test_brand_maps_website_and_license(self):
        brand = doc_mapping.build_brand(TEMPLATE_WITH_EXTRAS, root=REPO)
        self.assertEqual(brand["website"], "www.publicadjustermidwest.com")
        self.assertEqual(brand["license"], "OH LIC 104.8872")

    def test_company_lines_include_website_and_license(self):
        brand = doc_mapping.build_brand(TEMPLATE_WITH_EXTRAS, root=REPO)
        lines = company_lines(brand)
        self.assertIn("www.publicadjustermidwest.com", lines)
        # The value already contains "lic", so it is NOT prefixed with
        # "License " (2026-11-03 fix: no more "License OH LIC 104.8872").
        self.assertIn("OH LIC 104.8872", lines)
        self.assertNotIn("License OH LIC", " ".join(lines))

    def test_company_lines_omit_empty_website_and_license(self):
        brand = doc_mapping.build_brand(TEMPLATE, root=REPO)
        lines = company_lines(brand)
        self.assertNotIn("License", " ".join(lines))
        self.assertEqual(len([ln for ln in lines if ln.strip()]), 3)

    def test_brand_missing_logo_does_not_crash(self):
        tpl = dict(TEMPLATE, logo_path="data/logos/does-not-exist.png")
        brand = doc_mapping.build_brand(tpl, root=REPO)
        self.assertIsNone(brand["logo_path"])

    def test_job_maps_payload_fields(self):
        job = doc_mapping.build_job(PAYLOAD, today="11/03/2026")
        self.assertEqual(job["customer_name"], "Jordan Ellis")
        self.assertEqual(job["street"], "1428 Maple Ridge Dr")
        self.assertEqual(job["city_state_zip"], "Columbus, OH 43215")
        self.assertEqual(job["job_number"], "1042")
        self.assertEqual(job["insurance_claim"], "CLM-9988")
        self.assertEqual(job["sales_rep"], "Ken Chapman")
        self.assertEqual(job["invoice_number"], "VA-2041")
        self.assertEqual(job["invoice_date"], "11/03/2026")
        self.assertEqual(job["terms"], "Due on receipt")
        self.assertEqual(job["invoice_notes"], "Payment by check or ACH.")

    def test_job_completion_date_defaults_to_today(self):
        job = doc_mapping.build_job(dict(PAYLOAD, completion_date=""), today="11/03/2026")
        self.assertEqual(job["completion_date"], "11/03/2026")
        job2 = doc_mapping.build_job(dict(PAYLOAD, completion_date="10/31/2026"), today="11/03/2026")
        self.assertEqual(job2["completion_date"], "10/31/2026")

    def test_items_base_amount_is_first_row_labelled_base_charge(self):
        # The base charge used to be labelled with the payment TERMS
        # ("Due on receipt"), which reads like a payment condition rather
        # than a charge description. It is now "Base charge".
        items = doc_mapping.build_items(PAYLOAD)
        self.assertEqual(items[0], ("Base charge", 1000.0))
        self.assertEqual(items[1], ("Roof replacement", 18450.0))
        self.assertEqual(items[2], ("Gutters", 2310.5))

    def test_items_base_charge_label_is_not_the_payment_terms(self):
        items = doc_mapping.build_items(dict(PAYLOAD, terms="Net 30"))
        self.assertEqual(items[0][0], "Base charge")
        self.assertNotIn("Net 30", [d for d, _a in items])

    def test_items_base_charge_label_can_be_overridden(self):
        items = doc_mapping.build_items(dict(PAYLOAD, base_description="Labor - initial draw"))
        self.assertEqual(items[0], ("Labor - initial draw", 1000.0))

    def test_items_total_matches_classic_grand_total(self):
        items = doc_mapping.build_items(PAYLOAD)
        total = sum(a for _d, a in items)
        # Classic: base_amount + sum(line_items)
        expected = 1000.0 + 18450.0 + 2310.50
        self.assertAlmostEqual(total, expected, places=2)

    def test_items_base_amount_zero_is_omitted(self):
        items = doc_mapping.build_items(dict(PAYLOAD, base_amount="0"))
        self.assertEqual(len(items), 2)
        self.assertNotIn(("Due on receipt", 0.0), items)

    def test_items_explicit_line_items_override_payload(self):
        items = doc_mapping.build_items(PAYLOAD, [{"description": "Override", "amount": "5"}])
        self.assertEqual(items[-1], ("Override", 5.0))

    def test_items_hide_zero_amount_rows(self):
        """Ken's decision (2026-11-03): a $0.00 line is not printed."""
        items = doc_mapping.build_items(dict(PAYLOAD, base_amount="0", line_items=[
            {"description": "Real work", "amount": "100"},
            {"description": "No charge", "amount": "0.00"},
            {"description": "Also no charge", "amount": "0"},
            {"description": "Also real", "amount": "50"},
        ]))
        descriptions = [d for d, _a in items]
        self.assertIn("Real work", descriptions)
        self.assertIn("Also real", descriptions)
        self.assertNotIn("No charge", descriptions)
        self.assertNotIn("Also no charge", descriptions)
        self.assertEqual(len(items), 2)

    def test_items_total_unchanged_by_hidden_zero_rows(self):
        with_zero = doc_mapping.build_items(dict(PAYLOAD, line_items=[
            {"description": "Real work", "amount": "100"},
            {"description": "No charge", "amount": "0.00"},
        ]))
        without_zero = doc_mapping.build_items(dict(PAYLOAD, line_items=[
            {"description": "Real work", "amount": "100"},
        ]))
        self.assertEqual(sum(a for _d, a in with_zero),
                         sum(a for _d, a in without_zero))

    def test_coc_opts_statement_and_no_homeowner_line(self):
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        self.assertEqual(opts["statement"], doc_mapping.COC_STATEMENT)
        # Ken's decision (2026-11-03): no property-owner signature line, so
        # the option no longer exists at all.
        self.assertNotIn("homeowner_line", opts)
        self.assertEqual(opts["signer_name"], "Ken Chapman")
        self.assertEqual(opts["signer_title"], "")

    def test_coc_opts_signer_title_from_template(self):
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        self.assertEqual(opts["signer_title"], "Licensed Public Adjuster")

    def test_coc_opts_signature_gated_by_include_signature(self):
        tpl = dict(TEMPLATE, signature_path="data/sigs/sig.png")
        on = doc_mapping.build_coc_opts(dict(PAYLOAD, include_signature=True), tpl, root=REPO)
        off = doc_mapping.build_coc_opts(dict(PAYLOAD, include_signature=False), tpl, root=REPO)
        self.assertIsNone(off["signature_path"])
        # The file does not exist in the repo, so both resolve to None.
        self.assertIsNone(on["signature_path"])

    def test_invoice_signature_gated_by_include_signature(self):
        sig = doc_mapping.build_invoice_signature(dict(PAYLOAD, include_signature=False), TEMPLATE, root=REPO)
        self.assertIsNone(sig)
        sig = doc_mapping.build_invoice_signature(dict(PAYLOAD, include_signature=True), TEMPLATE, root=REPO)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["name"], "Ken Chapman")
        self.assertEqual(sig["date"], "11/03/2026")
        self.assertEqual(sig["title"], "")

    def test_invoice_signature_carries_the_signer_title(self):
        sig = doc_mapping.build_invoice_signature(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        self.assertEqual(sig["title"], "Licensed Public Adjuster")


# ── Builders ────────────────────────────────────────────────────────────────

class TestBuilders(unittest.TestCase):
    """The ported ReportLab builders."""

    def setUp(self):
        if build_invoice is None:
            self.skipTest(f"doc_pdf not importable: {_PDF_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.brand = doc_mapping.build_brand(TEMPLATE, root=REPO)
        self.job = doc_mapping.build_job(PAYLOAD, today="11/03/2026")
        self.items = doc_mapping.build_items(PAYLOAD)

    def tearDown(self):
        self.tmp.cleanup()

    def test_invoice_contains_key_strings(self):
        out = self.dir / "invoice.pdf"
        total = build_invoice(str(out), self.brand, self.job, self.items)
        self.assertAlmostEqual(total, 21760.50, places=2)
        text = pdf_text(out)
        for bit in ("Public Adjustment Midwest LLC", "INVOICE", "VA-2041", "11/03/2026",
                    "Jordan Ellis", "1428 Maple Ridge Dr", "CLM-9988", "1042",
                    "Roof replacement", "Gutters", "$18,450.00", "$2,310.50",
                    "$21,760.50", "Payment by check or ACH.",
                    "Generated by Sietch CRM", "Page 1 of 1"):
            self.assertIn(bit, text, f"missing {bit!r} in invoice text")

    def test_invoice_footer_absent_when_branding_footer_off(self):
        brand = doc_mapping.build_brand(dict(TEMPLATE, show_branding_footer=False), root=REPO)
        out = self.dir / "invoice-nofooter.pdf"
        build_invoice(str(out), brand, self.job, self.items)
        text = pdf_text(out)
        self.assertNotIn("Generated by Sietch CRM", text)
        self.assertIn("Page 1 of 1", text)

    def test_invoice_signature_block(self):
        sig = doc_mapping.build_invoice_signature(PAYLOAD, TEMPLATE, root=REPO)
        out = self.dir / "invoice-sig.pdf"
        build_invoice(str(out), self.brand, self.job, self.items, signature=sig)
        text = pdf_text(out)
        self.assertIn("By Ken Chapman", text)
        self.assertIn("Date: 11/03/2026", text)

    def test_invoice_multipage_page_numbers_and_repeated_header(self):
        items = [(f"Line item number {i:02d} - a description long enough to matter",
                  float(i) * 100) for i in range(1, 61)]
        out = self.dir / "invoice-long.pdf"
        build_invoice(str(out), self.brand, self.job, items)
        self.assertGreater(pdf_page_count(out), 1)
        text = pdf_text(out)
        pages = pdf_page_count(out)
        self.assertIn(f"Page 1 of {pages}", text)
        self.assertIn(f"Page {pages} of {pages}", text)
        # The items table header repeats on every page.
        self.assertGreaterEqual(text.count("DESCRIPTION"), pages)
        self.assertGreaterEqual(text.count("AMOUNT"), pages)

    def test_coc_contains_key_strings(self):
        out = self.dir / "coc.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), self.brand, self.job, opts)
        text = pdf_text(out)
        for bit in ("Public Adjustment Midwest LLC", "Certificate of Completion",
                    "Jordan Ellis", "1428 Maple Ridge Dr", "CLM-9988", "1042",
                    "11/03/2026", "Contractor representative", "Page 1 of 1"):
            self.assertIn(bit, text, f"missing {bit!r} in COC text")

    def test_coc_has_no_property_owner_line(self):
        out = self.dir / "coc.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), self.brand, self.job, opts)
        self.assertNotIn("Property owner", pdf_text(out))

    def test_coc_footer_absent_when_branding_footer_off(self):
        brand = doc_mapping.build_brand(dict(TEMPLATE, show_branding_footer=False), root=REPO)
        out = self.dir / "coc-nofooter.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), brand, self.job, opts)
        text = pdf_text(out)
        self.assertNotIn("Generated by Sietch CRM", text)
        self.assertIn("Page 1 of 1", text)

    def test_missing_logo_and_signature_do_not_crash(self):
        brand = doc_mapping.build_brand(
            dict(TEMPLATE, logo_path="data/logos/missing.png"), root=REPO)
        self.assertIsNone(brand["logo_path"])
        out = self.dir / "invoice-nologo.pdf"
        build_invoice(str(out), brand, self.job, self.items,
                      signature={"path": "/nonexistent/sig.png", "name": "Ken Chapman",
                                 "date": "11/03/2026"})
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)

    def test_light_accent_stays_readable(self):
        """A near-white accent must not render white-on-white text."""
        brand = doc_mapping.build_brand(dict(TEMPLATE, primary_color="#f7f7f2"), root=REPO)
        c = accent(brand)
        self.assertEqual(on_accent(c), __import__("doc_pdf.pdf_common", fromlist=["INK"]).INK)
        self.assertEqual(text_accent(c), __import__("doc_pdf.pdf_common", fromlist=["INK"]).INK)
        out = self.dir / "invoice-light.pdf"
        build_invoice(str(out), brand, self.job, self.items)
        self.assertTrue(out.exists())

    def test_dark_accent_uses_white_text_on_fill(self):
        brand = doc_mapping.build_brand(dict(TEMPLATE, primary_color="#1f4e79"), root=REPO)
        c = accent(brand)
        self.assertNotEqual(on_accent(c), __import__("doc_pdf.pdf_common", fromlist=["INK"]).INK)

    def test_zero_amount_rows_are_dropped(self):
        items = doc_mapping.build_items(dict(PAYLOAD, line_items=[
            {"description": "Real work", "amount": "100"},
            {"description": "", "amount": "0"},
            {"description": "Also real", "amount": "50"},
        ]))
        out = self.dir / "invoice-zero.pdf"
        total = build_invoice(str(out), self.brand, self.job, items)
        self.assertAlmostEqual(total, 1150.0, places=2)
        text = pdf_text(out)
        self.assertIn("Real work", text)
        self.assertIn("Also real", text)

    def test_zero_amount_row_is_not_printed(self):
        """Ken's decision (2026-11-03): $0.00 lines are hidden."""
        items = doc_mapping.build_items(dict(PAYLOAD, line_items=[
            {"description": "Real work", "amount": "100"},
            {"description": "Follow-up site visit", "amount": "0.00"},
            {"description": "Also real", "amount": "50"},
        ]))
        out = self.dir / "invoice-zero-hidden.pdf"
        total = build_invoice(str(out), self.brand, self.job, items)
        self.assertAlmostEqual(total, 1150.0, places=2)
        text = pdf_text(out)
        self.assertNotIn("Follow-up site visit", text)
        self.assertNotIn("$0.00", text)
        self.assertIn("Real work", text)

    def test_invoice_shows_website_and_license_when_set(self):
        brand = doc_mapping.build_brand(TEMPLATE_WITH_EXTRAS, root=REPO)
        out = self.dir / "invoice-extras.pdf"
        build_invoice(str(out), brand, self.job, self.items)
        text = pdf_text(out)
        self.assertIn("www.publicadjustermidwest.com", text)
        self.assertIn("OH LIC 104.8872", text)

    def test_invoice_omits_website_and_license_when_empty(self):
        out = self.dir / "invoice-no-extras.pdf"
        build_invoice(str(out), self.brand, self.job, self.items)
        text = pdf_text(out)
        self.assertNotIn("License", text)
        self.assertNotIn("www.", text)

    def test_invoice_signature_shows_the_signer_title(self):
        sig = doc_mapping.build_invoice_signature(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        out = self.dir / "invoice-sig-title.pdf"
        build_invoice(str(out), self.brand, self.job, self.items, signature=sig)
        text = pdf_text(out)
        self.assertIn("By Ken Chapman", text)
        self.assertIn("Licensed Public Adjuster", text)

    def test_coc_shows_website_and_license_when_set(self):
        brand = doc_mapping.build_brand(TEMPLATE_WITH_EXTRAS, root=REPO)
        out = self.dir / "coc-extras.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        build_coc(str(out), brand, self.job, opts)
        text = pdf_text(out)
        self.assertIn("www.publicadjustermidwest.com", text)
        self.assertIn("OH LIC 104.8872", text)

    def test_coc_omits_website_and_license_when_empty(self):
        out = self.dir / "coc-no-extras.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), self.brand, self.job, opts)
        text = pdf_text(out)
        self.assertNotIn("License", text)
        self.assertNotIn("www.", text)

    def test_coc_shows_the_signer_title(self):
        out = self.dir / "coc-sig-title.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        build_coc(str(out), self.brand, self.job, opts)
        text = pdf_text(out)
        self.assertIn("Ken Chapman, Licensed Public Adjuster", text)

    def test_coc_has_no_property_owner_line(self):
        """Ken's decision (2026-11-03): no property-owner signature line."""
        out = self.dir / "coc-nohomeowner.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        build_coc(str(out), self.brand, self.job, opts)
        text = pdf_text(out)
        self.assertNotIn("Property owner", text)
        self.assertNotIn("____", text)
        self.assertIn("Contractor representative", text)

    def test_company_lines_and_money_helpers(self):
        lines = company_lines(self.brand)
        self.assertIn("1234 N Main St", lines)
        self.assertIn("Columbus, OH 43215", lines)
        self.assertIn("(614) 555-0100  |  info@example.com", lines)
        self.assertEqual(money(1234.5), "$1,234.50")
        self.assertEqual(money(-5), "-$5.00")


# ── Server-side layout switch + handlers ────────────────────────────────────

class TestServerLayoutSwitch(unittest.TestCase):
    """The layout switch and the four HTTP handlers, with a mocked DB."""

    @classmethod
    def setUpClass(cls):
        try:
            import server as srv
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"server module not importable (no DB?): {exc}")
        cls.server = srv

    def _handler_class(self):
        srv = self.server
        # The HTTP handlers live on the request handler class; find it.
        for name in dir(srv):
            obj = getattr(srv, name)
            if isinstance(obj, type) and hasattr(obj, "_handle_project_invoice_generate"):
                return obj
        self.fail("could not locate the request handler class in server.py")

    def test_resolve_doc_layout_defaults_to_modern(self):
        srv = self.server
        old = os.environ.pop("SIETCH_DOC_LAYOUT", None)
        try:
            self.assertEqual(srv._resolve_doc_layout({}), "modern")
            self.assertEqual(srv._resolve_doc_layout(None), "modern")
            self.assertEqual(srv._resolve_doc_layout({"layout": "classic"}), "classic")
            self.assertEqual(srv._resolve_doc_layout({"layout": "modern"}), "modern")
            self.assertEqual(srv._resolve_doc_layout({"layout": "bogus"}), "modern")
        finally:
            if old is not None:
                os.environ["SIETCH_DOC_LAYOUT"] = old

    def test_default_layout_constant_is_modern(self):
        """Ken's decision (2026-11-03): modern is the default."""
        srv = self.server
        self.assertEqual(srv.DOC_LAYOUT_DEFAULT, "modern")
        self.assertEqual(srv.DOC_LAYOUT_MODERN, "modern")
        self.assertEqual(srv.DOC_LAYOUT_CLASSIC, "classic")
        self.assertIn("classic", srv.DOC_LAYOUTS)
        self.assertIn("modern", srv.DOC_LAYOUTS)

    def test_resolve_doc_layout_honours_env_var(self):
        srv = self.server
        old = os.environ.get("SIETCH_DOC_LAYOUT")
        try:
            os.environ["SIETCH_DOC_LAYOUT"] = "classic"
            self.assertEqual(srv._resolve_doc_layout({}), "classic")
            # An explicit per-request value still wins.
            self.assertEqual(srv._resolve_doc_layout({"layout": "modern"}), "modern")
            os.environ["SIETCH_DOC_LAYOUT"] = "modern"
            self.assertEqual(srv._resolve_doc_layout({}), "modern")
        finally:
            if old is None:
                os.environ.pop("SIETCH_DOC_LAYOUT", None)
            else:
                os.environ["SIETCH_DOC_LAYOUT"] = old

    def test_server_source_keeps_classic_renderers(self):
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("def _create_invoice_pdf(", src)
        self.assertIn("def _create_coc_pdf(", src)
        self.assertIn("def _create_invoice_pdf_modern(", src)
        self.assertIn("def _create_coc_pdf_modern(", src)


# ── HTTP handler level ──────────────────────────────────────────────────────

class _FakeHandler:
    """Minimal stand-in for the http.server request handler.

    Only the bits the four generation handlers touch are implemented:
    headers, rfile, send_response / send_header / end_headers / wfile.
    """

    def __init__(self, body=b"{}"):
        import io
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.status = None
        self.headers_sent = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass


class TestGenerationHandlers(unittest.TestCase):
    """Project + standalone invoice / COC endpoints with a mocked DB."""

    @classmethod
    def setUpClass(cls):
        try:
            import server as srv
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"server module not importable (no DB?): {exc}")
        cls.server = srv

    def _handler_class(self):
        srv = self.server
        for name in dir(srv):
            obj = getattr(srv, name)
            if isinstance(obj, type) and hasattr(obj, "_handle_project_invoice_generate"):
                return obj
        self.fail("could not locate the request handler class in server.py")

    def _run(self, method_name, payload, *, opp_id=None, root=None, template=None):
        """Invoke one generation handler with mocked db + auth + storage."""
        import json as _json
        srv = self.server
        method = getattr(self._handler_class(), method_name)
        # The handler class subclasses SimpleHTTPRequestHandler, whose
        # __init__ needs a live socket; build the instance without it.
        instance = object.__new__(self._handler_class())

        response = {}

        def _json_response(h, status, data):
            response["status"] = status
            response["data"] = data

        with patch.object(srv, "_json_response", _json_response), \
             patch.object(srv, "_read_body",
                          lambda h: _json.dumps(payload).encode("utf-8")), \
             patch.object(srv, "_require_auth",
                          lambda h: {"id": 42, "role": "admin"}), \
             patch.object(srv, "db") as mock_db, \
             patch.object(srv, "log_infra_event"), \
             patch.object(srv, "_resolve_action_category_id", return_value=1), \
             patch.object(srv, "_resolve_document_template",
                          return_value=dict(template or TEMPLATE)):
            mock_db.query_one.return_value = None
            mock_db.insert_returning.return_value = 999
            if root is not None:
                with patch.object(srv, "ROOT", root):
                    if opp_id is None:
                        method(instance)
                    else:
                        method(instance, opp_id)
            else:
                if opp_id is None:
                    method(instance)
                else:
                    method(instance, opp_id)
        return response, mock_db

    def _tmp_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "data" / "documents" / "77").mkdir(parents=True, exist_ok=True)
        return root

    def test_project_invoice_modern_returns_201_and_inserts_with_opp_id(self):
        srv = self.server
        root = self._tmp_root()
        response, mock_db = self._run(
            "_handle_project_invoice_generate", dict(PAYLOAD, template_id=7),
            opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        data = response["data"]
        self.assertEqual(data["id"], 999)
        self.assertEqual(data["mimeType"], "application/pdf")
        self.assertTrue(data["title"].endswith(".pdf"))
        self.assertGreater(data["fileSize"], 0)
        self.assertTrue(data.get("historyNoteCreated"))

        # project_documents insert carries the opportunity id.
        insert_args = None
        for call in mock_db.insert_returning.call_args_list:
            if "INSERT INTO project_documents" in (call.args[0] if call.args else ""):
                insert_args = call.args[1]
                break
        self.assertIsNotNone(insert_args, "project_documents insert not called")
        self.assertEqual(insert_args[0], 77)

        # The PDF landed under data/documents/<opp_id>/
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        self.assertIn("Jordan Ellis", text)
        self.assertIn("VA-2041", text)
        self.assertIn("Page 1 of 1", text)

    def test_project_coc_modern_returns_201_and_uses_completion_date(self):
        srv = self.server
        root = self._tmp_root()
        response, mock_db = self._run(
            "_handle_project_coc_generate",
            dict(PAYLOAD, template_id=7, completion_date="10/31/2026", note="Roof done"),
            opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        insert_args = None
        for call in mock_db.insert_returning.call_args_list:
            if "INSERT INTO project_documents" in (call.args[0] if call.args else ""):
                insert_args = call.args[1]
                break
        self.assertIsNotNone(insert_args)
        self.assertEqual(insert_args[0], 77)

        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        self.assertIn("Certificate of Completion", text)
        # The posted completion_date is honoured (the classic renderer
        # ignored it and stamped today's date).
        self.assertIn("10/31/2026", text)
        self.assertIn("Roof done", text)

    def test_project_coc_classic_uses_completion_date(self):
        """Ken's decision (2026-11-03): the classic COC honours the date too."""
        srv = self.server
        root = self._tmp_root()
        response, _ = self._run(
            "_handle_project_coc_generate",
            dict(PAYLOAD, template_id=7, layout="classic",
                 completion_date="10/31/2026", note="Roof done"),
            opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        self.assertIn("10/31/2026", text)
        self.assertIn("Roof done", text)

    def test_project_coc_classic_defaults_completion_date_to_today(self):
        srv = self.server
        root = self._tmp_root()
        payload = dict(PAYLOAD, template_id=7, layout="classic")
        payload.pop("completion_date", None)
        response, _ = self._run("_handle_project_coc_generate", payload,
                                opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        import datetime
        today = datetime.datetime.now().strftime("%m/%d/%Y")
        self.assertIn(today, pdf_text(written[0]))

    def test_project_coc_modern_defaults_completion_date_to_today(self):
        srv = self.server
        root = self._tmp_root()
        payload = dict(PAYLOAD, template_id=7)
        payload.pop("completion_date", None)
        response, _ = self._run("_handle_project_coc_generate", payload,
                                opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        # The classic renderer always stamped today's date; the modern one
        # falls back to today when the payload omits completion_date.
        import datetime
        today = datetime.datetime.now().strftime("%m/%d/%Y")
        self.assertIn(today, text)

    def test_project_invoice_classic_layout_still_renders(self):
        srv = self.server
        root = self._tmp_root()
        response, mock_db = self._run(
            "_handle_project_invoice_generate",
            dict(PAYLOAD, template_id=7, layout="classic"),
            opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        self.assertIn("Jordan Ellis", text)
        self.assertIn("Grand Total", text)
        # The classic renderer uses its own footer, not the branding footer.
        self.assertNotIn("Generated by Sietch CRM", text)

    def test_branding_extras_render_on_both_layouts(self):
        """Ken's decision (2026-11-03): website / license / signer title
        appear on the invoice and the COC, in both layouts, and are omitted
        entirely when empty."""
        srv = self.server
        for layout in ("modern", "classic"):
            for kind in ("invoice", "coc"):
                root = self._tmp_root()
                payload = dict(PAYLOAD, template_id=7, layout=layout)
                method = f"_handle_project_{kind}_generate"
                response, _ = self._run(method, payload, opp_id=77, root=root,
                                       template=TEMPLATE_WITH_EXTRAS)
                self.assertEqual(response["status"], 201)
                written = list((root / "data" / "documents" / "77").glob("*.pdf"))
                self.assertEqual(len(written), 1, f"{layout} {kind}")
                text = pdf_text(written[0])
                self.assertIn("www.publicadjustermidwest.com", text,
                              f"{layout} {kind}: website missing")
                self.assertIn("OH LIC 104.8872", text,
                              f"{layout} {kind}: license missing")

    def test_branding_extras_omitted_when_empty(self):
        srv = self.server
        for layout in ("modern", "classic"):
            for kind in ("invoice", "coc"):
                root = self._tmp_root()
                payload = dict(PAYLOAD, template_id=7, layout=layout)
                method = f"_handle_project_{kind}_generate"
                response, _ = self._run(method, payload, opp_id=77, root=root,
                                       template=TEMPLATE)
                self.assertEqual(response["status"], 201)
                written = list((root / "data" / "documents" / "77").glob("*.pdf"))
                self.assertEqual(len(written), 1, f"{layout} {kind}")
                text = pdf_text(written[0])
                self.assertNotIn("License", text, f"{layout} {kind}")
                self.assertNotIn("www.", text, f"{layout} {kind}")

    def test_coc_signature_lines_off_in_both_layouts(self):
        """v28.2: "Include signature lines" off drops the signature block
        from the certificate in both layouts."""
        srv = self.server
        for layout in ("modern", "classic"):
            root = self._tmp_root()
            tpl = dict(TEMPLATE, show_signature_lines=False)
            response, _ = self._run(
                "_handle_project_coc_generate",
                dict(PAYLOAD, template_id=7, layout=layout),
                opp_id=77, root=root, template=tpl)
            self.assertEqual(response["status"], 201)
            written = list((root / "data" / "documents" / "77").glob("*.pdf"))
            self.assertEqual(len(written), 1, layout)
            text = pdf_text(written[0])
            self.assertNotIn("SIGNATURES", text, layout)
            self.assertNotIn("Contractor representative", text, layout)
            # The classic signature block prints "By <name>"; it must be gone.
            self.assertNotIn("By Ken", text, layout)
            # The rest of the certificate is untouched.
            self.assertIn("Certificate of Completion", text, layout)
            self.assertIn("Jordan Ellis", text, layout)

    def test_classic_coc_signature_block_respects_the_toggle(self):
        """The classic signature block needs a signature IMAGE; with one
        present the toggle decides whether it is drawn."""
        srv = self.server
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"Pillow not installed: {exc}")
        for lines_on, expect_block in ((True, True), (False, False)):
            root = self._tmp_root()
            # A tiny PNG to stand in for the signature image, inside the
            # same root the handler resolves asset paths against.
            png = root / "sig.png"
            Image.new("RGB", (120, 40), (20, 30, 70)).save(png)
            tpl = dict(TEMPLATE, show_signature_lines=lines_on,
                       signature_path=str(png))
            response, _ = self._run(
                "_handle_project_coc_generate",
                dict(PAYLOAD, template_id=7, layout="classic"),
                opp_id=77, root=root, template=tpl)
            self.assertEqual(response["status"], 201)
            written = list((root / "data" / "documents" / "77").glob("*.pdf"))
            self.assertEqual(len(written), 1)
            text = pdf_text(written[0])
            # The classic signature block prints "By <name>" (it can wrap
            # across lines, so match on "By " + the first name part).
            if expect_block:
                self.assertIn("By Ken", text)
            else:
                self.assertNotIn("By Ken", text)
            # The certificate itself is always there.
            self.assertIn("Certificate of Completion", text)

    def test_coc_signature_lines_on_by_default(self):
        srv = self.server
        # Modern: the SIGNATURES heading + the contractor block.
        root = self._tmp_root()
        response, _ = self._run(
            "_handle_project_coc_generate",
            dict(PAYLOAD, template_id=7, layout="modern"),
            opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        text = pdf_text(written[0])
        self.assertIn("SIGNATURES", text)
        self.assertIn("Contractor representative", text)

        # Classic: the signature block only appears when the template has a
        # signature IMAGE (this one does not), but the toggle must never
        # remove anything else from the certificate.
        root = self._tmp_root()
        response, _ = self._run(
            "_handle_project_coc_generate",
            dict(PAYLOAD, template_id=7, layout="classic"),
            opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        text = pdf_text(written[0])
        self.assertIn("Certificate of Completion", text)
        self.assertIn("Jordan Ellis", text)
        self.assertIn("Work completed per agreement.", text)

    def test_classic_coc_title_is_certificate_of_completion(self):
        """v28.2: the classic layout uses the title helper too."""
        srv = self.server
        root = self._tmp_root()
        response, _ = self._run(
            "_handle_project_coc_generate",
            dict(PAYLOAD, template_id=7, layout="classic"),
            opp_id=77, root=root,
            template=dict(TEMPLATE, coc_title="CERTIFICATE OF COMPLETION"))
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        text = pdf_text(written[0])
        self.assertIn("Certificate of Completion", text)
        self.assertNotIn("Certificate Of Completion", text)
        self.assertNotIn("CERTIFICATE OF COMPLETION", text)

    def test_classic_footer_page_count_is_substituted(self):
        """The classic footer used to print the literal "Page 1 of (1)".

        fpdf2 substitutes a SINGLE-brace "{nb}" alias with the page count
        when the document is closed; the old code emitted "{{nb}}" (double
        braces, from an f-string) and never registered the alias, so
        nothing was substituted.
        """
        srv = self.server
        for method in ("_handle_project_invoice_generate",
                       "_handle_project_coc_generate"):
            root = self._tmp_root()
            payload = dict(PAYLOAD, template_id=7, layout="classic")
            response, _ = self._run(method, payload, opp_id=77, root=root)
            self.assertEqual(response["status"], 201, method)
            written = list((root / "data" / "documents" / "77").glob("*.pdf"))
            self.assertEqual(len(written), 1, method)
            text = pdf_text(written[0])
            self.assertIn("Page 1 of 1", text, method)
            self.assertNotIn("(1)", text, method)
            self.assertNotIn("{nb}", text, method)

    def test_classic_multipage_footer_counts_the_pages(self):
        """The classic invoice's page count is right.

        Note: the classic renderer draws its footer once, at the end of the
        content, so it appears on the LAST page only (the modern renderer
        draws it on every page). The page count itself must still be the
        document's total.
        """
        srv = self.server
        root = self._tmp_root()
        payload = dict(PAYLOAD, template_id=7, layout="classic",
                       line_items=[{"description": f"Line {i:03d}", "amount": str(i)}
                                   for i in range(1, 61)])
        response, _ = self._run("_handle_project_invoice_generate", payload,
                                opp_id=77, root=root)
        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        pages = pdf_page_count(written[0])
        self.assertGreater(pages, 1)
        # The footer carries the real total, not "(1)".
        self.assertIn(f"Page {pages} of {pages}", text)
        self.assertNotIn("(1)", text)
        self.assertNotIn("{nb}", text)

    def test_classic_coc_renders_with_reportlab_blocked(self):
        """The classic layout is the FALLBACK when reportlab is missing, so
        it must not import it.

        coc_title() used to be imported from doc_pdf.pdf_common, which
        imports reportlab at module load -- so the classic COC died with
        "No module named 'reportlab'" on exactly the machine that needed
        the fallback. It now lives in doc_pdf.mapping, which is
        reportlab-free.
        """
        import builtins
        srv = self.server
        root = self._tmp_root()
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "reportlab" or name.startswith("reportlab."):
                raise ImportError("No module named 'reportlab' (blocked by the test)")
            return real_import(name, *args, **kwargs)

        # Drop any cached doc_pdf / reportlab modules so the imports run
        # again under the blocker.
        saved = {k: v for k, v in sys.modules.items()
                 if k.startswith("doc_pdf") or k.startswith("reportlab")}
        for k in saved:
            del sys.modules[k]
        builtins.__import__ = blocked
        try:
            response, _ = self._run(
                "_handle_project_coc_generate",
                dict(PAYLOAD, template_id=7, layout="classic",
                     completion_date="10/31/2026"),
                opp_id=77, root=root,
                template=dict(TEMPLATE, coc_title="CERTIFICATE OF COMPLETION"))
        finally:
            builtins.__import__ = real_import
            for k in list(sys.modules):
                if k.startswith("doc_pdf") or k.startswith("reportlab"):
                    del sys.modules[k]
            sys.modules.update(saved)

        self.assertEqual(response["status"], 201)
        written = list((root / "data" / "documents" / "77").glob("*.pdf"))
        self.assertEqual(len(written), 1)
        text = pdf_text(written[0])
        # The certificate is complete and correctly titled without reportlab.
        self.assertIn("Certificate of Completion", text)
        self.assertNotIn("Certificate Of Completion", text)
        self.assertIn("Jordan Ellis", text)
        self.assertIn("10/31/2026", text)
        self.assertIn("Page 1 of 1", text)

    def test_classic_coc_title_helper_does_not_need_reportlab(self):
        """doc_pdf.mapping must stay importable without reportlab."""
        import builtins
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "reportlab" or name.startswith("reportlab."):
                raise ImportError("No module named 'reportlab' (blocked by the test)")
            return real_import(name, *args, **kwargs)

        saved = {k: v for k, v in sys.modules.items()
                 if k.startswith("doc_pdf") or k.startswith("reportlab")}
        for k in saved:
            del sys.modules[k]
        builtins.__import__ = blocked
        try:
            from doc_pdf.mapping import coc_title as title_fn
            self.assertEqual(title_fn("CERTIFICATE OF COMPLETION"),
                             "Certificate of Completion")
            self.assertEqual(title_fn("NOTICE OF COMPLETION AND RELEASE"),
                             "Notice of Completion and Release")
        finally:
            builtins.__import__ = real_import
            for k in list(sys.modules):
                if k.startswith("doc_pdf") or k.startswith("reportlab"):
                    del sys.modules[k]
            sys.modules.update(saved)

    def test_pdf_common_re_exports_coc_title(self):
        """The modern builders import coc_title from pdf_common, which
        re-exports the one in mapping."""
        from doc_pdf.pdf_common import coc_title as from_common
        from doc_pdf.mapping import coc_title as from_mapping
        self.assertIs(from_common, from_mapping)

    def test_standalone_invoice_and_coc(self):
        srv = self.server
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = Path(tmp.name) / "documents"
        response, mock_db = self._run(
            "_handle_standalone_invoice_generate", dict(PAYLOAD, template_id=7))
        self.assertEqual(response["status"], 201)
        insert_args = None
        for call in mock_db.insert_returning.call_args_list:
            if "INSERT INTO project_documents" in (call.args[0] if call.args else ""):
                insert_args = call.args[1]
                break
        self.assertIsNotNone(insert_args)
        # Standalone documents have opportunity_id = NULL. Since the
        # fix-mail-attachments merge the insert goes through
        # _insert_project_document_safely, which writes the NULL literally
        # in the SQL (columns: title, file_path, file_size, mime_type,
        # uploaded_by, opportunity_id=NULL, company_scope, folder_id).
        sql = " ".join(call.args[0].split())
        self.assertIn("uploaded_by, opportunity_id, company_scope", sql)
        self.assertIn("VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)", sql)
        self.assertNotIn(42, insert_args[:4])

        response2, _ = self._run(
            "_handle_standalone_coc_generate",
            dict(PAYLOAD, template_id=7, completion_date="10/31/2026"))
        self.assertEqual(response2["status"], 201)


# ── Sherwood v28.2 port ─────────────────────────────────────────────────────

class TestSherwoodV282Port(unittest.TestCase):
    """The v28.2 (upstream 94e66ac) changes.

    Ported from upstream bee1b9e ("Include signature lines" toggle) and
    bb35d36 (the certificate title helper).
    """

    @classmethod
    def setUpClass(cls):
        if doc_mapping is None:
            raise unittest.SkipTest(f"doc_pdf not importable: {_PDF_IMPORT_ERROR}")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.brand = doc_mapping.build_brand(TEMPLATE, root=REPO)
        self.job = doc_mapping.build_job(PAYLOAD, today="11/03/2026")

    def tearDown(self):
        self.tmp.cleanup()

    # --- 1. the "Include signature lines" toggle ----------------------------

    def test_coc_opts_signatures_defaults_to_true(self):
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        self.assertTrue(opts["signatures"])

    def test_coc_opts_signatures_off_from_the_template(self):
        tpl = dict(TEMPLATE, show_signature_lines=False)
        opts = doc_mapping.build_coc_opts(PAYLOAD, tpl, root=REPO)
        self.assertFalse(opts["signatures"])

    def test_coc_opts_signatures_payload_overrides_the_template(self):
        tpl = dict(TEMPLATE, show_signature_lines=False)
        opts = doc_mapping.build_coc_opts(
            dict(PAYLOAD, include_signature_lines=True), tpl, root=REPO)
        self.assertTrue(opts["signatures"])
        opts = doc_mapping.build_coc_opts(
            dict(PAYLOAD, include_signature_lines=False),
            dict(TEMPLATE, show_signature_lines=True), root=REPO)
        self.assertFalse(opts["signatures"])

    def test_modern_coc_has_no_signatures_section_when_off(self):
        """opts["signatures"] False drops the whole SIGNATURES section --
        the heading included (upstream bee1b9e)."""
        out = self.dir / "coc-nosig.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        opts["signatures"] = False
        build_coc(str(out), self.brand, self.job, opts)
        text = pdf_text(out)
        self.assertNotIn("SIGNATURES", text)
        self.assertNotIn("Contractor representative", text)
        self.assertNotIn("By ", text)
        # The rest of the certificate is untouched.
        self.assertIn("Certificate of Completion", text)
        self.assertIn("Jordan Ellis", text)
        self.assertIn("Page 1 of 1", text)

    def test_modern_coc_has_the_signatures_section_when_on(self):
        out = self.dir / "coc-sig.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), self.brand, self.job, opts)
        text = pdf_text(out)
        self.assertIn("SIGNATURES", text)
        self.assertIn("Contractor representative", text)

    # --- 2. the certificate title helper -----------------------------------

    def test_coc_title_helper(self):
        """Ported from upstream bb35d36."""
        from doc_pdf.pdf_common import coc_title
        for raw in (None, "", "CERTIFICATE OF COMPLETION", "Certificate Of Completion",
                    " certificate of completion ", "CERTIFICATE   OF   COMPLETION"):
            self.assertEqual(coc_title(raw), "Certificate of Completion")
        self.assertEqual(coc_title("NOTICE OF COMPLETION AND RELEASE"),
                         "Notice of Completion and Release")
        # A mixed-case custom title is left exactly as typed.
        self.assertEqual(coc_title("Job Sign-Off"), "Job Sign-Off")
        self.assertEqual(coc_title("Certificate of Work Completion"),
                         "Certificate of Work Completion")

    def test_modern_coc_title_is_certificate_of_completion(self):
        """The all-caps template title must render as "Certificate of
        Completion", not "Certificate Of Completion"."""
        out = self.dir / "coc-title.pdf"
        brand = doc_mapping.build_brand(
            dict(TEMPLATE, coc_title="CERTIFICATE OF COMPLETION"), root=REPO)
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), brand, self.job, opts)
        text = pdf_text(out)
        self.assertIn("Certificate of Completion", text)
        self.assertNotIn("Certificate Of Completion", text)
        self.assertNotIn("CERTIFICATE OF COMPLETION", text)

    def test_modern_coc_keeps_a_custom_mixed_case_title(self):
        out = self.dir / "coc-custom-title.pdf"
        brand = doc_mapping.build_brand(
            dict(TEMPLATE, coc_title="Job Sign-Off Certificate"), root=REPO)
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE, root=REPO)
        build_coc(str(out), brand, self.job, opts)
        self.assertIn("Job Sign-Off Certificate", pdf_text(out))

    # --- 3. the license label ----------------------------------------------

    def test_license_label_helper(self):
        """No "License " prefix when the value already contains "lic"
        anywhere, or starts with "#"."""
        self.assertEqual(doc_mapping.license_label("OH LIC 104.8872"), "OH LIC 104.8872")
        self.assertEqual(doc_mapping.license_label("lic 12345"), "lic 12345")
        self.assertEqual(doc_mapping.license_label("LIC-998"), "LIC-998")
        self.assertEqual(doc_mapping.license_label("#A-123"), "#A-123")
        self.assertEqual(doc_mapping.license_label("OH 12345"), "License OH 12345")
        self.assertEqual(doc_mapping.license_label(""), "")
        self.assertEqual(doc_mapping.license_label(None), "")

    def test_invoice_does_not_double_the_license_label(self):
        brand = doc_mapping.build_brand(TEMPLATE_WITH_EXTRAS, root=REPO)
        out = self.dir / "invoice-license.pdf"
        build_invoice(str(out), brand, self.job, doc_mapping.build_items(PAYLOAD))
        text = pdf_text(out)
        self.assertIn("OH LIC 104.8872", text)
        self.assertNotIn("License OH LIC", text)

    def test_coc_does_not_double_the_license_label(self):
        brand = doc_mapping.build_brand(TEMPLATE_WITH_EXTRAS, root=REPO)
        out = self.dir / "coc-license.pdf"
        opts = doc_mapping.build_coc_opts(PAYLOAD, TEMPLATE_WITH_EXTRAS, root=REPO)
        build_coc(str(out), brand, self.job, opts)
        text = pdf_text(out)
        self.assertIn("OH LIC 104.8872", text)
        self.assertNotIn("License OH LIC", text)

    # --- 4. the base charge label ------------------------------------------

    def test_base_charge_is_not_labelled_with_the_payment_terms(self):
        out = self.dir / "invoice-base.pdf"
        payload = dict(PAYLOAD, terms="Due on receipt")
        items = doc_mapping.build_items(payload)
        build_invoice(str(out), self.brand, self.job, items)
        text = pdf_text(out)
        self.assertIn("Base charge", text)
        self.assertNotIn("Due on receipt $1,000.00", text)
        # The terms still show in the header meta block -- that is correct.
        self.assertIn("Due on receipt", text)


# ── Migration 045 (branding extras) ─────────────────────────────────────────

class TestBrandingExtrasMigration(unittest.TestCase):
    """migration 045 adds company_website / company_license / signature_title
    / show_signature_lines.

    The migration must be additive, idempotent and must not collide with the
    numbering used by the other in-flight branch (043 / 044).
    """

    MIGRATION = REPO / "migrations" / "045_document_templates_branding_extras.sql"

    def test_migration_file_exists(self):
        self.assertTrue(self.MIGRATION.exists(),
                        "migrations/045_document_templates_branding_extras.sql is missing")

    def test_migration_number_does_not_collide(self):
        # 043 / 044 belong to the separate fix-mail-attachments branch, so
        # this branch starts at 045.
        names = sorted(p.name for p in (REPO / "migrations").glob("*.sql"))
        self.assertNotIn("043_document_templates_branding_extras.sql", names)
        self.assertIn("045_document_templates_branding_extras.sql", names)
        self.assertFalse([n for n in names if n.startswith("043_document_templates")])
        self.assertFalse([n for n in names if n.startswith("044_document_templates")])

    def test_migration_adds_the_three_columns(self):
        src = self.MIGRATION.read_text(encoding="utf-8")
        for col in ("company_website", "company_license", "signature_title",
                    "show_signature_lines"):
            self.assertIn(col, src)
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col}", src)

    def test_migration_is_idempotent(self):
        """ADD COLUMN IF NOT EXISTS makes a re-run a no-op."""
        src = self.MIGRATION.read_text(encoding="utf-8")
        self.assertEqual(src.count("ADD COLUMN IF NOT EXISTS"), 4)
        self.assertNotIn("DROP COLUMN", src)

    def test_migration_applies_and_is_re_runnable(self):
        """Apply it to a scratch database, twice, and check the columns."""
        try:
            import psycopg2
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"psycopg2 not installed: {exc}")
        conn = None
        try:
            conn = psycopg2.connect(
                host=os.getenv("DB_HOST", "127.0.0.1"),
                port=os.getenv("DB_PORT", "5432"),
                dbname=os.getenv("DB_NAME", "sietch_crm"),
                user=os.getenv("DB_USER", "sietch"),
                password=os.getenv("DB_PASSWORD", ""),
                connect_timeout=10,
            )
            conn.autocommit = True
        except Exception as exc:
            self.skipTest(f"no reachable PostgreSQL: {exc}")
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TEMP TABLE document_templates (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        company_name TEXT NOT NULL DEFAULT '',
                        company_address TEXT NOT NULL DEFAULT '',
                        company_city_state_zip TEXT NOT NULL DEFAULT '',
                        company_phone TEXT NOT NULL DEFAULT '',
                        company_email TEXT NOT NULL DEFAULT '',
                        primary_color TEXT NOT NULL DEFAULT '#1a1a1a',
                        invoice_title TEXT NOT NULL DEFAULT 'INVOICE',
                        coc_title TEXT NOT NULL DEFAULT 'CERTIFICATE OF COMPLETION',
                        default_terms TEXT NOT NULL DEFAULT 'Due on receipt',
                        default_sales_rep TEXT NOT NULL DEFAULT '',
                        default_line_items JSONB NOT NULL DEFAULT '[]'::jsonb,
                        show_signature BOOLEAN NOT NULL DEFAULT TRUE,
                        signature_name TEXT NOT NULL DEFAULT '',
                        signature_path TEXT,
                        show_branding_footer BOOLEAN NOT NULL DEFAULT TRUE,
                        is_default BOOLEAN NOT NULL DEFAULT FALSE
                    )""")
                sql = self.MIGRATION.read_text(encoding="utf-8")
                # Apply twice -- the second run must be a no-op.
                cur.execute(sql)
                cur.execute(sql)
                cur.execute("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema LIKE 'pg_temp%'
                      AND table_name = 'document_templates'
                      AND column_name IN ('company_website','company_license',
                                          'signature_title','show_signature_lines')
                    ORDER BY column_name""")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 4)
                for name, dtype, nullable, default in rows:
                    if name == "show_signature_lines":
                        self.assertEqual(dtype, "boolean", name)
                        self.assertEqual(nullable, "NO", name)
                        self.assertIn("true", str(default).lower(), name)
                    else:
                        self.assertEqual(dtype, "text", name)
                        self.assertEqual(nullable, "NO", name)
                        self.assertIn("''", str(default), name)
        finally:
            if conn is not None:
                conn.close()

    def test_server_inlines_the_same_columns(self):
        """server.py carries an inline ALTER for databases that skip the
        migration files, so the columns exist either way."""
        src = (REPO / "server.py").read_text(encoding="utf-8")
        for col in ("company_website", "company_license", "signature_title",
                    "show_signature_lines"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col}", src)

    def test_admin_api_accepts_the_new_fields(self):
        src = (REPO / "server.py").read_text(encoding="utf-8")
        # The create + update handlers must persist all of them.
        self.assertGreaterEqual(src.count('"company_website"'), 1)
        self.assertGreaterEqual(src.count('"company_license"'), 1)
        self.assertGreaterEqual(src.count('"signature_title"'), 1)
        self.assertGreaterEqual(src.count('"show_signature_lines"'), 1)
        self.assertIn("company_website", src)
        self.assertIn("company_license", src)
        self.assertIn("signature_title", src)
        self.assertIn("show_signature_lines", src)

    def test_admin_ui_exposes_the_new_fields(self):
        html = (REPO / "public" / "index.html").read_text(encoding="utf-8")
        for field_id in ("doc-tpl-admin-website", "doc-tpl-admin-license",
                         "doc-tpl-admin-sig-title", "doc-tpl-admin-show-sig-lines"):
            self.assertIn(f'id="{field_id}"', html)
        js = (REPO / "public" / "app.js").read_text(encoding="utf-8")
        for field_id in ("doc-tpl-admin-website", "doc-tpl-admin-license",
                         "doc-tpl-admin-sig-title", "doc-tpl-admin-show-sig-lines"):
            self.assertIn(field_id, js)
        for key in ("company_website", "company_license", "signature_title",
                    "show_signature_lines"):
            self.assertIn(key, js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
