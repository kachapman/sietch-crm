"""Persistence for the OCR page cache and worker queue (migration 052).

This module is the ONLY place that reads/writes ``ocr_page_cache`` and
``ocr_queue``. It is deliberately small and boring so the access rules live in
one place:

* Cache rows are written **only** by the worker endpoints
  (``/api/v2/ocr/queue/{id}/pages``).
* The only CRM-side write anywhere in the read/OCR feature is a queue row
  (``request_ocr``).
* Audit/log helpers accept a fixed whitelist of keys (ids, sha256, page
  numbers, status, engine, engine_version). Text, filenames and tokens are
  dropped even if a caller passes them.

Nothing here imports paddleocr/paddle — OCR runs on a host-agnostic worker,
never on the server.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

import db

logger = logging.getLogger("sietch.ocr_store")

# Engine identity recorded on cache rows. Kept in sync with the worker's
# engine.py (paddleocr 3.7.0, PP-StructureV3 pipeline).
ENGINE_NAME = "pp-structurev3"
ENGINE_VERSION = "paddleocr-3.7.0"

QUEUE_STATUSES = ("pending", "in_progress", "done", "failed")
SOURCES = ("project_document", "mail_attachment")

# A single page of OCR text should never be huge; this also stops a buggy or
# malicious worker from filling the database in one request.
MAX_PAGE_TEXT_CHARS = 2_000_000
MAX_PAGES_PER_CALL = 5_000

# Audit whitelist — everything else is dropped before logging.
_AUDIT_KEYS = frozenset({
    "source", "source_id", "project_id", "message_id", "document_id",
    "attachment_id", "job_id", "file_sha256", "page", "pages", "page_count",
    "pages_cached", "pages_needed", "pages_missing", "status", "engine",
    "engine_version", "requested_by", "token_id", "count", "error",
})


def audit(event: str, **fields: Any) -> None:
    """Log one OCR audit line with ids/hashes/status only.

    Deliberately whitelists keys: a caller that passes ``text=`` (or a token,
    or a filename) cannot leak it into the log. Values are truncated and never
    include document content because text-like keys are not in the whitelist.
    """
    safe = []
    for key in sorted(fields):
        if key not in _AUDIT_KEYS:
            continue
        value = fields[key]
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value[:50])
        text = str(value)
        if len(text) > 200:
            text = text[:197] + "..."
        safe.append(f"{key}={text}")
    logger.info("ocr %s %s", event, " ".join(safe))


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def serialize_job(row: dict | None) -> dict | None:
    """JSON-ready queue row (no text ever lives in these columns)."""
    if not row:
        return None
    return {
        "id": row.get("id"),
        "source": row.get("source"),
        "source_id": row.get("source_id"),
        "project_id": row.get("project_id"),
        "message_id": row.get("message_id"),
        "file_sha256": row.get("file_sha256"),
        "status": row.get("status"),
        "error": row.get("error"),
        "requested_at": _iso(row.get("requested_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

def get_cached_page(file_sha256: str, page_number: int) -> dict | None:
    return db.query_one(
        """SELECT file_sha256, page_number, text, engine, engine_version,
                  source, source_id, created_at
             FROM ocr_page_cache
            WHERE file_sha256 = %s AND page_number = %s""",
        (file_sha256, int(page_number)),
    )


def cache_lookup(file_sha256: str, page_number: int) -> dict | None:
    """Callable injected into ``document_text.extract_document``."""
    return get_cached_page(file_sha256, page_number)


def cached_pages(file_sha256: str) -> list[int]:
    rows = db.query(
        "SELECT page_number FROM ocr_page_cache WHERE file_sha256 = %s ORDER BY page_number",
        (file_sha256,),
    )
    return [int(r[0]) for r in rows]


def cache_meta(file_sha256: str) -> dict:
    row = db.query_one(
        """SELECT COUNT(*) AS pages, MIN(created_at) AS created_at,
                  MAX(engine) AS engine, MAX(engine_version) AS engine_version
             FROM ocr_page_cache WHERE file_sha256 = %s""",
        (file_sha256,),
    ) or {}
    return {
        "pages_cached": int(row.get("pages") or 0),
        "engine": row.get("engine"),
        "engine_version": row.get("engine_version"),
        "cached_at": _iso(row.get("created_at")),
    }


def write_page(file_sha256: str, page_number: int, text: str, *,
               engine: str, engine_version: str,
               source: str, source_id: int) -> None:
    db.execute(
        """INSERT INTO ocr_page_cache
             (file_sha256, page_number, text, engine, engine_version, source, source_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (file_sha256, page_number) DO UPDATE
              SET text = EXCLUDED.text,
                  engine = EXCLUDED.engine,
                  engine_version = EXCLUDED.engine_version,
                  source = EXCLUDED.source,
                  source_id = EXCLUDED.source_id""",
        (file_sha256, int(page_number), text, engine, engine_version,
         source, int(source_id)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────────────

def get_job(job_id: int) -> dict | None:
    return db.query_one("SELECT * FROM ocr_queue WHERE id = %s", (int(job_id),))


def active_job_for_hash(file_sha256: str) -> dict | None:
    return db.query_one(
        """SELECT * FROM ocr_queue
            WHERE file_sha256 = %s AND status IN ('pending', 'in_progress')
            ORDER BY id DESC LIMIT 1""",
        (file_sha256,),
    )


def latest_job_for_hash(file_sha256: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM ocr_queue WHERE file_sha256 = %s ORDER BY id DESC LIMIT 1",
        (file_sha256,),
    )


def queue_status_lookup(file_sha256: str) -> str | None:
    """Callable injected into ``document_text.extract_document``."""
    row = active_job_for_hash(file_sha256)
    if row:
        return row.get("status")
    # A failed job is reported as failed (the page still shows pending, since
    # it can be requested again), a done job with missing cache pages likewise
    # has nothing active. Only active states matter for per-page "queued".
    return None


def enqueue(*, source: str, source_id: int, file_sha256: str,
            project_id: int | None = None, message_id: int | None = None,
            requested_by: int | None = None) -> tuple[dict, bool]:
    """Insert (or return the existing active) queue row for ``file_sha256``.

    Returns ``(row, created)``. The partial unique index guarantees no
    duplicate active row for the same file hash even under concurrent calls.
    """
    if source not in SOURCES:
        raise ValueError("unknown OCR source")
    existing = active_job_for_hash(file_sha256)
    if existing:
        return existing, False
    try:
        job_id = db.insert_returning(
            """INSERT INTO ocr_queue
                 (source, source_id, project_id, message_id, file_sha256,
                  status, requested_by)
               VALUES (%s, %s, %s, %s, %s, 'pending', %s)
               RETURNING id""",
            (source, int(source_id), project_id, message_id, file_sha256,
             requested_by),
        )
    except Exception:
        # Concurrent insert lost the partial-unique race: return the winner.
        existing = active_job_for_hash(file_sha256)
        if existing:
            return existing, False
        raise
    row = get_job(int(job_id))
    if row is None:  # pragma: no cover - insert just succeeded
        raise RuntimeError("OCR queue insert returned no row")
    return row, True


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    if status:
        if status not in QUEUE_STATUSES:
            raise ValueError("unknown OCR queue status")
        rows = db.query_dicts(
            """SELECT * FROM ocr_queue WHERE status = %s
                ORDER BY requested_at ASC, id ASC LIMIT %s""",
            (status, limit),
        )
    else:
        rows = db.query_dicts(
            "SELECT * FROM ocr_queue ORDER BY id DESC LIMIT %s", (limit,),
        )
    return [serialize_job(r) for r in rows]


def claim_job(job_id: int, *, stale_after_minutes: int = 120) -> dict | None:
    """Move a pending (or stale in-progress / failed) job to in_progress.

    A crashed worker leaves a row in_progress; the staleness window lets a
    restarted worker reclaim it without a second table.
    """
    row = db.query_one(
        """UPDATE ocr_queue
              SET status = 'in_progress', started_at = NOW(),
                  finished_at = NULL, error = NULL
            WHERE id = %s
              AND (status IN ('pending', 'failed')
                   OR (status = 'in_progress'
                       AND (started_at IS NULL
                            OR started_at < NOW() - make_interval(mins => %s))))
            RETURNING *""",
        (int(job_id), int(stale_after_minutes)),
    )
    return row


def write_pages(job: dict, pages: Iterable[dict], *,
                engine: str, engine_version: str) -> int:
    """Write cache rows for one job (worker endpoint only).

    Returns the number of pages written. Caller validates each page first.
    """
    written = 0
    for page in pages:
        write_page(
            job["file_sha256"],
            int(page["page"]),
            str(page["text"]),
            engine=engine,
            engine_version=engine_version,
            source=job["source"],
            source_id=int(job["source_id"]),
        )
        written += 1
    return written


def complete_job(job_id: int, status: str, error: str | None = None) -> dict | None:
    if status not in ("done", "failed"):
        raise ValueError("complete status must be done or failed")
    short_error = None
    if error:
        # Machine code / short message only. Text never reaches this column.
        short_error = str(error).strip().replace("\n", " ")[:500]
    row = db.query_one(
        """UPDATE ocr_queue
              SET status = %s, error = %s, finished_at = NOW()
            WHERE id = %s
            RETURNING *""",
        (status, short_error, int(job_id)),
    )
    return row


__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "MAX_PAGES_PER_CALL",
    "MAX_PAGE_TEXT_CHARS",
    "active_job_for_hash",
    "audit",
    "cache_lookup",
    "cache_meta",
    "cached_pages",
    "claim_job",
    "complete_job",
    "enqueue",
    "get_cached_page",
    "get_job",
    "latest_job_for_hash",
    "list_jobs",
    "queue_status_lookup",
    "serialize_job",
    "write_page",
    "write_pages",
]
