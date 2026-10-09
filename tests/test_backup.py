#!/usr/bin/env python3
"""Unit tests for backup_service archive builders (audit fixes).

Verifies the data tar excludes the backup dir when nested inside data/
(recursive-nesting fix), per-bundle splitting, and the zip-slip guard.
Run as:  python3 tests/test_backup.py
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

from backup_service import (
    DATA_BUNDLES,
    _build_data_tar_command,
    _run_data_bundles,
    sanitize_archive_member,
)


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


class TestExportParts(unittest.TestCase):
    """Split-export part mapping guards (parsed via ast, no server import)."""

    def _load_assign(self, name):
        import ast

        src = Path("/home/chapman-server/Projects/new-crm/server.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            else:
                continue
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return value
        self.fail(f"{name} not found in server.py")

    def test_expected_parts(self):
        from backup_service import EXPECTED_EXPORT_PARTS
        self.assertEqual(set(EXPECTED_EXPORT_PARTS),
                         {"db-core", "files-documents", "files-state"})

    def test_part_dirs_cover_real_layout_and_exclude_blobs(self):
        mapping = {}
        for key, val in zip(
            [k.value for k in self._load_assign("_EXPORT_PART_FILES").keys],
            self._load_assign("_EXPORT_PART_FILES").values,
        ):
            mapping[key] = [sub for sub, _ in
                            [(e.elts[0].value, e.elts[1].value) for e in val.elts]]
        covered = {d for dirs in mapping.values() for d in dirs}
        for d in ("documents", "photos", "avatars", "user-profiles",
                  "bot-customers", "logos", "branding"):
            self.assertIn(d, covered, f"{d} must be in some part")
        for d in ("history-attachments", "backups"):
            self.assertNotIn(d, covered, f"{d} must never be browser-served")


class TestBuildDataTarCommand(unittest.TestCase):
    def test_excludes_nested_backup_dir(self):
        cmd = _build_data_tar_command(
            Path("data/backups/sietch-data-x.tar.gz"),
            Path("data"),
            Path("data/backups"),
            ["documents"],
        )
        self.assertIn("--exclude=data/backups", cmd)

    def test_no_exclude_when_backup_dir_outside(self):
        cmd = _build_data_tar_command(
            Path("/backups/sietch-data-x.tar.gz"),
            Path("data"),
            Path("/backups"),
            ["documents"],
        )
        self.assertFalse([a for a in cmd if a.startswith("--exclude=")])

    def test_extra_sources_appended_only_when_present(self):
        cmd = _build_data_tar_command(
            Path("out.tar.gz"),
            Path("data"),
            Path("/backups"),
            ["documents"],
            extra_sources=[("scanner", "definitely-not-a-real-dir-xyz")],
        )
        self.assertNotIn("scanner", cmd)

    def test_archive_root_present(self):
        cmd = _build_data_tar_command(
            Path("out.tar.gz"), Path("data"), Path("data/backups"),
            ["documents", "photos"],
        )
        self.assertEqual(cmd[0:3], ["tar", "czf", "out.tar.gz"])
        self.assertIn("data/documents", cmd)
        self.assertIn("data/photos", cmd)

    def test_excludes_backup_dir_with_subdir_members(self):
        cmd = _build_data_tar_command(
            Path("data/backups/out.tar.gz"), Path("data"),
            Path("data/backups"), ["documents"],
        )
        self.assertIn("--exclude=data/backups", cmd)
        self.assertIn("data/documents", cmd)


class TestDataBundles(unittest.TestCase):
    def test_bundle_names(self):
        self.assertEqual([b for b, _ in DATA_BUNDLES],
                         ["docs", "history", "state"])

    def test_bundles_cover_expected_dirs_and_skip_backups(self):
        covered = {d for _, dirs in DATA_BUNDLES for d in dirs}
        for d in ("documents", "photos", "history-attachments", "avatars",
                  "user-profiles", "bot-customers", "logos", "branding"):
            self.assertIn(d, covered, f"{d} must be in some bundle")
        self.assertNotIn("backups", covered)

    @unittest.skipUnless(shutil.which("tar"), "tar binary required")
    def test_run_data_bundles_end_to_end(self):
        import os
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Isolate CWD so the repo's real scanner/data isn't pulled in.
            prev_cwd = os.getcwd()
            os.chdir(root)
            try:
                self._run_bundles_in(root)
            finally:
                os.chdir(prev_cwd)

    def _run_bundles_in(self, root):
        import tarfile

        data = root / "data"
        (data / "documents" / "1").mkdir(parents=True)
        (data / "documents" / "1" / "a.pdf").write_text("doc")
        (data / "user-profiles" / "sietch").mkdir(parents=True)
        (data / "user-profiles" / "sietch" / "u.json").write_text("{}")
        (data / "backups").mkdir()
        (data / "backups" / "old.tar.gz").write_text("stale")
        outdir = root / "out"
        outdir.mkdir()

        done = _run_data_bundles(outdir, "ts", data_dir=data)
        names = sorted(p.name for _, p in done)
        self.assertEqual(names, [
            "sietch-data-docs-ts.tar.gz",
            "sietch-data-state-ts.tar.gz",
        ])
        # history bundle skipped (no history-attachments dir present)
        with tarfile.open(outdir / "sietch-data-docs-ts.tar.gz") as tf:
            members = tf.getnames()
        self.assertIn("data/documents/1/a.pdf", members)
        self.assertFalse([m for m in members if m.startswith("data/backups")])
        with tarfile.open(outdir / "sietch-data-state-ts.tar.gz") as tf:
            members = tf.getnames()
        self.assertIn("data/user-profiles/sietch/u.json", members)

        # backup_dir nested inside the tree (the production default):
        # the --exclude keeps prior artifacts AND the outputs being
        # written out of the new archives.
        nested_out = data / "backups"
        done3 = _run_data_bundles(nested_out, "ts3", data_dir=data)
        self.assertTrue(all(p.exists() for _, p in done3))
        with tarfile.open(nested_out / "sietch-data-docs-ts3.tar.gz") as tf:
            members = tf.getnames()
        self.assertIn("data/documents/1/a.pdf", members)
        self.assertFalse([m for m in members if "backups" in m])


if __name__ == "__main__":
    unittest.main(verbosity=2)
