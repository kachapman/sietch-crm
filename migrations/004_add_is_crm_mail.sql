ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS is_crm_mail BOOLEAN DEFAULT FALSE;
-- Mark existing OAuth accounts as CRM Mail accounts
UPDATE mail_accounts SET is_crm_mail = TRUE WHERE oauth_provider IS NOT NULL;
