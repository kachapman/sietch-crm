-- Migration 012: Bot configuration + activity log tables
-- Phase 2K: Telegram Bot Overhaul

-- Bot configuration (token, settings, feature flags)
CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT NOW(),
    updated_by INTEGER REFERENCES users(id)
);

-- Seed default bot settings (token is empty — admin fills via UI)
INSERT INTO bot_config (key, value) VALUES ('settings', '{
    "token": "",
    "botUsername": "",
    "botLink": "",
    "maxFileSizeMb": 20,
    "pollInterval": 5,
    "enabled": true,
    "allowCustomerUploads": false,
    "welcomeMessage": "Welcome! Send an invite code to link your account.",
    "configVersion": 0
}'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- Bot activity log (searches, notes, uploads, errors, heartbeats)
CREATE TABLE IF NOT EXISTS bot_activity_log (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT,
    user_name TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    project_id INTEGER REFERENCES opportunities(id),
    is_error BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_activity_log_created ON bot_activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_activity_log_error ON bot_activity_log(is_error, created_at DESC) WHERE is_error = TRUE;
