#!/usr/bin/env python3
"""Unit tests for linked-email attachments in project history (2026-10-22).

Three gaps, all covered:
1. Compose-send never stored attachments anywhere downloadable: attachments_json
   metadata only, zero mail_attachments rows (only the IMAP scanner wrote those).
2. The download endpoint was IMAP-only: Sent copies (synthetic UIDs, no IMAP row)
   could never resolve.
3. The history timeline never showed linked files: snapshots carry filenames
   without URLs, and history_attachments has no rows for mail.

Run with: python3 tests/test_linked_email_attachments.py (mocked db, real tmp files, no Postgres)
"""
import base64
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


class Shim:
    headers = {"Cookie": ""}

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def __init__(self):
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


ACCT = {"id": 1, "email": "a@outlook.com", "smtp_from_name": "",
        "auto_bcc_addr": ""}


def _send_payload():
    return {
        "account_id": 1, "to": "t@x.com", "subject": "s", "body": "<p>hi</p>",
        "attachments": [{"filename": "est.pdf", "mime_type": "application/pdf",
                         "content": base64.b64encode(b"%PDF-1.4 fake").decode()}],
    }


class TestSendPersistsAttachments(unittest.TestCase):
    def test_send_writes_files_and_rows(self):
        import json as _json
        tmp = TemporaryDirectory()
        responses = []
        executes = []
        mock_db = MagicMock()

        def fake_query_one(sql, params=None, *a, **k):
            if "FROM mail_accounts" in sql:
                return dict(ACCT)
            if "INSERT INTO mail_outgoing" in sql:
                return {"id": 3}
            if "INSERT INTO mail_messages" in sql:
                return {"id": 9}
            return None

        mock_db.query_one.side_effect = fake_query_one
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: executes.append((sql, params))
        shim = Shim()
        shim._try_send_for_account = lambda acct, **kw: (True, None, "graph")
        with patch.object(_server, "_require_auth", lambda h: ADMIN), \
            patch.object(_server, "_mail_account_accessible", lambda u, i: True), \
            patch.object(_server, "_read_body", lambda h: _json.dumps(_send_payload()).encode()), \
            patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"), \
            patch.object(_server, "ROOT", Path(tmp.name)):
            _server.KanbanHandler._handle_mail_send(shim)
        self.assertTrue(responses and responses[0][0] == 200)
        att_inserts = [p for s, p in executes if "INSERT INTO mail_attachments" in s]
        self.assertEqual(len(att_inserts), 1)
        params = att_inserts[0]
        self.assertEqual(params[0], 9)  # message_id of the Sent copy
        self.assertTrue(params[4].startswith("data/mail-attachments/9/"))
        stored = Path(tmp.name) / params[4]
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), b"%PDF-1.4 fake")
        tmp.cleanup()


class TestDownloadStoredPath(unittest.TestCase):
    def test_serves_from_disk_without_imap(self):
        tmp = TemporaryDirectory()
        rel = "data/mail-attachments/9/est.pdf"
        full = Path(tmp.name) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"%PDF-1.4 fake")
        shim = Shim()

        def fake_query_one(sql, params=None, *a, **k):
            if "FROM mail_attachments" in sql:
                return {"id": 4, "message_id": 9, "filename": "est.pdf",
                        "mime_type": "application/pdf", "stored_path": rel,
                        "imap_part_id": None}
            if "FROM mail_messages" in sql:
                return {"imap_uid": "sent:outgoing:3", "folder": "Sent", "account_id": 1}
            if "FROM mail_accounts" in sql:
                return dict(ACCT)
            return None

        mock_db = MagicMock()
        mock_db.query_one.side_effect = fake_query_one
        with patch.object(_server, "_require_auth", lambda h: ADMIN), \
            patch.object(_server, "_mail_message_accessible", lambda u, i: True), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"), \
            patch.object(_server, "ROOT", Path(tmp.name)):
            _server.KanbanHandler._handle_mail_attachment_download(shim, 9, 4)
        self.assertEqual(shim.status, 200)
        self.assertEqual(shim.body, b"%PDF-1.4 fake")
        self.assertIn("est.pdf", shim.headers_sent.get("Content-Disposition", ""))
        tmp.cleanup()


def _history_response(event_rows, mail_att_rows):
    responses = []
    mock_db = MagicMock()

    def fake_query(sql, params=None, *a, **k):
        if "FROM history_events h" in sql:
            return event_rows
        if "FROM mail_attachments" in sql:
            return mail_att_rows
        return []

    mock_db.query.side_effect = fake_query
    with patch.object(_server, "_require_auth", lambda h: ADMIN), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_project_history_get(Shim(), 46, {"count": ["10"], "startIndex": ["0"]})
    assert responses, "no response"
    return responses[0]


def _snap_event(mid):
    snap = {"type": "email_snapshot", "message_id": mid, "subject": "s",
            "from": "a@b.c", "to": "", "cc": "", "date_sent": "",
            "body_html": "", "body_text": "hi",
            "attachments": [{"filename": "est.pdf", "size_bytes": 10, "mime_type": "application/pdf"}]}
    return (100, 13, "Email", "t", json.dumps(snap), 7,
            datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc), None, "Ken")


class TestHistoryResolvesLinkedFiles(unittest.TestCase):
    def test_snapshot_links_become_download_urls(self):
        status, events = _history_response(
            [_snap_event(9)], [(9, 4, "est.pdf")])
        self.assertEqual(status, 200)
        atts = events[0]["attachments"]
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["filename"], "est.pdf")
        self.assertEqual(atts[0]["url"], "/api/v2/mail/messages/9/attachments/4/download")

    def test_no_mail_rows_no_links_no_crash(self):
        status, events = _history_response([_snap_event(9)], [])
        self.assertEqual(status, 200)
        self.assertEqual(events[0]["attachments"], [])

    def test_stale_snapshot_self_heals_on_read(self):
        # Event snapshotted before attachments existed: display reads live rows.
        old = list(_snap_event(9))
        snap = json.loads(old[4])
        snap["attachments"] = []
        old[4] = json.dumps(snap)
        status, events = _history_response([tuple(old)], [(9, 4, "est.pdf")])
        self.assertEqual(len(events[0]["attachments"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
