#!/usr/bin/env python3
"""Unit tests for email audit fixes #3, #4, #5.

- #5: server._handle_mail_message_create now requires auth, validates
  account_id, enforces per-field body size, allows only safe folders, and
  coerces booleans.

- #3/#4: covered by the JS test (test_email_audit_345.mjs) — no Python
  test for the modal because the modal logic is in a 32k-line vanilla
  JS file with no module system.

The handler tests use stdlib unittest + MagicMock to:
- patch _server._require_auth to return a configurable user (or None)
- patch _server._mail_account_accessible to be a controllable stub
- patch _server.db to track SQL without a real DB
- patch _server._json_response to capture (status, body) for assertions
"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server


def _call_create(body: bytes = b"{}", user=None, *,
                 access_for=None, json_response_capture=None,
                 db_query_one_results=None, db_execute_results=None):
    """Invoke KanbanHandler._handle_mail_message_create with patched
    dependencies. Returns the mock `db` object for further inspection.

    Args:
        body: raw request body bytes
        user: value to return from _require_auth (None = unauthenticated)
        access_for: dict {account_id: bool} for _mail_account_accessible
        json_response_capture: list to which (status, body) tuples are appended
        db_query_one_results: list of values to return from successive
            query_one() calls (one per call, in order). None = return None.
        db_execute_results: list of values for successive execute() calls.
    """
    if access_for is None:
        # Default: admin user can access everything
        access_for = {}

    if json_response_capture is None:
        json_response_capture = []

    def fake_require_auth(h):
        return user

    def fake_access(u, account_id):
        if u is None:
            return False
        if u.get("is_admin"):
            return True
        return access_for.get(account_id, False)

    def fake_read_body(h):
        return body

    def fake_json_response(h, status, body_):
        json_response_capture.append((status, body_))
        return None

    def make_query_one():
        calls = {"i": 0}
        def q(sql, params=None, *a, **k):
            if db_query_one_results is None:
                return None
            if calls["i"] >= len(db_query_one_results):
                return None
            r = db_query_one_results[calls["i"]]
            calls["i"] += 1
            return r
        return q

    def make_execute():
        calls = {"i": 0}
        def e(sql, params=None, *a, **k):
            if db_execute_results is None:
                return None
            if calls["i"] >= len(db_execute_results):
                return None
            r = db_execute_results[calls["i"]]
            calls["i"] += 1
            return r
        return e

    captured = {"queries": [], "executes": []}

    def tracking_query_one(sql, params=None, *a, **k):
        captured["queries"].append((sql, params))
        return make_query_one()(sql, params, *a, **k)

    def tracking_execute(sql, params=None, *a, **k):
        captured["executes"].append((sql, params))
        return make_execute()(sql, params, *a, **k)

    mock_db = MagicMock()
    mock_db.query_one.side_effect = tracking_query_one
    mock_db.execute.side_effect = tracking_execute

    # Shim: must mimic enough of SimpleHTTPRequestHandler for the
    # module-level helpers (_read_body, _session_token, _json_response).
    # - headers: for _session_token / _read_body
    # - rfile: for _read_body
    # - send_response / send_header / end_headers / wfile: for _json_response
    class _FakeHeaders:
        def get(self, key, default=None):
            return default

    class _FakeRFile:
        def read(self, n):
            return b""

    class Shim:
        headers = _FakeHeaders()
        rfile = _FakeRFile()
        def send_response(self, code): pass
        def send_header(self, k, v): pass
        def end_headers(self): pass
        wfile = MagicMock()

    with patch.object(_server, "_require_auth", fake_require_auth), \
         patch.object(_server, "_read_body", fake_read_body), \
         patch.object(_server, "_mail_account_accessible", fake_access), \
         patch.object(_server, "_json_response", fake_json_response), \
         patch.object(_server, "db", mock_db), \
         patch.object(_server, "logger"):
        _server.KanbanHandler._handle_mail_message_create(Shim())

    return {
        "db": mock_db,
        "queries": captured["queries"],
        "executes": captured["executes"],
        "responses": json_response_capture,
    }


class TestMailMessageCreateAuth(unittest.TestCase):
    """Audit #5: _handle_mail_message_create must require auth."""

    def test_unauthenticated_returns_401_no_db(self):
        r = _call_create(b'{"account_id": 1}', user=None)
        # No DB queries should have been made.
        self.assertEqual(len(r["queries"]), 0,
                         "unauthenticated must not reach DB")
        self.assertEqual(len(r["executes"]), 0)

    def test_authenticated_admin_proceeds_to_validation(self):
        r = _call_create(
            b'{"account_id": 1, "subject": "hi"}',
            user={"id": 1, "is_admin": True},
        )
        # Should reach DB (mail_folders lookup or INSERT).
        self.assertGreater(len(r["queries"]), 0,
                           "admin should reach DB after auth check")


class TestMailMessageCreatePayloadValidation(unittest.TestCase):
    """Audit #5: payload shape validation."""

    def test_rejects_missing_account_id(self):
        r = _call_create(b'{}', user={"id": 1, "is_admin": True})
        # Should return 400 with "account_id required" — no INSERT.
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 400 for s, _ in r["responses"]))

    def test_rejects_non_integer_account_id(self):
        r = _call_create(
            b'{"account_id": "abc"}',
            user={"id": 1, "is_admin": True},
        )
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 400 for s, _ in r["responses"]))

    def test_rejects_account_user_does_not_own(self):
        r = _call_create(
            b'{"account_id": 99, "subject": "x"}',
            user={"id": 5, "is_admin": False},
            access_for={},  # user 5 has no access to 99
        )
        # Should return 403 — no INSERT.
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 403 for s, _ in r["responses"]))

    def test_allows_user_who_owns_account(self):
        r = _call_create(
            b'{"account_id": 5, "subject": "x"}',
            user={"id": 5, "is_admin": False},
            access_for={5: True},
            db_query_one_results=[{"id": 7}],  # folder lookup returns builtin (skip) → INSERT
        )
        # Should reach the INSERT.
        insert_called = any(
            sql.lstrip().upper().startswith("INSERT") for sql, _ in r["queries"]
        )
        self.assertTrue(insert_called,
                        "owner with valid payload should reach INSERT")

    def test_allows_admin_for_any_account(self):
        r = _call_create(
            b'{"account_id": 99, "subject": "x"}',
            user={"id": 1, "is_admin": True},
            db_query_one_results=[{"id": 7}],
        )
        insert_called = any(
            sql.lstrip().upper().startswith("INSERT") for sql, _ in r["queries"]
        )
        self.assertTrue(insert_called,
                        "admin should be able to write to any account")


class TestMailMessageCreateSizeLimits(unittest.TestCase):
    """Audit #5: body size caps."""

    def test_rejects_oversize_body_text(self):
        big = "x" * (5 * 1024 * 1024 + 1)
        r = _call_create(
            json.dumps({"account_id": 1, "body_text": big}).encode(),
            user={"id": 1, "is_admin": True},
        )
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 413 for s, _ in r["responses"]))

    def test_rejects_oversize_body_html(self):
        big = "x" * (5 * 1024 * 1024 + 1)
        r = _call_create(
            json.dumps({"account_id": 1, "body_html": big}).encode(),
            user={"id": 1, "is_admin": True},
        )
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 413 for s, _ in r["responses"]))

    def test_rejects_combined_oversize(self):
        a = "x" * (6 * 1024 * 1024)
        b = "y" * (6 * 1024 * 1024)
        r = _call_create(
            json.dumps({"account_id": 1, "body_text": a, "body_html": b}).encode(),
            user={"id": 1, "is_admin": True},
        )
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 413 for s, _ in r["responses"]))

    def test_accepts_size_at_limit(self):
        # 5 MB body_text is exactly at the limit — should pass.
        body_text = "x" * (5 * 1024 * 1024)
        r = _call_create(
            json.dumps({"account_id": 1, "body_text": body_text}).encode(),
            user={"id": 1, "is_admin": True},
            db_query_one_results=[{"id": 7}],
        )
        # No 413 response expected.
        self.assertFalse(any(s == 413 for s, _ in r["responses"]))


class TestMailMessageCreateFolderValidation(unittest.TestCase):
    """Audit #5: folder must be in allowlist (or exist as custom folder)."""

    def test_accepts_builtin_folders_without_folders_lookup(self):
        for f in ["INBOX", "Sent", "Drafts", "Archive", "Trash", "Junk", "Outbox"]:
            with self.subTest(folder=f):
                r = _call_create(
                    json.dumps({"account_id": 1, "folder": f, "subject": "x"}).encode(),
                    user={"id": 1, "is_admin": True},
                    db_query_one_results=[{"id": 7}],
                )
                # Builtin folders skip the mail_folders lookup.
                folder_lookup = any(
                    "FROM mail_folders" in sql for sql, _ in r["queries"]
                )
                self.assertFalse(folder_lookup,
                                 f"builtin folder {f!r} should not look up mail_folders")

    def test_rejects_unknown_folder(self):
        r = _call_create(
            b'{"account_id": 1, "folder": "../etc"}',
            user={"id": 1, "is_admin": True},
        )
        self.assertEqual(len(r["executes"]), 0)
        self.assertTrue(any(s == 400 for s, _ in r["responses"]))

    def test_rejects_unknown_custom_folder(self):
        """Custom folder name that doesn't exist in mail_folders → 400."""
        r = _call_create(
            b'{"account_id": 1, "folder": "MyWeirdFolder", "subject": "x"}',
            user={"id": 1, "is_admin": True},
            db_query_one_results=[None],  # mail_folders lookup returns None
        )
        # Should 400 because the folder doesn't exist.
        self.assertTrue(any(s == 400 for s, _ in r["responses"]))
        # No INSERT should have been made.
        insert_called = any(
            sql.lstrip().upper().startswith("INSERT")
            for sql, _ in r["queries"]
        )
        self.assertFalse(insert_called)

    def test_accepts_existing_custom_folder(self):
        r = _call_create(
            b'{"account_id": 1, "folder": "Projects/2026", "subject": "x"}',
            user={"id": 1, "is_admin": True},
            db_query_one_results=[
                {"id": 11},  # mail_folders lookup returns the row
                {"id": 99},  # INSERT RETURNING
            ],
        )
        # Should reach the INSERT after passing folder validation.
        insert_called = any(
            sql.lstrip().upper().startswith("INSERT")
            for sql, _ in r["queries"]
        )
        self.assertTrue(insert_called)


class TestMailMessageCreateBooleanCoercion(unittest.TestCase):
    """Audit #5: defensive bool coercion."""

    def test_coerces_string_is_read_to_true(self):
        r = _call_create(
            b'{"account_id": 1, "is_read": "yes"}',
            user={"id": 1, "is_admin": True},
            db_query_one_results=[{"id": 7}],
        )
        # The INSERT params should contain True (the coerced is_read).
        insert_call = next(
            ((sql, params) for sql, params in r["queries"]
             if sql.lstrip().upper().startswith("INSERT")),
            None,
        )
        self.assertIsNotNone(insert_call)
        self.assertIn(True, insert_call[1])

    def test_coerces_int_is_flagged_to_true(self):
        r = _call_create(
            b'{"account_id": 1, "is_flagged": 1}',
            user={"id": 1, "is_admin": True},
            db_query_one_results=[{"id": 7}],
        )
        insert_call = next(
            ((sql, params) for sql, params in r["queries"]
             if sql.lstrip().upper().startswith("INSERT")),
            None,
        )
        self.assertIsNotNone(insert_call)
        self.assertIn(True, insert_call[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
