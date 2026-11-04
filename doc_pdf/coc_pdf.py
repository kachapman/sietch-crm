"""Modern certificate of completion PDF (US Letter).

Ported from kachapman/sherwood-toolbox @ 3d5bf83 (toolbox/tools/documents/coc_pdf.py)
Updated for v28.2 (upstream 94e66ac).

job: customer_name, street, city_state_zip, job_number, insurance_claim,
     sales_rep, completion_date, note
opts: statement, signature_path, signer_name, signer_title,
      signatures (bool, default True; v28.2: False leaves out the whole
      SIGNATURES section, heading included)

Deviations from upstream 3d5bf83:
  * Absolute import of pdf_common (Sietch package layout).
  * `build_coc` accepts an optional `root` argument used to resolve a
    repo-relative logo/signature path.
  * `signature_path` is honored only when the caller asks for it (Sietch
    gates signatures on the template's show_signature flag); upstream always
    draws a contractor signature block.
  * **No property-owner signature line.** Upstream draws a second
    "Property owner" block gated on opts["homeowner_line"] (default True).
    Ken's decision (2026-11-03): the Sietch COC has NO homeowner line, so
    that branch was removed and the `homeowner_line` option no longer
    exists.

v28.2 (upstream 94e66ac):
  * `opts["signatures"]` (bool, default True). False leaves out the whole
    SIGNATURES section -- the heading, the contractor block and the (removed)
    homeowner block. Ported from upstream bee1b9e.
  * The title now comes from `pdf_common.coc_title()` (upstream bb35d36)
    instead of an inline `.title()` call, so "CERTIFICATE OF COMPLETION"
    renders as "Certificate of Completion" and a custom mixed-case title is
    left exactly as typed.
"""
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.platypus import HRFlowable, KeepTogether, SimpleDocTemplate, Spacer, Table, TableStyle

from doc_pdf.pdf_common import (INK, LINE, MUTED, TA_RIGHT, accent, coc_title,
                                company_lines, logo_flowable, numbered_canvas, p,
                                style, text_accent, tint)


PAGE_W, PAGE_H = letter
M = 50
CONTENT_W = PAGE_W - 2 * M


def _header(brand, root=None):
    logo = logo_flowable(brand.get("logo_path"), 210, 70, root=root)
    left = logo or p(brand.get("name") or "", style("cn", 15, INK, True))
    right = [p(brand.get("name") or "", style("co", 10.5, INK, True, TA_RIGHT))] if logo else []
    right += [p(x, style("cl", 9, MUTED, align=TA_RIGHT)) for x in company_lines(brand)]
    t = Table([[left, right]], colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _details(job, acc):
    addr = ", ".join(x for x in (job.get("street"), job.get("city_state_zip")) if x)
    rows = [("Customer", job.get("customer_name")), ("Property address", addr),
            ("Job number", job.get("job_number")), ("Claim number", job.get("insurance_claim")),
            ("Sales rep", job.get("sales_rep")), ("Completion date", job.get("completion_date")),
            ("Scope / notes", job.get("note"))]
    data = [[p(k.upper(), style("dk", 7.5, MUTED, True)), p(v, style("dv", 10.5, INK))] for k, v in rows if v]
    t = Table(data, colWidths=[120, CONTENT_W - 120])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
                           ("BACKGROUND", (0, 0), (-1, -1), tint(acc, 0.05)),
                           ("LINEABOVE", (0, 0), (-1, 0), 2, acc), ("LEFTPADDING", (0, 0), (-1, -1), 12)]))
    return t


def _sig_block(img_path, name_line, label, date_text, root=None):
    w = (CONTENT_W - 40) / 2
    top = logo_flowable(img_path, w * 0.8, 48, root=root) if img_path else None
    cells = [top or Spacer(1, 48),
             HRFlowable(width="100%", thickness=0.8, color=INK, spaceBefore=2, spaceAfter=4),
             p(name_line or label, style("sn", 9.5, INK, True)),
             p(label if name_line else "", style("sl", 8, MUTED)),
             p(date_text, style("sd", 9, MUTED))]
    return cells


def _signatures(job, opts, acc, root=None):
    """The SIGNATURES section: heading + the contractor signature block.

    Sietch decision 2026-11-03: there is NO property-owner signature line on
    the COC. The upstream Sherwood builder draws a second "Property owner"
    block; that branch was removed here, so only the contractor block is
    drawn.
    """
    signer = ", ".join(x for x in (opts.get("signer_name"), opts.get("signer_title")) if x)
    contractor = _sig_block(opts.get("signature_path"), signer, "Contractor representative",
                            f"Date: {job.get('completion_date') or ''}", root=root)
    sig = Table([[contractor]], colWidths=[CONTENT_W])
    sig.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return KeepTogether([p("SIGNATURES", style("sh", 7.5, text_accent(acc), True)), Spacer(1, 10), sig])


def build_coc(out_path, brand, job, opts, root=None):
    """Render a modern Certificate of Completion PDF."""
    acc = accent(brand)
    doc = SimpleDocTemplate(out_path, pagesize=letter, leftMargin=M, rightMargin=M, topMargin=40,
                            bottomMargin=62, title="Certificate of Completion", author=brand.get("name") or "")
    title = coc_title(brand.get("coc_title"))
    story = [_header(brand, root=root), Spacer(1, 44),
             p(title, style("t", 28, INK, True, TA_CENTER, 34)), Spacer(1, 8),
             HRFlowable(width=70, thickness=3, color=acc, hAlign="CENTER"), Spacer(1, 16),
             p(opts.get("statement") or "", style("st", 11, MUTED, align=TA_CENTER, leading=16)),
             Spacer(1, 26), _details(job, acc)]
    # v28.2: opts["signatures"] (default True). False leaves out the whole
    # SIGNATURES section -- the heading included.
    if opts.get("signatures", True):
        story += [Spacer(1, 48), _signatures(job, opts, acc, root=root)]
    doc.build(story, canvasmaker=numbered_canvas(brand, PAGE_W, PAGE_H))
