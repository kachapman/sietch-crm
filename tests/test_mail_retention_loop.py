#!/usr/bin/env python3
"""Unit tests for the mail retention re-import loop fix (2026-11-04).

Covers, with stubs/mocks (no live IMAP, no Postgres):
  - The IMAP `n:*` rule: a folder whose HIGHEST UID is below `n` still
    returns that UID — it must be dropped client-side (regression).
  - The incremental range starts at `last_seen + 1`, not `last_seen`.
  - A message older than the account's retention window is NOT imported
    (no message row, no attachments stored) and, since 2026-11-11, the
    watermark does NOT advance past it in that poll (only a contiguous prefix
    of resolved UIDs moves the watermark).
  - Undated / 1900-sentinel messages ARE imported (Date → INTERNALDATE →
    Received → import time) and are never marked skipped_older_than_retention.
  - A failed FETCH, a failed STORE or a remaining retention skip holds the
    watermark immediately before the first unresolved UID.
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
        """The skip and the purge must agree.

        Live bug 2026-11-09: `date_received` was a plain TIMESTAMP and both
        sides of the comparison were naive wall clock, so the stored instant
        depended on the DB session timezone. Migration 048 makes the column
        `timestamptz`; the cutoff is therefore AWARE UTC, exactly like the
        stored value, and the comparison is an instant comparison.
        """
        cfg = _cfg(retention_days=90)
        cutoff = ms._retention_cutoff(cfg)
        self.assertIsNotNone(cutoff)
        self.assertEqual(cutoff.tzinfo, timezone.utc,
                         "the cutoff must be aware UTC, like mail_messages.date_received")
        # A message exactly on the boundary is NOT older (the purge uses a
        # strict `<` comparison too).
        boundary = cutoff.isoformat()
        self.assertFalse(ms._message_older_than_cutoff({"date": boundary}, cutoff))
        self.assertTrue(ms._message_older_than_cutoff(
            {"date": (cutoff - timedelta(days=1)).isoformat()}, cutoff))

    def test_parsed_date_is_converted_to_utc(self):
        """The offset must be APPLIED, not dropped.

        `2026-07-19T10:00:00-04:00` is 14:00 UTC. The old code kept 10:00 and
        threw the offset away, which is how a message could be judged (and
        stored) hours away from when it was actually sent.
        """
        parsed = ms._parse_message_date("2026-07-19T10:00:00-04:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed, datetime(2026, 7, 19, 14, 0, 0, tzinfo=timezone.utc))

    def test_parsed_rfc2822_header(self):
        parsed = ms._parse_message_date("Mon, 9 Nov 2026 13:20:03 -0500")
        self.assertEqual(parsed, datetime(2026, 11, 9, 18, 20, 3, tzinfo=timezone.utc))


# --------------------------------------------------------------------------
# Undated / 1900-sentinel messages are IMPORTED (2026-11-11)
# --------------------------------------------------------------------------
class _FakeImapMessage:
    """Minimal imap_tools MailMessage stand-in for _fetch_single_message."""

    def __init__(self, uid, date=None, headers=None, internal_date=None):
        self.uid = str(uid)
        self.date = date
        self.headers = headers or {}
        self.internal_date = internal_date
        self.attachments = []
        self.from_values = None
        self.to_values = []
        self.subject = "s"
        self.text = "body"
        self.html = ""
        self.flags = []
        self.obj = None


def _single_message_mailbox(message):
    """A fake MailBox class that returns exactly `message` for any fetch."""

    class _Box:
        client = None

        def __init__(self, host, port=None):
            self.folder = self
            self._message = message

        def set(self, folder):
            pass

        def login(self, user, password):
            pass

        def xoauth2(self, user, token):
            pass

        def fetch(self, criteria=None, mark_seen=False, bulk=False):
            return [self._message]

        def logout(self):
            pass

    return _Box


def _internaldate_now(days=0):
    return datetime.now(timezone.utc) - timedelta(days=days)


class TestUndatedMessagesImport(unittest.TestCase):
    """Live bug 2026-11-11: SMTP sends had no Date header, imap_tools reported
    1900-01-01, and the 90-day retention guard skipped them as ancient."""

    def _fetch(self, date=None, headers=None, internal_date=None, uid=12397):
        message = _FakeImapMessage(uid, date=date, headers=headers,
                                   internal_date=internal_date)
        with _TempStaging(), \
            patch.object(ms, "MailBox", _single_message_mailbox(message)):
            return ms._fetch_single_message(_cfg(), "INBOX", uid)

    def test_1900_sentinel_does_not_produce_a_1900_date(self):
        msg = self._fetch(date=datetime(1900, 1, 1))
        self.assertIsNotNone(msg)
        self.assertNotIn("1900", msg["date"])
        self.assertNotEqual(msg["date_source"], "header")

    def test_1900_sentinel_prefers_internaldate(self):
        internal = datetime(2026, 7, 17, 9, 20, 0, tzinfo=timezone.utc).strftime(
            "%d-%b-%Y %H:%M:%S +0000")
        msg = self._fetch(date=datetime(1900, 1, 1), internal_date=internal)
        self.assertEqual(msg["date_source"], "internaldate")
        self.assertEqual(msg["date"], "2026-07-17T09:20:00Z")

    def test_1900_aware_sentinel_is_also_undated(self):
        msg = self._fetch(date=datetime(1900, 1, 1, tzinfo=timezone.utc))
        self.assertNotIn("1900", msg["date"])

    def test_missing_date_uses_received_header(self):
        headers = {"received": ("from a by b with ESMTPS id x;\n"
                                " Mon, 9 Nov 2026 13:20:03 -0500",)}
        msg = self._fetch(date=None, headers=headers)
        self.assertEqual(msg["date_source"], "received")
        self.assertEqual(msg["date"], "2026-11-09T18:20:03Z")

    def test_missing_date_and_no_fallbacks_uses_import_time(self):
        msg = self._fetch(date=None)
        self.assertEqual(msg["date_source"], "import")
        self.assertTrue(msg["date"].endswith("Z"))
        self.assertNotIn("1900", msg["date"])

    def test_a_real_date_is_kept_verbatim(self):
        msg = self._fetch(date=datetime(2026, 11, 9, 18, 20, 3, tzinfo=timezone.utc))
        self.assertEqual(msg["date_source"], "header")
        self.assertEqual(msg["date"], "2026-11-09T18:20:03Z")

    def test_sentinel_message_imports_and_is_never_retention_skipped(self):
        internal = _internaldate_now(days=1).strftime("%d-%b-%Y %H:%M:%S +0000")
        msg = self._fetch(date=datetime(1900, 1, 1), internal_date=internal)
        stored = []
        with _TempProcessedIds(), \
            patch.object(ms, "_store_message",
                         side_effect=lambda m, c: stored.append(m) or 1), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            out = ms._process_message(msg, _cfg(retention_days=90))
        self.assertNotEqual(out.get("status"), "skipped_older_than_retention")
        self.assertEqual(len(stored), 1, "an undated message must be imported")
        self.assertEqual(stored[0]["date_source"], "internaldate")

    def test_a_really_old_date_is_still_skipped(self):
        msg = self._fetch(date=datetime.now(timezone.utc) - timedelta(days=150))
        with _TempProcessedIds(), \
            patch.object(ms, "_store_message", return_value=1), \
            patch.object(ms, "_append_log"), \
            patch.object(ms, "logger"):
            out = ms._process_message(msg, _cfg(retention_days=90))
        self.assertEqual(out.get("status"), "skipped_older_than_retention")

    def test_store_message_persists_the_date_source(self):
        writes = []

        def fake_query_one(sql, params=None, *a, **k):
            if "INSERT INTO mail_messages" in sql:
                writes.append((sql, params))
                return {"id": 9}
            return None

        mock_db = MagicMock()
        mock_db.query_one.side_effect = fake_query_one
        msg = {
            "uid": "12397", "message_id": "", "from": "a@b.c", "to": "d@e.f",
            "subject": "no date", "body_text": "b", "body_html": "",
            "date": "2026-11-11T12:00:00Z", "date_source": "import",
            "folder": "INBOX", "attachments": [], "raw_headers": "",
        }
        with _TempProcessedIds(), patch.object(ms, "db", mock_db), \
            patch.object(ms, "logger"):
            msg_id = ms._store_message(msg, _cfg())
        self.assertEqual(msg_id, 9)
        sql, params = writes[0]
        self.assertIn("date_source", sql)
        self.assertIn("import", params)


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

    def test_watermark_does_not_advance_past_a_skipped_message(self):
        """BEHAVIOUR CHANGE 2026-11-11: a retention skip holds the watermark.

        The old rule advanced to the max fetched UID even when the message was
        skipped, which is how the six stranded SMTP messages were marked done
        without ever being imported. The watermark now stays immediately
        before the first unresolved UID so a later poll retries it.
        """
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
        self.assertEqual(saved.get("last"), 4000,
                         "the watermark must stay before the skipped UID")

    def test_next_poll_advances_once_the_skip_is_already_processed(self):
        """The skip is marked processed, so the NEXT poll resolves it and the
        watermark can move on. Retrying is not a permanent stall."""
        saved = {}

        def fake_fetch(cfg, folder, since=None, uid_min=None):
            return [{"uid": "4114", "subject": "s", "from": "f", "folder": folder,
                     "date": _old_date(150), "attachments": []}]

        with _TempProcessedIds(), \
            patch.object(ms, "_get_folder_watermark", return_value=(7, 4000)), \
            patch.object(ms, "_read_folder_status", return_value=(7, 4115)), \
            patch.object(ms, "_fetch_messages", side_effect=fake_fetch), \
            patch.object(ms, "_process_message", return_value={"status": "skipped_older_than_retention"}), \
            patch.object(ms, "_save_folder_watermark",
                         side_effect=lambda a, f, v, l: saved.update(last=l)), \
            patch.object(ms, "logger"):
            ms._poll_mailbox_cfg(_cfg(retention_days=90, folders=["Sent"]))
        self.assertEqual(saved.get("last"), 4000)

        with _TempProcessedIds(), \
            patch.object(ms, "_get_folder_watermark", return_value=(7, 4000)), \
            patch.object(ms, "_read_folder_status", return_value=(7, 4115)), \
            patch.object(ms, "_fetch_messages", side_effect=fake_fetch), \
            patch.object(ms, "_process_message", return_value={"status": "already_processed"}), \
            patch.object(ms, "_save_folder_watermark",
                         side_effect=lambda a, f, v, l: saved.update(last=l)), \
            patch.object(ms, "logger"):
            ms._poll_mailbox_cfg(_cfg(retention_days=90, folders=["Sent"]))
        self.assertEqual(saved.get("last"), 4114)


class TestWatermarkHoldsOnFailures(unittest.TestCase):
    """2026-11-11: the watermark only moves through resolved UIDs."""

    def _poll(self, results):
        saved = {}
        with _TempProcessedIds(), \
            patch.object(ms, "_list_imap_folders", return_value=[]), \
            patch.object(ms, "_get_folder_watermark", return_value=(7, 1000)), \
            patch.object(ms, "_read_folder_status", return_value=(7, 1010)), \
            patch.object(ms, "_fetch_messages", side_effect=lambda *a, **k: iter(results)), \
            patch.object(ms, "_process_message",
                         side_effect=lambda msg, cfg: {"status": msg.get("_status", "processed"),
                                                       "uid": msg.get("uid")}), \
            patch.object(ms, "_save_folder_watermark",
                         side_effect=lambda a, f, v, l: saved.update(last=l)), \
            patch.object(ms, "logger"):
            ms._poll_mailbox_cfg(_cfg(folders=["INBOX"]))
        return saved

    def test_all_resolved_advances_to_the_max_uid(self):
        saved = self._poll([
            {"uid": "1003", "folder": "INBOX"},
            {"uid": "1002", "folder": "INBOX"},
            {"uid": "1001", "folder": "INBOX"},
        ])
        self.assertEqual(saved.get("last"), 1003)

    def test_fetch_failure_holds_the_watermark_below_that_uid(self):
        saved = self._poll([
            {"uid": "1003", "folder": "INBOX"},
            {"uid": "1002", "folder": "INBOX", "fetch_failed": True},
            {"uid": "1001", "folder": "INBOX"},
        ])
        self.assertEqual(saved.get("last"), 1001,
                         "must stop immediately before the failed UID 1002")

    def test_store_failure_holds_the_watermark_below_that_uid(self):
        saved = self._poll([
            {"uid": "1003", "folder": "INBOX"},
            {"uid": "1002", "folder": "INBOX", "_status": "store_failed"},
            {"uid": "1001", "folder": "INBOX"},
        ])
        self.assertEqual(saved.get("last"), 1001)

    def test_newer_resolved_messages_are_not_passed_over_a_blocked_uid(self):
        saved = self._poll([
            {"uid": "1010", "folder": "INBOX"},
            {"uid": "1005", "folder": "INBOX", "_status": "skipped_older_than_retention"},
            {"uid": "1003", "folder": "INBOX"},
        ])
        self.assertEqual(saved.get("last"), 1003,
                         "1003 and 1010 resolved, but 1005 never did")

    def test_failed_fetch_is_not_processed(self):
        """A fetch-failure marker must never reach _process_message (nothing to
        store, and the message is retried on a later poll)."""
        processed = []
        with _TempProcessedIds(), \
            patch.object(ms, "_list_imap_folders", return_value=[]), \
            patch.object(ms, "_get_folder_watermark", return_value=(7, 1000)), \
            patch.object(ms, "_read_folder_status", return_value=(7, 1010)), \
            patch.object(ms, "_fetch_messages",
                         side_effect=lambda *a, **k: iter([
                             {"uid": "1002", "folder": "INBOX", "fetch_failed": True}])), \
            patch.object(ms, "_process_message",
                         side_effect=lambda msg, cfg: processed.append(msg) or {"status": "processed"}), \
            patch.object(ms, "_save_folder_watermark"), \
            patch.object(ms, "logger"):
            out = ms._poll_mailbox_cfg(_cfg(folders=["INBOX"]))
        self.assertEqual(processed, [])
        self.assertEqual(out[0].get("status"), "fetch_failed")

    def test_real_fetch_generator_reports_a_fetch_error_as_a_marker(self):
        """`_fetch_messages` must not silently drop a UID whose FETCH threw —
        otherwise the poll loop can never know to hold the watermark."""
        import scanner.mail_scanner as _ms

        class _MiniMsg:
            def __init__(self, uid):
                self.uid = str(uid)

        class _BoomBox:
            def __init__(self, host, port=None):
                self.folder = self

            def set(self, folder):
                pass

            def login(self, user, password):
                pass

            def xoauth2(self, user, token):
                pass

            def fetch(self, criteria=None, mark_seen=False, bulk=False):
                crit = str(criteria or "")
                if ":" in crit:          # the UID-listing pass
                    return [_MiniMsg(1001), _MiniMsg(1002)]
                raise RuntimeError("IMAP connection dropped")

            def logout(self):
                pass

        with _TempStaging(), patch.object(_ms, "MailBox", _BoomBox):
            out = list(_ms._fetch_messages(_cfg(), "INBOX", uid_min=1000))
        self.assertEqual([m.get("uid") for m in out], ["1002", "1001"])
        self.assertTrue(all(m.get("fetch_failed") for m in out),
                        "a failed FETCH must be reported, not dropped")


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
