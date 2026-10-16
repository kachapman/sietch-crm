-- Migration 034: Calendar recurrence occurrences
-- Recurring ICS masters are expanded at sync into per-occurrence rows
-- (stable uid "<master>#<timestamp>", recurrence_id = master uid).
-- Adds recurrence_id + is_occurrence to calendar_events.

ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS recurrence_id TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS is_occurrence BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_cal_events_recurrence
    ON calendar_events(user_id, recurrence_id) WHERE recurrence_id IS NOT NULL;
