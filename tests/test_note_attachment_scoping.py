#!/usr/bin/env python3
"""Unit tests for note-attachment scoping (phantom cross-project files).

Live case: pasting images into a preview note uploaded them to /photos, whose
ids were then sent as fileIds — but the server resolves fileIds against
project_documents, a different table. Photo ids 5/6 linked project_documents
5/6 (iws_calculation / ALLEN-G PDFs from other projects) onto Pete Morrison's
note (event 49444, history_attachments rows 19382/19383).

Locks in both halves of the fix with a mocked db (no live Postgres needed):
  1. _handle_project_history_create scopes the doc lookup by opportunity_id,
     so foreign-opp fileIds are rejected and only own-opp docs link.
  2. The preview submit path no longer merges photo uploadedIds into fileIds.

Run with: python3 tests/test_note_attachment_scoping.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}


class Shim:
    class headers:
        @staticmethod
        def get(key, default=None):
            return default

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    wfile = MagicMock()


def _call_create_note(opp_id, body_dict, doc_rows, user=ADMIN):
    """Invoke _handle_project_history_create with stubbed db.

    doc_rows: {doc_id: tuple(id, title, path, size, mime, uploader, opp_id)}.
    Returns (responses, executes).
    """
    import json as _json

    responses = []
    executes = []

    def fake_query(sql, params=None, *a, **k):
        if "FROM project_documents" in sql:
            row = doc_rows.get(params[0])
            if row and len(row) == 7:
                doc_id, _title, _path, _size, _mime, _up, doc_opp = row
                # Emulate the real WHERE clause, including the opp scoping.
                if "opportunity_id" in sql and doc_opp != params[1]:
                    return None
                return row[:6]
            return None
        return None

    def fake_execute(sql, params=None, *a, **k):
        executes.append((sql, params))
        return None

    mock_db = MagicMock()
    mock_db.query.side_effect = fake_query
    mock_db.execute.side_effect = fake_execute
    mock_db.insert_returning.return_value = 777

    with patch.object(_server, "_require_auth", lambda h: user), \
        patch.object(_server, "_read_body", lambda h: _json.dumps(body_dict).encode()), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_project_history_create(Shim(), opp_id)
    return responses, executes


def _attachment_inserts(executes):
    return [e for e in executes if "INSERT INTO history_attachments" in e[0]]


class TestCrossOppFileIdsRejected(unittest.TestCase):
    DOCS = {
        # photo ids 5/6 from the live incident resolve to other opps' docs
        5: (5, "iws_calculation_11029.pdf", "data/history-attachments/54/155/x.pdf", 10, "application/pdf", 1, 174),
        6: (6, "ALLEN-G_Roof.pdf", "data/history-attachments/6/3/y.pdf", 10, "application/pdf", 1, 193),
        7: (7, "mine.pdf", "data/documents/51/mine.pdf", 10, "application/pdf", 1, 51),
    }

    def test_foreign_opp_files_not_linked(self):
        responses, executes = _call_create_note(
            51, {"content": "note", "categoryId": 1, "fileIds": ["5", "6", "abc"]}, self.DOCS)
        self.assertEqual(responses[0][0], 201)
        self.assertEqual(_attachment_inserts(executes), [])

    def test_own_opp_file_still_links(self):
        responses, executes = _call_create_note(
            51, {"content": "note", "categoryId": 1, "fileIds": ["7"]}, self.DOCS)
        self.assertEqual(responses[0][0], 201)
        inserts = _attachment_inserts(executes)
        self.assertEqual(len(inserts), 1)
        _sql, params = inserts[0]
        self.assertEqual(params[0], 777)  # event_id
        self.assertEqual(params[1], "mine.pdf")

    def test_lookup_scoped_by_opportunity(self):
        src = open("/home/chapman-server/Projects/new-crm/server.py", encoding="utf-8").read()
        start = src.index("Link uploaded document IDs as history attachments")
        block = src[start:start + 1200]
        self.assertIn("opportunity_id", block)


class TestPreviewSubmitNoPhotoIds(unittest.TestCase):
    def test_uploaded_ids_never_feed_file_ids(self):
        src = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        start = src.index("// Upload pending images.")
        end = src.index("await createOpportunityHistoryEvent(oppId, {", start)
        region = src[start:end]
        self.assertNotIn("uploadedIds", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
