#!/usr/bin/env python3
"""Route + approval tests for the Phase 1 reconciler.

Two layers:

* **Route tests** (no DB): a minimal handler shim drives the real
  ``KanbanHandler`` methods with the session lookup, ``db`` and the service
  patched.  They pin authentication, Admin-only posting, limited-user scoping
  and the 409 returned when the tie-out gate blocks a run.
* **DB-backed approval tests** (skipped without a reachable PostgreSQL): the
  real service posts a real history event, removes only the "Needs
  reconciliation" tag, notifies the run creator, audits, and is idempotent.

``import server`` opens a connection pool, so like the other server-importing
suites this file prints SKIP and exits 0 when no database is reachable.

Run with: python3 tests/test_reconciler_routes.py
"""
from __future__ import annotations

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
    import reconciler_service as _service
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    _server = None
    _service = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    sys.exit(0)


SLOTS = ("prior", "revised", "our")
DOC_IDS = {"prior": 11, "revised": 12, "our": 13}
DOC_TITLES = {"prior": "prior.pdf", "revised": "revised.pdf", "our": "our.pdf"}


class Shim:
    """Minimal KanbanHandler stand-in: headers/path/command + response capture."""

    def __init__(self, path="/", headers=None, command="POST", body=b"", session=True):
        self.path = path
        headers = dict(headers or {})
        if session:
            headers.setdefault("Cookie", "sietch_session=test")
        if body:
            headers.setdefault("Content-Length", str(len(body)))
        self.headers = headers
        self.command = command
        self._req_body = body
        self.status = None
        self.sent = {}
        self.body = b""
        self.api_path = path.split("?")[0]

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

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8") or "{}")
        except ValueError:
            return {}


def _user(is_admin=False, uid=7, role="member"):
    return {"id": uid, "is_admin": is_admin, "role": role,
            "email": f"u{uid}@example.test", "display_name": f"User {uid}"}


# The handlers call two helpers through ``self``; the real methods only use
# module globals (db / ROOT) so they work unchanged with the Shim as ``self``.
Shim._reconciler_doc = _server.KanbanHandler._reconciler_doc
Shim._reconciler_scoped = _server.KanbanHandler._reconciler_scoped


def _patch_auth(user):
    return patch.object(_server.auth_mod, "get_session_user", return_value=user)


def _patch_service(**attrs):
    mock = MagicMock()
    mock.SLOTS = SLOTS
    for k, v in attrs.items():
        setattr(mock, k, v)
    return mock


def _run(shim, handler, *args, patches=()):
    for p in patches:
        p.start()
    try:
        getattr(_server.KanbanHandler, handler)(shim, *args)
    finally:
        for p in reversed(patches):
            p.stop()
    return shim


def _create_body():
    return json.dumps({
        "projectId": 118,
        "documents": {
            slot: {"kind": "doc", "id": DOC_IDS[slot]} for slot in SLOTS
        },
    }).encode()


def _doc_row(doc_id):
    for slot, did in DOC_IDS.items():
        if did == doc_id:
            return {"id": did, "title": DOC_TITLES[slot],
                    "file_path": f"docs/{DOC_TITLES[slot]}"}
    return None


def _db_stub():
    mock = MagicMock()

    def query_one(sql, params=None):
        text = str(sql)
        if "FROM project_documents" in text:
            return _doc_row((params or (0,))[0])
        if "FROM opportunities" in text:
            return {"id": 118}
        return None

    mock.query_one.side_effect = query_one
    return mock


class TestReconcilerRoutes(unittest.TestCase):
    def test_create_requires_auth(self):
        shim = Shim("/api/v2/reconciler/runs", body=_create_body())
        service = _patch_service()
        shim = _run(
            shim, "_handle_reconciler_run_create",
            patches=[_patch_auth(None), patch.object(_server, "reconciler_service", service)],
        )
        self.assertEqual(shim.status, 401)
        service.build_run.assert_not_called()

    def test_create_resolves_docs_and_drafts(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "docs").mkdir()
            for title in DOC_TITLES.values():
                (Path(tmp) / "docs" / title).write_bytes(b"%PDF-1.4 test")
            built = {"status": "drafted", "gateAllowed": True, "draftNote": "Note",
                     "billingSummary": [], "inputs": {}, "result": {"parserVersion": "0.3.0"}}
            created = {"id": 5, "projectId": 118, "status": "drafted"}
            service = _patch_service(
                build_run=MagicMock(return_value=built),
                create_run=MagicMock(return_value=created),
            )
            shim = Shim("/api/v2/reconciler/runs", body=_create_body())
            shim = _run(
                shim, "_handle_reconciler_run_create",
                patches=[
                    _patch_auth(_user()),
                    patch.object(_server, "db", _db_stub()),
                    patch.object(_server, "ROOT", Path(tmp)),
                    patch.object(_server, "reconciler_service", service),
                ],
            )
            self.assertEqual(shim.status, 201, shim.body)
            self.assertEqual(shim.json()["run"]["id"], 5)
            docs = service.build_run.call_args[0][1]
            self.assertEqual(set(docs), set(SLOTS))
            self.assertEqual(docs["prior"]["docRef"], "doc:11")
            service.create_run.assert_called_once()

    def test_create_rejects_a_doc_from_another_project(self):
        service = _patch_service()
        db = MagicMock()

        def query_one(sql, params=None):
            # the project resolves, but no document belongs to it
            if "FROM opportunities" in str(sql):
                return {"id": 118}
            return None

        db.query_one.side_effect = query_one
        shim = Shim("/api/v2/reconciler/runs", body=_create_body())
        shim = _run(
            shim, "_handle_reconciler_run_create",
            patches=[
                _patch_auth(_user()),
                patch.object(_server, "db", db),
                patch.object(_server, "reconciler_service", service),
            ],
        )
        self.assertEqual(shim.status, 400)
        service.build_run.assert_not_called()

    def test_limited_user_cannot_create_on_someone_elses_deal(self):
        service = _patch_service()
        shim = Shim("/api/v2/reconciler/runs", body=_create_body())
        shim = _run(
            shim, "_handle_reconciler_run_create",
            patches=[
                _patch_auth(_user(role="limited")),
                patch.object(_server, "db", _db_stub()),
                patch.object(Shim, "_reconciler_scoped", return_value=False),
                patch.object(_server, "reconciler_service", service),
            ],
        )
        self.assertEqual(shim.status, 403)
        service.build_run.assert_not_called()

    def test_non_admin_cannot_approve(self):
        service = _patch_service()
        shim = Shim("/api/v2/reconciler/runs/5/approve", body=b"{}")
        shim = _run(
            shim, "_handle_reconciler_run_approve", 5,
            patches=[_patch_auth(_user(is_admin=False)),
                     patch.object(_server, "reconciler_service", service)],
        )
        self.assertEqual(shim.status, 403)
        service.approve_run.assert_not_called()

    def test_admin_approve_blocked_by_the_gate_returns_409(self):
        service = _patch_service(
            approve_run=MagicMock(return_value={
                "ok": False, "code": 409,
                "error": "The tie-out gate blocked this run: old estimate failed tie-out",
            })
        )
        shim = Shim("/api/v2/reconciler/runs/5/approve", body=b"{}")
        shim = _run(
            shim, "_handle_reconciler_run_approve", 5,
            patches=[_patch_auth(_user(is_admin=True)),
                     patch.object(_server, "reconciler_service", service)],
        )
        self.assertEqual(shim.status, 409)
        self.assertIn("tie-out gate", shim.json()["error"])
        service.approve_run.assert_called_once()

    def test_admin_approve_success(self):
        run = {"id": 5, "projectId": 118, "status": "approved", "draftNote": "Note"}
        service = _patch_service(
            approve_run=MagicMock(return_value={
                "ok": True, "alreadyApproved": False, "noteEventId": 99,
                "tagRemoved": True, "run": run,
            })
        )
        shim = Shim("/api/v2/reconciler/runs/5/approve", body=b'{"draftNote": "Note"}')
        shim = _run(
            shim, "_handle_reconciler_run_approve", 5,
            patches=[_patch_auth(_user(is_admin=True)),
                     patch.object(_server, "reconciler_service", service)],
        )
        self.assertEqual(shim.status, 200, shim.body)
        self.assertTrue(shim.json()["ok"])
        self.assertEqual(shim.json()["noteEventId"], 99)
        self.assertTrue(shim.json()["tagRemoved"])

    def test_get_run_is_scoped_for_limited_users(self):
        run = {"id": 5, "projectId": 118, "draftNote": "", "result": {}}
        service = _patch_service(get_run=MagicMock(return_value=run))
        shim = Shim("/api/v2/reconciler/runs/5", command="GET")
        shim = _run(
            shim, "_handle_reconciler_run_get", 5,
            patches=[
                _patch_auth(_user(role="limited")),
                patch.object(_server, "db", _db_stub()),
                patch.object(Shim, "_reconciler_scoped", return_value=False),
                patch.object(_server, "reconciler_service", service),
            ],
        )
        self.assertEqual(shim.status, 403)

    def test_patch_note_requires_auth_and_a_note(self):
        service = _patch_service()
        shim = Shim("/api/v2/reconciler/runs/5", body=b'{"draftNote": "edited"}')
        shim = _run(
            shim, "_handle_reconciler_run_patch", 5,
            patches=[_patch_auth(None), patch.object(_server, "reconciler_service", service)],
        )
        self.assertEqual(shim.status, 401)
        service.update_draft_note.assert_not_called()

    def test_health_reports_parser_version(self):
        shim = Shim("/api/v2/reconciler/health", command="GET")
        shim = _run(shim, "_handle_reconciler_health", patches=[_patch_auth(_user())])
        self.assertEqual(shim.status, 200)
        payload = shim.json()
        self.assertIn("parserVersion", payload)
        self.assertIn("ocr", payload)


# ---------------------------------------------------------------------------
# DB-backed approval path
# ---------------------------------------------------------------------------
def _db_ready() -> bool:
    try:
        return bool(_server.db.query_one("SELECT to_regclass('reconciler_runs') AS t"))
    except Exception:
        return False


@unittest.skipUnless(_db_ready(), "no reachable PostgreSQL with migration 046")
class TestApprovalPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = {}
        cls.ids["creator"] = _server.db.insert_returning(
            "INSERT INTO users (email, display_name, is_admin) VALUES (%s, %s, TRUE) RETURNING id",
            ("reconciler-test-creator@example.test", "Reconciler Test Creator"),
        )
        cls.ids["approver"] = _server.db.insert_returning(
            "INSERT INTO users (email, display_name, is_admin) VALUES (%s, %s, TRUE) RETURNING id",
            ("reconciler-test-approver@example.test", "Reconciler Test Approver"),
        )
        cls.ids["project"] = _server.db.insert_returning(
            "INSERT INTO opportunities (title, responsible_user_id, created_by) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("Reconciler test project", cls.ids["creator"], cls.ids["creator"]),
        )
        cls.ids["tag"] = _tag_id("Needs reconciliation")
        cls.ids["other_tag"] = _tag_id("$$$")
        _server.db.execute(
            "INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (cls.ids["project"], cls.ids["tag"]),
        )
        _server.db.execute(
            "INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (cls.ids["project"], cls.ids["other_tag"]),
        )
        cls.creator = _server.db.query_one("SELECT * FROM users WHERE id = %s", (cls.ids["creator"],))
        cls.approver = _server.db.query_one("SELECT * FROM users WHERE id = %s", (cls.ids["approver"],))

    @classmethod
    def tearDownClass(cls):
        try:
            _server.db.execute(
                "DELETE FROM reconciler_audit WHERE opportunity_id = %s", (cls.ids["project"],)
            )
            _server.db.execute(
                "DELETE FROM reconciler_runs WHERE opportunity_id = %s", (cls.ids["project"],)
            )
            _server.db.execute(
                "DELETE FROM history_events WHERE opportunity_id = %s", (cls.ids["project"],)
            )
            _server.db.execute("DELETE FROM opportunities WHERE id = %s", (cls.ids["project"],))
            _server.db.execute(
                "DELETE FROM users WHERE id IN (%s, %s)",
                (cls.ids["creator"], cls.ids["approver"]),
            )
        except Exception:
            pass

    def _payload(self, gate=True, note="Claim increased by $1,000.00 ($1,000.00 to $1,500.00, no PP)"):
        return {
            "status": "drafted" if gate else "needs_review",
            "gateAllowed": gate,
            "confidence": 0.95 if gate else 0.0,
            "draftNote": note,
            "billingSummary": ["RCV: $1,000.00 -> $1,500.00 (+$500.00)"],
            "inputs": {"prior": {"docRef": "doc:1"}, "revised": {"docRef": "doc:2"}},
            "result": {"parserVersion": "test", "blockers": [] if gate else ["old estimate failed tie-out"]},
        }

    def _event_count(self):
        row = _server.db.query_one(
            "SELECT COUNT(*) AS n FROM history_events WHERE opportunity_id = %s",
            (self.ids["project"],),
        )
        return int(row["n"])

    def test_blocked_run_posts_nothing(self):
        run = _service.create_run(self.ids["project"], self.ids["creator"], self._payload(gate=False))
        before = self._event_count()
        result = _service.approve_run(run["id"], self.approver)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 409)
        self.assertEqual(self._event_count(), before)
        # still needs review, not approved
        self.assertNotEqual(_service.get_run(run["id"])["status"], "approved")

    def test_approve_posts_note_removes_only_the_tag_and_is_idempotent(self):
        run = _service.create_run(self.ids["project"], self.ids["creator"], self._payload())
        before = self._event_count()
        result = _service.approve_run(run["id"], self.approver)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["alreadyApproved"])
        self.assertTrue(result["tagRemoved"])
        self.assertEqual(self._event_count(), before + 1)

        event = _server.db.query_one(
            "SELECT category_id, content, created_by FROM history_events "
            "WHERE opportunity_id = %s ORDER BY id DESC LIMIT 1",
            (self.ids["project"],),
        )
        self.assertEqual(event["category_id"], 8)  # "Note"
        self.assertIn("Claim increased by $1,000.00", event["content"])
        self.assertEqual(event["created_by"], self.approver["id"])

        # the creator is notified
        notify = _server.db.query_one(
            "SELECT COUNT(*) AS n FROM history_notify_users WHERE event_id = "
            "(SELECT id FROM history_events WHERE opportunity_id = %s ORDER BY id DESC LIMIT 1) "
            "AND user_id = %s",
            (self.ids["project"], self.ids["creator"]),
        )
        self.assertEqual(int(notify["n"]), 1)

        # only "Needs reconciliation" was removed; "$$$" survives
        tags = {
            r["title"]
            for r in _server.db.query_dicts(
                "SELECT t.title FROM opportunity_tags ot "
                "JOIN tag_definitions t ON t.id = ot.tag_id WHERE ot.opportunity_id = %s",
                (self.ids["project"],),
            )
        }
        self.assertNotIn("Needs reconciliation", tags)
        self.assertIn("$$$", tags)

        # idempotent: a second approve posts nothing
        again = _service.approve_run(run["id"], self.approver)
        self.assertTrue(again["ok"])
        self.assertTrue(again["alreadyApproved"])
        self.assertEqual(self._event_count(), before + 1)

        # audit trail
        audit = _server.db.query_one(
            "SELECT COUNT(*) AS n FROM reconciler_audit WHERE run_id = %s AND action = 'approved'",
            (run["id"],),
        )
        self.assertEqual(int(audit["n"]), 1)

        # RCV custom fields were never touched (no call exists to make one)
        custom = _server.db.query_one(
            "SELECT COUNT(*) AS n FROM opportunity_custom_field_values WHERE opportunity_id = %s",
            (self.ids["project"],),
        )
        self.assertEqual(int(custom["n"]), 0)


def _tag_id(title: str) -> int:
    _server.db.execute(
        "INSERT INTO tag_definitions (title) VALUES (%s) ON CONFLICT (title) DO NOTHING", (title,)
    )
    row = _server.db.query_one("SELECT id FROM tag_definitions WHERE title = %s", (title,))
    return int(row["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
