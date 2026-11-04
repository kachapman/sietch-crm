#!/usr/bin/env python3
"""Unit tests for the mail retention re-import loop fix (2026-11-04).

Covers, with stubs/mocks (no live IMAP, no Postgres):
  - The IMAP `n:*` rule: a folder whose HIGHEST UID is below `n` still
    returns that UID — it must be dropped client-side (regression).
  - The incremental range starts at `last_seen + 1`, not `last_seen`.
  - A message older than the account's retention window is NOT imported
    (no message row, no attachments stored) and the watermark advances past it.
  - The retention purge KEEPS the processed key, so the next poll does not
    re-import the purged message.
  - The processed key includes the FOLDER (a Sent UID must not collide with
    an INBOX UID), and the legacy `account:uid` form is still honoured.
  - The server accepts `retention_days = 0` ("keep forever") on create/update.

Run with: python3 tests/test_mail_retention_loop.py
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scanner.mail_scanner as ms  # noqa: E402

try:
    import server as _server  # noqa: E402
    _SERVER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent (needs a live DB)
    _server = None
    _SERVER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeMsg:
    """Minimal imap_tools message stand-in."""

    def __init__(self, uid, date=None, attachments=None):
        self.uid = str(uid)
        self.attachments = attachments or []
        self.headers = {}
        self.from_values = None
        self.to_values = []
        self.subject = f"msg {uid}"
        self.date = date
        self.text = "body"
        self.html = ""
        self.flags = []


class FakeFolder:
    def set(self, folder):
        pass

    def status(self):
        return {"MESSAGES": 1, "RECENT": 0, "UIDNEXT": 2, "UIDVALIDITY": 7, "UNSEEN": 0}


class FakeMailbox:
    """Fake imap_tools MailBox.

    `uids` is the list of UIDs the server "has". The IMAP `n:*` rule is
    implemented faithfully: a `n:*` range ALWAYS returns the folder's highest
    UID, even when that UID is BELOW n.
    """

    uids = [103, 102, 101]
    seen_criteria = []
    fetched_uids = []

    def __init__(self, host, port=None):
        self.host = host
        self.folder = FakeFolder()

    def login(self, user, password):
        pass

    def xoauth2(self, user, token):
        pass

    def fetch(self, criteria=None, mark_seen=False, bulk=False):
        FakeMailbox.seen_criteria.append(str(criteria))
        crit = str(criteria)
        if "UID" in crit:
            # Parse "UID <start>:*" out of the criterion, e.g. "(UID 101:*)".
            start = 0
            for token in crit.replace("(", " ").replace(")", " ").split():
                if token.isdigit():
                    start = int(token)
                    break
            # RFC 3501: `n:*` resolves to the HIGHEST UID in the folder, even
            # when that UID is lower than n. The other UIDs are those >= n.
            in_range = [u for u in FakeMailbox.uids if u >= start]
            highest = max(FakeMailbox.uids) if FakeMailbox.uids else None
            picked = set(in_range)
            if highest is not None and highest not in picked:
                picked.add(highest)
            out = [u for u in FakeMailbox.uids if u in picked]
        else:
            out = list(FakeMailbox.uids)
        FakeMailbox.fetched_uids.extend(out)
        return [FakeMsg(u) for u in out]

    def logout(self):
        pass


def _cfg(**kw):
    base = {"host": "h", "port": 993, "user": "u", "password": "p",
            "inbox": "INBOX", "folders": ["INBOX"], "account_id": 1,
            "auth_type": "password", "retention_days": 0}
    base.update(kw)
    return base


def _old_date(days=120):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _fresh_date(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class _TempProcessedIds:
    """Redirect the processed-IDs file into a temp dir for the test."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = ms.PROCESSED_IDS_FILE
        ms.PROCESSED_IDS_FILE = Path(self._tmp.name) / "processed_ids.json"
        return self

    def __exit__(self, *exc):
        ms.PROCESSED_IDS_FILE = self._orig
        self._tmp.cleanup()
        return False


class _TempStaging:
    """Redirect the attachment staging root into a private temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = ms._staging_root
        ms._staging_root = lambda: Path(self._tmp.name)
        return self

    def __exit__(self, *exc):
        ms._staging_root = self._orig
        self._tmp.cleanup()
        return False


# --------------------------------------------------------------------------
# Fix 1 — incremental fetch: `last_seen + 1` + client-side UID filter
# --------------------------------------------------------------------------
class TestIncrementalUidFilter(unittest.TestCase):
    def setUp(self):
        FakeMailbox.seen_criteria = []
        FakeMailbox.fetched_uids = []
        FakeMailbox.uids = [103, 102, 101]

    def test_range_starts_at_last_seen_plus_one(self):
        FakeMailbox.uids = [103, 102, 101]
        with _TempStaging(), \
            patch.object(ms, "MailBox", FakeMailbox):
            list(ms._fetch_messages(_cfg(), "INBOX", uid_min=100))
        joined = " ".join(FakeMailbox.seen_criteria)
        self.assertIn("101", joined,
                      f"expected the UID range to start at last_seen+1 (101): {joined}")
        self.assertNotIn("100", joined,
                         f"the range must not include last_seen itself: {joined}")

    def test_highest_uid_below_min_is_dropped(self):
        """REGRESSION: IMAP `n:*` returns the folder's HIGHEST UID even when
        it is below n. That overlap row must be dropped client-side."""
        # The folder's highest UID is 50 — BELOW the watermark of 100.
        FakeMailbox.uids = [50]
        with _TempStaging(), \
            patch.object(ms, "MailBox", FakeMailbox):
            msgs = list(ms._fetch_messages(_cfg(), "INBOX", uid_min=100))
        self.assertEqual(msgs, [], "the stale overlap row must not be yielded")

    def test_the_fake_really_implements_the_n_star_rule(self):
        """Guard for the guard: the fake IMAP must return the folder's
        highest UID even when it is below the requested start — that is the
        IMAP behaviour the fix defends against."""
        FakeMailbox.uids = [50]
        box = FakeMailbox("h", port=993)
        seen = box.fetch(criteria=type("C", (), {"__str__": lambda s: "(UID 101:*)"})())
        self.assertEqual([m.uid for m in seen], ["50"],
                         "the fake must reproduce the IMAP n:* rule")

    def test_highest_uid_below_min_is_not_fetched_at_all(self):
        """The filter must run BEFORE the per-message fetch, so no attachment
        bytes are downloaded/staged for a dropped message."""
        FakeMailbox.uids = [50]
        fetched = []

        def fake_single(cfg, folder, uid, staging_dir=None):
            fetched.append(uid)
            return {"uid": uid, "subject": "s", "from": "f", "folder": folder,
                    "attachments": []}

        with _TempStaging(), \
            patch.object(ms, "MailBox", FakeMailbox), \
            patch.object(ms, "_fetch_single_message", side_effect=fake_single):
            msgs = list(ms._fetch_messages(_cfg(), "INBOX", uid_min=100))
        self.assertEqual(msgs, [])
        self.assertEqual(fetched, [], "a dropped message must never be fetched")

    def test_newer_messages_survive_the_filter(self):
        FakeMailbox.uids = [103, 102, 101]
        with _TempStaging(), \
            patch.object(ms, "MailBox", FakeMailbox), \
            patch.object(ms, "_fetch_single_message",
                         side_effect=lambda cfg, folder, uid, staging_dir=None: {
                             "uid": str(uid), "subject": "s", "from": "f",
                             "folder": folder, "attachments": []}):
            msgs = list(ms._fetch_messages(_cfg(), "INBOX", uid_min=100))
        self.assertEqual([m["uid"] for m in msgs], ["103", "102", "101"])


# --------------------------------------------------------------------------
# Fix 2 — skip messages older than the retention window
# --------------------------------------------------------------------------
class TestRetentionSkipOnImport(unittest.TestCase):
    def test_old_message_is_not_stored(self):
        stored = []
        with _TempProcessedIds(), \
            patch.object(ms, "_store_message",
                         side_effect=lambda m, c: stored.append(m) or 1), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            out = ms._process_message(
                {"uid": "1", "subject": "old", "from": "f", "to": "t",
                 "body_text": "b", "body_html": "", "date": _old_date(120),
                 "folder": "Sent"},
                _cfg(retention_days=90))
        self.assertEqual(out.get("status"), "skipped_older_than_retention")
        self.assertEqual(stored, [], "an over-retention message must not be stored")

    def test_old_message_stores_no_attachments(self):
        """Attachments must not be written for a skipped message."""
        store_calls = []

        def fake_store(msg_id, account_id, attachments, msg):
            store_calls.append(attachments)

        with _TempProcessedIds(), \
            patch.object(ms, "_store_message", return_value=1), \
            patch.object(ms, "_store_attachments", side_effect=fake_store), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            ms._process_message(
                {"uid": "1", "subject": "old", "from": "f", "to": "t",
                 "body_text": "b", "body_html": "", "date": _old_date(120),
                 "folder": "Sent",
                 "attachments": [{"filename": "1.pdf", "size_bytes": 10}]},
                _cfg(retention_days=90))
        self.assertEqual(store_calls, [], "no attachment may be stored for a skipped message")

    def test_fresh_message_is_stored(self):
        stored = []
        with _TempProcessedIds(), \
            patch.object(ms, "_store_message",
                         side_effect=lambda m, c: stored.append(m) or 1), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            out = ms._process_message(
                {"uid": "2", "subject": "new", "from": "f", "to": "t",
                 "body_text": "b", "body_html": "", "date": _fresh_date(1),
                 "folder": "INBOX"},
                _cfg(retention_days=90))
        self.assertNotEqual(out.get("status"), "skipped_older_than_retention")
        self.assertEqual(len(stored), 1)

    def test_retention_zero_keeps_everything(self):
        """retention_days <= 0 means keep forever — nothing is skipped."""
        stored = []
        with _TempProcessedIds(), \
            patch.object(ms, "_store_message",
                         side_effect=lambda m, c: stored.append(m) or 1), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            out = ms._process_message(
                {"uid": "3", "subject": "ancient", "from": "f", "to": "t",
                 "body_text": "b", "body_html": "", "date": _old_date(3000),
                 "folder": "Sent"},
                _cfg(retention_days=0))
        self.assertNotEqual(out.get("status"), "skipped_older_than_retention")
        self.assertEqual(len(stored), 1)

    def test_skipped_message_is_marked_processed(self):
        with _TempProcessedIds():
            with patch.object(ms, "_store_message", return_value=1), \
                patch.object(ms, "_append_log"), \
                patch.object(ms, "logger"):
                ms._process_message(
                    {"uid": "77", "subject": "old", "from": "f", "to": "t",
                     "body_text": "b", "body_html": "", "date": _old_date(200),
                     "folder": "Sent"},
                    _cfg(retention_days=90))
            self.assertTrue(ms._is_processed(1, "77", "Sent"))

    def test_undated_message_is_not_skipped(self):
        stored = []
        with _TempProcessedIds(), \
            patch.object(ms, "_store_message",
                         side_effect=lambda m, c: stored.append(m) or 1), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            out = ms._process_message(
                {"uid": "4", "subject": "no date", "from": "f", "to": "t",
                 "body_text": "b", "body_html": "", "date": "",
                 "folder": "Sent"},
                _cfg(retention_days=90))
        self.assertNotEqual(out.get("status"), "skipped_older_than_retention")

    def test_cutoff_matches_the_retention_purge_semantics(self):
        """The skip and the purge must agree: `date_received` is a plain
        TIMESTAMP, so the comparison uses the wall-clock value."""
        cfg = _cfg(retention_days=90)
        cutoff = ms._retention_cutoff(cfg)
        self.assertIsNotNone(cutoff)
        self.assertIsNone(cutoff.tzinfo,
                          "the cutoff must be naive, like mail_messages.date_received")
        # A message exactly on the boundary is NOT older (the purge uses a
        # strict `<` comparison too).
        boundary = cutoff.isoformat()
        self.assertFalse(ms._message_older_than_cutoff({"date": boundary}, cutoff))
        self.assertTrue(ms._message_older_than_cutoff(
            {"date": (cutoff - timedelta(days=1)).isoformat()}, cutoff))

    def test_parsed_date_keeps_the_wall_clock_value(self):
        """The stored date drops the offset — the skip must use the same value."""
        parsed = ms._parse_message_date("2026-07-19T10:00:00-04:00")
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.tzinfo)
        self.assertEqual(parsed, datetime(2026, 7, 19, 10, 0, 0))


# --------------------------------------------------------------------------
# The full loop: poll twice, nothing is imported twice
# --------------------------------------------------------------------------
class TestNoReimportLoop(unittest.TestCase):
    def test_second_poll_imports_nothing(self):
        """A folder whose highest message is older than retention: the first
        poll imports nothing, the second poll imports nothing, and no
        attachment is ever stored."""
        FakeMailbox.uids = [4114]
        FakeMailbox.seen_criteria = []
        FakeMailbox.fetched_uids = []

        processed = []
        stored_attachments = []

        def fake_store(msg_id, account_id, attachments, msg):
            stored_attachments.append(attachments)

        def fake_process(msg, cfg):
            # Route through the real guard logic (skip + processed check),
            # but never touch the DB.
            if ms._is_processed(cfg.get("account_id"), msg.get("uid", ""), msg.get("folder", "")):
                return {"status": "already_processed"}
            cutoff = ms._retention_cutoff(cfg)
            if ms._message_older_than_cutoff(msg, cutoff):
                ms._mark_processed(cfg.get("account_id"), msg.get("uid", ""), msg.get("folder", ""))
                return {"status": "skipped_older_than_retention"}
            processed.append(msg)
            ms._mark_processed(cfg.get("account_id"), msg.get("uid", ""), msg.get("folder", ""))
            return {"status": "processed"}

        with _TempProcessedIds(), \
            _TempStaging(), \
            patch.object(ms, "MailBox", FakeMailbox), \
            patch.object(ms, "_fetch_single_message",
                         side_effect=lambda cfg, folder, uid, staging_dir=None: {
                             "uid": str(uid), "subject": "s", "from": "f",
                             "folder": folder, "date": _old_date(150),
                             "attachments": [{"filename": "1.pdf", "size_bytes": 10}]}), \
            patch.object(ms, "_store_attachments", side_effect=fake_store), \
            patch.object(ms, "_process_message", side_effect=fake_process), \
            patch.object(ms, "_read_folder_status", return_value=(7, 4115)), \
            patch.object(ms, "_get_folder_watermark", return_value=(7, 4113)), \
            patch.object(ms, "_save_folder_watermark"), \
            patch.object(ms, "logger"):
            cfg = _cfg(retention_days=90, folders=["Sent"])
            first = ms._poll_mailbox_cfg(cfg)
            second = ms._poll_mailbox_cfg(cfg)

        self.assertEqual(len(processed), 0, "nothing may be imported")
        self.assertEqual(stored_attachments, [], "no attachment may be stored")
        self.assertEqual(first[0].get("status"), "skipped_older_than_retention")
        # The second poll must not import it either: either the processed-ID
        # guard catches it (the message was marked processed when skipped) or
        # the retention guard skips it again. Never "processed".
        self.assertIn(second[0].get("status"),
                      ("already_processed", "skipped_older_than_retention"))
        self.assertNotEqual(second[0].get("status"), "processed")

    def test_watermark_advances_past_a_skipped_message(self):
        """The watermark must still advance past a skipped message."""
        saved = {}

        def fake_fetch(cfg, folder, since=None, uid_min=None):
            return [{"uid": "4114", "subject": "s", "from": "f", "folder": folder,
                     "date": _old_date(150), "attachments": []}]

        with _TempProcessedIds(), \
            patch.object(ms, "_get_folder_watermark", return_value=(7, 4000)), \
            patch.object(ms, "_read_folder_status", return_value=(7, 4115)), \
            patch.object(ms, "_fetch_messages", side_effect=fake_fetch), \
            patch.object(ms, "_process_message",
                         return_value={"status": "skipped_older_than_retention"}), \
            patch.object(ms, "_save_folder_watermark",
                         side_effect=lambda a, f, v, l: saved.update(validity=v, last=l)), \
            patch.object(ms, "logger"):
            ms._poll_mailbox_cfg(_cfg(retention_days=90, folders=["Sent"]))
        self.assertEqual(saved.get("last"), 4114)


# --------------------------------------------------------------------------
# Fix 3 — the retention purge keeps the processed key
# --------------------------------------------------------------------------
class TestRetentionKeepsProcessedKey(unittest.TestCase):
    def _run_retention(self, purged_rows):
        executes = []

        def fake_query_dicts(sql, params=None, *a, **k):
            if "FROM mail_accounts" in sql:
                return [{"id": 2, "email": "crm@x.com", "retention_days": 90}]
            if "SELECT account_id, imap_uid, folder" in sql:
                return purged_rows
            return []

        def fake_execute(sql, params=None, *a, **k):
            executes.append((sql, params))
            return 1 if "DELETE FROM mail_messages" in sql else None

        mock_db = MagicMock()
        mock_db.query_dicts.side_effect = fake_query_dicts
        mock_db.execute.side_effect = fake_execute
        with patch.object(ms, "db", mock_db), patch.object(ms, "logger"):
            ms._enforce_retention()

    def test_purged_uid_stays_processed(self):
        with _TempProcessedIds():
            self._run_retention([{"account_id": 2, "imap_uid": "4114", "folder": "Sent"}])
            self.assertTrue(ms._is_processed(2, "4114", "Sent"),
                            "a purged UID must stay in the processed set")

    def test_purged_uid_stays_processed_legacy_form(self):
        with _TempProcessedIds():
            self._run_retention([{"account_id": 2, "imap_uid": "4114", "folder": "Sent"}])
            self.assertTrue(ms._is_processed(2, "4114"),
                            "the legacy account:uid key must also be kept")

    def test_purged_message_is_not_reimported_on_the_next_poll(self):
        """End-to-end: purge, then poll — the message is not imported again."""
        with _TempProcessedIds():
            self._run_retention([{"account_id": 2, "imap_uid": "4114", "folder": "Sent"}])
            stored = []
            with patch.object(ms, "_store_message",
                             side_effect=lambda m, c: stored.append(m) or 1), \
                patch.object(ms, "_append_log"), patch.object(ms, "logger"):
                out = ms._process_message(
                    {"uid": "4114", "subject": "s", "from": "f", "to": "t",
                     "body_text": "b", "body_html": "", "date": _old_date(150),
                     "folder": "Sent"},
                    _cfg(account_id=2, retention_days=90))
            self.assertEqual(out.get("status"), "already_processed")
            self.assertEqual(stored, [])


# --------------------------------------------------------------------------
# Fix 3b — the processed key includes the folder; old keys still work
# --------------------------------------------------------------------------
class TestProcessedKeyFolderScoping(unittest.TestCase):
    def test_key_includes_folder(self):
        with _TempProcessedIds():
            ms._mark_processed(6, "4114", "Sent")
            key = ms._processed_key(6, "4114", "Sent")
            self.assertIn("Sent", key)
            self.assertTrue(key.startswith("6:"))

    def test_sent_uid_does_not_collide_with_inbox_uid(self):
        with _TempProcessedIds():
            ms._mark_processed(6, "4114", "Sent")
            self.assertTrue(ms._is_processed(6, "4114", "Sent"))
            self.assertFalse(ms._is_processed(6, "4114", "INBOX"),
                             "a Sent UID must not suppress an INBOX UID")

    def test_legacy_key_is_still_honoured(self):
        """A deployed processed_ids.json holds `account:uid` keys — they must
        still suppress a re-import (no mass re-import on deploy)."""
        with _TempProcessedIds() as t:
            ms.PROCESSED_IDS_FILE.write_text(json.dumps(["6:4114"]), encoding="utf-8")
            self.assertTrue(ms._is_processed(6, "4114", "Sent"))
            self.assertTrue(ms._is_processed(6, "4114"))
            # A different UID is still processed.
            self.assertFalse(ms._is_processed(6, "9999", "Sent"))

    def test_legacy_key_does_not_leak_across_accounts(self):
        with _TempProcessedIds() as t:
            ms.PROCESSED_IDS_FILE.write_text(json.dumps(["6:4114"]), encoding="utf-8")
            self.assertFalse(ms._is_processed(7, "4114", "Sent"))

    def test_new_key_is_written_in_the_folder_scoped_form(self):
        with _TempProcessedIds() as t:
            ms._mark_processed(6, "4114", "Sent")
            data = json.loads(ms.PROCESSED_IDS_FILE.read_text(encoding="utf-8"))
            self.assertIn("6:Sent:4114", data)


# --------------------------------------------------------------------------
# Fix 4 — retention_days = 0 ("keep forever") on the server
# --------------------------------------------------------------------------
@unittest.skipIf(_server is None, "needs importable server (live DB)")
class TestRetentionDaysZeroAccepted(unittest.TestCase):
    def test_coerce_keeps_zero(self):
        srv = _server
        self.assertEqual(srv._coerce_retention_days(0, 90), 0)
        self.assertEqual(srv._coerce_retention_days("0", 90), 0)

    def test_coerce_defaults_on_garbage(self):
        srv = _server
        self.assertEqual(srv._coerce_retention_days(None, 90), 90)
        self.assertEqual(srv._coerce_retention_days("", 90), 90)
        self.assertEqual(srv._coerce_retention_days("abc", 90), 90)
        self.assertEqual(srv._coerce_retention_days(-5, 90), 90)

    def test_coerce_keeps_a_real_value(self):
        srv = _server
        self.assertEqual(srv._coerce_retention_days(365, 90), 365)
        self.assertEqual(srv._coerce_retention_days("365", 90), 365)

    def test_update_handler_writes_zero(self):
        srv = _server
        executes = []
        mock_db = MagicMock()
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: executes.append((sql, params))
        handler = MagicMock()
        with patch.object(srv, "_require_auth", return_value={"id": 1, "is_admin": True}), \
            patch.object(srv, "_mail_account_manageable", return_value=True), \
            patch.object(srv, "_read_body", return_value=b'{"retention_days": 0}'), \
            patch.object(srv, "_json_response", lambda h, s, b: None), \
            patch.object(srv, "db", mock_db), \
            patch.object(srv, "logger"):
            srv.KanbanHandler._handle_mail_account_update(handler, 2)
        self.assertTrue(executes, "the update must reach the DB")
        sql, params = executes[-1]
        self.assertIn("retention_days = %s", sql)
        self.assertIn(0, params, "retention_days 0 (keep forever) must be stored")

    def test_update_handler_defaults_garbage_to_90(self):
        srv = _server
        executes = []
        mock_db = MagicMock()
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: executes.append((sql, params))
        handler = MagicMock()
        with patch.object(srv, "_require_auth", return_value={"id": 1, "is_admin": True}), \
            patch.object(srv, "_mail_account_manageable", return_value=True), \
            patch.object(srv, "_read_body", return_value=b'{"retention_days": ""}'), \
            patch.object(srv, "_json_response", lambda h, s, b: None), \
            patch.object(srv, "db", mock_db), \
            patch.object(srv, "logger"):
            srv.KanbanHandler._handle_mail_account_update(handler, 2)
        sql, params = executes[-1]
        self.assertIn(90, params)

    def test_frontend_keeps_zero(self):
        js = (REPO_ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function mailRetentionDaysInput", js)
        # The old `parseInt(...) || 90` pattern must be gone from the
        # retention field's three call sites.
        self.assertNotIn("parseInt($('#account-settings-retention-days').value) || 90", js)
        self.assertNotIn("parseInt($('#account-settings-retention-days')?.value) || 90", js)
        self.assertEqual(js.count("mailRetentionDaysInput("), 3,
                         "all three retention call sites must use the helper")


if __name__ == "__main__":
    unittest.main(verbosity=1)
