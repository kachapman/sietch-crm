-- Migration 046: reconciler Phase 1 runs + audit
--
-- Stores a *draft* reconciliation of a carrier estimate revision against its
-- prior revision (and optionally our own estimate): the tied-out parse, the
-- draft note, and the approve/deny trail. Nothing here posts anything by
-- itself and nothing ever writes the "Final RCV" custom fields.
--
-- One migration for Phase 1 (the plan split this into 046/047/048); the
-- parsed artifacts for the MVP are kept as JSON on the run row, which the
-- plan explicitly allows ("Storing the parsed JSON per run is acceptable for
-- the MVP"). If a later phase needs queryable line items, split them out then.
--
-- Notes:
--   * status: drafted | needs_review | needs_ocr | approved | rejected
--   * gate_allowed is the parse/delta tie-out verdict. Approve is refused
--     unless it is TRUE, so a note can never post a number from a broken
--     parse (K1).
--   * inputs records the picked docs ({slot: {docRef, title, sha256}}), not
--     the file bytes.
--   * approved_at/approved_by make approval idempotent per run.

CREATE TABLE IF NOT EXISTS reconciler_runs (
    id              SERIAL PRIMARY KEY,
    opportunity_id  INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'drafted',
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    inputs          JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_version  TEXT,
    gate_allowed    BOOLEAN NOT NULL DEFAULT FALSE,
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 0,
    draft_note      TEXT,
    billing_summary JSONB,
    result          JSONB,
    approved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TIMESTAMP,
    rejected_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    rejected_at     TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reconciler_runs_opp
    ON reconciler_runs (opportunity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reconciler_runs_status
    ON reconciler_runs (status);

-- Audit trail: every create / edit / approve / reject, with before/after
-- JSON. Kept even if the run is deleted (run_id becomes NULL) so the record
-- of what was posted survives.
CREATE TABLE IF NOT EXISTS reconciler_audit (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER REFERENCES reconciler_runs(id) ON DELETE SET NULL,
    opportunity_id  INTEGER,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,
    before          JSONB,
    after           JSONB,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reconciler_audit_run
    ON reconciler_audit (run_id, created_at DESC);
