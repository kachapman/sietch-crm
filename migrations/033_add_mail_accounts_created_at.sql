-- Migration 033: add created_at to mail_accounts
-- _handle_mail_accounts queries this column but it was never created.
-- init.sql mirror is updated separately.

ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
