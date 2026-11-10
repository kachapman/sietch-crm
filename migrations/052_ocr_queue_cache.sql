-- 052_ocr_queue_cache.sql
-- OCR page cache + worker queue for scanned documents.
--
-- WHY: carrier PDFs are often scans with no text layer. The droplet must never
-- run OCR (no CPU/GPU budget, and Ken's rule is laptop-only). It extracts with
-- pdftotext, detects pages that need OCR, serves cached OCR text when a worker
-- has already produced it, and exposes a queue that a scoped API token can
-- poll from the laptop.
--
-- KEYS: the cache is keyed by file content hash + page number, so two copies
-- of the same carrier PDF (a mail attachment and a project document, or two
-- deals) are OCR'd exactly once.
--
-- SAFETY: neither table stores tokens. `error` is a short machine code, never
-- document text. No CRM rows are written by any of this.

CREATE TABLE IF NOT EXISTS ocr_page_cache (
    file_sha256    TEXT NOT NULL,
    page_number    INTEGER NOT NULL CHECK (page_number > 0),
    text           TEXT NOT NULL,
    engine         TEXT NOT NULL,          -- e.g. 'pp-structurev3'
    engine_version TEXT NOT NULL,          -- e.g. 'paddleocr-3.7.0'
    source         TEXT NOT NULL,          -- 'project_document' | 'mail_attachment'
    source_id      INTEGER NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (file_sha256, page_number)
);

CREATE INDEX IF NOT EXISTS idx_ocr_page_cache_created
    ON ocr_page_cache (created_at DESC);

CREATE TABLE IF NOT EXISTS ocr_queue (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,           -- 'project_document' | 'mail_attachment'
    source_id     INTEGER NOT NULL,        -- project_documents.id | mail_attachments.id
    project_id    INTEGER,                 -- opportunities.id (project documents only)
    message_id    INTEGER,                 -- mail_messages.id (mail attachments only)
    file_sha256   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'in_progress', 'done', 'failed')),
    error         TEXT,                    -- short machine error, never file text
    requested_by  INTEGER,                 -- users.id of the requester (never a token)
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

-- One active job per file hash. Failed/done rows stay for history, so a failed
-- job can be re-queued and a fresh copy of an already-OCR'd file never is.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ocr_queue_active_hash
    ON ocr_queue (file_sha256) WHERE status IN ('pending', 'in_progress');

CREATE INDEX IF NOT EXISTS idx_ocr_queue_status
    ON ocr_queue (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_ocr_queue_hash
    ON ocr_queue (file_sha256);
CREATE INDEX IF NOT EXISTS idx_ocr_queue_source
    ON ocr_queue (source, source_id);
