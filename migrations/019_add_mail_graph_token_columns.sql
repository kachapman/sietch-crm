-- 019_add_mail_graph_token_columns.sql
-- Adds Graph sendMail OAuth token storage to mail_accounts.
-- Separate from the existing oauth_* columns (which hold Exchange-audience
-- tokens for IMAP/SMTP). Microsoft can disable SMTP AUTH on a mailbox (5.7.139
-- "SmtpClientAuthentication is disabled") for consumer accounts and refuses
-- to re-enable it via support; Graph sendMail with Mail.Send scope bypasses
-- the SMTP AUTH disable by using a different code path entirely.

ALTER TABLE mail_accounts
  ADD COLUMN IF NOT EXISTS oauth_graph_access_token  TEXT,
  ADD COLUMN IF NOT EXISTS oauth_graph_refresh_token TEXT,
  ADD COLUMN IF NOT EXISTS oauth_graph_token_expires TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS oauth_graph_scope         TEXT;
