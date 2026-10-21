#!/usr/bin/env python3
"""Unit tests for documents batch-download-as-zip.

The Documents tab Download action fired one dialog per file; 2+ files now
POST /api/v2/documents/batch-download and fetch a single zip (mirrors the
photos batch endpoint). Single-file downloads keep the direct path.

fs is real (tmp files under a patched ROOT); db is mocked (no Postgres).

Run with: python3 tests/test_documents_batch_download.py
"""
import io
import sys
import unittest
import zipfile
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
        self.sent = {}
        self.status = None
        self.body = b""

    def send_response(self, code):
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


def _call(body_dict, rows, user=ADMIN, tmp=None):
    import json as _json
    responses = []
    shim = Shim()
    mock_db = MagicMock()
    mock_db.query.return_value = rows

    def fake_require_auth(h):
        if user is None:
            responses.append((401, {"error": "Not authenticated"}))
            return None
        return user

    patches = [
        patch.object(_server, "_require_auth", fake_require_auth),
        patch.object(_server, "_read_body", lambda h: _json.dumps(body_dict).encode()),
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))),
        patch.object(_server, "db", mock_db),
        patch.object(_server, "logger"),
    ]
    if tmp is not None:
        patches.append(patch.object(_server, "ROOT", Path(tmp)))
    for p in patches:
        p.start()
    try:
        _server.KanbanHandler._handle_documents_batch_download(shim)
    finally:
        for p in reversed(patches):
            p.stop()
    return shim, responses


class TestBatchDownload(unittest.TestCase):
    def _files(self, tmp):
        (Path(tmp) / "a.pdf").write_bytes(b"AAA")
        (Path(tmp) / "b.pdf").write_bytes(b"BBB")
        return [
            (1, 43, "report.pdf", "a.pdf", 3, "application/pdf"),
            (2, 43, "report.pdf", "b.pdf", 3, "application/pdf"),
            (3, 43, "gone.pdf", "missing.pdf", 3, "application/pdf"),
        ]

    def test_zip_dedupes_and_skips_missing(self):
        with TemporaryDirectory() as tmp:
            rows = self._files(tmp)
            shim, responses = _call({"ids": [1, 2, 3]}, rows, tmp=tmp)
        self.assertEqual(shim.status, 200)
        self.assertEqual(shim.sent["Content-Type"], "application/zip")
        self.assertIn("project-43-documents.zip", shim.sent["Content-Disposition"])
        names = zipfile.ZipFile(io.BytesIO(shim.body)).namelist()
        self.assertIn("report.pdf", names)
        self.assertIn("report (1).pdf", names)
        self.assertEqual(len(names), 2)  # missing file skipped, not fatal

    def test_empty_ids_400(self):
        shim, responses = _call({"ids": []}, [])
        self.assertEqual(responses[0][0], 400)

    def test_no_rows_404(self):
        shim, responses = _call({"ids": [9]}, [])
        self.assertEqual(responses[0][0], 404)

    def test_requires_auth(self):
        shim, responses = _call({"ids": [1]}, [], user=None)
        self.assertEqual(responses[0][0], 401)


class TestDownloadActionWiring(unittest.TestCase):
    def _region(self):
        js = (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(encoding="utf-8")
        at = js.index('actionsSelect.addEventListener("change"')
        return js[at:at + 3500]

    def test_multi_select_routes_to_batch(self):
        region = self._region()
        self.assertIn("/api/v2/documents/batch-download", region)
        self.assertIn("ids.length === 1", region)

    def test_single_file_keeps_direct_download(self):
        region = self._region()
        self.assertIn("/api/v2/documents/${ids[0]}", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
