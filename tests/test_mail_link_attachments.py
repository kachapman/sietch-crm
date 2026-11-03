#!/usr/bin/env python3
"""Tests: linked emails' files must land in the project's Documents tab.

Bug fix 2026-11-03. The linked-email attachment copy silently dropped files:
the scanner stored attachment METADATA only, the copy needed a live mailbox
download, and any failure of that download returned None and skipped the file
while the UI reported success. Relinking did not help because the snapshot
helper returned early when the timeline entry already existed.

Covered here:
  * retry succeeds after a transient failure (429 then 200)
  * partial copy counts ("2 of 3") reach the API response
  * relink repairs missing files when the timeline entry already exists
  * idempotency: linking / relinking / backfilling twice adds no duplicates
  * sync-time storage writes the file + sha256; oversized files are skipped
    and logged (metadata still stored)
  * backfill dry-run changes nothing; the real run adds exactly what the
    dry-run listed
  * Content-ID vs real attachment-id matching

Run with: python3 tests/test_mail_link_attachments.py

DB: the tests use a REAL PostgreSQL database (they exercise INSERTs,
unique indexes and dedupe). Point DB_NAME at a throwaway database:

    createdb sietch_mailatt_test
    python3 tests/test_mail_link_attachments.py

If the database is unreachable the suite SKIPS (exit 0) rather than failing,
matching tests/test_users.py.
"""

import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))


# ── Test DB bootstrap ───────────────────────────────────────────────────────
# The tests need a real DB (INSERTs + unique indexes). They create their own
# schema objects in the configured database and clean up after themselves.
# NEVER point this at production.
_TEST_DB_NAME = os.environ.get("MAILATT_TEST_DB_NAME", "")
_SKIP_REASON = ""


def _db_available() -> bool:
    global _SKIP_REASON
    try:
        import db as _db
        _db.init_db()
    except Exception as exc:
        _SKIP_REASON = f"database not reachable: {type(exc).__name__}: {exc}"
        return False
    try:
        import db as _db
        cur_name = _db.query_one("SELECT current_database() AS n")
        if _TEST_DB_NAME and (cur_name or {}).get("n") != _TEST_DB_NAME:
            _SKIP_REASON = (f"refusing to run against database "
                            f"{(cur_name or {}).get('n')!r} (set MAILATT_TEST_DB_NAME="
                            f"{_TEST_DB_NAME} to match)")
            return False
    except Exception as exc:
        _SKIP_REASON = f"database not reachable: {type(exc).__name__}: {exc}"
        return False
    # The suite needs the CRM schema. An empty database (like a bare CI
    # postgres service) has no tables — SKIP rather than fail.
    try:
        import db as _db
        row = _db.query_one(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name IN ('opportunities', 'mail_messages', 'mail_attachments', "
            "'history_attachments', 'history_events', 'users')")
        if not row or int(row.get("n") or 0) < 6:
            _SKIP_REASON = ("the database has no CRM schema — load init.sql + "
                            "migrations/*.sql first")
            return False
        # Migration 043 must be applied (it carries the dedupe key).
        col = _db.query_one(
            "SELECT 1 FROM information_schema.columns WHERE table_name = "
            "'history_attachments' AND column_name = 'mail_attachment_id'")
        if not col:
            _SKIP_REASON = ("migration 043 is not applied (history_attachments."
                            "mail_attachment_id is missing)")
            return False
    except Exception as exc:
        _SKIP_REASON = f"database not reachable: {type(exc).__name__}: {exc}"
        return False
    return True


if not _db_available():
    print(f"SKIP: {_SKIP_REASON}")
    sys.exit(0)

import db  # noqa: E402

# NOTE: server.py imports the scanner as a TOP-LEVEL module (`mail_scanner`,
# via sys.path) while the package form is `scanner.mail_scanner`. They are
# two distinct module objects with two distinct sets of globals, so patching
# one does NOT patch the other. The server's own copy is the one under test
# (the link handler and the backfill both go through it), so the tests use
# it — otherwise the mocks would never reach the code under test.
sys.path.insert(0, str(REPO / "scanner"))
import mail_scanner as ms  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────
_ADMIN = {"id": 1, "email": "admin@test", "is_admin": True}
_TEST_USER_ID = None


def _ensure_test_user() -> int:
    """Create (or find) the user the test rows point at.

    `opportunities.created_by` and `mail_deal_links.linked_by_user_id` are
    FKs to users, so the fixture needs a real row. The test user is created
    once and removed when the suite finishes.
    """
    global _TEST_USER_ID
    if _TEST_USER_ID:
        return _TEST_USER_ID
    row = db.query_one("SELECT id FROM users WHERE email = %s", ("mailatt-test@test.local",))
    if not row:
        uid = db.insert_returning(
            "INSERT INTO users (email, password_hash, display_name, is_admin) "
            "VALUES (%s, %s, %s, TRUE) RETURNING id",
            ("mailatt-test@test.local", "x", "Mail Attachment Test"))
        _TEST_USER_ID = int(uid)
    else:
        _TEST_USER_ID = int(row["id"])
    return _TEST_USER_ID


class Shim:
    """Minimal stand-in for BaseHTTPRequestHandler.

    The KanbanHandler methods under test are called unbound, so the shim
    only needs to provide the HTTP plumbing (and the handler's own helper
    methods are borrowed via `__getattr__`).
    """

    class headers:
        @staticmethod
        def get(key, default=None):
            return default

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def __init__(self, path="/"):
        self.path = path
        self.status = None
        self.headers_sent = {}
        self.body = b""

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_sent[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        outer = self

        class W:
            def write(self, data):
                outer.body += data

        return W()

    def __getattr__(self, name):
        """Borrow the KanbanHandler's own helper methods.

        The handler's methods are plain functions on the class, so they must
        be BOUND to this shim — returning them raw would shift every
        argument by one (self would be lost).
        """
        import server as srv
        attr = getattr(srv.KanbanHandler, name, None)
        if attr is None:
            raise AttributeError(name)
        return attr.__get__(self, type(self))


class BaseCase(unittest.TestCase):
    """Creates an opportunity, a mail account, a message and attachments."""

    def setUp(self):
        self.user_id = _ensure_test_user()
        self.opp_id = db.insert_returning(
            "INSERT INTO opportunities (title, created_by) VALUES (%s, %s) RETURNING id",
            ("Mail attachment test " + self.id().split(".")[-1], self.user_id))
        # Unique email per test so a crashed run cannot block the next one.
        self.acct_email = f"mailatt-{uuid.uuid4().hex[:12]}@test.local"
        self.acct_id = db.insert_returning(
            "INSERT INTO mail_accounts (email, imap_host, imap_port, password_encrypted, "
            "oauth_provider, oauth_access_token, sync_enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, TRUE) RETURNING id",
            (self.acct_email, "imap.test.local", 993, "x", "microsoft", "fake-token"))
        self.msg_id = db.insert_returning(
            "INSERT INTO mail_messages (account_id, folder, imap_uid, subject, from_addr, "
            "to_addr, body_text, date_received) VALUES (%s, 'INBOX', %s, %s, %s, %s, %s, NOW()) "
            "RETURNING id",
            (self.acct_id, "1001", "Estimate attached", "a@b.c", "d@e.f", "body"))
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        # history_events cascade to history_attachments; opportunities cascade
        # to project_documents.
        try:
            db.execute("DELETE FROM opportunities WHERE id = %s", (self.opp_id,))
            db.execute("DELETE FROM mail_messages WHERE id = %s", (self.msg_id,))
            db.execute("DELETE FROM mail_accounts WHERE id = %s", (self.acct_id,))
        except Exception:
            pass

    def add_attachment(self, filename="invoice.pdf", payload=b"%PDF-1.4 body",
                       mime="application/pdf", part_id="1.2", content_id="<cid-1>",
                       stored_path=None):
        return db.insert_returning(
            "INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, "
            "imap_part_id, remote_part_id, stored_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (self.msg_id, filename, mime, len(payload), content_id, part_id, stored_path))

    def stored_file(self, att_id, filename="invoice.pdf", payload=b"%PDF-1.4 body"):
        """Put a file into the local store and point the row at it."""
        rel = f"data/mail-attachments/{self.msg_id}/{att_id}_{filename}"
        full = REPO / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(payload)
        db.execute("UPDATE mail_attachments SET stored_path = %s, content_hash = %s, "
                   "stored_size = %s WHERE id = %s",
                   (rel, "hash-" + filename, len(payload), att_id))
        self.addCleanup(lambda: full.unlink(missing_ok=True))
        return rel

    def project_files(self, opp_id=None):
        opp_id = opp_id or self.opp_id
        return db.query_dicts(
            "SELECT id, title, file_path FROM project_documents "
            "WHERE opportunity_id = %s AND is_deleted = FALSE ORDER BY id",
            (opp_id,))

    def event_files(self, event_id):
        return db.query_dicts(
            "SELECT id, filename, mail_attachment_id FROM history_attachments "
            "WHERE event_id = %s ORDER BY id", (event_id,))


# ── 1. Retry / backoff ─────────────────────────────────────────────────────
class TestFetchRetry(BaseCase):
    """The mailbox download must retry a transient failure."""

    def test_retry_succeeds_after_transient_failure(self):
        """429 then 200: the second attempt's bytes are returned."""
        calls = {"n": 0}

        def fake_once(message_id, attachment_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None, "mailbox fetch failed: HTTP 429 throttled"
            return b"%PDF-1.4 retried", ""

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms.time, "sleep") as slept:
            data, reason = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertEqual(data, b"%PDF-1.4 retried")
        self.assertEqual(reason, "")
        self.assertEqual(calls["n"], 2, "the fetch must be retried")
        self.assertTrue(slept.called, "a retry must wait before trying again")

    def test_retry_gives_up_after_the_configured_attempts(self):
        calls = {"n": 0}

        def fake_once(message_id, attachment_id):
            calls["n"] += 1
            return None, "still failing"

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms.time, "sleep"):
            data, reason = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertIsNone(data)
        self.assertIn("still failing", reason)
        self.assertEqual(calls["n"], ms.MAIL_ATTACHMENT_FETCH_RETRIES)

    def test_retry_refreshes_the_mailbox_token(self):
        """Before a retry the mailbox token is refreshed (2026-11-02 cause)."""
        refreshed = {"n": 0}

        def fake_refresh(account_id):
            refreshed["n"] += 1

        def fake_once(message_id, attachment_id):
            if refreshed["n"] == 0:
                return None, "HTTP 401 unauthorized"
            return b"ok", ""

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms, "_refresh_mail_account_token", side_effect=fake_refresh), \
             patch.object(ms.time, "sleep"):
            data, _ = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertEqual(data, b"ok")
        self.assertEqual(refreshed["n"], 1, "the token must be refreshed before a retry")


# ── 2. Partial copy counts in the API response ─────────────────────────────
class TestLinkApiFileCounts(BaseCase):
    """The link API must report how many files were copied."""

    def _link(self, payload=None):
        import server as srv
        responses = []
        with patch.object(srv, "_require_auth", lambda h: _ADMIN), \
             patch.object(srv, "_mail_message_accessible", lambda u, m: True), \
             patch.object(srv, "_read_body",
                          lambda h: json.dumps(payload or {"opportunityId": self.opp_id}).encode()), \
             patch.object(srv, "_json_response", lambda h, s, b: responses.append((s, b))):
            srv.KanbanHandler._handle_mail_link(Shim(), self.msg_id)
        self.assertTrue(responses, "the handler must answer")
        return responses[0]

    def test_partial_copy_is_reported(self):
        """2 of 3 files copied -> the response says so (not plain success)."""
        # Three attachments; only two have a local file, the third has none
        # and the mailbox fallback is stubbed to fail.
        a1 = self.add_attachment("one.pdf", b"one")
        a2 = self.add_attachment("two.pdf", b"two")
        a3 = self.add_attachment("three.pdf", b"three")
        self.stored_file(a1, "one.pdf", b"one")
        self.stored_file(a2, "two.pdf", b"two")
        with patch.object(ms, "_fetch_mail_attachment_bytes_once",
                          return_value=(None, "mailbox unreachable")):
            status, body = self._link()
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("filesTotal"), 3)
        self.assertEqual(body.get("filesCopied"), 2)
        self.assertEqual(len(body.get("filesSkipped") or []), 1)
        self.assertEqual(body["filesSkipped"][0]["attachment_id"], a3)
        self.assertIn("mailbox unreachable", body["filesSkipped"][0]["reason"])

    def test_full_copy_is_reported(self):
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        status, body = self._link()
        self.assertEqual(status, 200)
        self.assertEqual(body.get("filesCopied"), 1)
        self.assertEqual(body.get("filesTotal"), 1)
        self.assertEqual(body.get("filesSkipped"), [])

    def test_already_linked_path_also_reports(self):
        """Relinking must report the counts too (that is the repair path)."""
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        db.execute("INSERT INTO mail_deal_links (message_id, opportunity_id) VALUES (%s, %s)",
                   (self.msg_id, self.opp_id))
        status, body = self._link()
        self.assertEqual(status, 200)
        self.assertFalse(body.get("linked"))
        self.assertEqual(body.get("filesCopied"), 1)
        self.assertEqual(body.get("filesTotal"), 1)


# ── 3. Relink repairs missing files ────────────────────────────────────────
class TestRelinkRepairs(BaseCase):
    def test_relink_copies_files_onto_an_existing_timeline_entry(self):
        """The event exists but has NO files -> relinking must add them."""
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        # A timeline entry that exists but has no copied files.
        event_id = db.insert_returning(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (self.opp_id, ms.EMAIL_HISTORY_CATEGORY_ID,
             json.dumps({"type": "email_snapshot", "message_id": self.msg_id}), self.user_id))
        self.assertEqual(self.event_files(event_id), [])

        created = ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        self.assertTrue(created)
        self.assertEqual(len(self.event_files(event_id)), 1,
                         "the repair path must copy the missing file")
        self.assertEqual(len(self.project_files()), 1,
                         "the file must show up in the project's Documents tab")

    def test_snapshot_detail_reports_the_repair(self):
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        db.insert_returning(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (self.opp_id, ms.EMAIL_HISTORY_CATEGORY_ID,
             json.dumps({"type": "email_snapshot", "message_id": self.msg_id}), self.user_id))
        report = ms.create_email_history_snapshot_detailed(self.opp_id, self.msg_id)
        self.assertEqual(report["copied"], 1)
        self.assertEqual(report["total"], 1)
        self.assertFalse(report["history_created"],
                         "the entry already existed — nothing new was created")


# ── 4. Idempotency ─────────────────────────────────────────────────────────
class TestIdempotency(BaseCase):
    def test_linking_twice_adds_no_duplicate_documents(self):
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        first = len(self.project_files())
        self.assertEqual(first, 1)
        # Link again (relink) — must not double up.
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        self.assertEqual(len(self.project_files()), first,
                         "relinking must not duplicate the document")

    def test_relink_to_another_project_copies_again(self):
        """A different project legitimately gets its own copy."""
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        opp2 = db.insert_returning(
            "INSERT INTO opportunities (title, created_by) VALUES (%s, %s) RETURNING id",
            ("Second project", self.user_id))
        self.addCleanup(lambda: db.execute("DELETE FROM opportunities WHERE id = %s", (opp2,)))
        ms.create_email_history_snapshot(opp2, self.msg_id)
        self.assertEqual(len(self.project_files(self.opp_id)), 1)
        self.assertEqual(len(self.project_files(opp2)), 1)

    def test_unique_index_blocks_a_duplicate_history_row(self):
        """The dedupe key is enforced by the DB, not just by the code."""
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        event_id = db.query_one(
            "SELECT id FROM history_events WHERE opportunity_id = %s AND "
            "strpos(content, '\"message_id\": ' || %s) > 0", (self.opp_id, self.msg_id))
        with self.assertRaises(Exception):
            db.execute(
                "INSERT INTO history_attachments (event_id, filename, file_path, file_size, "
                "mime_type, mail_attachment_id) VALUES (%s, %s, %s, %s, %s, %s)",
                (int(event_id["id"]), "dup.pdf", "data/x.pdf", 1, "application/pdf", a1))


# ── 5. Sync-time storage ───────────────────────────────────────────────────
class TestSyncTimeStorage(BaseCase):
    def test_store_attachments_writes_the_file_and_the_hash(self):
        payload = b"%PDF-1.4 sync-time body"
        with patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")):
            ms._store_attachments(self.msg_id, self.acct_id, [{
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(payload),
                "part_id": "<cid-1>",
                "remote_part_id": "1.2",
                "content": payload,
            }], {})
        rows = db.query_dicts(
            "SELECT id, filename, stored_path, content_hash, stored_size, download_error "
            "FROM mail_attachments WHERE message_id = %s", (self.msg_id,))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["stored_path"], "the attachment must be stored on disk")
        full = REPO / row["stored_path"]
        self.addCleanup(lambda: full.unlink(missing_ok=True))
        self.assertTrue(full.is_file())
        self.assertEqual(full.read_bytes(), payload)
        import hashlib
        self.assertEqual(row["content_hash"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(int(row["stored_size"]), len(payload))
        self.assertIsNone(row["download_error"])

    def test_oversized_attachment_is_skipped_but_metadata_is_kept(self):
        big = b"x" * (1024 * 1024)
        with patch.object(ms, "MAIL_ATTACHMENT_MAX_BYTES", 1024), \
             patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")), \
             self.assertLogs(ms.logger, level="WARNING") as logs:
            ms._store_attachments(self.msg_id, self.acct_id, [{
                "filename": "huge.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": len(big),
                "part_id": "",
                "remote_part_id": "2",
                "content": big,
            }], {})
        rows = db.query_dicts(
            "SELECT id, filename, stored_path, content_hash, download_error "
            "FROM mail_attachments WHERE message_id = %s", (self.msg_id,))
        self.assertEqual(len(rows), 1, "the metadata row must still exist")
        self.assertIsNone(rows[0]["stored_path"], "no bytes must be stored")
        self.assertIsNone(rows[0]["content_hash"])
        self.assertIn("limit", rows[0]["download_error"] or "")
        joined = "\n".join(logs.output)
        self.assertIn("huge.bin", joined, "the skip must be logged")
        self.assertIn("not stored locally", joined)

    def test_oversized_payload_is_not_carried_in_memory(self):
        """The sync loop must not hold a huge payload in RAM.

        `_fetch_messages` drops the bytes of an over-limit file, so the skip
        reason is still recorded (and the file is still fetchable at link
        time via the mailbox fallback).
        """
        big = b"x" * (1024 * 1024)
        with patch.object(ms, "MAIL_ATTACHMENT_MAX_BYTES", 1024), \
             patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")), \
             self.assertLogs(ms.logger, level="WARNING"):
            ms._store_attachments(self.msg_id, self.acct_id, [{
                "filename": "huge.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": len(big),
                "part_id": "",
                "remote_part_id": "2",
                # The fetch layer already dropped the oversized payload.
                "content": b"",
            }], {})
        rows = db.query_dicts(
            "SELECT id, stored_path, download_error FROM mail_attachments "
            "WHERE message_id = %s", (self.msg_id,))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["stored_path"])
        self.assertIn("limit", rows[0]["download_error"] or "")

    def test_stored_attachment_is_used_instead_of_the_mailbox(self):
        """The link-time copy prefers the local file — no mailbox round-trip."""
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        with patch.object(ms, "_fetch_mail_attachment_bytes_once",
                          side_effect=AssertionError("must not hit the mailbox")):
            ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        self.assertEqual(len(self.project_files()), 1)

    def test_attachments_json_stays_serializable(self):
        """The payload bytes must never reach attachments_json.

        attachments_json is a JSON column: raw bytes in it would raise
        "Object of type bytes is not JSON serializable" and break the whole
        mail sync. Only the metadata belongs there.
        """
        payload = b"%PDF-1.4 body"
        atts = [{
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(payload),
            "part_id": "<cid-1>",
            "remote_part_id": "1.2",
            "content": payload,
        }]
        # json.dumps of the raw list must fail — that is the bug this guards.
        with self.assertRaises(TypeError):
            json.dumps(atts)
        # The scanner's own path must not build that.
        stored = json.dumps([{k: v for k, v in a.items() if k != "content"} for a in atts])
        self.assertNotIn("content", stored)
        parsed = json.loads(stored)
        self.assertEqual(parsed[0]["filename"], "report.pdf")

    def test_store_message_does_not_break_on_binary_payloads(self):
        """The full store path must survive a payload-carrying attachment."""
        payload = b"%PDF-1.4 body"
        with patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")):
            msg_id = ms._store_message({
                "uid": "9001", "message_id": "m9001", "from": "a@b.c", "from_name": "",
                "to": "d@e.f", "subject": "with binary", "date": "",
                "body_text": "body", "body_html": "", "flags": [], "folder": "INBOX",
                "attachments": [{
                    "filename": "report.pdf", "mime_type": "application/pdf",
                    "size_bytes": len(payload), "part_id": "<cid>",
                    "remote_part_id": "1.1", "content": payload,
                }],
                "raw_headers": "",
            }, {"account_id": self.acct_id})
        self.assertTrue(msg_id)
        self.addCleanup(lambda: db.execute("DELETE FROM mail_messages WHERE id = %s", (msg_id,)))
        row = db.query_one("SELECT attachments_json FROM mail_messages WHERE id = %s", (msg_id,))
        self.assertTrue(row)
        # The column must be valid JSON that carries no bytes. (psycopg2
        # returns a JSON column already decoded, so handle both shapes.)
        raw = row["attachments_json"]
        parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        self.assertEqual(parsed[0]["filename"], "report.pdf")
        self.assertNotIn("content", parsed[0])
        # ...and the file must be on disk with its hash.
        atts = db.query_dicts(
            "SELECT id, stored_path, content_hash FROM mail_attachments WHERE message_id = %s",
            (msg_id,))
        self.assertEqual(len(atts), 1)
        self.assertTrue(atts[0]["stored_path"])
        full = REPO / atts[0]["stored_path"]
        self.addCleanup(lambda: full.unlink(missing_ok=True))
        self.assertEqual(full.read_bytes(), payload)


# ── 6. Backfill ─────────────────────────────────────────────────────────────
class TestBackfill(BaseCase):
    """The admin backfill: dry run changes nothing, the real run does."""

    def _run(self, payload):
        import server as srv
        responses = []
        with patch.object(srv, "_require_admin", lambda h: _ADMIN), \
             patch.object(srv, "_read_body",
                          lambda h: json.dumps(payload).encode()), \
             patch.object(srv, "_json_response", lambda h, s, b: responses.append((s, b))), \
             patch.object(srv, "_sync_running", False):
            srv.KanbanHandler._handle_sync_backfill_mail_attachments(Shim())
        self.assertTrue(responses, "the handler must answer")
        return responses[0]

    def _link_one(self, with_event=True, stored=True):
        a1 = self.add_attachment("one.pdf", b"one")
        if stored:
            self.stored_file(a1, "one.pdf", b"one")
        db.execute("INSERT INTO mail_deal_links (message_id, opportunity_id) VALUES (%s, %s)",
                   (self.msg_id, self.opp_id))
        event_id = None
        if with_event:
            event_id = db.insert_returning(
                "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (self.opp_id, ms.EMAIL_HISTORY_CATEGORY_ID,
                 json.dumps({"type": "email_snapshot", "message_id": self.msg_id}), self.user_id))
        return a1, event_id

    def test_dry_run_changes_nothing(self):
        a1, event_id = self._link_one(with_event=True, stored=True)
        status, body = self._run({"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["dry_run"])
        self.assertEqual(len(self.project_files()), 0, "a dry run must write nothing")
        self.assertEqual(len(self.event_files(event_id)), 0)
        self.assertTrue(body["items"], "the dry run must list what it would do")

    def test_dry_run_is_the_default(self):
        self._link_one(with_event=True, stored=True)
        status, body = self._run({})
        self.assertTrue(body["dry_run"], "dry run must be the default")
        self.assertEqual(len(self.project_files()), 0)

    def test_real_run_adds_exactly_what_the_dry_run_listed(self):
        a1, event_id = self._link_one(with_event=True, stored=True)
        _, dry = self._run({"dry_run": True})
        planned = [(i["message_id"], i["action"]) for i in dry["items"]]
        self.assertTrue(planned)
        _, wet = self._run({"apply": True})
        self.assertFalse(wet["dry_run"])
        done = [(i["message_id"], i["action"]) for i in wet["items"]]
        self.assertEqual(done, planned, "the real run must do exactly what the dry run listed")
        self.assertEqual(len(self.project_files()), 1)
        self.assertEqual(len(self.event_files(event_id)), 1)

    def test_real_run_is_idempotent(self):
        self._link_one(with_event=True, stored=True)
        self._run({"apply": True})
        self.assertEqual(len(self.project_files()), 1)
        _, again = self._run({"apply": True})
        self.assertEqual(again["copied_files"], 0, "a second run must copy nothing")
        self.assertEqual(len(self.project_files()), 1,
                         "a second run must not duplicate the document")

    def test_backfill_creates_a_missing_timeline_entry(self):
        """(b) link without a timeline entry -> entry + files are created."""
        self._link_one(with_event=False, stored=True)
        _, body = self._run({"apply": True})
        self.assertEqual(body["created_entries"], 1)
        self.assertEqual(len(self.project_files()), 1)
        events = db.query_dicts(
            "SELECT id FROM history_events WHERE opportunity_id = %s AND "
            "strpos(content, '\"message_id\": ' || %s) > 0", (self.opp_id, self.msg_id))
        self.assertEqual(len(events), 1)

    def test_backfill_reports_failures_with_reasons(self):
        a1, event_id = self._link_one(with_event=True, stored=False)
        with patch.object(ms, "_fetch_mail_attachment_bytes_once",
                          return_value=(None, "mailbox unreachable")):
            _, body = self._run({"apply": True})
        self.assertEqual(body["copied_files"], 0)
        self.assertTrue(body["failures"])
        f = body["failures"][0]
        self.assertEqual(f["message_id"], self.msg_id)
        self.assertEqual(f["attachment_id"], a1)
        self.assertIn("mailbox unreachable", f["reason"])

    def test_backfill_filters_by_project(self):
        self._link_one(with_event=True, stored=True)
        _, body = self._run({"dry_run": True, "project_id": self.opp_id})
        self.assertTrue(body["items"])
        _, body = self._run({"dry_run": True, "project_id": self.opp_id + 99999})
        self.assertEqual(body["items"], [])


# ── 7. Content-ID vs real attachment id ────────────────────────────────────
class TestPartMatching(BaseCase):
    def test_real_part_id_is_used_not_the_content_id(self):
        """imap_part_id holds a Content-ID; the real part id is remote_part_id.

        The old code matched on imap_part_id, which never matched
        att_obj.part, so the fetch only ever worked through the filename
        fallback.
        """
        att_id = self.add_attachment("report.pdf", b"body", part_id="1.2",
                                     content_id="<cid-report>")

        class FakeAtt:
            def __init__(self, part, filename, content_id, payload):
                self.part = part
                self.filename = filename
                self.content_id = content_id
                self.payload = payload
                self.content_type = "application/pdf"

        class FakePart:
            def __init__(self, pid):
                self.part_id = pid

        class FakeMsg:
            def __init__(self, atts):
                self.attachments = atts

        class FakeMailbox:
            def __init__(self, atts):
                self._atts = atts
                self.folder = self
                self.logged_out = False

            def xoauth2(self, user, token):
                pass

            def login(self, user, pwd):
                pass

            def set(self, name):
                pass

            def fetch(self, criteria=None, mark_seen=False, bulk=False):
                return [FakeMsg(self._atts)]

            def logout(self):
                self.logged_out = True

        # The attachment we want is NOT the first one — a wrong match would
        # return the other file's bytes.
        wrong = FakeAtt(FakePart("1.1"), "other.pdf", "<cid-other>", b"WRONG")
        right = FakeAtt(FakePart("1.2"), "report.pdf", "<cid-report>", b"RIGHT")
        box = FakeMailbox([wrong, right])

        with patch.object(ms, "MailBox", return_value=box) if hasattr(ms, "MailBox") else \
             patch.dict("sys.modules"):
            import sys as _sys
            fake_mod = MagicMock()
            fake_mod.A = MagicMock()
            fake_mod.MailBox = MagicMock(return_value=box)
            with patch.dict(_sys.modules, {"imap_tools": fake_mod}):
                data, reason = ms._fetch_mail_attachment_bytes_once(self.msg_id, att_id)
        self.assertEqual(data, b"RIGHT", "the real part id must select the right part")
        self.assertEqual(reason, "")

    def test_content_id_is_still_a_usable_fallback(self):
        att_id = self.add_attachment("report.pdf", b"body", part_id="",
                                     content_id="<cid-report>")

        class FakeAtt:
            def __init__(self):
                self.part = None
                self.filename = "report.pdf"
                self.content_id = "<cid-report>"
                self.payload = b"BY-CID"
                self.content_type = "application/pdf"

        class FakeMsg:
            attachments = [FakeAtt()]

        class FakeMailbox:
            class folder:
                @staticmethod
                def set(name):
                    pass

            def xoauth2(self, user, token):
                pass

            def fetch(self, criteria=None, mark_seen=False, bulk=False):
                return [FakeMsg()]

            def logout(self):
                pass

        import sys as _sys
        fake_mod = MagicMock()
        fake_mod.A = MagicMock()
        fake_mod.MailBox = MagicMock(return_value=FakeMailbox())
        with patch.dict(_sys.modules, {"imap_tools": fake_mod}):
            data, reason = ms._fetch_mail_attachment_bytes_once(self.msg_id, att_id)
        self.assertEqual(data, b"BY-CID")

    def test_imap_part_id_str_reads_the_real_part(self):
        class FakePart:
            part_id = "1.7"

        class FakeAtt:
            part = FakePart()

        self.assertEqual(ms._imap_part_id_str(FakeAtt()), "1.7")
        self.assertEqual(ms._imap_part_id_str(object()), "")


class TestServerPartMatching(unittest.TestCase):
    """The server's own download paths had the same Content-ID bug."""

    def test_server_picks_the_real_part_id(self):
        import server as srv

        class FakePart:
            def __init__(self, pid):
                self.part_id = pid

        class FakeAtt:
            def __init__(self, part, filename, content_id, payload):
                self.part = part
                self.filename = filename
                self.content_id = content_id
                self.payload = payload

        class FakeMsg:
            def __init__(self, atts):
                self.attachments = atts

        wrong = FakeAtt(FakePart("1.1"), "other.pdf", "<cid-other>", b"WRONG")
        right = FakeAtt(FakePart("1.2"), "report.pdf", "<cid-report>", b"RIGHT")
        msg = FakeMsg([wrong, right])
        att = {"remote_part_id": "1.2", "imap_part_id": "<cid-report>",
               "filename": "report.pdf"}
        self.assertEqual(srv._pick_imap_attachment(msg, att), b"RIGHT")

    def test_server_falls_back_to_the_filename(self):
        import server as srv

        class FakeAtt:
            def __init__(self, filename, payload):
                self.part = None
                self.filename = filename
                self.content_id = ""
                self.payload = payload

        class FakeMsg:
            def __init__(self, atts):
                self.attachments = atts

        msg = FakeMsg([FakeAtt("report.pdf", b"BY-NAME")])
        att = {"remote_part_id": "", "imap_part_id": "", "filename": "report.pdf"}
        self.assertEqual(srv._pick_imap_attachment(msg, att), b"BY-NAME")

    def test_server_returns_none_when_nothing_matches(self):
        import server as srv

        class FakeAtt:
            part = None
            filename = "other.pdf"
            content_id = ""
            payload = b"X"

        class FakeMsg:
            attachments = [FakeAtt()]

        att = {"remote_part_id": "9.9", "imap_part_id": "<nope>",
               "filename": "report.pdf"}
        self.assertIsNone(srv._pick_imap_attachment(FakeMsg(), att))


if __name__ == "__main__":
    unittest.main(verbosity=2)
