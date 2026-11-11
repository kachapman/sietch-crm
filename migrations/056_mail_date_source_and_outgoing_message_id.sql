-- 056_mail_date_source_and_outgoing_message_id.sql
--
-- Two nullable columns (2026-11-11), both additive and idempotent.
--
-- 1. mail_messages.date_source — HOW date_received was resolved.
--
--    Live bug: SMTP sends from Sietch carried no `Date` header, so Bluehost
--    stored them that way. imap_tools then reports the 1900-01-01 sentinel,
--    and the scanner's 90-day retention guard treated brand-new mail as
--    ancient: it skipped the import and advanced the watermark. The scanner
--    now resolves a real timestamp through a documented fallback chain and
--    records which step produced it:
--
--      'header'       a real Date header / imap_tools date
--      'internaldate' IMAP INTERNALDATE (RFC 3501)
--      'received'     the date inside a Received: header
--      'import'       import time (UTC) — nothing else was usable
--
--    NULL on every pre-existing row (their provenance was never recorded).
--
-- 2. mail_outgoing.message_id — the Message-ID actually placed on the sent
--    message, so a sent row can be matched to the provider's Sent copy and to
--    the recipient's copy. NULL when the transport exposes none (Graph mints
--    its own Message-ID server-side and returns no identifier).
--
-- No backfill, no UPDATE, no DELETE: existing data is untouched.

ALTER TABLE mail_messages ADD COLUMN IF NOT EXISTS date_source TEXT;

ALTER TABLE mail_outgoing ADD COLUMN IF NOT EXISTS message_id TEXT;
