#!/usr/bin/env python3
"""Regression tests for the 2026-11-02 live bot-note failures (NOT the dupe
branch — these bugs are on deployed main).

Symptom 1 (9:21 AM ET, "Failed to add note"): every POST /api/bot/note got
a 404 because _handle_bot_api_post called self.send_error(404) BEFORE the
route table — the note handler below it was unreachable dead code that
STILL executed server-side (dashboard log shows "POST /api/bot/note" 404
immediately followed by 200 for the same request: ghost-written notes the
user was told had failed). The same stray 404 broke every bot POST below
it (heartbeat failure #5640, config-check, task-*, stage, ...).

Symptom 2 ("Who should be notified?" taps do nothing): the notify_toggle
handler parsed int(data[13:]) but the prefix "notify_toggle:" is 14 chars,
so every tap parsed ":<uid>", raised ValueError, and silently returned —
the picker stayed at "0 selected" forever.

Run with: python3 tests/test_bot_note_flow.py
(DB-backed route tests SKIP when server is not importable, like
tests/test_users.py; parser + source-guard tests always run.)
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Stub the telegram imports so telegram_bot imports on machines without
# python-telegram-bot (same pattern as tests/test_telegram_bot.py).
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

import telegram_bot  # noqa: E402

try:
    import server as _server  # noqa: E402
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    _server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class TestParseNotifyToggle(unittest.TestCase):
    def test_valid_uid(self):
        self.assertEqual(telegram_bot._parse_notify_toggle("notify_toggle:7"), 7)

    def test_large_uid(self):
        self.assertEqual(telegram_bot._parse_notify_toggle("notify_toggle:1381714204"), 1381714204)

    def test_empty_uid_rejected(self):
        self.assertIsNone(telegram_bot._parse_notify_toggle("notify_toggle:"))

    def test_non_numeric_rejected(self):
        self.assertIsNone(telegram_bot._parse_notify_toggle("notify_toggle:abc"))

    def test_wrong_prefix_rejected(self):
        self.assertIsNone(telegram_bot._parse_notify_toggle("cat:5"))
        self.assertIsNone(telegram_bot._parse_notify_toggle(""))
        self.assertIsNone(telegram_bot._parse_notify_toggle(None))


class TestToggleSliceGuard(unittest.TestCase):
    """The 13-vs-14 off-by-one must never come back."""

    def test_no_hardcoded_13_slice(self):
        src = Path(HERE.parent, "telegram_bot.py").read_text(encoding="utf-8")
        self.assertNotIn("data[13:]", src)

    def test_handler_uses_parser(self):
        src = Path(HERE.parent, "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("_parse_notify_toggle(data)", src)


class TestBotRouteOrderGuard(unittest.TestCase):
    """send_error(404) must be the LAST statement of _handle_bot_api_post —
    any route below a 404 answers 404 first and ghost-executes."""

    def _func_src(self):
        src = Path(HERE.parent, "server.py").read_text(encoding="utf-8")
        start = src.index("def _handle_bot_api_post")
        # Next method at class indent ends the function.
        end = src.index("\n    def _handle_presence_users")
        return src[start:end]

    def test_404_is_last(self):
        func = self._func_src()
        last_route = max(func.rindex(f'api_path == "{p}"')
                         for p in ("/api/bot/note", "/api/bot/heartbeat",
                                   "/api/bot/config-check", "/api/bot/activity"))
        last_404 = func.rindex("self.send_error(404)")
        first_404 = func.index("self.send_error(404)")
        self.assertEqual(first_404, last_404, "more than one send_error(404) in _handle_bot_api_post")
        self.assertGreater(last_404, last_route)


if _server is None:
    print(f"SKIP: server-backed route tests need a database ({_IMPORT_ERROR})")
else:
    print("server-backed route tests active")


class _Shim:
    """Minimal handler stand-in following tests/test_reply_tagged_telegram.py."""

    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}
        self.codes = []

    def send_response(self, code):
        self.codes.append(code)

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def send_error(self, code, message=None):
        self.codes.append(code)

    wfile = MagicMock()


@unittest.skipIf(_server is None, "needs importable server (live DB)")
class TestBotNoteRoute(unittest.TestCase):
    def _call(self, path, body_dict, token="sekret"):
        import server
        responses = []
        mock_db = MagicMock()
        mock_db.insert_returning.return_value = 99
        h = _Shim(path, headers={"Authorization": f"Bearer {token}"})
        with patch.object(server, "_get_bot_token", lambda: "sekret"), \
            patch.object(server, "_read_body", lambda hh: json.dumps(body_dict).encode()), \
            patch.object(server, "_json_response", lambda hh, s, b: responses.append((s, b))), \
            patch.object(server, "db", mock_db), \
            patch.object(server, "get_mapping_by_chat", return_value={"userId": 5}), \
            patch.object(server, "logger"), \
            patch.object(server, "event_bus"):
            server.KanbanHandler._handle_bot_api_post(h, "POST")
        assert responses, "no response recorded"
        return responses[0], mock_db, h

    def test_note_returns_200_not_404(self):
        (status, body), _mock_db, shim = self._call("/api/bot/note", {
            "opportunityId": 130, "content": "hello", "chatId": 111, "categoryId": 10,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertNotIn(404, shim.codes)

    def test_note_with_notify_list_uses_dedupe_key(self):
        (_status, _body), mock_db, _shim = self._call("/api/bot/note", {
            "opportunityId": 130, "content": "hi <b>x</b>", "chatId": 111,
            "categoryId": 10, "notifyUserList": [7, 7, "bad"],
        })
        executed = [c.args[0] for c in mock_db.execute.call_args_list]
        self.assertTrue(any("dedupe_key" in sql and "ON CONFLICT (dedupe_key) DO NOTHING" in sql
                            for sql in executed),
                        "notification INSERT must carry the 042 dedupe key")
        # Invalid entries dropped, dupes collapsed: exactly one notify insert.
        note_inserts = [sql for sql in executed if "history_notify_users" in sql]
        self.assertEqual(len(note_inserts), 1)

    def test_heartbeat_returns_200_not_404(self):
        (status, body), _mock_db, shim = self._call("/api/bot/heartbeat", {})
        self.assertEqual(status, 200)
        self.assertNotIn(404, shim.codes)


if __name__ == "__main__":
    unittest.main(verbosity=1)
