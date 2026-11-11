#!/usr/bin/env python3
"""Unit tests for personal API tokens (full-access default + optional restrictions).

Covers:
  - create stores only the SHA-256 hash; default scope is full
  - blank name / unknown scope rejected; restrictions validated
  - verify returns creator-shaped user; expired/revoked/garbage → None
  - _require_auth with Bearer: full token may GET/POST/admin; read_only /
    method / resource restrictions enforced; writes audited
  - file routes still accept Bearer
  - migration + inline DDL source guards

db is mocked (no Postgres). `import server` still needs a reachable DB for
its connection pool, so like the other server-importing suites this file
SKIPs cleanly without one.

Run with: python3 tests/test_api_tokens.py
"""

import hashlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import server as _server
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    _server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    sys.exit(0)

import api_tokens as _tokens  # noqa: E402


class Shim:
    def __init__(self, path="/", headers=None, command="GET", body=b""):
        self.path = path
        self.headers = headers or {}
        self.command = command
        self._req_body = body
        self.status = None
        self.sent = {}
        self.body = b""

    def send_response(self, code):
        self.status = code

    def send_error(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        self.sent[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        outer = self

        class W:
            def write(self, data):
                outer.body += data

        return W()

    @property
    def rfile(self):
        outer = self

        class R:
            def read(self, n=-1):
                return outer._req_body

        return R()


TOKEN_JOIN_ROW = {
    "token_id": 3, "token_name": "llm agent", "scope": "full",
    "last_used_at": None, "expires_at": None, "restrictions": {},
    "id": 1, "email": "admin@example.com",
    "display_name": "Admin", "first_name": "A", "last_name": "D",
    "is_admin": True, "must_change_password": False,
    "role": "admin", "twofa_method": None,
}


def _mock_token_db(join_row=TOKEN_JOIN_ROW, execute_ret=1):
    mock_db = MagicMock()
    mock_db.query_one.return_value = join_row
    mock_db.execute.return_value = execute_ret
    return mock_db


def _full_user(**overrides):
    base = {
        "id": 1, "email": "admin@example.com", "display_name": "Admin",
        "first_name": "A", "last_name": "D", "is_admin": True,
        "must_change_password": False, "role": "admin", "twofa_method": None,
        "api_token_id": 3, "api_token_name": "llm agent",
        "api_token_scope": "full", "api_token_restrictions": {},
    }
    base.update(overrides)
    return base


class TestCreateStoresHashOnly(unittest.TestCase):
    def test_plaintext_never_persisted_and_default_scope_full(self):
        mock_db = MagicMock()
        captured = {}

        def fake_query_one(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "id": 9, "name": "agent", "token_prefix": "sietch_xxxx",
                "scope": "full", "created_by": 1, "created_at": None,
                "expires_at": None, "restrictions": {},
            }

        mock_db.query_one.side_effect = fake_query_one
        with patch.object(_tokens, "db", mock_db):
            rec = _tokens.create_api_token("agent", 1)
        plaintext = rec["token"]
        self.assertTrue(plaintext.startswith("sietch_"))
        self.assertEqual(rec["scope"], "full")
        stored = " ".join(str(p) for p in captured["params"])
        self.assertNotIn(plaintext, stored)
        self.assertIn(hashlib.sha256(plaintext.encode()).hexdigest(), stored)
        self.assertIn("full", captured["params"])

    def test_blank_name_rejected(self):
        with patch.object(_tokens, "db", MagicMock()):
            with self.assertRaises(ValueError):
                _tokens.create_api_token("   ", 1)

    def test_unknown_scope_rejected(self):
        with patch.object(_tokens, "db", MagicMock()):
            with self.assertRaises(ValueError):
                _tokens.create_api_token("x", 1, scope="write")

    def test_read_scope_adds_get_methods_restriction(self):
        mock_db = MagicMock()

        def fake_query_one(sql, params):
            return {
                "id": 1, "name": "ro", "token_prefix": "sietch_xxxx",
                "scope": "read", "created_by": 1, "created_at": None,
                "expires_at": None, "restrictions": json.loads(params[6]),
                # migration 053: per-token send lock (NULL for this token)
                "mail_send_locked_recipient": params[7],
            }

        mock_db.query_one.side_effect = fake_query_one
        with patch.object(_tokens, "db", mock_db):
            rec = _tokens.create_api_token("ro", 1, scope="read")
        self.assertEqual(rec["scope"], "read")
        self.assertEqual(set(rec["restrictions"]["methods"]), {"GET", "HEAD", "OPTIONS"})

    def test_resource_and_method_restrictions_validated(self):
        with patch.object(_tokens, "db", MagicMock()):
            with self.assertRaises(ValueError):
                _tokens.create_api_token("x", 1, restrictions={"resources": ["nope"]})
            with self.assertRaises(ValueError):
                _tokens.create_api_token("x", 1, restrictions={"methods": ["TRACE"]})

    def test_past_expiry_rejected(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.object(_tokens, "db", MagicMock()):
            with self.assertRaises(ValueError):
                _tokens.create_api_token("x", 1, expires_at=past)


class TestVerify(unittest.TestCase):
    def test_valid_token_returns_creator_user(self):
        with patch.object(_tokens, "db", _mock_token_db()):
            user = _tokens.verify_api_token("sietch_presented")
        self.assertEqual(user["id"], 1)
        self.assertTrue(user["is_admin"])
        self.assertEqual(user["api_token_id"], 3)
        self.assertEqual(user["api_token_scope"], "full")

    def test_garbage_token_returns_none(self):
        mock_db = _mock_token_db(join_row=None)
        with patch.object(_tokens, "db", mock_db):
            self.assertIsNone(_tokens.verify_api_token("sietch_nope"))

    def test_expired_token_returns_none(self):
        row = dict(TOKEN_JOIN_ROW, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        with patch.object(_tokens, "db", _mock_token_db(join_row=row)):
            self.assertIsNone(_tokens.verify_api_token("sietch_x"))

    def test_last_used_update_throttled_to_about_a_minute(self):
        mock_db = _mock_token_db()
        with patch.object(_tokens, "db", mock_db):
            _tokens.verify_api_token("sietch_x")
        update_sql = mock_db.execute.call_args[0][0]
        self.assertIn("last_used_at", update_sql)
        self.assertIn("60", update_sql + str(mock_db.execute.call_args[0][1]))

    def test_bookkeeping_failure_does_not_break_auth(self):
        mock_db = _mock_token_db()
        mock_db.execute.side_effect = Exception("db down")
        with patch.object(_tokens, "db", mock_db):
            user = _tokens.verify_api_token("sietch_x")
        self.assertIsNotNone(user)


class TestRequestAllowed(unittest.TestCase):
    def test_full_default_allows_writes_and_admin(self):
        user = _full_user()
        ok, _ = _tokens.request_allowed(user, "POST", "/api/v2/notes")
        self.assertTrue(ok)
        ok, _ = _tokens.request_allowed(user, "DELETE", "/api/v2/admin/api-tokens/1/revoke")
        self.assertTrue(ok)

    def test_read_only_blocks_post(self):
        user = _full_user(api_token_scope="read", api_token_restrictions={})
        ok, err = _tokens.request_allowed(user, "POST", "/api/v2/notes")
        self.assertFalse(ok)
        self.assertTrue("read-only" in err.lower() or "does not allow" in err.lower(), err)

    def test_method_allowlist(self):
        user = _full_user(api_token_restrictions={"methods": ["GET", "POST"]})
        self.assertTrue(_tokens.request_allowed(user, "POST", "/api/v2/notes")[0])
        ok, err = _tokens.request_allowed(user, "DELETE", "/api/v2/notes/1")
        self.assertFalse(ok)
        self.assertIn("DELETE", err)

    def test_resource_allowlist(self):
        user = _full_user(api_token_restrictions={"resources": ["notes"]})
        self.assertTrue(_tokens.request_allowed(user, "POST", "/api/v2/notes")[0])
        ok, err = _tokens.request_allowed(user, "GET", "/api/v2/contacts")
        self.assertFalse(ok)
        self.assertIn("resource", err.lower())


class TestRequireAuthWithBearer(unittest.TestCase):
    def _auth(self, path, command="GET", user=None, bearer="sietch_good"):
        headers = {}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        shim = Shim(path=path, headers=headers, command=command)
        verified = user if user is not None else _full_user()
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token", return_value=verified), \
             patch.object(_server.api_tokens_mod, "log_token_write") as log_write:
            result = _server._require_auth(shim)
            return shim, result, log_write

    def test_get_data_route_works(self):
        shim, user, _ = self._auth("/api/v2/projects?x=1")
        self.assertEqual(shim.status, None)
        self.assertEqual(user["id"], 1)

    def test_full_token_post_allowed_and_audited(self):
        shim, user, log_write = self._auth("/api/v2/notes", command="POST")
        self.assertEqual(shim.status, None)
        self.assertIsNotNone(user)
        log_write.assert_called_once()
        self.assertEqual(log_write.call_args[0][1].upper(), "POST")

    def test_full_token_admin_get_allowed(self):
        shim, user, _ = self._auth("/api/v2/admin/api-tokens")
        self.assertEqual(shim.status, None)
        self.assertIsNotNone(user)

    def test_read_only_post_rejected(self):
        user = _full_user(api_token_scope="read", api_token_restrictions={})
        shim, result, log_write = self._auth("/api/v2/notes", command="POST", user=user)
        self.assertIsNone(result)
        self.assertEqual(shim.status, 403)
        log_write.assert_not_called()

    def test_method_restriction_blocks_delete(self):
        user = _full_user(api_token_restrictions={"methods": ["GET", "POST"]})
        shim, result, _ = self._auth("/api/v2/notes/1", command="DELETE", user=user)
        self.assertIsNone(result)
        self.assertEqual(shim.status, 403)

    def test_resource_restriction_blocks_contacts(self):
        user = _full_user(api_token_restrictions={"resources": ["notes"]})
        shim, result, _ = self._auth("/api/v2/contacts", user=user)
        self.assertIsNone(result)
        self.assertEqual(shim.status, 403)

    def test_garbage_token_401(self):
        shim = Shim(path="/api/v2/projects", headers={"Authorization": "Bearer junk"})
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token", return_value=None):
            user = _server._require_auth(shim)
        self.assertIsNone(user)
        self.assertEqual(shim.status, 401)

    def test_session_cookie_still_works(self):
        shim = Shim(path="/api/v2/projects",
                    headers={"Cookie": "sietch_session=good"})
        with patch.object(_server.auth_mod, "get_session_user",
                          return_value={"id": 2, "is_admin": False}):
            user = _server._require_auth(shim)
        self.assertEqual(user["id"], 2)
        self.assertEqual(shim.status, None)


class TestSourceGuards(unittest.TestCase):
    def test_migration_046_present(self):
        mig = (HERE.parent / "migrations" / "046_api_tokens_full_access.sql").read_text()
        self.assertIn("api_token_audit_log", mig)
        self.assertIn("expires_at", mig)
        self.assertIn("restrictions", mig)
        self.assertIn("SET DEFAULT 'full'", mig)

    def test_inline_ddl_mentions_046(self):
        src = (HERE.parent / "server.py").read_text()
        self.assertIn("Migration 046", src)
        self.assertIn("api_token_audit_log", src)
        self.assertIn("SCOPE_FULL", src)
        # No hard GET-only gate left
        self.assertNotIn("API tokens are read-only (GET only)", src)


if __name__ == "__main__":
    unittest.main()
