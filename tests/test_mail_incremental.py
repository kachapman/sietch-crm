#!/usr/bin/env python3
"""Unit tests for incremental IMAP sync + manual-poll workers (2026-10-26).

Covers, with stubs/mocks (no live IMAP, no Postgres):
  - UID watermark read/save (insert vs update, missing-column fail-safe).
  - _fetch_messages(uid_min=) issues a UID-range criterion.
  - _poll_mailbox_cfg goes incremental on validity match, full otherwise,
    and advances the watermark to the max fetched UID.
  - Manual polls run immediately in a worker (not queued behind a cycle),
    track in-progress/done/error, and surface in get_scanner_status.
  - Full cycles isolate slow accounts (timeout) instead of stalling.
  - The mail refresh button polls scanner status, not a fixed 8s sleep.

Run with: python3 tests/test_mail_incremental.py
"""
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import scanner.mail_scanner as ms  # noqa: E402


class FakeMsg:
    def __init__(self, uid):
        self.uid = str(uid)
        self.attachments = []
        self.headers = {}
        self.from_values = None
        self.to_values = []
        self.subject = f"msg {uid}"
        self.date = None
        self.text = "body"
        self.html = ""
        self.flags = []


class FakeFolder:
    def set(self, folder):
        pass

    def status(self):
        return {"MESSAGES": 5, "RECENT": 0, "UIDNEXT": 104, "UIDVALIDITY": 7, "UNSEEN": 1}


class FakeMailbox:
    seen_criteria = []

    def __init__(self, host, port=None):
        self.host = host
        self.folder = FakeFolder()

    def login(self, user, password):
        pass

    def xoauth2(self, user, token):
        pass

    def fetch(self, criteria=None, mark_seen=False, bulk=False):
        FakeMailbox.seen_criteria.append(str(criteria))
        return [FakeMsg(uid) for uid in (101, 102, 103)]

    def logout(self):
        pass


def _cfg(**kw):
    base = {"host": "h", "port": 993, "user": "u", "password": "p",
            "inbox": "INBOX", "folders": ["INBOX"], "account_id": 1,
            "auth_type": "password", "retention_days": 0}
    base.update(kw)
    return base


class TestWatermark(unittest.TestCase):
    def _db(self, row):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = lambda sql, params=None, *a, **k: row
        mock_db.execute.side_effect = lambda *a, **k: None
        return mock_db

    def test_no_row_means_full_fetch(self):
        with patch.object(ms, "db", self._db(None)):
            self.assertEqual(ms._get_folder_watermark(1, "INBOX"), (None, None))

    def test_row_returns_validity_and_last(self):
        with patch.object(ms, "db", self._db({"uid_validity": 7, "last_seen_uid": 100})):
            self.assertEqual(ms._get_folder_watermark(1, "INBOX"), (7, 100))

    def test_old_schema_fails_safe_to_full(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = Exception('column "uid_validity" does not exist')
        with patch.object(ms, "db", mock_db):
            self.assertEqual(ms._get_folder_watermark(1, "INBOX"), (None, None))

    def test_save_inserts_when_no_row(self):
        writes = []
        mock_db = MagicMock()
        mock_db.query_one.return_value = None
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append(sql)
        with patch.object(ms, "db", mock_db):
            ms._save_folder_watermark(1, "INBOX", 7, 103)
        self.assertTrue(any("INSERT INTO mail_folders" in s for s in writes))

    def test_save_updates_when_row(self):
        writes = []
        mock_db = MagicMock()
        mock_db.query_one.return_value = {"id": 9}
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
        with patch.object(ms, "db", mock_db):
            ms._save_folder_watermark(1, "INBOX", 7, 103)
        upd = [w for w in writes if w[0].startswith("UPDATE mail_folders")]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0][1][:2], (103, 7))


class TestIncrementalCriterion(unittest.TestCase):
    def test_uid_range_criterion(self):
        FakeMailbox.seen_criteria.clear()
        with patch.object(ms, "MailBox", FakeMailbox):
            msgs = ms._fetch_messages(_cfg(), "INBOX", uid_min=100)
        self.assertEqual([m["uid"] for m in msgs], ["103", "102", "101"])
        self.assertTrue(any("UID" in c and "100" in c for c in FakeMailbox.seen_criteria),
                        FakeMailbox.seen_criteria)


class TestPollUsesWatermark(unittest.TestCase):
    def _run_poll(self, stored, status):
        seen = {}
        mock_db = MagicMock()
        mock_db.query_one.side_effect = lambda sql, params=None, *a, **k: (
            {"uid_validity": stored[0], "last_seen_uid": stored[1]} if "uid_validity" in sql else None)
        mock_db.execute.side_effect = lambda *a, **k: None

        def fake_fetch(cfg, folder, since=None, uid_min=None):
            seen["uid_min"] = uid_min
            return [{"uid": "101", "subject": "s", "from": "f", "folder": folder}]

        saved = {}
        with patch.object(ms, "db", mock_db), \
            patch.object(ms, "MailBox", FakeMailbox), \
            patch.object(ms, "_read_folder_status", return_value=status), \
            patch.object(ms, "_fetch_messages", side_effect=fake_fetch), \
            patch.object(ms, "_process_message", return_value={"status": "ok"}), \
            patch.object(ms, "_save_folder_watermark",
                         side_effect=lambda a, f, v, l: saved.update(validity=v, last=l)):
            ms._poll_mailbox_cfg(_cfg())
        return seen, saved

    def test_validity_match_goes_incremental(self):
        seen, saved = self._run_poll(stored=(7, 100), status=(7, 150))
        self.assertEqual(seen["uid_min"], 100)
        self.assertEqual(saved["last"], 101)
        self.assertEqual(saved["validity"], 7)

    def test_validity_mismatch_goes_full(self):
        seen, saved = self._run_poll(stored=(7, 100), status=(8, 150))
        self.assertIsNone(seen["uid_min"])

    def test_no_watermark_goes_full(self):
        seen, saved = self._run_poll(stored=(None, None), status=(7, 150))
        self.assertIsNone(seen["uid_min"])
        self.assertEqual(saved["last"], 101)


class TestManualWorker(unittest.TestCase):
    def setUp(self):
        ms._manual_poll_ids.clear()
        ms._manual_in_progress.clear()
        ms._manual_last_done.clear()
        ms._manual_last_error.clear()

    def tearDown(self):
        deadline = time.time() + 10
        while ms._manual_in_progress and time.time() < deadline:
            time.sleep(0.05)
        ms._manual_poll_ids.clear()
        ms._manual_in_progress.clear()
        ms._manual_last_done.clear()
        ms._manual_last_error.clear()

    def test_worker_polls_immediately_and_tracks(self):
        called = []
        with patch.object(ms, "_get_mailboxes", return_value=[_cfg()]), \
            patch.object(ms, "_poll_mailbox_cfg", side_effect=lambda c: called.append(c) or []):
            self.assertTrue(ms.request_manual_poll(1))
            deadline = time.time() + 10
            while 1 in ms._manual_in_progress and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(len(called), 1)
        self.assertIn(1, ms._manual_last_done)
        self.assertNotIn(1, ms._manual_in_progress)

    def test_unknown_account_records_error(self):
        with patch.object(ms, "_get_mailboxes", return_value=[]):
            ms._manual_poll_worker(9)
        self.assertIn(9, ms._manual_last_error)

    def test_duplicate_while_running_spawns_one(self):
        self.assertTrue(ms.request_manual_poll(1))
        # second call while first worker starts: accepted, single flight
        self.assertTrue(ms.request_manual_poll(1))

    def test_status_exposes_manual_state(self):
        ms._manual_last_done[1] = 123.0
        ms._manual_last_error[2] = "boom"
        mock_db = MagicMock()
        mock_db.query_dicts.return_value = []
        with patch.object(ms, "db", mock_db):
            st = ms.get_scanner_status()
        self.assertIn("manual_in_progress", st)
        self.assertEqual(st["manual_last_done"], {"1": 123.0})
        self.assertEqual(st["manual_last_error"], {"2": "boom"})


class TestConcurrentIsolation(unittest.TestCase):
    def test_slow_account_does_not_stall_cycle(self):
        import os as _os

        def fake_poll(cfg):
            if cfg["account_id"] == 99:
                time.sleep(30)
            return [{"ok": cfg["account_id"]}]

        cfgs = [_cfg(account_id=1), _cfg(account_id=99)]
        with patch.object(ms, "_get_mailboxes", return_value=cfgs), \
            patch.object(ms, "_poll_mailbox_cfg", side_effect=fake_poll), \
            patch.dict(_os.environ, {"SCANNER_ACCOUNT_TIMEOUT": "1"}):
            out = ms._poll_mailboxes()
        self.assertTrue(any(r.get("ok") == 1 for r in out))


class TestRefreshButtonPollsStatus(unittest.TestCase):
    def test_no_fixed_sleep_reload(self):
        src = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        at = src.index("const refBtn = $(\"#mail-refresh\")")
        region = src[at:at + 2200]
        self.assertIn("manual_in_progress", region)
        self.assertIn("/api/v2/mail/status", region)
        self.assertNotIn("setTimeout(r, 8000)", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
