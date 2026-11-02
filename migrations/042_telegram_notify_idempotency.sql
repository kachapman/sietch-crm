-- 042: idempotent Telegram notification delivery (+ creation dedupe key).
--
-- Background: a reply_tagged notification was delivered ~91 times to one
-- employee. The dispatcher (notification_dispatcher.py) had no idempotency:
--   * SELECT-then-SEND-then-MARK across separate statements, so two
--     dispatcher passes (stacked server processes after the frequent
--     restarts, or a crash between send and mark) both delivered the row;
--   * any row left with telegram_sent FALSE was re-sent on EVERY 5s poll
--     forever — no backoff, no attempt cap, no dead-letter;
--   * tag-notification INSERTs had no dedupe guard, so a retried POST
--     created a second row (and a second delivery) for the same reply.
--
-- This migration adds the columns the fixed dispatcher needs:
--   * dedupe_key   — stable key per (type, event, reply, recipient); unique
--                     so creation is idempotent even under retried POSTs.
--   * telegram_attempts      — incremented on each atomic claim.
--   * telegram_next_retry_at — claim lease + exponential-backoff gate; rows
--                              with a future timestamp are skipped by the poll.
--   * telegram_last_error    — last send failure, for drawer/admin diagnosis.

ALTER TABLE notifications ADD COLUMN IF NOT EXISTS dedupe_key TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS telegram_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS telegram_next_retry_at TIMESTAMP;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS telegram_last_error TEXT;

-- One delivery per tagged note/reply per recipient. Plain (non-partial)
-- UNIQUE index: NULL dedupe_keys (legacy rows, other channel types) never
-- conflict with each other in Postgres, so no predicate is needed — and a
-- non-partial index is what plain `ON CONFLICT (dedupe_key) DO NOTHING`
-- requires (a partial index would need the predicate repeated at every
-- INSERT site).
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe_key
    ON notifications (dedupe_key);

-- Poll fast-path: only rows that still need Telegram and whose backoff
-- lease has expired (plus the unchanged email-side predicate in the query).
CREATE INDEX IF NOT EXISTS idx_notifications_tg_pending
    ON notifications (telegram_sent, telegram_next_retry_at, created_at)
    WHERE user_id IS NOT NULL;
