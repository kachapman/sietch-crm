#!/usr/bin/env python3
"""Unit tests for Telegram tag-notification preview length (2026-10-22).

The preview was cut twice: 120 chars when the snippet was stored
(server.py x3 sites) and 500 chars at format time — while Telegram allows
4096/message. Raised to 1500 (constant NOTIFICATION_SNIPPET_LEN + dispatcher
default), keeping the composed message under half the Telegram limit.

Run with: python3 tests/test_notification_snippet.py (no Postgres; dispatcher imports cleanly)
"""
import re
import sys
import unittest
from html import unescape as _unescape

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server
import notification_dispatcher as _nd


def _make_snippet(html_note):
    # Mirrors server.py creation sites: strip tags, unescape, cap, trim.
    return _unescape(re.sub(r'<[^>]+>', '', html_note))[:_server.NOTIFICATION_SNIPPET_LEN].strip()


LONG_NOTE = "<p>" + "Draft update for A+: the adjuster increased the amount. " * 60 + "</p>"


class TestSnippetLength(unittest.TestCase):
    def test_constant_is_1500(self):
        self.assertEqual(_server.NOTIFICATION_SNIPPET_LEN, 1500)
        import inspect
        src = inspect.getsource(_nd._truncate)
        self.assertIn("1500", src)

    def test_long_note_keeps_much_more_than_before(self):
        snippet = _make_snippet(LONG_NOTE)
        self.assertGreater(len(snippet), 120)
        self.assertLessEqual(len(snippet), 1500)
        self.assertNotIn("<p>", snippet)

    def test_no_120_snippet_sites_remain(self):
        src = open("/home/chapman-server/Projects/new-crm/server.py", encoding="utf-8").read()
        for i, ln in enumerate(src.splitlines(), 1):
            if "snippet" in ln.lower() and "[:120]" in ln:
                self.fail(f"120-char snippet site still present at line {i}: {ln.strip()[:100]}")

    def test_format_keeps_full_snippet(self):
        snippet = _make_snippet(LONG_NOTE)
        notification = {"payload": {"snippet": snippet}}
        msg = _nd._format_note_tagged(notification, "John and Cari Baumann (CARUSO, CATHRYN)", "Claudiu Chereji", project_id=1, project_number=46)
        self.assertIn(snippet, msg)
        self.assertNotIn("...", msg.split("<blockquote>")[1].split("</blockquote>")[0][-20:])

    def test_composed_message_under_telegram_limit(self):
        snippet = _make_snippet(LONG_NOTE)
        notification = {"payload": {"snippet": snippet}}
        msg = _nd._format_note_tagged(notification, "X" * 200, "Y" * 60, project_id=1, project_number=46)
        self.assertLess(len(msg), 4096)
        self.assertLess(len(msg), 2500)


if __name__ == "__main__":
    unittest.main(verbosity=1)
