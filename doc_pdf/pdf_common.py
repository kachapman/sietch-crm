"""Shared look for the modern invoice and certificate PDFs.

Ported from kachapman/sherwood-toolbox @ 3d5bf83 (toolbox/tools/documents/pdf_common.py)

A `brand` dict drives both layouts:
  name, address, city_state_zip, phone, email, website, license,
  accent (#rrggbb), logo_path, footer_text, invoice_title, coc_title.
Helvetica only (always available, no font files). No em dashes in any
fixed text.

Deviations from upstream 3d5bf83:
  * Module moved from a package-relative import (`from .pdf_common import ...`)
    to an absolute one (`from doc_pdf.pdf_common import ...`) because Sietch
    keeps it as a top-level package next to server.py.
  * `logo_flowable` and `numbered_canvas` gained an optional `root` argument
    so a repo-relative asset path can be resolved against the Sietch ROOT
    directory. When `root` is None the behaviour is identical to upstream.

v28.2 (upstream 94e66ac):
  * `coc_title(raw)` -- the certificate title as printed. Blank or any
    casing of "Certificate of Completion" gives "Certificate of Completion";
    another all-caps title is title-cased with short words ("of", "and",
    "the", ...) lowercased; a mixed-case title is left as typed. Ported
    verbatim from upstream bb35d36.

    It was moved OUT of this module into `doc_pdf.mapping` (which does not
    import reportlab) so the classic fpdf2 renderers can use it without
    pulling reportlab in -- the classic layout is the fallback when
    reportlab is missing, so it must not depend on it. It is re-exported
    here for the modern builders.
"""
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Image, Paragraph

# coc_title lives in doc_pdf.mapping, which does NOT import reportlab, so
# the classic (fpdf2) renderers can use it even when reportlab is missing.
# It is re-exported here for the modern builders, which already need
# reportlab anyway.
from doc_pdf.mapping import COC_TITLE, coc_title

INK = HexColor("#1c241c")
MUTED = HexColor("#5d6b5a")
LINE = HexColor("#d9ded3")
ZEBRA = HexColor("#f6f7f3")
FALLBACK_ACCENT = "#2c5130"


def accent(brand):
    try:
        return HexColor(brand.get("accent") or FALLBACK_ACCENT)
    except Exception:
        return HexColor(FALLBACK_ACCENT)


def _lum(c):
    def ch(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red) + 0.7152 * ch(c.green) + 0.0722 * ch(c.blue)


def on_accent(c):
    """Text color that reads on an accent fill."""
    return INK if _lum(c) > 0.45 else white


def text_accent(c):
    """Accent for text on white; very light accents fall back to ink."""
    return INK if _lum(c) > 0.55 else c


def tint(c, amount=0.08):
    return Color(1 - (1 - c.red) * amount, 1 - (1 - c.green) * amount, 1 - (1 - c.blue) * amount)


def style(name, size=9.5, color=INK, bold=False, align=TA_LEFT, leading=None, space_after=0):
    return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica",
                          fontSize=size, leading=leading or size * 1.3, textColor=color,
                          alignment=align, spaceAfter=space_after)


def p(text, st):
    """Paragraph from plain text (escaped, newlines kept)."""
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), st)


def _resolve_asset(path, root):
    """Resolve a possibly repo-relative asset path.

    `root` may be None (upstream behaviour: use the path as-is), a str, or a
    pathlib.Path. Absolute paths are always used unchanged.
    """
    if not path:
        return None
    if root is None:
        return path
    try:
        import os
        if os.path.isabs(str(path)):
            return str(path)
        return str(root / str(path))
    except Exception:
        return path


def logo_flowable(path, max_w, max_h, root=None):
    """Logo image flowable, or None when there is no usable logo."""
    if not path:
        return None
    resolved = _resolve_asset(path, root)
    try:
        iw, ih = ImageReader(resolved).getSize()
        scale = min(max_w / float(iw), max_h / float(ih))
        img = Image(resolved, width=iw * scale, height=ih * scale)
        img.hAlign = "LEFT"
        return img
    except Exception:
        return None


def company_lines(brand):
    """Contact lines under the company name (blank parts dropped).

    Sietch (2026-11-03): brand["license"] is already the finished label
    (mapping.license_label applies the "License " prefix when it is needed),
    so it is printed verbatim here. Upstream applied the prefix in this
    function instead.
    """
    lines = [brand.get("address"), brand.get("city_state_zip")]
    contact = "  |  ".join(x for x in (brand.get("phone"), brand.get("email")) if x)
    lines += [contact, brand.get("website")]
    if brand.get("license"):
        lines.append(brand["license"])
    return [x for x in lines if x]


def money(v):
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def numbered_canvas(brand, page_w, page_h):
    """Canvas class that draws the accent bar and a 'Page x of y' footer."""
    acc = accent(brand)
    footer = (brand.get("footer_text") or "").replace("\n", " ").strip()

    class _Numbered(rl_canvas.Canvas):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._chrome(total)
                super().showPage()
            super().save()

        def _chrome(self, total):
            self.saveState()
            self.setFillColor(acc)
            self.rect(0, page_h - 10, page_w, 10, stroke=0, fill=1)
            self.setStrokeColor(LINE)
            self.setLineWidth(0.6)
            self.line(43, 46, page_w - 43, 46)
            self.setFont("Helvetica", 8)
            self.setFillColor(MUTED)
            text = footer
            while text and self.stringWidth(text, "Helvetica", 8) > page_w - 160:
                text = text[:-2]
            if text != footer:
                text = text.rstrip() + "..."
            self.drawString(43, 33, text)
            self.drawRightString(page_w - 43, 33, f"Page {self._pageNumber} of {total}")
            self.restoreState()

    return _Numbered


__all__ = ["INK", "MUTED", "LINE", "ZEBRA", "accent", "on_accent", "text_accent", "tint",
           "style", "p", "logo_flowable", "company_lines", "money", "numbered_canvas",
           "coc_title", "COC_TITLE", "TA_LEFT", "TA_RIGHT"]
