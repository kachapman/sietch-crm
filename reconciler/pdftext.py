"""Extract text from PDFs with ``pdftotext -layout`` and detect scans.

The reconciler never OCRs.  A PDF without a usable text layer comes back as a
:class:`PdfTextResult` with ``status == "needs_ocr"`` and a human-readable
``reason``.  Callers can then hand the file to the (separate, later-phase)
OCR worker.  This module never raises for a scanned or garbled document.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

__all__ = ["PdfTextResult", "extract_text", "run_pdftotext", "looks_garbled"]

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
    if letters < printable * 0.20:
        return (
            "PDF text layer is mostly non-letter garbage "
            f"({printable} printable chars, {letters} letters) - the document is most likely a scan"
        )
    if len(words) < 20:
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
