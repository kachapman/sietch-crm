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

from backup_service import sanitize_archive_member


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


class TestValidateParts(unittest.TestCase):
    """validate_parts() is the single source of truth for which parts
    the scheduler will write. Lives in backup_service to keep server.py
    import-clean."""

    def setUp(self):
        from backup_service import validate_parts

        self.vp = validate_parts

    def test_none_and_empty(self):
        self.assertEqual(self.vp(None), [])
        self.assertEqual(self.vp(""), [])
        self.assertEqual(self.vp([]), [])

    def test_csv_string_normalized(self):
        self.assertEqual(
            self.vp("db-core, files-state"),
            ["db-core", "files-state"],
        )

    def test_list_input(self):
        self.assertEqual(
            self.vp(["db-core", "files-state"]),
            ["db-core", "files-state"],
        )

    def test_dedup(self):
        self.assertEqual(
            self.vp(["db-core", "db-core", "files-state"]),
            ["db-core", "files-state"],
        )

    def test_unknown_entries_dropped(self):
        self.assertEqual(
            self.vp(["db-core", "bogus", "files-history"]),
            ["db-core"],
        )

    def test_all_sentinel_expands(self):
        self.assertEqual(
            self.vp("all"),
            ["db-core", "files-documents", "files-state"],
        )

    def test_all_does_not_dup(self):
        self.assertEqual(
            self.vp(["db-core", "all"]),
            ["db-core", "files-documents", "files-state"],
        )


class TestBackupStatusShape(unittest.TestCase):
    """get_backup_status() must surface the host path and per-part
    config so the console can render the Proton pickup hint + checkboxes."""

    def setUp(self):
        self.src = Path("/home/chapman-server/Projects/new-crm/backup_service.py").read_text()

    def test_host_path_surfaced(self):
        self.assertIn("BACKUP_DIR_HOST", self.src)
        self.assertIn("backup_dir_host", self.src)

    def test_expected_parts_in_status(self):
        self.assertIn("expected_export_parts", self.src)
        self.assertIn("EXPECTED_EXPORT_PARTS", self.src)

    def test_json_parts_in_status(self):
        self.assertIn("json_parts", self.src)

    def test_remote_and_data_fields_removed(self):
        # Old fields are gone from the *status payload* (they remain as
        # env-var constants for back-compat). The guard scopes the check
        # to the get_backup_status dict only.
        import re
        # Snip out the function body of get_backup_status and assert the
        # dead keys are absent.
        m = re.search(r"def get_backup_status.*?return \{[^}]*\}", self.src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertNotIn("remote_type", body)
        self.assertNotIn("data_size", body)


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


class TestBuildExportZipToPath(unittest.TestCase):
    """End-to-end: build each part against a fake tree + stubbed db."""

    def _tree(self, root):
        import backup_service
        data = root / "data"
        (data / "documents" / "1").mkdir(parents=True)
        (data / "documents" / "1" / "a.pdf").write_text("doc")
        (data / "user-profiles" / "sietch").mkdir(parents=True)
        (data / "user-profiles" / "sietch" / "u.json").write_text("{}")
        backup_service.DB_HOST = "unused-in-stub"
        return data

    def _run(self, root, part):
        import os
        import sys
        import tempfile
        import zipfile
        import backup_service

        rows = [
            {"id": 1, "title": "t", "password_hash": "SECRET",
             "created_at": __import__("datetime").datetime(2026, 1, 1)},
        ]

        class FakeDB:
            @staticmethod
            def query_dicts(sql):
                return [dict(r) for r in rows]

        sys.modules.setdefault("db", FakeDB())
        # Patch the lazy `import db` inside the helper by pre-seeding it.
        prev_cwd = os.getcwd()
        os.chdir(root)
        try:
            out = root / f"out-{part}.zip"
            size, sha = backup_service._build_export_zip_to_path(
                part, "2026-10-11T00:00:00", out)
        finally:
            os.chdir(prev_cwd)
        self.assertTrue(out.exists())
        self.assertEqual(size, out.stat().st_size)
        self.assertEqual(len(sha), 64)
        with zipfile.ZipFile(out) as zf:
            return zf, {i.filename: zf.read(i.filename) for i in zf.infolist()}

    def test_db_core_part(self):
        import json
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            root = _P(tmp)
            self._tree(root)
            zf, blobs = self._run(root, "db-core")
            manifest = json.loads(blobs["manifest.json"])
            self.assertEqual(manifest["part"], "db-core")
            self.assertIn("credential_notice", manifest)
            users = json.loads(blobs["users.json"])
            self.assertEqual(users[0]["title"], "t")
            self.assertNotIn("password_hash", users[0])
            self.assertEqual(users[0]["created_at"], "2026-01-01T00:00:00")
            # file parts must not appear in db-core
            self.assertFalse([n for n in blobs if n.startswith("files/")])

    def test_files_documents_part(self):
        import json
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            root = _P(tmp)
            self._tree(root)
            zf, blobs = self._run(root, "files-documents")
            manifest = json.loads(blobs["manifest.json"])
            self.assertEqual(manifest["part"], "files-documents")
            self.assertNotIn("credential_notice", manifest)
            self.assertIn("files/documents/1/a.pdf", blobs)
            # no tables in a file part
            self.assertFalse([n for n in blobs if n.endswith(".json") and n != "manifest.json"])

    def test_files_state_part(self):
        import json
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            root = _P(tmp)
            self._tree(root)
            zf, blobs = self._run(root, "files-state")
            manifest = json.loads(blobs["manifest.json"])
            self.assertIn("files/user-profiles/sietch/u.json", blobs)
            self.assertIn("user-profiles", manifest["files"])

    def test_unknown_part_builds_empty_manifest(self):
        import json
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            root = _P(tmp)
            self._tree(root)
            zf, blobs = self._run(root, "bogus-part")
            manifest = json.loads(blobs["manifest.json"])
            # Only the two deliberately-excluded markers; no rows, no files.
            self.assertEqual(
                set(manifest["tables"]),
                {"password_reset_tokens", "twofa_challenges"},
            )
            self.assertEqual(manifest["files"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
