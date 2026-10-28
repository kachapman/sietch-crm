-- Migration 039: photo report template wordmark default — no placeholder.
--
-- The column default was '["Company"]', which injected the literal word into
-- any template row created without text_logo_lines (and, before the write-path
-- fix, into every saved template with an empty wordmark field). The reports
-- then faithfully printed "Company" next to the logo.
--
-- Column default only — NO data changes. Existing template rows are left as-is
-- (the user deletes/recreates the templates).
ALTER TABLE photo_report_templates
    ALTER COLUMN text_logo_lines SET DEFAULT '[]'::jsonb;
