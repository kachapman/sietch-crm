-- Mail account settings: auto-BCC, auto-delete retention, tab icon/accent color
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS auto_bcc_addr TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS auto_delete_days INTEGER DEFAULT 0;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS tab_icon TEXT DEFAULT 'user';
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS tab_color TEXT DEFAULT 'var(--accent)';
