-- =============================================================================
-- Migration 001: Add mail tags, templates, tag assignments, sharing, outgoing
-- =============================================================================

-- Tags
CREATE TABLE IF NOT EXISTS mail_tags (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    color TEXT DEFAULT '#6c757d',
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(title)
);

CREATE TABLE IF NOT EXISTS mail_tag_assignments (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES mail_tags(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(message_id, tag_id)
);

-- Templates
CREATE TABLE IF NOT EXISTS mail_templates (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    subject TEXT DEFAULT '',
    body_html TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Account sharing
CREATE TABLE IF NOT EXISTS mail_account_access (
    id SERIAL PRIMARY KEY,
    account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    granted_by INTEGER REFERENCES users(id),
    granted_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(account_id, user_id)
);

-- Outgoing mail tracking
CREATE TABLE IF NOT EXISTS mail_outgoing (
    id SERIAL PRIMARY KEY,
    account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    from_addr TEXT,
    to_addr TEXT,
    cc_addr TEXT,
    bcc_addr TEXT,
    subject TEXT,
    body_text TEXT,
    body_html TEXT,
    deal_id INTEGER REFERENCES opportunities(id),
    template_id INTEGER REFERENCES mail_templates(id),
    status TEXT DEFAULT 'queued',
    sent_at TIMESTAMP,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mail_outgoing_deal ON mail_outgoing(deal_id);
CREATE INDEX IF NOT EXISTS idx_mail_outgoing_status ON mail_outgoing(status);

-- Add SMTP columns to mail_accounts
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS smtp_host TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS smtp_port INTEGER DEFAULT 587;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS smtp_user TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS smtp_password_encrypted TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS smtp_use_tls BOOLEAN DEFAULT TRUE;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS smtp_from_name TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_provider TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_access_token TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_refresh_token TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_token_expires TIMESTAMP;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_scopes TEXT;