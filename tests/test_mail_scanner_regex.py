#!/usr/bin/env python3
"""Unit tests for the mail scanner's regex trigger engine (mail_scanner.py).

Covers audit findings #2 and #16:
- _get_regex returns a never-match sentinel on bad patterns (no exception)
- _is_valid_regex rejects bad patterns (so the rule-save endpoint can 400)
- _get_regex is bounded via lru_cache(maxsize=1024)
- get_scanner_log / get_feedback_entries default limit to 200 when 0/None
- get_scanner_log / get_feedback_entries skip malformed JSON lines
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

# mail_scanner.py does `import db` at module top, which tries to connect.
# Stub the db import so the test can import mail_scanner in isolation.
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


class TestGetRegex(unittest.TestCase):
    """Audit #2: regex cache is bounded and safe for bad patterns."""

    def test_valid_pattern_compiles_and_matches(self):
        pat = ms._get_regex(r"claim\s+\d+")
        self.assertTrue(pat.search("claim 12345"))
        self.assertFalse(pat.search("policy 99"))

    def test_bad_pattern_returns_never_match_sentinel(self):
        """Unmatched parenthesis is a compile error. _get_regex must not raise
        — it should return a sentinel that matches nothing."""
        # Note: the cache may already have a previous result. Clear it to
        # exercise the failure path on a clean compile attempt.
        ms._get_regex.cache_clear()
        bad = ms._get_regex(r"(")  # unmatched group
        # The sentinel must match nothing.
        self.assertIsNone(bad.search("anything"))
        # The sentinel must not raise.
        bad.search("((")

    def test_bad_pattern_does_not_raise(self):
        """Even with a deeply broken pattern, _get_regex must return cleanly."""
        ms._get_regex.cache_clear()
        for bad in [r"(", r"[", r"(?P<", r"*", r"(?P<x>x"]:
            with self.subTest(pattern=bad):
                pat = ms._get_regex(bad)
                # search() must not raise.
                pat.search("test")
                pat.search("")

    def test_cache_is_bounded(self):
        """The cache uses functools.lru_cache(maxsize=1024). Inserting 2000
        distinct patterns should not blow up and should not unboundedly grow
        memory."""
        ms._get_regex.cache_clear()
        for i in range(2000):
            ms._get_regex(f"a{i}b")
        # lru_cache exposes cache_info(). currentsize should be <= 1024.
        info = ms._get_regex.cache_info()
        self.assertLessEqual(info.currsize, 1024)

    def test_is_valid_regex_accepts_good_patterns(self):
        self.assertTrue(ms._is_valid_regex(r"claim\s+\d+"))
        self.assertTrue(ms._is_valid_regex(r".*"))
        self.assertTrue(ms._is_valid_regex(""))  # empty matches anywhere

    def test_is_valid_regex_rejects_bad_patterns(self):
        self.assertFalse(ms._is_valid_regex(r"("))
        self.assertFalse(ms._is_valid_regex(r"["))
        self.assertFalse(ms._is_valid_regex(r"(?P<"))
        self.assertFalse(ms._is_valid_regex(r"*"))


class TestEvaluateConditionRegex(unittest.TestCase):
    """End-to-end: _evaluate_condition must not raise on bad regex patterns,
    and must return False (no match) for them."""

    def setUp(self):
        self.msg = {
            "subject": "Claim 12345 filed",
            "body_text": "Body about claim 12345",
            "from": "user@example.com",
            "from_name": "Test User",
        }

    def test_subject_regex_with_good_pattern(self):
        cond = {"type": "subject_regex", "value": r"claim\s+\d+"}
        ms._get_regex.cache_clear()
        self.assertTrue(ms._evaluate_condition(cond, self.msg, {}))

    def test_subject_regex_with_bad_pattern_returns_false(self):
        """Audit #2 hot path: bad pattern must not raise, must return False."""
        cond = {"type": "subject_regex", "value": r"(unmatched"}
        ms._get_regex.cache_clear()
        # Must not raise.
        result = ms._evaluate_condition(cond, self.msg, {})
        self.assertFalse(result)

    def test_body_regex_with_bad_pattern_returns_false(self):
        cond = {"type": "body_regex", "value": r"[unclosed"}
        ms._get_regex.cache_clear()
        result = ms._evaluate_condition(cond, self.msg, {})
        self.assertFalse(result)

    def test_subject_regex_with_negate(self):
        cond = {"type": "subject_regex", "value": r"(bad", "negate": True}
        # We have to construct it correctly; condition dict:
        cond = {"type": "subject_regex", "value": r"(bad", "negate": True}
        ms._get_regex.cache_clear()
        # bad pattern → no match → not negate → True
        self.assertTrue(ms._evaluate_condition(cond, self.msg, {}))


class TestLogHelpersLimitDefault(unittest.TestCase):
    """Audit #16: get_scanner_log / get_feedback_entries must default limit
    to 200 when 0/None is passed (and not return the entire log)."""

    def test_scanner_log_default_limit_when_zero(self):
        """Build a 5-line log, pass limit=0, expect only the last 200 (i.e.
        all 5 here) — but specifically the function must not explode or
        return more than what fits in the cap. The key invariant is that
        limit=0 behaves the same as limit=200, not like limit=None."""
        with patch.object(ms, "LOG_FILE") as mock_log:
            mock_log.exists.return_value = True
            # 5 lines
            lines = [json.dumps({"i": i, "account_id": 1}) + "\n" for i in range(5)]
            # The function uses the built-in `open()`, not LOG_FILE.open, so
            # patch the built-in.
            with patch("builtins.open", lambda *a, **k: _FakeFile(lines)):
                result = ms.get_scanner_log(0)
            self.assertEqual(len(result), 5)

    def test_scanner_log_skips_malformed_lines(self):
        """Partial writes from record_feedback_candidate must not crash
        get_scanner_log."""
        with patch.object(ms, "LOG_FILE") as mock_log:
            mock_log.exists.return_value = True
            lines = [
                json.dumps({"i": 1, "account_id": 1}) + "\n",
                "{not valid json",  # partial write
                "\n",  # blank
                json.dumps({"i": 2, "account_id": 1}) + "\n",
            ]
            with patch("builtins.open", lambda *a, **k: _FakeFile(lines)):
                result = ms.get_scanner_log(10)
            # Only the 2 valid lines should be returned.
            self.assertEqual(len(result), 2)

    def test_feedback_entries_skips_malformed_lines(self):
        with patch.object(ms, "FEEDBACK_FILE") as mock_log:
            mock_log.exists.return_value = True
            lines = [
                "{bad",
                json.dumps({"x": 1}) + "\n",
                json.dumps({"x": 2}) + "\n",
            ]
            with patch("builtins.open", lambda *a, **k: _FakeFile(lines)):
                result = ms.get_feedback_entries(10)
            self.assertEqual(len(result), 2)

    def test_feedback_entries_default_limit_when_none(self):
        with patch.object(ms, "FEEDBACK_FILE") as mock_log:
            mock_log.exists.return_value = True
            lines = [json.dumps({"i": i}) + "\n" for i in range(5)]
            with patch("builtins.open", lambda *a, **k: _FakeFile(lines)):
                result = ms.get_feedback_entries(None)
            self.assertEqual(len(result), 5)


class _FakeFile:
    """Minimal file-like context manager for the log/feedback read tests."""

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.lines)

    def readlines(self):
        return self.lines


if __name__ == "__main__":
    unittest.main(verbosity=2)
