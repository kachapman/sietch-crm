"""Modern invoice PDF (US Letter). See pdf_common for the brand dict.

Ported from kachapman/sherwood-toolbox @ 3d5bf83 (toolbox/tools/documents/invoice_pdf.py)

job: customer_name, street, city_state_zip, job_number, insurance_claim,
     sales_rep, invoice_number, invoice_date, terms, invoice_notes
items: list of (description, amount); zero-amount rows are dropped.

Deviations from upstream 3d5bf83:
  * Absolute import of pdf_common (Sietch package layout).
  * `build_invoice` accepts an optional `signature` dict
    ({path, name, date}) and renders a signature block when given. Upstream
    has no invoice signature block; Sietch's classic fpdf2 invoice did, so
    this is a Sietch-only additive change (see docs/features/2026-11-03-invoice-coc-upgrade.md).
  * `build_invoice` accepts an optional `root` argument used to resolve a
    repo-relative logo/signature path.
  * `keep_zero_rows=False` default keeps upstream behaviour (zero-amount rows
    are dropped). Sietch's classic renderer dropped them too, so totals match.
"""
from reportlab.lib.pagesizes import letter
from reportlab.platypus import HRFlowable, KeepTogether, SimpleDocTemplate, Spacer, Table, TableStyle

from doc_pdf.pdf_common import (INK, LINE, MUTED, TA_RIGHT, ZEBRA, accent, company_lines,
                                logo_flowable, money, numbered_canvas, on_accent, p, style,
                                text_accent, tint)

PAGE_W, PAGE_H = letter
M = 43
CONTENT_W = PAGE_W - 2 * M


def _header(brand, job, acc, root=None):
    left = []
    logo = logo_flowable(brand.get("logo_path"), 190, 64, root=root)
    if logo:
        left += [logo, Spacer(1, 8)]
    left.append(p(brand.get("name") or "", style("co", 12, INK, True)))
    for line in company_lines(brand):
        left.append(p(line, style("col", 9, MUTED)))
    title = (brand.get("invoice_title") or "INVOICE").upper()
    meta_rows = [("Invoice #", job.get("invoice_number")), ("Date", job.get("invoice_date")),
                 ("Terms", job.get("terms")), ("Job #", job.get("job_number"))]
    meta = Table([[p(k, style("mk", 8.5, MUTED, align=TA_RIGHT)), p(v, style("mv", 9.5, INK, True, TA_RIGHT))]
                  for k, v in meta_rows if v], colWidths=[70, 120])
    meta.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    right = [p(title, style("t", 26, text_accent(acc), True, TA_RIGHT, 30)), Spacer(1, 10), meta]
    t = Table([[left, right]], colWidths=[CONTENT_W - 200, 200])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _cards(job, acc):
    lab = style("lab", 7.5, text_accent(acc), True)
    bill = [p("BILL TO", lab), Spacer(1, 3), p(job.get("customer_name"), style("bn", 11, INK, True))]
    for x in (job.get("street"), job.get("city_state_zip")):
        if x:
            bill.append(p(x, style("ba", 9.5, INK)))
    proj = [p("PROJECT", lab), Spacer(1, 3)]
    addr = ", ".join(x for x in (job.get("street"), job.get("city_state_zip")) if x)
    for k, v in (("Property", addr), ("Claim #", job.get("insurance_claim")),
                 ("Job #", job.get("job_number")), ("Rep", job.get("sales_rep"))):
        if v:
            proj.append(p(f"{k}: {v}", style("pj", 9.5, INK)))
    gap = 12
    t = Table([[bill, "", proj]], colWidths=[(CONTENT_W - gap) / 2, gap, (CONTENT_W - gap) / 2])
    bg = tint(acc, 0.07)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), bg),
                           ("BACKGROUND", (2, 0), (2, 0), bg), ("PADDING", (0, 0), (-1, -1), 10),
                           ("LINEABOVE", (0, 0), (0, 0), 2, acc), ("LINEABOVE", (2, 0), (2, 0), 2, acc)]))
    return t


def _items(items, acc):
    head_c = on_accent(acc)
    hs = style("h", 8.5, head_c, True)
    rows = [[p("#", hs), p("DESCRIPTION", hs), p("AMOUNT", style("ha", 8.5, head_c, True, TA_RIGHT))]]
    for i, (desc, amt) in enumerate(items, 1):
        rows.append([p(str(i), style("n", 9, MUTED)), p(desc, style("d", 10, INK)),
                     p(money(amt), style("a", 10, INK, align=TA_RIGHT))])
    if len(rows) == 1:
        rows.append(["", p("No line items", style("d0", 10, MUTED)), ""])
    t = Table(rows, colWidths=[28, CONTENT_W - 28 - 110, 110], repeatRows=1)
    cmds = [("BACKGROUND", (0, 0), (-1, 0), acc), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE)]
    for r in range(2, len(rows), 2):
        cmds.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
    t.setStyle(TableStyle(cmds))
    return t


def _totals(total, acc):
    rows = [[p("Subtotal", style("sl", 10, MUTED, align=TA_RIGHT)), p(money(total), style("sv", 10, INK, align=TA_RIGHT))],
            [p("Total due", style("tl", 12, on_accent(acc), True, TA_RIGHT)),
             p(money(total), style("tv", 13, on_accent(acc), True, TA_RIGHT, 16))]]
    t = Table(rows, colWidths=[110, 120], hAlign="RIGHT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 1), (-1, 1), acc), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 1), (-1, 1), 8), ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
    return t


def _signature(signature, acc, root=None):
    """Sietch-only signature block (classic fpdf2 invoice had one).

    signature: {path, name, title, date}. The image is optional; the
    name/title/date lines are printed either way. Empty parts are omitted
    rather than printed as blank labels.
    """
    name = (signature.get("name") or "").strip()
    title = (signature.get("title") or "").strip()
    date_text = (signature.get("date") or "").strip()
    img = logo_flowable(signature.get("path"), 170, 48, root=root)
    cells = []
    if img:
        cells += [img, Spacer(1, 4)]
    cells.append(HRFlowable(width="100%", thickness=0.8, color=INK, spaceBefore=2, spaceAfter=4))
    if name:
        cells.append(p(f"By {name}", style("sn", 9.5, INK, True)))
    if title:
        cells.append(p(title, style("st", 8.5, MUTED)))
    if date_text:
        cells.append(p(f"Date: {date_text}", style("sd", 9, MUTED)))
    if len(cells) == 1:
        return None
    return KeepTogether(cells)


def build_invoice(out_path, brand, job, items, signature=None, root=None):
    """Render a modern invoice PDF. Returns the grand total as a float."""
    acc = accent(brand)
    items = [(str(d or "").strip() or "Item", float(a)) for d, a in items if float(a or 0) != 0]
    total = sum(a for _d, a in items)
    doc = SimpleDocTemplate(out_path, pagesize=letter, leftMargin=M, rightMargin=M, topMargin=36,
                            bottomMargin=62, title=f"Invoice {job.get('invoice_number') or ''}".strip(),
                            author=brand.get("name") or "")
    story = [_header(brand, job, acc, root=root), Spacer(1, 22), _cards(job, acc), Spacer(1, 20),
             _items(items, acc), Spacer(1, 12), KeepTogether([_totals(total, acc)])]
    if job.get("invoice_notes"):
        story += [Spacer(1, 18), KeepTogether([p("NOTES", style("nl", 7.5, text_accent(acc), True)), Spacer(1, 3),
                                               p(job["invoice_notes"], style("nt", 9.5, INK))])]
    if signature:
        sig = _signature(signature, acc, root=root)
        if sig is not None:
            story += [Spacer(1, 22), sig]
    doc.build(story, canvasmaker=numbered_canvas(brand, PAGE_W, PAGE_H))
    return total
