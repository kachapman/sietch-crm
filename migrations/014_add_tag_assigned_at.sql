-- Migration 014: Add assigned_at timestamp to opportunity_tags

ALTER TABLE opportunity_tags ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMP DEFAULT NOW();

-- Backfill from tag_definitions.created_at as approximation
UPDATE opportunity_tags ot
SET assigned_at = COALESCE(
    (SELECT t.created_at FROM tag_definitions t WHERE t.id = ot.tag_id),
    NOW()
)
WHERE ot.assigned_at IS NULL;
