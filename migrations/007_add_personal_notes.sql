-- 007: Personal notes — PostgreSQL storage + entity linking
-- Applied automatically at server startup via IF NOT EXISTS blocks in server.py;
-- this file documents the intended schema for fresh installs / reference.

CREATE TABLE IF NOT EXISTS personal_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner INTEGER NOT NULL REFERENCES users(id),
    title VARCHAR(200) NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    content_prev TEXT,
    content_prev2 TEXT,
    content_prev_at TIMESTAMPTZ,
    content_prev2_at TIMESTAMPTZ,
    color VARCHAR(32) NOT NULL DEFAULT '',
    pinned BOOLEAN NOT NULL DEFAULT false,
    archived BOOLEAN NOT NULL DEFAULT false,
    archived_at TIMESTAMPTZ,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_personal_notes_owner ON personal_notes(owner, archived, sort_order);
CREATE INDEX IF NOT EXISTS ix_personal_notes_fts ON personal_notes USING gin(
    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,''))
);

CREATE TABLE IF NOT EXISTS personal_note_links (
    note_id UUID NOT NULL REFERENCES personal_notes(id) ON DELETE CASCADE,
    entity_type VARCHAR NOT NULL,
    entity_id VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (note_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS ix_personal_note_links_entity ON personal_note_links(entity_type, entity_id);
