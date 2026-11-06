#!/usr/bin/env python3
"""Admin API-token UI: backend contract + authorization guards.

The HTTP behaviour of these endpoints is exercised end-to-end by
tests/test_admin_api_tokens_http.py against a throwaway database. This suite
is the fast, DB-free companion: it pins the response shape the UI reads and
the admin gates that protect the routes, so a refactor cannot silently hand
the UI a different contract (or drop the guard).

No database connection is opened: api_tokens.db is mocked.

Run with: python3 tests/test_admin_api_token_ui.py -v
"""

import hashlib
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import api_tokens  # noqa: E402  (imports db but never connects)

SERVER_SRC = (ROOT / "server.py").read_text(encoding="utf-8")
TOKENS_SRC = (ROOT / "api_tokens.py").read_text(encoding="utf-8")
APP_SRC = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
HELPER_SRC = (ROOT / "public" / "api-token-ui.js").read_text(encoding="utf-8")

SAFE_LIST_KEYS = {
    "id", "name", "token_prefix", "scope", "created_by",
    "created_by_email", "created_at", "last_used_at", "revoked_at", "revoked",
}


def _list_row(**over):
    row = {
        "id": 4, "name": "synthetic bulk pull", "token_prefix": "sietch_abcde",
        "scope": "read", "created_by": 1, "created_by_email": "admin@synthetic.test",
        "created_at": None, "last_used_at": None, "revoked_at": None,
    }
    row.update(over)
    return row


class TestListContract(unittest.TestCase):
    def test_list_returns_only_safe_metadata(self):
        leaked = "sietch_" + "x" * 40
        mock_db = MagicMock()
        mock_db.query_dicts.return_value = [_list_row(token_hash="deadbeef", token=leaked)]
        with patch.object(api_tokens, "db", mock_db):
            rows = api_tokens.list_api_tokens()
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), SAFE_LIST_KEYS)
        self.assertNotIn(leaked, repr(rows))
        self.assertNotIn("deadbeef", repr(rows))
        self.assertFalse(rows[0]["revoked"])

    def test_list_marks_revoked_rows(self):
        mock_db = MagicMock()
        mock_db.query_dicts.return_value = [_list_row(revoked_at=datetime(2026, 11, 6, 12, 0))]
        with patch.object(api_tokens, "db", mock_db):
            rows = api_tokens.list_api_tokens()
        self.assertTrue(rows[0]["revoked"])
        self.assertEqual(rows[0]["revoked_at"], "2026-11-06T12:00:00")

    def test_list_select_never_reads_the_hash(self):
        mock_db = MagicMock()
        mock_db.query_dicts.return_value = []
        with patch.object(api_tokens, "db", mock_db):
            api_tokens.list_api_tokens()
        sql = mock_db.query_dicts.call_args[0][0]
        self.assertNotIn("token_hash", sql)
        for field in ("token_prefix", "scope", "created_at", "last_used_at", "revoked_at"):
            self.assertIn(field, sql)


class TestCreateContract(unittest.TestCase):
    def _create(self, name="bulk pull", scope="read"):
        captured = {}
        mock_db = MagicMock()

        def fake_query_one(sql, params):
            captured["params"] = params
            return {
                "id": 9, "name": name, "token_prefix": "sietch_abcde",
                "scope": scope, "created_by": 1, "created_at": None,
            }

        mock_db.query_one.side_effect = fake_query_one
        with patch.object(api_tokens, "db", mock_db):
            return api_tokens.create_api_token(name, 1, scope), captured

    def test_create_response_carries_the_one_time_secret(self):
        rec, captured = self._create()
        self.assertIn("token", rec)
        self.assertTrue(rec["token"].startswith(api_tokens.TOKEN_PREFIX))
        self.assertEqual(set(rec), {
            "id", "name", "token", "token_prefix", "scope", "created_by", "created_at",
        })

    def test_only_the_hash_is_stored(self):
        rec, captured = self._create()
        params = captured["params"]
        stored_hash = params[1]
        self.assertEqual(stored_hash, hashlib.sha256(rec["token"].encode()).hexdigest())
        self.assertEqual(len(stored_hash), 64)
        self.assertNotIn(rec["token"], [str(p) for p in params])

    def test_blank_name_is_rejected(self):
        for name in ("", "   ", "\t\n", None):
            with self.assertRaises(ValueError):
                api_tokens.create_api_token(name, 1)

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            api_tokens.create_api_token("x", 1, "write")
        with self.assertRaises(ValueError):
            api_tokens.create_api_token("x", 1, "admin")


class TestServerAuthorizationGuards(unittest.TestCase):
    def test_get_route_requires_admin_before_listing(self):
        anchor = 'if api_path == "/api/v2/admin/api-tokens":'
        at = SERVER_SRC.index(anchor)
        region = SERVER_SRC[at:at + 500]
        guard = region.index("user = _require_admin(self)")
        handler = region.index("self._handle_api_tokens_list()")
        self.assertLess(guard, handler)

    def test_post_route_requires_admin_before_creating(self):
        anchor = 'if api_path == "/api/v2/admin/api-tokens" and method == "POST":'
        at = SERVER_SRC.index(anchor)
        region = SERVER_SRC[at:at + 500]
        self.assertLess(
            region.index("user = _require_admin(self)"),
            region.index("self._handle_api_token_create()"),
        )

    def test_revoke_route_requires_admin(self):
        anchor = 'r"^/api/v2/admin/api-tokens/(\\d+)/revoke$"'
        at = SERVER_SRC.index(anchor)
        region = SERVER_SRC[at:at + 500]
        self.assertLess(
            region.index("user = _require_admin(self)"),
            region.index("self._handle_api_token_revoke"),
        )

    def test_handlers_recheck_admin(self):
        for name, call in (
            ("_handle_api_tokens_list", "_json_response(self, 200"),
            ("_handle_api_token_create", "json.loads"),
            ("_handle_api_token_revoke", "revoke_api_token"),
        ):
            at = SERVER_SRC.index("def %s(" % name)
            region = SERVER_SRC[at:at + 900]
            self.assertLess(
                region.index("user = _require_admin(self)"),
                region.index(call),
                "%s must gate before doing work" % name,
            )

    def test_bearer_tokens_are_blocked_from_admin_paths(self):
        self.assertIn('"/api/v2/admin"', SERVER_SRC)
        self.assertIn("def _api_token_path_blocked", SERVER_SRC)

    def test_create_warning_is_returned_once(self):
        at = SERVER_SRC.index("def _handle_api_token_create(")
        region = SERVER_SRC[at:at + 1200]
        self.assertIn('rec["warning"]', region)
        self.assertIn("shown only once and cannot be recovered", region)
        self.assertIn("_json_response(self, 201, rec)", region)


class TestUiBackendFieldAlignment(unittest.TestCase):
    def test_helper_reads_the_fields_the_backend_returns(self):
        for field in ("token_prefix", "created_by_email", "revoked_at",
                      "last_used_at", "created_at", "scope", "name", "id"):
            self.assertIn(field, HELPER_SRC, "helper must read %s" % field)
            self.assertIn('"%s"' % field, TOKENS_SRC, "backend must return %s" % field)

    def test_ui_endpoints_exist_in_the_server(self):
        for path in ("/api/v2/admin/api-tokens", "/revoke"):
            self.assertIn(path, APP_SRC)
        self.assertIn('api_path == "/api/v2/admin/api-tokens"', SERVER_SRC)
        self.assertIn("revoke$", SERVER_SRC)

    def test_create_body_is_name_only(self):
        match = re.search(r"body:\s*\{([^}]*)\}", HELPER_SRC)
        self.assertIsNotNone(match, "buildCreateRequest must return a body")
        self.assertIn("name", match.group(1))
        self.assertNotIn("scope", match.group(1))


class TestNoPlaintextLeaksInTokenCode(unittest.TestCase):
    def test_module_never_prints_or_logs_the_plaintext(self):
        self.assertNotIn("print(", TOKENS_SRC)
        self.assertNotIn("logger.info", TOKENS_SRC)
        self.assertNotIn("logger.warning", TOKENS_SRC)
        # The only logger use is the debug line for bookkeeping failures.
        for line in TOKENS_SRC.splitlines():
            if "logger." in line:
                self.assertNotIn("plaintext", line)
                self.assertNotIn("rec[", line)
                self.assertNotIn("token=", line)

    def test_create_handler_never_logs_the_response(self):
        at = SERVER_SRC.index("def _handle_api_token_create(")
        region = SERVER_SRC[at:at + 1200]
        for forbidden in ("logger.", "print(", "console."):
            self.assertNotIn(forbidden, region)


if __name__ == "__main__":
    unittest.main(verbosity=2)
