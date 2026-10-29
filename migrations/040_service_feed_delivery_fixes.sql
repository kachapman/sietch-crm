-- Migration 040: service feed delivery fixes (live 2026-10-29)
--
-- 1) service_feed_items — per-entry delivery ledger.
--    The poller used to compare every entry id against ONE remembered id
--    (service_feeds.last_item_id). The Proton feed has 25 entries with
--    stable ids and only the newest ever matched, so the other 24 were
--    re-delivered on EVERY poll (every 5 min) to 8 users each:
--    192 rows per poll, ~2,300/hour, ~55k/day, 1,438,873 rows in a month.
--    Now each (feed_id, item_id) is recorded once and delivered once.
--
-- 2) service_feeds.notify_keywords — per-feed "only notify about updates
--    containing..." filter. Empty JSON array = notify about everything.
--
-- 3) service_feed_subscriptions — per-user opt-out. A row with
--    enabled = FALSE means the user switched that feed off; the row is
--    written by the user's own notification preferences.
--
-- 4) service_feeds.target_mode default changes from 'all' to 'admins'.
--    A status feed must not notify every user by default (the user asked
--    for this explicitly). Existing rows keep their current value; the
--    UPDATE below moves the existing Proton feed to 'admins' too, since
--    every feed notification it produced was unreadable anyway (see 5).
--
-- 5) The orphaned notifications are deleted here.
--    service_feed notifications were inserted WITHOUT user_id, so every one
--    of them had user_id = NULL. The drawer query filters
--    "WHERE n.user_id = %s", so NOT ONE was ever visible to anyone —
--    1,438,873 rows that nobody could read, costing 822MB and a month of
--    pointless dispatcher UPDATE churn. Deleting them removes nothing any
--    user has ever seen. The insert path is fixed in server.py.
--
-- NOTE: the table is huge, so this deletes in batches to keep the
-- transaction small. It is idempotent.

-- ── 1) delivery ledger ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_feed_items (
    feed_id      INTEGER NOT NULL REFERENCES service_feeds(id) ON DELETE CASCADE,
    item_id      TEXT NOT NULL,
    delivered_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (feed_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_service_feed_items_delivered
    ON service_feed_items(delivered_at);

-- ── 2) per-feed keyword filter ──────────────────────────────────────────
ALTER TABLE service_feeds
    ADD COLUMN IF NOT EXISTS notify_keywords TEXT NOT NULL DEFAULT '[]';

-- ── 3) per-user opt-out ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_feed_subscriptions (
    feed_id    INTEGER NOT NULL REFERENCES service_feeds(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (feed_id, user_id)
);

-- ── 4) safer default audience ───────────────────────────────────────────
-- New feeds default to admins only.
ALTER TABLE service_feeds ALTER COLUMN target_mode SET DEFAULT 'admins';

-- The existing feeds move to the safe audience as well (the user asked for
-- feeds not to notify every user).
UPDATE service_feeds SET target_mode = 'admins' WHERE target_mode = 'all';

-- The Proton Status feed is mostly "Service Maintenance, <date>" entries;
-- the user only wants outage news. 10 of its 25 entries match.
UPDATE service_feeds
   SET notify_keywords = '["outage", "disruption", "incident"]'
 WHERE name ILIKE '%Proton%';

-- ── 5) remove the orphaned, unreadable feed notifications ───────────────
-- Batched so each statement stays small on a 1.4M-row table.
--
-- NOTE: no VACUUM here. The migration runner executes each file as a single
-- statement batch inside one implicit transaction, and "VACUUM cannot run
-- inside a transaction block" would abort the whole migration. The DELETE
-- still frees the rows so the space is reusable; a plain
-- "VACUUM (ANALYZE) notifications;" can be run separately if the space
-- needs to be returned to the filesystem.
DO $$
DECLARE
    removed INTEGER := 0;
BEGIN
    LOOP
        DELETE FROM notifications
         WHERE id IN (
             SELECT id FROM notifications
              WHERE type = 'service_feed'
              LIMIT 50000
         );
        GET DIAGNOSTICS removed = ROW_COUNT;
        EXIT WHEN removed = 0;
    END LOOP;
END $$;
