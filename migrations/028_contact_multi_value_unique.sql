-- Migration 028: Unique constraints on contact multi-value tables
--
-- Required so the import scripts can use ON CONFLICT DO NOTHING for idempotent
-- re-runs. (Per-row, not just per-contact.)

CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_emails_oo_id
    ON contact_emails(onlyoffice_id) WHERE onlyoffice_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_phones_oo_id
    ON contact_phones(onlyoffice_id) WHERE onlyoffice_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_addresses_oo_id
    ON contact_addresses(onlyoffice_id) WHERE onlyoffice_id IS NOT NULL;