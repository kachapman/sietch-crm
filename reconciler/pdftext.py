"""Extract text from PDFs with ``pdftotext -layout`` and detect scans.

The reconciler never OCRs.  A PDF without a usable text layer comes back as a
:class:`PdfTextResult` with ``status == "needs_ocr"`` and a human-readable
``reason``.  Callers can then hand the file to the (separate, later-phase)
OCR worker.  This module never raises for a scanned or garbled document.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = [
    "PdfTextResult",
    "extract_text",
    "extract_bbox_continuations",
    "run_pdftotext",
    "looks_garbled",
]

# Markers that show the text layer is a real Xactimate estimate page rather
# than noise.  Used as a secondary signal: a document with plenty of printable
# characters but no Xactimate structure at all is "garbled / not an estimate".
_XACTIMATE_MARKERS = re.compile(
    r"QUANTITY|Line Item Totals|Replacement Cost Value|Summary for|Coverage Item Total|"
    r"Actual Cash Value|Depreciation|Xactimate|Deductible|Overhead|Profit",
    re.I,
)

_PRINTABLE_RE = re.compile(r"[ -~]")
_LETTER_RE = re.compile(r"[A-Za-z]")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")

# A *structural* signal, much stronger than a letter ratio: a real
# Xactimate/Symbility line-item table plus its printed totals.  Drawings and
# diagrams (which push the letter ratio down on our own contractor estimates)
# cannot fake these, while a scanned page with an obfuscated text layer cannot
# produce them either.  When both are present the letter-ratio heuristic is
# skipped - the page carries real, parseable line items.
_STRUCTURED_HEADER_RE = re.compile(r"QUANTITY\s+UNIT\s+PRICE|QUANTITY\s+.*\bRCV\b", re.I)
_STRUCTURED_TOTALS_RE = re.compile(r"Line Item Totals?:|Replacement Cost Value", re.I)


def has_line_item_structure(text: str) -> bool:
    """True when the text carries a real line-item table header *and* totals.

    Used to overrule the "mostly non-letter garbage" heuristic: a contractor
    estimate with large drawing pages is legitimate even though most of its
    characters are diagram labels.
    """
    if not text:
        return False
    return bool(_STRUCTURED_HEADER_RE.search(text) and _STRUCTURED_TOTALS_RE.search(text))


@dataclass
class PdfTextResult:
    """Outcome of a pdftotext run.

    status is one of:
      ``ok``         - usable text layer
      ``needs_ocr``  - missing / too sparse / garbled text layer
      ``error``      - pdftotext missing or the file could not be read
    """

    path: str
    status: str
    text: str = ""
    reason: str = ""
    pages: int = 0
    page_stats: List[dict] = field(default_factory=list)
    returncode: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def needs_ocr(self) -> bool:
        return self.status == "needs_ocr"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
            "pages": self.pages,
            "page_stats": self.page_stats,
        }


def run_pdftotext(path: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run ``pdftotext -layout <path> -`` and return the CompletedProcess."""
    return subprocess.run(
        ["pdftotext", "-layout", path, "-"],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


# A numbered line item start inside the bbox layout ("12. Laminated - ...").
_BBOX_ITEM_RE = re.compile(r"\*?\s*((?:\d{1,2},\d{3}|\d(?: ?\d){0,3})[a-z]?) ?\. ")
_BBOX_LINE_RE = re.compile(
    r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">(.*?)</line>',
    re.S,
)
_BBOX_WORD_RE = re.compile(r">([^<]*)</word>")

# Derived geometry cache: (absolute path, mtime_ns, size) -> continuations.
# Parsing the same document twice in one process (a retry, or both slots of a
# pair pointing at the same file) should not rerun pdftotext.  The mapping is
# read-only for callers; a changed file has a different mtime/size key.
_BBOX_CACHE: "OrderedDict[Tuple[str, int, int], Dict[Tuple[int, str], List[str]]]" = (
    OrderedDict()
)
_BBOX_CACHE_MAX = 8
_BBOX_LOCK = threading.Lock()


def extract_bbox_continuations(path: str, timeout: int = 120) -> Dict[Tuple[int, str], List[str]]:
    """Wrapped-description continuations from ``pdftotext -bbox-layout``.

    Returns ``{(page_index_0_based, line_id): [continuation texts]}``.  A
    continuation is a line that sits directly under a numbered item (vertical
    gap smaller than 1.4x the line height) at the same x position, i.e. a
    description that wrapped onto the next line.

    PDF input only.  Any failure (missing pdftotext, unreadable file, timeout)
    returns an empty map - parsing never depends on this information.
    """
    path = str(path)
    if not path.lower().endswith(".pdf"):
        return {}
    if not os.path.exists(path) or shutil.which("pdftotext") is None:
        return {}
    try:
        st = os.stat(path)
        cache_key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _BBOX_LOCK:
        cached = _BBOX_CACHE.get(cache_key)
        if cached is not None:
            _BBOX_CACHE.move_to_end(cache_key)
            return cached
    try:
        proc = subprocess.run(
            ["pdftotext", "-bbox-layout", path, "-"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    out: Dict[Tuple[int, str], List[str]] = {}
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    for pi, page in enumerate(text.split("<page ")[1:]):
        rows: List[Tuple[float, float, float, str]] = []
        for x0, y0, y1, body in _BBOX_LINE_RE.findall(page):
            t = " ".join(html.unescape(w) for w in _BBOX_WORD_RE.findall(body))
            rows.append((float(y0), float(x0), float(y1) - float(y0), t))
        rows.sort()
        for i, (y, x, h, t) in enumerate(rows):
            m = _BBOX_ITEM_RE.match(t)
            if not m:
                continue
            parts: List[str] = []
            prev = y
            for y2, x2, _h2, t2 in rows[i + 1:]:
                if y2 - prev < 1 or abs(x2 - x) > 25:
                    continue  # same visual row (number columns) or another column
                if (
                    y2 - prev < 1.4 * h
                    and not _BBOX_ITEM_RE.match(t2)
                    and not re.match(r"(Totals?|Date):", t2)
                ):
                    parts.append(t2)
                    prev = y2
                else:
                    break
            out[(pi, m.group(1).replace(" ", "").replace(",", ""))] = parts
    with _BBOX_LOCK:
        _BBOX_CACHE[cache_key] = out
        _BBOX_CACHE.move_to_end(cache_key)
        while len(_BBOX_CACHE) > _BBOX_CACHE_MAX:
            _BBOX_CACHE.popitem(last=False)
    return out


def looks_garbled(text: str) -> Optional[str]:
    """Return a reason string when the text looks unusable, else ``None``.

    A page (or the whole document) is considered unusable when it has very few
    printable characters, when the printable characters are mostly noise rather
    than words, or when it carries no recognisable Xactimate estimate
    structure.
    """
    if text is None:
        return "no text extracted from PDF"
    stripped = text.strip()
    if not stripped:
        return "PDF has no text layer (scanned image) - a text-based estimate could not be read"

    printable = len(_PRINTABLE_RE.findall(text))
    if printable < 200:
        return (
            f"PDF text layer is too sparse to parse ({printable} printable characters) "
            f"- the document is most likely a scan"
        )

    letters = len(_LETTER_RE.findall(text))
    words = _WORD_RE.findall(text)
    structured = has_line_item_structure(text)
    if letters < printable * 0.20 and not structured:
        return (
            "PDF text layer is mostly non-letter garbage "
            f"({printable} printable chars, {letters} letters) - the document is most likely a scan"
        )
    if len(words) < 20 and not structured:
        return (
            f"PDF text layer contains almost no readable words ({len(words)} words) "
            f"- the document is most likely a scan"
        )
    if not _XACTIMATE_MARKERS.search(text):
        return (
            "PDF text layer does not look like a Xactimate estimate "
            "(no QUANTITY / Line Item Totals / RCV structure found)"
        )
    return None


def extract_text(path: str, timeout: int = 120) -> PdfTextResult:
    """Extract ``pdftotext -layout`` text from ``path``.

    Never raises for scanned/garbled input - the caller gets a result object
    with ``status="needs_ocr"`` instead.
    """
    path = str(path)
    if not os.path.exists(path):
        return PdfTextResult(path=path, status="error", reason=f"file not found: {path}")

    if shutil.which("pdftotext") is None:
        return PdfTextResult(
            path=path,
            status="error",
            reason="pdftotext not found (install poppler-utils to read PDF estimates)",
        )

    try:
        proc = run_pdftotext(path, timeout=timeout)
    except subprocess.TimeoutExpired:
        return PdfTextResult(path=path, status="error", reason=f"pdftotext timed out after {timeout}s")
    except OSError as exc:
        return PdfTextResult(path=path, status="error", reason=f"pdftotext could not be run: {exc}")

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return PdfTextResult(
            path=path,
            status="error",
            reason=f"pdftotext failed on {path}: {err[:300] or 'unknown error'}",
            returncode=proc.returncode,
        )

    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    pages = text.split("\f")
    page_stats = []
    too_few = 0
    for i, page in enumerate(pages, 1):
        if not page.strip():
            continue
        printable = len(_PRINTABLE_RE.findall(page))
        letters = len(_LETTER_RE.findall(page))
        words = len(_WORD_RE.findall(page))
        page_stats.append({"page": i, "printable": printable, "letters": letters, "words": words})
        if printable < 40 or words < 3:
            too_few += 1

    result = PdfTextResult(path=path, status="ok", text=text, pages=len(pages), page_stats=page_stats)

    reason = looks_garbled(text)
    if reason:
        result.status = "needs_ocr"
        result.reason = reason
        return result

    # Mostly-empty pages (a scanned cover + a tiny text appendix) still need OCR.
    if page_stats and too_few >= max(1, (len(page_stats) // 2)):
        result.status = "needs_ocr"
        result.reason = (
            f"{too_few} of {len(page_stats)} text pages are effectively empty - "
            f"the document is most likely a scan"
        )
    return result
