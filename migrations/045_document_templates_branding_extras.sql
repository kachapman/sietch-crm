-- Migration 045: document_templates branding extras (website, license, signer title)
--
-- Adds three optional columns to document_templates so the Invoice and the
-- Certificate of Completion can show the company website, the contractor
-- licence number, and the signer's role/title next to the signature.
--
-- Numbering note: the highest migration on this branch is 042. The separate
-- `fix-mail-attachments` branch uses 043 and 044, so this one deliberately
-- starts at 045 to avoid a filename collision when the branches merge.
--
-- All three are additive with empty-string defaults, so existing template
-- rows keep working and print no extra labels. Empty values are omitted
-- from the rendered PDFs rather than printed as blank fields.

ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS company_website  TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS company_license  TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS signature_title   TEXT NOT NULL DEFAULT '';

-- Notes:
--   * company_website  -> shown on the invoice + COC company block
--   * company_license  -> shown on the invoice + COC company block
--                         (prefixed with "License " unless it already starts
--                         with "lic" / "#")
--   * signature_title  -> shown next to the signer name on the COC
--                         signature block and on the invoice signature block
--   * is_default stays a partial unique index; these columns do not affect it.
