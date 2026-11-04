-- Migration 045: document_templates branding extras (website, license, signer title)
--
-- Adds optional columns to document_templates so the Invoice and the
-- Certificate of Completion can show the company website, the contractor
-- licence number, and the signer's role/title next to the signature, and so
-- the template can switch the certificate's signature lines off.
--
-- Numbering note: the highest migration on this branch is 042. The separate
-- `fix-mail-attachments` branch uses 043 and 044, so this one deliberately
-- starts at 045 to avoid a filename collision when the branches merge.
--
-- All of these are additive, so existing template rows keep working. The
-- text columns default to '' and are omitted from the rendered PDFs rather
-- than printed as blank fields; the boolean defaults to TRUE so nothing
-- changes unless the switch is turned off.

ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS company_website  TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS company_license  TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS signature_title   TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS show_signature_lines BOOLEAN NOT NULL DEFAULT TRUE;

-- Notes:
--   * company_website  -> shown on the invoice + COC company block
--   * company_license  -> shown on the invoice + COC company block
--                         (prefixed with "License " unless it already
--                         contains "lic" or starts with "#")
--   * signature_title  -> shown next to the signer name on the COC
--                         signature block and on the invoice signature block
--   * show_signature_lines -> FALSE leaves the whole SIGNATURES section out
--                         of the certificate (heading included) and the
--                         signature block out of the classic certificate.
--                         v28.2 port of Sherwood's "Include signature lines".
--   * is_default stays a partial unique index; these columns do not affect it.
