#!/usr/bin/env python3
"""Unit tests for backup_service tar-command builder (audit fix items 1+4).

Verifies the data archive excludes the backup dir when nested inside
data/ (recursive-nesting fix) and includes scanner state as an extra
source. Run as:  python3 tests/test_backup.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

from backup_service import _build_data_tar_command, sanitize_archive_member


class TestSanitizeArchiveMember(unittest.TestCase):
    def test_allows_plain_relative(self):
        self.assertEqual(
            sanitize_archive_member("files/documents/2383/a.pdf"),
            "documents/2383/a.pdf",
        )

    def test_rejects_dotdot(self):
        self.assertIsNone(sanitize_archive_member("files/../../etc/evil"))
        self.assertIsNone(sanitize_archive_member("files/a/../../evil"))
        self.assertIsNone(sanitize_archive_member("files/.."))

    def test_rejects_absolute_and_drive(self):
        self.assertIsNone(sanitize_archive_member("files//etc/passwd"))
        self.assertIsNone(sanitize_archive_member("files/C:/evil"))

    def test_rejects_wrong_prefix_and_empty(self):
        self.assertIsNone(sanitize_archive_member("other/a.pdf"))
        self.assertIsNone(sanitize_archive_member("files/"))
        self.assertIsNone(sanitize_archive_member(""))


class TestExportTableCoverage(unittest.TestCase):
    """_EXPORT_TABLES in server.py must cover every real table.

    Parsed via ast (no server.py import — module has import-time side
    effects). sessions is exported empty by design; the two ephemeral
    secret tables are deliberately excluded (see _EXPORT_TABLES).
    """

    EXPECTED_TABLES = {
        "users", "user_roles", "stages", "system_settings", "contacts",
        "contact_emails", "contact_phones", "contact_addresses",
        "custom_field_definitions", "custom_field_options",
        "custom_field_groups", "opportunities",
        "opportunity_custom_field_values", "tag_definitions",
        "opportunity_tags", "history_categories", "history_events",
        "history_replies", "history_attachments", "history_notify_users",
        "history_reply_notify_users", "tasks", "task_labels",
        "notifications", "notification_preferences",
        "telegram_notification_log", "bot_config", "bot_activity_log",
        "project_photo_folders", "project_photos", "document_folders",
        "project_documents", "document_activity", "photo_exif_cache",
        "photo_report_templates", "document_templates", "service_feeds",
        "mail_accounts", "mail_messages", "mail_deal_links",
        "mail_flag_queue", "mail_tags", "mail_tag_assignments",
        "mail_templates", "mail_account_access", "mail_outgoing",
        "mail_folders", "mail_attachments", "mail_contractors",
        "mail_classification_rules", "mail_contacts", "sync_watermarks",
        "opportunity_changes", "sync_errors", "email_classifications",
        "classifier_training_data", "mail_contacts", "user_profiles",
        "branding", "minimized_modal_state", "sessions",
        "personal_notes", "personal_note_links", "calendar_sources",
        "calendar_events", "ics_feed_cache", "automation_rules",
        "automation_logs", "time_entries",
    }
    DELIBERATELY_EXCLUDED = {"password_reset_tokens", "twofa_challenges"}

    def _load_export_tables(self):
        import ast

        src = Path("/home/chapman-server/Projects/new-crm/server.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_EXPORT_TABLES":
                        return {
                            elt.elts[0].value: (elt.elts[1].value if not isinstance(elt.elts[1], ast.Constant) or elt.elts[1].value is not None else None)
                            for elt in node.value.elts
                        }
        self.fail("_EXPORT_TABLES not found in server.py")

    def test_all_tables_covered_or_deliberately_excluded(self):
        tables = self._load_export_tables()
        exported = {k for k, v in tables.items() if v is not None}
        missing = self.EXPECTED_TABLES - exported - {"sessions"}
        self.assertEqual(missing, set(), f"tables missing from export: {sorted(missing)}")
        self.assertIn("sessions", exported)  # present but LIMIT 0 by design
        for t in self.DELIBERATELY_EXCLUDED:
            self.assertIn(t, tables)
            self.assertIsNone(tables[t])

    def test_sensitive_columns_stripped(self):
        import ast

        src = Path("/home/chapman-server/Projects/new-crm/server.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_SENSITIVE_COLUMNS":
                        cols = {e.value for e in node.value.elts}
                        for c in (
                            "password_hash", "oauth_access_token",
                            "oauth_refresh_token", "oauth_graph_access_token",
                            "oauth_graph_refresh_token", "webhook_secret",
                        ):
                            self.assertIn(c, cols, f"{c} must stay stripped")
                        return
        self.fail("_SENSITIVE_COLUMNS not found in server.py")


class TestBuildDataTarCommand(unittest.TestCase):
    def test_excludes_nested_backup_dir(self):
        cmd = _build_data_tar_command(
            Path("data/backups/sietch-data-x.tar.gz"),
            Path("data"),
            Path("data/backups"),
        )
        self.assertIn("--exclude=data/backups", cmd)

    def test_no_exclude_when_backup_dir_outside(self):
        cmd = _build_data_tar_command(
            Path("/backups/sietch-data-x.tar.gz"),
            Path("data"),
            Path("/backups"),
        )
        self.assertFalse([a for a in cmd if a.startswith("--exclude=")])

    def test_extra_sources_appended_only_when_present(self):
        cmd = _build_data_tar_command(
            Path("out.tar.gz"),
            Path("data"),
            Path("/backups"),
            extra_sources=[("scanner", "definitely-not-a-real-dir-xyz")],
        )
        self.assertNotIn("scanner", cmd)

    def test_archive_root_present(self):
        cmd = _build_data_tar_command(
            Path("out.tar.gz"), Path("data"), Path("data/backups")
        )
        self.assertEqual(cmd[0:3], ["tar", "czf", "out.tar.gz"])
        self.assertIn("data", cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
