#!/usr/bin/env python3
"""Unit tests: no "Company" placeholder in photo-report write paths (2026-10-23).

The 2026-10-23 generator fix stopped the RENDERER from synthesizing the word,
but every WRITE path still injected it:
  - app.js editor default ["Company"]
  - app.js save payload  [line1.trim() || "Company", line2.trim()]
  - server.py create default ["Company"]
  - init.sql / migration 018 column DEFAULT '["Company"]'
so stored rows kept the literal and reports printed it.

Run with: python3 tests/test_photo_report_no_placeholder.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

ROOT = Path('/home/chapman-server/Projects/new-crm')


class TestNoPlaceholderWrites(unittest.TestCase):
    def test_editor_default_is_empty(self):
        src = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        at = src.index("function openAdminPREditor")
        region = src[at:at + 3000]
        self.assertNotIn('["Company"]', region)
        self.assertNotIn("'Company'", region)

    def test_save_payload_has_no_placeholder_fallback(self):
        src = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        at = src.index("text_logo_lines: [")
        line = src[at:at + 300].split("\n")[0]
        self.assertNotIn("Company", line)
        self.assertIn("filter(Boolean)", line)

    def test_server_create_default_is_empty(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        at = src.index("def _handle_photo_report_template_create")
        region = src[at:at + 4000]
        self.assertNotIn('"Company"', region)

    def test_schema_default_is_empty(self):
        init = (ROOT / "init.sql").read_text(encoding="utf-8")
        self.assertIn("text_logo_lines JSONB DEFAULT '[]'", init)

    def test_migration_changes_default_without_touching_data(self):
        mig = ROOT / "migrations" / "039_photo_report_logo_placeholder.sql"
        self.assertTrue(mig.exists(), "migration 039 missing")
        sql = mig.read_text(encoding="utf-8")
        self.assertIn("SET DEFAULT '[]'::jsonb", sql)
        # No data rewrite: the user deletes/recreates the templates.
        body = "\n".join(l for l in sql.splitlines()
                         if not l.strip().startswith("--"))
        self.assertNotIn("UPDATE", body.upper())
        self.assertNotIn("DELETE", body.upper())

    def test_generator_never_falls_back_to_branding_on_the_left(self):
        import server as _server
        lines, name = _server._photo_report_header_identity(
            {"company_name": "MDM Construction", "text_logo_lines": []}, "Vanguard Adjusting")
        self.assertEqual(lines, [], "left wordmark must stay empty (nothing was entered)")
        self.assertEqual(name, "MDM Construction", "right side keeps the template company")
        lines2, name2 = _server._photo_report_header_identity(None, "Vanguard Adjusting")
        self.assertEqual(lines2, [])
        self.assertEqual(name2, "Vanguard Adjusting")

    def test_generator_keeps_real_wordmark(self):
        import server as _server
        lines, name = _server._photo_report_header_identity(
            {"company_name": "MDM Construction",
             "text_logo_lines": ["Advancing Property Claims", "TAGLINE"]},
            "Vanguard Adjusting")
        self.assertEqual(lines, ["Advancing Property Claims", "TAGLINE"])
        self.assertEqual(name, "MDM Construction")

    def test_no_company_literal_in_template_write_handlers(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        for fn in ("_handle_photo_report_template_create",
                   "_handle_photo_report_template_update"):
            at = src.index(f"def {fn}")
            region = src[at:at + 6000]
            offenders = [ln.strip() for ln in region.splitlines()
                         if '"Company"' in ln or "'Company'" in ln]
            self.assertEqual(offenders, [], f"{fn}: {offenders[:3]}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
