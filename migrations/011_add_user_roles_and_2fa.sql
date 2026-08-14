-- Migration 011: Add user roles, 2FA support
-- Run after 010_add_calendar.sql

-- ── Role column ────────────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE users ADD CONSTRAINT users_role_check
  CHECK (role IN ('admin','manager','member','limited'));

-- Backfill from existing is_admin
UPDATE users SET role = 'admin' WHERE is_admin = TRUE AND role = 'member';

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- ── 2FA method preference ──────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS twofa_method TEXT
  CHECK (twofa_method IN ('email','telegram'));

-- ── 2FA challenge codes (short-lived, used during login and setup) ─────────
CREATE TABLE IF NOT EXISTS twofa_challenges (
    token TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('email','telegram')),
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ── Deprecate old user_roles table ─────────────────────────────────────────
COMMENT ON TABLE user_roles IS 'DEPRECATED: replaced by users.role column. Kept for compatibility; not used by application code.';
