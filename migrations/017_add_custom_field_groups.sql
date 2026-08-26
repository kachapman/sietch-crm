-- Migration 017: Custom field groups for project fields preview layout
-- Admins create groups with labels; custom fields and standard fields are assigned to groups.
-- Groups appear as collapsible sections in the project preview modal.

CREATE TABLE custom_field_groups (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    is_collapsed_default BOOLEAN DEFAULT FALSE,
    standard_fields JSONB DEFAULT '[]',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Add group_id FK to custom_field_definitions (nullable — ungrouped fields have NULL)
ALTER TABLE custom_field_definitions ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES custom_field_groups(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_custom_field_definitions_group ON custom_field_definitions(group_id);

-- Add "Action" system history category for field change tracking
INSERT INTO history_categories (title, display_color, is_system, sort_order, icon_key, icon_style)
VALUES ('Action', '#6c757d', TRUE, 15, 'pencil', 'accent')
ON CONFLICT (title) DO NOTHING;

-- Add content_hash to project_photos for deduplication
ALTER TABLE project_photos ADD COLUMN IF NOT EXISTS content_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_project_photos_hash ON project_photos(content_hash);
