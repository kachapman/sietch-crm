#!/usr/bin/env python3
"""Unit tests for the mail-surface auth gap closure (follow-up).

Dozens of /api/v2/mail/* JSON endpoints served without any login check —
including writes (tag/template/contact/folder/contractor/rule CRUD,
reprocess, retrain, send-undo, config PUT). They now use the same auth as
the other /api/v2 routes: _require_auth, except scanner config + log which
are admin-only (for sessions AND api tokens).

Covers:
  - every newly gated handler: unauthenticated -> 401
  - logged-in session works (representative handlers, mocked deps)
  - non-admin session on config/log -> 403
  - Bearer token: allowed on tags/status (normal GET rules), 403 on
    config/log from a non-admin token, 200 from an admin token, 403 on
    any write
  - source guards pinning the gate in each handler body, and frontend proof
    that the logged-in UI calls these routes with cookies (api() uses
    credentials: same-origin)

db is mocked (no Postgres). `import server` still needs a reachable DB for
its connection pool, so like the other server-importing suites this file
SKIPs cleanly without one.

Run with: python3 tests/test_mail_auth_gaps.py
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


ADMIN = {"id": 1, "email": "a@b.c", "is_admin": True}
MEMBER = {"id": 2, "email": "m@b.c", "is_admin": False}


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


def _patches(session_user, mock_db=None, mock_scanner=None):
    ps = [patch.object(_server.auth_mod, "get_session_user", return_value=session_user)]
    ps.append(patch.object(_server, "db", mock_db or MagicMock()))
    if mock_scanner is not None:
        ps.append(patch.object(_server, "mail_scanner", mock_scanner))
    ps.append(patch.object(_server, "_read_body", lambda h: h._req_body))
    return ps


def _run(patches, fn, *args):
    for p in patches:
        p.start()
    try:
        fn(*args)
    finally:
        for p in reversed(patches):
            p.stop()


# Handler -> (ctor args, http command, request body, mock-db setup)
def _db_for(name):
    db = MagicMock()
    db.query.return_value = []
    db.query_dicts.return_value = []
    db.execute.return_value = 1
    if "tags" in name and "tag" in name:
        db.query_one.return_value = {"id": 1}
    elif "template" in name:
        db.query_one.return_value = {"id": 1}
    elif "contact" in name:
        db.query_one.return_value = {"id": 1}
    elif "folder" in name and "folders" not in name:
        db.query_one.return_value = {"name": "x", "is_system": False}
    elif "contractor" in name and "contractors" not in name:
        db.query_one.return_value = {"id": 1}
    elif "rule" in name and "rules" not in name:
        db.query_one.return_value = {"id": 1}
    return db


CASES = [
    # (handler, args, command, body-dict-or-None)
    ("_handle_mail_tags", (), "GET", None),
    ("_handle_mail_tag_get", (1,), "GET", None),
    ("_handle_mail_tag_update", (1,), "PUT", {"title": "t"}),
    ("_handle_mail_tag_delete", (1,), "DELETE", None),
    ("_handle_mail_templates", (), "GET", None),
    ("_handle_mail_template_get", (1,), "GET", None),
    ("_handle_mail_template_update", (1,), "PUT", {"title": "t"}),
    ("_handle_mail_template_delete", (1,), "DELETE", None),
    ("_handle_mail_status", (), "GET", None),
    ("_handle_mail_log", (), "GET", None),
    ("_handle_mail_config", (), "GET", None),
    ("_handle_mail_config_put", (), "PUT", {"a": 1}),
    ("_handle_mail_contact_get", (1,), "GET", None),
    ("_handle_mail_contact_update", (1,), "PUT", {"name": "n"}),
    ("_handle_mail_contact_delete", (1,), "DELETE", None),
    ("_handle_mail_folders", (), "GET", None),
    ("_handle_mail_folder_update", (1,), "PUT", {"name": "n"}),
    ("_handle_mail_folder_delete", (1,), "DELETE", None),
    ("_handle_mail_contractors", (), "GET", None),
    ("_handle_mail_contractor_get", (1,), "GET", None),
    ("_handle_mail_contractor_create", (), "POST", {"name": "n"}),
    ("_handle_mail_contractor_update", (1,), "PUT", {"name": "n"}),
    ("_handle_mail_contractor_delete", (1,), "DELETE", None),
    ("_handle_mail_classification_rules", (), "GET", None),
    ("_handle_mail_classification_rule_get", (1,), "GET", None),
    ("_handle_mail_classification_rule_create", (), "POST",
     {"rule_name": "r", "rule_type": "subject_contains", "pattern": "x"}),
    ("_handle_mail_classification_rule_update", (1,), "PUT", {"rule_name": "r"}),
    ("_handle_mail_classification_rule_delete", (1,), "DELETE", None),
    ("_handle_mail_feedback", (), "GET", None),
    ("_handle_mail_reprocess", (), "POST", {"entries": []}),
    ("_handle_mail_retrain", (), "POST", {}),
    ("_handle_mail_send_undo", (), "POST", {"id": 1}),
]

ADMIN_ONLY = {"_handle_mail_log", "_handle_mail_config", "_handle_mail_config_put"}


class TestMailAuthGapsUnauthenticated(unittest.TestCase):
    def test_every_open_handler_returns_401_without_session(self):
        for name, args, command, payload in CASES:
            with self.subTest(handler=name):
                body = b"" if payload is None else json.dumps(payload).encode()
                shim = Shim(path="/api/v2/mail/x", headers={}, command=command, body=body)
                scanner = MagicMock()
                scanner.get_scanner_status.return_value = {"enabled": False}
                scanner.get_scanner_log.return_value = []
                scanner.get_contractors.return_value = {}
                scanner.get_feedback_entries.return_value = []
                scanner.reprocess_conversations.return_value = []
                scanner.retrain_classifier_head.return_value = {"ok": True}
                scanner.update_contractors.side_effect = lambda p: p
                fn = getattr(_server.KanbanHandler, name)
                _run(_patches(None, _db_for(name), scanner), fn, shim, *args)
                self.assertEqual(shim.status, 401, name)


class TestMailAuthGapsLoggedIn(unittest.TestCase):
    def _authed(self, name, args, command, payload, user):
        body = b"" if payload is None else json.dumps(payload).encode()
        shim = Shim(path="/api/v2/mail/x", headers={"Cookie": "sietch_session=good"},
                    command=command, body=body)
        scanner = MagicMock()
        scanner.get_scanner_status.return_value = {"enabled": True}
        scanner.get_scanner_log.return_value = []
        scanner.get_contractors.return_value = {"contractors": []}
        scanner.get_feedback_entries.return_value = []
        scanner.reprocess_conversations.return_value = []
        scanner.retrain_classifier_head.return_value = {"ok": True}
        scanner.update_contractors.side_effect = lambda p: p
        scanner._is_valid_regex.return_value = True
        fn = getattr(_server.KanbanHandler, name)
        _run(_patches(user, _db_for(name), scanner), fn, shim, *args)
        return shim

    def test_member_session_reaches_normal_handlers(self):
        for name, args, command, payload in CASES:
            if name in ADMIN_ONLY:
                continue
            with self.subTest(handler=name):
                shim = self._authed(name, args, command, payload, MEMBER)
                self.assertNotEqual(shim.status, 401, name)
                self.assertIn(shim.status, (200, 201), name)

    def test_member_session_reaches_status_and_tags(self):
        for name in ("_handle_mail_status", "_handle_mail_tags",
                     "_handle_mail_templates", "_handle_mail_folders",
                     "_handle_mail_contractors", "_handle_mail_classification_rules",
                     "_handle_mail_feedback"):
            with self.subTest(handler=name):
                case = next(c for c in CASES if c[0] == name)
                shim = self._authed(case[0], case[1], case[2], case[3], MEMBER)
                self.assertIn(shim.status, (200, 201), name)

    def test_non_admin_forbidden_on_config_and_log(self):
        for name, args, command, payload in [
                ("_handle_mail_config", (), "GET", None),
                ("_handle_mail_log", (), "GET", None),
                ("_handle_mail_config_put", (), "PUT", {"a": 1})]:
            with self.subTest(handler=name):
                shim = self._authed(name, args, command, payload, MEMBER)
                self.assertEqual(shim.status, 403, name)

    def test_admin_session_reaches_config_and_log(self):
        scanner = MagicMock()
        scanner.get_contractors.return_value = {"contractors": []}
        scanner.get_scanner_log.return_value = []
        scanner.update_contractors.side_effect = lambda p: p
        shim = Shim(path="/api/v2/mail/config", headers={"Cookie": "sietch_session=good"})
        _run(_patches(ADMIN, MagicMock(), scanner),
             _server.KanbanHandler._handle_mail_config, shim)
        self.assertEqual(shim.status, 200)
        shim = Shim(path="/api/v2/mail/log", headers={"Cookie": "sietch_session=good"})
        _run(_patches(ADMIN, MagicMock(), scanner),
             _server.KanbanHandler._handle_mail_log, shim)
        self.assertEqual(shim.status, 200)


class TestMailAuthGapsTokens(unittest.TestCase):
    def _bearer(self, path, command="GET", body=b""):
        return Shim(path=path, headers={"Authorization": "Bearer sietch_good"},
                    command=command, body=body)

    def test_token_allowed_on_tags_and_status(self):
        for path, handler, args in [
                ("/api/v2/mail/tags", "_handle_mail_tags", ()),
                ("/api/v2/mail/status", "_handle_mail_status", ())]:
            with self.subTest(path=path):
                shim = self._bearer(path)
                user = dict(ADMIN, api_token_id=9, api_token_name="t")
                scanner = MagicMock()
                scanner.get_scanner_status.return_value = {"enabled": True}
                with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
                     patch.object(_server.api_tokens_mod, "verify_api_token", return_value=user), \
                     patch.object(_server, "db", _db_for(handler)), \
                     patch.object(_server, "mail_scanner", scanner):
                    getattr(_server.KanbanHandler, handler)(shim, *args)
                self.assertEqual(shim.status, 200, path)

    def test_non_admin_token_blocked_on_config_and_log(self):
        """Admin-only routes stay admin-only for tokens too.

        Commit 6d65ea5 made API tokens act as their creating admin across
        /api/v2 (optional per-token restrictions replace the old global path
        blocklist). The gate that must not move is `_require_admin`: a token
        belonging to a NON-admin gets 403 here, exactly like that user's
        session would.
        """
        for path, handler, args in [
                ("/api/v2/mail/config", "_handle_mail_config", ()),
                ("/api/v2/mail/log", "_handle_mail_log", ())]:
            with self.subTest(path=path):
                shim = self._bearer(path)
                user = dict(MEMBER, api_token_id=9, api_token_name="t")
                scanner = MagicMock()
                with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
                     patch.object(_server.api_tokens_mod, "verify_api_token", return_value=user), \
                     patch.object(_server, "db", MagicMock()), \
                     patch.object(_server, "mail_scanner", scanner):
                    getattr(_server.KanbanHandler, handler)(shim, *args)
                    # Rejected by _require_admin before any scanner data is read.
                    self.assertEqual(shim.status, 403, path)
                    self.assertEqual(scanner.get_contractors.call_count, 0)
                    self.assertEqual(scanner.get_scanner_log.call_count, 0)

    def test_admin_token_reaches_config_and_log(self):
        """A token minted by an admin has the admin's access (6d65ea5)."""
        for path, handler, args, mock_name in [
                ("/api/v2/mail/config", "_handle_mail_config", (), "get_contractors"),
                ("/api/v2/mail/log", "_handle_mail_log", (), "get_scanner_log")]:
            with self.subTest(path=path):
                shim = self._bearer(path)
                user = dict(ADMIN, api_token_id=9, api_token_name="t")
                scanner = MagicMock()
                getattr(scanner, mock_name).return_value = {"ok": True}
                with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
                     patch.object(_server.api_tokens_mod, "verify_api_token", return_value=user), \
                     patch.object(_server, "db", _db_for(handler)), \
                     patch.object(_server, "mail_scanner", scanner):
                    getattr(_server.KanbanHandler, handler)(shim, *args)
                self.assertEqual(shim.status, 200, path)

    def test_token_writes_are_audited_and_restrictions_apply(self):
        """Tokens may write (6d65ea5), but every write is audit-logged and a
        restricted token is still refused by `request_allowed`."""
        shim = self._bearer("/api/v2/mail/tags/1", command="PUT",
                            body=json.dumps({"title": "t"}).encode())
        user = dict(ADMIN, api_token_id=9, api_token_name="t")
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token", return_value=user), \
             patch.object(_server.api_tokens_mod, "request_allowed",
                          return_value=(True, None)), \
             patch.object(_server.api_tokens_mod, "log_token_write") as audited, \
             patch.object(_server, "db", MagicMock()), \
             patch.object(_server, "_read_body", lambda h: h._req_body):
            _server.KanbanHandler._handle_mail_tag_update(shim, 1)
        self.assertNotEqual(shim.status, 401)
        audited.assert_called_once()
        self.assertEqual(audited.call_args.args[0], 9)

    def test_restricted_token_write_is_refused(self):
        shim = self._bearer("/api/v2/mail/tags/1", command="PUT",
                            body=json.dumps({"title": "t"}).encode())
        user = dict(ADMIN, api_token_id=9, api_token_name="t")
        db = MagicMock()
        with patch.object(_server.auth_mod, "get_session_user", return_value=None), \
             patch.object(_server.api_tokens_mod, "verify_api_token", return_value=user), \
             patch.object(_server.api_tokens_mod, "request_allowed",
                          return_value=(False, "API token is read-only")), \
             patch.object(_server, "db", db), \
             patch.object(_server, "_read_body", lambda h: h._req_body):
            _server.KanbanHandler._handle_mail_tag_update(shim, 1)
        self.assertEqual(shim.status, 403)
        self.assertEqual(db.execute.call_count, 0, "refused write must not reach the DB")


class TestMailAuthGapsGuards(unittest.TestCase):
    """Source guards: every closed handler keeps its single gate."""

    SRC = Path(_server.__file__).read_text(encoding="utf-8")

    def test_all_handlers_still_gated(self):
        for name, _, _, _ in CASES:
            with self.subTest(handler=name):
                start = self.SRC.index(f"def {name}(")
                body = self.SRC[start:start + 2500]
                gate_end = body.index("\n", body.index(f"def {name}("))
                head = body[:600]
                if name in ADMIN_ONLY:
                    self.assertIn("user = _require_admin(self)", head, name)
                else:
                    self.assertIn("user = _require_auth(self)", head, name)

    def test_config_and_log_routes_are_admin_gated(self):
        """Both routes keep the admin gate (tokens inherit it — 6d65ea5)."""
        for name in ("_handle_mail_config", "_handle_mail_log"):
            with self.subTest(handler=name):
                start = self.SRC.index(f"def {name}(")
                nxt = self.SRC.find("\n    def ", start + 10)
                body = self.SRC[start:nxt]
                self.assertIn("user = _require_admin(self)", body)
                self.assertNotIn("user = _require_auth(self)", body)

    def test_frontend_calls_these_routes_with_cookies(self):
        app = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        for route in ("/api/v2/mail/tags", "/api/v2/mail/status",
                      "/api/v2/mail/config", "/api/v2/mail/templates",
                      "/api/v2/mail/contacts"):
            self.assertIn(route, app)
        # api() sends the session cookie, so logged-in flows are unaffected.
        self.assertIn('credentials: "same-origin"', app)

    def test_no_handler_grew_a_second_gate(self):
        for name, _, _, _ in CASES:
            with self.subTest(handler=name):
                start = self.SRC.index(f"def {name}(")
                nxt = self.SRC.find("\n    def ", start + 10)
                body = self.SRC[start:nxt]
                gates = (body.count("user = _require_auth(self)") +
                         body.count("user = _require_admin(self)"))
                self.assertEqual(gates, 1, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
