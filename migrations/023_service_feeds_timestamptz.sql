-- Migration 023: Convert service_feeds timestamps to TIMESTAMP WITH TIME ZONE
--
-- Bug: last_polled_at was stored as TIMESTAMP WITHOUT TIME ZONE. Postgres wrote
-- the current server-local time (America/New_York) into the column. When the
-- dashboard API returned an ISO string, the user's browser parsed it as UTC
-- (per JS Date spec for ISO strings without offset), then converted to the
-- user's local TZ for display. For an Eastern user this happened to look
-- correct (server stored Eastern, browser interpreted as UTC → same Eastern).
-- For a Pacific user it was 3 hours off.
--
-- Fix: switch all three timestamp columns to TIMESTAMP WITH TIME ZONE. Postgres
-- always normalizes timestamptz values to UTC under the hood, regardless of the
-- server's session TZ. The existing data is re-interpreted as UTC (which is
-- what was stored, just without the TZ marker). psycopg2 then emits ISO
-- strings with the +00:00 / Z suffix, browsers correctly parse as UTC, and
-- every user's toLocaleString() shows the right local time.

ALTER TABLE service_feeds
    ALTER COLUMN last_polled_at TYPE TIMESTAMPTZ
    USING last_polled_at AT TIME ZONE 'UTC';

ALTER TABLE service_feeds
    ALTER COLUMN created_at TYPE TIMESTAMPTZ
    USING created_at AT TIME ZONE 'UTC';

ALTER TABLE service_feeds
    ALTER COLUMN updated_at TYPE TIMESTAMPTZ
    USING updated_at AT TIME ZONE 'UTC';