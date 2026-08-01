-- =============================================================================
-- Migration 003: mail_outgoing.account_id -> ON DELETE CASCADE
-- Fixes: "update or delete on table mail_accounts violates foreign key
-- constraint mail_outgoing_account_id_fkey" when deleting a mail account.
-- Consistent with mail_messages / mail_account_access (already CASCADE).
-- =============================================================================

ALTER TABLE mail_outgoing
    DROP CONSTRAINT IF EXISTS mail_outgoing_account_id_fkey;

ALTER TABLE mail_outgoing
    ADD CONSTRAINT mail_outgoing_account_id_fkey
    FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE;
