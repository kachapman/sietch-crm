#!/usr/bin/env python3
"""Auth gates on the /api/v2/mail/templates surface.

The mail-template endpoints are shared-team data: every logged-in user may
read and manage templates (`_require_auth`, not `_require_admin`). What must
never happen is an ANONYMOUS request reaching the database — and that is
exactly what `_handle_mail_template_create` did: it checked the body first,
called `_require_auth()` afterwards, and then carried on regardless, so an
unauthenticated POST was answered 401 while the template row was still
INSERTed (with `created_by` NULL).

These tests pin:
  1. all five handlers answer 401 and touch no DB for an anonymous caller,
  2. `create` refuses before parsing the body (no `_read_body` call),
  3. a logged-in member can list/create (shared templates),
  4. source guards: one gate per handler, and it is the first statement.

`import server` needs a reachable DB for its connection pool, so like the
other server-importing suites this file SKIPs cleanly without one.

Run with: python3 tests/test_mail_template_auth.py
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
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    _server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    sys.exit(0)


MEMBER = {"id": 2, "email": "m@b.c", "is_admin": False}

HANDLERS = [
    # (handler, args, http command, body)
    ("_handle_mail_templates", (), "GET", None),
    ("_handle_mail_template_get", (1,), "GET", None),
    ("_handle_mail_template_create", (), "POST", {"title": "Estimate intro"}),
    ("_handle_mail_template_update", (1,), "PUT", {"title": "renamed"}),
    ("_handle_mail_template_delete", (1,), "DELETE", None),
]


class Shim:
    """Minimal stand-in for the BaseHTTPRequestHandler surface these use."""

    def __init__(self, path="/", headers=None, command="GET", body=b"",
                 session="sietch_session=good"):
        self.path = path
        self.headers = headers if headers is not None else {"Cookie": session}
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


def _db(template=True):
    db = MagicMock()
    db.query_dicts.return_value = []
    db.query.return_value = []
    db.query_one.return_value = {"id": 1} if template else None
    db.execute.return_value = 1
    return db


def _run(patched, fn, *args):
    for p in patched:
        p.start()
    try:
        return fn(*args)
    finally:
        for p in reversed(patched):
            p.stop()


def _anon(handler, args, command, body):
    # No Cookie header at all: a genuinely anonymous caller.
    shim = Shim(path="/api/v2/mail/templates", command=command,
                headers={}, body=json.dumps(body or {}).encode())
    db = _db()
    read = MagicMock(side_effect=lambda h: h._req_body)
    _run([
        patch.object(_server.auth_mod, "get_session_user", return_value=None),
        patch.object(_server, "db", db),
        patch.object(_server, "_read_body", read),
    ], getattr(_server.KanbanHandler, handler), shim, *args)
    return shim, db, read


class TestAnonymousIsRefused(unittest.TestCase):
    def test_every_template_handler_401s_without_a_session(self):
        for handler, args, command, body in HANDLERS:
            with self.subTest(handler=handler):
                shim, _, _ = _anon(handler, args, command, body)
                self.assertEqual(shim.status, 401, handler)

    def test_no_database_write_on_anonymous_requests(self):
        """401 must not come with a side effect the caller did not earn."""
        for handler, args, command, body in HANDLERS:
            with self.subTest(handler=handler):
                _, db, _ = _anon(handler, args, command, body)
                self.assertEqual(db.query.call_count, 0, handler)
                self.assertEqual(db.query_dicts.call_count, 0, handler)
                self.assertEqual(db.query_one.call_count, 0, handler)
                self.assertEqual(db.execute.call_count, 0, handler)

    def test_create_checks_auth_before_reading_the_body(self):
        """The body is attacker-controlled: refuse before parsing it."""
        _, _, read = _anon("_handle_mail_template_create", (), "POST",
                           {"title": "Estimate intro"})
        self.assertEqual(read.call_count, 0)


class TestMemberCanManageTemplates(unittest.TestCase):
    """Templates are shared with the whole team, not admin-only."""

    def test_member_lists_templates(self):
        shim = Shim(path="/api/v2/mail/templates?page=1")
        db = _db()
        db.query_dicts.return_value = [{"id": 1, "title": "Estimate intro"}]
        _run([
            patch.object(_server.auth_mod, "get_session_user", return_value=MEMBER),
            patch.object(_server, "db", db),
        ], _server.KanbanHandler._handle_mail_templates, shim)
        self.assertEqual(shim.status, 200)

    def test_member_creates_template_attributed_to_them(self):
        shim = Shim(path="/api/v2/mail/templates", command="POST",
                    body=json.dumps({"title": "Storm checklist"}).encode())
        db = _db()
        _run([
            patch.object(_server.auth_mod, "get_session_user", return_value=MEMBER),
            patch.object(_server, "db", db),
            patch.object(_server, "_read_body", lambda h: h._req_body),
        ], _server.KanbanHandler._handle_mail_template_create, shim)
        self.assertEqual(shim.status, 201)
        sql, params = db.query_one.call_args.args
        self.assertIn("INSERT INTO mail_templates", sql)
        # created_by is the session user, never NULL and never caller-supplied.
        self.assertEqual(params[-1], MEMBER["id"])

    def test_title_is_required_after_auth(self):
        shim = Shim(path="/api/v2/mail/templates", command="POST",
                    body=json.dumps({"title": "   "}).encode())
        db = _db()
        _run([
            patch.object(_server.auth_mod, "get_session_user", return_value=MEMBER),
            patch.object(_server, "db", db),
            patch.object(_server, "_read_body", lambda h: h._req_body),
        ], _server.KanbanHandler._handle_mail_template_create, shim)
        self.assertEqual(shim.status, 400)
        self.assertEqual(db.query_one.call_count, 0)


class TestTemplateAuthGuards(unittest.TestCase):
    """Source guards: one gate per handler, and it is unconditional."""

    SRC = Path(_server.__file__).read_text(encoding="utf-8")

    def _body(self, name):
        start = self.SRC.index(f"def {name}(")
        nxt = self.SRC.find("\n    def ", start + 10)
        return self.SRC[start:] if nxt == -1 else self.SRC[start:nxt]

    def test_each_handler_gates_with_require_auth(self):
        for handler, _, _, _ in HANDLERS:
            with self.subTest(handler=handler):
                body = self._body(handler)
                self.assertIn("user = _require_auth(self)", body)
                self.assertEqual(body.count("_require_auth(self)"), 1,
                                 "a second, divergent gate must not appear")
                self.assertNotIn("_require_admin(self)", body)

    def test_gate_returns_and_precedes_the_first_side_effect(self):
        for handler, _, _, _ in HANDLERS:
            with self.subTest(handler=handler):
                body = self._body(handler)
                gate = body.index("user = _require_auth(self)")
                self.assertIn("if not user:\n            return", body[gate:gate + 120],
                              "the gate must return, not fall through")
                for effect in ("_read_body(", "db.query", "db.execute"):
                    at = body.find(effect)
                    if at != -1:
                        self.assertLess(gate, at,
                                        f"{effect} must come after the gate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
