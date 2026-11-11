-- 055_seed_missing_history_categories.sql
--
-- Why: migrations 016 and 017 are recorded in `_applied_migrations` on
-- production, but some of their seed INSERTs never landed (the old runner
-- marked a failed file as applied). Read-only audit 2026-11-10:
--
--   * 017: the Action history category is missing. Field-change writers then
--     stored category_id = NULL (compact rendering now relies on
--     history_events.is_system_event from 053).
--   * 016: Follow-Up, Estimate, Inspection, and Expression of gratitude are
--     missing. Opportunity and Stage Estimate already exist and are left
--     alone. task_labels from 016 are already present.
--
-- This file inserts only those five missing history_categories rows, with the
-- same columns and values as the original 016/017 INSERT statements.
-- Idempotent: INSERT ... SELECT ... WHERE NOT EXISTS (title).
--
-- SAFETY: no UPDATE of tasks.category_id or history_events.category_id.
-- Ken (2026-11-10 10:10 PM ET): do not reassign existing tasks or history
-- rows. The 016 backfill (`UPDATE tasks SET category_id = Follow-Up WHERE
-- category_id IS NULL`) is intentionally omitted.

INSERT INTO history_categories (title, display_color, is_system, sort_order, icon_key, icon_style)
SELECT 'Action', '#6c757d', TRUE, 15, 'pencil', 'accent'
WHERE NOT EXISTS (
    SELECT 1 FROM history_categories WHERE title = 'Action'
);

INSERT INTO history_categories (title, icon_key, icon_style, is_system, sort_order, display_color)
SELECT 'Follow-Up', 'clock', 'stroke', TRUE, 9, NULL
WHERE NOT EXISTS (
    SELECT 1 FROM history_categories WHERE title = 'Follow-Up'
);

INSERT INTO history_categories (title, icon_key, icon_style, is_system, sort_order, display_color)
SELECT 'Estimate', 'document', 'stroke', TRUE, 10, NULL
WHERE NOT EXISTS (
    SELECT 1 FROM history_categories WHERE title = 'Estimate'
);

INSERT INTO history_categories (title, icon_key, icon_style, is_system, sort_order, display_color)
SELECT 'Inspection', 'building', 'stroke', TRUE, 11, NULL
WHERE NOT EXISTS (
    SELECT 1 FROM history_categories WHERE title = 'Inspection'
);

INSERT INTO history_categories (title, icon_key, icon_style, is_system, sort_order, display_color)
SELECT 'Expression of gratitude', 'star', 'stroke', TRUE, 12, NULL
WHERE NOT EXISTS (
    SELECT 1 FROM history_categories WHERE title = 'Expression of gratitude'
);
