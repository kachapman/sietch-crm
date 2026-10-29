#!/usr/bin/env python3
"""Unit tests: the backup must be memory-safe and land on its own volume.

THE OUTAGE (2026-10-29)
-----------------------
Starting the backup scheduler for the first time in production took the
site down. The export loaded whole tables into memory:

    rows = db.query_dicts(sql)           # every row as a dict
    clean_rows = [{...} for row in rows]  # a second copy
    json.dumps(clean_rows)                # a third copy (one huge string)
    zf.writestr(name, that_string)

Measured on production against the real `notifications` table (1,439,331
rows, 822MB of live data): 2.9KB of Python overhead per row →
**4,142MB projected**, against a container limit of 3,824MB. The box
thrashed at load average 37 with 89MB free and SSH stopped answering.

The same table streamed through a server-side cursor peaked at **25MB**.

These tests pin the memory-safe behaviour using SYNTHETIC data only — no
production table is ever read, and no backup is executed.

Run with: python3 tests/test_backup_streaming.py
"""
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

ROOT = Path('/home/chapman-server/Projects/new-crm')


class TestStreamingExport(unittest.TestCase):
    """The export must never materialise a whole table."""

    def setUp(self):
        import backup_service as bs
        self.bs = bs

    def test_no_whole_table_materialisation(self):
        """The old three-copy pattern must be gone from the export path."""
        src = (ROOT / "backup_service.py").read_text(encoding="utf-8")
        at = src.index("def _build_export_zip_to_path")
        region = src[at:at + 6000]
        self.assertNotIn("db.query_dicts(sql)", region,
                         "the export must not load a whole table into memory")
        self.assertNotIn("clean_rows", region)
        self.assertIn("_stream_table_to_zip", region)

    def test_streaming_helper_uses_a_server_side_cursor(self):
        src = (ROOT / "backup_service.py").read_text(encoding="utf-8")
        at = src.index("def _stream_table_to_zip")
        region = src[at:at + 4000]
        self.assertIn('cursor(name="backup_export_cursor")', region)
        self.assertIn("fetchmany(1000)", region)
        self.assertIn('zf.open(f"{table_name}.json", "w")', region,
                      "rows must be streamed into the zip, not writestr'd")

    def test_streaming_writes_valid_json(self):
        """A synthetic table round-trips through the streaming writer."""
        import db as db_mod

        fake_conn = MagicMock()
        fake_cursor = MagicMock()
        rows = [
            (1, "alice@example.com", "secret_hash"),
            (2, "bob@example.com", "another_hash"),
            (3, "carol@example.com", None),
        ]
        fake_cursor.description = [("id",), ("email",), ("password_hash",)]
        fake_cursor.fetchmany.side_effect = [rows, []]
        fake_conn.cursor.return_value = fake_cursor

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.zip"
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                with patch.object(db_mod, "get_conn", lambda: fake_conn), \
                     patch.object(db_mod, "put_conn", lambda c: None):
                    count = self.bs._stream_table_to_zip(
                        zf, "users", "SELECT * FROM users", {"password_hash"})
            with zipfile.ZipFile(out) as zf:
                data = json.loads(zf.read("users.json"))
        self.assertEqual(count, 3)
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["email"], "alice@example.com")
        self.assertNotIn("password_hash", data[0],
                         "sensitive columns must still be excluded")

    def test_streaming_batches_rather_than_one_query(self):
        """Rows must be fetched in batches, not in one giant fetchall."""
        import db as db_mod
        fake_conn = MagicMock()
        fake_cursor = MagicMock()
        fake_cursor.description = [("id",)]
        fake_cursor.fetchmany.side_effect = [[(1,)], [(2,)], []]
        fake_conn.cursor.return_value = fake_cursor
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.zip"
            with zipfile.ZipFile(out, "w") as zf:
                with patch.object(db_mod, "get_conn", lambda: fake_conn), \
                     patch.object(db_mod, "put_conn", lambda c: None):
                    self.bs._stream_table_to_zip(zf, "t", "SELECT 1", set())
        self.assertEqual(fake_cursor.fetchmany.call_count, 3)


class TestHistoryBundleSafety(unittest.TestCase):
    def test_timeout_is_raised_above_the_old_value(self):
        """The 29GB tar needs ~31 minutes; the old 900s timeout failed it."""
        src = (ROOT / "backup_service.py").read_text(encoding="utf-8")
        self.assertIn("history_timeout_sec", src)
        self.assertNotIn("timeout=900", src,
                         "900s cannot complete the 29GB history tar")

    def test_free_space_preflight_exists(self):
        src = (ROOT / "backup_service.py").read_text(encoding="utf-8")
        self.assertIn("shutil.disk_usage", src)
        self.assertIn("Not enough free space for the history bundle", src)

    def test_retention_keeps_the_most_recent_tars(self):
        """~29GB per run does not fit on a 100GB volume by age."""
        src = (ROOT / "backup_service.py").read_text(encoding="utf-8")
        self.assertIn("history_keep_count", src)
        at = src.index("def _cleanup_old_backups")
        region = src[at:at + 3000]
        self.assertIn("sietch-data-history-*.tar.gz", region)
        self.assertIn("reverse=True", region,
                      "the newest tars must be the ones kept")

    def test_scheduler_does_not_run_at_startup(self):
        """A container restart must never trigger a heavy backup run."""
        src = (ROOT / "backup_service.py").read_text(encoding="utf-8")
        at = src.index("def _backup_loop")
        region = src[at:at + 2500]
        wait_at = region.index("stop_event.wait")
        run_at = region.index("run_backup()")
        self.assertLess(wait_at, run_at,
                        "the loop must wait before the first run")


class TestVolumeWiring(unittest.TestCase):
    def test_compose_mounts_the_backup_volume(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("/mnt/sietch_backup:/backup", compose,
                      "the dedicated backup volume must be mounted")

    def test_example_env_points_at_the_volume(self):
        env = (ROOT / "config.example.env").read_text(encoding="utf-8")
        self.assertIn("BACKUP_DIR=/backup", env)
        self.assertIn("BACKUP_DIR_HOST=/mnt/sietch_backup", env)


class TestMigrationSafety(unittest.TestCase):
    def test_migration_has_no_vacuum(self):
        """The runner executes each migration in one transaction, and VACUUM
        cannot run inside a transaction block."""
        mig = (ROOT / "migrations" / "040_service_feed_delivery_fixes.sql")
        self.assertTrue(mig.exists())
        sql = mig.read_text(encoding="utf-8")
        body = "\n".join(l for l in sql.splitlines()
                         if not l.strip().startswith("--"))
        self.assertNotIn("VACUUM", body.upper(),
                         "VACUUM aborts the whole migration")

    def test_migration_deletes_the_orphans(self):
        sql = (ROOT / "migrations" / "040_service_feed_delivery_fixes.sql").read_text(encoding="utf-8")
        self.assertIn("DELETE FROM notifications", sql)
        self.assertIn("type = 'service_feed'", sql)


if __name__ == "__main__":
    unittest.main(verbosity=1)
