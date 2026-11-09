#!/usr/bin/env python3
"""MCP server tests: auth, allowlist, propose/apply, approvals, no-send.

Uses SYNTHETIC data only (a throwaway user, token, project and task that are
deleted in tearDownClass). Needs a live PostgreSQL, like the other DB-backed
suites; it exits 0 with a SKIP line when the CRM schema is absent.

Run with: python3 tests/test_mcp_server.py
"""

import json
import os
import sys
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import db
    import auth
    import api_tokens
    import server  # noqa: F401  (initializes the pool + migrations)
    import mcp_server
    import mcp_tools
    import mcp_local_api
    HAVE_DB = True
except Exception as exc:  # pragma: no cover - environment without a DB
    print(f"SKIP: {exc}")
    HAVE_DB = False

SUFFIX = uuid.uuid4().hex[:8]
TOKEN_PLAINTEXT = None
USER_ID = None
ADMIN_ID = None
PROJECT_ID = None
_G: dict = {}


def _table_exists(name: str) -> bool:
    return db.query_one(
        "SELECT 1 AS ok FROM information_schema.tables WHERE table_name = %s", (name,)) is not None


class _HttpClient:
    """A real MCP streamable-HTTP client (JSON-RPC over a socket)."""

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token
        self.session_id = None

    def post(self, payload, session=True):
        import httpx
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        r = httpx.post(self.base + "/mcp", json=payload, headers=headers, timeout=15)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        return r

    def initialize(self):
        return self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "sietch-test", "version": "1.0"}},
        }, session=False)

    def initialized(self):
        return self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tools_list(self):
        return self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    def call(self, name, args):
        return self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})


def setUpModule():
    """Create the synthetic fixtures ONCE for every class in this module."""
    if not HAVE_DB:
        return
    if True:
        global TOKEN_PLAINTEXT, USER_ID, ADMIN_ID, PROJECT_ID
        if not _table_exists("api_tokens") or not _table_exists("mcp_pending_actions"):
            raise unittest.SkipTest("CRM schema/migration 049 not present")
        user = db.query_one(
            """INSERT INTO users (email, display_name, first_name, last_name,
                                  password_hash, password_salt, is_admin, is_active, role)
               VALUES (%s, %s, 'MCP', 'Test', 'x', 'x', FALSE, TRUE, 'member')
               RETURNING id""",
            (f"mcp-test-{SUFFIX}@example.invalid", f"MCP Test {SUFFIX}"),
        )
        USER_ID = user["id"]
        admin = db.query_one(
            """INSERT INTO users (email, display_name, first_name, last_name,
                                  password_hash, password_salt, is_admin, is_active, role)
               VALUES (%s, %s, 'MCP', 'Admin', 'x', 'x', TRUE, TRUE, 'admin')
               RETURNING id""",
            (f"mcp-admin-{SUFFIX}@example.invalid", f"MCP Admin {SUFFIX}"),
        )
        ADMIN_ID = admin["id"]
        TOKEN_PLAINTEXT = api_tokens.create_api_token(
            f"mcp-test-{SUFFIX}", USER_ID, "full")["token"]
        project = db.query_one(
            """INSERT INTO opportunities (title, description, responsible_user_id, created_by)
               VALUES (%s, 'synthetic', %s, %s) RETURNING id""",
            (f"MCP test project {SUFFIX}", USER_ID, USER_ID),
        )
        PROJECT_ID = project["id"]
        _G["prev_enabled"] = mcp_server.is_enabled()
        mcp_server.set_enabled(True)


def tearDownModule():
    if not HAVE_DB:
        return
    if True:
        try:
            db.execute("DELETE FROM mcp_pending_actions WHERE token_id IN "
                       "(SELECT id FROM api_tokens WHERE name = %s)", (f"mcp-test-{SUFFIX}",))
            db.execute("DELETE FROM mcp_audit_log WHERE token_name = %s", (f"mcp-test-{SUFFIX}",))
            db.execute("DELETE FROM mcp_token_tools WHERE token_id IN "
                       "(SELECT id FROM api_tokens WHERE name = %s)", (f"mcp-test-{SUFFIX}",))
            db.execute("DELETE FROM api_tokens WHERE name = %s", (f"mcp-test-{SUFFIX}",))
            db.execute("DELETE FROM history_events WHERE opportunity_id = %s", (PROJECT_ID,))
            db.execute("DELETE FROM tasks WHERE title LIKE %s", (f"%{SUFFIX}%",))
            db.execute("DELETE FROM opportunities WHERE id = %s", (PROJECT_ID,))
            db.execute("DELETE FROM sessions WHERE user_id IN (%s, %s)", (USER_ID, ADMIN_ID))
            db.execute("DELETE FROM users WHERE id IN (%s, %s)", (USER_ID, ADMIN_ID))
        except Exception as exc:  # pragma: no cover
            print(f"cleanup warning: {exc}")
        try:
            mcp_server.set_enabled(_G.get("prev_enabled", False))
        except Exception:
            pass


@unittest.skipUnless(HAVE_DB, "no database")
class McpTestBase(unittest.TestCase):

    def setUp(self):
        mcp_server.set_enabled(True)
        self.user = mcp_server.authenticate(TOKEN_PLAINTEXT)
        self.assertIsNotNone(self.user)
        mcp_server.set_tool_allowlist(self.user["api_token_id"], None)

    def _token_id(self):
        return self.user["api_token_id"]

    def _call_tool(self, name, args, token=None):
        user = mcp_server.authenticate(token or TOKEN_PLAINTEXT)
        return mcp_server.process_message(
            user, token or TOKEN_PLAINTEXT,
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": name, "arguments": args}},
            None)[0]


class TestAuth(McpTestBase):
    def test_missing_token_is_rejected(self):
        self.assertIsNone(mcp_server.authenticate(None))
        self.assertIsNone(mcp_server.authenticate(""))

    def test_invalid_token_is_rejected(self):
        self.assertIsNone(mcp_server.authenticate("sietch_not_a_real_token"))

    def test_expired_token_is_rejected(self):
        rec = api_tokens.create_api_token(f"mcp-test-{SUFFIX}", USER_ID, "full")
        db.execute("UPDATE api_tokens SET expires_at = NOW() - INTERVAL '1 day' WHERE id = %s",
                   (rec["id"],))
        self.assertIsNone(mcp_server.authenticate(rec["token"]))
        db.execute("DELETE FROM api_tokens WHERE id = %s", (rec["id"],))

    def test_revoked_token_is_rejected(self):
        rec = api_tokens.create_api_token(f"mcp-test-{SUFFIX}", USER_ID, "full")
        api_tokens.revoke_api_token(rec["id"])
        self.assertIsNone(mcp_server.authenticate(rec["token"]))
        db.execute("DELETE FROM api_tokens WHERE id = %s", (rec["id"],))

    def test_read_only_token_cannot_write_or_propose(self):
        rec = api_tokens.create_api_token(f"mcp-test-{SUFFIX}-ro", USER_ID, "read")
        try:
            for mode in ("apply", "propose"):
                out = self._call_tool("create_task",
                                      {"title": f"RO {mode} {SUFFIX}", "mode": mode},
                                      token=rec["token"])
                self.assertTrue(out["result"]["isError"], out)
                self.assertIn("does not allow", json.dumps(out))
            self.assertIsNone(db.query_one(
                "SELECT id FROM tasks WHERE title = %s", (f"RO apply {SUFFIX}",)))
        finally:
            db.execute("DELETE FROM mcp_token_tools WHERE token_id = %s", (rec["id"],))
            db.execute("DELETE FROM api_tokens WHERE id = %s", (rec["id"],))


class TestAllowlist(McpTestBase):
    def test_allowlist_filters_list_and_calls(self):
        mcp_server.set_tool_allowlist(self._token_id(), ["search_projects"])
        listing = mcp_server.process_message(
            self.user, TOKEN_PLAINTEXT,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)[0]
        names = [t["name"] for t in listing["result"]["tools"]]
        self.assertEqual(names, ["search_projects"])

        denied = self._call_tool("list_tasks", {})
        self.assertTrue(denied["result"]["isError"])
        self.assertIn("not enabled", json.dumps(denied))

        allowed = self._call_tool("search_projects", {"count": 1})
        self.assertFalse(allowed["result"]["isError"])

    def test_clearing_allowlist_restores_all_tools(self):
        mcp_server.set_tool_allowlist(self._token_id(), ["search_projects"])
        mcp_server.set_tool_allowlist(self._token_id(), None)
        self.assertIsNone(mcp_server.allowed_tool_names(self._token_id()))


class TestProposeApply(McpTestBase):
    def test_propose_writes_nothing(self):
        title = f"Proposed task {SUFFIX}"
        out = self._call_tool("create_task", {"title": title, "mode": "propose"})
        payload = out["result"]["structuredContent"]
        self.assertEqual(payload["status"], "pending")
        self.assertIsNone(db.query_one("SELECT id FROM tasks WHERE title = %s", (title,)))

    def test_apply_writes_once(self):
        title = f"Applied task {SUFFIX}"
        out = self._call_tool("create_task", {"title": title, "mode": "apply"})
        self.assertFalse(out["result"]["isError"], out)
        row = db.query_one("SELECT id FROM tasks WHERE title = %s", (title,))
        self.assertIsNotNone(row)

    def test_approve_executes_and_reject_does_not(self):
        approve_title = f"Approve me {SUFFIX}"
        reject_title = f"Reject me {SUFFIX}"
        p1 = self._call_tool("create_task", {"title": approve_title})["result"]["structuredContent"]
        p2 = self._call_tool("create_task", {"title": reject_title})["result"]["structuredContent"]
        admin = {"id": ADMIN_ID, "email": "admin"}
        res = mcp_server.decide_proposal(p1["proposal_id"], "approve", admin)
        self.assertEqual(res["status"], "approved")
        self.assertIsNotNone(db.query_one("SELECT id FROM tasks WHERE title = %s", (approve_title,)))

        res2 = mcp_server.decide_proposal(p2["proposal_id"], "reject", admin)
        self.assertEqual(res2["status"], "rejected")
        self.assertIsNone(db.query_one("SELECT id FROM tasks WHERE title = %s", (reject_title,)))

        # idempotent: a decided proposal cannot be decided again
        with self.assertRaises(ValueError):
            mcp_server.decide_proposal(p1["proposal_id"], "approve", admin)

    def test_decisions_export_as_jsonl(self):
        p = self._call_tool("create_task", {"title": f"Export me {SUFFIX}"})
        pid = p["result"]["structuredContent"]["proposal_id"]
        mcp_server.decide_proposal(pid, "reject", {"id": ADMIN_ID}, "nope")
        text = mcp_server.training_jsonl()
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        ids = {row["proposal_id"] for row in lines}
        self.assertIn(pid, ids)
        row = next(r for r in lines if r["proposal_id"] == pid)
        self.assertEqual(row["decision"], "rejected")
        self.assertIn("tool", row)
        self.assertIn("args", row)

    def test_note_write_goes_through_history(self):
        out = self._call_tool("add_note", {
            "project_id": PROJECT_ID, "content": f"note {SUFFIX}", "mode": "apply"})
        self.assertFalse(out["result"]["isError"], out)
        self.assertIsNotNone(db.query_one(
            "SELECT id FROM history_events WHERE opportunity_id = %s AND content LIKE %s",
            (PROJECT_ID, f"%note {SUFFIX}%")))


class TestNoSend(McpTestBase):
    def test_no_send_tool_exists(self):
        for name in mcp_tools.TOOL_NAMES:
            self.assertNotIn("send", name.lower().replace("_", ""),
                             f"tool {name} looks like it could send email")
        self.assertNotIn("send_email", mcp_tools.TOOLS_BY_NAME)

    def test_send_paths_are_refused(self):
        for path in ("/api/v2/mail/send", "/api/v2/mail/send/undo"):
            with self.assertRaises(ValueError):
                mcp_tools.check_path_allowed(path)

    def test_draft_tool_creates_a_draft_not_a_send(self):
        out = self._call_tool("create_email_draft", {
            "to": "someone@example.invalid", "subject": f"draft {SUFFIX}",
            "body": "<p>hi</p>", "mode": "apply"})
        self.assertFalse(out["result"]["isError"], out)
        row = db.query_one(
            "SELECT id, status FROM mail_outgoing WHERE subject = %s", (f"draft {SUFFIX}",))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "draft")
        db.execute("DELETE FROM mail_outgoing WHERE id = %s", (row["id"],))


class TestAdminApi(McpTestBase):
    def test_token_cannot_manage_mcp(self):
        resp = mcp_local_api.call(TOKEN_PLAINTEXT, "GET", "/api/v2/admin/mcp/settings")
        self.assertEqual(resp.status, 403)

    def test_admin_session_can_read_settings_and_audit(self):
        session = auth.create_session(ADMIN_ID, ip_address="mcp-test")
        try:
            resp = mcp_local_api.call_session(session, "GET", "/api/v2/admin/mcp/settings")
            self.assertEqual(resp.status, 200, resp.text)
            self.assertIn("tools", resp.payload)
            self.assertIn("url", resp.payload)
            # never leak a token value
            self.assertNotIn(TOKEN_PLAINTEXT, resp.text)
            self.assertNotIn("token_hash", resp.text)

            audit = mcp_local_api.call_session(session, "GET", "/api/v2/admin/mcp/audit?limit=5")
            self.assertEqual(audit.status, 200)
            self.assertIn("entries", audit.payload)
            self.assertNotIn(TOKEN_PLAINTEXT, audit.text)

            export = mcp_local_api.call_session(
                session, "GET", "/api/v2/admin/mcp/training-export")
            self.assertEqual(export.status, 200)
            self.assertNotIn(TOKEN_PLAINTEXT, export.text)
        finally:
            auth.destroy_session(session)


class TestProtocolSmoke(unittest.TestCase):
    """A REAL MCP client over a REAL socket (full handshake)."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_DB:
            raise unittest.SkipTest("no database")
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.KanbanHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        if not TOKEN_PLAINTEXT:
            self.skipTest("test fixtures unavailable")
        mcp_server.set_enabled(True)
        self.client = _HttpClient(f"http://127.0.0.1:{self.port}", TOKEN_PLAINTEXT)

    def test_full_handshake_and_tool_call(self):
        r = self.client.initialize()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", body["result"]["capabilities"])
        self.assertIsNotNone(self.client.session_id)

        note = self.client.initialized()
        self.assertEqual(note.status_code, 202)

        listing = self.client.tools_list().json()
        names = [t["name"] for t in listing["result"]["tools"]]
        self.assertIn("search_projects", names)
        self.assertIn("create_task", names)
        self.assertNotIn("send_email", names)

        called = self.client.call("search_projects", {"query": f"MCP test project {SUFFIX}"}).json()
        self.assertFalse(called["result"]["isError"], called)
        self.assertIn("content", called["result"])

    def test_get_mcp_is_405(self):
        import httpx
        r = httpx.get(f"http://127.0.0.1:{self.port}/mcp",
                      headers={"Authorization": f"Bearer {TOKEN_PLAINTEXT}"}, timeout=10)
        self.assertEqual(r.status_code, 405)

    def test_missing_token_is_401(self):
        import httpx
        r = httpx.post(f"http://127.0.0.1:{self.port}/mcp", json={"jsonrpc": "2.0"},
                       timeout=10)
        self.assertEqual(r.status_code, 401)

    def test_disabled_server_is_503(self):
        import httpx
        mcp_server.set_enabled(False)
        try:
            r = httpx.post(f"http://127.0.0.1:{self.port}/mcp", json={"jsonrpc": "2.0"},
                           headers={"Authorization": f"Bearer {TOKEN_PLAINTEXT}"}, timeout=10)
            self.assertEqual(r.status_code, 503)
        finally:
            mcp_server.set_enabled(True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
