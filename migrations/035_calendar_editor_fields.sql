-- Migration 035: Calendar editor fields
-- Local event editing needs URL, free/busy, attendees, and the wall-time
-- timezone; ICS sync also stores them so DB rows match feed content.

ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS transparency TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS attendees TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS timezone TEXT;
