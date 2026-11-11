#!/usr/bin/env python3
"""Regression: Junk-family folders are polled, per account (2026-11-11).

THE BUG (live)
--------------
crm@ (account 2) monitored `INBOX,Sent,Drafts,Spam,Trash`. Bluehost had filed
the CRM's own self-sends (UIDs 112-114) in **Junk**, which was never polled, so
those messages could never be imported — even with a valid Date. "Spam" was
listed and is a DIFFERENT folder on Bluehost (it was empty), so simply
replacing one with the other would not have fixed it.

THE RULES PINNED HERE
---------------------
1. The folders actually polled for an account are:
       configured monitored_folders  ∪  that mailbox's own Junk-family folders
   A Junk folder is therefore picked up even when `monitored_folders` omits it.
2. "Spam" is not a Junk-family name: it stays polled only when configured,
   and adding Junk does not remove it.
3. Discovery is scoped per account: the listing comes from the SAME account's
   own gated login, and the poll config (account_id, credentials) is unchanged.
4. No duplication: a Junk folder that is already configured is not added twice.

NO NETWORK / NO DB: every IMAP call is faked and `db` is a mock.

Run with: python3 tests/test_mail_junk_folders.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import network_guard  # noqa: E402

network_guard.install()

import scanner.mail_scanner as ms  # noqa: E402


def _cfg(**kw):
    base = {"host": "imap.vanguardadj.online", "port": 993,
            "user": "crm@vanguardadj.online", "email": "crm@vanguardadj.online",
            "password": "not-a-real-password", "inbox": "INBOX",
            "folders": ["INBOX", "Sent", "Drafts", "Spam", "Trash"],
            "account_id": 2, "auth_type": "password", "retention_days": 90}
    base.update(kw)
    return base


class TestJunkNameMatch(unittest.TestCase):
    def test_common_junk_names(self):
        for name in ("Junk", "junk", "Junk E-mail", "Junk Email", "Junk Mail",
                     "Bulk", "Bulk Mail"):
            self.assertTrue(ms._is_junk_folder(name), name)

    def test_nested_junk_names(self):
        self.assertTrue(ms._is_junk_folder("INBOX/Junk"))
        self.assertTrue(ms._is_junk_folder("INBOX.Junk E-mail"))

    def test_imap_junk_flag(self):
        self.assertTrue(ms._is_junk_folder("SomethingElse", ["\\Junk", "\\HasNoChildren"]))

    def test_spam_and_normal_folders_are_not_junk_family(self):
        for name in ("Spam", "INBOX", "Sent", "Drafts", "Trash", "Archive",
                     "Junk Drawer R Us", ""):
            self.assertFalse(ms._is_junk_folder(name), name)

    def test_sent_folder_helper_is_unchanged(self):
        self.assertTrue(ms._is_sent_folder("Sent Items"))
        self.assertFalse(ms._is_sent_folder("Junk"))


class TestPollFolderUnion(unittest.TestCase):
    LISTING = [
        {"name": "INBOX", "flags": []},
        {"name": "Drafts", "flags": ["\\Drafts"]},
        {"name": "Sent", "flags": ["\\Sent"]},
        {"name": "Junk", "flags": ["\\Junk"]},
        {"name": "Spam", "flags": []},
        {"name": "Trash", "flags": ["\\Trash"]},
    ]

    def test_junk_is_added_when_monitored_folders_omit_it(self):
        folders = ms._poll_folder_names(_cfg(), self.LISTING)
        self.assertIn("Junk", folders)
        self.assertIn("Spam", folders, "a configured Spam must still be polled")
        self.assertIn("INBOX", folders)

    def test_configured_folders_are_preserved_in_order(self):
        cfg = _cfg()
        folders = ms._poll_folder_names(cfg, self.LISTING)
        self.assertEqual(folders[:len(cfg["folders"])], cfg["folders"])

    def test_junk_is_not_duplicated_when_already_configured(self):
        cfg = _cfg(folders=["INBOX", "Junk", "Spam"])
        folders = ms._poll_folder_names(cfg, self.LISTING)
        self.assertEqual(folders.count("Junk"), 1)
        self.assertEqual(folders.count("Spam"), 1)

    def test_junk_e_mail_name_is_used_verbatim(self):
        listing = [{"name": "Junk E-mail", "flags": []}]
        folders = ms._poll_folder_names(_cfg(folders=["INBOX"]), listing)
        self.assertIn("Junk E-mail", folders)

    def test_mailbox_without_junk_is_unchanged(self):
        listing = [{"name": "INBOX", "flags": []}, {"name": "Sent", "flags": ["\\Sent"]}]
        cfg = _cfg(folders=["INBOX", "Sent"])
        self.assertEqual(ms._poll_folder_names(cfg, listing), ["INBOX", "Sent"])

    def test_no_listing_falls_back_to_configured_folders(self):
        cfg = _cfg()
        self.assertEqual(ms._poll_folder_names(cfg, None), cfg["folders"])


class TestPollUsesJunkPerAccount(unittest.TestCase):
    """The poll loop must poll Junk for the account it belongs to."""

    def _poll(self, cfg, listing):
        polled: list[tuple[int | None, str]] = []
        processed: list[tuple[int | None, str]] = []

        def fake_fetch(cfg_arg, folder, since=None, uid_min=None):
            polled.append((cfg_arg.get("account_id"), folder))
            uid = 112 if ms._is_junk_folder(folder) else 12397
            yield {"uid": str(uid), "folder": folder, "subject": "s", "from": "f",
                   "date": "2026-11-11T00:00:00Z", "attachments": []}

        def fake_process(msg, cfg_arg):
            processed.append((cfg_arg.get("account_id"), msg["folder"]))
            return {"status": "processed", "uid": msg["uid"]}

        mock_db = MagicMock()
        mock_db.query_one.return_value = None
        mock_db.execute.return_value = None
        with patch.object(ms, "db", mock_db), \
            patch.object(ms, "_list_imap_folders", return_value=listing), \
            patch.object(ms, "_sync_imap_folders", return_value=0), \
            patch.object(ms, "_get_folder_watermark", return_value=(None, None)), \
            patch.object(ms, "_read_folder_status", return_value=(None, None)), \
            patch.object(ms, "_fetch_messages", side_effect=fake_fetch), \
            patch.object(ms, "_process_message", side_effect=fake_process), \
            patch.object(ms, "_save_folder_watermark"), \
            patch.object(ms, "logger"):
            ms._poll_mailbox_cfg(cfg)
        return polled, processed

    def test_junk_folder_is_polled(self):
        listing = [{"name": "INBOX", "flags": []}, {"name": "Junk", "flags": ["\\Junk"]}]
        polled, _processed = self._poll(_cfg(folders=["INBOX", "Spam"]), listing)
        self.assertIn("Junk", [folder for _acct, folder in polled])

    def test_junk_messages_are_processed_under_the_same_account(self):
        listing = [{"name": "INBOX", "flags": []}, {"name": "Junk", "flags": ["\\Junk"]}]
        _polled, processed = self._poll(_cfg(folders=["INBOX"]), listing)
        junk = [acct for acct, folder in processed if folder == "Junk"]
        self.assertEqual(junk, [2], "Junk mail must stay under the polled account")

    def test_account_without_junk_does_not_gain_one(self):
        listing = [{"name": "INBOX", "flags": []}]
        polled, _processed = self._poll(_cfg(folders=["INBOX"]), listing)
        self.assertEqual([folder for _acct, folder in polled], ["INBOX"])

    def test_each_account_only_gains_its_own_junk(self):
        listings = {
            2: [{"name": "INBOX", "flags": []}, {"name": "Junk", "flags": ["\\Junk"]}],
            4: [{"name": "INBOX", "flags": []}],
        }
        cfg2 = _cfg(account_id=2, folders=["INBOX"])
        cfg4 = _cfg(account_id=4, user="ken@example.invalid",
                    email="ken@example.invalid", folders=["INBOX"])
        polled2, _ = self._poll(cfg2, listings[2])
        polled4, _ = self._poll(cfg4, listings[4])
        self.assertIn((2, "Junk"), polled2)
        self.assertNotIn((4, "Junk"), polled4)
        self.assertTrue(all(acct == 2 for acct, _f in polled2))
        self.assertTrue(all(acct == 4 for acct, _f in polled4))

    def test_folder_listing_is_requested_once_per_poll(self):
        listing = [{"name": "INBOX", "flags": []}]
        mock_db = MagicMock()
        mock_db.query_one.return_value = None
        with patch.object(ms, "db", mock_db), \
            patch.object(ms, "_list_imap_folders", return_value=listing) as list_folders, \
            patch.object(ms, "_sync_imap_folders", return_value=0) as sync_folders, \
            patch.object(ms, "_get_folder_watermark", return_value=(None, None)), \
            patch.object(ms, "_fetch_messages", side_effect=lambda *a, **k: iter(())), \
            patch.object(ms, "_save_folder_watermark"), \
            patch.object(ms, "logger"):
            ms._poll_mailbox_cfg(_cfg(folders=["INBOX"]))
        self.assertEqual(list_folders.call_count, 1,
                         "one LIST per poll feeds both the mirror and Junk discovery")
        self.assertEqual(sync_folders.call_count, 1)
        # The listing is passed through instead of being fetched a second time.
        self.assertEqual(sync_folders.call_args.args[1], listing)


class TestNoStolenMailbox(unittest.TestCase):
    def test_poll_folder_names_never_touches_credentials_or_identity(self):
        cfg = _cfg()
        before = dict(cfg)
        ms._poll_folder_names(cfg, [{"name": "Junk", "flags": ["\\Junk"]}])
        self.assertEqual(cfg, before, "folder discovery must not mutate the poll config")

    def test_monitored_folders_row_is_not_rewritten(self):
        src = (ROOT / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")
        self.assertIn("def _poll_folder_names", src)
        # The union is computed at poll time; the admin's list stays authoritative.
        self.assertNotIn("UPDATE mail_accounts SET monitored_folders", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
