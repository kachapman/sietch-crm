-- 048_mail_dates_utc.sql
-- Store mail timestamps as tz-aware UTC (live bug 2026-11-09: an email
-- received 1:20 PM EST displayed as "2:20:03 AM" in the Sietch email view).
--
-- ROOT CAUSE (storage half)
--   mail_messages.date_received was `TIMESTAMP` (no time zone). The scanner
--   wrote `msg.date.isoformat()` — which still carries the sender's offset —
--   into it, so Postgres converted the value using the *session* TimeZone.
--   With TimeZone=UTC the stored wall clock is UTC (correct); with any other
--   session timezone every new row becomes local wall clock while every
--   reader keeps assuming UTC. That is why the error keeps coming back.
--
-- WHAT THIS DOES
--   Retypes the column to `timestamptz`. Existing naive values are declared
--   to be UTC wall clock (`USING date_received AT TIME ZONE 'UTC'`), which
--   preserves the instant for rows written under the production UTC session
--   — it does NOT shift them by another offset.
--
--   Rows whose original `raw_headers` Date: header proves a different
--   instant are corrected separately by
--   `scripts/backfill_mail_dates.py` (dry-run by default; run only with
--   Ken's approval).
--
-- IDEMPOTENT: the DO block checks the current column type, so running this
-- again (or the mirrored inline DDL in server.py on every boot) is a no-op.
-- Additive/compatible: no column is dropped, no row is deleted.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'mail_messages'
      AND column_name = 'date_received'
      AND data_type = 'timestamp without time zone'
  ) THEN
    ALTER TABLE mail_messages
      ALTER COLUMN date_received TYPE timestamptz
      USING date_received AT TIME ZONE 'UTC';
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_mail_date ON mail_messages(date_received DESC);
