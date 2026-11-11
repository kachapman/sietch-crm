#!/usr/bin/env python3
"""Migration runner: record as applied only after a successful commit.

Ken (2026-11-10): failed migrations were inserted into `_applied_migrations`
after a rollback, so prod could mark 017 applied while the Action seed never
landed. A failure must roll back, stay unrecorded, and stop boot/deploy with
a non-zero exit. Already-recorded filenames must still be skipped so they are
not re-executed on deploy.

Run with: python3 tests/test_migration_runner.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import network_guard  # noqa: E402

network_guard.install()

import psycopg2  # noqa: E402


DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "sietch")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "local_dev_password")
SCRATCH = f"sietch_migrunner_{os.getpid()}"


def _admin():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname="postgres",
        user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
    )


def _connect(dbname: str):
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=dbname,
        user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
    )


def _write_migs(dirpath: Path, files: dict[str, str]) -> None:
    for name, sql in files.items():
        (dirpath / name).write_text(sql)


class TestMigrationRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = _admin()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
        cur.execute(f"CREATE DATABASE {SCRATCH}")
        cur.close()
        conn.close()
        os.environ["DB_HOST"] = DB_HOST
        os.environ["DB_PORT"] = str(DB_PORT)
        os.environ["DB_NAME"] = SCRATCH
        os.environ["DB_USER"] = DB_USER
        os.environ["DB_PASSWORD"] = DB_PASSWORD

    @classmethod
    def tearDownClass(cls):
        try:
            import db as dbmod
            dbmod.close_pool()
        except Exception:
            pass
        conn = _admin()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
        cur.close()
        conn.close()

    def setUp(self):
        import db as dbmod
        dbmod.close_pool()
        conn = _admin()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
        cur.execute(f"CREATE DATABASE {SCRATCH}")
        cur.close()
        conn.close()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="sietch_migs_")
        self.migdir = Path(self._tmpdir.name)
        os.environ["SIETCH_MIGRATIONS_DIR"] = str(self.migdir)

    def tearDown(self):
        import db as dbmod
        dbmod.close_pool()
        os.environ.pop("SIETCH_MIGRATIONS_DIR", None)
        self._tmpdir.cleanup()

    def _run(self):
        import db as dbmod
        dbmod.init_db()

    def _applied(self) -> list[str]:
        conn = _connect(SCRATCH)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT filename FROM _applied_migrations ORDER BY filename"
            )
            rows = [r[0] for r in cur.fetchall()]
            cur.close()
            return rows
        finally:
            conn.close()

    def _tables(self) -> set[str]:
        conn = _connect(SCRATCH)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            return {r[0] for r in cur.fetchall()}
        finally:
            conn.close()

    def test_successful_migration_is_recorded_and_applied(self):
        _write_migs(self.migdir, {
            "001_ok.sql": "CREATE TABLE mig_ok (id INTEGER PRIMARY KEY);",
        })
        self._run()
        self.assertIn("001_ok.sql", self._applied())
        self.assertIn("mig_ok", self._tables())

    def test_failed_migration_is_not_recorded_and_is_rolled_back(self):
        from db import MigrationFailed

        _write_migs(self.migdir, {
            "001_ok.sql": "CREATE TABLE mig_ok (id INTEGER PRIMARY KEY);",
            "002_bad.sql": (
                "CREATE TABLE mig_partial (id INTEGER PRIMARY KEY);"
                "CREATE TABLE this_will_fail (oops INT INT);"
            ),
            "003_later.sql": "CREATE TABLE mig_later (id INTEGER PRIMARY KEY);",
        })
        # First apply 001 only by temporarily not having 002/003? We'll run all
        # and expect failure on 002 after 001 has been committed.
        with self.assertRaises(SystemExit) as ctx:
            self._run()
        self.assertIn("002_bad.sql", str(ctx.exception))
        self.assertIn("001_ok.sql", self._applied())
        self.assertNotIn("002_bad.sql", self._applied())
        self.assertNotIn("003_later.sql", self._applied())
        tables = self._tables()
        self.assertIn("mig_ok", tables)
        self.assertNotIn("mig_partial", tables)
        self.assertNotIn("mig_later", tables)

    def test_already_recorded_filename_is_skipped_even_if_file_would_fail(self):
        _write_migs(self.migdir, {
            "001_ok.sql": "CREATE TABLE mig_ok (id INTEGER PRIMARY KEY);",
        })
        self._run()
        # Record a poisonous file as already applied, then put a failing
        # body in the directory. Boot must succeed and must not execute it.
        import db as dbmod
        dbmod.close_pool()
        conn = _connect(SCRATCH)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO _applied_migrations (filename) VALUES (%s)",
                ("002_poison.sql",),
            )
            conn.commit()
        finally:
            conn.close()
        _write_migs(self.migdir, {
            "001_ok.sql": "CREATE TABLE mig_ok (id INTEGER PRIMARY KEY);",
            "002_poison.sql": "CREATE TABLE this_must_not_run (oops INT INT);",
        })
        self._run()  # must not raise
        self.assertEqual(self._applied(), ["001_ok.sql", "002_poison.sql"])
        self.assertNotIn("this_must_not_run", self._tables())

    def test_init_db_exits_nonzero_on_failure(self):
        _write_migs(self.migdir, {
            "001_bad.sql": "THIS IS NOT SQL;",
        })
        with self.assertRaises(SystemExit) as ctx:
            self._run()
        # SystemExit with a string exits the process with status 1.
        self.assertIsInstance(ctx.exception, SystemExit)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertNotIn("001_bad.sql", self._applied())

    def test_source_no_longer_records_failures_as_applied(self):
        src = (ROOT / "db.py").read_text(encoding="utf-8")
        self.assertNotIn("prevent retry loop", src)
        self.assertNotIn("recording as applied to", src)
        self.assertIn("class MigrationFailed", src)
        self.assertIn("raise SystemExit", src)
        self.assertIn("SIETCH_MIGRATIONS_DIR", src)
        # server still boots through init_db
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("db.init_db()", server)


if __name__ == "__main__":
    unittest.main()
