#!/usr/bin/env python3
"""Unit tests for personal read-only API tokens (Part 2).

Covers:
  - create stores only the SHA-256 hash (plaintext never persisted);
    blank name / unknown scope rejected
  - verify returns the creator-shaped user, None for garbage/revoked/
    wrong-scope tokens; last_used_at update is throttled to ~60s
  - _require_auth with Bearer: GET data route works; POST/PUT/DELETE get
    403; auth/user/admin/me/my routes get 403 even for GET; garbage gets 401
  - file routes accept Bearer: document + history-attachment downloads 200
  - token-management handlers refuse Bearer (403, no list leak)
  - migration + init.sql + inline DDL + smoke wiring source guards

db is mocked (no Postgres). `import server` still needs a reachable DB for
its connection pool, so like the other server-importing suites this file
SKIPs cleanly without one.

Run with: python3 tests/test_api_tokens.py
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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
    "token_id": 3, "token_name": "bulk pull", "scope": "read",
    "last_used_at": None, "id": 1, "email": "admin@example.com",
    "display_name": "Admin", "first_name": "A", "last_name": "D",
    "is_admin": True, "must_change_password": False,
    "role": "admin", "twofa_method": None,
}


def _mock_token_db(join_row=TOKEN_JOIN_ROW, execute_ret=1):
    mock_db = MagicMock()
    mock_db.query_one.return_value = join_row
    mock_db.execute.return_value = execute_ret
    return mock_db


class TestCreateStoresHashOnly(unittest.TestCase):
    def test_plaintext_never_persisted(self):
        mock_db = MagicMock()
        captured = {}

        def fake_query_one(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return {"id": 9, "name": "bulk", "token_prefix": "sietch_xxxx",
                    "scope": "read", "created_by": 1, "created_at": None}

        mock_db.query_one.side_effect = fake_query_one
        with patch.object(_tokens, "db", mock_db):
            rec = _tokens.create_api_token("bulk", 1)
        plaintext = rec["token"]
        self.assertTrue(plaintext.startswith("sietch_"))
        stored = " ".join(str(p) for p in captured["params"])
        self.assertNotIn(plaintext, stored)
        self.assertIn(hashlib.sha256(plaintext.encode()).hexdigest(), stored)

    def test_blank_name_rejected(self):
        with patch.object(_tokens, "db", MagicMock()):
            with self.assertRaises(ValueError):
                _tokens.create_api_token("   ", 1)

    def test_unknown_scope_rejected(self):
        with patch.object(_tokens, "db", MagicMock()):
            with self.assertRaises(ValueError):
                _tokens.create_api_token("x", 1, scope="write")


class TestVerify(unittest.TestCase):
    def test_valid_token_returns_creator_user(self):
        with patch.object(_tokens, "db", _mock_token_db()):
            user = _tokens.verify_api_token("sietch_presented")
        self.assertEqual(user["id"], 1)
        self.assertTrue(user["is_admin"])
        self.assertEqual(user["api_token_id"], 3)
        self.assertEqual(user["api_token_name"], "bulk pull")

    def test_garbage_token_returns_none(self):
        mock_db = _mock_token_db(join_row=None)
        with patch.object(_tokens, "db", mock_db):
            self.assertIsNone(_tokens.verify_api_token("sietch_nope"))

    def test_wrong_scope_returns_none(self):
        row = dict(TOKEN_JOIN_ROW, scope="write")
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


class TestRequireAuthWithBearer(unittest.TestCase):
    def _auth(self, path, command="GET", bearer="sietch_good", session=None):
        headers = {}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        if session is not None:
            headers["Cookie"] = f"sietch_session={session}"
        shim = Shim(path=path, headers=headers, command=command)
        with patch.object(_server.auth_mod, "get_session_user",
                          return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token",
                          return_value=dict(TOKEN_JOIN_ROW, id=1, is_admin=True,
                                            api_token_id=3)):
            user = _server._require_auth(shim)
        return shim, user

    def test_get_data_route_works(self):
        shim, user = self._auth("/api/v2/projects?x=1")
        self.assertEqual(shim.status, None)  # no error sent
        self.assertEqual(user["id"], 1)

    def test_post_rejected(self):
        shim, user = self._auth("/api/v2/projects", command="POST")
        self.assertIsNone(user)
        self.assertEqual(shim.status, 403)

    def test_put_and_delete_rejected(self):
        for method in ("PUT", "DELETE", "PATCH"):
            shim, user = self._auth("/api/v2/projects/5", command=method)
            self.assertIsNone(user, method)
            self.assertEqual(shim.status, 403, method)

    def test_auth_routes_blocked(self):
        for path in ("/api/v2/auth/login", "/api/v2/auth/twofa/verify",
                     "/api/v2/auth/reset", "/api/v2/auth/logout"):
            shim, user = self._auth(path)
            self.assertIsNone(user, path)
            self.assertEqual(shim.status, 403, path)

    def test_user_management_blocked(self):
        for path in ("/api/v2/users", "/api/v2/users/1",
                     "/api/v2/users/1/avatar", "/api/v2/users/avatar-styles",
                     "/api/v2/me", "/api/v2/me/notification-prefs",
                     "/api/v2/my/avatar"):
            shim, user = self._auth(path)
            self.assertIsNone(user, path)
            self.assertEqual(shim.status, 403, path)

    def test_admin_routes_blocked(self):
        for path in ("/api/v2/admin/api-tokens", "/api/v2/admin/backup/status",
                     "/api/v2/admin/bot-settings"):
            shim, user = self._auth(path)
            self.assertIsNone(user, path)
            self.assertEqual(shim.status, 403, path)

    def test_garbage_token_401(self):
        shim = Shim(path="/api/v2/projects", headers={"Authorization": "Bearer junk"})
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token", return_value=None):
            user = _server._require_auth(shim)
        self.assertIsNone(user)
        self.assertEqual(shim.status, 401)

    def test_data_routes_not_blocked(self):
        for path in ("/api/v2/projects", "/api/v2/documents/1",
                     "/api/v2/history-attachments/5/download",
                     "/api/v2/mail/messages", "/api/v2/contacts"):
            self.assertFalse(_server._api_token_path_blocked(path), path)

    def test_session_cookie_still_works(self):
        shim = Shim(path="/api/v2/projects",
                    headers={"Cookie": "sietch_session=good"})
        with patch.object(_server.auth_mod, "get_session_user",
                          return_value={"id": 2, "is_admin": False}):
            user = _server._require_auth(shim)
        self.assertEqual(user["id"], 2)
        self.assertEqual(shim.status, None)


class TestBearerOnFileRoutes(unittest.TestCase):
    def _token_db(self, doc_row=None, att_row=None):
        mock_db = MagicMock()

        def fake_query_one(sql, params):
            if "FROM api_tokens" in sql:
                return dict(TOKEN_JOIN_ROW)
            if "FROM project_documents" in sql:
                return doc_row
            if "FROM history_attachments" in sql:
                return att_row
            return None

        mock_db.query_one.side_effect = fake_query_one
        mock_db.execute.return_value = 1
        return mock_db

    def test_document_download_with_bearer(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "docs").mkdir()
            (Path(tmp) / "docs" / "r.pdf").write_bytes(b"PDFDATA")
            doc_row = {"opportunity_id": 43, "title": "r.pdf",
                       "file_path": "docs/r.pdf", "file_size": 7,
                       "mime_type": "application/pdf"}
            shim = Shim(path="/api/v2/documents/1",
                        headers={"Authorization": "Bearer sietch_good"})
            patches = [patch.object(_server, "ROOT", Path(tmp)),
                       patch.object(_server.api_tokens_mod, "db", self._token_db()),
                       patch.object(_server, "db", self._token_db(doc_row=doc_row))]
            for p in patches:
                p.start()
            try:
                _server.KanbanHandler._handle_document_download(shim, 1)
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertEqual(shim.status, 200)
        self.assertEqual(shim.body, b"PDFDATA")

    def test_history_attachment_download_with_bearer(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "history-attachments" / "43" / "7"
            base.mkdir(parents=True)
            (base / "9_note.pdf").write_bytes(b"ATTDATA")
            att_row = {"filename": "note.pdf",
                       "file_path": "data/history-attachments/43/7/9_note.pdf",
                       "mime_type": "application/pdf", "file_size": 7}
            shim = Shim(path="/api/v2/history-attachments/9/download",
                        headers={"Authorization": "Bearer sietch_good"})
            patches = [patch.object(_server, "ROOT", Path(tmp)),
                       # history-attachment paths resolve off Path(__file__).parent
                       patch.object(_server, "__file__", str(Path(tmp) / "server.py")),
                       patch.object(_server.api_tokens_mod, "db", self._token_db()),
                       patch.object(_server, "db", self._token_db(att_row=att_row))]
            for p in patches:
                p.start()
            try:
                _server.KanbanHandler._handle_history_attachment_download(shim, 9)
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertEqual(shim.status, 200)
        self.assertEqual(shim.body, b"ATTDATA")

    def test_history_attachment_still_401_without_credential(self):
        shim = Shim(path="/api/v2/history-attachments/9/download", headers={})
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server, "db", MagicMock()):
            _server.KanbanHandler._handle_history_attachment_download(shim, 9)
        self.assertEqual(shim.status, 401)


class TestTokenManagementBlockedForTokens(unittest.TestCase):
    def _bearer_shim(self, path, command="GET", body=b""):
        return Shim(path=path, headers={"Authorization": "Bearer sietch_good"},
                    command=command, body=body)

    def test_list_refused_for_bearer(self):
        shim = self._bearer_shim("/api/v2/admin/api-tokens")
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token",
                          return_value={"id": 1, "is_admin": True}):
            _server.KanbanHandler._handle_api_tokens_list(shim)
        self.assertEqual(shim.status, 403)
        self.assertNotIn(b"bulk", shim.body)

    def test_create_refused_for_bearer(self):
        shim = self._bearer_shim("/api/v2/admin/api-tokens", command="POST",
                                 body=json.dumps({"name": "evil"}).encode())
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token",
                          return_value={"id": 1, "is_admin": True}), \
             patch.object(_server, "_read_body", lambda h: shim._req_body):
            _server.KanbanHandler._handle_api_token_create(shim)
        self.assertEqual(shim.status, 403)

    def test_create_works_for_admin_session(self):
        shim = Shim(path="/api/v2/admin/api-tokens", command="POST",
                    body=json.dumps({"name": "bulk"}).encode())
        mock_db = MagicMock()
        mock_db.query_one.return_value = {"id": 9, "name": "bulk",
                                          "token_prefix": "sietch_xxxx",
                                          "scope": "read", "created_by": 1,
                                          "created_at": None}
        admin = {"id": 1, "email": "a@b.c", "is_admin": True}
        with patch.object(_server, "_require_admin", return_value=admin), \
             patch.object(_server, "_read_body", lambda h: shim._req_body), \
             patch.object(_server.api_tokens_mod, "db", mock_db):
            _server.KanbanHandler._handle_api_token_create(shim)
        self.assertEqual(shim.status, 201)
        body = json.loads(shim.body.decode())
        self.assertIn("token", body)
        self.assertIn("warning", body)
        self.assertTrue(body["token"].startswith("sietch_"))

    def test_revoke_roundtrip(self):
        shim = Shim(path="/api/v2/admin/api-tokens/9/revoke", command="POST")
        admin = {"id": 1, "email": "a@b.c", "is_admin": True}
        mock_db = MagicMock()
        mock_db.execute.return_value = 1
        with patch.object(_server, "_require_admin", return_value=admin), \
             patch.object(_server.api_tokens_mod, "db", mock_db):
            _server.KanbanHandler._handle_api_token_revoke(shim, 9)
        self.assertEqual(shim.status, 200)
        mock_db.execute.return_value = 0
        shim2 = Shim(path="/api/v2/admin/api-tokens/9/revoke", command="POST")
        with patch.object(_server, "_require_admin", return_value=admin), \
             patch.object(_server.api_tokens_mod, "db", mock_db):
            _server.KanbanHandler._handle_api_token_revoke(shim2, 9)
        self.assertEqual(shim2.status, 404)


class TestTokenWiringGuards(unittest.TestCase):
    """Source guards: migration/init/inline DDL converge; routes stay admin-gated."""

    def test_migration_file_exists(self):
        mig = HERE.parent / "migrations" / "041_api_tokens.sql"
        self.assertTrue(mig.is_file())
        sql = mig.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS api_tokens", sql)
        self.assertIn("token_hash", sql)
        self.assertIn("revoked_at", sql)

    def test_init_sql_has_table(self):
        init = (HERE.parent / "init.sql").read_text(encoding="utf-8")
        self.assertIn("api_tokens", init)
        self.assertIn("token_hash", init)

    def test_inline_ddl_present(self):
        src = Path(_server.__file__).read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS api_tokens", src)

    def test_admin_token_routes_use_require_admin(self):
        src = Path(_server.__file__).read_text(encoding="utf-8")
        for route in ('"/api/v2/admin/api-tokens"',):
            idx = src.index(f"if api_path == {route}")
            region = src[idx:idx + 400]
            self.assertIn("user = _require_admin(self)", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
