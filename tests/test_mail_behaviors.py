#!/usr/bin/env python3
"""Unit tests for mail scanner behavior gating + search wiring + NUL safety.

Live cases behind these tests:
  - Claim-code auto-linking fired with only "[#PROJECTID]" checked: the
    claim sub-pipeline had no toggle of its own (and two fallthroughs had no
    gate at all). Now auto_link_claim_code governs every claim path and each
    checkbox licenses exactly its own matches (link_method).
  - Inbox search typed but never applied: the input handler reloaded without
    storing mailState.search.
  - crm@ messages with NUL bytes died on INSERT every cycle: all stored
    strings are now sanitized.
  - Passwordless/hostless accounts skipped silently: loader warns per skip.

Run with: python3 tests/test_mail_behaviors.py
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import scanner.mail_scanner as ms  # noqa: E402


def _mock_db():
    mock = MagicMock()
    mock.query_dicts.return_value = []
    mock.query_one.return_value = None
    mock.execute.return_value = None
    return mock


def _behaviors(claim_on=False, claim_dry=False, project_on=True):
    return {"scanner_behavior": {
        "auto_link_project_id": {"enabled": project_on, "dry_run": False, "accounts": "all"},
        "auto_link_claim_code": {"enabled": claim_on, "dry_run": claim_dry, "accounts": "all"},
        "auto_link_by_content": {"enabled": False, "dry_run": True, "accounts": "all"},
    }}


def _classify(subject, behaviors, claim_deal=None, project_deal=None):
    msg = {"subject": subject, "body_text": "", "body_html": "",
           "from": "a@b.c", "from_name": "", "uid": "1"}
    with patch.object(ms, "db", _mock_db()), \
        patch.object(ms, "get_contractors", lambda: behaviors), \
        patch.object(ms, "_find_opportunity_by_claim_code",
                     lambda code: claim_deal), \
        patch.object(ms, "_find_opportunity_by_deal_id",
                     lambda pid: project_deal):
        return ms._classify_message(msg)


class TestClaimToggleGating(unittest.TestCase):
    def test_claim_subject_links_when_claim_toggle_on(self):
        out = _classify("0825902562", _behaviors(claim_on=True),
                        claim_deal={"id": 42})
        self.assertEqual(out["classification"], "claim_code_deal_link")
        self.assertEqual(out["linked_deal_id"], 42)
        self.assertEqual(out["link_method"], "claim_code")
        self.assertEqual(out["action"], "link_deal")

    def test_claim_subject_silent_when_claim_toggle_off(self):
        # Project-ID box checked, claim box off: no link of any kind.
        out = _classify("0825902562", _behaviors(claim_on=False),
                        claim_deal={"id": 42})
        self.assertIsNone(out["linked_deal_id"])
        self.assertNotEqual(out["action"], "link_deal")
        self.assertNotEqual(out.get("link_method"), "claim_code")

    def test_project_id_link_unaffected_by_claim_toggle(self):
        out = _classify("Re: invoice [#123]", _behaviors(claim_on=False),
                        project_deal={"id": 123})
        self.assertEqual(out["linked_deal_id"], 123)
        self.assertEqual(out["link_method"], "project_id")

    def test_deal_dash_format_behind_claim_toggle(self):
        out = _classify("[#DEAL-4242]", _behaviors(claim_on=True),
                        claim_deal={"id": 9})
        self.assertEqual(out["linked_deal_id"], 9)
        out2 = _classify("[#DEAL-4242]", _behaviors(claim_on=False),
                         claim_deal={"id": 9})
        self.assertIsNone(out2["linked_deal_id"])


class TestLinkToggleAllows(unittest.TestCase):
    def test_each_toggle_governs_own_method(self):
        claim_on = {"auto_link_claim_code": {"enabled": True, "dry_run": False, "accounts": "all"}}
        self.assertEqual(ms._link_toggle_allows({"link_method": "claim_code"}, claim_on, 6), (True, False))
        proj_on = {"auto_link_project_id": {"enabled": True, "dry_run": False, "accounts": "all"}}
        self.assertEqual(ms._link_toggle_allows({"link_method": "claim_code"}, proj_on, 6), (False, False))
        self.assertEqual(ms._link_toggle_allows({"link_method": "project_id"}, proj_on, 6), (True, False))

    def test_dry_run_reported_per_matching_toggle(self):
        toggles = {"auto_link_claim_code": {"enabled": True, "dry_run": True, "accounts": "all"}}
        self.assertEqual(ms._link_toggle_allows({"link_method": "claim_code"}, toggles, 6), (True, True))

    def test_missing_method_falls_back_to_project_toggle(self):
        toggles = {"auto_link_project_id": {"enabled": True, "dry_run": False, "accounts": "all"}}
        self.assertEqual(ms._link_toggle_allows({}, toggles, 6), (True, False))
        self.assertEqual(ms._link_toggle_allows(None, {}, 6), (False, False))


class TestNulSanitizing(unittest.TestCase):
    def test_store_strips_nul_bytes(self):
        mock_db = _mock_db()
        mock_db.query_one.side_effect = [None, {"id": 5}]
        msg = {"uid": "u1", "folder": "INBOX", "subject": "a\x00b",
               "from": "x@y\x00z", "to": "", "body_text": "hi\x00",
               "body_html": "", "date": None, "attachments": [],
               "raw_headers": "h\x00"}
        with patch.object(ms, "db", mock_db), \
            patch.object(ms, "_mark_processed", lambda *a, **k: None):
            msg_id = ms._store_message(msg, {"account_id": 1})
        self.assertEqual(msg_id, 5)
        insert = mock_db.query_one.call_args_list[1]
        _sql, params = insert.args
        for p in params:
            if isinstance(p, str):
                self.assertNotIn("\x00", p)

    def test_cleaner_passes_non_strings_through(self):
        self.assertIsNone(ms._clean_nul_str(None))
        self.assertEqual(ms._clean_nul_str(123), 123)


class TestSkipLogging(unittest.TestCase):
    def _mailboxes(self, rows):
        mock_db = _mock_db()
        mock_db.query_dicts.return_value = rows
        env = {"SCANNER_IMAP_HOST": "", "SCANNER_IMAP_USER": "",
               "SCANNER_IMAP_PASSWORD": "", "SCANNER_INBOX": "INBOX"}
        with patch.object(ms, "db", mock_db), \
            patch.dict(os.environ, env, clear=False):
            with self.assertLogs("sietch.mail_scanner", level="WARNING") as logs:
                boxes = ms._get_mailboxes()
        return boxes, "\n".join(logs.output)

    def test_passwordless_account_skipped_with_warning(self):
        rows = [
            {"id": 6, "email": "r@x.com", "imap_host": "h", "imap_port": 993,
             "password_encrypted": "", "oauth_provider": None,
             "sync_enabled": True, "monitored_folders": "INBOX"},
            {"id": 2, "email": "ok@x.com", "imap_host": "h", "imap_port": 993,
             "password_encrypted": "pw", "oauth_provider": None,
             "sync_enabled": True, "monitored_folders": "INBOX"},
        ]
        boxes, logs = self._mailboxes(rows)
        self.assertEqual([b["account_id"] for b in boxes], [2])
        self.assertIn("6", logs)
        self.assertIn("no password", logs.lower())

    def test_hostless_account_skipped_with_warning(self):
        rows = [{"id": 9, "email": "x@y.com", "imap_host": "",
                 "password_encrypted": "pw", "oauth_provider": None,
                 "sync_enabled": True, "monitored_folders": "INBOX"}]
        boxes, logs = self._mailboxes(rows)
        self.assertEqual(boxes, [])
        self.assertIn("9", logs)


class TestSearchAndClaimUiWiring(unittest.TestCase):
    def _read(self, name):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")

    def test_search_input_stored_before_reload(self):
        js = self._read("public/app.js")
        # The mail search handler is the only place storing searchIn's value.
        start = js.index("searchIn.value.trim()")
        region = js[max(0, start - 300):start + 300]
        self.assertIn("mailState.search =", region)
        self.assertIn("mailState.page = 1", region)
        self.assertIn("loadMailMessagesForModal", region)

    def test_search_reset_on_navigation(self):
        js = self._read("public/app.js")
        self.assertGreaterEqual(js.count("mailState.search ="), 3)

    def test_claim_toggle_ui_wired(self):
        html = self._read("public/index.html")
        self.assertIn('id="scanner-toggle-auto-link-claim"', html)
        js = self._read("public/app.js")
        self.assertIn("scanner-toggle-auto-link-claim", js)
        self.assertIn("auto_link_claim_code", js)


if __name__ == "__main__":
    unittest.main(verbosity=1)
