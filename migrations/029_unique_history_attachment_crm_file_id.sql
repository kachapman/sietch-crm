-- Migration 029: Promote history_attachments.crm_file_id to a unique index
--
-- Required so the live sync can use ON CONFLICT (crm_file_id) for idempotent
-- file-attachment downloads.

DO $$
DECLARE
    dup_count INT;
BEGIN
    -- Drop existing non-unique index
    DROP INDEX IF EXISTS idx_history_attachments_crm_file_id;

    -- Pre-flight: surface any duplicate crm_file_id rows so the operator can clean
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT crm_file_id FROM history_attachments
        WHERE crm_file_id IS NOT NULL
        GROUP BY crm_file_id HAVING COUNT(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'Cannot create uq_history_attachments_crm_file_id: % duplicate crm_file_id rows; clean up first', dup_count;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_history_attachments_crm_file_id
    ON history_attachments(crm_file_id) WHERE crm_file_id IS NOT NULL;