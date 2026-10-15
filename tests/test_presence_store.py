"""Unit tests for presence_store (the persistence layer for status,
heartbeats, and DMs).

Covers:
  - _safe_segment: collision semantics for the file naming scheme
  - load/save round-trip: every field round-trips through JSON
  - touch_heartbeat: sets lastHeartbeat; visible=True also bumps
    lastDashboardActivity; offline=True clears lastHeartbeat
  - set_status: manual status, autoStatus, lastDashboardActivity bump
  - clean_stale_presence_records: clears heartbeat + autoStatus for
    records older than 3h, leaves newer ones alone
  - get_portal_presence_snapshot: returns all user presences
  - append_dm: appends a message, round-trips through get_conversation
  - mark_messages_read: flips per-message read flag + read_at
  - clear_conversation: removes the file, clears last-read marker
  - set_last_read_dm: per-other-user last-read timestamp
  - get_conversation pagination: offset=N skips the N newest messages
  - get_recent_dms_for_user: returns one latest per conversation partner

The disk layer is redirected to a tmpdir via monkey-patching
DATA_DIR and MESSAGES_DIR at import time so the test doesn't touch
the real data/ tree.

Run with: python3 tests/test_presence_store.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add the project root to the import path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Create a tmpdir for the test and patch presence_store.DATA_DIR
# and presence_store.MESSAGES_DIR BEFORE we import it, so the module's
# module-level path constants point at the tmpdir.
_TMP = tempfile.mkdtemp(prefix="presence_store_test_")
_DATA_DIR = Path(_TMP) / "user-presence"
_MSG_DIR = Path(_TMP) / "presence-messages"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_MSG_DIR.mkdir(parents=True, exist_ok=True)

# Pre-create the test data so the import works (presence_file_path
# and conversation_file_path join on the module-level DATA_DIR/MESSAGES_DIR).
import presence_store  # noqa: E402
presence_store.DATA_DIR = _DATA_DIR
presence_store.MESSAGES_DIR = _MSG_DIR


class TestSafeSegment(unittest.TestCase):
    """The _safe_segment function determines the file name for a user
    or conversation. If two distinct IDs segment to the same string,
    their presence / conversation files collide on disk."""

    def test_whitespace_replaced_with_underscore(self):
        self.assertEqual(presence_store._safe_segment("abc xyz"), "abc_xyz")
        self.assertEqual(presence_store._safe_segment("abc\txyz"), "abc_xyz")

    def test_dots_and_hyphens_kept(self):
        # Dots and hyphens are explicitly allowed (GUIDs, emails).
        self.assertEqual(presence_store._safe_segment("abc-123"), "abc-123")
        self.assertEqual(presence_store._safe_segment("abc.123"), "abc.123")

    def test_special_chars_replaced(self):
        self.assertEqual(presence_store._safe_segment("a/b\\c|d"), "a_b_c_d")

    def test_unicode_kept(self):
        # \w in Python 3 re defaults to Unicode for str patterns; the
        # non-ASCII letters pass through untouched.
        self.assertEqual(presence_store._safe_segment("café"), "café")

    def test_empty_string_falls_back(self):
        self.assertEqual(presence_store._safe_segment(""), "unknown")
        self.assertEqual(presence_store._safe_segment(None), "unknown")
        self.assertEqual(presence_store._safe_segment("   "), "unknown")

    def test_length_cap_at_120(self):
        # > 120 chars gets truncated.
        long_id = "a" * 200
        self.assertEqual(len(presence_store._safe_segment(long_id)), 120)


class TestPresenceRoundTrip(unittest.TestCase):
    """load + save should preserve every field the schema defines."""

    def test_full_round_trip(self):
        uid = "42"
        rec = presence_store.save_user_presence("sietch", uid, {
            "status": "Working on: Deal X",
            "inferred": True,
            "autoStatus": "Reviewing: Project Y",
            "lastHeartbeat": "2026-10-15T10:00:00+00:00",
            "lastCrmActivity": "2026-10-15T10:01:00+00:00",
            "lastDashboardActivity": "2026-10-15T10:02:00+00:00",
            "lastReadDms": {"7": "2026-10-15T09:00:00+00:00", "9": "2026-10-15T09:30:00+00:00"},
        })
        loaded = presence_store.load_user_presence("sietch", uid)
        self.assertEqual(loaded["status"], "Working on: Deal X")
        self.assertEqual(loaded["autoStatus"], "Reviewing: Project Y")
        self.assertEqual(loaded["inferred"], True)
        self.assertEqual(loaded["lastHeartbeat"], "2026-10-15T10:00:00+00:00")
        self.assertEqual(loaded["lastCrmActivity"], "2026-10-15T10:01:00+00:00")
        self.assertEqual(loaded["lastDashboardActivity"], "2026-10-15T10:02:00+00:00")
        self.assertEqual(loaded["lastReadDms"], {
            "7": "2026-10-15T09:00:00+00:00",
            "9": "2026-10-15T09:30:00+00:00",
        })
        # updatedAt is auto-stamped if not supplied.
        self.assertTrue(loaded["updatedAt"])

    def test_load_returns_empty_for_unknown_user(self):
        loaded = presence_store.load_user_presence("sietch", "9999")
        self.assertEqual(loaded["status"], "")
        self.assertEqual(loaded["inferred"], False)
        self.assertEqual(loaded["autoStatus"], "")
        self.assertEqual(loaded["lastHeartbeat"], "")
        self.assertEqual(loaded["lastReadDms"], {})

    def test_status_field_capped(self):
        # 5000-char status should be truncated to 200 chars on save.
        rec = presence_store.save_user_presence("sietch", "50", {
            "status": "x" * 5000,
        })
        self.assertEqual(len(rec["status"]), 200)

    def test_corrupt_file_returns_empty(self):
        # A corrupt JSON file should not crash the loader; just return
        # the empty default.
        path = presence_store.presence_file_path("sietch", "corrupt_user")
        path.write_text("not valid json {{{", encoding="utf-8")
        loaded = presence_store.load_user_presence("sietch", "corrupt_user")
        self.assertEqual(loaded["status"], "")
        self.assertEqual(loaded["inferred"], False)


class TestTouchHeartbeat(unittest.TestCase):
    def test_basic_heartbeat_sets_lastHeartbeat(self):
        presence_store.touch_heartbeat("sietch", "60", offline=False, visible=False)
        rec = presence_store.load_user_presence("sietch", "60")
        self.assertTrue(rec["lastHeartbeat"])
        # visible=False does NOT bump lastDashboardActivity
        self.assertEqual(rec["lastDashboardActivity"], "")

    def test_visible_heartbeat_bumps_dashboard(self):
        presence_store.touch_heartbeat("sietch", "61", offline=False, visible=True)
        rec = presence_store.load_user_presence("sietch", "61")
        self.assertTrue(rec["lastHeartbeat"])
        self.assertTrue(rec["lastDashboardActivity"])

    def test_offline_heartbeat_clears(self):
        # First, set a heartbeat.
        presence_store.touch_heartbeat("sietch", "62", offline=False, visible=True)
        # Then mark offline.
        presence_store.touch_heartbeat("sietch", "62", offline=True, visible=False)
        rec = presence_store.load_user_presence("sietch", "62")
        self.assertEqual(rec["lastHeartbeat"], "")
        # lastDashboardActivity is NOT cleared on offline (audit: the
        # 'last seen' badge still works for users who logged out)
        self.assertTrue(rec["lastDashboardActivity"])


class TestSetStatus(unittest.TestCase):
    def test_status_text_set_with_inferred(self):
        rec = presence_store.set_status("sietch", "70", status="AFK - Lunch", inferred=False)
        self.assertEqual(rec["status"], "AFK - Lunch")
        self.assertEqual(rec["inferred"], False)
        self.assertTrue(rec["lastDashboardActivity"])

    def test_status_None_leaves_existing(self):
        presence_store.set_status("sietch", "71", status="Working", inferred=False)
        rec = presence_store.set_status("sietch", "71", status=None, autoStatus="Inferred Auto")
        # Manual status untouched when status=None
        self.assertEqual(rec["status"], "Working")
        # autoStatus updated
        self.assertEqual(rec["autoStatus"], "Inferred Auto")

    def test_empty_status_clears(self):
        presence_store.set_status("sietch", "72", status="AFK", inferred=False)
        rec = presence_store.set_status("sietch", "72", status="", inferred=False)
        self.assertEqual(rec["status"], "")


class TestCleanStalePresence(unittest.TestCase):
    def test_clears_old_heartbeat_and_autoStatus(self):
        # Write a presence record with a 4-hour-old heartbeat.
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        presence_store.save_user_presence("sietch", "80", {
            "status": "manual",
            "inferred": False,
            "autoStatus": "stale auto",
            "lastHeartbeat": old_ts,
        })
        presence_store.clean_stale_presence_records("sietch")
        rec = presence_store.load_user_presence("sietch", "80")
        # Heartbeat cleared (older than 3h threshold)
        self.assertEqual(rec["lastHeartbeat"], "")
        # autoStatus also cleared
        self.assertEqual(rec["autoStatus"], "")
        # Manual status NOT touched
        self.assertEqual(rec["status"], "manual")

    def test_keeps_recent_heartbeat(self):
        # Write a presence record with a 1-hour-old heartbeat.
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        presence_store.save_user_presence("sietch", "81", {
            "status": "manual",
            "autoStatus": "fresh",
            "lastHeartbeat": recent_ts,
        })
        presence_store.clean_stale_presence_records("sietch")
        rec = presence_store.load_user_presence("sietch", "81")
        # Recent heartbeat is preserved.
        self.assertTrue(rec["lastHeartbeat"])
        self.assertEqual(rec["autoStatus"], "fresh")


class TestGetPortalPresenceSnapshot(unittest.TestCase):
    def test_returns_all_users_for_portal(self):
        presence_store.save_user_presence("sietch", "90", {"status": "alpha"})
        presence_store.save_user_presence("sietch", "91", {"status": "beta"})
        snap = presence_store.get_portal_presence_snapshot("sietch")
        self.assertIn("90", snap)
        self.assertIn("91", snap)
        self.assertEqual(snap["90"]["status"], "alpha")
        self.assertEqual(snap["91"]["status"], "beta")

    def test_isolates_by_portal(self):
        presence_store.save_user_presence("sietch", "92", {"status": "sietch_user"})
        presence_store.save_user_presence("other_portal", "92", {"status": "other_user"})
        snap_a = presence_store.get_portal_presence_snapshot("sietch")
        snap_b = presence_store.get_portal_presence_snapshot("other_portal")
        self.assertEqual(snap_a["92"]["status"], "sietch_user")
        self.assertEqual(snap_b["92"]["status"], "other_user")


class TestConversationRoundTrip(unittest.TestCase):
    def test_append_and_get_round_trip(self):
        msg = presence_store.append_dm("sietch", "100", "200", "hello world")
        self.assertEqual(msg["from"], "100")
        self.assertEqual(msg["to"], "200")
        self.assertEqual(msg["text"], "hello world")
        self.assertTrue(msg["ts"])
        self.assertFalse(msg["read"])
        # Fetch from either direction.
        msgs_a, has_more = presence_store.get_conversation("sietch", "100", "200")
        msgs_b, _ = presence_store.get_conversation("sietch", "200", "100")
        self.assertEqual(len(msgs_a), 1)
        self.assertEqual(len(msgs_b), 1)
        self.assertEqual(msgs_a[0]["text"], "hello world")
        self.assertEqual(msgs_b[0]["text"], "hello world")
        self.assertFalse(has_more)

    def test_append_with_reply_resolves_text(self):
        # The original message.
        m1 = presence_store.append_dm("sietch", "110", "210", "first message")
        # Reply without reply_text — the store should resolve the
        # snippet from the recent history.
        m2 = presence_store.append_dm("sietch", "210", "110", "responding", reply_to=m1["ts"])
        self.assertEqual(m2["reply_to"], m1["ts"])
        self.assertEqual(m2["reply_text"], "first message")

    def test_caps_at_history_limit(self):
        # Append DM_HISTORY_LIMIT + 5 messages; the store should trim
        # the file to the most recent DM_HISTORY_LIMIT, dropping the
        # oldest 5 entirely (not paginating them).
        for i in range(presence_store.DM_HISTORY_LIMIT + 5):
            presence_store.append_dm("sietch", "120", "220", f"msg {i}")
        msgs, has_more = presence_store.get_conversation("sietch", "120", "220")
        # Total file size is bounded by the cap.
        self.assertLessEqual(len(msgs), presence_store.DM_HISTORY_LIMIT)
        # Most recent is preserved.
        self.assertEqual(msgs[-1]["text"], f"msg {presence_store.DM_HISTORY_LIMIT + 4}")
        # The default limit is DM_HISTORY_LIMIT, so the entire file
        # fits in one page. has_more is False (we have everything).
        self.assertFalse(has_more)
        # Pagination against the truncated file: asking for the page
        # BEFORE the truncation window returns nothing (the file no
        # longer has those messages).
        msgs_older, has_more_older = presence_store.get_conversation(
            "sietch", "120", "220", offset=presence_store.DM_HISTORY_LIMIT
        )
        self.assertEqual(msgs_older, [])

    def test_text_capped_at_2000(self):
        long_text = "x" * 5000
        msg = presence_store.append_dm("sietch", "130", "230", long_text)
        self.assertEqual(len(msg["text"]), 2000)

    def test_self_dm_rejected(self):
        # from_id == to_id should return {} and not persist anything.
        msg = presence_store.append_dm("sietch", "140", "140", "talking to myself")
        self.assertEqual(msg, {})
        msgs, _ = presence_store.get_conversation("sietch", "140", "140")
        self.assertEqual(msgs, [])


class TestMarkMessagesRead(unittest.TestCase):
    def test_flips_incoming_read_flag(self):
        # A sends 3 to B.
        for i in range(3):
            presence_store.append_dm("sietch", "150", "250", f"msg {i}")
        # B marks as read.
        presence_store.mark_messages_read("sietch", "250", "150")
        msgs, _ = presence_store.get_conversation("sietch", "250", "150")
        for m in msgs:
            self.assertTrue(m["read"], f"message {m['ts']} should be marked read")
            self.assertTrue(m["read_at"])

    def test_idempotent(self):
        presence_store.append_dm("sietch", "151", "251", "once")
        presence_store.mark_messages_read("sietch", "251", "151")
        first = presence_store.load_user_presence("sietch", "251")
        # Second call shouldn't clobber read_at or crash.
        presence_store.mark_messages_read("sietch", "251", "151")
        second = presence_store.load_user_presence("sietch", "251")
        # read_at was set on the message, not on the user; both calls
        # should leave the message file in the same state.
        msgs, _ = presence_store.get_conversation("sietch", "251", "151")
        self.assertTrue(msgs[0]["read"])


class TestClearConversation(unittest.TestCase):
    def test_clears_messages_and_last_read(self):
        # Two users exchange a few messages; B reads; then clear.
        for i in range(3):
            presence_store.append_dm("sietch", "160", "260", f"msg {i}")
        presence_store.set_last_read_dm("sietch", "260", "160")
        # Confirm pre-clear state.
        pre = presence_store.load_user_presence("sietch", "260")
        self.assertIn("160", pre["lastReadDms"])
        # Clear.
        presence_store.clear_conversation("sietch", "260", "160")
        # Conversation file gone.
        msgs, _ = presence_store.get_conversation("sietch", "260", "160")
        self.assertEqual(msgs, [])
        # lastReadDms for that peer also cleared.
        post = presence_store.load_user_presence("sietch", "260")
        self.assertNotIn("160", post["lastReadDms"])


class TestSetLastReadDm(unittest.TestCase):
    def test_basic_set_and_load(self):
        presence_store.set_last_read_dm("sietch", "170", "270")
        d = presence_store.load_user_last_read_dms("sietch", "170")
        self.assertIn("270", d)
        self.assertTrue(d["270"])

    def test_idempotent_overwrite(self):
        presence_store.set_last_read_dm("sietch", "171", "271")
        first = presence_store.load_user_last_read_dms("sietch", "171")["271"]
        # Second set should overwrite, not append.
        presence_store.set_last_read_dm("sietch", "171", "271")
        second = presence_store.load_user_last_read_dms("sietch", "171")["271"]
        # Both should be valid ISO strings. Second is later-or-equal.
        self.assertGreaterEqual(second, first)

    def test_per_user_isolation(self):
        presence_store.set_last_read_dm("sietch", "172", "272")
        d_172 = presence_store.load_user_last_read_dms("sietch", "172")
        d_173 = presence_store.load_user_last_read_dms("sietch", "173")
        self.assertIn("272", d_172)
        self.assertNotIn("272", d_173)


class TestGetConversationPagination(unittest.TestCase):
    """Audit round 6 part 2: the GET must respect offset so Load-More
    actually returns older pages."""

    def test_offset_zero_returns_most_recent(self):
        for i in range(5):
            presence_store.append_dm("sietch", "180", "280", f"msg {i}")
        msgs, has_more = presence_store.get_conversation("sietch", "180", "280", offset=0)
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[-1]["text"], "msg 4")  # most recent
        self.assertFalse(has_more)

    def test_offset_three_skips_three_newest(self):
        for i in range(5):
            presence_store.append_dm("sietch", "181", "281", f"msg {i}")
        msgs, has_more = presence_store.get_conversation("sietch", "181", "281", limit=10, offset=3)
        # Returns the 2 oldest messages (5 total - 3 skip = 2 left)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["text"], "msg 0")
        self.assertEqual(msgs[1]["text"], "msg 1")
        self.assertFalse(has_more)

    def test_offset_with_limit_paginates(self):
        for i in range(10):
            presence_store.append_dm("sietch", "182", "282", f"msg {i}")
        # Page 1: offset=0, limit=4 → msgs 6,7,8,9
        page1, has_more_1 = presence_store.get_conversation("sietch", "182", "282", limit=4, offset=0)
        self.assertEqual([m["text"] for m in page1], ["msg 6", "msg 7", "msg 8", "msg 9"])
        self.assertTrue(has_more_1)
        # Page 2: offset=4, limit=4 → msgs 2,3,4,5
        page2, has_more_2 = presence_store.get_conversation("sietch", "182", "282", limit=4, offset=4)
        self.assertEqual([m["text"] for m in page2], ["msg 2", "msg 3", "msg 4", "msg 5"])
        self.assertTrue(has_more_2)
        # Page 3: offset=8, limit=4 → msgs 0,1
        page3, has_more_3 = presence_store.get_conversation("sietch", "182", "282", limit=4, offset=8)
        self.assertEqual([m["text"] for m in page3], ["msg 0", "msg 1"])
        self.assertFalse(has_more_3)
        # Page 4: offset=10 → empty
        page4, has_more_4 = presence_store.get_conversation("sietch", "182", "282", limit=4, offset=10)
        self.assertEqual(page4, [])
        self.assertFalse(has_more_4)


class TestGetRecentDmsForUser(unittest.TestCase):
    def test_returns_latest_per_conversation_partner(self):
        # A <-> B: 3 messages.
        a, b, c, d = "a_partner", "b_partner", "c_partner", "d_partner"
        for i in range(3):
            presence_store.append_dm("sietch", a, b, f"ab-{i}")
        # A <-> C: 1 message.
        presence_store.append_dm("sietch", a, c, "ac")
        # C <-> D: 1 message (involves C but not A or B — should not
        # appear in A's or B's recent feed).
        presence_store.append_dm("sietch", c, d, "cd")
        # B's recent feed: only the AB thread (B never talked to C or D).
        recents = presence_store.get_recent_dms_for_user("sietch", b)
        others = set()
        for r in recents:
            others.add(str(r["from"]) if str(r["from"]) != b else str(r["to"]))
        self.assertEqual(others, {a})  # only A; not C or D
        # The latest AB message is the most recent of the 3.
        ab_msgs = [r for r in recents if (str(r["from"]) == a or str(r["to"]) == a)]
        self.assertEqual(len(ab_msgs), 1)  # one per partner
        self.assertEqual(ab_msgs[0]["text"], "ab-2")
        self.assertEqual(len(recents), 1)

    def test_includes_outgoing_messages(self):
        # A sends 2 to B. From A's perspective, the recent feed
        # INCLUDES the conversation (showing the sent messages too) —
        # this is by design: the inbox is a recent activity feed,
        # not an inbox-of-incoming. The frontend then filters
        # "unread" using the per-user lastReadDms marker.
        # Use unique user IDs so the test is isolated from other tests
        # that run in the same module (the disk layer is shared).
        import time
        a, b = "a_outgoing", "b_outgoing"
        for i in range(2):
            presence_store.append_dm("sietch", a, b, f"sent {i}")
            time.sleep(0.002)  # ensure distinct microsecond timestamps
        recents = presence_store.get_recent_dms_for_user("sietch", a)
        self.assertEqual(len(recents), 1)
        self.assertEqual(recents[0]["to"], b)
        self.assertEqual(recents[0]["text"], "sent 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
