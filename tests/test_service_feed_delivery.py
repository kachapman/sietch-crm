#!/usr/bin/env python3
"""Unit tests: service feed delivery, filtering and audiences (live 2026-10-29).

THREE LIVE DEFECTS, all verified on production
-----------------------------------------------
1. THE NOTIFICATIONS HAD NO OWNER.
   `_insert_feed_notification` looped over the target user ids but the
   INSERT omitted `user_id`, so every service_feed row was written with
   user_id = NULL. The drawer query filters "WHERE n.user_id = %s", so NOT
   ONE of them was ever visible to anyone — 1,438,873 unreadable rows
   costing 822MB and a month of pointless dispatcher UPDATE churn.
   The user asked: "why is there not a single proton notification in my
   notifications drawer?"

2. THE POLLER RE-DELIVERED EVERY ENTRY ON EVERY POLL.
   The dedup compared each entry id against ONE remembered id
   (service_feeds.last_item_id). The Proton feed has 25 entries with stable
   ids and only the newest ever matched, so the other 24 were re-delivered
   every 5 minutes to 8 users each: 192 rows per poll, ~2,300/hour,
   ~55k/day. Now every (feed_id, item_id) is recorded once.

3. FEEDS NOTIFIED EVERYBODY AND EVERY UPDATE.
   The default target_mode was 'all' and there was no way to choose which
   updates reach you — the Proton feed is mostly "Service Maintenance"
   entries. Now: per-feed keyword filter, audience modes
   (all / admins / specific / none) with 'admins' as the default, and a
   per-user opt-out.

Run with: python3 tests/test_service_feed_delivery.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

ROOT = Path('/home/chapman-server/Projects/new-crm')


class TestNotificationHasAnOwner(unittest.TestCase):
    """Defect 1 — the row must belong to the recipient."""

    def setUp(self):
        import server as _server
        self.server = _server

    def test_insert_writes_user_id(self):
        """The INSERT must include user_id, and pass the recipient id."""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        at = src.index("def _insert_feed_notification")
        region = src[at:at + 6000]
        self.assertIn("user_id", region,
                      "the notification row must record its recipient")
        self.assertIn("VALUES (%s, 'service_feed'", region,
                      "the INSERT must bind user_id")
        self.assertNotIn("(type, opportunity_id, actor_user_id, message, payload)",
                         region,
                         "the old INSERT omitted user_id entirely")

    def test_recipient_loop_writes_each_user(self):
        import server as _server
        mock_db = MagicMock()
        feed = {"id": 2, "name": "Proton Status", "target_mode": "all"}
        entry = {"id": "tag:x,2005:Incident/1", "title": "Partial outage",
                 "summary": "servers are down", "link": "https://x/1"}
        with patch.object(_server, "db", mock_db), \
             patch.object(_server, "_resolve_feed_target_user_ids",
                          lambda f: [3, 7]), \
             patch.object(_server, "_filter_feed_subscribers",
                          lambda fid, ids: ids):
            _server._insert_feed_notification(feed, entry)
        inserts = [c for c in mock_db.execute.call_args_list
                   if c.args and isinstance(c.args[0], str)
                   and "INSERT INTO notifications" in c.args[0]]
        self.assertEqual(len(inserts), 2, "one row per recipient")
        users = sorted(int(c.args[1][0]) for c in inserts)
        self.assertEqual(users, [3, 7],
                         "each row must carry its own recipient id")


class TestKeywordFilter(unittest.TestCase):
    """The Proton feed is mostly maintenance windows; the user wants outages."""

    def setUp(self):
        import server as _server
        self.server = _server

    def test_no_keywords_means_notify_everything(self):
        feed = {"id": 2, "notify_keywords": "[]"}
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Service Maintenance, October 22nd", ""))

    def test_keyword_matches_case_insensitively(self):
        feed = {"id": 2, "notify_keywords": '["outage", "disruption", "incident"]'}
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Partial outage on Proton VPN servers", ""))
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Global Outage", ""))
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Service disruption", ""))
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Investigating Incident", ""))

    def test_maintenance_is_filtered_out(self):
        feed = {"id": 2, "notify_keywords": '["outage", "disruption", "incident"]'}
        self.assertFalse(self.server._feed_entry_is_notifiable(
            feed, "Service Maintenance, October 22nd", ""))
        self.assertFalse(self.server._feed_entry_is_notifiable(
            feed, "Global ProtonVPN infrastructure upgrade campaign", ""))
        self.assertFalse(self.server._feed_entry_is_notifiable(
            feed, "Proton Calendar Issue", ""))

    def test_summary_is_also_searched(self):
        feed = {"id": 2, "notify_keywords": '["outage"]'}
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Status update", "There is an outage in Andorra"))

    def test_keywords_accept_a_plain_list(self):
        feed = {"id": 2, "notify_keywords": ["outage"]}
        self.assertTrue(self.server._feed_entry_is_notifiable(
            feed, "Server outage", ""))
        self.assertFalse(self.server._feed_entry_is_notifiable(
            feed, "Server outage", "") is False)  # sanity: returns a bool

    def test_normalize_keywords_from_comma_string(self):
        out = json.loads(self.server._normalize_notify_keywords("outage, disruption ,"))
        self.assertEqual(out, ["outage", "disruption"])

    def test_normalize_keywords_from_list(self):
        out = json.loads(self.server._normalize_notify_keywords(["a", "a", "b"]))
        self.assertEqual(out, ["a", "b"])

    def test_normalize_keywords_empty(self):
        self.assertEqual(json.loads(self.server._normalize_notify_keywords(None)), [])
        self.assertEqual(json.loads(self.server._normalize_notify_keywords("")), [])


class TestAudienceModes(unittest.TestCase):
    """A status feed must not notify every user by default."""

    def setUp(self):
        import server as _server
        self.server = _server

    def _users(self, rows):
        mock_db = MagicMock()
        mock_db.query.return_value = rows
        return mock_db

    def test_admins_mode_returns_only_admins(self):
        with patch.object(self.server, "db", self._users([(1,), (2,)])):
            got = self.server._resolve_feed_target_user_ids({"target_mode": "admins"})
        self.assertEqual(got, [1, 2])

    def test_none_mode_returns_nobody(self):
        with patch.object(self.server, "db", self._users([(1,), (2,)])):
            got = self.server._resolve_feed_target_user_ids({"target_mode": "none"})
        self.assertEqual(got, [])

    def test_all_mode_returns_every_active_user(self):
        with patch.object(self.server, "db", self._users([(1,), (2,), (3,)])):
            got = self.server._resolve_feed_target_user_ids({"target_mode": "all"})
        self.assertEqual(got, [1, 2, 3])

    def test_specific_mode_returns_only_listed_users(self):
        with patch.object(self.server, "db", self._users([(1,), (2,)])):
            got = self.server._resolve_feed_target_user_ids(
                {"target_mode": "specific", "target_user_ids": [2]})
        self.assertEqual(got, [2])

    def test_admins_query_filters_on_is_admin(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("is_active = TRUE AND is_admin = TRUE", src)

    def test_new_feed_default_is_admins_not_all(self):
        """The user asked: a feed should not automatically notify every user."""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        at = src.index("def _handle_service_feed_create")
        region = src[at:at + 3000]
        self.assertIn('data.get("target_mode", "admins")', region,
                      "new feeds must default to admins, not all users")

    def test_schema_default_is_admins(self):
        init = (ROOT / "init.sql").read_text(encoding="utf-8")
        self.assertIn("target_mode         TEXT NOT NULL DEFAULT 'admins'", init)


class TestPerUserOptOut(unittest.TestCase):
    def setUp(self):
        import server as _server
        self.server = _server

    def test_muted_users_are_dropped(self):
        mock_db = MagicMock()
        mock_db.query.return_value = [(7,)]
        with patch.object(self.server, "db", mock_db):
            got = self.server._filter_feed_subscribers(2, [3, 7, 9])
        self.assertEqual(got, [3, 9])

    def test_no_mutes_keeps_everyone(self):
        mock_db = MagicMock()
        mock_db.query.return_value = []
        with patch.object(self.server, "db", mock_db):
            got = self.server._filter_feed_subscribers(2, [3, 7])
        self.assertEqual(got, [3, 7])

    def test_audience_check_for_a_user(self):
        admins_feed = {"target_mode": "admins"}
        self.assertTrue(self.server._user_is_feed_audience(
            admins_feed, {"id": 1, "is_admin": True}))
        self.assertFalse(self.server._user_is_feed_audience(
            admins_feed, {"id": 2, "is_admin": False}))

    def test_audience_check_specific(self):
        feed = {"target_mode": "specific", "target_user_ids": [5]}
        self.assertTrue(self.server._user_is_feed_audience(feed, {"id": 5}))
        self.assertFalse(self.server._user_is_feed_audience(feed, {"id": 6}))

    def test_audience_check_none(self):
        self.assertFalse(self.server._user_is_feed_audience(
            {"target_mode": "none"}, {"id": 1, "is_admin": True}))


class TestPollerDedup(unittest.TestCase):
    """Defect 2 — each feed entry is delivered exactly once."""

    def test_poller_uses_the_ledger_not_a_single_id(self):
        src = (ROOT / "service_feed_poller.py").read_text(encoding="utf-8")
        self.assertIn("service_feed_items", src,
                      "the poller must record delivered entry ids")
        self.assertIn("ON CONFLICT (feed_id, item_id) DO NOTHING", src)
        # The old single-id comparison must be gone.
        at = src.index("new_entries: list[dict] = []")
        region = src[at:at + 1200]
        self.assertNotIn("if eid == last_id:", region,
                         "the single-id dedup is what re-delivered 24/25 entries")

    def test_ledger_table_exists_in_schema(self):
        init = (ROOT / "init.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS service_feed_items", init)

    def test_migration_creates_the_tables(self):
        mig = ROOT / "migrations" / "040_service_feed_delivery_fixes.sql"
        self.assertTrue(mig.exists(), "migration 040 missing")
        sql = mig.read_text(encoding="utf-8")
        self.assertIn("service_feed_items", sql)
        self.assertIn("service_feed_subscriptions", sql)
        self.assertIn("notify_keywords", sql)
        self.assertIn("DELETE FROM notifications", sql,
                      "the orphaned rows must be removed")


class TestDispatcherIgnoresOrphans(unittest.TestCase):
    def test_dispatcher_skips_rows_without_a_user(self):
        src = (ROOT / "notification_dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("n.user_id IS NOT NULL", src,
                      "orphaned notifications must not enter the delivery queue")


class TestSourceGuards(unittest.TestCase):
    def test_admin_form_has_the_new_audience_options(self):
        idx = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn('value="admins"', idx)
        self.assertIn('value="none"', idx)
        self.assertIn("sf-edit-notify-keywords", idx)

    def test_profile_has_a_status_alerts_section(self):
        idx = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn("pref-service-feeds", idx)

    def test_frontend_posts_the_keyword_filter(self):
        app = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn("notify_keywords", app)
        self.assertIn("_profileLoadFeedSubs", app)
        self.assertIn("/api/v2/me/service-feed-subscriptions", app)


if __name__ == "__main__":
    unittest.main(verbosity=1)
