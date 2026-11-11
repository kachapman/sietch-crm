#!/usr/bin/env python3
"""MCP/API gap tests (2026-11-10): tags, paging, spec honesty, mail body +
attachments, and the audit wrappers.

Everything uses SYNTHETIC fixtures created in setUpModule and deleted in
tearDownModule. Needs a live PostgreSQL with the CRM schema (it exercises real
INSERTs/SELECTs); exits 0 with a SKIP line when the schema is absent.

Covers:
  * list_project_tags / untag_project (propose vs apply; the tag definition
    survives), tags on GET /projects/{id}, tag_id filter on the list route.
  * Projects list paging past 500 (startIndex + X-Total-Count), the `limit`
    alias, MCP search_projects startIndex/total, history paging + total.
  * Download routes documented as binary; OCR ids required per source
    (spec oneOf + handler 400 + MCP schema/build rejection);
    get_carrier_estimate_lines is gone; Document.fromHistoryAttachment;
    409 on the text routes; PATCH /documents/{id} documented with a body.
  * get_mail_message body + attachments metadata, list_mail_attachments,
    list_mail_accounts (no secrets).
  * list_users, list_stages, create_contact, list_history_replies,
    add_history_reply, list_company_documents.

Run with: python3 tests/test_mcp_api_gaps.py
"""

import json
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import db
    import auth  # noqa: F401
    import api_tokens
    import server  # noqa: F401  (initializes the pool + migrations)
    import mcp_server
    import mcp_tools
    import mcp_local_api
    import api_spec
    HAVE_DB = True
except Exception as exc:  # pragma: no cover - environment without a DB
    print(f"SKIP: {exc}")
    HAVE_DB = False

SUFFIX = uuid.uuid4().hex[:8]
TOKEN = None
USER_ID = None
ADMIN_ID = None
OTHER_USER_ID = None
PROJECT_TAGGED = None
PROJECT_PLAIN = None
PROJECT_HISTORY = None
PROJECT_MAIL = None
TAG_A = None
TAG_B = None
HISTORY_IDS = []
MAIL_MSG = None
MAIL_ATT_1 = None
MAIL_ATT_2 = None
FOREIGN_MSG = None
COMPANY_DOC = None
COMPANY_FOLDER = None
PAGING_PREFIX = f"GapsPaging {SUFFIX}"
_G = {}


def _table_exists(name: str) -> bool:
    return db.query_one(
        "SELECT 1 AS ok FROM information_schema.tables WHERE table_name = %s",
        (name,)) is not None


def setUpModule():
    global TOKEN, USER_ID, ADMIN_ID, OTHER_USER_ID
    global PROJECT_TAGGED, PROJECT_PLAIN, PROJECT_HISTORY, PROJECT_MAIL
    global TAG_A, TAG_B, HISTORY_IDS
    global MAIL_MSG, MAIL_ATT_1, MAIL_ATT_2, FOREIGN_MSG
    global COMPANY_DOC, COMPANY_FOLDER

    if not HAVE_DB:
        return
    if not _table_exists("opportunities") or not _table_exists("mcp_pending_actions"):
        raise unittest.SkipTest("CRM schema/migration 049 not present")

    def make_user(label, admin=False):
        row = db.query_one(
            """INSERT INTO users (email, display_name, first_name, last_name,
                                  password_hash, password_salt, is_admin, is_active, role)
               VALUES (%s, %s, %s, 'Gaps', 'x', 'x', %s, TRUE, %s) RETURNING id""",
            (f"gaps-{label}-{SUFFIX}@example.invalid", f"Gaps {label} {SUFFIX}",
             label.title(), admin, "admin" if admin else "member"),
        )
        return row["id"]

    USER_ID = make_user("user")
    ADMIN_ID = make_user("admin", admin=True)
    OTHER_USER_ID = make_user("other")
    TOKEN = api_tokens.create_api_token(f"gaps-test-{SUFFIX}", USER_ID, "full")["token"]

    def make_project(title):
        return db.query_one(
            """INSERT INTO opportunities (title, description, responsible_user_id, created_by)
               VALUES (%s, 'synthetic', %s, %s) RETURNING id""",
            (title, USER_ID, USER_ID),
        )["id"]

    PROJECT_TAGGED = make_project(f"Gaps tagged {SUFFIX}")
    PROJECT_PLAIN = make_project(f"Gaps plain {SUFFIX}")
    PROJECT_HISTORY = make_project(f"Gaps history {SUFFIX}")
    PROJECT_MAIL = make_project(f"Gaps mail {SUFFIX}")

    TAG_A = db.query_one(
        "INSERT INTO tag_definitions (title, color) VALUES (%s, '#f1c40f') RETURNING id",
        (f"$$$ {SUFFIX}",),
    )["id"]
    TAG_B = db.query_one(
        "INSERT INTO tag_definitions (title, color) VALUES (%s, '#3498db') RETURNING id",
        (f"Needs reconciliation {SUFFIX}",),
    )["id"]
    db.execute(
        "INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s)",
        (PROJECT_TAGGED, TAG_A),
    )
    db.execute(
        "INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s)",
        (PROJECT_PLAIN, TAG_B),
    )

    cat = db.query_one("SELECT id FROM history_categories ORDER BY id LIMIT 1")
    for i in range(5):
        row = db.query_one(
            """INSERT INTO history_events (opportunity_id, category_id, title, content, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (PROJECT_HISTORY, cat["id"] if cat else None,
             f"Event {i}", f"synthetic note {i}", USER_ID),
        )
        HISTORY_IDS.append(row["id"])

    acct = db.query_one(
        """INSERT INTO mail_accounts (email, display_name, imap_host, password_encrypted,
                                      owner_user_id, is_crm_mail, sync_enabled)
           VALUES (%s, 'Gaps Mail', 'imap.example.invalid', 'x', %s, FALSE, FALSE)
           RETURNING id""",
        (f"gaps-mail-{SUFFIX}@example.invalid", USER_ID),
    )
    MAIL_MSG = db.query_one(
        """INSERT INTO mail_messages (account_id, imap_uid, message_id, from_addr, to_addr,
                                      subject, body_text, body_html, folder, is_read)
           VALUES (%s, %s, %s, 'sender@example.invalid', 'user@example.invalid',
                   %s, %s, %s, 'INBOX', FALSE) RETURNING id""",
        (acct["id"], f"uid-{SUFFIX}", f"<msg-{SUFFIX}@example.invalid>",
         f"Gaps subject {SUFFIX}", "plain body text", "<p>html body</p>"),
    )["id"]
    MAIL_ATT_1 = db.query_one(
        """INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, stored_path)
           VALUES (%s, %s, 'application/pdf', 1234, %s) RETURNING id""",
        (MAIL_MSG, "stored.pdf", f"data/mail-attachments/{MAIL_MSG}/stored.pdf"),
    )["id"]
    MAIL_ATT_2 = db.query_one(
        """INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, stored_path)
           VALUES (%s, %s, 'image/png', 99, NULL) RETURNING id""",
        (MAIL_MSG, "notstored.png"),
    )["id"]

    foreign_acct = db.query_one(
        """INSERT INTO mail_accounts (email, display_name, imap_host, password_encrypted,
                                      owner_user_id, is_crm_mail, sync_enabled)
           VALUES (%s, 'Other Mail', 'imap.example.invalid', 'x', %s, FALSE, FALSE)
           RETURNING id""",
        (f"gaps-other-{SUFFIX}@example.invalid", OTHER_USER_ID),
    )
    FOREIGN_MSG = db.query_one(
        """INSERT INTO mail_messages (account_id, imap_uid, subject, body_text, folder)
           VALUES (%s, %s, 'other', 'x', 'INBOX') RETURNING id""",
        (foreign_acct["id"], f"uid-other-{SUFFIX}"),
    )["id"]

    COMPANY_FOLDER = db.query_one(
        """INSERT INTO document_folders (name, scope, uploaded_by)
           VALUES (%s, 'company', %s) RETURNING id""",
        (f"Gaps folder {SUFFIX}", USER_ID),
    )["id"]
    COMPANY_DOC = db.query_one(
        """INSERT INTO project_documents (opportunity_id, title, file_path, file_size,
                                          mime_type, uploaded_by, company_scope, folder_id)
           VALUES (NULL, %s, %s, 10, 'application/pdf', %s, TRUE, %s) RETURNING id""",
        (f"Gaps company doc {SUFFIX}", f"data/documents/gaps-{SUFFIX}.pdf",
         USER_ID, COMPANY_FOLDER),
    )["id"]

    _G["prev_enabled"] = mcp_server.is_enabled()
    mcp_server.set_enabled(True)


def tearDownModule():
    if not HAVE_DB:
        return
    try:
        db.execute(
            "DELETE FROM mcp_pending_actions WHERE token_id IN "
            "(SELECT id FROM api_tokens WHERE name = %s)", (f"gaps-test-{SUFFIX}",))
        db.execute("DELETE FROM mcp_audit_log WHERE token_name = %s", (f"gaps-test-{SUFFIX}",))
        db.execute(
            "DELETE FROM mcp_token_tools WHERE token_id IN "
            "(SELECT id FROM api_tokens WHERE name = %s)", (f"gaps-test-{SUFFIX}",))
        db.execute("DELETE FROM api_tokens WHERE name = %s", (f"gaps-test-{SUFFIX}",))
        db.execute("DELETE FROM opportunities WHERE title LIKE %s", (f"Gaps %{SUFFIX}%",))
        db.execute("DELETE FROM opportunities WHERE title LIKE %s", (f"{PAGING_PREFIX}%",))
        db.execute("DELETE FROM mail_messages WHERE subject LIKE %s", (f"%{SUFFIX}%",))
        db.execute("DELETE FROM mail_messages WHERE id = %s", (FOREIGN_MSG,))
        db.execute("DELETE FROM mail_accounts WHERE email LIKE %s", (f"%{SUFFIX}%",))
        db.execute("DELETE FROM project_documents WHERE title LIKE %s", (f"%{SUFFIX}%",))
        db.execute("DELETE FROM document_folders WHERE name LIKE %s", (f"%{SUFFIX}%",))
        db.execute("DELETE FROM tag_definitions WHERE title LIKE %s", (f"%{SUFFIX}",))
        db.execute("DELETE FROM sessions WHERE user_id IN (%s, %s, %s)",
                   (USER_ID, ADMIN_ID, OTHER_USER_ID))
        db.execute("DELETE FROM users WHERE id IN (%s, %s, %s)",
                   (USER_ID, ADMIN_ID, OTHER_USER_ID))
    except Exception as exc:  # pragma: no cover
        print(f"cleanup warning: {exc}")
    try:
        mcp_server.set_enabled(_G.get("prev_enabled", False))
    except Exception:
        pass


@unittest.skipUnless(HAVE_DB, "no database")
class GapsTestBase(unittest.TestCase):
    def setUp(self):
        mcp_server.set_enabled(True)
        self.user = mcp_server.authenticate(TOKEN)
        self.assertIsNotNone(self.user)
        mcp_server.set_tool_allowlist(self.user["api_token_id"], None)

    def call_tool(self, name, args):
        return mcp_server.process_message(
            self.user, TOKEN,
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": name, "arguments": args}},
            None)[0]

    def payload(self, name, args):
        out = self.call_tool(name, args)
        self.assertFalse(out["result"]["isError"], out)
        if "structuredContent" in out["result"]:
            return out["result"]["structuredContent"]
        # List results have no structuredContent (MCP requires an object), so
        # the JSON text block is the payload.
        return json.loads(out["result"]["content"][0]["text"])

    def rest(self, method, path, body=None):
        return mcp_local_api.call(TOKEN, method, path, body)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tags
# ─────────────────────────────────────────────────────────────────────────────

class TestTags(GapsTestBase):
    def test_list_project_tags(self):
        tags = self.payload("list_project_tags", {"project_id": PROJECT_TAGGED})
        self.assertIsInstance(tags, list)
        self.assertEqual([t["id"] for t in tags], [TAG_A])
        self.assertEqual(set(tags[0]), {"id", "title", "color", "isSystem"})

    def test_untag_propose_writes_nothing(self):
        db.execute("INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s) "
                   "ON CONFLICT DO NOTHING", (PROJECT_TAGGED, TAG_B))
        try:
            out = self.call_tool("untag_project", {
                "project_id": PROJECT_TAGGED, "tag_id": TAG_B, "mode": "propose"})
            self.assertEqual(out["result"]["structuredContent"]["status"], "pending")
            self.assertIsNotNone(db.query_one(
                "SELECT 1 AS ok FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
                (PROJECT_TAGGED, TAG_B)))
        finally:
            db.execute("DELETE FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
                       (PROJECT_TAGGED, TAG_B))

    def test_untag_apply_removes_only_the_link(self):
        db.execute("INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s) "
                   "ON CONFLICT DO NOTHING", (PROJECT_TAGGED, TAG_B))
        out = self.call_tool("untag_project", {
            "project_id": PROJECT_TAGGED, "tag_id": TAG_B, "mode": "apply"})
        self.assertFalse(out["result"]["isError"], out)
        self.assertIsNone(db.query_one(
            "SELECT 1 AS ok FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
            (PROJECT_TAGGED, TAG_B)))
        # The tag definition and its use elsewhere survive.
        self.assertIsNotNone(db.query_one(
            "SELECT 1 AS ok FROM tag_definitions WHERE id = %s", (TAG_B,)))
        self.assertIsNotNone(db.query_one(
            "SELECT 1 AS ok FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
            (PROJECT_PLAIN, TAG_B)))

    def test_get_project_returns_tags(self):
        project = self.payload("get_project", {"project_id": PROJECT_TAGGED})
        self.assertEqual([t["id"] for t in project["tags"]], [TAG_A])
        self.assertIn("title", project["tags"][0])

    def test_tag_filter_only_returns_tagged_projects(self):
        out = self.payload("search_projects", {
            "query": f"Gaps tagged {SUFFIX}", "tag_id": str(TAG_A)})
        self.assertEqual([p["id"] for p in out["projects"]], [PROJECT_TAGGED])

        # Same search without the tag matches everything we created with that
        # prefix; the untagged project must not appear in the tagged query.
        out2 = self.payload("search_projects", {"query": f"Gaps plain {SUFFIX}"})
        ids = [p["id"] for p in out2["projects"]]
        self.assertIn(PROJECT_PLAIN, ids)
        self.assertNotIn(PROJECT_TAGGED, ids)

        nothing = self.payload("search_projects", {
            "query": f"Gaps tagged {SUFFIX}", "tag_id": str(TAG_B)})
        self.assertEqual(nothing["projects"], [])

    def test_tag_filter_or_matches_several_ids(self):
        out = self.payload("search_projects", {
            "query": SUFFIX, "tag_id": f"{TAG_A},{TAG_B}", "count": 100})
        ids = {p["id"] for p in out["projects"]}
        self.assertTrue({PROJECT_TAGGED, PROJECT_PLAIN} <= ids)
        self.assertNotIn(PROJECT_HISTORY, ids)


# ─────────────────────────────────────────────────────────────────────────────
# 1e. Pagination
# ─────────────────────────────────────────────────────────────────────────────

class TestPagination(GapsTestBase):
    @classmethod
    def setUpClass(cls):
        if not HAVE_DB:
            raise unittest.SkipTest("no database")
        db.execute(
            """INSERT INTO opportunities (title, description, responsible_user_id, created_by)
               SELECT %s || ' ' || lpad(g::text, 4, '0'), 'synthetic', %s, %s
                 FROM generate_series(1, 501) g""",
            (PAGING_PREFIX, USER_ID, USER_ID),
        )

    def test_projects_page_past_500(self):
        first = self.rest("GET", f"/api/v2/projects?search={PAGING_PREFIX}&count=500")
        self.assertEqual(first.status, 200, first.text)
        self.assertEqual(len(first.payload), 500)
        self.assertEqual(first.headers.get("x-total-count"), "501")

        second = self.rest(
            "GET", f"/api/v2/projects?search={PAGING_PREFIX}&count=500&startIndex=500",
            None)
        self.assertEqual(second.status, 200, second.text)
        self.assertEqual(len(second.payload), 1, "project 501 must be retrievable")
        self.assertEqual(second.payload[0]["title"], f"{PAGING_PREFIX} 0501")

    def test_limit_is_a_count_alias(self):
        resp = self.rest("GET", f"/api/v2/projects?search={PAGING_PREFIX}&limit=3")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(len(resp.payload), 3)
        self.assertEqual(resp.headers.get("x-total-count"), "501")

    def test_search_projects_start_index_and_total(self):
        page = self.payload("search_projects", {
            "query": PAGING_PREFIX, "count": 2, "startIndex": 500})
        self.assertEqual(page["total"], 501)
        self.assertEqual(page["startIndex"], 500)
        self.assertEqual(len(page["projects"]), 1)
        self.assertFalse(page["hasMore"])
        self.assertIsNone(page["nextStartIndex"])

        page1 = self.payload("search_projects", {
            "query": PAGING_PREFIX, "count": 2, "startIndex": 0})
        self.assertEqual(len(page1["projects"]), 2)
        self.assertTrue(page1["hasMore"])
        self.assertEqual(page1["nextStartIndex"], 2)

    def test_history_pages_with_total(self):
        page = self.payload("list_project_history", {
            "project_id": PROJECT_HISTORY, "count": 2, "startIndex": 0})
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["events"]), 2)
        self.assertEqual(page["startIndex"], 0)

        last = self.payload("list_project_history", {
            "project_id": PROJECT_HISTORY, "count": 2, "startIndex": 4})
        self.assertEqual(len(last["events"]), 1)
        self.assertEqual(last["total"], 5)

    def test_history_rest_shape(self):
        resp = self.rest(
            "GET", f"/api/v2/projects/{PROJECT_HISTORY}/history?count=3&startIndex=0")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertIsInstance(resp.payload, dict)
        self.assertEqual(resp.payload["total"], 5)
        self.assertEqual(len(resp.payload["events"]), 3)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Spec honesty
# ─────────────────────────────────────────────────────────────────────────────

class TestSpecHonesty(unittest.TestCase):
    DOC = api_spec.build_openapi()

    def test_download_routes_are_binary(self):
        for path in ("/api/v2/documents/{id}",
                     "/api/v2/history-attachments/{id}/download",
                     "/api/v2/mail/messages/{id}/attachments/{attachment_id}/download"):
            content = self.DOC["paths"][path]["get"]["responses"]["200"]["content"]
            self.assertNotIn("application/json", content, path)
            self.assertIn("application/octet-stream", content, path)
            schema = content["application/octet-stream"]["schema"]
            self.assertEqual(schema.get("format"), "binary", path)

    def test_ocr_request_requires_ids_per_source(self):
        op = self.DOC["paths"]["/api/v2/ocr/request"]["post"]
        body = op["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("oneOf", body)
        schemas = self.DOC["components"]["schemas"]

        def resolve(branch):
            ref = branch.get("$ref")
            if not ref:
                return branch
            return schemas[ref.rsplit("/", 1)[-1]]

        branches = {resolve(b)["properties"]["source"]["enum"][0]: resolve(b)
                    for b in body["oneOf"]}
        self.assertEqual(set(branches), {"project_document", "mail_attachment"})
        self.assertEqual(set(branches["project_document"]["required"]),
                         {"source", "project_id", "document_id"})
        self.assertEqual(set(branches["mail_attachment"]["required"]),
                         {"source", "message_id", "attachment_id"})

    def test_ocr_status_documents_the_same_requirement(self):
        op = self.DOC["paths"]["/api/v2/ocr/status"]["get"]
        self.assertIn("x-ocr-target", op)
        self.assertIn("oneOf", op["x-ocr-target"])
        names = {p["name"] for p in op["parameters"]}
        self.assertTrue({"source", "project_id", "document_id",
                         "message_id", "attachment_id"} <= names)

    def test_carrier_estimate_tool_is_gone(self):
        self.assertNotIn("get_carrier_estimate_lines", mcp_tools.TOOL_NAMES)
        self.assertNotIn("get_carrier_estimate_lines", api_spec.MCP_TOOL_OPERATIONS)
        self.assertNotIn("/api/v2/projects/{id}/carrier-estimate-lines", self.DOC["paths"])

    def test_document_schema_has_from_history_attachment(self):
        doc = self.DOC["components"]["schemas"]["Document"]
        self.assertIn("fromHistoryAttachment", doc["properties"])
        self.assertFalse(doc["additionalProperties"])
        op = self.DOC["paths"]["/api/v2/projects/{id}/documents"]["get"]
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["properties"]["documents"]["items"]["$ref"],
                         "#/components/schemas/Document")

    def test_text_routes_document_409(self):
        for path in ("/api/v2/projects/{id}/documents/{document_id}/text",
                     "/api/v2/mail/messages/{id}/attachments/{attachment_id}/text"):
            responses = self.DOC["paths"][path]["get"]["responses"]
            self.assertIn("409", responses, path)
            self.assertIn("not_stored", responses["409"]["description"].lower(), path)
        queue_file = self.DOC["paths"]["/api/v2/ocr/queue/{id}/file"]["get"]["responses"]
        self.assertIn("409", queue_file)
        self.assertIn("not_stored", queue_file["409"]["description"])
        self.assertIn("source_changed", queue_file["409"]["description"])

    def test_document_text_status_enum(self):
        status = self.DOC["components"]["schemas"]["DocumentText"]["properties"]["status"]
        self.assertEqual(set(status["enum"]), {"ok", "ocr_pending", "unsupported_type"})

    def test_patch_document_is_documented_with_a_body(self):
        op = self.DOC["paths"]["/api/v2/documents/{id}"]["patch"]
        self.assertTrue(op["summary"])
        body = op["requestBody"]["content"]["application/json"]["schema"]
        self.assertTrue({"title", "opportunity_id", "folder_id", "scope"} <= set(body["properties"]))
        self.assertEqual(set(body["properties"]["scope"]["enum"]), {"personal", "company"})

    def test_projects_list_documents_total_header_and_limit_alias(self):
        op = self.DOC["paths"]["/api/v2/projects"]["get"]
        names = {p["name"] for p in op["parameters"]}
        self.assertTrue({"count", "limit", "startIndex", "tag_id"} <= names)
        self.assertIn("X-Total-Count", op["responses"]["200"]["headers"])
        self.assertEqual(op["responses"]["200"]["content"]["application/json"]["schema"]["type"],
                         "array")

    def test_project_tags_summaries_and_schema(self):
        get_op = self.DOC["paths"]["/api/v2/projects/{id}/tags"]["get"]
        self.assertNotIn("Get one", get_op["summary"])
        schema = get_op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["type"], "array")
        self.assertEqual(schema["items"]["$ref"], "#/components/schemas/Tag")
        delete_op = self.DOC["paths"]["/api/v2/projects/{id}/tags/{tag_id}"]["delete"]
        self.assertNotIn("Get one", delete_op["summary"])

    def test_mail_attachment_list_summary_and_schema(self):
        op = self.DOC["paths"]["/api/v2/mail/messages/{id}/attachments"]["get"]
        self.assertNotIn("Get one", op["summary"])
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["required"], ["attachments"])
        self.assertEqual(schema["properties"]["attachments"]["items"]["$ref"],
                         "#/components/schemas/MailAttachment")

    def test_mail_message_schema_describes_body_and_attachments(self):
        msg = self.DOC["components"]["schemas"]["MailMessage"]
        self.assertIn("body_text", msg["properties"])
        self.assertIn("body_html", msg["properties"])
        self.assertIn("attachments", msg["properties"])
        self.assertIn("linked_deals", msg["properties"])
        self.assertIn("tags", msg["properties"])

    def test_mcp_ocr_schema_requires_ids(self):
        for name in ("ocr_status", "request_ocr"):
            tool = mcp_tools.TOOLS_BY_NAME[name]
            schema = tool.input_schema
            self.assertIn("oneOf", schema, name)
            for branch in schema["oneOf"]:
                self.assertGreaterEqual(len(branch["required"]), 2)
                self.assertIn("source", branch["required"])


class TestOcrValidation(GapsTestBase):
    def test_mcp_schema_rejects_source_only(self):
        out = self.call_tool("ocr_status", {"source": "project_document"})
        self.assertTrue(out["result"]["isError"], out)
        self.assertIn("requires", json.dumps(out))

    def test_mcp_build_rejects_unknown_source(self):
        out = self.call_tool("request_ocr", {"source": "nope"})
        self.assertTrue(out["result"]["isError"], out)

    def test_rest_ocr_request_requires_ids(self):
        resp = self.rest("POST", "/api/v2/ocr/request", {"source": "project_document"})
        self.assertEqual(resp.status, 400, resp.text)
        self.assertIn("project_id", resp.text)

    def test_rest_ocr_status_requires_ids(self):
        resp = self.rest("GET", "/api/v2/ocr/status?source=mail_attachment")
        self.assertEqual(resp.status, 400, resp.text)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Mail: body + attachments metadata
# ─────────────────────────────────────────────────────────────────────────────

class TestMailMessage(GapsTestBase):
    def test_get_mail_message_returns_body_and_attachments(self):
        msg = self.payload("get_mail_message", {"message_id": MAIL_MSG})
        self.assertEqual(msg["subject"], f"Gaps subject {SUFFIX}")
        self.assertEqual(msg["body_text"], "plain body text")
        self.assertEqual(msg["body_html"], "<p>html body</p>")
        self.assertEqual(msg["from_addr"], "sender@example.invalid")
        self.assertEqual(len(msg["attachments"]), 2)
        ids = {a["id"] for a in msg["attachments"]}
        self.assertEqual(ids, {MAIL_ATT_1, MAIL_ATT_2})
        by_id = {a["id"]: a for a in msg["attachments"]}
        self.assertTrue(by_id[MAIL_ATT_1]["stored"])
        self.assertFalse(by_id[MAIL_ATT_2]["stored"])
        self.assertEqual(by_id[MAIL_ATT_1]["mime_type"], "application/pdf")
        self.assertEqual(by_id[MAIL_ATT_1]["size_bytes"], 1234)

    def test_list_mail_attachments_matches_message(self):
        listed = self.payload("list_mail_attachments", {"message_id": MAIL_MSG})
        atts = listed["attachments"]
        self.assertEqual({a["id"] for a in atts}, {MAIL_ATT_1, MAIL_ATT_2})
        first = next(a for a in atts if a["id"] == MAIL_ATT_1)
        self.assertEqual(set(first), {"id", "filename", "mime_type", "size_bytes", "stored"})
        self.assertEqual(first["filename"], "stored.pdf")
        self.assertTrue(first["stored"])
        self.assertFalse(next(a for a in atts if a["id"] == MAIL_ATT_2)["stored"])

    def test_missing_message_is_an_error(self):
        out = self.call_tool("get_mail_message", {"message_id": 2147483000})
        self.assertTrue(out["result"]["isError"])
        self.assertEqual(out["result"]["structuredContent"]["status"], 404)

    def test_inaccessible_message_is_403(self):
        out = self.call_tool("get_mail_message", {"message_id": FOREIGN_MSG})
        self.assertTrue(out["result"]["isError"], out)
        self.assertEqual(out["result"]["structuredContent"]["status"], 403)
        out2 = self.call_tool("list_mail_attachments", {"message_id": FOREIGN_MSG})
        self.assertTrue(out2["result"]["isError"], out2)
        self.assertEqual(out2["result"]["structuredContent"]["status"], 403)

    def test_rest_attachment_route_reports_stored(self):
        resp = self.rest("GET", f"/api/v2/mail/messages/{MAIL_MSG}/attachments")
        self.assertEqual(resp.status, 200, resp.text)
        by_id = {a["id"]: a for a in resp.payload["attachments"]}
        self.assertTrue(by_id[MAIL_ATT_1]["stored"])
        self.assertFalse(by_id[MAIL_ATT_2]["stored"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Wrappers: users, stages, mail accounts, contact, replies, company docs
# ─────────────────────────────────────────────────────────────────────────────

class TestWrappers(GapsTestBase):
    def test_list_users(self):
        users = self.payload("list_users", {})
        self.assertIsInstance(users, list)
        row = next(u for u in users if u["id"] == USER_ID)
        self.assertEqual(row["email"], f"gaps-user-{SUFFIX}@example.invalid")
        self.assertIn("displayName", row)
        self.assertIn("isAdmin", row)
        # No credential material ever leaves this route.
        for u in users:
            for key in u:
                self.assertNotIn("password_hash", key.lower())
                self.assertNotIn("salt", key.lower())
                self.assertNotIn("token", key.lower())

    def test_list_stages(self):
        stages = self.payload("list_stages", {})
        self.assertIsInstance(stages, list)
        if stages:
            self.assertIn("id", stages[0])
            self.assertIn("title", stages[0])
            self.assertIn("stageType", stages[0])

    def test_list_mail_accounts_has_no_secrets(self):
        accounts = self.payload("list_mail_accounts", {})
        self.assertIsInstance(accounts, list)
        mine = next(a for a in accounts if a["email"] == f"gaps-mail-{SUFFIX}@example.invalid")
        self.assertEqual(mine["display_name"], "Gaps Mail")
        for acct in accounts:
            for key in acct:
                lowered = key.lower()
                for needle in ("password", "token", "encrypted", "secret",
                               "credential", "refresh"):
                    self.assertNotIn(needle, lowered, f"leaked key {key!r}")
        # authType is a label ("password" | "oauth"), never a credential.
        self.assertIn(mine.get("authType"), (None, "password", "oauth"))

    def test_create_contact_propose_and_apply(self):
        email = f"gaps-contact-{SUFFIX}@example.invalid"
        proposed = self.call_tool("create_contact", {
            "firstName": "Pat", "lastName": f"Proposed {SUFFIX}",
            "email": email, "mode": "propose"})
        self.assertEqual(proposed["result"]["structuredContent"]["status"], "pending")
        self.assertIsNone(db.query_one("SELECT id FROM contacts WHERE email = %s", (email,)))

        applied = self.call_tool("create_contact", {
            "firstName": "Pat", "lastName": f"Applied {SUFFIX}",
            "email": email, "phone": "555-0100", "jobTitle": "Adjuster",
            "mode": "apply"})
        self.assertFalse(applied["result"]["isError"], applied)
        row = db.query_one(
            "SELECT id, first_name, last_name, email, phone, job_title FROM contacts "
            "WHERE email = %s", (email,))
        self.assertIsNotNone(row)
        self.assertEqual(row["first_name"], "Pat")
        self.assertEqual(row["phone"], "555-0100")
        self.assertEqual(row["job_title"], "Adjuster")
        db.execute("DELETE FROM contacts WHERE id = %s", (row["id"],))

    def test_history_replies_list_and_add(self):
        event_id = HISTORY_IDS[0]
        before = self.payload("list_history_replies", {
            "project_id": PROJECT_HISTORY, "history_id": event_id})
        self.assertIsInstance(before, list)

        content = f"synthetic reply {SUFFIX}"
        proposed = self.call_tool("add_history_reply", {
            "project_id": PROJECT_HISTORY, "history_id": event_id,
            "content": content, "mode": "propose"})
        self.assertEqual(proposed["result"]["structuredContent"]["status"], "pending")
        self.assertIsNone(db.query_one(
            "SELECT id FROM history_replies WHERE event_id = %s AND content = %s",
            (event_id, content)))

        applied = self.call_tool("add_history_reply", {
            "project_id": PROJECT_HISTORY, "history_id": event_id,
            "content": content, "mode": "apply"})
        self.assertFalse(applied["result"]["isError"], applied)
        row = db.query_one(
            "SELECT id FROM history_replies WHERE event_id = %s AND content = %s",
            (event_id, content))
        self.assertIsNotNone(row)

        after = self.payload("list_history_replies", {
            "project_id": PROJECT_HISTORY, "history_id": event_id})
        self.assertIn(row["id"], [r["id"] for r in after])

    def test_list_company_documents(self):
        out = self.payload("list_company_documents", {"folder_id": COMPANY_FOLDER})
        self.assertIn("documents", out)
        self.assertIn("folders", out)
        self.assertIn("total", out)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["documents"][0]["id"], COMPANY_DOC)
        self.assertEqual(out["documents"][0]["folderId"], COMPANY_FOLDER)

    def test_company_document_ids_share_the_document_id_space(self):
        # Same project_documents id space as GET /api/v2/documents/{id}.
        resp = self.rest("GET", f"/api/v2/documents/{COMPANY_DOC}")
        # No file on disk -> 404, but the id resolved (not a 400/500).
        self.assertIn(resp.status, (200, 404))


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

class TestAuth(unittest.TestCase):
    def test_list_routes_require_auth(self):
        for method, path in (
            ("GET", "/api/v2/users"),
            ("GET", "/api/v2/stages"),
            ("GET", "/api/v2/mail/accounts"),
            ("GET", "/api/v2/projects"),
            ("GET", f"/api/v2/projects/{PROJECT_TAGGED}/tags"),
        ):
            with self.subTest(path=path):
                resp = mcp_local_api.call("sietch_bogus_token", method, path, None)
                self.assertEqual(resp.status, 401, resp.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
