-- Migration 015: Task labels + history category icons

-- Task labels (normalized vocabulary for task categorization)
CREATE TABLE IF NOT EXISTS task_labels (
    id SERIAL PRIMARY KEY,
    title TEXT UNIQUE NOT NULL,
    color TEXT,
    sort_order INTEGER DEFAULT 0
);

-- History category icon customization
ALTER TABLE history_categories ADD COLUMN IF NOT EXISTS icon_key TEXT DEFAULT 'note';
ALTER TABLE history_categories ADD COLUMN IF NOT EXISTS icon_style TEXT DEFAULT 'stroke';
-- icon_style: 'stroke' (currentColor), 'fill' (icon colored), 'accent' (background wash + border)

-- Migrate existing categories to icon system
UPDATE history_categories SET icon_key = 'note', icon_style = 'stroke' WHERE title = 'Note';
UPDATE history_categories SET icon_key = 'call', icon_style = 'stroke' WHERE title = 'Call';
UPDATE history_categories SET icon_key = 'meeting', icon_style = 'stroke' WHERE title = 'Meeting';
UPDATE history_categories SET icon_key = 'email', icon_style = 'stroke' WHERE title = 'Email';
UPDATE history_categories SET icon_key = 'customer', icon_style = 'accent', display_color = '#f97316' WHERE title = 'Customer Update';
UPDATE history_categories SET icon_key = 'sms', icon_style = 'stroke' WHERE title = 'Text Message';
UPDATE history_categories SET icon_key = 'task', icon_style = 'stroke' WHERE title = 'Task';
UPDATE history_categories SET icon_key = 'comment', icon_style = 'stroke' WHERE title = 'Comment';
UPDATE history_categories SET icon_key = 'pin', icon_style = 'accent', display_color = '#2563eb' WHERE title ILIKE '%quick%context%';

-- Migrate existing freeform task labels from tasks.labels
INSERT INTO task_labels (title, sort_order)
SELECT DISTINCT LOWER(TRIM(label)), ROW_NUMBER() OVER (ORDER BY LOWER(TRIM(label)))
FROM tasks, UNNEST(STRING_TO_ARRAY(labels, ' ')) AS label
WHERE labels IS NOT NULL AND labels != '' AND TRIM(label) != ''
ON CONFLICT (title) DO NOTHING;
