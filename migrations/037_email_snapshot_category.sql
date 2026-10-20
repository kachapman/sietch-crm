-- Migration 037: Recategorize linked/imported email snapshots to "Email"
--
-- Bug: EMAIL_HISTORY_CATEGORY_ID was 16 ("Customer Update" after the taxonomy
-- reseed), so every manually linked email and every OnlyOffice-imported email
-- filed its history event as a customer update. That mislabelled the project
-- timeline AND leaked raw email dumps into the Telegram customer-updates feed
-- (which filters on category 16). The constant is now 13 ("Email").
--
-- email_snapshot rows are machine-written JSON and self-identifying, so this
-- targeted UPDATE only touches them. New rows are written correctly already.
UPDATE history_events
SET category_id = 13
WHERE category_id = 16
  AND content LIKE '%"type": "email_snapshot"%';
