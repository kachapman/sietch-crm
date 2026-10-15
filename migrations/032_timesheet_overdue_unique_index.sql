-- Migration 032: Unique index for timesheet overdue notifications.
--
-- The timesheet_overdue_checker (timesheet_overdue_checker.py) fires a
-- "you've been clocked in for 10+ hours" notification per open shift.
-- Two thresholds are used: 10h and 14h. We want exactly one notification
-- per (user, entry, threshold) tuple, even if the checker runs twice,
-- the server restarts, or two instances are running.
--
-- The unique index uses JSONB expressions (payload->>'entry_id' and
-- payload->>'threshold_hours') so the de-duplication is data-driven
-- and survives schema changes. ON CONFLICT DO NOTHING in the checker
-- turns the INSERT into a no-op when the row already exists.

CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_timesheet_overdue
    ON notifications (
        user_id,
        type,
        ((payload->>'entry_id')),
        ((payload->>'threshold_hours'))
    )
    WHERE type = 'timesheet_overdue';
