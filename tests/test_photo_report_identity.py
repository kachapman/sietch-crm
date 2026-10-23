#!/usr/bin/env python3
"""Unit tests for photo-report header identity (2026-10-23).

Reports printed the literal word "Company": the no-template fallback
hardcoded company_name "Company" + text_logo_lines ["Company"], and an
unconfigured template text logo fell through to the same default.

Fix: template values win, branding company is the fallback, and nothing
ever emits a hardcoded placeholder.

Run with: python3 tests/test_photo_report_identity.py (no Postgres)
"""
import sys
import unittest

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server


class TestHeaderIdentity(unittest.TestCase):
    def test_no_template_uses_branding(self):
        lines, name = _server._photo_report_header_identity(None, "MDM Construction")
        self.assertEqual(name, "MDM Construction")
        self.assertEqual(lines, ["MDM Construction"])

    def test_no_template_no_brand_is_blank_never_company(self):
        lines, name = _server._photo_report_header_identity(None, "")
        self.assertEqual(name, "")
        self.assertEqual(lines, [])
        self.assertNotIn("Company", name)
        self.assertNotIn("Company", lines)

    def test_template_values_win(self):
        t = {"company_name": "A+ Restoration", "text_logo_lines": '["A+", "Roofing"]'}
        lines, name = _server._photo_report_header_identity(t, "MDM Construction")
        self.assertEqual(name, "A+ Restoration")
        self.assertEqual(lines, ["A+", "Roofing"])

    def test_unconfigured_template_falls_back_to_brand(self):
        t = {"company_name": "", "text_logo_lines": None}
        lines, name = _server._photo_report_header_identity(t, "MDM Construction")
        self.assertEqual(name, "MDM Construction")
        self.assertEqual(lines, ["MDM Construction"])

    def test_garbage_logo_json_is_blank(self):
        t = {"company_name": "", "text_logo_lines": "{broken"}
        lines, name = _server._photo_report_header_identity(t, "")
        self.assertEqual(lines, [])
        self.assertEqual(name, "")

    def test_no_company_literal_in_handler(self):
        src = open("/home/chapman-server/Projects/new-crm/server.py", encoding="utf-8").read()
        at = src.index("def _handle_project_photo_report")
        region = src[at:at + 6000]
        self.assertNotIn('"Company"', region)
        self.assertNotIn("['Company']", region)
        self.assertNotIn('["Company"]', region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
