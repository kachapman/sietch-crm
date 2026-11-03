#!/usr/bin/env python3
"""Regression tests for the 2026-11 Telegram ~91x duplicate incident.

Root cause: the notification dispatcher (the sole sender of notification
Telegram messages) had no idempotency —
  * SELECT-then-SEND-then-MARK in separate statements, so overlapping
    dispatcher passes (stacked server processes, crash between send and
    mark) delivered the same row twice or more;
  * any row left with telegram_sent FALSE was re-sent on EVERY 5s poll
    forever: no backoff, no attempt cap, no dead-letter;
  * tag-notification INSERTs had no dedupe guard, so a retried POST queued
    a second row (and a second delivery) for the same reply.
The fix: atomic claim UPDATE before sending, exponential backoff +
dead-letter after TG_MAX_ATTEMPTS, and a dedupe_key UNIQUE guard at
creation. Companion bug: httpx logged full Telegram API URLs (with the bot
token) at INFO into the service journal; both entry points now silence
httpx/httpcore to WARNING.

These tests are mock-based (no Postgres) and follow the repo's source-guard
convention for behavior that needs a live socket or DB.

Run with: python3 tests/test_telegram_dedupe.py
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import notification_dispatcher as nd  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 18, 0, 0)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeDB:
    """Minimal in-memory stand-in honoring the dispatcher's SQL semantics:
    claim UPDATE (atomic, lease, cap), mark, backoff schedule, dead-letter,
    log INSERT, pending SELECT with the backoff-lease filter."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = []
        self.log = []
        self._next_id = 1

    def add_row(self, **kw):
        row = {
            "id": self._next_id, "user_id": 5, "type": "reply_tagged",
            "message": "x", "payload": {"event_id": 7, "reply_id": 9, "snippet": "hi"},
            "opportunity_id": 130, "actor_user_id": 8,
            "telegram_sent": False, "email_sent": True,
            "telegram_attempts": 0, "telegram_next_retry_at": None,
            "telegram_last_error": None,
            "actor_name": "Claudiu Chereji", "project_title": "Katherine Nelson",
            "project_number": 4242,
        }
        row.update(kw)
        self._next_id += 1
        self.rows.append(row)
        return row

    # -- db API --
    def query_dicts(self, sql, params=None):
        assert "telegram_next_retry_at" in sql, "pending SELECT must filter backoff leases"
        out = [r for r in self.rows
               if r["user_id"] is not None
               and ((not r["telegram_sent"]
                     and (r["telegram_next_retry_at"] is None or r["telegram_next_retry_at"] <= self.clock.now))
                    or (not r["email_sent"]))]
        limit = params[0] if params else 10
        return [dict(r) for r in out[:limit]]

    def query_one(self, sql, params=None):
        if "telegram_attempts" in sql and "FROM notifications WHERE id" in sql:
            for r in self.rows:
                if r["id"] == params[0]:
                    return {"telegram_attempts": r["telegram_attempts"]}
            return None
        if "notification_preferences" in sql:
            return None  # email disabled by default; telegram enabled by default
        if "users" in sql and "email" in sql:
            return None
        return None

    def execute(self, sql, params=None):
        if "INSERT INTO telegram_notification_log" in sql:
            self.log.append({"notification_id": params[0], "chat_id": params[1], "message_id": params[2]})
            return 1
        if "SET telegram_attempts = telegram_attempts + 1" in sql:
            lease, nid, cap = params
            for r in self.rows:
                if (r["id"] == nid and not r["telegram_sent"]
                        and r["telegram_attempts"] < cap
                        and (r["telegram_next_retry_at"] is None
                             or r["telegram_next_retry_at"] <= self.clock.now)):
                    r["telegram_attempts"] += 1
                    r["telegram_next_retry_at"] = self.clock.now + timedelta(seconds=lease)
                    return 1
            return 0
        if "telegram_last_error" in sql and "dead-letter" in sql:
            nid, cap = params
            for r in self.rows:
                if r["id"] == nid and not r["telegram_sent"] and r["telegram_attempts"] >= cap:
                    r["telegram_sent"] = True
                    r["telegram_last_error"] = (r["telegram_last_error"] or "") + " [dead-lettered: max attempts]"
                    return 1
            return 0
        if "SET telegram_sent = TRUE" in sql:
            for r in self.rows:
                if r["id"] == params[0]:
                    r["telegram_sent"] = True
                    return 1
            return 0
        if "SET telegram_next_retry_at = NOW()" in sql:
            delay, err, nid = params
            for r in self.rows:
                if r["id"] == nid:
                    r["telegram_next_retry_at"] = self.clock.now + timedelta(seconds=delay)
                    r["telegram_last_error"] = err
                    return 1
            return 0
        if "SET email_sent = TRUE" in sql:
            for r in self.rows:
                if r["id"] == params[0]:
                    r["email_sent"] = True
                    return 1
            return 0
        raise AssertionError(f"unexpected SQL in fake: {sql[:80]}")


def _patch_db(testcase, fake):
    testcase._patches = [
        patch.object(nd, "db", fake),
        patch.object(nd, "_get_employee_chat_id", lambda uid: 12345),
        patch.object(nd, "_is_customer", lambda uid: False),
        patch.object(nd, "_user_telegram_enabled", lambda uid, ntype="": True),
    ]
    for p in testcase._patches:
        p.start()
    testcase.addCleanup(lambda: [p.stop() for p in testcase._patches])


class TestAtomicClaim(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fake = FakeDB(self.clock)
        _patch_db(self, self.fake)

    def test_happy_path_sends_once_and_marks(self):
        self.fake.add_row()
        with patch.object(nd, "_send_telegram_message", return_value=4242) as send:
            sent = nd._process_pending_notifications()
        self.assertEqual(sent, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.fake.log), 1)
        self.assertTrue(self.fake.rows[0]["telegram_sent"])
        # Second poll: nothing left to send.
        with patch.object(nd, "_send_telegram_message", return_value=4242) as send2:
            self.assertEqual(nd._process_pending_notifications(), 0)
        self.assertEqual(send2.call_count, 0)

    def test_second_claim_loses_so_no_double_send(self):
        row = self.fake.add_row()
        self.assertTrue(nd._claim_telegram_notification(row["id"]))
        # Concurrent dispatcher racing the same row gets nothing.
        self.assertFalse(nd._claim_telegram_notification(row["id"]))
        with patch.object(nd, "_send_telegram_message", return_value=1) as send:
            self.assertFalse(nd._process_telegram(
                row["id"], row["user_id"], dict(row), "Katherine Nelson",
                "Claudiu Chereji", 130, 4242))
        self.assertEqual(send.call_count, 0)
        self.assertEqual(len(self.fake.log), 0)

    def test_no_send_without_claim(self):
        row = self.fake.add_row(telegram_next_retry_at=self.clock.now + timedelta(seconds=60))
        with patch.object(nd, "_send_telegram_message", return_value=1) as send:
            self.assertFalse(nd._process_telegram(
                row["id"], row["user_id"], dict(row), "Katherine Nelson",
                "Claudiu Chereji", 130, 4242))
        self.assertEqual(send.call_count, 0)

    def test_lost_mark_does_not_restorm(self):
        """A send that never marks (crash between send and mark) must NOT be
        re-sent on the next poll: the claim lease hides it."""
        row = self.fake.add_row()
        real_mark = nd._mark_telegram_sent
        try:
            nd._mark_telegram_sent = lambda nid: None  # lost mark
            with patch.object(nd, "_send_telegram_message", return_value=7):
                nd._process_pending_notifications()
                second = nd._process_pending_notifications()
            self.assertEqual(second, 0)
            self.assertEqual(len(self.fake.log), 1)
        finally:
            nd._mark_telegram_sent = real_mark


class TestBackoffAndDeadLetter(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fake = FakeDB(self.clock)
        _patch_db(self, self.fake)

    def test_failed_send_parks_behind_backoff(self):
        row = self.fake.add_row()
        with patch.object(nd, "_send_telegram_message", return_value=None):
            self.assertFalse(nd._process_telegram(
                row["id"], row["user_id"], dict(row), "Katherine Nelson",
                "Claudiu Chereji", 130, 4242))
        stored = self.fake.rows[0]
        self.assertFalse(stored["telegram_sent"])
        self.assertIsNotNone(stored["telegram_next_retry_at"])
        self.assertGreater(stored["telegram_next_retry_at"], self.clock.now)
        # Immediate re-poll: row filtered by the lease, no send attempted.
        with patch.object(nd, "_send_telegram_message", return_value=999) as send:
            self.assertEqual(nd._process_pending_notifications(), 0)
        self.assertEqual(send.call_count, 0)

    def test_retry_after_backoff_expiry(self):
        row = self.fake.add_row()
        with patch.object(nd, "_send_telegram_message", return_value=None):
            nd._process_telegram(row["id"], row["user_id"], dict(row),
                                 "Katherine Nelson", "Claudiu Chereji", 130, 4242)
        self.clock.advance(3600 + 1)
        with patch.object(nd, "_send_telegram_message", return_value=55) as send:
            self.assertEqual(nd._process_pending_notifications(), 1)
        self.assertEqual(send.call_count, 1)

    def test_poison_row_dead_lettered_at_cap(self):
        row = self.fake.add_row(telegram_attempts=nd.TG_MAX_ATTEMPTS,
                                telegram_next_retry_at=self.clock.now - timedelta(seconds=1))
        with patch.object(nd, "_send_telegram_message", return_value=1) as send:
            self.assertFalse(nd._process_telegram(
                row["id"], row["user_id"], dict(row), "Katherine Nelson",
                "Claudiu Chereji", 130, 4242))
        self.assertEqual(send.call_count, 0)
        stored = self.fake.rows[0]
        self.assertTrue(stored["telegram_sent"])
        self.assertIn("dead-letter", stored["telegram_last_error"] or "")
        # And it never comes back.
        with patch.object(nd, "_send_telegram_message", return_value=1) as send2:
            self.assertEqual(nd._process_pending_notifications(), 0)
        self.assertEqual(send2.call_count, 0)

    def test_backoff_grows_exponentially_and_caps(self):
        self.assertEqual(nd._tg_retry_delay_sec(1), 60)
        self.assertEqual(nd._tg_retry_delay_sec(2), 120)
        self.assertEqual(nd._tg_retry_delay_sec(3), 240)
        self.assertEqual(nd._tg_retry_delay_sec(100), 3600)


class TestDedupeKey(unittest.TestCase):
    def test_stable_for_same_reply(self):
        k1 = nd.build_tag_dedupe_key("reply_tagged", 5, 100, 7)
        k2 = nd.build_tag_dedupe_key("reply_tagged", 5, 100, 7)
        self.assertEqual(k1, k2)
        self.assertIsNotNone(k1)

    def test_distinct_across_replies_recipients_types(self):
        base = nd.build_tag_dedupe_key("reply_tagged", 5, 100, 7)
        self.assertNotEqual(base, nd.build_tag_dedupe_key("reply_tagged", 5, 100, 8))
        self.assertNotEqual(base, nd.build_tag_dedupe_key("reply_tagged", 6, 100, 7))
        self.assertNotEqual(base, nd.build_tag_dedupe_key("note_tagged", 5, 100, 7))
        self.assertNotEqual(base, nd.build_tag_dedupe_key("note_tagged", 5, 100))

    def test_rejects_junk(self):
        self.assertIsNone(nd.build_tag_dedupe_key("reply_tagged", 0, 100, 7))
        self.assertIsNone(nd.build_tag_dedupe_key("reply_tagged", 5, None, 7))
        self.assertIsNone(nd.build_tag_dedupe_key("task_assigned", 5, 100, 7))
        self.assertIsNone(nd.build_tag_dedupe_key("reply_tagged", "abc", 100, 7))

    def test_dedupe_notify_list(self):
        self.assertEqual(nd.dedupe_notify_list([5, 5, "7", 7, None, "x", 0], actor_id=7), [5])
        self.assertEqual(nd.dedupe_notify_list([3, 1, 3, 2]), [3, 1, 2])
        self.assertEqual(nd.dedupe_notify_list(None), [])


class TestCreationSqlGuards(unittest.TestCase):
    """Source-level guards: creation INSERTs must carry the dedupe key and
    ON CONFLICT, and build the recipient list through dedupe_notify_list."""

    def _server_src(self):
        return Path(HERE.parent, "server.py").read_text(encoding="utf-8")

    def test_inserts_carry_dedupe_key(self):
        src = self._server_src()
        self.assertEqual(src.count("ON CONFLICT (dedupe_key) DO NOTHING"), 3)
        self.assertIn("build_tag_dedupe_key(\"note_tagged\"", src)
        self.assertIn("build_tag_dedupe_key(\"reply_tagged\"", src)

    def test_notify_lists_deduped(self):
        src = self._server_src()
        self.assertIn("notify_list = dedupe_notify_list(payload.get(\"notifyUserList\"))", src)
        self.assertIn("notify_list = dedupe_notify_list(payload.get(\"notifyUserList\"), actor_id=actor_id)", src)


class TestTokenLeakGuards(unittest.TestCase):
    """BUG 2: httpx logs full request URLs (bot token included) at INFO.
    Both entry points must silence httpx/httpcore to WARNING."""

    def test_telegram_bot_silences_httpx(self):
        src = Path(HERE.parent, "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn('logging.getLogger("httpx").setLevel(logging.WARNING)', src)
        self.assertIn('logging.getLogger("httpcore").setLevel(logging.WARNING)', src)

    def test_server_silences_httpx(self):
        src = Path(HERE.parent, "server.py").read_text(encoding="utf-8")
        self.assertIn('logging.getLogger("httpx").setLevel(logging.WARNING)', src)
        self.assertIn('logging.getLogger("httpcore").setLevel(logging.WARNING)', src)

    def test_importing_bot_applies_levels(self):
        import logging as _logging
        telegram_stub, ext_stub = MagicMock(), MagicMock()
        sys.modules.setdefault("telegram", telegram_stub)
        sys.modules.setdefault("telegram.ext", ext_stub)
        if "telegram_bot" in sys.modules:
            del sys.modules["telegram_bot"]
        import telegram_bot  # noqa: F401
        self.assertEqual(_logging.getLogger("httpx").level, _logging.WARNING)
        self.assertEqual(_logging.getLogger("httpcore").level, _logging.WARNING)


if __name__ == "__main__":
    unittest.main(verbosity=1)
