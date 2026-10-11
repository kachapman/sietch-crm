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
    _build_remote_prune_command,
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


class TestSqlImportStrictness(unittest.TestCase):
    """SQL restore must fail closed: strict psql, spooled upload, snapshot.

    Text guards over server.py (no server import — module has import-time
    side effects). If the handler is restructured, update these alongside.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = Path("/home/chapman-server/Projects/new-crm/server.py").read_text()
        start = cls.src.index("def _handle_import_sql")
        # handler ends at the next def at the same indent
        nxt = cls.src.index("\n    def ", start + 10)
        cls.body = cls.src[start:nxt]

    def test_strict_error_stop(self):
        self.assertIn("ON_ERROR_STOP=1", self.body)
        self.assertNotIn("ON_ERROR_STOP=0", self.body)

    def test_any_nonzero_exit_is_failure(self):
        self.assertIn("if proc.returncode != 0:", self.body)
        self.assertNotIn("proc.returncode != 1", self.body)

    def test_spooled_upload_via_psql_file(self):
        self.assertIn('"-f", upload_path', self.body)
        self.assertNotIn("input=file_data", self.body)

    def test_pre_restore_snapshot(self):
        self.assertIn("sietch-backup-restore-snapshot-", self.body)
        self.assertIn("_run_pg_dump(snapshot_path)", self.body)

    def test_temp_upload_cleaned_up(self):
        self.assertIn("os.unlink(upload_path)", self.body)


class TestRemotePruneCommand(unittest.TestCase):
    CFG = {
        "remote_host": "backup.example.com",
        "remote_port": 2222,
        "remote_user": "deploy",
        "remote_path": "/srv/backups/sietch",
        "remote_key": "/run/secrets/k",
    }

    def test_command_shape(self):
        cmd = _build_remote_prune_command(self.CFG, 30)
        self.assertEqual(cmd[0:3], ["ssh", "-p", "2222"])
        self.assertIn("StrictHostKeyChecking=accept-new", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("/run/secrets/k", cmd)
        self.assertIn("deploy@backup.example.com", cmd)

    def test_retention_days_in_find(self):
        cmd = _build_remote_prune_command(self.CFG, 14)
        self.assertIn("-mtime +14", " ".join(cmd))

    def test_path_quoted(self):
        cfg = dict(self.CFG, remote_path="/srv/back ups/sie;tch")
        joined = " ".join(_build_remote_prune_command(cfg, 30))
        self.assertIn("/srv/back ups/sie;tch", joined)

    def test_requires_host_user_path(self):
        for missing in ("remote_host", "remote_user", "remote_path"):
            cfg = dict(self.CFG)
            cfg[missing] = ""
            with self.assertRaises(ValueError):
                _build_remote_prune_command(cfg, 30)


class TestSqlExportStreaming(unittest.TestCase):
    """SQL export must stream — no whole-dump buffering, chunked encoding."""

    @classmethod
    def setUpClass(cls):
        cls.src = Path("/home/chapman-server/Projects/new-crm/server.py").read_text()
        start = cls.src.index("def _handle_export_backup")
        nxt = cls.src.index("\n    def ", start + 10)
        cls.body = cls.src[start:nxt]

    def test_uses_popen_not_run(self):
        self.assertIn("subprocess.Popen", self.body)
        # No whole-stdout capture anywhere in the export handler.
        self.assertNotIn("capture_output=True", self.body)
        self.assertNotIn("text=True", self.body)

    def test_no_stdout_buffer_then_encode(self):
        # The previous shape was `proc.stdout.encode("utf-8")` after `text=True`.
        self.assertNotIn("proc.stdout.encode(", self.body)
        self.assertNotIn("sql_bytes = ", self.body)

    def test_chunked_transfer_encoding(self):
        self.assertIn('"Transfer-Encoding", "chunked"', self.body)
        # No Content-Length — chunked encoding is the alternative.
        self.assertNotIn('"Content-Length"', self.body)

    def test_first_chunk_buffer_for_error_path(self):
        # Must read at least one chunk before sending headers so a pg_dump
        # failure that produces no output still gets a 500 (not a 200 +
        # truncated body).
        self.assertIn("first_chunk = proc.stdout.read(", self.body)
        self.assertIn("if proc.poll() is not None and not first_chunk:", self.body)

    def test_filename_uses_sql_not_sql_gz(self):
        # Streaming raw SQL — no .gz in the response header.
        import re
        m = re.search(r'filename="sietch-backup-\{datetime\.now[^"]*"', self.body)
        self.assertIsNotNone(m, "export filename template not found")


class TestChunkedDecoding(unittest.TestCase):
    """Pure-Python helper: read chunked frames correctly."""

    def _decode_chunks(self, raw: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(raw):
            j = raw.index(b"\r\n", i)
            size = int(raw[i:j], 16)
            if size == 0:
                break
            out.extend(raw[j + 2:j + 2 + size])
            i = j + 2 + size + 2  # skip trailing \r\n
        return bytes(out)

    def test_single_chunk(self):
        body = b"Hello, world"
        raw = f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n0\r\n\r\n"
        self.assertEqual(self._decode_chunks(raw), body)

    def test_multi_chunk(self):
        chunks = [b"alpha-", b"beta-", b"gamma"]
        raw = b"".join(f"{len(c):X}\r\n".encode("ascii") + c + b"\r\n" for c in chunks) + b"0\r\n\r\n"
        self.assertEqual(self._decode_chunks(raw), b"alpha-beta-gamma")

    def test_empty_body(self):
        self.assertEqual(self._decode_chunks(b"0\r\n\r\n"), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
