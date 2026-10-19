-- Migration 036: Mail attachments — OnlyOffice-first source tracking
--
-- Bug fix 2026-10-19: scanner previously stored mail_attachments metadata only,
-- then the docs editor tried to fetch the file live from IMAP at view time.
-- IMAP frequently 404s (auto-archive, mailbox cleanup, UIDVALIDITY drift),
-- so the editor shows "Download failed" for any email that's no longer on
-- the live IMAP server.
--
-- Fix: track the OnlyOffice source for each mail attachment. The canonical
-- archive lives in OnlyOffice's DB and is fetchable via the bot-accessible
-- /Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={id}
-- endpoint (works for years-old emails, independent of the email module).
--
-- The serve endpoint now tries:
--   1. local stored_path (already-on-disk mirror)
--   2. OnlyOffice via crm_file_id (filehandler.ashx)  ← NEW primary path
--   3. live IMAP (existing fallback for brand-new mail not yet archived)
--
-- The scanner will populate these columns via a delayed OnlyOffice lookup
-- (5-30 min after first sight) so mail that arrives via the live scanner
-- still gets linked to its OnlyOffice archive once the mail aggregator
-- catches up.

ALTER TABLE mail_attachments
  ADD COLUMN IF NOT EXISTS crm_file_id        BIGINT,
  ADD COLUMN IF NOT EXISTS crm_message_url    TEXT,
  ADD COLUMN IF NOT EXISTS fetch_source       TEXT;

-- Partial index: most rows will have NULL crm_file_id (legacy scanner metadata
-- only). The index is small and only covers rows that are eligible for the
-- OnlyOffice-first serve path.
CREATE INDEX IF NOT EXISTS idx_mail_attachments_crm_file_id
  ON mail_attachments(crm_file_id)
  WHERE crm_file_id IS NOT NULL;

-- Source values: 'onlyoffice' (filehandler.ashx), 'imap' (live fallback),
-- 'local' (mirrored to disk), or NULL (legacy, unlinked).
-- No CHECK constraint — would break if we add new sources.