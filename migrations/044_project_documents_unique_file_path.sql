-- 044: unique index on project_documents(opportunity_id, file_path).
--
-- Bug fix 2026-11-03 (round 2): two concurrent link requests (a
-- double-click on "Link to deal") could each insert a project_documents
-- row for the same file, so the project showed the PDF twice in its
-- Documents tab. The link-time copy now relies on
--
--     ON CONFLICT (opportunity_id, file_path) WHERE is_deleted = FALSE
--     DO NOTHING
--
-- which needs a matching unique index.
--
-- ⚠ WHY THIS IS A SEPARATE MIGRATION (round 3)
--
-- The index was originally added to migration 043, but a UNIQUE index
-- cannot be created on a table that already holds duplicates: Postgres
-- raises
--
--     ERROR: could not create unique index "uq_project_documents_opp_file_path"
--     DETAIL: Key (opportunity_id, file_path)=(42, data/x.pdf) is duplicated.
--
-- and the whole migration file would then fail (and be recorded as
-- "applied" by db.py's runner, so it would never be retried — see
-- db.py `_run_pending_migrations`).
--
-- This migration therefore checks FIRST:
--   * no duplicates  -> build the index;
--   * duplicates     -> LOG them clearly (the count and a sample of the
--                       offending keys) and SKIP building the index.
--
-- ⚠ IT NEVER DELETES OR MODIFIES ANY DATA. The duplicate rows are left
-- exactly as they are; deduplicating them is a separate, explicit
-- decision (and the Duplicates can be reviewed in the admin console).
--
-- When the index is skipped, the link-time copy still works: its
-- `ON CONFLICT ... DO NOTHING` needs the index to be a no-op-safe
-- arbiter, so without it the INSERT simply never conflicts and the
-- duplicate-document behaviour returns. That is why the log message is
-- loud — the operator needs to know the index is missing.

DO $$
DECLARE
    dup_count   BIGINT;
    dup_sample  TEXT;
BEGIN
    -- How many (opportunity_id, file_path) keys are duplicated among the
    -- ACTIVE (not soft-deleted) rows?
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT opportunity_id, file_path
        FROM project_documents
        WHERE is_deleted = FALSE
          AND opportunity_id IS NOT NULL
          AND file_path IS NOT NULL
        GROUP BY opportunity_id, file_path
        HAVING COUNT(*) > 1
    ) d;

    IF dup_count > 0 THEN
        -- A readable sample of the offending keys (up to 10).
        SELECT string_agg(
                   format('opportunity_id=%s file_path=%s (x%s)',
                          d.opportunity_id, d.file_path, d.cnt),
                   E'\n  ')
        INTO dup_sample
        FROM (
            SELECT opportunity_id, file_path, COUNT(*) AS cnt
            FROM project_documents
            WHERE is_deleted = FALSE
              AND opportunity_id IS NOT NULL
              AND file_path IS NOT NULL
            GROUP BY opportunity_id, file_path
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC, opportunity_id
            LIMIT 10
        ) d;

        RAISE WARNING
            'MIGRATION 044 SKIPPED: uq_project_documents_opp_file_path was NOT created.'
            ' % duplicate (opportunity_id, file_path) key(s) exist among the active'
            ' project_documents rows. No data was changed. Sample of the duplicates:'
            '   %'
            ' The duplicate documents must be reviewed and removed by an operator'
            ' before the index can be built.',
            dup_count, dup_sample;

        RETURN;   -- skip the index; leave every row untouched
    END IF;

    RAISE NOTICE
        'MIGRATION 044: no duplicate (opportunity_id, file_path) keys — building'
        ' uq_project_documents_opp_file_path.';

    CREATE UNIQUE INDEX IF NOT EXISTS uq_project_documents_opp_file_path
        ON project_documents (opportunity_id, file_path)
        WHERE is_deleted = FALSE;
END
$$;
