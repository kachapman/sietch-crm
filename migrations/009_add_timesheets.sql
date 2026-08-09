-- Migration 009: Add timesheet system
-- time_entries table + time_entries_daily view

CREATE TABLE IF NOT EXISTS time_entries (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    clock_in TIMESTAMP NOT NULL,
    clock_out TIMESTAMP,
    break_minutes INTEGER DEFAULT 0,
    break_start TIMESTAMP,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    notes TEXT DEFAULT '',
    auto_closed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_time_entries_user ON time_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_time_entries_clock_in ON time_entries(clock_in);
CREATE INDEX IF NOT EXISTS idx_time_entries_opp ON time_entries(opportunity_id) WHERE opportunity_id IS NOT NULL;

CREATE OR REPLACE VIEW time_entries_daily AS
SELECT
    user_id,
    DATE(clock_in) AS work_date,
    COUNT(*) AS entry_count,
    SUM(
        EXTRACT(EPOCH FROM (COALESCE(clock_out, NOW()) - clock_in)) / 3600
        - break_minutes / 60.0
    ) AS total_hours,
    SUM(break_minutes) AS total_break_minutes
FROM time_entries
GROUP BY user_id, DATE(clock_in);
