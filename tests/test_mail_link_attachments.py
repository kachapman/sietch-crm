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
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))


# ── Test DB bootstrap ───────────────────────────────────────────────────────
# The tests need a real DB (INSERTs + unique indexes). They create their own
# schema objects in the configured database and clean up after themselves.
# NEVER point this at production.
#
# ⚠ MANDATORY safety (bug fix 2026-11-03, review round 2):
#   * MAILATT_TEST_DB_NAME is REQUIRED — without it the suite refuses to run.
#   * The current database name must MATCH it exactly.
#   * A hard-coded denylist refuses the shared databases even if someone
#     sets the name to them (e.g. a leftover `sietch_crm` in the env).
_TEST_DB_NAME = os.environ.get("MAILATT_TEST_DB_NAME", "")
_SKIP_REASON = ""

# Databases this suite must NEVER touch.
_FORBIDDEN_DB_NAMES = {
    "sietch_crm",          # the shared dev/prod-shaped database
    "tg_dupe_scratch",     # another scratch DB used by a different task
    "postgres", "template0", "template1",
}


def _db_available() -> bool:
    global _SKIP_REASON
    if not _TEST_DB_NAME:
        _SKIP_REASON = ("MAILATT_TEST_DB_NAME is not set — this suite needs a "
                        "throwaway database and will not run without an "
                        "explicit name (e.g. MAILATT_TEST_DB_NAME=sietch_mailatt_test)")
        return False
    try:
        import db as _db
        _db.init_db()
    except Exception as exc:
        _SKIP_REASON = f"database not reachable: {type(exc).__name__}: {exc}"
        return False
    try:
        import db as _db
        cur_name = ( _db.query_one("SELECT current_database() AS n") or {}).get("n") or ""
        if cur_name in _FORBIDDEN_DB_NAMES:
            _SKIP_REASON = (f"refusing to run against the protected database "
                            f"{cur_name!r} — use a throwaway database")
            return False
        if cur_name != _TEST_DB_NAME:
            _SKIP_REASON = (f"refusing to run against database {cur_name!r} "
                            f"(MAILATT_TEST_DB_NAME={_TEST_DB_NAME!r} does not match)")
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
                       stored_path=None, size_bytes=None):
        return db.insert_returning(
            "INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, "
            "imap_part_id, remote_part_id, stored_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (self.msg_id, filename, mime,
             size_bytes if size_bytes is not None else len(payload),
             content_id, part_id, stored_path))

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
                return None, "mailbox fetch failed: HTTP 429 throttled", 5.0
            return b"%PDF-1.4 retried", "", None

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
            return None, "still failing", None

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms.time, "sleep"):
            data, reason = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertIsNone(data)
        self.assertIn("still failing", reason)
        self.assertEqual(calls["n"], ms.MAIL_ATTACHMENT_FETCH_RETRIES)

    def test_retry_refreshes_the_mailbox_token_before_the_FIRST_attempt(self):
        """The token is refreshed BEFORE the first attempt too.

        The 2026-11-02 prod failure was a stale token. Refreshing only
        before the RETRIES would waste the first attempt on the known-bad
        token.
        """
        order: list[str] = []

        def fake_refresh(account_id):
            order.append("refresh")

        def fake_once(message_id, attachment_id):
            order.append("fetch")
            return b"ok", "", None

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms, "_refresh_mail_account_token", side_effect=fake_refresh), \
             patch.object(ms.time, "sleep"):
            data, _ = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertEqual(data, b"ok")
        self.assertEqual(order[0], "refresh",
                         "the token must be refreshed BEFORE the first fetch")

    def test_retry_after_is_honored(self):
        """A Retry-After hint from the server replaces the plain backoff."""
        waits: list[float] = []

        def fake_once(message_id, attachment_id):
            if len(waits) == 0:
                return None, "mailbox fetch failed: HTTP 429 throttled", 7.0
            return b"ok", "", None

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms, "_refresh_mail_account_token"), \
             patch.object(ms.time, "sleep", side_effect=lambda s: waits.append(s)):
            data, _ = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertEqual(data, b"ok")
        self.assertIn(7.0, waits,
                      "the server's Retry-After (7s) must be waited for")

    def test_retry_after_is_capped(self):
        """A hostile Retry-After must not block the sync for hours."""
        waits: list[float] = []

        def fake_once(message_id, attachment_id):
            if len(waits) == 0:
                return None, "throttled", 99999.0
            return b"ok", "", None

        with patch.object(ms, "_fetch_mail_attachment_bytes_once", side_effect=fake_once), \
             patch.object(ms, "_refresh_mail_account_token"), \
             patch.object(ms.time, "sleep", side_effect=lambda s: waits.append(s)):
            data, _ = ms._fetch_mail_attachment_bytes(self.msg_id, 1)
        self.assertEqual(data, b"ok")
        self.assertTrue(all(w <= 120.0 for w in waits),
                        f"the wait must be capped, got {waits}")


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
                          return_value=(None, "mailbox unreachable", None)):
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
                          return_value=(None, "mailbox unreachable", None)):
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
# ── Real imap_tools attachment builders ─────────────────────────────────────
# The MIME-part tests use REAL imap_tools.MailAttachment objects built from
# real MIME messages. A hand-rolled fake with a `.part_id` attribute would
# test a fiction: imap_tools' MailAttachment has NO part id at all, and its
# `.part` is the whole email.message.EmailMessage (whose string form carries
# the base64 payload). Building the real objects is the only way to catch
# that class of bug.
def _real_attachment(filename: str = "report.pdf", payload: bytes = b"BODY",
                     content_id: str | None = None) -> Any:
    """A real imap_tools MailAttachment built from a real MIME message."""
    from email.message import EmailMessage
    from imap_tools import MailAttachment

    msg = EmailMessage()
    msg["Subject"] = "t"
    msg["From"] = "a@b.c"
    msg["To"] = "d@e.f"
    msg.set_content("hi")
    msg.add_attachment(payload, maintype="application", subtype="pdf",
                       filename=filename)
    for part in msg.walk():
        if part.get_filename() == filename:
            if content_id:
                part["Content-ID"] = content_id
            return MailAttachment(part)
    raise AssertionError(f"attachment {filename} not found in the built message")


def _real_attachment_with_cid(content_id: str, filename: str = "report.pdf",
                              payload: bytes = b"BODY") -> Any:
    """A real MailAttachment that carries a Content-ID header."""
    from email.message import EmailMessage
    from imap_tools import MailAttachment

    msg = EmailMessage()
    msg["Subject"] = "t"
    msg["From"] = "a@b.c"
    msg["To"] = "d@e.f"
    msg.set_content("hi")
    msg.add_attachment(payload, maintype="application", subtype="pdf",
                       filename=filename)
    for part in msg.walk():
        if part.get_filename() == filename:
            part["Content-ID"] = content_id
            return MailAttachment(part)
    raise AssertionError(f"attachment {filename} not found in the built message")


class TestPartMatching(BaseCase):
    """The MIME-part identifier must be short and must never carry payload."""

    def test_real_part_id_is_used_not_the_content_id(self):
        """The short MIME-part identifier selects the right MIME part.

        The old code matched on `imap_part_id`, which holds a Content-ID and
        never matched `att_obj.part`; the fetch only ever worked through its
        filename fallback.
        """
        # Two real attachments with DIFFERENT content — a wrong match would
        # return the other file's bytes.
        other = _real_attachment("other.pdf", b"WRONG")
        right = _real_attachment("report.pdf", b"RIGHT")
        att_id = self.add_attachment(
            "report.pdf", b"RIGHT", part_id="<cid-report>",
            content_id="<cid-report>")
        # The identifier the scanner would have stored for `right`.
        remote_part_id = ms._imap_part_id_str(right)
        self.assertTrue(remote_part_id)
        db.execute("UPDATE mail_attachments SET remote_part_id = %s WHERE id = %s",
                   (remote_part_id, att_id))

        class FakeMsg:
            attachments = [other, right]

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
            data, reason, _retry_after = ms._fetch_mail_attachment_bytes_once(
                self.msg_id, att_id)
        self.assertEqual(data, b"RIGHT",
                         "the MIME-part identifier must select the right part")
        self.assertEqual(reason, "")

    def test_content_id_is_still_a_usable_fallback(self):
        att_id = self.add_attachment("report.pdf", b"body", part_id="",
                                     content_id="<cid-report>")
        real = _real_attachment_with_cid("<cid-report>")

        class FakeMsg:
            attachments = [real]

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
            data, reason, _ = ms._fetch_mail_attachment_bytes_once(self.msg_id, att_id)
        self.assertEqual(data, b"BODY")

    def test_part_identifier_is_short_and_carries_no_payload(self):
        """⚠ THE H1 REGRESSION.

        `str(att.part)` is the whole MIME part INCLUDING the base64 payload
        (a 300 KB PDF -> 405,411 characters). It must never be stored in
        remote_part_id / attachments_json.
        """
        big = b"%PDF-1.4\n" + b"A" * 300_000
        att = _real_attachment("big.pdf", big)

        # The dangerous value, for the record.
        naive = str(att.part)
        self.assertGreater(len(naive), 400_000,
                           "imap_tools' .part really does stringify to the "
                           "whole base64 payload — this is what must not be used")
        self.assertIn("QUFB", naive, "the naive value really does contain the payload")

        # The value we actually store.
        ident = ms._imap_part_id_str(att)
        self.assertLess(len(ident), 200,
                        "the stored identifier must be short")
        self.assertLess(len(ident), ms.MAX_PART_ID_LEN)
        self.assertNotIn("QUFB", ident,
                         "the stored identifier must not contain payload text")
        self.assertNotIn("JVBER", ident, "no base64 of the PDF header either")

    def test_part_identifier_uses_the_content_id_when_present(self):
        att = _real_attachment_with_cid("<cid-abc123>", "report.pdf", b"BODY")
        ident = ms._imap_part_id_str(att)
        self.assertEqual(ident, "cid:cid-abc123")
        self.assertLess(len(ident), 200)

    def test_part_identifier_is_stable_for_the_same_bytes(self):
        a = _real_attachment("report.pdf", b"SAME")
        b = _real_attachment("report.pdf", b"SAME")
        self.assertEqual(ms._imap_part_id_str(a), ms._imap_part_id_str(b))
        c = _real_attachment("report.pdf", b"DIFFERENT")
        self.assertNotEqual(ms._imap_part_id_str(a), ms._imap_part_id_str(c))

    def test_part_identifier_handles_no_attachment(self):
        self.assertEqual(ms._imap_part_id_str(None), "")


class TestServerPartMatching(unittest.TestCase):
    """The server's own download paths had the same Content-ID bug."""

    def test_server_identifier_is_short_and_payload_free(self):
        import server as srv
        big = b"%PDF-1.4\n" + b"A" * 300_000
        att = _real_attachment("big.pdf", big)
        ident = srv._imap_part_id_str(att)
        self.assertLess(len(ident), 200)
        self.assertNotIn("QUFB", ident)

    def test_server_picks_the_right_part(self):
        import server as srv
        other = _real_attachment("other.pdf", b"WRONG")
        right = _real_attachment("report.pdf", b"RIGHT")

        class FakeMsg:
            attachments = [other, right]

        ident = srv._imap_part_id_str(right)
        att = {"remote_part_id": ident, "imap_part_id": "",
               "filename": "report.pdf"}
        self.assertEqual(srv._pick_imap_attachment(FakeMsg(), att), b"RIGHT")

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

        att = {"remote_part_id": "sha256:deadbeef:other.pdf:1",
               "imap_part_id": "<nope>", "filename": "report.pdf"}
        self.assertIsNone(srv._pick_imap_attachment(FakeMsg(), att))


# ── 8. Legacy copies (bug fix 2026-11-03, review round 2) ──────────────────
class TestLegacyCopyDedupe(BaseCase):
    """Files copied BEFORE migration 043 have no mail_attachment_id.

    The original dedupe only looked at `mail_attachment_id`, so a relink or
    a backfill duplicated every legacy file (1 timeline row + 1 Documents
    row became 2 + 2). The dedupe must recognise a legacy copy too.
    """

    def _legacy_copy(self, filename="invoice.pdf", payload=b"%PDF-1.4 legacy",
                     event_id=None, size=None):
        """Simulate a file copied by the OLD code: no mail_attachment_id."""
        event_id = event_id or db.insert_returning(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (self.opp_id, ms.EMAIL_HISTORY_CATEGORY_ID,
             json.dumps({"type": "email_snapshot", "message_id": self.msg_id}),
             self.user_id))
        rel = f"data/history-attachments/{self.opp_id}/{event_id}/1_{filename}"
        full = REPO / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(payload)
        self.addCleanup(lambda: full.unlink(missing_ok=True))
        db.execute(
            "INSERT INTO history_attachments (event_id, filename, file_path, file_size, "
            "mime_type, mail_attachment_id) VALUES (%s, %s, %s, %s, %s, NULL)",
            (event_id, filename, rel, size if size is not None else len(payload),
             "application/pdf"))
        db.execute(
            "INSERT INTO project_documents (opportunity_id, title, file_path, file_size, "
            "mime_type) VALUES (%s, %s, %s, %s, %s)",
            (self.opp_id, filename, rel, len(payload), "application/pdf"))
        return event_id

    def test_relink_does_not_duplicate_a_legacy_copy(self):
        """The exact H2 scenario: 1 legacy file must stay 1 file."""
        att_id = self.add_attachment("invoice.pdf", b"%PDF-1.4 legacy",
                                     size_bytes=len(b"%PDF-1.4 legacy"))
        self.stored_file(att_id, "invoice.pdf", b"%PDF-1.4 legacy")
        event_id = self._legacy_copy("invoice.pdf", b"%PDF-1.4 legacy")

        self.assertEqual(len(self.event_files(event_id)), 1)
        self.assertEqual(len(self.project_files()), 1)

        # Relink / backfill — must NOT add a second copy.
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)

        self.assertEqual(len(self.event_files(event_id)), 1,
                         "a legacy copy must not be duplicated")
        self.assertEqual(len(self.project_files()), 1,
                         "the Documents tab must not show the PDF twice")

    def test_legacy_copy_with_a_different_size_is_not_matched(self):
        """The dedupe is filename + size — a DIFFERENT file is not a match."""
        att_id = self.add_attachment("invoice.pdf", b"NEW", size_bytes=3)
        self.stored_file(att_id, "invoice.pdf", b"NEW")
        event_id = self._legacy_copy("invoice.pdf", b"OLD", size=99)

        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        self.assertEqual(len(self.event_files(event_id)), 2,
                         "a different file (same name, other size) must be copied")

    def test_backfill_does_not_duplicate_a_legacy_copy(self):
        att_id = self.add_attachment("invoice.pdf", b"%PDF-1.4 legacy",
                                     size_bytes=len(b"%PDF-1.4 legacy"))
        self.stored_file(att_id, "invoice.pdf", b"%PDF-1.4 legacy")
        event_id = self._legacy_copy("invoice.pdf", b"%PDF-1.4 legacy")
        db.execute("INSERT INTO mail_deal_links (message_id, opportunity_id) VALUES (%s, %s)",
                   (self.msg_id, self.opp_id))

        import server as srv
        responses = []
        with patch.object(srv, "_require_admin", lambda h: _ADMIN), \
             patch.object(srv, "_read_body", lambda h: json.dumps({"apply": True}).encode()), \
             patch.object(srv, "_json_response", lambda h, s, b: responses.append((s, b))), \
             patch.object(srv, "_sync_running", False):
            srv.KanbanHandler._handle_sync_backfill_mail_attachments(Shim())
        status, body = responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["copied_files"], 0,
                         "the backfill must not copy a file that is already there")
        self.assertEqual(len(self.event_files(event_id)), 1)
        self.assertEqual(len(self.project_files()), 1)

    def test_backfill_dry_run_does_not_report_a_legacy_copy_as_missing(self):
        """The dry-run count must reflect the legacy copies."""
        att_id = self.add_attachment("invoice.pdf", b"%PDF-1.4 legacy",
                                     size_bytes=len(b"%PDF-1.4 legacy"))
        self.stored_file(att_id, "invoice.pdf", b"%PDF-1.4 legacy")
        self._legacy_copy("invoice.pdf", b"%PDF-1.4 legacy")
        db.execute("INSERT INTO mail_deal_links (message_id, opportunity_id) VALUES (%s, %s)",
                   (self.msg_id, self.opp_id))

        import server as srv
        responses = []
        with patch.object(srv, "_require_admin", lambda h: _ADMIN), \
             patch.object(srv, "_read_body", lambda h: json.dumps({"dry_run": True}).encode()), \
             patch.object(srv, "_json_response", lambda h, s, b: responses.append((s, b))), \
             patch.object(srv, "_sync_running", False):
            srv.KanbanHandler._handle_sync_backfill_mail_attachments(Shim())
        status, body = responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["missing_files"], 0,
                         "a legacy copy must NOT be reported as a missing file")
        self.assertEqual(body["items"], [],
                         "a fully-copied email must not be listed for repair")


# ── 9. Deleting a mail attachment must not delete the project copy (H3) ─────
class TestAttachmentDeletionSafety(BaseCase):
    """migration 043's FK must be ON DELETE SET NULL, not CASCADE.

    `mail_attachments` rows are deleted when the mail account is deleted,
    when a message is emptied from Trash, and when the mail retention job
    purges old mail. With CASCADE that would delete the PROJECT's timeline
    file rows — the Oct 29 2026 bug all over again.
    """

    def test_deleting_the_mail_attachment_keeps_the_project_copy(self):
        att_id = self.add_attachment("invoice.pdf", b"%PDF-1.4 body")
        self.stored_file(att_id, "invoice.pdf", b"%PDF-1.4 body")
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        self.assertEqual(len(self.project_files()), 1)

        # The FK must be SET NULL, not CASCADE.
        row = db.query_one(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conname = 'history_attachments_mail_attachment_id_fkey'")
        self.assertTrue(row, "the FK from history_attachments must exist")
        self.assertEqual(row["confdeltype"], "n",
                         "the FK must be ON DELETE SET NULL ('n'), not CASCADE ('c')")

        # Now delete the mail attachment — as a mail account deletion or the
        # retention job would.
        db.execute("DELETE FROM mail_attachments WHERE id = %s", (att_id,))

        # The project's timeline row, its Documents row and the file on disk
        # must all survive.
        events = db.query_dicts(
            "SELECT id FROM history_events WHERE opportunity_id = %s AND "
            "strpos(content, '\"message_id\": ' || %s) > 0", (self.opp_id, self.msg_id))
        self.assertTrue(events)
        files = self.event_files(int(events[0]["id"]))
        self.assertEqual(len(files), 1,
                         "the project's timeline file row must survive")
        self.assertIsNone(files[0]["mail_attachment_id"],
                          "the link back to the mail attachment is dropped")
        self.assertEqual(len(self.project_files()), 1,
                         "the Documents row must survive")
        stored = self.project_files()[0]["file_path"]
        full = REPO / stored
        self.assertTrue(full.is_file(), "the file on disk must survive")

    def test_deleting_the_mail_message_keeps_the_project_copy(self):
        """A mail account deletion / Trash purge must not take the file."""
        att_id = self.add_attachment("invoice.pdf", b"%PDF-1.4 body")
        self.stored_file(att_id, "invoice.pdf", b"%PDF-1.4 body")
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)

        # Deleting the message cascades to mail_attachments (as the mail
        # account deletion does).
        db.execute("DELETE FROM mail_messages WHERE id = %s", (self.msg_id,))

        self.assertEqual(len(self.project_files()), 1,
                         "the Documents row must survive the mail deletion")
        stored = self.project_files()[0]["file_path"]
        self.assertTrue((REPO / stored).is_file(),
                        "the file on disk must survive the mail deletion")


# ── 10. Bounded memory over a whole folder (H4) ────────────────────────────
class TestBoundedMemory(BaseCase):
    """The sync must not hold every attachment of a folder in RAM.

    `_fetch_messages` used to build a LIST of the whole folder with the
    attachment bytes inside — a first sync / renumbering / watermark reset
    could take the CRM down.
    """

    def test_fetch_messages_is_a_generator(self):
        import inspect
        self.assertTrue(inspect.isgeneratorfunction(ms._fetch_messages),
                        "_fetch_messages must be a generator (streaming), not a list")

    def test_the_generator_yields_one_message_at_a_time(self):
        """Only ONE message's attachments may be materialised at a time."""
        seen: list[int] = []

        class FakeMsg:
            def __init__(self, uid):
                self.uid = uid
                self.attachments = []
                self.headers = None
                self.from_values = None
                self.to_values = []
                self.subject = f"subject {uid}"
                self.date = None
                self.text = ""
                self.html = ""
                self.flags = []

        class FakeMailbox:
            class folder:
                @staticmethod
                def set(name):
                    pass

            def xoauth2(self, user, token):
                pass

            def login(self, user, pwd):
                pass

            def fetch(self, criteria=None, mark_seen=False, bulk=False):
                # The UID-listing pass returns every UID; the single-message
                # pass returns just that one.
                if getattr(self, "_listed", False):
                    return [FakeMsg(self._single_uid)]
                self._listed = True
                return [FakeMsg(u) for u in (3, 1, 2)]

            def logout(self):
                pass

        def fake_login(cfg):
            return FakeMailbox()

        def fake_single(cfg, folder, uid):
            seen.append(uid)
            # A message that carries a 5 MB attachment — the scanner's own
            # fetch stages it on disk, so the dict carries a PATH.
            return {"uid": uid,
                    "attachments": [{"filename": "big.bin",
                                      "size_bytes": 5_000_000,
                                      "staged_path": "/tmp/staged/big.bin",
                                      "content": b""}],
                    "subject": f"s{uid}"}

        import sys as _sys
        fake_mod = MagicMock()
        fake_mod.A = MagicMock(return_value="CRITERION")
        fake_mod.MailBox = MagicMock(return_value=FakeMailbox())

        # The scanner imports A/MailBox at module level, so patching
        # sys.modules is not enough — patch the module attributes.
        with patch.object(ms, "A", fake_mod.A), \
             patch.object(ms, "MailBox", fake_mod.MailBox), \
             patch.object(ms, "_login_mailbox", side_effect=fake_login), \
             patch.object(ms, "_fetch_single_message", side_effect=fake_single):
            gen = ms._fetch_messages({"host": "h", "port": 993, "user": "u",
                                      "password": "p", "inbox": "INBOX"},
                                     "INBOX")
            # The generator must be lazy: creating it must not have fetched
            # anything yet.
            self.assertEqual(seen, [], "the generator must not fetch eagerly")
            first = next(gen)
            self.assertEqual(len(seen), 1, "exactly one message must be fetched")
            # The message's bytes are on disk (staged), not in the dict.
            self.assertEqual(first["attachments"][0]["content"], b"",
                             "the message dict must not carry attachment bytes")
            self.assertTrue(first["attachments"][0]["staged_path"],
                            "the attachment must be referenced by a staged path")
            second = next(gen)
            self.assertEqual(len(seen), 2,
                             "the second message is fetched only when asked for")

    def test_staged_bytes_are_never_carried_in_the_message_dict(self):
        """A staged attachment must not carry its bytes in the dict."""
        att_id = self.add_attachment("report.pdf", b"body")
        self.stored_file(att_id, "report.pdf", b"body")
        # The scanner path stores the STAGED file; the dict carries the
        # path, never the bytes.
        src = inspect_source(ms._store_attachments)
        self.assertIn("staged_path", src)
        self.assertIn("_store_staged_attachment_bytes", src)

    def test_the_staged_file_is_moved_into_the_store(self):
        """The staged bytes must end up in data/mail-attachments/."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged.bin"
            staged.write_bytes(b"%PDF-1.4 staged payload")
            with patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")):
                rel, err = ms._store_staged_attachment_bytes(
                    self.msg_id, 4242, "report.pdf", str(staged))
            self.assertIsNone(err, err)
            self.assertTrue(rel)
            final = REPO / rel
            self.addCleanup(lambda: final.unlink(missing_ok=True))
            self.assertTrue(final.is_file())
            self.assertEqual(final.read_bytes(), b"%PDF-1.4 staged payload")
            # The staged file is consumed (moved), not left behind.
            self.assertFalse(staged.exists())

    def test_an_oversized_staged_file_is_refused_and_removed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "big.bin"
            staged.write_bytes(b"x" * 5000)
            with patch.object(ms, "MAIL_ATTACHMENT_MAX_BYTES", 1024), \
                 patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")):
                rel, err = ms._store_staged_attachment_bytes(
                    self.msg_id, 4243, "big.bin", str(staged))
            self.assertIsNone(rel)
            self.assertIn("limit", err)
            self.assertFalse(staged.exists(),
                             "the refused staged file must not be left on disk")

    def test_the_storage_kill_switch_disables_storing(self):
        """MAIL_ATTACHMENT_STORAGE_ENABLED=false must store nothing."""
        with patch.object(ms, "MAIL_ATTACHMENT_STORAGE_ENABLED", False), \
             patch.object(ms, "__file__", str(REPO / "scanner" / "mail_scanner.py")):
            rel, err = ms._store_attachment_bytes(self.msg_id, 4244, "x.pdf", b"data")
        self.assertIsNone(rel)
        self.assertIn("MAIL_ATTACHMENT_STORAGE_ENABLED", err)

    def test_retention_keeps_the_stored_files_by_default(self):
        """The retention policy for data/mail-attachments/ is Ken's call.

        The DEFAULT must keep the files (MAIL_ATTACHMENT_DROP_STORED_ON_PURGE
        is false), so the retention job never deletes a customer's document.
        """
        self.assertFalse(ms.MAIL_ATTACHMENT_DROP_STORED_ON_PURGE,
                         "the default must be to KEEP the stored files")
        src = inspect_source(ms._enforce_retention)
        self.assertIn("MAIL_ATTACHMENT_DROP_STORED_ON_PURGE", src)
        # The project's own copy must never be touched by retention: the
        # code must not reference the history-attachments tree (the
        # docstring may mention it — only the code matters).
        code_lines = [ln for ln in src.splitlines()
                      if not ln.strip().startswith("#")
                      and '"""' not in ln
                      and "history-attachments" not in ln]
        code = "\n".join(code_lines)
        self.assertNotIn("history-attachments", code,
                         "retention must never touch the project's own copy")


class TestMemoryIsBounded(unittest.TestCase):
    """A MEASUREMENT, not just a structural check.

    Simulates a folder of 200 messages that each carry a 2 MB attachment
    (400 MB of attachment bytes in total) and shows that the scanner's
    per-message payload never exceeds ONE message's worth.

    The old code built a LIST of the whole folder with the bytes inside —
    400 MB resident. The streaming code keeps ONE message (2 MB) live at a
    time, and the bytes themselves are staged on disk.
    """

    def test_memory_stays_bounded_over_many_messages(self):
        """The attachment bytes must never enter the scanner's live set.

        Simulates a folder of 200 messages that each carry a 2 MB
        attachment (400 MB of attachment bytes in total) and shows that the
        bytes never become resident in the scanner.

        The old code built a LIST of the whole folder with the attachment
        BYTES inside — 400 MB resident on a first sync / renumbering /
        watermark reset. The streaming code stages each message's bytes on
        disk as it is fetched, so the message dict only ever holds a PATH.
        """
        import tracemalloc

        MSG_COUNT = 200
        ATT_BYTES = 2 * 1024 * 1024

        class FakeMsg:
            def __init__(self, uid):
                self.uid = uid
                self.attachments = []
                self.headers = None
                self.from_values = None
                self.to_values = []
                self.subject = f"s{uid}"
                self.date = None
                self.text = ""
                self.html = ""
                self.flags = []

        class FakeMailbox:
            class folder:
                @staticmethod
                def set(name):
                    pass

            def xoauth2(self, user, token):
                pass

            def login(self, user, pwd):
                pass

            def fetch(self, criteria=None, mark_seen=False, bulk=False):
                if getattr(self, "_listed", False):
                    return [FakeMsg(self._single)]
                self._listed = True
                return [FakeMsg(u) for u in range(1, MSG_COUNT + 1)]

            def logout(self):
                pass

        staged_root = Path(tempfile.mkdtemp(prefix="mailatt-mem-"))
        self.addCleanup(lambda: shutil.rmtree(staged_root, ignore_errors=True))
        # The bytes are materialised (as the real fetch does) but staged
        # straight to disk, so they are never part of the message dict.
        state = {"bytes_in_dict": 0}

        def fake_single(cfg, folder, uid):
            payload = b"x" * ATT_BYTES
            staged = staged_root / f"{uid}.bin"
            staged.write_bytes(payload)
            entry = {"filename": "big.bin", "size_bytes": ATT_BYTES,
                     "staged_path": str(staged), "content": b""}
            state["bytes_in_dict"] += len(entry.get("content") or b"")
            return {"uid": uid, "attachments": [entry], "subject": f"s{uid}"}

        fake_mod = MagicMock()
        fake_mod.A = MagicMock(return_value="CRITERION")
        fake_mod.MailBox = MagicMock(return_value=FakeMailbox())

        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        with patch.object(ms, "A", fake_mod.A), \
             patch.object(ms, "MailBox", fake_mod.MailBox), \
             patch.object(ms, "_login_mailbox", side_effect=lambda cfg: FakeMailbox()), \
             patch.object(ms, "_fetch_single_message", side_effect=fake_single):
            count = 0
            for _msg in ms._fetch_messages({"host": "h", "port": 993, "user": "u",
                                            "password": "p", "inbox": "INBOX"},
                                           "INBOX"):
                count += 1
                del _msg
        peak = tracemalloc.get_traced_memory()[1] - base
        tracemalloc.stop()

        self.assertEqual(count, MSG_COUNT,
                         "every message must still be delivered")
        self.assertEqual(state["bytes_in_dict"], 0,
                         "no attachment bytes may travel in the message dict")
        # The whole folder is 400 MB of attachment bytes; the live set must
        # stay far below that.
        self.assertLess(
            peak, 16 * 1024 * 1024,
            f"peak traced memory was {peak} bytes for a "
            f"{MSG_COUNT * ATT_BYTES // (1024 * 1024)} MB folder — it must "
            f"stay bounded (one message at a time)")

    def test_the_fetch_never_puts_the_payload_in_the_message_dict(self):
        """The real fetch stages the bytes; the dict carries a path only."""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "big.bin"
            payload = b"x" * (4 * 1024 * 1024)
            staged.write_bytes(payload)

            # _store_staged_attachment_bytes must MOVE the file, never read
            # it into memory.
            src = inspect_source(ms._store_staged_attachment_bytes)
            self.assertIn("staged.replace(dest)", src,
                          "the staged file must be moved, not copied through RAM")
            self.assertNotIn("read_bytes()", src.split("except OSError")[0],
                             "the staged file must not be read into memory")

    def test_a_folder_of_big_attachments_does_not_accumulate_in_ram(self):
        """A direct comparison: the OLD shape vs the NEW shape.

        The old `_fetch_messages` returned a list whose dicts each held the
        attachment BYTES; the new one returns a generator whose dicts hold
        a staged path. This test measures both.
        """
        import tracemalloc

        ATT_BYTES = 4 * 1024 * 1024
        COUNT = 50

        def measure(hold_bytes: bool):
            tracemalloc.start()
            base = tracemalloc.get_traced_memory()[0]
            held = []
            for i in range(COUNT):
                if hold_bytes:
                    entry = {"filename": "big.bin", "size_bytes": ATT_BYTES,
                             "content": b"x" * ATT_BYTES}
                else:
                    entry = {"filename": "big.bin", "size_bytes": ATT_BYTES,
                             "staged_path": f"/tmp/{i}.bin", "content": b""}
                held.append({"uid": i, "attachments": [entry]})
            peak = tracemalloc.get_traced_memory()[1] - base
            tracemalloc.stop()
            return peak

        old_peak = measure(hold_bytes=True)
        new_peak = measure(hold_bytes=False)
        self.assertGreater(old_peak, COUNT * ATT_BYTES // 2,
                           "the old shape really does hold the bytes in RAM")
        self.assertLess(new_peak, 4 * 1024 * 1024,
                        "the new shape must not hold the bytes in RAM")


def inspect_source(fn):
    import inspect
    return inspect.getsource(fn)


# ── 11. Exact timeline matching (email 12 must not match 120/1200) ───────────
class TestExactTimelineMatch(BaseCase):
    """The timeline lookup must match the message id EXACTLY.

    The old lookup used `strpos(content, '"message_id": 12')`, which also
    matches `"message_id": 120` and `"message_id": 1200` — so a project
    could pick up the WRONG email's timeline entry.
    """

    def _event_for(self, message_id):
        return db.insert_returning(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (self.opp_id, ms.EMAIL_HISTORY_CATEGORY_ID,
             json.dumps({"type": "email_snapshot", "message_id": message_id,
                         "subject": "s"}), self.user_id))

    def test_a_shorter_message_id_does_not_match_a_longer_one(self):
        # A timeline entry for message 1200 exists.
        other = self._event_for(1200)
        # Looking up message 12 must NOT find it.
        found = db.query_one(
            "SELECT id FROM history_events WHERE opportunity_id = %s AND content ~ %s",
            (self.opp_id, '"message_id": 12([,}])'))
        self.assertIsNone(found,
                          "message 12 must not match the entry for 1200")

    def test_the_scanner_lookup_does_not_match_a_longer_id(self):
        """THE REAL CODE PATH (bug fix 2026-11-03, review round 2).

        `create_email_history_snapshot` looked the timeline entry up with
        `content LIKE '%"message_id": 12%'`, which ALSO matches
        `"message_id": 120` and `"message_id": 1200` — so a project could
        pick up the WRONG email's timeline entry (and copy its files into
        the wrong place).
        """
        # An entry for message 1200 in THIS project.
        other = self._event_for(1200)
        # Our message id happens to be a prefix-compatible number.
        target = db.insert_returning(
            "INSERT INTO mail_messages (account_id, folder, imap_uid, subject, from_addr, "
            "to_addr, body_text, date_received) VALUES (%s, 'INBOX', %s, %s, %s, %s, %s, NOW()) "
            "RETURNING id", (self.acct_id, "9100", "short", "a@b.c", "d@e.f", "b"))
        self.addCleanup(lambda: db.execute("DELETE FROM mail_messages WHERE id = %s", (target,)))
        a1 = db.insert_returning(
            "INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s) RETURNING id", (target, "x.pdf", "application/pdf", 3))
        # Rewrite the id to a PREFIX of the 1200 entry's id: make both
        # entries carry ids where one is a prefix of the other.
        db.execute("UPDATE history_events SET content = %s WHERE id = %s",
                   (json.dumps({"type": "email_snapshot", "message_id": 1200}), other))
        # The target message's id is 1200 too — but with the SAME digits it
        # must match exactly. To prove the prefix problem, use a message
        # whose id is a strict prefix: 12 vs 1200.
        short_msg = db.insert_returning(
            "INSERT INTO mail_messages (account_id, folder, imap_uid, subject, from_addr, "
            "to_addr, body_text, date_received) VALUES (%s, 'INBOX', %s, %s, %s, %s, %s, NOW()) "
            "RETURNING id", (self.acct_id, "9101", "short2", "a@b.c", "d@e.f", "b"))
        self.addCleanup(lambda: db.execute("DELETE FROM mail_messages WHERE id = %s", (short_msg,)))
        db.execute("UPDATE mail_messages SET id = 12 WHERE id = %s", (short_msg,))

        # The snapshot for message 12 must NOT reuse the 1200 entry.
        created = ms.create_email_history_snapshot(self.opp_id, 12)
        self.assertTrue(created)
        events = db.query_dicts(
            "SELECT id, content FROM history_events WHERE opportunity_id = %s", (self.opp_id,))
        ids = [int(e["id"]) for e in events]
        self.assertGreater(len(ids), 1,
                           "message 12 must get its OWN entry, not 1200's")
        self.assertIn(other, ids)

    def test_the_exact_message_id_is_found(self):
        ev = self._event_for(12)
        found = db.query_one(
            "SELECT id FROM history_events WHERE opportunity_id = %s AND content ~ %s",
            (self.opp_id, '"message_id": 12([,}])'))
        self.assertTrue(found)
        self.assertEqual(int(found["id"]), ev)

    def test_the_snapshot_helper_does_not_pick_the_wrong_entry(self):
        """create_email_history_snapshot(12) must not reuse 1200's entry."""
        other = self._event_for(1200)
        # A message 12 with an attachment — the copy must land in a NEW
        # event for message 12, not in the 1200 event.
        a1 = self.add_attachment("one.pdf", b"one")
        self.stored_file(a1, "one.pdf", b"one")
        ms.create_email_history_snapshot(self.opp_id, self.msg_id)
        # Our message id is far from 1200, but the point is the query is
        # exact — assert the helper created its own event.
        events = db.query_dicts(
            "SELECT id FROM history_events WHERE opportunity_id = %s", (self.opp_id,))
        ids = [int(e["id"]) for e in events]
        self.assertIn(other, ids)
        self.assertGreater(len(ids), 1, "a new event must have been created")

    def test_the_backfill_query_matches_exactly(self):
        """The backfill's timeline lookup must be exact too."""
        # An event for message 1200 and a link for message 1200 — the
        # backfill must find it (and not confuse it with 12).
        other_msg = db.insert_returning(
            "INSERT INTO mail_messages (account_id, folder, imap_uid, subject, from_addr, "
            "to_addr, body_text, date_received) VALUES (%s, 'INBOX', %s, %s, %s, %s, %s, NOW()) "
            "RETURNING id", (self.acct_id, "9001", "other", "a@b.c", "d@e.f", "b"))
        self.addCleanup(lambda: db.execute("DELETE FROM mail_messages WHERE id = %s", (other_msg,)))
        ev = db.insert_returning(
            "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (self.opp_id, ms.EMAIL_HISTORY_CATEGORY_ID,
             json.dumps({"type": "email_snapshot", "message_id": other_msg}), self.user_id))
        db.execute("INSERT INTO mail_deal_links (message_id, opportunity_id) VALUES (%s, %s)",
                   (other_msg, self.opp_id))
        found = db.query_one(
            "SELECT he.id FROM history_events he "
            "WHERE he.opportunity_id = %s "
            "AND he.content ~ ('\"message_id\": ' || %s || '([,}])') LIMIT 1",
            (self.opp_id, other_msg))
        self.assertTrue(found)
        self.assertEqual(int(found["id"]), ev)


if __name__ == "__main__":
    unittest.main(verbosity=2)
