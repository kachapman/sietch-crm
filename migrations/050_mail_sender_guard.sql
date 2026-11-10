-- 050_mail_sender_guard.sql
-- Durable outgoing send errors + mail account auth health, and a safe
-- message-id uniqueness guard. There is no job/Contact → mailbox mapping:
-- a reply/forward must simply be sent from the account that received the
-- original message, and the selected account's credential identity must
-- match the mailbox (enforced in server.py, no audit table).
--
-- Additive only. No row is deleted. The message-id unique index is created
-- ONLY when no duplicates exist yet (existing installs may have them); the
-- cleanup script handles those.

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
