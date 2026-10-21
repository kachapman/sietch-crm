#!/usr/bin/env python3
"""Unit tests for server-side note normalization + render entity decode.

Live case: event 49535 stored byte-exact
"From Christian:&nbsp;Please update to current day pricing. Retail estimate"
(raw contenteditable HTML posted by project.html, no tags) and rendered the
entity literally, because tagless content goes to textContent verbatim.

Coverage:
  - _normalize_note_content: raw entities converge; correct input
    round-trips byte-identical (idempotent); rich HTML untouched.
  - _handle_project_history_create stores the normalized form (mocked db).
  - Render sites decode before display (source guards; DOM-dependent).

Run with: python3 tests/test_note_normalize.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}
NBSP = "\u00a0"


class TestNormalizeNoteContent(unittest.TestCase):
    def test_raw_nbsp_becomes_char(self):
        out = _server._normalize_note_content("From Christian:&nbsp;Please")
        self.assertNotIn("&nbsp;", out)
        self.assertIn(f"Christian:{NBSP}Please", out)

    def test_correct_escaping_round_trips(self):
        for s in ["plain text", "a &amp; b", "1 &lt; 2", "100%"]:
            self.assertEqual(_server._normalize_note_content(s), s)

    def test_raw_ampersand_escaped_once(self):
        out = _server._normalize_note_content("fish & chips")
        self.assertEqual(out, "fish &amp; chips")
        # ... and stays stable if normalized again (no double-escaping).
        self.assertEqual(_server._normalize_note_content(out), out)

    def test_rich_html_untouched(self):
        html = 'Hi <b>there</b>&nbsp;<img src="x">'
        self.assertEqual(_server._normalize_note_content(html), html)

    def test_blank_stays_blank(self):
        self.assertEqual(_server._normalize_note_content(""), "")
        self.assertEqual(_server._normalize_note_content("   "), "")


class _Shim:
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


def _call_create(opp_id, body_dict, user=ADMIN):
    import json as _json
    responses = []
    inserts = []

    def fake_returning(sql, params=None, *a, **k):
        inserts.append((sql, params))
        return 7

    mock_db = MagicMock()
    mock_db.insert_returning.side_effect = fake_returning
    with patch.object(_server, "_require_auth", lambda h: user), \
        patch.object(_server, "_read_body", lambda h: _json.dumps(body_dict).encode()), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_project_history_create(_Shim(), opp_id)
    return responses, inserts


class TestHistoryCreateNormalizes(unittest.TestCase):
    def test_raw_entity_content_stored_decoded(self):
        responses, inserts = _call_create(
            1240, {"content": "From Christian:&nbsp;Please update", "categoryId": 8})
        self.assertEqual(responses[0][0], 201)
        ev = [i for i in inserts if "INSERT INTO history_events" in i[0]]
        self.assertEqual(len(ev), 1)
        _sql, params = ev[0]
        self.assertNotIn("&nbsp;", params[2])
        self.assertIn(f"Christian:{NBSP}Please", params[2])

    def test_plain_text_stored_singly_escaped(self):
        responses, inserts = _call_create(
            1240, {"content": "fish & chips", "categoryId": 8})
        self.assertEqual(responses[0][0], 201)
        _sql, params = [i for i in inserts if "INSERT INTO history_events" in i[0]][0]
        self.assertIn("fish &amp; chips", params[2])
        self.assertNotIn("&amp;amp;", params[2])


class TestRenderDecodeGuards(unittest.TestCase):
    def _app(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "public" / "app.js").read_text(encoding="utf-8")

    def test_preview_render_decodes_plain_notes(self):
        js = self._app()
        at = js.index("function renderHistoryNoteBody")
        region = js[at:at + 2500]
        self.assertIn("container.textContent = decodeHtmlEntities(raw)", region)

    def test_contact_renders_decode_before_escape(self):
        js = self._app()
        self.assertIn("escapeHtml(decodeHtmlEntities(content))", js)


if __name__ == "__main__":
    unittest.main(verbosity=1)
