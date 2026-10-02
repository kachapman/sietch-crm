-- Migration 022: Service Feeds (third-party status / webhook → notifications)
--
-- Lets admins subscribe to third-party services (Proton status page, BlueBubbles
-- webhooks, etc.) and surface their incidents/status changes as in-app
-- notifications. RSS/Atom feeds are polled on an interval; webhooks are
-- delivered by the provider POSTing to /api/v2/service-feeds/webhook/{secret}.
--
-- Each feed has a target_mode:
--   'all'      → every active user receives notifications from this feed
--   'specific' → only users whose id is in target_user_ids receive them
--
-- accent_color lets admins tint the notification card (icon background + left
-- stripe) so different feeds are visually distinguishable.

CREATE TABLE IF NOT EXISTS service_feeds (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'rss',     -- 'rss' | 'webhook'
    url                 TEXT,
    webhook_secret      TEXT,
    poll_interval_sec   INTEGER NOT NULL DEFAULT 300,
    last_polled_at      TIMESTAMP,
    last_item_id        TEXT,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    target_mode         TEXT NOT NULL DEFAULT 'all',     -- 'all' | 'specific'
    target_user_ids     INTEGER[] NOT NULL DEFAULT '{}'::integer[],
    accent_color        TEXT NOT NULL DEFAULT '#e6a700',
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Webhook secret lookup is hot (every webhook POST does this). Unique index
-- on non-null webhook_secret values for fast O(log n) lookup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_service_feeds_webhook_secret
    ON service_feeds(webhook_secret)
    WHERE webhook_secret IS NOT NULL;

-- Notes:
--   * `url` is required for kind='rss'; null for kind='webhook'.
--   * `webhook_secret` is generated server-side on insert (random token); the
--     provider POSTs to /api/v2/service-feeds/webhook/{webhook_secret}.
--   * `target_user_ids` is only consulted when target_mode='specific'.
--   * Polling thread (server.py / service_feed_poller.py) updates
--     last_polled_at + last_item_id after each successful poll so the same
--     RSS entry is never delivered twice.