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

from backup_service import _build_data_tar_command


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
