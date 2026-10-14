#!/usr/bin/env python3
"""Unit tests for email audit fix #6 — feedback + scanner log file locking.

Verifies that:
- Multiple threads writing to FEEDBACK_FILE / LOG_FILE concurrently do
  not produce torn lines.
- Concurrent readers (via get_feedback_entries / get_scanner_log) see
  only well-formed JSON entries.
- The I/O failure path (write to a read-only file) now logs an
  exception instead of silently passing.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

# Stub the heavy dependencies before importing mail_scanner.
import types
db_stub = types.ModuleType("db")
db_stub.query_dicts = lambda *a, **k: []
db_stub.query_one = lambda *a, **k: None
db_stub.execute = lambda *a, **k: None
sys.modules.setdefault("db", db_stub)

event_bus_stub = types.ModuleType("event_bus")
event_bus_stub.event_bus = object()
sys.modules.setdefault("event_bus", event_bus_stub)

user_profile_stub = types.ModuleType("user_profile_store")
user_profile_stub.load_user_profile = lambda *a, **k: {}
user_profile_stub.save_user_profile = lambda *a, **k: None
sys.modules.setdefault("user_profile_store", user_profile_stub)

oauth_stub = types.ModuleType("oauth_providers")
oauth_stub.get_provider = lambda *a, **k: None
sys.modules.setdefault("oauth_providers", oauth_stub)

infra_stub = types.ModuleType("infra_log")
sys.modules.setdefault("infra_log", infra_stub)

import scanner.mail_scanner as ms


class TestFeedbackFileLocking(unittest.TestCase):
    """Audit #6: concurrent writers must not produce torn lines."""

    def test_concurrent_writers_no_torn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            feedback_path = Path(tmp) / "feedback.jsonl"
            # Point the module at our temp paths.
            ms.LOG_FILE = log_path
            ms.FEEDBACK_FILE = feedback_path

            n_writers = 8
            writes_per_thread = 50
            barrier = threading.Barrier(n_writers)

            def writer():
                barrier.wait()
                for i in range(writes_per_thread):
                    ms.record_feedback_candidate({
                        "thread": threading.current_thread().name,
                        "i": i,
                        "subject": "claim " + str(i) * 20,
                    })

            threads = [threading.Thread(target=writer, name=f"w{i}") for i in range(n_writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Read every line, count well-formed JSON, look for torn lines.
            with open(feedback_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), n_writers * writes_per_thread,
                             f"expected {n_writers * writes_per_thread} lines, got {len(lines)}")
            parsed = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    parsed += 1
                except json.JSONDecodeError as e:
                    self.fail(f"torn or invalid JSON line: {line!r} ({e})")
            self.assertEqual(parsed, n_writers * writes_per_thread,
                             "every line must parse as JSON")

    def test_concurrent_readers_and_writers(self):
        """A reader (get_feedback_entries) running concurrently with writers
        must never see a torn line (the lock + flock guarantee this)."""
        with tempfile.TemporaryDirectory() as tmp:
            feedback_path = Path(tmp) / "feedback.jsonl"
            ms.FEEDBACK_FILE = feedback_path

            stop = threading.Event()
            write_count = [0]
            read_count = [0]
            torn_count = [0]
            import time as _time

            def writer():
                i = 0
                while not stop.is_set():
                    ms.record_feedback_candidate({"i": i, "subj": "x" * 30})
                    write_count[0] += 1
                    i += 1

            def reader():
                # Read at a lower rate than the writer so we don't starve it.
                while not stop.is_set():
                    entries = ms.get_feedback_entries(limit=1000)
                    for e in entries:
                        read_count[0] += 1
                        if "i" not in e or "subj" not in e:
                            torn_count[0] += 1
                    _time.sleep(0.01)

            threads = [
                threading.Thread(target=writer, name="writer"),
                threading.Thread(target=reader, name="reader1"),
            ]
            for t in threads:
                t.start()
            _time.sleep(0.3)
            stop.set()
            for t in threads:
                t.join(timeout=2.0)

            self.assertGreater(write_count[0], 50, "writers should have written many entries")
            self.assertEqual(torn_count[0], 0, "readers must never see torn entries")

    def test_write_failure_is_logged_not_swallowed(self):
        """When the file can't be written (read-only directory), the writer
        should now log the exception instead of silently passing."""
        with tempfile.TemporaryDirectory() as tmp:
            readonly_dir = Path(tmp) / "readonly"
            readonly_dir.mkdir()
            os.chmod(readonly_dir, 0o555)
            feedback_path = readonly_dir / "feedback.jsonl"
            ms.FEEDBACK_FILE = feedback_path

            with patch.object(ms, "logger") as mock_logger:
                try:
                    ms.record_feedback_candidate({"i": 1})
                finally:
                    os.chmod(readonly_dir, 0o755)

            self.assertTrue(mock_logger.exception.called,
                            "logger.exception should be called on I/O failure")


class TestScannerLogFileLocking(unittest.TestCase):
    """Same as above but for LOG_FILE / _append_log / get_scanner_log."""

    def test_concurrent_appenders_no_torn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            ms.LOG_FILE = log_path

            n_writers = 8
            writes_per_thread = 50
            barrier = threading.Barrier(n_writers)

            def writer():
                barrier.wait()
                for i in range(writes_per_thread):
                    ms._append_log({
                        "ts": "2026-10-14",
                        "i": i,
                        "status": "ok",
                    })

            threads = [threading.Thread(target=writer, name=f"w{i}") for i in range(n_writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), n_writers * writes_per_thread)
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    self.fail(f"torn log line: {line!r} ({e})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
