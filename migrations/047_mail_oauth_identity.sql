-- 047_mail_oauth_identity.sql
-- Binds a mail account's OAuth tokens to the identity that granted them.
--
-- Live bug 2026-11-09: the settings page for estimates.masterx@outlook.com
-- showed "Connected as mdm.estimates@outlook.com" and every Outlook account
-- ended up auth_failed. The `oauth_graph_*` columns already recorded the
-- connected identity; the IMAP/SMTP (Exchange audience) tokens did not, so
-- the UI had to *assume* the identity was the account email and nothing
-- could detect a mis-bound Exchange token.
--
-- `oauth_identity` is the signed-in identity read from the token's id_token
-- at connect time and re-verified on every refresh. When it differs from
-- `email` the connection is refused instead of silently used.
--
-- `oauth_last_error` / `oauth_last_error_at` mirror the Graph error columns
-- so an Exchange refresh failure (invalid_grant, expired client secret,
-- revoked consent, identity mismatch) survives a restart and can be shown
-- in the UI + the read-only diagnostic script.
--
-- Additive only: no DROP, no DELETE, no data rewrite.

ALTER TABLE mail_accounts
  ADD COLUMN IF NOT EXISTS oauth_identity        TEXT,
  ADD COLUMN IF NOT EXISTS oauth_last_error      TEXT,
  ADD COLUMN IF NOT EXISTS oauth_last_error_at   TIMESTAMPTZ;
