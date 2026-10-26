-- Migration 038: IMAP UID watermark per folder for incremental sync.
--
-- The scanner used to re-download full bodies for the whole retention window
-- every cycle and dedup afterwards (minutes per cycle). With these columns it
-- fetches only UID > last_seen_uid when UIDVALIDITY is unchanged, falling back
-- to the date window on first run or UIDVALIDITY drift.
ALTER TABLE mail_folders ADD COLUMN IF NOT EXISTS last_seen_uid BIGINT;
ALTER TABLE mail_folders ADD COLUMN IF NOT EXISTS uid_validity BIGINT;
