-- Migration 036: Calendar reminder snooze
-- snoozed_until suppresses the reminder sweep until the timestamp passes.

ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ;
