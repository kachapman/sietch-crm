"""Shared, read-only text extraction for documents and mail attachments.

ONE implementation is used by both the project-document path and the
mail-attachment path. It is deliberately stdlib-only and has no database
dependency: the caller injects cache/queue lookups, which keeps the module
unit-testable without PostgreSQL and keeps every query in `ocr_store.py`.

What it does
------------
* PDF   — ``pdftotext -layout -f N -l N`` per page (poppler). A page with
          fewer than :data:`OCR_NEEDED_MIN_PRINTABLE` printable characters is
          marked ``ocr_needed``. Cached OCR text wins for such pages.
* txt   — decoded as UTF-8 (invalid bytes replaced).
* html  — tag-stripped with the stdlib parser (no browser, no network).
* eml   — text parts extracted; HTML parts tag-stripped.
* other — structured ``unsupported_type`` result (mime + filename, no body).

This module NEVER runs OCR and NEVER logs extracted text. The only hashes it
returns are SHA-256 of the file bytes (cache/queue key).

Subprocess safety: every ``pdftotext``/``pdfinfo``/``pdftoppm`` call passes the
file as one list argument. User-controlled filenames are never interpolated
into a shell string (and ``shell=True`` is never used).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import subprocess
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("sietch.document_text")

# A page needs OCR when pdftotext returns fewer than this many printable
# characters after stripping whitespace. 20 is deliberately low: a carrier
# cover page with only a claim number is still useful as a text layer, while a
# scanned page usually has 0-5 stray characters.
OCR_NEEDED_MIN_PRINTABLE = 20

# Default response budget for extracted text (characters). The MCP transport is
# JSON-RPC, so an unbounded document would blow the response size.
DEFAULT_MAX_CHARS = 100_000
HARD_MAX_CHARS = 1_000_000
MAX_PDF_PAGES = 5_000

# pdftotext should never hang a request; these are local files.
PDFTOTEXT_TIMEOUT = 120
PDFINFO_TIMEOUT = 30

_PRINTABLE_RE = re.compile(r"[ -~]")
_KIND_BY_EXT = {
    ".pdf": "pdf",
    ".txt": "txt",
    ".text": "txt",
    ".md": "txt",
    ".markdown": "txt",
    ".log": "txt",
    ".csv": "txt",
    ".html": "html",
    ".htm": "html",
    ".eml": "eml",
}


def detect_kind(filename: str | None, mime_type: str | None) -> str:
    """Classify a file as ``pdf``/``txt``/``html``/``eml``/``unsupported``.

    Extension wins (CRM rows often carry ``application/octet-stream``); the
    MIME type is the fallback for extension-less rows.
    """
    ext = Path(str(filename or "")).suffix.lower()
    kind = _KIND_BY_EXT.get(ext)
    if kind:
        return kind
    mime = str(mime_type or "").split(";")[0].strip().lower()
    if mime == "application/pdf":
        return "pdf"
    if mime == "text/html":
        return "html"
    if mime in ("message/rfc822", "application/eml", "application/x-eml"):
        return "eml"
    if mime.startswith("text/"):
        return "txt"
    return "unsupported"


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 of the file bytes (the OCR cache/queue dedupe key)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def count_printable(text: str) -> int:
    """Printable (ASCII 0x20-0x7E) characters, ignoring whitespace-only text."""
    if not text or not text.strip():
        return 0
    return len(_PRINTABLE_RE.findall(text))


def pdf_page_count(path: str | Path) -> int:
    """Page count via ``pdfinfo`` (fallback: form feeds from pdftotext)."""
    try:
        proc = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            timeout=PDFINFO_TIMEOUT,
            check=False,
        )
        if proc.returncode == 0:
            for line in (proc.stdout or b"").decode("utf-8", errors="replace").splitlines():
                if line.lower().startswith("pages:"):
                    try:
                        return max(0, int(line.split(":", 1)[1].strip()))
                    except ValueError:
                        break
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT,
            check=False,
        )
        if proc.returncode == 0:
            text = (proc.stdout or b"").decode("utf-8", errors="replace")
            # pdftotext terminates every page with a form feed.
            return max(1, text.count("\f"))
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


def pdf_page_text(path: str | Path, page: int) -> str:
    """Extract one page with ``pdftotext -layout``. Empty string on failure."""
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-f", str(int(page)), "-l", str(int(page)),
             str(path), "-"],
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("pdftotext failed for page %s: %s", page, type(exc).__name__)
        return ""
    if proc.returncode != 0:
        logger.warning("pdftotext exited %s for page %s", proc.returncode, page)
        return ""
    # pdftotext terminates every page with a form feed; a single-page range
    # therefore ends with one that would otherwise inflate char_count.
    return (proc.stdout or b"").decode("utf-8", errors="replace").rstrip("\f")


class _TextCollector(HTMLParser):
    """Collect visible text, skipping script/style/head content."""

    _SKIP = {"script", "style", "head", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102
        if tag.lower() in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        if tag.lower() in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:  # noqa: D102
        if not self._skip_depth and data:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "\n".join(self.parts)).strip()


def strip_html(text: str) -> str:
    """Visible text of an HTML fragment (stdlib parser; no browser)."""
    parser = _TextCollector()
    try:
        parser.feed(text or "")
        parser.close()
    except Exception:  # malformed HTML must not break extraction
        return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()
    out = parser.text()
    return out or html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def extract_eml_text(data: bytes) -> str:
    """Extract text parts of an RFC-822 message; HTML parts are stripped."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except Exception:
        return _decode_text(data)
    body = None
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        body = None
    if body is not None:
        try:
            payload = body.get_content()
        except Exception:
            raw = body.get_payload(decode=True)
            payload = _decode_text(raw) if isinstance(raw, bytes) else str(raw or "")
        if body.get_content_type() == "text/html":
            return strip_html(payload)
        return payload if isinstance(payload, str) else str(payload)
    # Fallback: concatenate every text/* part.
    parts: list[str] = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        raw = part.get_payload(decode=True)
        text = _decode_text(raw) if isinstance(raw, bytes) else str(raw or "")
        parts.append(strip_html(text) if ctype == "text/html" else text)
    return "\n".join(p for p in parts if p)


def _clamp_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _single_page_text(path: str | Path, kind: str) -> str:
    data = Path(path).read_bytes()
    if kind == "txt":
        return _decode_text(data)
    if kind == "html":
        return strip_html(_decode_text(data))
    if kind == "eml":
        return extract_eml_text(data)
    return ""


def _unsupported(filename: str | None, mime_type: str | None,
                 size_bytes: int, file_sha256: str | None) -> dict:
    return {
        "status": "unsupported_type",
        "error": "unsupported_type",
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "file_sha256": file_sha256,
        "page_count": 0,
        "page_start": None,
        "page_end": None,
        "truncated": False,
        "text": "",
        "pages": [],
    }


def extract_document(
    path: str | Path,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    page_start: Any = None,
    page_end: Any = None,
    max_chars: Any = None,
    cache_lookup: Callable[[str, int], Any] | None = None,
    queue_status_lookup: Callable[[str], str | None] | None = None,
    file_sha256: str | None = None,
) -> dict:
    """Extract text from ``path``.

    ``cache_lookup(sha256, page_number)`` returns the cached OCR row (a dict
    with ``text`` or a plain string) or ``None``.

    ``queue_status_lookup(sha256)`` returns ``pending``/``in_progress``/
    ``done``/``failed`` for the file's active queue row, or ``None``.

    Returns a JSON-ready dict; never raises for unsupported types. Raises
    ``FileNotFoundError``/``OSError`` when the file itself cannot be read
    (callers translate that to ``file_missing``).
    """
    path = Path(str(path))
    filename = filename or path.name
    size_bytes = path.stat().st_size
    if file_sha256 is None:
        file_sha256 = sha256_file(path)

    kind = detect_kind(filename, mime_type)
    if kind == "unsupported":
        return _unsupported(filename, mime_type, size_bytes, file_sha256)

    try:
        budget = int(max_chars) if max_chars not in (None, "") else DEFAULT_MAX_CHARS
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_CHARS
    budget = max(1, min(budget, HARD_MAX_CHARS))

    if kind != "pdf":
        raw = _single_page_text(path, kind)
        returned = raw[:budget]
        return {
            "status": "ok",
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "file_sha256": file_sha256,
            "page_count": 1,
            "page_start": 1,
            "page_end": 1,
            "truncated": len(raw) > len(returned),
            "text": returned,
            "pages": [{
                "page": 1,
                "text": returned,
                "source": {"txt": "text", "html": "html", "eml": "eml"}.get(kind, "text"),
                "char_count": len(raw),
                "ocr_needed": False,
                "ocr_status": None,
            }],
        }

    total_pages = min(pdf_page_count(path), MAX_PDF_PAGES)
    if total_pages <= 0:
        raise OSError("could not determine PDF page count")

    start = _clamp_int(page_start) or 1
    end = _clamp_int(page_end) or total_pages
    if start < 1 or end < 1 or start > end:
        raise ValueError("invalid_page_range")
    if start > total_pages:
        raise ValueError("page_start beyond page_count")
    end = min(end, total_pages)

    queue_state = queue_status_lookup(file_sha256) if queue_status_lookup else None
    pages: list[dict] = []
    overflow = False
    remaining = budget

    for page_no in range(start, end + 1):
        cached = None
        if cache_lookup is not None:
            try:
                cached = cache_lookup(file_sha256, page_no)
            except Exception as exc:  # a cache outage must not break reading
                logger.warning("ocr cache lookup failed: %s", type(exc).__name__)
                cached = None
        cache_text = None
        if isinstance(cached, dict):
            cache_text = cached.get("text")
        elif isinstance(cached, str):
            cache_text = cached

        if cache_text is not None:
            full_text = str(cache_text)
            source = "ocr_cache"
            ocr_needed = True
            ocr_status = "cached"
        else:
            full_text = pdf_page_text(path, page_no)
            source = "pdftotext"
            ocr_needed = count_printable(full_text) < OCR_NEEDED_MIN_PRINTABLE
            ocr_status = None
            if ocr_needed:
                if queue_state in ("pending", "in_progress"):
                    ocr_status = "queued" if queue_state == "pending" else "in_progress"
                else:
                    # No active job (or a failed/done job that produced no row
                    # for this page): it still needs OCR and can be requested.
                    ocr_status = "pending"

        if remaining <= 0:
            overflow = True
            page_text = ""
        elif len(full_text) > remaining:
            page_text = full_text[:remaining]
            remaining = 0
            overflow = True
        else:
            page_text = full_text
            remaining -= len(full_text)

        pages.append({
            "page": page_no,
            "text": page_text,
            "source": source,
            "char_count": len(full_text),
            "ocr_needed": ocr_needed,
            "ocr_status": ocr_status,
        })

    needs_ocr = any(p["ocr_status"] in ("pending", "queued", "in_progress") for p in pages)
    combined = "\n\n".join(p["text"] for p in pages if p["text"])
    return {
        "status": "ocr_pending" if needs_ocr else "ok",
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "file_sha256": file_sha256,
        "page_count": total_pages,
        "page_start": start,
        "page_end": end,
        "truncated": overflow,
        "text": combined,
        "pages": pages,
    }


def needed_pages(extraction: dict) -> list[int]:
    """Pages that require OCR text (cached or not), in page order."""
    return [p["page"] for p in extraction.get("pages", []) if p.get("ocr_needed")]


__all__ = [
    "DEFAULT_MAX_CHARS",
    "HARD_MAX_CHARS",
    "OCR_NEEDED_MIN_PRINTABLE",
    "count_printable",
    "detect_kind",
    "extract_document",
    "extract_eml_text",
    "needed_pages",
    "pdf_page_count",
    "pdf_page_text",
    "sha256_file",
    "strip_html",
]
