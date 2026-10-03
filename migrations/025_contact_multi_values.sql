-- Migration 025: Multi-value contact storage
--
-- OnlyOffice stores multiple per-type entries per contact (e.g. Work / Mobile /
-- Home phones, multiple emails, multiple addresses). The base contacts table
-- only has one email/phone/address, so 2-N entries get lost on import.
--
-- These tables preserve them. They also keep `isPrimary` and `category` (Work,
-- Home, Billing, Office, etc.) for round-trip fidelity.

CREATE TABLE IF NOT EXISTS contact_emails (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    category TEXT,
    category_id INTEGER,
    is_primary BOOLEAN DEFAULT FALSE,
    onlyoffice_id BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contact_emails_contact ON contact_emails(contact_id);
CREATE INDEX IF NOT EXISTS idx_contact_emails_email ON contact_emails(email);

CREATE TABLE IF NOT EXISTS contact_phones (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    phone TEXT NOT NULL,
    category TEXT,
    category_id INTEGER,
    is_primary BOOLEAN DEFAULT FALSE,
    onlyoffice_id BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contact_phones_contact ON contact_phones(contact_id);

CREATE TABLE IF NOT EXISTS contact_addresses (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    street TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    country TEXT,
    category TEXT,
    category_id INTEGER,
    is_primary BOOLEAN DEFAULT FALSE,
    onlyoffice_id BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contact_addresses_contact ON contact_addresses(contact_id);

-- Persist a few extra OnlyOffice contact fields that don't fit the base schema
ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS onlyoffice_task_count INTEGER;

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS onlyoffice_status_id BIGINT;

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS raw_about TEXT;

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS raw_industry TEXT;

-- Avatar is downloaded at import time so it survives the OnlyOffice retirement
ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS avatar_local_path TEXT;