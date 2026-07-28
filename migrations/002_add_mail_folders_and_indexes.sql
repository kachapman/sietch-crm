-- =============================================================================
-- Migration 002: Add mail_folders table, attachment tracking, raw headers
-- =============================================================================

-- Mail folders (referenced by server.py + scanner but missing from init.sql)
CREATE TABLE IF NOT EXISTS mail_folders (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT DEFAULT 'folder',
    folder_type TEXT DEFAULT 'local',
    imap_account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    imap_path TEXT,
    last_sync TIMESTAMP,
    user_id INTEGER REFERENCES users(id),
    is_system BOOLEAN DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mail_folders_account ON mail_folders(imap_account_id);
CREATE INDEX IF NOT EXISTS idx_mail_folders_user ON mail_folders(user_id);

-- Attachment tracking for incoming messages
CREATE TABLE IF NOT EXISTS mail_attachments (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    mime_type TEXT DEFAULT 'application/octet/octet-stream',
    size_bytes INTEGER DEFAULT 0,
    imap_part_id TEXT,
    stored_path TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mail_attachments_msg ON mail_attachments(message_id);

-- Raw headers for full email header view
ALTER TABLE mail_messages ADD COLUMN IF NOT EXISTS raw_headers TEXT;

-- Add attachments_json to mail_messages for quick metadata access
ALTER TABLE mail_messages ADD COLUMN IF NOT EXISTS attachments_json JSONB;

-- Scanner contractors (replaces contractors.json file)
CREATE TABLE IF NOT EXISTS mail_contractors (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    imap_account_id INTEGER REFERENCES mail_accounts(id) ON DELETE SET NULL,
    folder TEXT DEFAULT 'INBOX',
    action TEXT DEFAULT 'link_only',
    responsible_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    enabled BOOLEAN DEFAULT TRUE,
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Classification rules
CREATE TABLE IF NOT EXISTS mail_classification_rules (
    id SERIAL PRIMARY KEY,
    rule_name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    action TEXT DEFAULT 'tag',
    action_target TEXT,
    priority INTEGER DEFAULT 0,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
