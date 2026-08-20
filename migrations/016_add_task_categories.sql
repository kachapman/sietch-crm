-- Migration 016: Task category_id + missing history categories + task labels

-- Add task category column
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES history_categories(id);

-- Add missing history categories from OnlyOffice CRM
INSERT INTO history_categories (title, icon_key, icon_style, is_system, sort_order, display_color)
VALUES
  ('Follow-Up', 'clock', 'stroke', TRUE, 9, NULL),
  ('Estimate', 'document', 'stroke', TRUE, 10, NULL),
  ('Inspection', 'building', 'stroke', TRUE, 11, NULL),
  ('Expression of gratitude', 'star', 'stroke', TRUE, 12, NULL),
  ('Opportunity', 'alert', 'stroke', TRUE, 13, NULL),
  ('Stage Estimate', 'document', 'fill', TRUE, 14, NULL)
ON CONFLICT (title) DO NOTHING;

-- Populate task_labels with OnlyOffice task categories
INSERT INTO task_labels (title, sort_order)
VALUES
  ('follow-up', 1), ('estimate', 2), ('inspection', 3),
  ('phone call', 4), ('email', 5), ('appointment', 6),
  ('note', 7), ('expression of gratitude', 8),
  ('opportunity', 9), ('stage estimate', 10)
ON CONFLICT (title) DO NOTHING;

-- Set default category to "Follow-Up" for existing tasks with no category
UPDATE tasks SET category_id = (SELECT id FROM history_categories WHERE title = 'Follow-Up' LIMIT 1)
WHERE category_id IS NULL;
