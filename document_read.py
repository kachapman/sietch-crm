"""Read-only document/attachment text service + OCR status/queue.

This sits between the HTTP handlers and the two lower layers:

    document_text.py  — pure extraction (pdftotext / txt / html / eml)
    ocr_store.py      — ocr_page_cache / ocr_queue persistence

It resolves a ``(project_id, document_id)`` pair to a stored file the same way
``list_project_documents`` does (``project_documents`` first, then
history attachments joined to that project) so a client can pass an id it got
from the Documents tab. Cross-project reads are impossible: every query is
scoped by the project/message.

No OCR runs here. No network calls. No CRM writes except a queue row in
``request_ocr``.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import db
import document_text
import ocr_store

ROOT = Path(__file__).resolve().parent

# Hard cap for the base64 download tool. MCP is JSON-RPC over HTTP and the
# in-process shim only parses JSON/text, so there is no binary transport. A
# short-lived path on chapmanserver is useless to a remote MCP client, and a
# signed URL would mint a second download auth surface. Base64 in the JSON
# result is the option that fits the existing transport — with a cap so a
# 200 MB scan cannot blow up the MCP response. Refused calls still report
# ``size_bytes`` and send no body.
MAX_DOCUMENT_CONTENT_BYTES = 10 * 1024 * 1024

MAX_CHARS_DEFAULT = document_text.DEFAULT_MAX_CHARS


class DocumentReadError(Exception):
    """A structured, non-2xx result. ``code`` is machine-readable."""

    def __init__(self, code: str, status: int = 400, **extra):
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra

    def payload(self) -> dict:
        out = {"error": self.code}
        out.update(self.extra)
        return out


def absolute_path(raw_path: str | None) -> Path | None:
    """Resolve a stored ``file_path`` (relative to the repo root or absolute)."""
    text = str(raw_path or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = ROOT / path
    return path


def resolve_project_document(project_id: int, document_id: int) -> dict | None:
    """Find a stored file for (project, document) — never another project's.

    ``project_documents`` wins (the Documents tab id); history attachments that
    were never mirrored are the fallback, exactly like
    ``_handle_project_documents_get``. Both queries are scoped to the project.
    """
    row = None
    try:
        row = db.query_one(
            """SELECT id, title AS filename, file_path, file_size, mime_type
                 FROM project_documents
                WHERE id = %s AND opportunity_id = %s AND is_deleted = FALSE""",
            (int(document_id), int(project_id)),
        )
    except Exception:
        row = None
    if row:
        return {
            "id": int(row["id"]),
            "filename": row.get("filename") or f"document-{document_id}",
            "file_path": row.get("file_path"),
            "file_size": row.get("file_size"),
            "mime_type": row.get("mime_type"),
            "from_history_attachment": False,
            "project_id": int(project_id),
        }
    extra = db.query_one(
        """SELECT ha.id, ha.filename, ha.file_path, ha.file_size, ha.mime_type
             FROM history_attachments ha
             JOIN history_events he ON he.id = ha.event_id
            WHERE ha.id = %s AND he.opportunity_id = %s
            ORDER BY ha.id DESC LIMIT 1""",
        (int(document_id), int(project_id)),
    )
    if not extra:
        return None
    return {
        "id": int(extra["id"]),
        "filename": extra.get("filename") or f"attachment-{document_id}",
        "file_path": extra.get("file_path"),
        "file_size": extra.get("file_size"),
        "mime_type": extra.get("mime_type"),
        "from_history_attachment": True,
        "project_id": int(project_id),
    }


def resolve_mail_attachment(message_id: int, attachment_id: int) -> dict | None:
    """Attachment row scoped to its message (access is checked by the caller)."""
    row = db.query_one(
        """SELECT id, message_id, filename, mime_type, size_bytes, stored_path
             FROM mail_attachments WHERE id = %s AND message_id = %s""",
        (int(attachment_id), int(message_id)),
    )
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "message_id": int(row["message_id"]),
        "filename": row.get("filename") or f"attachment-{attachment_id}",
        "file_path": row.get("stored_path"),
        "file_size": row.get("size_bytes"),
        "mime_type": row.get("mime_type"),
    }


def _stat_size(path: Path, fallback) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        try:
            return int(fallback or 0)
        except (TypeError, ValueError):
            return 0


def _require_file(row: dict) -> Path:
    path = absolute_path(row.get("file_path"))
    if path is None or not path.is_file():
        # Mail attachments: stored bytes only, no IMAP/network fallback.
        raise DocumentReadError("not_stored", 409, filename=row.get("filename"))
    return path


def extract_row(row: dict, *, page_start=None, page_end=None,
                max_chars=None) -> dict:
    """Extract text for a resolved document/attachment row."""
    path = _require_file(row)
    try:
        result = document_text.extract_document(
            path,
            filename=row.get("filename"),
            mime_type=row.get("mime_type"),
            page_start=page_start,
            page_end=page_end,
            max_chars=max_chars,
            cache_lookup=ocr_store.cache_lookup,
            queue_status_lookup=ocr_store.queue_status_lookup,
        )
    except ValueError as exc:
        raise DocumentReadError(str(exc), 400, filename=row.get("filename")) from exc
    except OSError as exc:
        raise DocumentReadError("extraction_failed", 500,
                                filename=row.get("filename"),
                                reason=type(exc).__name__) from exc
    # Report the on-disk size (row sizes go stale after a re-sync).
    result["size_bytes"] = _stat_size(path, row.get("file_size"))
    return result


def document_content(row: dict) -> dict:
    """Base64 payload for the download tool, with a hard size cap."""
    path = _require_file(row)
    size = _stat_size(path, row.get("file_size"))
    if size > MAX_DOCUMENT_CONTENT_BYTES:
        raise DocumentReadError(
            "document_too_large", 413,
            size_bytes=size,
            max_bytes=MAX_DOCUMENT_CONTENT_BYTES,
            filename=row.get("filename"),
            mime_type=row.get("mime_type"),
        )
    data = path.read_bytes()
    mime_type = row.get("mime_type") or mimetypes.guess_type(row.get("filename") or "")[0]
    return {
        "filename": row.get("filename"),
        "mime_type": mime_type or "application/octet-stream",
        "size_bytes": len(data),
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
    }


def _coverage(row: dict, extraction: dict) -> dict:
    pages = extraction.get("pages") or []
    needed = [int(p["page"]) for p in pages if p.get("ocr_needed")]
    cached = [int(p["page"]) for p in pages if p.get("source") == "ocr_cache"]
    missing = [int(p["page"]) for p in pages
               if p.get("ocr_needed") and p.get("source") != "ocr_cache"]
    job = ocr_store.active_job_for_hash(extraction.get("file_sha256") or "")
    if job is None:
        job = ocr_store.latest_job_for_hash(extraction.get("file_sha256") or "")
    queue_status = job.get("status") if job else None

    if extraction.get("status") == "unsupported_type":
        state = "unsupported_type"
    elif not needed:
        state = "not_needed"
    elif not missing:
        state = "cached"
    elif queue_status in ("pending", "in_progress"):
        state = "queued" if queue_status == "pending" else "in_progress"
    else:
        state = "pending"

    meta = ocr_store.cache_meta(extraction.get("file_sha256") or "")
    return {
        "source": ("mail_attachment" if row.get("message_id") else "project_document"),
        "source_id": row.get("id"),
        "project_id": row.get("project_id"),
        "message_id": row.get("message_id"),
        "filename": row.get("filename"),
        "mime_type": row.get("mime_type"),
        "file_sha256": extraction.get("file_sha256"),
        "page_count": extraction.get("page_count") or 0,
        "pages_needed": needed,
        "pages_cached": cached,
        "pages_missing": missing,
        "pages_needed_count": len(needed),
        "pages_cached_count": len(cached),
        "status": state,
        "queue": ocr_store.serialize_job(job),
        "engine": meta.get("engine") or (ocr_store.ENGINE_NAME if cached else None),
        "engine_version": meta.get("engine_version")
                         or (ocr_store.ENGINE_VERSION if cached else None),
        "cached_at": meta.get("cached_at"),
    }


def ocr_status(row: dict) -> dict:
    extraction = extract_row(row, max_chars=1)
    return _coverage(row, extraction)


def request_ocr(row: dict, requested_by: int | None) -> dict:
    """Queue the file for laptop OCR (the only allowed write: one queue row)."""
    extraction = extract_row(row, max_chars=1)
    coverage = _coverage(row, extraction)
    if coverage["status"] == "unsupported_type":
        return {"queued": False, "cache_reusable": False, **coverage}
    if coverage["status"] == "not_needed":
        return {"queued": False, "cache_reusable": False, **coverage}
    if coverage["status"] == "cached":
        return {"queued": False, "cache_reusable": True, **coverage}

    source = "mail_attachment" if row.get("message_id") else "project_document"
    job, created = ocr_store.enqueue(
        source=source,
        source_id=int(row["id"]),
        file_sha256=extraction["file_sha256"],
        project_id=row.get("project_id"),
        message_id=row.get("message_id"),
        requested_by=requested_by,
    )
    ocr_store.audit(
        "request" if created else "request_existing",
        job_id=job.get("id"),
        source=source,
        source_id=row.get("id"),
        project_id=row.get("project_id"),
        message_id=row.get("message_id"),
        file_sha256=extraction.get("file_sha256"),
        status=job.get("status"),
        requested_by=requested_by,
        # Deliberately passed to prove the whitelist drops it:
        text=extraction.get("text"),
        token="<dropped>",
    )
    out = {"queued": bool(created), "cache_reusable": False, **coverage}
    out["existing"] = not created
    out["job"] = ocr_store.serialize_job(job)
    job_status = job.get("status")
    out["status"] = {"pending": "queued", "in_progress": "in_progress"}.get(
        job_status, job_status or coverage["status"])
    return out


__all__ = [
    "DocumentReadError",
    "MAX_CHARS_DEFAULT",
    "MAX_DOCUMENT_CONTENT_BYTES",
    "absolute_path",
    "document_content",
    "extract_row",
    "ocr_status",
    "request_ocr",
    "resolve_mail_attachment",
    "resolve_project_document",
]
