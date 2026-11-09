#!/usr/bin/env python3
"""API tokens that "Post as" another user (migration 047).

An admin may point a token at a different user. The token then
AUTHENTICATES AS that user:

  * project activity / notes / every created_by column carry that user's id;
  * the token inherits that user's permissions (a member-identity token is
    NOT an admin, so admin routes stay closed to it);
  * the creator is still recorded, and minting the token is audited with
    actor + acted-as user;
  * writes are tagged with the token id so the UI can show
    "via API: <token name>".

Only admins may create a token for another user.

`import server` needs a reachable DB for its connection pool, so this suite
SKIPs cleanly without one.

Run with: python3 tests/test_api_token_acts_as.py
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import server as _server
    import api_tokens as at
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    _server = None
    at = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _server is None:
    print(f"SKIP: server/api_tokens not importable (no reachable database?): {_IMPORT_ERROR}")
    sys.exit(0)


ADMIN = {"id": 1, "email": "admin@example.com", "display_name": "Admin",
         "is_admin": True, "role": "admin"}
BAGGINS = {"id": 7, "email": "bilbo@example.com", "display_name": "Mr. Baggins",
           "first_name": "Bilbo", "last_name": "Baggins", "is_admin": False,
           "is_active": True, "role": "member"}
TOKEN_ROW = {
    "token_id": 3, "token_name": "llm agent", "scope": "full",
    "last_used_at": None, "expires_at": None, "restrictions": {},
    "acts_as_user_id": 7,
    "id": 7, "email": "bilbo@example.com", "display_name": "Mr. Baggins",
    "first_name": "Bilbo", "last_name": "Baggins",
    "is_admin": False, "must_change_password": False, "role": "member",
    "twofa_method": None,
    "owner_id": 1, "owner_email": "admin@example.com",
}


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


def _user_lookup(user=BAGGINS, active=True):
    row = dict(user)
    row["is_active"] = active
    return row


class TestResolveActsAsUser(unittest.TestCase):
    """Validation of the chosen identity (no HTTP involved)."""

    def test_default_is_the_creating_user(self):
        db = MagicMock()
        db.query_one.return_value = _user_lookup(dict(ADMIN, is_active=True))
        with patch.object(at, "db", db):
            who = at.resolve_acts_as_user(1, None)
        self.assertEqual(who["id"], 1)
        self.assertEqual(db.query_one.call_args.args[1], (1,))

    def test_blank_string_is_the_creating_user(self):
        db = MagicMock()
        db.query_one.return_value = _user_lookup(dict(ADMIN, is_active=True))
        with patch.object(at, "db", db):
            self.assertEqual(at.resolve_acts_as_user(1, "  ")["id"], 1)

    def test_explicit_active_user_is_accepted(self):
        db = MagicMock()
        db.query_one.return_value = _user_lookup()
        with patch.object(at, "db", db):
            who = at.resolve_acts_as_user(1, 7)
        self.assertEqual(who["id"], 7)
        self.assertEqual(who["display_name"], "Mr. Baggins")

    def test_unknown_user_is_refused(self):
        db = MagicMock()
        db.query_one.return_value = None
        with patch.object(at, "db", db):
            with self.assertRaises(ValueError):
                at.resolve_acts_as_user(1, 999)

    def test_inactive_user_is_refused(self):
        db = MagicMock()
        db.query_one.return_value = _user_lookup(active=False)
        with patch.object(at, "db", db):
            with self.assertRaises(ValueError):
                at.resolve_acts_as_user(1, 7)

    def test_non_numeric_is_refused(self):
        with patch.object(at, "db", MagicMock()):
            with self.assertRaises(ValueError):
                at.resolve_acts_as_user(1, "bilbo")


class TestCreateToken(unittest.TestCase):
    def _db(self, acts_as_row=None):
        """Mock DB: users lookup answers per requested id, INSERT returns a row."""
        admin_row = dict(ADMIN, is_active=True)
        baggins_row = _user_lookup()

        def _one(sql, params=None):
            if "FROM users" in sql:
                want = (params or [None])[0]
                if acts_as_row is not None:
                    return acts_as_row
                return baggins_row if want == 7 else admin_row
            return {
                "id": 3, "name": "llm agent", "token_prefix": "sietch_abcd",
                "scope": "full", "created_by": 1, "created_at": None,
                "expires_at": None, "restrictions": {},
                "acts_as_user_id": (params or [None])[-1],
            }

        db = MagicMock()
        db.query_one.side_effect = _one
        db.execute.return_value = 1
        return db

    def test_acts_as_is_stored_and_returned(self):
        db = self._db(_user_lookup())
        with patch.object(at, "db", db):
            rec = at.create_api_token("llm agent", 1, acts_as_user_id=7)
        insert_sql, insert_params = db.query_one.call_args_list[-1].args
        self.assertIn("acts_as_user_id", insert_sql)
        self.assertEqual(insert_params[-1], 7)
        self.assertEqual(rec["acts_as_user_id"], 7)
        self.assertEqual(rec["acts_as_email"], "bilbo@example.com")
        self.assertEqual(rec["created_by"], 1)
        self.assertTrue(rec["token"].startswith("sietch_"))

    def test_omitted_acts_as_defaults_to_creator(self):
        db = self._db()
        with patch.object(at, "db", db):
            rec = at.create_api_token("llm agent", 1)
        insert_params = db.query_one.call_args_list[-1].args[1]
        self.assertEqual(insert_params[-1], 1)
        self.assertEqual(rec["acts_as_user_id"], 1)

    def test_plaintext_is_never_stored(self):
        db = self._db(_user_lookup())
        with patch.object(at, "db", db):
            rec = at.create_api_token("llm agent", 1, acts_as_user_id=7)
        stored = [p for call in db.query_one.call_args_list for p in (call.args[1] or ())]
        self.assertNotIn(rec["token"], stored)


class TestVerifyReturnsTheActsAsIdentity(unittest.TestCase):
    def test_identity_is_the_acts_as_user_not_the_owner(self):
        db = MagicMock()
        db.query_one.return_value = dict(TOKEN_ROW)
        db.execute.return_value = 1
        with patch.object(at, "db", db):
            user = at.verify_api_token("sietch_secret")
        self.assertEqual(user["id"], 7)
        self.assertEqual(user["display_name"], "Mr. Baggins")
        self.assertFalse(user["is_admin"])
        self.assertEqual(user["api_token_owner_id"], 1)
        self.assertEqual(user["api_token_owner_email"], "admin@example.com")
        self.assertEqual(user["api_token_name"], "llm agent")
        self.assertEqual(user["api_token_acts_as_user_id"], 7)

    def test_owner_falls_back_to_identity_when_row_is_old(self):
        row = dict(TOKEN_ROW)
        row.pop("owner_id")
        row.pop("owner_email")
        db = MagicMock()
        db.query_one.return_value = row
        db.execute.return_value = 1
        with patch.object(at, "db", db):
            user = at.verify_api_token("sietch_secret")
        self.assertEqual(user["api_token_owner_id"], 7)
        self.assertEqual(user["api_token_owner_email"], "bilbo@example.com")

    def test_query_joins_the_acts_as_identity(self):
        db = MagicMock()
        db.query_one.return_value = dict(TOKEN_ROW)
        db.execute.return_value = 1
        with patch.object(at, "db", db):
            at.verify_api_token("sietch_secret")
        sql = db.query_one.call_args.args[0]
        self.assertIn("COALESCE(t.acts_as_user_id, t.created_by)", sql)


class TestWriteAudit(unittest.TestCase):
    def test_write_records_actor_and_acts_as(self):
        db = MagicMock()
        user = {
            "id": 7, "api_token_id": 3,
            "api_token_owner_id": 1, "api_token_acts_as_user_id": 7,
        }
        with patch.object(at, "db", db):
            at.log_token_write(user, "POST", "/api/v2/projects/5/history")
        sql, params = db.execute.call_args.args
        self.assertIn("actor_user_id", sql)
        self.assertIn("acts_as_user_id", sql)
        self.assertEqual(params, (3, "POST", "/api/v2/projects/5/history", 1, 7))

    def test_bare_token_id_still_works(self):
        db = MagicMock()
        with patch.object(at, "db", db):
            at.log_token_write(3, "PUT", "/api/v2/projects/1")
        params = db.execute.call_args.args[1]
        self.assertEqual(params[0], 3)
        self.assertIsNone(params[3])
        self.assertIsNone(params[4])

    def test_reads_are_not_audited(self):
        db = MagicMock()
        with patch.object(at, "db", db):
            at.log_token_write(3, "GET", "/api/v2/projects")
        db.execute.assert_not_called()

    def test_create_audit_records_who_and_for_whom(self):
        db = MagicMock()
        rec = {"id": 3, "name": "llm agent", "scope": "full",
               "restrictions": {}, "acts_as_user_id": 7,
               "acts_as_email": "bilbo@example.com"}
        with patch.object(at, "db", db):
            at.log_token_create(rec, actor_user_id=1, actor_email="admin@example.com")
        sql, params = db.execute.call_args.args
        self.assertIn("TOKEN_CREATE", sql)
        self.assertEqual(params[0], 3)
        self.assertEqual(params[1], 1)
        self.assertEqual(params[2], 7)
        detail = json.loads(params[3])
        self.assertEqual(detail["created_by"], 1)
        self.assertEqual(detail["acts_as_user_id"], 7)
        self.assertEqual(detail["created_by_email"], "admin@example.com")


class TestCreateEndpointPermissions(unittest.TestCase):
    """POST /api/v2/admin/api-tokens — admin-only, and audited."""

    def _post(self, session_user, payload, check_admin=True):
        shim = Shim(path="/api/v2/admin/api-tokens", command="POST",
                    headers={"Cookie": "sietch_session=good"},
                    body=json.dumps(payload).encode())
        db = MagicMock()
        db.query_one.side_effect = lambda sql, params=None: (
            _user_lookup() if "FROM users" in sql else {
                "id": 3, "name": payload.get("name", "t"), "token_prefix": "sietch_x",
                "scope": "full", "created_by": session_user["id"],
                "created_at": None, "expires_at": None, "restrictions": {},
                "acts_as_user_id": (params or [None])[-1],
            }
        )
        db.execute.return_value = 1
        audited = MagicMock()
        patches = [
            patch.object(_server.auth_mod, "get_session_user", return_value=session_user),
            patch.object(_server, "db", db),
            patch.object(_server, "_read_body", lambda h: h._req_body),
            patch.object(at, "db", db),
            patch.object(at, "log_token_create", audited),
        ]
        if not check_admin:
            # Bypass the admin gate so the handler's OWN identity rule is the
            # only thing standing between a member and another user's token.
            patches.append(patch.object(_server, "_require_admin",
                                        side_effect=lambda h: session_user))
        for p in patches:
            p.start()
        try:
            _server.KanbanHandler._handle_api_token_create(shim)
        finally:
            for p in reversed(patches):
                p.stop()
        return shim, db, audited

    def test_admin_can_create_a_token_for_another_user(self):
        shim, db, audited = self._post(ADMIN, {"name": "baggins-bot", "acts_as_user_id": 7})
        self.assertEqual(shim.status, 201)
        audited.assert_called_once()
        rec = audited.call_args.args[0]
        self.assertEqual(rec["acts_as_user_id"], 7)

    def test_admin_can_create_a_token_for_themselves(self):
        shim, _, audited = self._post(ADMIN, {"name": "self-bot"})
        self.assertEqual(shim.status, 201)
        audited.assert_called_once()

    def test_non_admin_cannot_reach_the_endpoint(self):
        shim, db, audited = self._post(
            dict(MEMBER_USER, is_admin=False), {"name": "nope", "acts_as_user_id": 7})
        self.assertEqual(shim.status, 403)
        audited.assert_not_called()
        self.assertEqual(db.query_one.call_count, 0)

    def test_non_admin_identity_cannot_name_another_user(self):
        """Defence in depth: even if the admin gate is bypassed, the explicit
        rules still refuse a non-admin minting a token for somebody else."""
        shim, db, audited = self._post(
            dict(MEMBER_USER, is_admin=False),
            {"name": "sneaky", "acts_as_user_id": 7}, check_admin=False)
        self.assertEqual(shim.status, 403)
        audited.assert_not_called()

    def test_unknown_user_is_a_400(self):
        shim = Shim(path="/api/v2/admin/api-tokens", command="POST",
                    headers={"Cookie": "sietch_session=good"},
                    body=json.dumps({"name": "x", "acts_as_user_id": 999}).encode())
        db = MagicMock()
        db.query_one.return_value = None
        with patch.object(_server.auth_mod, "get_session_user", return_value=ADMIN), \
             patch.object(_server, "db", db), \
             patch.object(_server, "_read_body", lambda h: h._req_body), \
             patch.object(at, "db", db):
            _server.KanbanHandler._handle_api_token_create(shim)
        self.assertEqual(shim.status, 400)

    def test_malformed_acts_as_is_a_400(self):
        shim, _, audited = self._post(ADMIN, {"name": "x", "acts_as_user_id": "bilbo"})
        self.assertEqual(shim.status, 400)
        audited.assert_not_called()


MEMBER_USER = {"id": 2, "email": "m@example.com", "display_name": "Member",
               "is_admin": False, "role": "member"}


class TestAttributionColumns(unittest.TestCase):
    """Writes carry api_token_id so the UI can render the tag."""

    def test_history_insert_persists_the_token_id(self):
        src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        at_idx = src.index("def _handle_project_history_create")
        body = src[at_idx:src.find("\n    def ", at_idx + 10)]
        self.assertIn("api_token_id", body)
        self.assertIn("INSERT INTO history_events", body)
        self.assertIn('user.get("api_token_id")', body)

    def test_history_read_returns_the_token_name(self):
        src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        at_idx = src.index("def _handle_project_history_get")
        body = src[at_idx:src.find("\n    def ", at_idx + 10)]
        self.assertIn("LEFT JOIN api_tokens t ON t.id = h.api_token_id", body)
        self.assertIn('"apiTokenName"', body)

    def test_note_insert_and_read_carry_the_token(self):
        src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        create_idx = src.index("def _handle_note_create")
        create = src[create_idx:src.find("\n    def ", create_idx + 10)]
        self.assertIn("api_token_id", create)
        self.assertIn("INSERT INTO personal_notes", create)
        # list handler resolves names in one query
        self.assertIn("FROM api_tokens WHERE id = ANY(%s)", src)
        self.assertIn('r["api_token_name"]', src)

    def test_migration_047_exists_and_is_additive(self):
        sql = (HERE.parent / "migrations" / "047_api_token_acts_as.sql").read_text(encoding="utf-8")
        self.assertIn("acts_as_user_id", sql)
        self.assertIn("api_token_id", sql)
        self.assertIn("api_token_audit_log", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("DELETE FROM", sql)

    def test_startup_ddl_mirrors_the_migration(self):
        src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        self.assertIn("Migration 047", src)
        self.assertIn("idx_history_events_api_token", src)
        self.assertIn("idx_personal_notes_api_token", src)


class TestRestrictionsUnchanged(unittest.TestCase):
    """Full access by default + optional restrictions must not regress."""

    def test_full_identity_token_still_has_full_access(self):
        user = {"id": 7, "api_token_scope": "full", "api_token_restrictions": {}}
        ok, err = at.request_allowed(user, "POST", "/api/v2/projects/1/history")
        self.assertTrue(ok, err)

    def test_member_identity_is_not_an_admin(self):
        """The acts-as identity carries its own is_admin — a member token
        cannot reach admin routes even though an admin minted it."""
        user = {"id": 7, "is_admin": False, "api_token_scope": "full",
                "api_token_restrictions": {}}
        self.assertFalse(user["is_admin"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
