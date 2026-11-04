#!/usr/bin/env python3
"""Build sample Invoice + Certificate of Completion PDFs for visual review.

Reads (READ ONLY) the local sietch-postgres DB for real dev projects and the
default `document_templates` row, then renders:

  * the modern (Sherwood-style ReportLab) invoice + COC for each project,
  * the classic (fpdf2) invoice + COC for the same projects, side by side,
  * one long multi-page modern invoice.

Output goes to tmp/invoice-coc-review/ (gitignored): the PDFs plus a PNG of
page 1 of each.

The script never writes to the database. If the local DB has no projects or
no document_templates rows (the usual case on a fresh dev box), it falls
back to a realistic seeded sample so the review pack is never empty.

Usage:
    python3 scripts/make_invoice_coc_review.py [--out DIR]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from doc_pdf import mapping as doc_mapping           # noqa: E402
from doc_pdf.coc_pdf import build_coc                # noqa: E402
from doc_pdf.invoice_pdf import build_invoice        # noqa: E402

try:
    from fpdf import FPDF                             # noqa: E402
    _HAVE_FPDF = True
except Exception:
    _HAVE_FPDF = False


# ── Sample data ─────────────────────────────────────────────────────────────

FALLBACK_TEMPLATE = {
    "id": 1,
    "name": "Public Adjustment Midwest",
    "logo_path": None,
    "company_name": "Public Adjustment Midwest LLC",
    "company_address": "4729 Easton Way, Suite 210",
    "company_city_state_zip": "Columbus, OH 43219",
    "company_phone": "(614) 555-0142",
    "company_email": "claims@publicadjustermidwest.com",
    # Branding extras (migration 045). Left blank in the fallback so the
    # "no extras" variant is what the default sample renders.
    "company_website": "",
    "company_license": "",
    "signature_title": "",
    "primary_color": "#1f4e79",
    "invoice_title": "INVOICE",
    "coc_title": "CERTIFICATE OF COMPLETION",
    "default_terms": "Due on receipt",
    "default_sales_rep": "Ken Chapman",
    "default_line_items": [
        {"description": "Emergency board-up and tarp", "amount": "1250.00"},
        {"description": "Water mitigation and drying", "amount": "3400.00"},
        {"description": "Structural repair estimate", "amount": "18750.00"},
    ],
    "show_signature": True,
    "signature_name": "Ken Chapman",
    "signature_path": None,
    "show_branding_footer": True,
    "is_default": True,
}

# A second template with every branding extra filled in, so the review pack
# shows both "no extras" (values omitted) and "with extras" (values printed).
TEMPLATE_WITH_EXTRAS = dict(
    FALLBACK_TEMPLATE,
    id=2,
    name="Public Adjustment Midwest (with website + license + signer title)",
    company_website="www.publicadjustermidwest.com",
    company_license="OH LIC 104.8872",
    signature_title="Licensed Public Adjuster",
)

# Realistic stand-ins used when the local DB has no projects.
FALLBACK_PROJECTS = [
    {
        "id": 101,
        "project_number": "1042",
        "title": "Jordan Ellis",
        "customer_name": "Jordan Ellis",
        "street": "1428 Maple Ridge Dr",
        "city_state_zip": "Columbus, OH 43215",
        "claim_number": "CLM-2026-99881",
        "sales_rep": "Ken Chapman",
        "base_amount": "500.00",
        "line_items": [
            {"description": "Emergency board-up and tarp", "amount": "1250.00"},
            {"description": "Water mitigation and drying", "amount": "3400.00"},
            {"description": "Roof replacement - 32 sq tear off and re-roof", "amount": "18450.00"},
            {"description": "Gutter replacement, 178 ft", "amount": "2310.50"},
        ],
        "notes": "Payment due within 30 days. Please remit to the address on file "
                 "or by ACH. Work performed under estimate dated 10/18/2026.",
    },
    {
        "id": 102,
        "project_number": "1057",
        "title": "Hollis Family Trust",
        "customer_name": "Hollis Family Trust",
        "street": "88 Cooperstone Ct",
        "city_state_zip": "Westerville, OH 43081",
        "claim_number": "CLM-2026-10042",
        "sales_rep": "Dana Whitfield",
        "base_amount": "0",
        "line_items": [
            {"description": "Interior drywall repair and paint", "amount": "4875.00"},
            {"description": "Hardwood floor refinishing", "amount": "6200.00"},
        ],
        "notes": "Includes moisture mitigation and mold remediation of affected "
                 "wall cavities.",
    },
    {
        "id": 103,
        "project_number": "1063",
        "title": "Miguel Santos",
        "customer_name": "Miguel Santos",
        "street": "3407 Fairwood Ave",
        "city_state_zip": "Dublin, OH 43017",
        "claim_number": "CLM-2026-10455",
        "sales_rep": "Ken Chapman",
        "base_amount": "275.00",
        "line_items": [
            {"description": "Permit and inspection fees", "amount": "275.00"},
            {"description": "HVAC condenser replacement, 3 ton", "amount": "6120.00"},
        ],
        "notes": "",
    },
]


def read_db_samples():
    """Read (never write) real dev projects + the default template.

    Uses a plain read-only psycopg2 connection (no db.init_db(), so no
    migrations are applied and nothing is written).
    """
    template = None
    projects = []
    conn = None
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "db"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "sietch_crm"),
            user=os.getenv("DB_USER", "sietch"),
            password=os.getenv("DB_PASSWORD", ""),
            connect_timeout=10,
        )
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_templates WHERE is_default = TRUE ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM document_templates ORDER BY id LIMIT 1")
                row = cur.fetchone()
            if row:
                template = dict(row)
            cur.execute(
                """SELECT id, project_number, title FROM opportunities
                   ORDER BY id DESC LIMIT 3""")
            for r in cur.fetchall() or []:
                projects.append({
                    "id": r.get("id"),
                    "project_number": str(r.get("project_number") or ""),
                    "title": r.get("title") or "Customer",
                })
    except Exception as exc:
        print(f"  (DB read skipped: {exc})")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return template, projects


def build_payload(project, index):
    """Payload shaped exactly like the invoice/COC modal sends.

    The DB project supplies the identity (id, project_number, title); the
    matching seeded sample supplies the address / claim / line items so the
    review PDFs look like real documents even on a dev box with no CRM data.
    """
    base = FALLBACK_PROJECTS[index % len(FALLBACK_PROJECTS)]
    title = (project.get("title") or "").strip()
    project_number = str(project.get("project_number") or "").strip()
    # When the DB row names a customer we do not have a seeded address for,
    # fall back to the seeded sample that carries the same title.
    match = next((p for p in FALLBACK_PROJECTS
                  if p["customer_name"] == title), None)
    if match is None:
        match = base
    merged = dict(match)
    merged["id"] = project.get("id")
    if project_number:
        merged["project_number"] = project_number
    if title:
        merged["title"] = title
        merged["customer_name"] = title
    return {
        "invoice_number": merged.get("project_number") or "1042",
        "invoice_date": "11/03/2026",
        "terms": "Due on receipt",
        "sales_rep": merged.get("sales_rep") or "Ken Chapman",
        "claim_number": merged.get("claim_number") or "",
        "project_number": merged.get("project_number") or "",
        "customer_name": merged.get("customer_name") or merged.get("title") or "Customer",
        "street": merged.get("street") or "",
        "city_state_zip": merged.get("city_state_zip") or "",
        "notes": merged.get("notes") or "",
        "base_amount": merged.get("base_amount") or "0",
        "line_items": merged.get("line_items") or [],
        "include_signature": True,
        "signature_name": "Ken Chapman",
        "completion_date": "11/03/2026",
        "note": "Work completed per agreement. All work inspected and approved "
                "by the owner on site.",
    }


# ── Classic (fpdf2) renderer, mirroring server.py's _create_invoice_pdf ─────

def classic_invoice(out_path, payload, template):
    from fpdf import FPDF
    from doc_pdf.mapping import BASE_CHARGE_LABEL, license_label
    hex_color = (template.get("primary_color") or "#1a1a1a").lstrip("#")
    r_c, g_c, b_c = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    grand_total = float(payload.get("base_amount") or 0)
    for li in payload["line_items"]:
        grand_total += float(li.get("amount") or 0)
    pdf = FPDF(orientation="P", unit="in", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(left=0.5, top=0.5, right=0.5)
    pdf.add_page()
    usable_w, margin_l, margin_r = 7.5, 0.5, 0.5
    page_w, page_h = 8.5, 11.0
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_xy(page_w - margin_r - 3.0, 0.5)
    pdf.cell(3.0, 0.4, template.get("invoice_title") or "INVOICE", align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(page_w - margin_r - 2.5, 0.95)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(2.5, 0.18, template.get("company_name") or "", align="R", ln=1)
    pdf.set_font("Helvetica", "", 9)
    comp_license = license_label(template.get("company_license"))
    for value in (template.get("company_address"), template.get("company_city_state_zip"),
                  template.get("company_phone"), template.get("company_email"),
                  template.get("company_website"), comp_license):
        if value:
            pdf.set_x(page_w - margin_r - 2.5)
            pdf.cell(2.5, 0.15, str(value), align="R", ln=1)
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.01)
    pdf.line(margin_l, 1.85, page_w - margin_r, 1.85)
    bill_to_y = 2.05
    pdf.set_xy(margin_l, bill_to_y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.cell(2.0, 0.18, "BILL TO", ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_x(margin_l)
    pdf.cell(3.5, 0.22, payload["customer_name"], ln=1)
    pdf.set_font("Helvetica", "", 10)
    if payload.get("street"):
        pdf.set_x(margin_l)
        pdf.cell(3.5, 0.18, payload["street"], ln=1)
    if payload.get("city_state_zip"):
        pdf.set_x(margin_l)
        pdf.cell(3.5, 0.18, payload["city_state_zip"], ln=1)
    meta_x, meta_w = page_w - margin_r - 2.7, 2.7
    meta_pairs = [("Invoice #", payload["invoice_number"]), ("Date", payload["invoice_date"]),
                  ("Job #", payload["project_number"]), ("Claim #", payload["claim_number"]),
                  ("Terms", payload["terms"])]
    for i, (label, value) in enumerate(meta_pairs):
        yy = bill_to_y + i * 0.22
        pdf.set_xy(meta_x, yy)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(0.9, 0.18, label, ln=0)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(meta_w - 0.9, 0.18, str(value or "-"), ln=1)
    table_top, header_h, row_h = 3.30, 0.28, 0.22
    pdf.set_fill_color(240, 240, 240)
    pdf.set_xy(margin_l, table_top)
    pdf.cell(usable_w, header_h, "", fill=True, ln=1)
    pdf.set_xy(margin_l, table_top)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.cell(usable_w - 1.3, header_h, "DESCRIPTION", align="L")
    pdf.cell(1.3, header_h, "AMOUNT", align="R")
    pdf.set_text_color(0, 0, 0)
    y = table_top + header_h
    if float(payload.get("base_amount") or 0) > 0:
        pdf.set_xy(margin_l, y)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(usable_w - 1.3, row_h, BASE_CHARGE_LABEL, align="L")
        pdf.cell(1.3, row_h, f"${float(payload['base_amount']):,.2f}", align="R")
        y += row_h
    for li in payload["line_items"]:
        desc = (li.get("description") or "").strip()
        amt = float(li.get("amount") or 0)
        if not desc and amt == 0:
            continue
        # $0.00 rows are hidden (Sietch decision 2026-11-03).
        if amt == 0:
            continue
        if y + row_h + 0.6 > page_h - 0.5:
            pdf.add_page()
            y = 0.7
            pdf.set_fill_color(240, 240, 240)
            pdf.set_xy(margin_l, 0.7)
            pdf.cell(usable_w, header_h, "", fill=True, ln=1)
            pdf.set_xy(margin_l, 0.7)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(r_c, g_c, b_c)
            pdf.cell(usable_w - 1.3, header_h, "DESCRIPTION", align="L")
            pdf.cell(1.3, header_h, "AMOUNT", align="R")
            pdf.set_text_color(0, 0, 0)
            y = 0.7 + header_h
        pdf.set_xy(margin_l, y)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(usable_w - 1.3, row_h, desc, align="L")
        pdf.cell(1.3, row_h, f"${amt:,.2f}" if amt >= 0 else f"-${abs(amt):,.2f}", align="R")
        y += row_h
    if y + 0.6 > page_h - 0.5:
        pdf.add_page()
        y = 1.0
    y += 0.1
    pdf.set_fill_color(240, 240, 240)
    pdf.rect(margin_l, y, usable_w, 0.35, style="F")
    pdf.set_xy(margin_l + 0.1, y + 0.05)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.cell(usable_w - 1.5, 0.25, "Grand Total", align="L")
    pdf.cell(1.4, 0.25, f"${grand_total:,.2f}", align="R")
    pdf.set_text_color(0, 0, 0)
    y += 0.45
    if payload.get("notes"):
        pdf.set_xy(margin_l, y)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(usable_w, 0.18, "Notes:", ln=1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(margin_l)
        pdf.multi_cell(usable_w, 0.16, payload["notes"], align="L")
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.01)
    pdf.line(margin_l, page_h - 0.4, page_w - margin_r, page_h - 0.4)
    pdf.set_xy(margin_l, page_h - 0.35)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    left_txt = f"Job #{payload['project_number']}" if payload["project_number"] else f"Invoice #{payload['invoice_number']}"
    pdf.cell(usable_w * 0.5, 0.15, left_txt, align="L")
    pdf.cell(usable_w * 0.5, 0.15, f"Page {pdf.page_no()} of {{{{nb}}}}", align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.output(out_path)


def classic_coc(out_path, payload, template, tpl=None):
    from fpdf import FPDF
    from doc_pdf.mapping import license_label
    from doc_pdf.pdf_common import coc_title as coc_title_fn
    hex_color = (template.get("primary_color") or "#1a1a1a").lstrip("#")
    r_c, g_c, b_c = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    pdf = FPDF(orientation="P", unit="in", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(left=0.75, top=0.75, right=0.75)
    pdf.add_page()
    margin_l = margin_r = 0.75
    page_w, page_h = 8.5, 11.0
    usable_w = page_w - margin_l - margin_r
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.set_xy(margin_l, 1.55)
    pdf.set_font("Helvetica", "", 10)
    comp_license = license_label(template.get("company_license"))
    for value in (template.get("company_address"), template.get("company_city_state_zip"),
                  template.get("company_email"), template.get("company_phone"),
                  template.get("company_website"), comp_license):
        if value:
            pdf.cell(usable_w, 0.16, str(value), align="R", ln=1)
            pdf.set_x(margin_l)
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(margin_l, 2.55)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(r_c, g_c, b_c)
    coc_title = coc_title_fn(template.get("coc_title"))
    title_w = pdf.get_string_width(coc_title)
    pdf.set_x((page_w - title_w) / 2)
    pdf.cell(title_w, 0.3, coc_title, ln=1)
    pdf.set_draw_color(r_c, g_c, b_c)
    pdf.set_line_width(0.015)
    title_y = pdf.get_y() + 0.02
    pdf.line((page_w - title_w) / 2, title_y, (page_w - title_w) / 2 + title_w, title_y)
    pdf.set_text_color(0, 0, 0)
    pdf.set_line_width(0.01)
    fields_y = title_y + 0.45
    address_one = ", ".join(filter(None, [payload.get("street"), payload.get("city_state_zip")]))
    fields = [("Customer:", payload["customer_name"]), ("Address:", address_one),
              ("Job #:", payload["project_number"]), ("Claim #:", payload["claim_number"]),
              ("Sales Rep:", payload["sales_rep"]),
              ("Completion Date:", payload.get("completion_date") or "")]
    label_w, line_h = 1.6, 0.30
    for i, (label, value) in enumerate(fields):
        yy = fields_y + i * line_h
        pdf.set_xy(margin_l, yy)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(label_w, 0.22, label)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(usable_w - label_w, 0.22, str(value or "-"))
    note_y = fields_y + len(fields) * line_h + 0.15
    pdf.set_xy(margin_l, note_y)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.cell(label_w, 0.22, "Note:")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_xy(margin_l + label_w, note_y)
    pdf.multi_cell(usable_w - label_w, 0.22, payload.get("note") or "Work completed per agreement.", align="L")
    # ── Signature block (only when the template has a signature image) ─────
    sig_path = (tpl or template).get("signature_path")
    show_lines = (tpl or template).get("show_signature_lines", True)
    if sig_path and show_lines:
        try:
            _sp = Path(str(sig_path))
            if not _sp.is_absolute():
                _sp = REPO / _sp
            if _sp.exists():
                sig_w, sig_h = 1.7, 0.55
                sig_x = page_w - margin_r - sig_w
                sig_y = page_h - 2.0
                pdf.image(str(_sp), x=sig_x, y=sig_y, w=sig_w, h=sig_h)
                pdf.set_draw_color(180, 180, 180)
                pdf.set_line_width(0.01)
                pdf.line(sig_x, sig_y + sig_h + 0.05, sig_x + sig_w, sig_y + sig_h + 0.05)
                pdf.set_xy(sig_x, sig_y + sig_h + 0.1)
                pdf.set_font("Helvetica", "", 8)
                pdf.cell(sig_w, 0.14, f"By {payload.get('signature_name') or 'Representative'}", align="L")
                pdf.set_x(sig_x)
                pdf.cell(sig_w, 0.14, f"Date: {payload.get('completion_date') or ''}", align="L")
        except Exception:
            pass
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.01)
    pdf.line(margin_l, page_h - 0.4, page_w - margin_r, page_h - 0.4)
    pdf.set_xy(margin_l, page_h - 0.35)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    if template.get("show_branding_footer", True):
        pdf.cell(usable_w * 0.5, 0.15, "Generated by Sietch CRM", align="L")
    else:
        pdf.cell(usable_w * 0.5, 0.15, "", align="L")
    pdf.cell(usable_w * 0.5, 0.15, f"Page {pdf.page_no()} of {{{{nb}}}}", align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.output(out_path)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "tmp" / "invoice-coc-review"))
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output dir: {out_dir}")
    template, db_projects = read_db_samples()
    if not template:
        print("  No document_templates row in the local DB -> using the seeded sample template.")
        template = dict(FALLBACK_TEMPLATE)
    else:
        print(f"  Using document_templates id={template.get('id')} from the local DB.")
    if not db_projects:
        print("  No opportunities in the local DB -> using the seeded sample projects.")
        db_projects = [{"id": p["id"], "project_number": p["project_number"], "title": p["title"]}
                       for p in FALLBACK_PROJECTS]
    else:
        print(f"  Using {len(db_projects)} project(s) from the local DB.")

    made = []

    # The template under review, plus a variant with every branding extra
    # filled in (website / license / signer title) so the pack shows both
    # "extras omitted" and "extras printed".
    template_extras = dict(template)
    for key, value in (("company_website", TEMPLATE_WITH_EXTRAS["company_website"]),
                       ("company_license", TEMPLATE_WITH_EXTRAS["company_license"]),
                       ("signature_title", TEMPLATE_WITH_EXTRAS["signature_title"])):
        if not (template_extras.get(key) or "").strip():
            template_extras[key] = value

    def _emit(payload, safe_stem, tpl, suffix, coc_only=False):
        """Render one project in both layouts with one template."""
        # Modern
        brand = doc_mapping.build_brand(tpl, root=REPO)
        job = doc_mapping.build_job(payload, today="11/03/2026")
        if not coc_only:
            items = doc_mapping.build_items(payload)
            sig = doc_mapping.build_invoice_signature(payload, tpl, root=REPO)
            inv_modern = out_dir / f"{safe_stem}-invoice-modern{suffix}.pdf"
            build_invoice(str(inv_modern), brand, job, items, signature=sig, root=REPO)
            made.append(inv_modern)

        opts = doc_mapping.build_coc_opts(payload, tpl, root=REPO)
        coc_modern = out_dir / f"{safe_stem}-coc-modern{suffix}.pdf"
        build_coc(str(coc_modern), brand, job, opts, root=REPO)
        made.append(coc_modern)

        # Classic
        if _HAVE_FPDF:
            if not coc_only:
                inv_classic = out_dir / f"{safe_stem}-invoice-classic{suffix}.pdf"
                classic_invoice(str(inv_classic), payload, tpl)
                made.append(inv_classic)
            coc_classic = out_dir / f"{safe_stem}-coc-classic{suffix}.pdf"
            classic_coc(str(coc_classic), payload, tpl, tpl)
            made.append(coc_classic)

    for idx, project in enumerate(db_projects):
        payload = build_payload(project, idx)
        stem = f"project-{project.get('id')}-{project.get('project_number')}"
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem)
        # No suffix -> the template as-is (extras omitted when empty).
        _emit(payload, safe_stem, template, "")

    # One project rendered again with every branding extra filled in.
    extra_payload = build_payload(db_projects[0], 0)
    extra_stem = "with-extras-" + "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in f"project-{db_projects[0].get('id')}-{db_projects[0].get('project_number')}")
    _emit(extra_payload, extra_stem, template_extras, "")

    # ── v28.2: the certificate with the signature lines ON and OFF ─────────
    # A stand-in signature image so the classic layout's signature block
    # (which needs an image file) is visible in the review.
    sig_img = out_dir / "_review-signature.png"
    try:
        from PIL import Image as _PILImage
        _PILImage.new("RGB", (240, 80), (28, 42, 96)).save(sig_img)
    except Exception:
        sig_img = None

    sig_payload = build_payload(db_projects[0], 0)
    sig_payload["signature_name"] = "Ken Chapman"
    sig_stem = "coc-signatures-" + "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in f"project-{db_projects[0].get('id')}-{db_projects[0].get('project_number')}")

    template_sig = dict(template_extras)
    if sig_img is not None:
        template_sig["signature_path"] = str(sig_img)
    template_sig["show_signature_lines"] = True
    _emit(sig_payload, sig_stem + "-on", template_sig, "-lines-on", coc_only=True)

    template_nosig = dict(template_sig)
    template_nosig["show_signature_lines"] = False
    _emit(sig_payload, sig_stem + "-off", template_nosig, "-lines-off", coc_only=True)

    # One long multi-page modern invoice.
    long_payload = build_payload(db_projects[0], 0)
    long_payload["line_items"] = [
        {"description": f"Line item {i:03d} - scope of work performed on the property",
         "amount": f"{(i * 137.5):.2f}"} for i in range(1, 61)]
    brand = doc_mapping.build_brand(template, root=REPO)
    job = doc_mapping.build_job(long_payload, today="11/03/2026")
    items = doc_mapping.build_items(long_payload)
    long_pdf = out_dir / "long-multipage-invoice-modern.pdf"
    build_invoice(str(long_pdf), brand, job, items, root=REPO)
    made.append(long_pdf)

    # A $0.00 line item must NOT be printed (Sietch decision 2026-11-03).
    zero_payload = build_payload(db_projects[0], 0)
    zero_payload["line_items"] = list(zero_payload["line_items"]) + [
        {"description": "Follow-up site visit (no charge)", "amount": "0.00"},
        {"description": "Second zero row", "amount": "0"},
    ]
    zero_stem = "zero-line-hidden"
    brand = doc_mapping.build_brand(template, root=REPO)
    job = doc_mapping.build_job(zero_payload, today="11/03/2026")
    items = doc_mapping.build_items(zero_payload)
    zero_pdf = out_dir / f"{zero_stem}-invoice-modern.pdf"
    build_invoice(str(zero_pdf), brand, job, items, root=REPO)
    made.append(zero_pdf)
    if _HAVE_FPDF:
        zero_classic = out_dir / f"{zero_stem}-invoice-classic.pdf"
        classic_invoice(str(zero_classic), zero_payload, template)
        made.append(zero_classic)

    # Page-1 PNGs for a quick visual scan.
    for pdf in made:
        stem = pdf.with_suffix("")
        subprocess.run(["pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "110",
                        str(pdf), str(stem)],
                       capture_output=True)
        produced = sorted(stem.parent.glob(stem.name + "-*.png"))
        target = stem.with_name(stem.name + ".page1.png")
        if produced:
            produced[0].replace(target)

    print("\nGenerated:")
    for pdf in sorted(made):
        print(f"  {pdf.relative_to(REPO) if str(pdf).startswith(str(REPO)) else pdf}"
              f"  ({pdf.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
