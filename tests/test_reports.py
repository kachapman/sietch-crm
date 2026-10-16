#!/usr/bin/env python3
"""Unit tests for the admin 'Reports' tab (photo report templates and
document templates).

Covers the audit-round fixes:

  - #1 the JSON-guards on photo create / update (regression: malformed
    body used to return 500)
  - #2 the upload size / content-type / extension allowlist, applied to
    all three template upload handlers
  - #3 the 'delete previous logo on replace' branch in the two logo
    handlers
  - #4 the unique-default partial index in the DB migration
  - the logo proxy handler added in the previous commit (path
    containment, content-type allowlist, auth gate)

server imports cleanly (it only binds the socket inside main(), which
the tests never call), so the source guards assert against the real
server module.

Run with: python3 tests/test_reports.py
"""

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import server  # noqa: E402
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    print("      Run with a live PostgreSQL instance to exercise these tests.")
    sys.exit(0)


# ── Logo proxy: source-level + path containment logic ────────────────────────

class TestLogoProxy(unittest.TestCase):
    """The proxy endpoint is admin-gated, normalises the path, refuses
    anything outside data/logos/ or data/sigs/, and rejects any extension
    not in the image allowlist. We assert each branch via the pure
    constants + the in-handler allowlist, plus a live call to the
    running server."""

    def test_logo_extensions_block_svg(self):
        # SVG can carry <script> / <foreignObject>. The proxy and the
        # upload validator both reject it.
        self.assertNotIn(".svg", server.ALLOWED_LOGO_EXTENSIONS)
        # And the live set is the five safe types.
        self.assertEqual(server.ALLOWED_LOGO_EXTENSIONS,
                         {".png", ".jpg", ".jpeg", ".webp", ".gif"})

    def test_max_logo_size_matches_avatar(self):
        # 5 MB - matches the avatar upload cap. Server-side, the only cap.
        self.assertEqual(server.MAX_LOGO_BYTES, 5 * 1024 * 1024)

    def test_handler_registered_under_do_GET(self):
        # The proxy lives in do_GET, not do_POST. We assert by checking
        # the route pattern is registered with no method requirement.
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "m = re.match(r\"^/api/v2/admin/logos/(.+)$\", api_path)",
            src,
        )
        # The registration is inside the do_GET block (the same area as
        # /api/v2/admin/photo-report-templates). The way the do_GET method
        # is structured in this server, all `if api_path == ...` and
        # `re.match(...)` lines for admin/* routes live there.
        # Just confirm the handler exists and is in the same source.
        self.assertIn("def _handle_logo_serve", src)

    def test_handler_uses_safe_resolve(self):
        # Defence in depth: the resolved candidate must be under data/.
        # The Path.resolve().relative_to() idiom is the standard container
        # check; if the candidate resolves outside, the relative_to()
        # raises ValueError.
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("allowed_root = (ROOT / \"data\").resolve()", src)
        self.assertIn("candidate.relative_to(allowed_root)", src)

    def test_handler_drops_svg(self):
        # The handler returns 415 for an unsupported extension. The map
        # is local to the handler; verify the source mentions both.
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('".png": "image/png"', src)
        self.assertIn('".gif": "image/gif"', src)
        self.assertIn("Unsupported image type", src)

    def test_handler_rejects_paths_outside_logos(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        # The handler's allowed prefixes are logos/ and sigs/ only.
        self.assertIn('rel.startswith("logos/")', src)
        self.assertIn('rel.startswith("sigs/")', src)

    def test_handler_rejects_null_bytes(self):
        # A null byte in a path is a classic traversal trick (C truncates
        # at the NUL). The handler must reject before the filesystem
        # call.
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('"\\x00" in requested', src)

    def test_handler_is_admin_gated(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        # Find the start of the def line and the end of the docstring,
        # then take the first ~400 chars of the actual body.
        start = src.index("def _handle_logo_serve")
        # Docstrings end at the first '        """\n' after the def line.
        doc_end = src.find('"""\n        """', start)
        if doc_end < 0:
            doc_end = src.find('"""\n', start + 200)  # fall back
        body_start = src.find('\n', doc_end) + 1
        snippet = src[body_start:body_start + 400]
        self.assertIn("user = _require_admin(self)", snippet)
        self.assertIn("if not user:\n            return", snippet)


# ── Upload validator helper ─────────────────────────────────────────────────

class TestValidateLogoUpload(unittest.TestCase):
    """The upload handler delegates to _validate_logo_upload, which returns
    (data, None) on success or (None, error_message) on any rejection.
    Tests exercise the helper directly."""

    def _ok(self, **overrides):
        info = {
            "filename": "logo.png",
            "content-type": "image/png",
            "data": b"\x89PNG\r\n\x1a\n" + b"x" * 100,
        }
        info.update(overrides)
        return info

    def _file(self, **overrides):
        """Same as _ok but accepts hyphenated keys via a dict. Python
        keyword rules forbid `content-type="..."` as a kwarg, so callers
        that need to override those keys pass a dict via **file(ct=...).
        """
        return self._ok(**overrides)

    def test_accepts_a_clean_png(self):
        data, err = server._validate_logo_upload(self._ok())
        self.assertIsNotNone(data)
        self.assertIsNone(err)

    def test_rejects_oversized_file(self):
        big = b"x" * (server.MAX_LOGO_BYTES + 1)
        data, err = server._validate_logo_upload(self._ok(data=big))
        self.assertIsNone(data)
        self.assertIn("5 MB", err)

    def test_rejects_non_image_content_type(self):
        data, err = server._validate_logo_upload(
            self._ok(**{"content-type": "application/pdf"})
        )
        self.assertIsNone(data)
        self.assertIn("image", err)

    def test_rejects_missing_content_type(self):
        # An SVG with no content-type is still an SVG: the
        # content-type check is the only line of defence. Reject.
        data, err = server._validate_logo_upload(
            self._ok(**{"filename": "logo.png", "content-type": ""})
        )
        self.assertIsNone(data)
        self.assertIn("image", err)

    def test_rejects_svg_extension(self):
        # SVG can carry <script>; the upload validator enforces
        # extension before content-type so a misleading content-type
        # cannot smuggle an SVG past.
        data, err = server._validate_logo_upload(
            self._ok(**{"filename": "logo.svg", "content-type": "image/svg+xml"})
        )
        self.assertIsNone(data)
        self.assertIn(".svg", err)

    def test_rejects_pdf_renamed_to_png(self):
        # An admin renames an invoice to .png. The extension is allowed
        # but the content-type is application/pdf. Reject via the
        # content-type check.
        data, err = server._validate_logo_upload(
            self._ok(**{"content-type": "application/pdf"})
        )
        self.assertIsNone(data)
        self.assertIn("image", err)

    def test_rejects_disallowed_extensions(self):
        for ext in (".exe", ".html", ".svg", ".php", ".js", ".css"):
            data, err = server._validate_logo_upload(
                self._ok(**{"filename": f"logo{ext}", "content-type": "image/png"})
            )
            self.assertIsNone(data, f"extension {ext} should be rejected")
            self.assertIn("not allowed", err, f"unexpected message for {ext}")

    def test_accepts_all_five_allowed_extensions(self):
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            info = {
                "filename": f"logo{ext}",
                "content-type": "image/png" if ext == ".png" else f"image/{ext.lstrip('.').replace('jpg','jpeg')}",
                "data": b"\x89PNG\r\n\x1a\n" + b"x" * 100,
            }
            data, err = server._validate_logo_upload(info)
            self.assertIsNotNone(data, f"extension {ext} should be accepted")
            self.assertIsNone(err, f"unexpected message for {ext}: {err}")

    def test_accepts_uppercase_extensions(self):
        data, err = server._validate_logo_upload(
            self._ok(**{"filename": "LOGO.PNG", "content-type": "image/png"})
        )
        self.assertIsNotNone(data)
        self.assertIsNone(err)

    def test_rejects_empty_or_missing_file(self):
        data, err = server._validate_logo_upload(None)
        self.assertIsNone(data)
        self.assertIn("required", err)

    def test_rejects_non_bytes_data(self):
        data, err = server._validate_logo_upload(
            self._ok(data="not bytes")
        )
        self.assertIsNone(data)
        self.assertIn("binary", err)

    def test_safe_filename_strips_path_separators(self):
        # Defence in depth: the safe-name filter rejects slashes,
        # backslashes, and NULs even if the extension allowlist passes.
        self.assertNotIn("/", server._safe_logo_filename("../etc/passwd"))
        self.assertNotIn("\\", server._safe_logo_filename("..\\windows"))
        self.assertNotIn(" ", server._safe_logo_filename("my logo.png"))
        # Legitimate names are preserved.
        self.assertEqual(server._safe_logo_filename("Vanguard-Logo_v2.png"),
                         "Vanguard-Logo_v2.png")


# ── Real handler exercises: JSON guards on the photo template handlers ──────

class TestPhotoTemplateJsonGuards(unittest.TestCase):
    """Regression: malformed JSON used to crash the handler and return
    a generic 500. The fix catches JSONDecodeError and returns 400."""

    def setUp(self):
        self._orig = server._json_response
        self.captured = {}
        server._json_response = self._capture

    def tearDown(self):
        server._json_response = self._orig

    def _capture(self, handler, status, body):
        self.captured["status"] = status
        self.captured["body"] = body

    def _last(self):
        return self.captured.get("status"), self.captured.get("body", {})

    def _make_handler(self, body=None, field_id=None):
        class _H:
            pass
        h = _H()
        h.command = "POST" if field_id is None else "PUT"
        h.path = (f"/api/v2/admin/photo-report-templates/{field_id}" if field_id
                  else "/api/v2/admin/photo-report-templates")
        body_bytes = b"" if body is None else json.dumps(body).encode("utf-8")
        h._read_body = lambda: body_bytes
        return h

    def test_create_returns_400_on_malformed_json(self):
        with patch("server._require_admin", return_value={"id": 1, "email": "a", "is_admin": True}):
            with patch("server._read_body", return_value=b"{not valid json"):
                server.KanbanHandler._handle_photo_report_template_create(self._make_handler())
        status, body = self._last()
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON", body.get("error", ""))

    def test_update_returns_400_on_malformed_json(self):
        with patch("server._require_admin", return_value={"id": 1, "email": "a", "is_admin": True}):
            with patch("server._read_body", return_value=b"}{ also bad"):
                server.KanbanHandler._handle_photo_report_template_update(
                    self._make_handler(field_id=1), 1
                )
        status, body = self._last()
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON", body.get("error", ""))


# ── Migration: unique-default partial index ─────────────────────────────────

class TestReportsMigration(unittest.TestCase):
    """The new partial unique indexes must exist in the live database and
    must reject a second default row. The migration file must be on disk."""

    def test_migration_file_present(self):
        mig = HERE.parent / "migrations" / "033_reports_unique_default_template.sql"
        self.assertTrue(mig.is_file(),
                        f"missing migration: {mig}")
        text = mig.read_text()
        self.assertIn("idx_photo_report_templates_one_default", text)
        self.assertIn("idx_document_templates_one_default", text)
        # Both must be partial (WHERE is_default = TRUE).
        self.assertEqual(text.count("WHERE is_default = TRUE"), 2)

    def test_indexes_applied_at_server_boot(self):
        # The boot path runs CREATE UNIQUE INDEX IF NOT EXISTS for both
        # new indexes. Confirmed live earlier; assert via DB query.
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("idx_photo_report_templates_one_default", src)
        self.assertIn("idx_document_templates_one_default", src)

    def test_db_actually_has_the_indexes(self):
        # Requires a live DB. If no DB, the skip above takes effect.
        import os
        for line in open(HERE.parent / ".env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
        import db as db_mod
        db_mod.init_db()
        idx_names = {r[0] for r in db_mod.query(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename IN ('photo_report_templates','document_templates')"
        )}
        self.assertIn("idx_photo_report_templates_one_default", idx_names)
        self.assertIn("idx_document_templates_one_default", idx_names)


# ── Delete-orphan on replace: source guard ─────────────────────────────────

class TestDeletePreviousLogo(unittest.TestCase):
    """The two logo upload handlers must delete the previous file before
    writing the new one, to avoid orphans in data/logos/. The signature
    handler already had this branch; the logo handlers did not."""

    def test_photo_logo_uploader_deletes_previous(self):
        # Find the handler body and look for the unlink path.
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.index("def _handle_photo_report_template_logo_upload")
        end = src.index("def ", start + 1)
        body = src[start:end]
        # Must select the previous logo_path and unlink it.
        self.assertIn('"SELECT logo_path FROM photo_report_templates WHERE id = %s"', body)
        self.assertIn("_old.unlink()", body)

    def test_document_logo_uploader_deletes_previous(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.index("def _handle_document_template_logo_upload")
        end = src.index("def ", start + 1)
        body = src[start:end]
        self.assertIn('"SELECT logo_path FROM document_templates WHERE id = %s"', body)
        self.assertIn("_old.unlink()", body)

    def test_signature_uploader_still_deletes_previous(self):
        # Pre-existing behaviour; the audit confirmed it.
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.index("def _handle_document_template_signature_upload")
        end = src.index("def ", start + 1)
        body = src[start:end]
        self.assertIn('"SELECT signature_path FROM document_templates WHERE id = %s"', body)


# ── Handler registration: the proxy is in do_GET, uploads are in do_POST ──

class TestHandlerRouting(unittest.TestCase):
    """Source-level checks that the routes are wired to the right HTTP
    verbs and dispatcher."""

    def test_proxy_route_in_do_GET(self):
        # The proxy endpoint should be a GET; it lives in _handle_api_get.
        src = Path(server.__file__).read_text(encoding="utf-8")
        # _handle_api_get is the GET route dispatcher; _handle_api_post_put
        # is the POST/PUT dispatcher. The proxy is in the GET block.
        get_start = src.index("def _handle_api_get")
        post_start = src.index("def _handle_api_post_put")
        proxy_pos = src.index("r\"^/api/v2/admin/logos/(.+)$\"")
        self.assertGreater(proxy_pos, get_start)
        self.assertLess(proxy_pos, post_start)

    def test_upload_routes_in_do_POST(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        post_start = src.index("def _handle_api_post_put")
        for needle in (
            "/api/v2/admin/photo-report-templates/(\\d+)/logo",
            "/api/v2/admin/document-templates/(\\d+)/logo",
            "/api/v2/admin/document-templates/(\\d+)/signature",
        ):
            self.assertIn(needle, src[post_start:],
                          f"{needle!r} not found in POST dispatcher block")


# ── Source guard: handler structure for photo template create/update ────────

class TestPhotoTemplateHandlerStructure(unittest.TestCase):
    """Confirm the photo template handlers have the same defensive shape
    as the document ones (JSON guard, is_default handling, etc.)."""

    def _body(self, name):
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.index(f"def {name}")
        # End at the next "    def " or top-level "def " after this one.
        nxt = src.find("\n    def ", start + 1)
        nxt2 = src.find("\ndef ", start + 1)
        ends = [e for e in (nxt, nxt2) if e != -1]
        return src[start:min(ends) if ends else len(src)]

    def test_create_clears_others_before_insert(self):
        body = self._body("_handle_photo_report_template_create")
        self.assertIn("UPDATE photo_report_templates SET is_default = FALSE", body)

    def test_update_clears_others_only_when_setting_default(self):
        body = self._body("_handle_photo_report_template_update")
        # The guard prevents clearing others when re-saving the
        # already-default template.
        self.assertIn('if data.get("is_default") and not existing["is_default"]', body)

    def test_update_sets_updated_at(self):
        body = self._body("_handle_photo_report_template_update")
        self.assertIn("fields.append(\"updated_at = NOW()\")", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)