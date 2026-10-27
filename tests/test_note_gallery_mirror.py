#!/usr/bin/env python3
"""Unit tests for note-image gallery mirror (2026-10-27).

Paperclip attachments on a note go to project_documents only; the Photos tab
never showed them. Fix: _handle_project_history_create mirrors explicit image
attachments into project_photos via _store_note_image_to_gallery (same
pipeline as photo upload: EXIF + JPEG thumbnail + content-hash dedup).

Run with: python3 tests/test_note_gallery_mirror.py (mocked db, real tmp files, no Postgres)
"""
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}

PNG_1PX = (
    # Valid 8x8 RGB PNG, generated once and inlined so the suite has no
    # filesystem fixtures (thumbnail/EXIF path needs decodable bytes).
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08"
    b"\x08\x02\x00\x00\x00Km)\xdc\x00\x00\x00\x11IDATx\x9cc8\x91b\x84\x151"
    b"\x0c-\t\x00VFW\x81<\xbd\x9f\x89\x00\x00\x00\x00IEND\xaeB`\x82"
)


class Shim:
    headers = {"Cookie": ""}

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


def _create_payload(file_ids=(11,)):
    import json as _json
    return _json.dumps({"content": "spoke with Robin", "categoryId": 1,
                        "fileIds": list(file_ids)}).encode()


def _run_create(doc_row, tmp, file_bytes=b"%PDF-fake"):
    responses = []
    executes = []
    inserted = []
    mock_db = MagicMock()

    def fake_query(sql, params=None, *a, **k):
        if "FROM project_documents" in sql:
            return doc_row
        return []

    def fake_insert_returning(sql, params=None, *a, **k):
        inserted.append((sql, params))
        if "history_replies" in sql or "history_events" in sql and "RETURNING" in sql:
            return 50
        if "project_photos" in sql:
            return 77
        return 1

    mock_db.query.side_effect = fake_query
    mock_db.query_one.side_effect = lambda *a, **k: None
    mock_db.insert_returning.side_effect = fake_insert_returning
    mock_db.execute.side_effect = lambda sql, params=None, *a, **k: executes.append((sql, params))
    photo_dir = Path(tmp) / "data" / "photos"
    with patch.object(_server, "_require_auth", lambda h: ADMIN), \
        patch.object(_server, "_read_body", lambda h: _create_payload()), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"), \
        patch.object(_server, "ROOT", Path(tmp)), \
        patch.object(_server, "PHOTO_STORAGE_PATH", photo_dir):
        _server.KanbanHandler._handle_project_history_create(Shim(), 46)
    return responses, executes, inserted


class TestGalleryMirror(unittest.TestCase):
    def test_image_doc_mirrors_to_photos(self):
        tmp = TemporaryDirectory()
        try:
            rel_dir = Path(tmp.name) / "data" / "history-attachments" / "46" / "50"
            rel_dir.mkdir(parents=True, exist_ok=True)
            (rel_dir / "11_shingle.jpg").write_bytes(PNG_1PX)
            doc_row = (11, "shingle.jpg", "data/history-attachments/46/50/11_shingle.jpg",
                       len(PNG_1PX), "image/jpeg", 7)
            responses, executes, inserted = _run_create(doc_row, tmp.name)
            self.assertTrue(responses and responses[0][0] == 201)
            photo_inserts = [p for s, p in inserted if "project_photos" in s]
            self.assertEqual(len(photo_inserts), 1)
            self.assertEqual(photo_inserts[0][0], 46)
            self.assertEqual(photo_inserts[0][1], "shingle.jpg")
            gallery_files = list((Path(tmp.name) / "data" / "photos" / "46").glob("*"))
            self.assertTrue(any("shingle" in f.name for f in gallery_files))
            self.assertTrue(any(f.name.startswith("thumb_") for f in gallery_files))
        finally:
            tmp.cleanup()

    def test_pdf_doc_does_not_mirror(self):
        tmp = TemporaryDirectory()
        try:
            rel_dir = Path(tmp.name) / "data" / "history-attachments" / "46" / "50"
            rel_dir.mkdir(parents=True, exist_ok=True)
            (rel_dir / "11_invoice.pdf").write_bytes(b"%PDF-fake")
            doc_row = (11, "invoice.pdf", "data/history-attachments/46/50/11_invoice.pdf",
                       10, "application/pdf", 7)
            responses, executes, inserted = _run_create(doc_row, tmp.name)
            self.assertTrue(responses and responses[0][0] == 201)
            # history link still recorded...
            self.assertTrue(any("history_attachments" in s for s, _ in executes))
            # ...but no gallery row
            self.assertEqual([p for s, p in inserted if "project_photos" in s], [])
        finally:
            tmp.cleanup()

    def test_dedup_second_link_single_row(self):
        tmp = TemporaryDirectory()
        try:
            rel_dir = Path(tmp.name) / "data" / "history-attachments" / "46" / "50"
            rel_dir.mkdir(parents=True, exist_ok=True)
            (rel_dir / "11_shingle.jpg").write_bytes(PNG_1PX)
            doc_row = (11, "shingle.jpg", "data/history-attachments/46/50/11_shingle.jpg",
                       len(PNG_1PX), "image/jpeg", 7)
            _, _, first = _run_create(doc_row, tmp.name)
            self.assertEqual(len([p for s, p in first if "project_photos" in s]), 1)
        finally:
            tmp.cleanup()

    def test_scope_guard_foreign_doc_not_linked(self):
        tmp = TemporaryDirectory()
        try:
            responses, executes, inserted = _run_create(None, tmp.name)
            self.assertTrue(responses and responses[0][0] == 201)
            self.assertEqual([p for s, p in inserted if "project_photos" in s], [])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=1)
