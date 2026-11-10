-- 050_mail_sender_guard.sql
-- Bind a mail account to the job Contact it is allowed to send for, plus the
-- audit/error columns the sender guard needs.
--
-- Mapping model (Ken, 2026-11-10): the job's Contact (the contractor customer)
-- is the contractor identity. An account mapped to that Contact is the only
-- account allowed to send for that job. When the Contact has no mapped
-- account, the user picks any account manually (no alphabetical default).
--
-- Additive only. No row is deleted. The message-id unique index is created
-- ONLY when no duplicates exist yet (existing installs may have them); the
-- cleanup script handles those.

ALTER TABLE mail_accounts
  ADD COLUMN IF NOT EXISTS contractor_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_mail_accounts_contractor_contact
  ON mail_accounts (contractor_contact_id);

CREATE TABLE IF NOT EXISTS mail_send_refusals (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INTEGER,
    account_id INTEGER,
    deal_id    INTEGER,
    reason     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mail_send_refusals_created
  ON mail_send_refusals (created_at DESC);

ALTER TABLE mail_outgoing ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE mail_outgoing ADD COLUMN IF NOT EXISTS fallback_reason TEXT;

ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS auth_error TEXT;
ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS auth_error_at TIMESTAMPTZ;

-- Safe partial unique index: only when the data is already clean.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM mail_messages
     WHERE message_id IS NOT NULL AND message_id <> ''
     GROUP BY account_id, message_id
    HAVING count(*) > 1
  ) THEN
    CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_messages_account_message_id
      ON mail_messages (account_id, message_id)
      WHERE message_id IS NOT NULL AND message_id <> '';
  END IF;
END $$;
