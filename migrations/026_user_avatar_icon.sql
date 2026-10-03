-- Migration 026: User avatar icon picker
--
-- Adds a column for storing a chosen Tabler.io icon name (e.g. "cat", "crown")
-- for users who pick an icon-style avatar instead of using their initials.
-- NULL = use initials (first + last name, fallback to email).
--
-- Also includes a one-shot cleanup: clear broken avatar_url from contacts that
-- have a working avatar_local_path, so the UI never accidentally renders the
-- soon-to-die OnlyOffice remote URL.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS avatar_icon TEXT;

-- One-shot cleanup: clear avatar_url on contacts that have avatar_local_path
UPDATE contacts
SET avatar_url = NULL
WHERE avatar_local_path IS NOT NULL
  AND avatar_url IS NOT NULL;