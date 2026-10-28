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
    def test_no_template_uses_branding_on_the_right_only(self):
        # 2026-10-23 decision: the LEFT wordmark renders ONLY what the
        # template stores; branding is the RIGHT-hand company fallback. Putting
        # branding on the left would print the workspace/vendor name on a
        # customer-facing report and duplicate the right side.
        lines, name = _server._photo_report_header_identity(None, "MDM Construction")
        self.assertEqual(name, "MDM Construction")
        self.assertEqual(lines, [])

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

    def test_unconfigured_template_keeps_left_blank(self):
        t = {"company_name": "", "text_logo_lines": None}
        lines, name = _server._photo_report_header_identity(t, "MDM Construction")
        self.assertEqual(name, "MDM Construction")
        self.assertEqual(lines, [])

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

    def test_class_body_intact(self):
        # Regression 2026-10-23: a column-0 helper inserted mid-file ended the
        # KanbanHandler body — every endpoint defined after it 500'd with
        # AttributeError (first seen: presence heartbeat). Pin the methods.
        import server as _s
        for name in ("_handle_presence_heartbeat", "_handle_presence_get",
                     "_handle_project_photo_report", "_handle_mail_send",
                     "_handle_sync_pull_deals", "_handle_mail_accounts",
                     "_handle_project_history_get", "_handle_project_update"):
            self.assertTrue(hasattr(_s.KanbanHandler, name), name)
        for name in ("_photo_report_header_identity", "_ts", "_json_default"):
            self.assertTrue(hasattr(_s, name), name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
