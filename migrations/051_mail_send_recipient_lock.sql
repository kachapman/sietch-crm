-- 051_mail_send_recipient_lock.sql
-- Per-user recipient lock for the live acceptance run (Ken 1:41 PM).
-- NULL = no lock (normal users). Set only on the acceptance test user.
ALTER TABLE users ADD COLUMN IF NOT EXISTS mail_send_locked_recipient TEXT;
