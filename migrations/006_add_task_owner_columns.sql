-- 006: Sietch Tasks revamp — private tasks + checklist/todo fields
-- Applied automatically at server startup via IF NOT EXISTS blocks in server.py;
-- this file documents the intended schema for fresh installs / reference.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES users(id);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS items JSONB DEFAULT '[]'::jsonb;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS labels TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS repeat TEXT DEFAULT 'none';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS color TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS pinned BOOLEAN DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS remind_at TIMESTAMP;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminder_fired_at TIMESTAMP;

-- All tasks are now private: every row is owned by exactly one user.
UPDATE tasks SET owner_user_id = COALESCE(responsible_user_id, created_by)
WHERE owner_user_id IS NULL AND (responsible_user_id IS NOT NULL OR created_by IS NOT NULL);
UPDATE tasks SET owner_user_id = (SELECT id FROM users WHERE is_admin = TRUE ORDER BY id LIMIT 1)
WHERE owner_user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_reminders ON tasks(owner_user_id, remind_at) WHERE remind_at IS NOT NULL;
