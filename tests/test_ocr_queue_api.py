#!/usr/bin/env python3
"""DB-backed tests for document text + OCR queue endpoints and MCP tools.

Synthetic fixtures only (tests/fixtures/ocr/pdfgen.py). Requires a live
PostgreSQL with the CRM schema (migration 052); exits 0 with a SKIP line when
the schema/tables are absent — same convention as tests/test_mcp_server.py.

Everything created here (users, tokens, projects, documents, mail rows, OCR
rows) is deleted in tearDownModule.

Run with: python3 tests/test_ocr_queue_api.py
"""

import base64
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "fixtures" / "ocr"))
sys.path.insert(0, str(HERE))

import network_guard  # noqa: E402

network_guard.install()

try:
    import db
    import api_tokens
    import mcp_local_api
    import mcp_server
    import document_read
    import ocr_store
    import server  # noqa: F401  (initializes the pool + migrations)
    from pdfgen import write_fixtures
    HAVE_DB = True
except Exception as exc:  # pragma: no cover - environment without a DB
    print(f"SKIP: {exc}")
    HAVE_DB = False

SUFFIX = uuid.uuid4().hex[:8]
TMPDIR = None
FIXTURES: dict[str, Path] = {}
_G: dict = {}
FULL_TOKEN = None
WORKER_TOKEN = None
USER_ID = None
PROJECT_A = None
PROJECT_B = None
MAIL_ACCOUNT_ID = None
MAIL_MESSAGE_ID = None
MAIL_MESSAGE_OTHER_ID = None
DOCS: dict[str, int] = {}
HISTORY_ATTACHMENT_ID = None
ATTACHMENTS: dict[str, int] = {}
EXTRA_MSG_ATTACHMENTS: dict[str, int] = {}
DIGITAL_SHA = None
SCANNED_SHA = None
TXTDOC_SHA = None


def _table_exists(name: str) -> bool:
    return db.query_one(
        "SELECT 1 AS ok FROM information_schema.tables WHERE table_name = %s", (name,)) is not None


def _add_project_document(project_id: int, filename: str, path: Path,
                          mime_type: str, title: str | None = None) -> int:
    return db.insert_returning(
        """INSERT INTO project_documents
             (opportunity_id, title, file_path, file_size, mime_type, uploaded_by)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (project_id, title or filename, str(path), path.stat().st_size,
         mime_type, USER_ID),
    )


def setUpModule():  # noqa: C901 - fixture setup is intentionally explicit
    global TMPDIR, FULL_TOKEN, WORKER_TOKEN, USER_ID, PROJECT_A, PROJECT_B
    global MAIL_ACCOUNT_ID, MAIL_MESSAGE_ID, MAIL_MESSAGE_OTHER_ID
    global HISTORY_ATTACHMENT_ID, DIGITAL_SHA, SCANNED_SHA, TXTDOC_SHA
    if not HAVE_DB:
        return
    if not all(_table_exists(t) for t in (
            "api_tokens", "project_documents", "history_attachments",
            "mail_attachments", "mail_messages", "mail_accounts",
            "ocr_page_cache", "ocr_queue")):
        raise unittest.SkipTest("CRM schema / migration 052 not present")

    TMPDIR = tempfile.mkdtemp(prefix="sietch-ocr-api-")
    FIXTURES.update(write_fixtures(TMPDIR))

    user = db.query_one(
        """INSERT INTO users (email, display_name, first_name, last_name,
                              password_hash, password_salt, is_admin, is_active, role)
           VALUES (%s, %s, 'OCR', 'Test', 'x', 'x', FALSE, TRUE, 'member')
           RETURNING id""",
        (f"ocr-test-{SUFFIX}@example.invalid", f"OCR Test {SUFFIX}"),
    )
    USER_ID = user["id"]
    FULL_TOKEN = api_tokens.create_api_token(f"ocr-test-{SUFFIX}", USER_ID, "full")["token"]
    WORKER_TOKEN = api_tokens.create_api_token(
        f"ocr-worker-{SUFFIX}", USER_ID, "full",
        restrictions={"methods": ["GET", "POST"], "resources": ["ocr"]},
    )["token"]

    PROJECT_A = db.insert_returning(
        "INSERT INTO opportunities (title, responsible_user_id, created_by) "
        "VALUES (%s, %s, %s) RETURNING id",
        (f"OCR project A {SUFFIX}", USER_ID, USER_ID),
    )
    PROJECT_B = db.insert_returning(
        "INSERT INTO opportunities (title, responsible_user_id, created_by) "
        "VALUES (%s, %s, %s) RETURNING id",
        (f"OCR project B {SUFFIX}", USER_ID, USER_ID),
    )

    DOCS["digital"] = _add_project_document(
        PROJECT_A, "synthetic-digital.pdf", FIXTURES["digital_pdf"], "application/pdf")
    DOCS["scanned"] = _add_project_document(
        PROJECT_A, "synthetic-scanned.pdf", FIXTURES["scanned_pdf"], "application/pdf")
    DOCS["png"] = _add_project_document(
        PROJECT_A, "synthetic-image.png", FIXTURES["png"], "image/png")
    DOCS["txt"] = _add_project_document(
        PROJECT_A, "synthetic-note.txt", FIXTURES["txt"], "text/plain")
    DOCS["other_project"] = _add_project_document(
        PROJECT_B, "synthetic-digital.pdf", FIXTURES["digital_pdf"], "application/pdf")

    # Missing-on-disk project document (a stale sync row).
    missing = Path(TMPDIR) / "already-deleted.pdf"
    missing.write_bytes(b"%PDF-1.4\n")
    DOCS["missing"] = _add_project_document(
        PROJECT_A, "deleted.pdf", missing, "application/pdf")
    missing.unlink()

    # Size cap: a sparse file just over the constant (no disk usage).
    big = Path(TMPDIR) / "oversized.pdf"
    with open(big, "wb") as fh:
        fh.truncate(document_read.MAX_DOCUMENT_CONTENT_BYTES + 1)
    DOCS["oversized"] = _add_project_document(
        PROJECT_A, "oversized.pdf", big, "application/pdf")

    # History attachment that was never mirrored into project_documents.
    event_id = db.insert_returning(
        "INSERT INTO history_events (opportunity_id, title, content, created_by) "
        "VALUES (%s, %s, 'synthetic', %s) RETURNING id",
        (PROJECT_A, f"OCR history {SUFFIX}", USER_ID),
    )
    HISTORY_ATTACHMENT_ID = db.insert_returning(
        """INSERT INTO history_attachments
             (event_id, filename, file_path, file_size, mime_type, uploaded_by)
           VALUES (%s, %s, %s, %s, 'application/pdf', %s) RETURNING id""",
        (event_id, "history-digital.pdf", str(FIXTURES["digital_pdf"]),
         FIXTURES["digital_pdf"].stat().st_size, USER_ID),
    )

    MAIL_ACCOUNT_ID = db.insert_returning(
        """INSERT INTO mail_accounts (email, display_name, imap_host, password_encrypted,
                                      owner_user_id, is_crm_mail)
           VALUES (%s, 'OCR test', 'imap.example.invalid', 'x', %s, FALSE) RETURNING id""",
        (f"ocr-mail-{SUFFIX}@example.invalid", USER_ID),
    )
    MAIL_MESSAGE_ID = db.insert_returning(
        """INSERT INTO mail_messages (account_id, imap_uid, subject, folder)
           VALUES (%s, %s, %s, 'INBOX') RETURNING id""",
        (MAIL_ACCOUNT_ID, f"uid-{SUFFIX}", f"OCR mail {SUFFIX}"),
    )
    MAIL_MESSAGE_OTHER_ID = db.insert_returning(
        """INSERT INTO mail_messages (account_id, imap_uid, subject, folder)
           VALUES (%s, %s, %s, 'INBOX') RETURNING id""",
        (MAIL_ACCOUNT_ID, f"uid-other-{SUFFIX}", f"OCR mail other {SUFFIX}"),
    )
    ATTACHMENTS["txt"] = db.insert_returning(
        """INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, stored_path)
           VALUES (%s, %s, 'text/plain', %s, %s) RETURNING id""",
        (MAIL_MESSAGE_ID, "synthetic-note.txt",
         FIXTURES["txt"].stat().st_size, str(FIXTURES["txt"])),
    )
    ATTACHMENTS["scanned"] = db.insert_returning(
        """INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, stored_path)
           VALUES (%s, %s, 'application/pdf', %s, %s) RETURNING id""",
        (MAIL_MESSAGE_ID, "synthetic-scanned.pdf",
         FIXTURES["scanned_pdf"].stat().st_size, str(FIXTURES["scanned_pdf"])),
    )
    ATTACHMENTS["not_stored"] = db.insert_returning(
        """INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, stored_path)
           VALUES (%s, %s, 'application/pdf', 10, NULL) RETURNING id""",
        (MAIL_MESSAGE_ID, "never-mirrored.pdf"),
    )
    EXTRA_MSG_ATTACHMENTS["txt"] = db.insert_returning(
        """INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, stored_path)
           VALUES (%s, %s, 'text/plain', %s, %s) RETURNING id""",
        (MAIL_MESSAGE_OTHER_ID, "synthetic-note.txt",
         FIXTURES["txt"].stat().st_size, str(FIXTURES["txt"])),
    )

    DIGITAL_SHA = document_read.extract_row(
        document_read.resolve_project_document(PROJECT_A, DOCS["digital"]),
        max_chars=1)["file_sha256"]
    SCANNED_SHA = document_read.extract_row(
        document_read.resolve_project_document(PROJECT_A, DOCS["scanned"]),
        max_chars=1)["file_sha256"]
    TXTDOC_SHA = document_read.extract_row(
        document_read.resolve_project_document(PROJECT_A, DOCS["txt"]),
        max_chars=1)["file_sha256"]

    _G["prev_enabled"] = mcp_server.is_enabled()
    mcp_server.set_enabled(True)


def tearDownModule():
    if not HAVE_DB:
        return
    try:
        for sha in (DIGITAL_SHA, SCANNED_SHA, TXTDOC_SHA):
            if sha:
                db.execute("DELETE FROM ocr_page_cache WHERE file_sha256 = %s", (sha,))
                db.execute("DELETE FROM ocr_queue WHERE file_sha256 = %s", (sha,))
        db.execute("DELETE FROM mcp_audit_log WHERE token_name LIKE %s", (f"%ocr-%{SUFFIX}%",))
        db.execute("DELETE FROM api_token_audit_log WHERE token_id IN "
                   "(SELECT id FROM api_tokens WHERE name LIKE %s)", (f"%ocr-%{SUFFIX}%",))
        db.execute("DELETE FROM api_tokens WHERE name LIKE %s", (f"%ocr-%{SUFFIX}%",))
        db.execute("DELETE FROM project_documents WHERE opportunity_id IN (%s, %s)",
                   (PROJECT_A, PROJECT_B))
        db.execute("DELETE FROM history_events WHERE opportunity_id IN (%s, %s)",
                   (PROJECT_A, PROJECT_B))
        db.execute("DELETE FROM mail_messages WHERE account_id = %s", (MAIL_ACCOUNT_ID,))
        db.execute("DELETE FROM mail_accounts WHERE id = %s", (MAIL_ACCOUNT_ID,))
        db.execute("DELETE FROM opportunities WHERE id IN (%s, %s)", (PROJECT_A, PROJECT_B))
        db.execute("DELETE FROM sessions WHERE user_id = %s", (USER_ID,))
        db.execute("DELETE FROM users WHERE id = %s", (USER_ID,))
    except Exception as exc:  # pragma: no cover
        print(f"cleanup warning: {exc}")
    try:
        mcp_server.set_enabled(_G.get("prev_enabled", False))
    except Exception:
        pass
    if TMPDIR:
        shutil.rmtree(TMPDIR, ignore_errors=True)


@unittest.skipUnless(HAVE_DB, "no database")
class OcrApiBase(unittest.TestCase):
    def call(self, method, path, body=None, token=None):
        return mcp_local_api.call(token or FULL_TOKEN, method, path, body)

    def project_text_path(self, document_id, project_id=None, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return (f"/api/v2/projects/{project_id or PROJECT_A}/documents/"
                f"{document_id}/text" + (f"?{query}" if query else ""))

    def request_ocr(self, source="project_document", **ids):
        body = {"source": source}
        body.update(ids)
        return self.call("POST", "/api/v2/ocr/request", body)

    def clear_cache_and_queue(self, sha):
        db.execute("DELETE FROM ocr_page_cache WHERE file_sha256 = %s", (sha,))
        db.execute("DELETE FROM ocr_queue WHERE file_sha256 = %s", (sha,))

    def assert_no_text_keys(self, payload):
        self.assertNotIn("text", payload)
        self.assertNotIn("contents", payload)


class TestProjectDocumentText(OcrApiBase):
    def test_text_happy_path(self):
        resp = self.call("GET", self.project_text_path(DOCS["digital"]))
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(resp.payload["status"], "ok")
        self.assertEqual(resp.payload["page_count"], 2)
        self.assertIn("Synthetic digital page one", resp.payload["text"])
        self.assertEqual(resp.payload["filename"], "synthetic-digital.pdf")

    def test_page_range_and_max_chars(self):
        resp = self.call("GET", self.project_text_path(
            DOCS["digital"], page_start=2, page_end=2, max_chars=12))
        self.assertEqual(resp.status, 200)
        self.assertEqual([p["page"] for p in resp.payload["pages"]], [2])
        self.assertTrue(resp.payload["truncated"])
        self.assertLessEqual(len(resp.payload["text"]), 12)

    def test_cross_project_id_is_404(self):
        resp = self.call("GET", self.project_text_path(
            DOCS["other_project"], project_id=PROJECT_A))
        self.assertEqual(resp.status, 404)
        self.assertIn("not found", resp.payload["error"].lower())

    def test_history_attachment_fallback(self):
        row = document_read.resolve_project_document(PROJECT_A, HISTORY_ATTACHMENT_ID)
        self.assertIsNotNone(row)
        self.assertTrue(row["from_history_attachment"])
        resp = self.call("GET", self.project_text_path(HISTORY_ATTACHMENT_ID))
        self.assertEqual(resp.status, 200)
        self.assertIn("page one", resp.payload["text"].lower())

    def test_history_attachment_is_scoped_to_its_project(self):
        resp = self.call("GET", self.project_text_path(
            HISTORY_ATTACHMENT_ID, project_id=PROJECT_B))
        self.assertEqual(resp.status, 404)

    def test_scanned_pdf_is_ocr_pending(self):
        resp = self.call("GET", self.project_text_path(DOCS["scanned"]))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.payload["status"], "ocr_pending")
        for page in resp.payload["pages"]:
            self.assertTrue(page["ocr_needed"])
            self.assertEqual(page["ocr_status"], "pending")
            self.assertEqual(page["text"], "")

    def test_cache_hit_returns_ocr_text(self):
        self.clear_cache_and_queue(SCANNED_SHA)
        try:
            for page in (1, 2):
                db.execute(
                    """INSERT INTO ocr_page_cache
                         (file_sha256, page_number, text, engine, engine_version, source, source_id)
                       VALUES (%s, %s, %s, %s, %s, 'project_document', %s)""",
                    (SCANNED_SHA, page, f"CACHED OCR PAGE {page}",
                     ocr_store.ENGINE_NAME, ocr_store.ENGINE_VERSION, DOCS["scanned"]),
                )
            resp = self.call("GET", self.project_text_path(DOCS["scanned"]))
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.payload["status"], "ok")
            self.assertIn("CACHED OCR PAGE 1", resp.payload["text"])
            self.assertEqual(resp.payload["pages"][0]["source"], "ocr_cache")
            self.assertEqual(resp.payload["pages"][0]["ocr_status"], "cached")
        finally:
            self.clear_cache_and_queue(SCANNED_SHA)

    def test_unsupported_type_has_no_body(self):
        resp = self.call("GET", self.project_text_path(DOCS["png"]))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.payload["status"], "unsupported_type")
        self.assertEqual(resp.payload["mime_type"], "image/png")
        self.assertEqual(resp.payload["text"], "")
        self.assertEqual(resp.payload["pages"], [])

    def test_missing_file_is_structured(self):
        resp = self.call("GET", self.project_text_path(DOCS["missing"]))
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.payload["error"], "not_stored")

    def test_bad_token_is_401(self):
        resp = self.call("GET", self.project_text_path(DOCS["digital"]),
                         token="sietch_not_a_real_token")
        self.assertEqual(resp.status, 401)


class TestDocumentContent(OcrApiBase):
    def test_base64_roundtrip(self):
        resp = self.call("GET", f"/api/v2/projects/{PROJECT_A}/documents/{DOCS['txt']}/content")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.payload["encoding"], "base64")
        decoded = base64.b64decode(resp.payload["content"])
        self.assertEqual(decoded, FIXTURES["txt"].read_bytes())
        self.assertEqual(resp.payload["size_bytes"], FIXTURES["txt"].stat().st_size)

    def test_size_cap_refuses_without_body(self):
        resp = self.call("GET",
                         f"/api/v2/projects/{PROJECT_A}/documents/{DOCS['oversized']}/content")
        self.assertEqual(resp.status, 413)
        self.assertNotIn("content", resp.payload)
        self.assertEqual(resp.payload["size_bytes"],
                         document_read.MAX_DOCUMENT_CONTENT_BYTES + 1)
        self.assertEqual(resp.payload["max_bytes"],
                         document_read.MAX_DOCUMENT_CONTENT_BYTES)

    def test_cross_project_content_is_404(self):
        resp = self.call("GET", f"/api/v2/projects/{PROJECT_A}/documents/"
                                f"{DOCS['other_project']}/content")
        self.assertEqual(resp.status, 404)


class TestMailAttachments(OcrApiBase):
    def test_stored_attachment_text(self):
        resp = self.call("GET", f"/api/v2/mail/messages/{MAIL_MESSAGE_ID}/attachments/"
                                f"{ATTACHMENTS['txt']}/text")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertIn("Synthetic plain text attachment", resp.payload["text"])

    def test_scanned_attachment_is_ocr_pending(self):
        resp = self.call("GET", f"/api/v2/mail/messages/{MAIL_MESSAGE_ID}/attachments/"
                                f"{ATTACHMENTS['scanned']}/text")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.payload["status"], "ocr_pending")

    def test_not_stored_never_uses_imap(self):
        resp = self.call("GET", f"/api/v2/mail/messages/{MAIL_MESSAGE_ID}/attachments/"
                                f"{ATTACHMENTS['not_stored']}/text")
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.payload["error"], "not_stored")

    def test_attachment_must_belong_to_message(self):
        resp = self.call("GET", f"/api/v2/mail/messages/{MAIL_MESSAGE_ID}/attachments/"
                                f"{EXTRA_MSG_ATTACHMENTS['txt']}/text")
        self.assertEqual(resp.status, 404)


class TestOcrStatusAndRequest(OcrApiBase):
    def tearDown(self):
        for sha in (SCANNED_SHA, DIGITAL_SHA):
            self.clear_cache_and_queue(sha)

    def status(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.call("GET", f"/api/v2/ocr/status?{query}")

    def test_status_reports_coverage_without_text(self):
        resp = self.status(source="project_document", project_id=PROJECT_A,
                           document_id=DOCS["scanned"])
        self.assertEqual(resp.status, 200, resp.text)
        payload = resp.payload
        self.assert_no_text_keys(payload)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["pages_needed_count"], 2)
        self.assertEqual(payload["pages_cached_count"], 0)
        self.assertEqual(payload["file_sha256"], SCANNED_SHA)

    def test_request_ocr_inserts_one_row_and_dedupes(self):
        first = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        self.assertEqual(first.status, 200, first.text)
        self.assertTrue(first.payload["queued"])
        self.assertEqual(first.payload["job"]["status"], "pending")
        job_id = first.payload["job"]["id"]
        second = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        self.assertEqual(second.status, 200)
        self.assertFalse(second.payload["queued"])
        self.assertTrue(second.payload["existing"])
        self.assertEqual(second.payload["job"]["id"], job_id)
        rows = db.query("SELECT id FROM ocr_queue WHERE file_sha256 = %s", (SCANNED_SHA,))
        self.assertEqual(len(rows), 1)
        audit = db.query_one(
            "SELECT 1 AS ok FROM api_token_audit_log WHERE path = '/api/v2/ocr/request' "
            "AND token_id = (SELECT id FROM api_tokens WHERE name = %s)",
            (f"ocr-test-{SUFFIX}",))
        self.assertIsNotNone(audit)

    def test_request_ocr_when_fully_cached_is_not_queued(self):
        for page in (1, 2):
            db.execute(
                """INSERT INTO ocr_page_cache
                     (file_sha256, page_number, text, engine, engine_version, source, source_id)
                   VALUES (%s, %s, 'cached', %s, %s, 'project_document', %s)""",
                (SCANNED_SHA, page, ocr_store.ENGINE_NAME,
                 ocr_store.ENGINE_VERSION, DOCS["scanned"]),
            )
        resp = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.payload["queued"])
        self.assertTrue(resp.payload["cache_reusable"])
        self.assertEqual(resp.payload["status"], "cached")
        self.assertIsNone(db.query_one(
            "SELECT id FROM ocr_queue WHERE file_sha256 = %s", (SCANNED_SHA,)))

    def test_request_ocr_unsupported_type_is_not_queued(self):
        resp = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["png"])
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.payload["queued"])
        self.assertEqual(resp.payload["status"], "unsupported_type")

    def test_request_ocr_mail_attachment(self):
        resp = self.request_ocr(source="mail_attachment", message_id=MAIL_MESSAGE_ID,
                                attachment_id=ATTACHMENTS["scanned"])
        self.assertEqual(resp.status, 200, resp.text)
        self.assertTrue(resp.payload["queued"])
        self.assertEqual(resp.payload["job"]["source"], "mail_attachment")
        self.assertEqual(resp.payload["job"]["message_id"], MAIL_MESSAGE_ID)
        mail_sha = resp.payload["file_sha256"]
        try:
            status = self.status(source="mail_attachment", message_id=MAIL_MESSAGE_ID,
                                 attachment_id=ATTACHMENTS["scanned"])
            self.assertEqual(status.payload["status"], "queued")
        finally:
            db.execute("DELETE FROM ocr_queue WHERE file_sha256 = %s", (mail_sha,))


class TestWorkerFlow(OcrApiBase):
    def tearDown(self):
        self.clear_cache_and_queue(SCANNED_SHA)

    def test_full_worker_cycle(self):
        created = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        job_id = created.payload["job"]["id"]

        listed = self.call("GET", "/api/v2/ocr/queue?status=pending&limit=10",
                           token=WORKER_TOKEN)
        self.assertEqual(listed.status, 200)
        self.assertIn(job_id, [j["id"] for j in listed.payload["jobs"]])

        claimed = self.call("POST", f"/api/v2/ocr/queue/{job_id}/claim",
                            token=WORKER_TOKEN)
        self.assertEqual(claimed.status, 200, claimed.text)
        self.assertEqual(claimed.payload["job"]["status"], "in_progress")

        fetched = self.call("GET", f"/api/v2/ocr/queue/{job_id}/file",
                            token=WORKER_TOKEN)
        self.assertEqual(fetched.status, 200)
        # The in-process shim decodes bytes as UTF-8 for its text wrapper;
        # exact-byte equality is pinned by TestWorkerFileOverHttp below.
        self.assertTrue(fetched.text.startswith("%PDF"))

        wrote = self.call("POST", f"/api/v2/ocr/queue/{job_id}/pages", {
            "engine": ocr_store.ENGINE_NAME,
            "engine_version": ocr_store.ENGINE_VERSION,
            "pages": [
                {"page": 1, "text": "WORKER OCR PAGE ONE"},
                {"page": 2, "text": "WORKER OCR PAGE TWO"},
            ],
        }, token=WORKER_TOKEN)
        self.assertEqual(wrote.status, 200, wrote.text)
        self.assertEqual(wrote.payload["pages_written"], 2)
        self.assertNotIn("WORKER OCR PAGE ONE", wrote.text)

        done = self.call("POST", f"/api/v2/ocr/queue/{job_id}/complete",
                         {"status": "done"}, token=WORKER_TOKEN)
        self.assertEqual(done.status, 200)
        self.assertEqual(done.payload["job"]["status"], "done")

        text = self.call("GET", self.project_text_path(DOCS["scanned"]))
        self.assertEqual(text.payload["status"], "ok")
        self.assertIn("WORKER OCR PAGE ONE", text.payload["text"])
        self.assertEqual(text.payload["pages"][0]["source"], "ocr_cache")

        status = self.call("GET", f"/api/v2/ocr/status?source=project_document"
                                  f"&project_id={PROJECT_A}&document_id={DOCS['scanned']}")
        self.assertEqual(status.payload["status"], "cached")
        self.assertEqual(status.payload["engine"], ocr_store.ENGINE_NAME)
        self.assertEqual(status.payload["engine_version"], ocr_store.ENGINE_VERSION)
        self.assertNotIn("WORKER OCR PAGE ONE", status.text)

        # A finished job is not downloadable any more.
        again = self.call("GET", f"/api/v2/ocr/queue/{job_id}/file", token=WORKER_TOKEN)
        self.assertEqual(again.status, 409)

    def test_file_hash_mismatch_is_refused(self):
        created = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        job_id = created.payload["job"]["id"]
        self.call("POST", f"/api/v2/ocr/queue/{job_id}/claim", token=WORKER_TOKEN)
        original = db.query_one(
            "SELECT file_path FROM project_documents WHERE id = %s", (DOCS["scanned"],))
        wrong = Path(TMPDIR) / "hash-mismatch-copy.pdf"
        wrong.write_bytes(FIXTURES["digital_pdf"].read_bytes())
        try:
            db.execute("UPDATE project_documents SET file_path = %s WHERE id = %s",
                       (str(wrong), DOCS["scanned"]))
            resp = self.call("GET", f"/api/v2/ocr/queue/{job_id}/file", token=WORKER_TOKEN)
            self.assertEqual(resp.status, 409)
            self.assertEqual(resp.payload["error"], "source_changed")
        finally:
            db.execute("UPDATE project_documents SET file_path = %s WHERE id = %s",
                       (original["file_path"], DOCS["scanned"]))

    def test_pages_require_a_claim(self):
        created = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        job_id = created.payload["job"]["id"]
        resp = self.call("POST", f"/api/v2/ocr/queue/{job_id}/pages", {
            "pages": [{"page": 1, "text": "too early"}],
        }, token=WORKER_TOKEN)
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.payload["error"], "job_not_claimed")

    def test_failed_job_can_be_requeued(self):
        created = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        job_id = created.payload["job"]["id"]
        self.call("POST", f"/api/v2/ocr/queue/{job_id}/claim", token=WORKER_TOKEN)
        failed = self.call("POST", f"/api/v2/ocr/queue/{job_id}/complete",
                           {"status": "failed", "error": "gpu_oom"}, token=WORKER_TOKEN)
        self.assertEqual(failed.status, 200)
        self.assertEqual(failed.payload["job"]["error"], "gpu_oom")
        again = self.request_ocr(project_id=PROJECT_A, document_id=DOCS["scanned"])
        self.assertTrue(again.payload["queued"])
        self.assertNotEqual(again.payload["job"]["id"], job_id)


class TestWorkerTokenScope(OcrApiBase):
    def test_ocr_token_cannot_read_documents_or_mail(self):
        for path in (
            self.project_text_path(DOCS["digital"]),
            f"/api/v2/projects/{PROJECT_A}/documents/{DOCS['digital']}/content",
            f"/api/v2/mail/messages/{MAIL_MESSAGE_ID}/attachments/{ATTACHMENTS['txt']}/text",
            "/api/v2/documents/search?q=synthetic",
            f"/api/v2/projects/{PROJECT_A}/documents",
        ):
            with self.subTest(path=path):
                resp = self.call("GET", path, token=WORKER_TOKEN)
                self.assertEqual(resp.status, 403, resp.text)

    def test_ocr_token_can_poll_the_queue(self):
        resp = self.call("GET", "/api/v2/ocr/queue?limit=1", token=WORKER_TOKEN)
        self.assertEqual(resp.status, 200)

    def test_ocr_token_cannot_use_admin(self):
        resp = self.call("GET", "/api/v2/admin/api-tokens", token=WORKER_TOKEN)
        self.assertEqual(resp.status, 403)


class TestWorkerFileOverHttp(unittest.TestCase):
    """Exact bytes: the in-process shim decodes responses as UTF-8."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.KanbanHandler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass

    def test_file_endpoint_returns_exact_bytes(self):
        import httpx

        created = mcp_local_api.call(WORKER_TOKEN, "POST", "/api/v2/ocr/request", {
            "source": "project_document", "project_id": PROJECT_A,
            "document_id": DOCS["scanned"],
        })
        self.assertEqual(created.status, 200, created.text)
        job_id = created.payload["job"]["id"]
        try:
            claimed = httpx.post(f"{self.base}/api/v2/ocr/queue/{job_id}/claim",
                                 headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                                 timeout=15)
            self.assertEqual(claimed.status_code, 200, claimed.text)
            r = httpx.get(f"{self.base}/api/v2/ocr/queue/{job_id}/file",
                          headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                          timeout=15)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, FIXTURES["scanned_pdf"].read_bytes())
        finally:
            db.execute("DELETE FROM ocr_queue WHERE file_sha256 = %s", (SCANNED_SHA,))

    def test_file_endpoint_refuses_an_ocr_token_on_other_paths(self):
        import httpx

        r = httpx.get(f"{self.base}/api/v2/projects/{PROJECT_A}/documents", 
                      headers={"Authorization": f"Bearer {WORKER_TOKEN}"}, timeout=15)
        self.assertEqual(r.status_code, 403)


class TestMcpTools(OcrApiBase):
    def call_tool(self, name, args, token=None):
        token = token or FULL_TOKEN
        user = mcp_server.authenticate(token)
        self.assertIsNotNone(user)
        mcp_server.set_tool_allowlist(user["api_token_id"], None)
        return mcp_server.process_message(
            user, token,
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": name, "arguments": args}},
            None)[0]

    def test_tools_are_listed(self):
        user = mcp_server.authenticate(FULL_TOKEN)
        names = {t["name"] for t in mcp_server._tool_list(user)}
        for name in ("get_project_document", "download_project_document",
                     "get_mail_attachment_text", "ocr_status", "request_ocr"):
            self.assertIn(name, names)

    def test_get_project_document_via_mcp(self):
        out = self.call_tool("get_project_document",
                             {"project_id": PROJECT_A, "document_id": DOCS["digital"]})
        self.assertFalse(out["result"]["isError"], out)
        self.assertEqual(out["result"]["structuredContent"]["status"], "ok")

    def test_request_ocr_via_mcp_is_immediate_not_a_proposal(self):
        try:
            out = self.call_tool("request_ocr",
                                 {"source": "project_document",
                                  "project_id": PROJECT_A,
                                  "document_id": DOCS["scanned"]})
            self.assertFalse(out["result"]["isError"], out)
            payload = out["result"]["structuredContent"]
            self.assertTrue(payload["queued"])
            self.assertNotIn("proposal_id", payload)
            self.assertIsNone(db.query_one(
                "SELECT id FROM mcp_pending_actions WHERE token_name = %s",
                (f"ocr-test-{SUFFIX}",)))
            self.assertIsNotNone(db.query_one(
                "SELECT id FROM ocr_queue WHERE file_sha256 = %s", (SCANNED_SHA,)))
        finally:
            self.clear_cache_and_queue(SCANNED_SHA)

    def test_download_via_mcp_is_base64_json(self):
        out = self.call_tool("download_project_document",
                             {"project_id": PROJECT_A, "document_id": DOCS["txt"]})
        payload = out["result"]["structuredContent"]
        self.assertEqual(payload["encoding"], "base64")
        self.assertEqual(base64.b64decode(payload["content"]), FIXTURES["txt"].read_bytes())


    def test_download_over_cap_reports_size_bytes(self):
        out = self.call_tool("download_project_document",
                             {"project_id": PROJECT_A, "document_id": DOCS["oversized"]})
        self.assertTrue(out["result"]["isError"])
        payload = out["result"]["structuredContent"]
        self.assertEqual(payload["error"], "document_too_large")
        self.assertNotIn("content", payload)
        self.assertEqual(payload["size_bytes"],
                         document_read.MAX_DOCUMENT_CONTENT_BYTES + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
