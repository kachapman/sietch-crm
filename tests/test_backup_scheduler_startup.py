#!/usr/bin/env python3
"""Unit tests: the backup scheduler must actually start (live bug 2026-10-29).

`_load_config_from_db()` returns a TUPLE `(config, configured)`, but three
call sites used it as a dict:

    config = _load_config_from_db()
    if not config.get("enabled", BACKUP_ENABLED):   # -> AttributeError

`start_backup_scheduler()` is called from server.main() inside a try/except
that only prints, so the scheduler died silently at startup:

    Backup scheduler not started: 'tuple' object has no attribute 'get'
    [READY] ... subsystems=... backup=disabled ...

Production therefore ran with NO automated backup since the file was first
committed — and a mistyped mail link that cascaded away rows could not be
restored from any dump.

These tests pin the tuple-unpack at every call site and exercise the real
start path with a stubbed config loader.

Run with: python3 tests/test_backup_scheduler_startup.py
"""
import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

ROOT = Path('/home/chapman-server/Projects/new-crm')
SERVICE = ROOT / "backup_service.py"


def _call_sites():
    """[(lineno, unpacked)] for every _load_config_from_db() call site."""
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_load_config_from_db"):
            out.append((node.lineno, isinstance(node.targets[0], ast.Tuple)))
    return out


class TestConfigLoaderContract(unittest.TestCase):
    def test_loader_returns_tuple(self):
        """The loader's documented contract is (config, configured)."""
        import backup_service as bs
        import inspect
        # `from __future__ import annotations` makes this a string.
        ann = inspect.signature(bs._load_config_from_db).return_annotation
        ann = ann if isinstance(ann, str) else str(ann)
        self.assertIn("tuple", ann)
        self.assertIn("dict", ann)
        self.assertIn("bool", ann)

    def test_every_call_site_unpacks_the_tuple(self):
        sites = _call_sites()
        self.assertGreaterEqual(len(sites), 5,
                                "expected all 5 _load_config_from_db call sites")
        broken = [ln for ln, ok in sites if not ok]
        self.assertEqual(broken, [],
                         f"these call sites use the tuple as a dict: {broken}")

    def test_scheduler_start_path_does_not_raise(self):
        """start_backup_scheduler() must not raise on the config tuple."""
        import backup_service as bs
        with patch.object(bs, "_load_config_from_db", lambda: ({}, False)), \
             patch.object(bs, "_backup_loop", lambda stop: None), \
             patch("threading.Thread") as thread:
            stop = bs.start_backup_scheduler()
        self.assertIsNotNone(stop)
        thread.return_value.start.assert_called_once()

    def test_scheduler_respects_disabled_config(self):
        """enabled=False in the DB config must disable the scheduler."""
        import backup_service as bs
        with patch.object(bs, "_load_config_from_db",
                          lambda: ({"enabled": False}, True)), \
             patch("threading.Thread") as thread:
            bs.start_backup_scheduler()
        thread.return_value.start.assert_not_called()

    def test_cleanup_uses_config_dict(self):
        """_cleanup_old_backups must read config.get(...) from a dict."""
        import backup_service as bs
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bs, "_load_config_from_db",
                              lambda: ({"retention_days": 7}, True)), \
                 patch.object(bs, "BACKUP_DIR", Path(tmp)):
                bs._cleanup_old_backups()  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=1)
