"""Detect what a stored document actually is, independent of its display title.

OnlyOffice's editor config used to take ``fileType`` from the display title
and default an extensionless name to ``docx``. A PDF whose editable title had
no suffix was therefore announced as a Word file, and the Document Server
rejected it as a PDF/docx mismatch. Renaming the title so it ended in ``.pdf``
changed that label only; the bytes were never consulted.

Detection here trusts unambiguous file signatures. The display title and the
stored MIME type are hints for ambiguous text (``.txt`` vs ``.csv``) and are
ignored when they disagree with the bytes. Unknown or unsafe contents are
reported as not openable so the editor fails with a clear error instead of
guessing ``docx``.

The OnlyOffice session key includes the detected type. The Document Server
caches a conversion under ``document.key``. A key that was only the document
id kept a session opened under the wrong type after the type was corrected.
The id-only key is still accepted on save callbacks so an editor session that
started before this change can finish saving. This module does not talk to a
Document Server and cannot prove a generic "could not open" error is gone.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_TEXT_SAMPLE = 64 * 1024
_OLE_SCAN = 2 * 1024 * 1024
_ZIP_ENTRY_CAP = 5000
_CONTENT_TYPES_CAP = 1_000_000

# Extensions we will append or strip. Dangerous ones are stripped so a file
# we refuse to open is not saved as ``.html`` / ``.exe`` either.
_OFFICE_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "docm", "xls", "xlsx", "xlsm",
    "ppt", "pptx", "pptm", "rtf", "odt", "ods", "odp", "txt", "csv",
    "png", "jpg", "jpeg", "gif", "webp",
})
_DANGEROUS_EXTENSIONS = frozenset({
    "html", "htm", "xhtml", "shtml", "svg", "js", "mjs",
    "exe", "dll", "bat", "cmd", "ps1", "sh",
})
_STRIP_EXTENSIONS = _OFFICE_EXTENSIONS | _DANGEROUS_EXTENSIONS

_ONLYOFFICE_TYPE = {
    "pdf": "pdf",
    "doc": "word", "docx": "word", "docm": "word", "rtf": "word",
    "txt": "word", "odt": "word",
    "xls": "cell", "xlsx": "cell", "xlsm": "cell", "csv": "cell", "ods": "cell",
    "ppt": "slide", "pptx": "slide", "pptm": "slide", "odp": "slide",
}

_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "rtf": "application/rtf",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "txt": "text/plain",
    "csv": "text/csv",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

_ODF_MIME = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
}

# Suffix on sietch-doc-{id}-{suffix}. Alphanumeric only, so the id boundary
# stays a hyphen and a crafted suffix cannot smuggle a path or a second id.
_KEY_SUFFIX_RE = re.compile(r"^[a-z0-9]{1,8}$")

_EMPTY_ERROR = "This file can't be opened in the editor because it is empty."
_IMAGE_ERROR = (
    "This file is an image. The full editor opens Word, spreadsheet, "
    "presentation, and PDF documents; use the preview pane to view it."
)
_ENCRYPTED_ERROR = (
    "This file looks encrypted, so its document type can't be confirmed. "
    "It was not opened in the editor."
)
_UNKNOWN_ERROR = (
    "This file can't be opened in the editor. Its contents are not a "
    "supported Word, spreadsheet, presentation, or PDF document."
)


def _result(*, ext, document_type, mime, onlyoffice, kind) -> dict:
    return {
        "ext": ext,
        "document_type": document_type,
        "mime": mime,
        "onlyoffice": onlyoffice,
        "kind": kind,
    }


def _unknown() -> dict:
    return _result(ext=None, document_type=None, mime="application/octet-stream",
                   onlyoffice=False, kind="unknown")


def _empty() -> dict:
    return _result(ext=None, document_type=None, mime="application/octet-stream",
                   onlyoffice=False, kind="empty")


def _encrypted() -> dict:
    return _result(ext=None, document_type=None, mime="application/octet-stream",
                   onlyoffice=False, kind="encrypted")


def _office(ext: str) -> dict:
    return _result(
        ext=ext,
        document_type=_ONLYOFFICE_TYPE[ext],
        mime=_MIME_BY_EXT[ext],
        onlyoffice=True,
        kind="text" if ext in ("txt", "csv") else ("pdf" if ext == "pdf" else "office"),
    )


def _image(ext: str) -> dict:
    return _result(ext=ext, document_type=None, mime=_MIME_BY_EXT[ext],
                   onlyoffice=False, kind="image")


def editor_format_error(fmt: dict) -> str:
    """User-facing reason the full editor will not open this file.

    The display title is intentionally not echoed: titles are caller-controlled
    and may contain claim identifiers.
    """
    kind = (fmt or {}).get("kind")
    if kind == "empty":
        return _EMPTY_ERROR
    if kind == "image":
        return _IMAGE_ERROR
    if kind == "encrypted":
        return _ENCRYPTED_ERROR
    return _UNKNOWN_ERROR


def _label_ext(title: str | None) -> str:
    name = str(title or "").replace("\\", "/").split("/")[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _mime_base(mime: str | None) -> str:
    return (mime or "").split(";")[0].strip().lower()


def _is_pdf(data: bytes) -> bool:
    # The PDF header may sit after a short preamble. It is not looked for
    # inside zip/OLE containers; those are classified first.
    return b"%PDF-" in data[:1024]


def _image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def _ole_ext_from_bytes(blob: bytes) -> str | None:
    window = blob[:_OLE_SCAN]
    if "WordDocument".encode("utf-16le") in window:
        return "doc"
    if "Workbook".encode("utf-16le") in window:
        return "xls"
    if "PowerPoint Document".encode("utf-16le") in window:
        return "ppt"
    return None


def _zip_office_ext(source) -> str | None:
    """Return an office ext, ``encrypted``, or None for a non-office zip.

    ``None`` means "do not guess". A zip that is not OOXML/ODF must not fall
    through to the display title and become a docx.
    """
    try:
        with zipfile.ZipFile(source) as zf:
            names = zf.namelist()
            if len(names) > _ZIP_ENTRY_CAP:
                return None
            name_set = set(names)
            if "EncryptionInfo" in name_set or "EncryptedPackage" in name_set:
                return "encrypted"
            if "mimetype" in name_set:
                info = zf.getinfo("mimetype")
                if info.file_size <= 256:
                    mt = zf.read("mimetype").decode("latin-1", "replace").strip()
                    ext = _ODF_MIME.get(mt)
                    if ext:
                        return ext
            if "[Content_Types].xml" in name_set:
                info = zf.getinfo("[Content_Types].xml")
                if info.file_size > _CONTENT_TYPES_CAP:
                    return None
                xml = zf.read("[Content_Types].xml").decode("utf-8", "replace").lower()
                if "spreadsheetml" in xml or "ms-excel" in xml:
                    return "xlsm" if "macroenabled" in xml else "xlsx"
                if "presentationml" in xml or "ms-powerpoint" in xml:
                    return "pptm" if "macroenabled" in xml else "pptx"
                if "wordprocessingml" in xml or "ms-word" in xml:
                    return "docm" if "macroenabled" in xml else "docx"
            if "word/document.xml" in name_set:
                return "docx"
            if "xl/workbook.xml" in name_set:
                return "xlsx"
            if "ppt/presentation.xml" in name_set:
                return "pptx"
    except (zipfile.BadZipFile, OSError, RuntimeError, KeyError, ValueError):
        return None
    return None


def _looks_like_text(sample: bytes) -> bool:
    if not sample or b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
        return printable / len(sample) >= 0.90


def _looks_like_markup(sample: bytes, title: str | None) -> bool:
    ext = _label_ext(title)
    if ext in {"html", "htm", "xhtml", "svg", "shtml"}:
        return True
    head = sample[:800].lstrip().lower()
    return (
        head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
        or head.startswith(b"<script")
        or head.startswith(b"<?xml")
        or head.startswith(b"<svg")
    )


def _classify_text(sample: bytes, title: str | None, mime: str | None) -> dict | None:
    if not _looks_like_text(sample):
        return None
    if _looks_like_markup(sample, title):
        return _unknown()
    ext = _label_ext(title)
    mb = _mime_base(mime)
    if ext == "csv" or mb in {"text/csv", "application/csv"}:
        return _office("csv")
    if ext in {"txt", "text", "log", "md"} or mb == "text/plain":
        return _office("txt")
    # A pdf/docx/xlsx/... label whose bytes are plain text is a disagreement,
    # not permission to open those bytes as the labeled format.
    if ext in _OFFICE_EXTENSIONS or mb in {
        "application/pdf", "application/msword", "application/rtf",
    } or "officedocument" in mb or "opendocument" in mb:
        return _unknown()
    if ext in _DANGEROUS_EXTENSIONS:
        return _unknown()
    return _office("txt")


def detect_document_format(data: bytes, title: str | None = "", mime: str | None = None) -> dict:
    """Classify an in-memory file. ``title`` and ``mime`` never override a signature."""
    if not data:
        return _empty()
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06") or data.startswith(b"PK\x07\x08"):
        kind = _zip_office_ext(io.BytesIO(data))
        if kind == "encrypted":
            return _encrypted()
        if kind:
            return _office(kind)
        return _unknown()
    if data.startswith(_OLE_MAGIC):
        ext = _ole_ext_from_bytes(data)
        if ext:
            return _office(ext)
        return _unknown()
    if _is_pdf(data):
        return _office("pdf")
    image = _image_ext(data)
    if image:
        return _image(image)
    if data[:32].lstrip().startswith(b"{\\rtf"):
        return _office("rtf")
    if data.startswith(b"MZ") or data.startswith(b"\x7fELF"):
        return _unknown()
    text = _classify_text(data[:_TEXT_SAMPLE], title, mime)
    if text is not None:
        return text
    return _unknown()


def detect_document_format_file(path: Path, title: str | None = "", mime: str | None = None) -> dict:
    """Classify a file on disk without reading it all.

    Zip and OLE need the container structures, which sit past the first page
    of a large file. Everything else is decided from a bounded prefix.
    """
    path = Path(path)
    try:
        if not path.is_file():
            return _unknown()
        if path.stat().st_size == 0:
            return _empty()
        with path.open("rb") as fh:
            prefix = fh.read(8192)
    except OSError:
        return _unknown()
    if not prefix:
        return _empty()
    if prefix.startswith(b"PK\x03\x04") or prefix.startswith(b"PK\x05\x06") or prefix.startswith(b"PK\x07\x08"):
        kind = _zip_office_ext(path)
        if kind == "encrypted":
            return _encrypted()
        if kind:
            return _office(kind)
        return _unknown()
    if prefix.startswith(_OLE_MAGIC):
        try:
            with path.open("rb") as fh:
                ext = _ole_ext_from_bytes(fh.read(_OLE_SCAN))
        except OSError:
            return _unknown()
        if ext:
            return _office(ext)
        return _unknown()
    if len(prefix) >= 8192:
        try:
            with path.open("rb") as fh:
                prefix = fh.read(_TEXT_SAMPLE)
        except OSError:
            return _unknown()
    return detect_document_format(prefix, title, mime)


def _sanitize_stem(title: str | None) -> str:
    # Cut at the first character that can break a header or glue a suffix onto
    # the real name ("file.pdf" + quote + CRLF + "Set-Cookie" must stay file.pdf).
    name = re.split(r"[\x00-\x1f\x7f\"';:]", str(title or ""), maxsplit=1)[0]
    name = name.replace("\\", "/").split("/")[-1]
    name = name.strip()
    while "." in name:
        base, ext = name.rsplit(".", 1)
        if not base or ext.lower() not in _STRIP_EXTENSIONS:
            break
        name = base
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        name = "document"
    if len(name) > 120:
        name = name[:120].rstrip(" .") or "document"
    return name


def safe_document_filename(title: str | None, detected_ext: str | None) -> str:
    """Filename safe to put in a header and to hand to the browser.

    A detected type replaces any suffix on the display title, including a
    misleading, doubled, or upper-case one. With no detected type the known
    office/dangerous suffixes are removed so the saved name does not claim a
    format the bytes do not have.
    """
    stem = _sanitize_stem(title)
    ext = (detected_ext or "").lower()
    if ext and _KEY_SUFFIX_RE.fullmatch(ext) and ext in _MIME_BY_EXT:
        return f"{stem}.{ext}"
    return stem


def content_disposition_header(disposition: str, filename: str) -> str:
    """One header line. CR/LF and quotes cannot break out of the filename.

    ``filename`` is the already-chosen safe name (see ``safe_document_filename``).
    This only makes that name header-safe; it does not invent a type.
    """
    kind = "attachment" if disposition == "attachment" else "inline"
    name = re.split(r"[\x00-\x1f\x7f\"';:]", str(filename or ""), maxsplit=1)[0]
    name = name.replace("\\", "/").split("/")[-1]
    name = name.strip(" .") or "document"
    if name in {".", ".."}:
        name = "document"
    if len(name) > 160:
        name = name[:160].rstrip(" .") or "document"
    ascii_fallback = name.encode("ascii", "replace").decode("ascii")
    ascii_fallback = ascii_fallback.replace("?", "_").replace("\\", "")
    ascii_fallback = re.sub(r"[\r\n;]", "", ascii_fallback).strip() or "document"
    encoded = quote(name, safe="")
    return f"{kind}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def wants_attachment_download(flag: str | None) -> bool:
    return (flag or "").strip().lower() in {"1", "true", "yes"}


def document_session_key(doc_id: int, file_ext: str) -> str:
    """OnlyOffice ``document.key`` for this id and detected type.

    Same id + same type → one co-editing session. A different detected type
    gets a different key so the Document Server does not reuse a conversion
    cached for the previous type. The key is not rotated per open and does
    not include a content hash: a hash would split a live session on every
    forcesave.
    """
    ext = (file_ext or "").lower()
    if not _KEY_SUFFIX_RE.fullmatch(ext):
        raise ValueError("refusing to mint a document key without a safe type")
    return f"sietch-doc-{int(doc_id)}-{ext}"


def document_key_bound_to(key: object, doc_id: int) -> bool:
    """True when a callback key belongs to this document and no other.

    ``sietch-doc-1`` must not match document 10, and ``sietch-doc-1-pdf`` must
    not match document 1 of a different file by way of a dotted or empty suffix.
    The unsuffixed key is still accepted so a session opened before the type
    suffix shipped can save.
    """
    if not isinstance(key, str) or not key:
        return False
    base = f"sietch-doc-{int(doc_id)}"
    if key == base:
        return True
    prefix = base + "-"
    if not key.startswith(prefix):
        return False
    return _KEY_SUFFIX_RE.fullmatch(key[len(prefix):]) is not None
