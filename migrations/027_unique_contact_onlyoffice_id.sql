-- Migration 027: Make contacts.onlyoffice_id a unique key for dedupe
--
-- The original onlyoffice_id column was added with a plain (non-unique) index.
-- We need a UNIQUE partial index so ON CONFLICT (onlyoffice_id) WHERE NOT NULL
-- works during re-runs of the import scripts. Pre-flight: ensure no dupes exist.

DO $$
BEGIN
    -- Drop existing non-unique index if present
    DROP INDEX IF EXISTS idx_contacts_onlyoffice_id;
EXCEPTION WHEN OTHERS THEN
    -- Ignore (may not exist on some envs)
    NULL;
END $$;

-- Add the unique partial index. If there are duplicate onlyoffice_id rows from a
-- botched import, surface them and abort so the operator can clean up first.
DO $$
DECLARE
    dup_count INT;
BEGIN
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT onlyoffice_id FROM contacts
        WHERE onlyoffice_id IS NOT NULL
        GROUP BY onlyoffice_id HAVING COUNT(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'Cannot create uq_contacts_onlyoffice_id: % duplicate onlyoffice_id rows exist; deduplicate first', dup_count;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_contacts_onlyoffice_id
    ON contacts(onlyoffice_id) WHERE onlyoffice_id IS NOT NULL;