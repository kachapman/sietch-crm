-- Migration 008: Add email notification channel
-- Adds email_sent tracking to notifications and email branding fields to branding table.

-- Email tracking on notifications
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS email_sent BOOLEAN DEFAULT FALSE;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMP;

-- Email branding fields
ALTER TABLE branding ADD COLUMN IF NOT EXISTS email_header_color TEXT DEFAULT '#3b82f6';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS email_footer_text TEXT DEFAULT 'Sietch CRM — Confidential';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS email_from_name TEXT;
