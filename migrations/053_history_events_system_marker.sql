-- 053_history_events_system_marker.sql
--
-- Why: a field change written by `_handle_project_update` used to be
-- identified in the UI only by its category ("Action", `is_system=TRUE`) or by
-- a title allowlist. On production the `Action` category row is missing
-- (migration 017 is recorded in `_applied_migrations` but its seed rolled back
-- when an earlier statement in the same file failed — see
-- `db._run_pending_migrations`, which records a failed migration as applied),
-- so the field-change writer stored `category_id = NULL`. The classifier then
-- saw no category and rendered Ken's "Follow-up Due Date changed from … to …"
-- row as a full card with a Reply button (2026-11-10 8:27 PM ET, event 50504).
--
-- The durable fix is a marker on the history row itself, written by every
-- auto-log writer. This migration adds the column and backfills existing rows
-- from durable signals only.
--
-- BACKFILL PREDICATE (reported before writing this file):
--   * `category_id IS NULL` AND content is exactly one of the tracked-field
--     logger shapes ("{label} changed from {old} to {new}", "{label} set to
--     {new}", "{label} cleared"), on a single plain-text line (no HTML tags,
--     no newlines); the label set comes from the server's TRACKED_FIELDS. `category_id IS NULL` is only ever produced by that
--     writer when the Action lookup misses — no human writer can emit NULL.
--   * any row filed under a category titled "action" (the CRM's auto-log
--     category);
--   * the three titles only the document generators emit.
-- On production (2026-11-10) this matched 405 rows, every one a NULL-category
-- field change; 0 Action-category rows and 0 generated-document rows. Rows whose
-- content merely contains "set to" in human prose (13 Note, 6 Mail Message,
-- 4 Customer Update, 1 Email) do NOT match the anchored shape and stay full
-- cards on purpose.
--
-- Emails are untouched: they keep their full card (body, attachments, reply
-- thread) even when a category is a system category.
--
-- SAFETY: additive + idempotent. No column is dropped, no row is deleted.

ALTER TABLE history_events
    ADD COLUMN IF NOT EXISTS is_system_event BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE history_events he
SET is_system_event = TRUE
WHERE he.is_system_event IS DISTINCT FROM TRUE
  AND (
      (he.category_id IS NULL
       AND he.content !~ '[<>]'
       AND he.content !~ E'[\n\r]'
       AND he.content ~ '^(Stage|Value|Contact|Responsible|Follow-up Due Date|Private) (changed from [^<>]+ to [^<>]+|set to [^<>]+|cleared)$')
      OR he.category_id IN (SELECT id FROM history_categories WHERE lower(btrim(title)) = 'action')
      OR he.title IN ('Invoice generated', 'Photo Report generated', 'Certificate of Completion generated')
  );
