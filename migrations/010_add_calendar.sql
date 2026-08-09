-- Migration 010: Full calendar system
-- calendar_sources: OAuth, CalDAV, and ICS feed connections
-- calendar_events: unified local event store
-- ics_feed_cache: server-side ICS caching (fixes dashboard hang)

-- Calendar sources (OAuth accounts, CalDAV accounts, ICS feeds)
CREATE TABLE IF NOT EXISTS calendar_sources (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('ics', 'google_oauth', 'microsoft_oauth', 'caldav')),
    -- OAuth fields
    oauth_provider TEXT,
    oauth_access_token TEXT,
    oauth_refresh_token TEXT,
    oauth_token_expires_at TIMESTAMPTZ,
    oauth_email TEXT,
    -- CalDAV fields
    caldav_url TEXT,
    caldav_username TEXT,
    caldav_password_encrypted TEXT,
    -- ICS fields
    ics_url TEXT,
    -- Sync state
    last_synced_at TIMESTAMPTZ,
    sync_cursor TEXT,
    -- Display
    color TEXT DEFAULT '#5b8abf',
    visible BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cal_sources_user ON calendar_sources(user_id);

-- Calendar events (unified local store)
CREATE TABLE IF NOT EXISTS calendar_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    source_id INTEGER REFERENCES calendar_sources(id) ON DELETE CASCADE,
    uid TEXT NOT NULL,
    summary TEXT,
    description TEXT,
    location TEXT,
    dtstart TIMESTAMPTZ NOT NULL,
    dtend TIMESTAMPTZ,
    all_day BOOLEAN DEFAULT FALSE,
    rrule TEXT,
    color TEXT,
    status TEXT DEFAULT 'confirmed',
    -- External sync
    external_id TEXT,
    last_modified TIMESTAMPTZ,
    -- Reminders
    reminder_minutes INTEGER,
    reminder_sent BOOLEAN DEFAULT FALSE,
    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_cal_events_user_time ON calendar_events(user_id, dtstart, dtend);
CREATE INDEX IF NOT EXISTS idx_cal_events_source ON calendar_events(source_id) WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cal_events_reminder ON calendar_events(user_id, dtstart) WHERE reminder_sent = FALSE AND reminder_minutes IS NOT NULL;

-- ICS feed cache (prevents dashboard hang on load)
CREATE TABLE IF NOT EXISTS ics_feed_cache (
    id SERIAL PRIMARY KEY,
    url_hash TEXT NOT NULL UNIQUE,
    raw_ics TEXT,
    parsed_events JSONB,
    fetched_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    calendar_name TEXT,
    timezone TEXT
);
