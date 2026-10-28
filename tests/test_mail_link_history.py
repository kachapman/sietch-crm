#!/usr/bin/env python3
"""Unit tests for auto-linked mail history snapshots (2026-10-26).

Live gap: scanner auto-links by [#id]/content wrote ONLY mail_deal_links —
no history event, so linked mail never appeared in project timelines
(181/236 linked mails affected). Fix: shared scanner helper
create_email_history_snapshot, called on every real auto-link; the server's
manual path delegates to it; admin backfill endpoint repairs the backlog.

Run with: python3 tests/test_mail_link_history.py
"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

ADMIN = {"id": 7, "is_admin": True}


class Shim:
    class headers:
        @staticmethod
        def get(key, default=None):
            return default

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def __init__(self, path="/"):
        self.path = path

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    wfile = MagicMock()


def _mock_db(msg_row=None, link_exists=False, snapshot_exists=False):
    """snapshot_exists=True emulates a DB where the snapshot row is already
    present: the existence check returns a row AND the backfill query finds
    nothing missing (mirrors the real NOT EXISTS subquery)."""
    mock_db = MagicMock()
    writes = []

    seen_snapshots = set()

    def fake_query_one(sql, params=None, *a, **k):
        if "FROM history_events WHERE opportunity_id" in sql:
            # Stateful like the real SELECT: pre-existing rows are found, and
            # rows inserted earlier in the SAME run are found too. The check
            # passes (opp_id, LIKE-pattern); inserts record (opp_id,
            # message_id) — match on opp_id, which is unambiguous here
            # because each test run touches one opportunity per message.
            if snapshot_exists or (params and params[0] in seen_snapshots):
                return {"id": 50}
            return None
        if "FROM mail_messages WHERE id" in sql:
            return msg_row
        if "INSERT INTO mail_messages" in sql:
            return {"id": 7560}
        if "SELECT owner_user_id FROM mail_accounts" in sql:
            return None

    def fake_query_dicts(sql, params=None, *a, **k):
        if "FROM mail_deal_links l" in sql:
            return [] if snapshot_exists else [
                {"message_id": 7560, "opportunity_id": 67},
                {"message_id": 7561, "opportunity_id": 44},
            ]
        if "FROM mail_attachments" in sql:
            return [{"filename": "est.pdf", "size_bytes": 10,
                     "mime_type": "application/pdf"}]
        return []

    def _recording_execute(sql, params=None, *a, **k):
        writes.append((sql, params))
        if sql.startswith("INSERT INTO history_events") and params:
            seen_snapshots.add(params[0])

    mock_db.query_one.side_effect = fake_query_one
    mock_db.query_dicts.side_effect = fake_query_dicts
    mock_db.query.side_effect = lambda *a, **k: []
    mock_db.execute.side_effect = _recording_execute
    return mock_db, writes


MSG = {"subject": "Fw: estimate [#67]", "from_addr": "rob@x.com",
       "to_addr": "crm@y.com", "cc_addr": "", "body_html": "<p>hi</p>",
       "body_text": "hi", "date_received": "2026-10-26 12:00:00"}


class TestSharedSnapshotHelper(unittest.TestCase):
    def test_creates_snapshot_once(self):
        import scanner.mail_scanner as ms
        mock_db, writes = _mock_db(msg_row=dict(MSG))
        with patch.object(ms, "db", mock_db), patch.object(ms, "logger"):
            self.assertTrue(ms.create_email_history_snapshot(67, 7560))
        ins = [w for w in writes if w[0].startswith("INSERT INTO history_events")]
        self.assertEqual(len(ins), 1)
        snap = json.loads(ins[0][1][2])
        self.assertEqual(snap["type"], "email_snapshot")
        self.assertEqual(snap["message_id"], 7560)
        self.assertEqual(snap["subject"], "Fw: estimate [#67]")
        self.assertEqual(len(snap["attachments"]), 1)
        self.assertEqual(ins[0][1][1], 13)  # Email category

    def test_idempotent_second_call(self):
        # Same mock DB across both calls: the second check must find the row
        # the first call inserted (stateful, like the real SELECT).
        import scanner.mail_scanner as ms
        mock_db, writes = _mock_db(msg_row=dict(MSG))
        with patch.object(ms, "db", mock_db), patch.object(ms, "logger"):
            self.assertTrue(ms.create_email_history_snapshot(67, 7560))
            self.assertTrue(ms.create_email_history_snapshot(67, 7560))
        self.assertEqual(len([w for w in writes if w[0].startswith("INSERT INTO history_events")]), 1)

    def test_missing_message_returns_false(self):
        import scanner.mail_scanner as ms
        mock_db, writes = _mock_db(msg_row=None)
        with patch.object(ms, "db", mock_db), patch.object(ms, "logger"):
            self.assertFalse(ms.create_email_history_snapshot(67, 9999))
        self.assertEqual(writes, [])


class TestScannerAutoLinkCreatesHistory(unittest.TestCase):
    def test_project_id_autolink_creates_snapshot(self):
        import scanner.mail_scanner as ms
        mock_db, writes = _mock_db(msg_row=dict(MSG))
        inserts = writes
        with patch.object(ms, "db", mock_db), \
            patch.object(ms, "logger"), \
            patch.object(ms, "get_contractors",
                         lambda: {"scanner_behavior": {"auto_link_project_id": {"enabled": True}}}), \
            patch.object(ms, "_find_opportunity_by_deal_id",
                         lambda pid: {"id": 67, "title": "Robin Appel"}), \
            patch.object(ms, "_is_behavior_enabled", lambda *a, **k: True), \
            patch.object(ms, "_is_processed", lambda *a, **k: False), \
            patch.object(ms, "_is_dry_run", lambda *a, **k: False), \
            patch.object(ms, "_link_toggle_allows",
                         lambda *a, **k: (True, False)), \
            patch.object(ms, "_post_note_to_deal", lambda *a, **k: True), \
            patch.object(ms, "event_bus"):
            out = ms._process_message(
                {"uid": "1", "subject": "Fw: estimate [#67]", "from": "rob@x.com",
                 "to": "crm@y.com", "body_text": "hi", "body_html": ""},
                {"account_id": 1})
        self.assertTrue(out.get("deal_linked"))
        self.assertTrue(out.get("history_created"))
        snaps = [w for w in inserts if w[0].startswith("INSERT INTO history_events")]
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0][1][1], 13)  # the email_snapshot, not a note

    def test_dry_run_creates_nothing(self):
        import scanner.mail_scanner as ms
        mock_db, writes = _mock_db(msg_row=dict(MSG))
        inserts = writes
        with patch.object(ms, "db", mock_db), \
            patch.object(ms, "logger"), \
            patch.object(ms, "get_contractors",
                         lambda: {"scanner_behavior": {"auto_link_project_id": {"enabled": True}}}), \
            patch.object(ms, "_find_opportunity_by_deal_id",
                         lambda pid: {"id": 67, "title": "Robin Appel"}), \
            patch.object(ms, "_is_behavior_enabled", lambda *a, **k: True), \
            patch.object(ms, "_is_processed", lambda *a, **k: False), \
            patch.object(ms, "_is_dry_run", lambda *a, **k: True), \
            patch.object(ms, "_link_toggle_allows",
                         lambda *a, **k: (True, True)), \
            patch.object(ms, "event_bus"):
            out = ms._process_message(
                {"uid": "1", "subject": "Fw: estimate [#67]", "from": "rob@x.com",
                 "to": "crm@y.com", "body_text": "hi", "body_html": ""},
                {"account_id": 1})
        self.assertEqual([w for w in inserts if w[0].startswith("INSERT INTO history_events")], [])


class TestBackfillEndpoint(unittest.TestCase):
    def _call(self, snapshot_exists):
        import server as _server
        mock_db, writes = _mock_db(msg_row=dict(MSG), snapshot_exists=snapshot_exists)
        responses = []
        with patch.object(_server, "_require_admin", lambda h: ADMIN), \
            patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
            patch.object(_server, "db", mock_db), \
            patch.object(_server, "logger"):
            import scanner.mail_scanner as ms
            with patch.object(_server, "mail_scanner", ms):
                with patch.object(ms, "db", mock_db), patch.object(ms, "logger"):
                    _server.KanbanHandler._handle_sync_backfill_email_history(Shim())
        assert responses, "no response"
        return responses[0], writes

    def test_backfill_creates_missing(self):
        (status, body), writes = self._call(snapshot_exists=False)
        self.assertEqual(status, 200)
        self.assertEqual(body["missing"], 2)
        self.assertEqual(body["created"], 2)
        ins = [w for w in writes if w[0].startswith("INSERT INTO history_events")]
        self.assertEqual(len(ins), 2)

    def test_backfill_skips_existing(self):
        (status, body), writes = self._call(snapshot_exists=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["missing"], 0)
        self.assertEqual(body["created"], 0)


class TestServerDelegates(unittest.TestCase):
    def test_manual_path_uses_shared_helper(self):
        import server as _server
        import scanner.mail_scanner as ms
        calls = []
        mock_db, _ = _mock_db(msg_row=dict(MSG))
        with patch.object(ms, "create_email_history_snapshot",
                          side_effect=lambda o, m, u=None: calls.append((o, m)) or True):
            with patch.object(_server, "mail_scanner", ms):
                self.assertTrue(_server.KanbanHandler._create_email_history_event(
                    Shim(), 67, 7560, 7))
        self.assertEqual(calls, [(67, 7560)])


class TestUiWiring(unittest.TestCase):
    def test_backfill_button_and_handler(self):
        src = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        self.assertIn("sync-backfill-email-history-btn", src)
        self.assertIn("/api/v2/admin/sync/backfill-email-history", src)
        idx = open("/home/chapman-server/Projects/new-crm/public/index.html", encoding="utf-8").read()
        self.assertIn("sync-backfill-email-history-btn", idx)


if __name__ == "__main__":
    unittest.main(verbosity=1)
