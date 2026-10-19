-- Migration 034: prevent duplicate history events per (opportunity, CRM event).
--
-- The pull-deals sync dedups on (opportunity_id, crm_event_id) in Python, but
-- that check is per-opportunity and only sees events loaded in the current run.
-- A separate import path (and a prior home-box sync) produced 7,074 duplicate
-- (opportunity_id, crm_event_id) groups -- 14,148 rows for 7,074 unique events.
--
-- The cleanup that removed those duplicates ran before this migration was
-- applied. This index is the durable guard.
--
-- Partial: only rows that actually carry a crm_event_id are constrained. Rows
-- without one (locally-created events) are unconstrained and can repeat.

CREATE UNIQUE INDEX IF NOT EXISTS uq_history_events_opp_crm_event
    ON history_events (opportunity_id, crm_event_id)
    WHERE crm_event_id IS NOT NULL;
