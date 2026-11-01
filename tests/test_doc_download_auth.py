#!/usr/bin/env python3
"""Unit tests for the document-download auth hole fix (Part 1).

`GET /api/v2/documents/{id}` used to serve any customer file with no auth
check at all (sequential ids, `?token=` ignored), and the OnlyOffice save
callback `POST /api/v2/documents/{id}/callback` accepted unsigned bodies
that could make the server download an arbitrary URL over the file on disk.

Covers:
  - unauthenticated download -> 401 (no DB row / filesystem touched first:
    the 401 path never queries)
  - logged-in session cookie -> 200 with file bytes
  - bad session cookie -> 401
  - OnlyOffice per-document JWT (?token=, minted by editor-config) -> 200
  - JWT signed with the wrong secret -> 403
  - JWT bound to a different doc id -> 403
  - stale JWT (ts older than the TTL) -> 403
  - unsigned/missing JWT -> 401
  - history-attachment + photo + batch downloads still require auth (pins)
  - callback without a token -> 403; with a valid token -> 200 {"error": 0}
  - source guard: the download handler authorizes before touching storage

fs is real (tmp files under a patched ROOT); db is mocked (no Postgres).
`import server` still needs a reachable DB for its connection pool, so like
the other server-importing suites this file SKIPs cleanly without one.

Run with: python3 tests/test_doc_download_auth.py
"""

import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import server as _server
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    _server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    sys.exit(0)


TEST_SECRET = "unit-test-docs-secret"


class Shim:
    """Minimal KanbanHandler stand-in: headers/path/command + response capture."""

    def __init__(self, path="/", headers=None, command="GET", body=b""):
        self.path = path
        self.headers = headers or {}
        self.command = command
        self._req_body = body
        self.status = None
        self.sent = {}
        self.body = b""

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


def _doc_row(filename="report.pdf", rel="docs/report.pdf"):
    return {
        "opportunity_id": 43, "title": filename, "file_path": rel,
        "file_size": 3, "mime_type": "application/pdf",
    }


def _patches(*, session_user="unset", db_query_one=None, secret=TEST_SECRET):
    """Common patches: DOCS secret, session lookup, db layer, body reader."""
    ps = [patch.object(_server, "DOCS_JWT_SECRET", secret)]
    if session_user == "unset":
        ps.append(patch.object(_server.auth_mod, "get_session_user",
                               return_value={"id": 7, "is_admin": True}))
    elif session_user is None:
        ps.append(patch.object(_server.auth_mod, "get_session_user", return_value=None))
    else:
        ps.append(patch.object(_server.auth_mod, "get_session_user", return_value=session_user))
    mock_db = MagicMock()
    mock_db.query_one.return_value = db_query_one
    ps.append(patch.object(_server, "db", mock_db))
    return ps, mock_db


def _run(patches, fn, *args):
    for p in patches:
        p.start()
    try:
        fn(*args)
    finally:
        for p in reversed(patches):
            p.stop()


def _download(path, headers=None, row=None, tmp=None, session_user="unset"):
    shim = Shim(path=path, headers=headers or {}, command="GET")
    ps, mock_db = _patches(session_user=session_user, db_query_one=row)
    if tmp is not None:
        ps.append(patch.object(_server, "ROOT", Path(tmp)))
    _run(ps, _server.KanbanHandler._handle_document_download, shim, 1)
    return shim, mock_db


def _mint(doc_id=1, secret=TEST_SECRET, ts=None):
    return _server._sign_jwt(
        {"id": doc_id, "path": "docs/report.pdf",
         "ts": ts if ts is not None else int(time.time())},
        secret,
    )


class TestDocumentDownloadAuth(unittest.TestCase):
    def test_unauthenticated_returns_401_without_touching_db(self):
        with TemporaryDirectory() as tmp:
            shim, mock_db = _download("/api/v2/documents/1", row=None, tmp=tmp,
                                      session_user=None)
        self.assertEqual(shim.status, 401)
        mock_db.query_one.assert_not_called()

    def test_bad_session_cookie_returns_401(self):
        with TemporaryDirectory() as tmp:
            shim, _ = _download("/api/v2/documents/1",
                                headers={"Cookie": "sietch_session=stale"},
                                row=None, tmp=tmp, session_user=None)
        self.assertEqual(shim.status, 401)

    def test_session_cookie_returns_file(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "docs").mkdir()
            (Path(tmp) / "docs" / "report.pdf").write_bytes(b"PDFDATA")
            shim, _ = _download(
                "/api/v2/documents/1",
                headers={"Cookie": "sietch_session=good"},
                row=_doc_row(), tmp=tmp)
        self.assertEqual(shim.status, 200)
        self.assertEqual(shim.body, b"PDFDATA")
        self.assertEqual(shim.sent["Content-Type"], "application/pdf")

    def test_onlyoffice_signed_token_returns_file(self):
        token = _mint(doc_id=1)
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "docs").mkdir()
            (Path(tmp) / "docs" / "report.pdf").write_bytes(b"PDFDATA")
            shim, _ = _download(f"/api/v2/documents/1?token={token}",
                                row=_doc_row(), tmp=tmp, session_user=None)
        self.assertEqual(shim.status, 200)
        self.assertEqual(shim.body, b"PDFDATA")

    def test_token_signed_with_wrong_secret_rejected(self):
        token = _mint(doc_id=1, secret="attacker-secret")
        with TemporaryDirectory() as tmp:
            shim, mock_db = _download(f"/api/v2/documents/1?token={token}",
                                       row=None, tmp=tmp, session_user=None)
        self.assertEqual(shim.status, 403)
        mock_db.query_one.assert_not_called()

    def test_token_for_other_doc_rejected(self):
        token = _mint(doc_id=999)
        with TemporaryDirectory() as tmp:
            shim, mock_db = _download(f"/api/v2/documents/1?token={token}",
                                       row=None, tmp=tmp, session_user=None)
        self.assertEqual(shim.status, 403)
        mock_db.query_one.assert_not_called()

    def test_stale_token_rejected(self):
        token = _mint(doc_id=1, ts=int(time.time()) - 10 * 86400)
        with TemporaryDirectory() as tmp:
            shim, _ = _download(f"/api/v2/documents/1?token={token}",
                                row=None, tmp=tmp, session_user=None)
        self.assertEqual(shim.status, 403)

    def test_garbage_token_rejected(self):
        with TemporaryDirectory() as tmp:
            shim, _ = _download("/api/v2/documents/1?token=not-a-jwt",
                                row=None, tmp=tmp, session_user=None)
        self.assertEqual(shim.status, 403)

    def test_missing_file_still_404_for_authorized_user(self):
        with TemporaryDirectory() as tmp:
            shim, _ = _download("/api/v2/documents/1",
                                headers={"Cookie": "sietch_session=good"},
                                row=None, tmp=tmp)
        self.assertEqual(shim.status, 404)


class TestOtherFileRoutesStillGated(unittest.TestCase):
    """Pins: the routes Part 1 audited were already gated — keep them so."""

    def test_history_attachment_requires_auth(self):
        shim = Shim(path="/api/v2/history-attachments/5/download", headers={}, command="GET")
        ps, _ = _patches(session_user=None, db_query_one=None)
        _run(ps, _server.KanbanHandler._handle_history_attachment_download, shim, 5)
        self.assertEqual(shim.status, 401)

    def test_photo_download_requires_auth(self):
        shim = Shim(path="/api/v2/photos/3", headers={}, command="GET")
        ps, _ = _patches(session_user=None, db_query_one=None)
        _run(ps, _server.KanbanHandler._handle_photo_download, shim, 3)
        self.assertEqual(shim.status, 401)

    def test_documents_batch_download_requires_auth(self):
        import json as _json
        shim = Shim(path="/api/v2/documents/batch-download", headers={},
                    command="POST", body=_json.dumps({"ids": [1]}).encode())
        responses = []
        ps, _ = _patches(session_user=None, db_query_one=None)

        def fake_require_auth(h):
            responses.append(401)
            return None

        ps.append(patch.object(_server, "_require_auth", fake_require_auth))
        ps.append(patch.object(_server, "_read_body", lambda h: shim._req_body))
        ps.append(patch.object(_server, "_json_response",
                               lambda h, s, b: responses.append(s)))
        _run(ps, _server.KanbanHandler._handle_documents_batch_download, shim)
        self.assertIn(401, responses)


class TestDocumentCallbackAuth(unittest.TestCase):
    def _callback(self, doc_id, body_dict, secret=TEST_SECRET):
        import json as _json
        shim = Shim(path=f"/api/v2/documents/{doc_id}/callback", headers={},
                    command="POST", body=_json.dumps(body_dict).encode())
        ps, mock_db = _patches(session_user=None, db_query_one=None, secret=secret)
        ps.append(patch.object(_server, "_read_body", lambda h: shim._req_body))
        _run(ps, _server.KanbanHandler._handle_document_callback, shim, doc_id)
        return shim, mock_db

    def test_callback_without_token_rejected(self):
        shim, _ = self._callback(1, {"status": 1, "key": "sietch-doc-1"})
        self.assertEqual(shim.status, 403)

    def test_callback_with_forged_token_rejected(self):
        bad = _server._sign_jwt({"key": "sietch-doc-1"}, "attacker-secret")
        shim, _ = self._callback(1, {"status": 1, "key": "sietch-doc-1", "token": bad})
        self.assertEqual(shim.status, 403)

    def test_callback_token_for_other_doc_rejected(self):
        other = _server._sign_jwt({"key": "sietch-doc-999"}, TEST_SECRET)
        shim, _ = self._callback(1, {"status": 1, "key": "sietch-doc-999", "token": other})
        self.assertEqual(shim.status, 403)

    def test_callback_with_valid_token_accepted(self):
        # status 1 ("editing") performs no download and no DB write, so the
        # auth path is exercised without touching storage or network.
        good = _server._sign_jwt({"key": "sietch-doc-1"}, TEST_SECRET)
        shim, mock_db = self._callback(1, {"status": 1, "key": "sietch-doc-1", "token": good})
        self.assertEqual(shim.status, 200)
        self.assertIn(b'"error": 0', shim.body)
        mock_db.query_one.assert_not_called()

    def test_callback_config_style_token_accepted(self):
        # The editor config itself is signed as config["token"]; the
        # verifier also accepts the document.key nested under "document".
        cfg = {"document": {"key": "sietch-doc-1", "url": "x", "title": "t"}}
        tok = _server._sign_jwt(cfg, TEST_SECRET)
        shim, _ = self._callback(1, {"status": 1, "key": "sietch-doc-1", "token": tok})
        self.assertEqual(shim.status, 200)


class TestDownloadAuthSourceGuard(unittest.TestCase):
    """Pin the fix structurally: the download handler must authorize before storage."""

    def test_download_handler_authorizes_first(self):
        src = Path(_server.__file__).read_text(encoding="utf-8")
        start = src.index("def _handle_document_download(")
        body = src[start:start + 2500]
        self.assertIn("_authorize_document_fetch", body)
        # Auth gate precedes any DB or filesystem access in the handler.
        self.assertLess(body.index("_authorize_document_fetch"), body.index("db.query_one"))

    def test_callback_handler_verifies_token(self):
        src = Path(_server.__file__).read_text(encoding="utf-8")
        start = src.index("def _handle_document_callback(")
        body = src[start:start + 1500]
        self.assertIn("_verify_document_callback", body)


if __name__ == "__main__":
    unittest.main(verbosity=1)
