-- Migration 031: Per-reply notify users + notification wiring
--
-- The reply editor (added in v2.3.0 follow-up) wired up @-mentions on the
-- client. Server-side, _handle_history_reply_create accepted notifyUserList
-- but silently ignored it (TODO). This migration:
--
--   1. Adds history_reply_notify_users — parallel to history_notify_users,
--      keyed on (reply_id, user_id) instead of (event_id, user_id). Reusing
--      history_notify_users would have required making it nullable on event_id
--      OR adding a reply_id column; both risk breaking the existing event-side
--      flow's PK / dedup semantics.
--   2. Adds an index on reply_id for join lookups (e.g. "is this reply
--      tagged?" — used by the reply GET handler in case we later surface
--      a "tagged" indicator in the UI).
--
-- Server change:
--   _handle_history_reply_create now:
--     - inserts into history_reply_notify_users
--     - creates notification rows of type 'reply_tagged' (mirrors the
--       'note_tagged' flow for events), with payload { event_id, reply_id,
--       snippet }, so the client can deep-link to the reply.

CREATE TABLE history_reply_notify_users (
    reply_id INTEGER REFERENCES history_replies(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    PRIMARY KEY (reply_id, user_id)
);

CREATE INDEX idx_history_reply_notify_users_reply
    ON history_reply_notify_users(reply_id);