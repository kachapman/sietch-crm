-- Migration 024: Add OnlyOffice ID columns + actual_close_date for migration
--
-- Provides stable identifiers that survive title edits in the OnlyOffice source
-- (so re-running import won't create duplicates), and captures actualCloseDate
-- for closed-won deals (was previously dropped on import).

ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS onlyoffice_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunities_onlyoffice_id
    ON opportunities(onlyoffice_id)
    WHERE onlyoffice_id IS NOT NULL;

ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS actual_close_date DATE;

ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS actual_close_value NUMERIC(12,2);

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS onlyoffice_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_onlyoffice_id
    ON tasks(onlyoffice_id)
    WHERE onlyoffice_id IS NOT NULL;

ALTER TABLE history_events
    ADD COLUMN IF NOT EXISTS crm_event_id BIGINT;

ALTER TABLE history_events
    ADD COLUMN IF NOT EXISTS crm_entity_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_history_events_crm_event_id
    ON history_events(crm_event_id)
    WHERE crm_event_id IS NOT NULL;

ALTER TABLE history_attachments
    ADD COLUMN IF NOT EXISTS original_url TEXT;

ALTER TABLE history_attachments
    ADD COLUMN IF NOT EXISTS crm_file_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_history_attachments_crm_file_id
    ON history_attachments(crm_file_id)
    WHERE crm_file_id IS NOT NULL;