-- Reports: enforce a single default template per kind
--
-- The photo_report_templates and document_templates tables each have an
-- is_default boolean that should be TRUE on at most one row. The handler
-- logic ("UPDATE all SET is_default = FALSE, then set the chosen one")
-- covers the common case, but two concurrent saves can both pass the
-- clear step and both write is_default = TRUE, leaving the table with
-- two defaults. The 'Cannot delete the default template' guard then
-- refuses to delete EITHER one, because both are flagged.
--
-- A partial unique index makes the constraint a database invariant
-- rather than a handler-level convention.

CREATE UNIQUE INDEX IF NOT EXISTS idx_photo_report_templates_one_default
    ON photo_report_templates (is_default)
    WHERE is_default = TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_templates_one_default
    ON document_templates (is_default)
    WHERE is_default = TRUE;
