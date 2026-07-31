ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS display_name TEXT;
UPDATE mail_accounts SET display_name = email WHERE display_name IS NULL;
