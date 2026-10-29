#!/usr/bin/env python3
"""Unit tests: relink orphaned email_snapshot timeline events (2026-10-29).

Deleting the Aplus/MDM mail accounts cascaded their mail_messages rows away
(ON DELETE CASCADE), but project timeline events survived (history_events has
no FK to mail_messages). 35 email_snapshot events now point at message ids
that no longer exist. Their bodies are intact inline; only the pointer
(content.message_id) and the mail_deal_links rows died.

After the mailboxes re-sync, each message returns with a NEW id but an
IDENTICAL (from_addr, subject, date_received) — the snapshot's date_sent is
the IMAP INTERNALDATE, which does not change on re-import. The relink
matches exactly and repoints; one old message linked to two projects (id
3856 -> opps 35 and 46) is repointed on BOTH events to the same new id.

Run with: python3 tests/test_relink_orphan_snapshots.py
"""
import json
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

ADMIN = {"id": 7, "is_admin": True}


class Shim:
    def __init__(self, path="/"):
        self.path = path

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


def _snap(mid, frm="a@x.com", subj="s", date="2026-10-28 21:51:58.227083"):
    return json.dumps({
        "type": "email_snapshot", "message_id": mid, "from": frm,
        "to": "t@y.com", "cc": "", "subject": subj, "date_sent": date,
        "body_html": "<p>hi</p>", "body_text": "hi", "attachments": [],
    })


class TestMatchRule(unittest.TestCase):
    def setUp(self):
        import server as _server
        self.h = _server.KanbanHandler

    def test_exact_timestamp_match_wins(self):
        cands = [{"id": 9001, "date_received": datetime(2026, 10, 28, 21, 51, 58, 227083)}]
        o = {"from": "a@x.com", "subject": "s", "date_sent": "2026-10-28 21:51:58.227083"}
        self.assertEqual(self.h._match_relinked_message(o, cands)["id"], 9001)

    def test_close_within_60s_matches_single(self):
        cands = [{"id": 9002, "date_received": datetime(2026, 10, 28, 21, 51, 59)}]
        o = {"from": "a@x.com", "subject": "s", "date_sent": "2026-10-28 21:51:58.227083"}
        self.assertEqual(self.h._match_relinked_message(o, cands)["id"], 9002)

    def test_two_close_candidates_is_ambiguous(self):
        # Two messages with byte-identical timestamps: exact rule matches
        # both, so the pair is ambiguous and must NOT auto-relink.
        cands = [{"id": 1, "date_received": datetime(2026, 10, 28, 21, 51, 58)},
                 {"id": 2, "date_received": datetime(2026, 10, 28, 21, 51, 58)}]
        o = {"from": "a@x.com", "subject": "s", "date_sent": "2026-10-28 21:51:58"}
        self.assertIsNone(self.h._match_relinked_message(o, cands))

    def test_no_candidates_is_unmatched(self):
        o = {"from": "a@x.com", "subject": "s", "date_sent": "2026-10-28 21:51:58"}
        self.assertIsNone(self.h._match_relinked_message(o, []))

    def test_far_timestamp_is_unmatched(self):
        cands = [{"id": 5, "date_received": datetime(2026, 10, 29, 12, 0, 0)}]
        o = {"from": "a@x.com", "subject": "s", "date_sent": "2026-10-28 21:51:58"}
        self.assertIsNone(self.h._match_relinked_message(o, cands))

    def test_dual_project_same_old_id_matches_same_new_id(self):
        """Old id 3856 was linked to opps 35 AND 46 — both repoint together."""
        cands = [{"id": 7777, "date_received": datetime(2026, 10, 22, 1, 29, 25)}]
        o1 = {"from": "s@x.com", "subject": "t", "date_sent": "2026-10-22 01:29:25"}
        o2 = dict(o1)
        m1 = self.h._match_relinked_message(o1, cands)
        m2 = self.h._match_relinked_message(o2, cands)
        self.assertEqual(m1["id"], 7777)
        self.assertEqual(m2["id"], 7777)


class TestApplyRepoint(unittest.TestCase):
    def test_rewrites_pointer_recreates_link_and_skips_copy_when_files_exist(self):
        import server as _server
        mock_db = MagicMock()
        old = _snap(1545, frm="e@a.com", subj="Re: 402385322",
                    date="2026-10-19 14:26:50")
        writes = []

        def fake_one(sql, params=None, *a, **k):
            if "FROM history_events WHERE id" in sql:
                return {"id": 48833, "opportunity_id": 44, "content": old}
            if "FROM mail_messages WHERE id" in sql:
                return {"id": 9100}
            if "FROM opportunities WHERE id" in sql:
                return {"id": 44}
            if "FROM history_attachments WHERE event_id" in sql:
                return {"id": 1}  # project copy already exists -> skip copy
            return None

        mock_db.query_one.side_effect = fake_one
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
        with patch.object(_server, "db", mock_db), \
             patch.object(_server, "logger"):
            ok = _server.KanbanHandler._apply_snapshot_relink({"event_id": 48833, "opportunity_id": 44,
                          "old_message_id": 1545, "new_message_id": 9100})
        self.assertTrue(ok)
        upd = [w for w in writes if w[0].startswith("UPDATE history_events SET content")]
        self.assertEqual(len(upd), 1)
        self.assertEqual(json.loads(upd[0][1][0])["message_id"], 9100)
        # Body and identity preserved — only the pointer changes.
        self.assertEqual(json.loads(upd[0][1][0])["subject"], "Re: 402385322")
        link = [w for w in writes if w[0].startswith("INSERT INTO mail_deal_links")]
        self.assertEqual(len(link), 1)
        self.assertIn("ON CONFLICT DO NOTHING", link[0][0])
        self.assertEqual(link[0][1][:2], (9100, 44))

    def test_backfills_project_copy_only_when_missing(self):
        import server as _server
        import scanner.mail_scanner as ms
        mock_db = MagicMock()
        old = _snap(1648, frm="w@e.com", subj="A00007611973",
                    date="2026-10-20 01:08:32")
        writes = []

        def fake_one(sql, params=None, *a, **k):
            if "FROM history_events WHERE id" in sql:
                return {"id": 49399, "opportunity_id": 43, "content": old}
            if "FROM mail_messages WHERE id" in sql:
                return {"id": 9200}
            if "FROM opportunities WHERE id" in sql:
                return {"id": 43}
            if "FROM history_attachments WHERE event_id" in sql:
                return None  # no project copy yet -> must attempt one
            return None

        mock_db.query_one.side_effect = fake_one
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append((sql, params))
        calls = []
        with patch.object(_server, "db", mock_db), \
             patch.object(_server, "logger"), \
             patch.object(_server, "mail_scanner", ms), \
             patch.object(ms, "_copy_mail_attachments_to_project",
                          side_effect=lambda o, e, m: calls.append((o, e, m)) or 2):
            ok = _server.KanbanHandler._apply_snapshot_relink({"event_id": 49399, "opportunity_id": 43,
                          "old_message_id": 1648, "new_message_id": 9200})
        self.assertTrue(ok)
        self.assertEqual(calls, [(43, 49399, 9200)])

    def test_refuses_when_new_message_is_gone(self):
        import server as _server
        mock_db = MagicMock()
        mock_db.query_one.side_effect = lambda sql, params=None, *a, **k: (
            {"id": 1, "opportunity_id": 44, "content": _snap(9)}
            if "history_events" in sql else None)
        with patch.object(_server, "db", mock_db), \
             patch.object(_server, "logger"):
            self.assertFalse(_server.KanbanHandler._apply_snapshot_relink({"event_id": 1, "opportunity_id": 44,
                          "old_message_id": 9, "new_message_id": 4242}))


class TestDryRunWritesNothing(unittest.TestCase):
    def _run(self, dry_run):
        import server as _server
        mock_db = MagicMock()
        writes = []
        rows = [(48833, 44, _snap(1545, frm="e@a.com", subj="Re: 402385322",
                                   date="2026-10-19 14:26:50"))]
        by_event = {e: (o, c) for e, o, c in rows}
        mock_db.query.return_value = rows
        mock_db.query_dicts.side_effect = lambda sql, params=None, *a, **k: (
            [{"id": 9100, "account_id": 5, "from_addr": "e@a.com",
              "subject": "Re: 402385322",
              "date_received": datetime(2026, 10, 19, 14, 26, 50),
              "folder": "INBOX"}]
            if "FROM mail_messages WHERE from_addr" in sql else [])

        def fake_one(sql, params=None, *a, **k):
            if "FROM history_events WHERE id" in sql:
                eid = params[0] if params else None
                if eid in by_event:
                    return {"id": eid, "opportunity_id": by_event[eid][0],
                            "content": by_event[eid][1]}
                return None
            if "FROM mail_messages WHERE id" in sql:
                return {"id": params[0]} if params else None
            if "FROM opportunities" in sql:
                return {"id": 44, "title": "Opp 44"}
            if "FROM history_attachments WHERE event_id" in sql:
                return {"id": 1}  # project copy already exists -> skip copy
            return None

        mock_db.query_one.side_effect = fake_one
        mock_db.execute.side_effect = lambda sql, params=None, *a, **k: writes.append(sql)
        responses = []
        with patch.object(_server, "_require_admin", lambda h: ADMIN), \
             patch.object(_server, "_read_body",
                          lambda h: json.dumps({"dry_run": dry_run}).encode()), \
             patch.object(_server, "_json_response",
                          lambda h, s, b: responses.append((s, b))), \
             patch.object(_server, "db", mock_db), \
             patch.object(_server, "logger"):
            _server.KanbanHandler._handle_sync_relink_orphan_email_snapshots(Shim())
        return responses[0], writes

    def test_dry_run_reports_pair_and_writes_nothing(self):
        (status, body), writes = self._run(True)
        self.assertEqual(status, 200)
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["relinkable"], 1)
        self.assertEqual(body["items"][0]["old_message_id"], 1545)
        self.assertEqual(body["items"][0]["new_message_id"], 9100)
        self.assertEqual(writes, [])

    def test_apply_rewrites_pointer(self):
        (status, body), writes = self._run(False)
        self.assertEqual(status, 200)
        self.assertFalse(body["dry_run"])
        self.assertEqual(body["applied"], 1)
        upd = [w for w in writes if w.startswith("UPDATE history_events SET content")]
        self.assertEqual(len(upd), 1)

    def test_respects_include_sent_false(self):
        import server as _server
        mock_db = MagicMock()
        rows = [(49916, 1244, _snap(7581, frm="m@x.com", subj="A00007921686",
                                     date="2026-10-27 20:05:35.080054"))]
        mock_db.query.return_value = rows
        mock_db.query_dicts.side_effect = lambda sql, params=None, *a, **k: (
            [{"id": 9300, "account_id": 7, "from_addr": "m@x.com",
              "subject": "A00007921686",
              "date_received": datetime(2026, 10, 27, 20, 5, 35, 80054),
              "folder": "Sent"}]
            if "FROM mail_messages WHERE from_addr" in sql else [])
        mock_db.query_one.side_effect = lambda sql, params=None, *a, **k: None
        responses = []
        with patch.object(_server, "_require_admin", lambda h: ADMIN), \
             patch.object(_server, "_read_body",
                          lambda h: json.dumps({"dry_run": True,
                                                "include_sent": False}).encode()), \
             patch.object(_server, "_json_response",
                          lambda h, s, b: responses.append((s, b))), \
             patch.object(_server, "db", mock_db), \
             patch.object(_server, "logger"):
            _server.KanbanHandler._handle_sync_relink_orphan_email_snapshots(Shim())
        self.assertEqual(responses[0][1]["relinkable"], 0)
        # The only candidate lives in Sent, which is excluded -> the orphan
        # has no candidate at all, so it counts as unmatched (not ambiguous).
        self.assertEqual(responses[0][1]["unmatched"], 1)
        self.assertEqual(responses[0][1]["ambiguous"], 0)


class TestNoLiteralPercentInScanSql(unittest.TestCase):
    def test_scan_query_uses_strpos_not_like(self):
        """Live bug 2026-10-29: the scan used LIKE '%email_snapshot%' next to
        a %s param, and psycopg2 raised 'tuple index out of range' — the dry
        run died before reporting anything. Literal % wildcards must never
        appear in a parameterized query (same class as the 2026-10-26
        backfill strpos fix)."""
        import server as _server
        import inspect
        src = inspect.getsource(
            _server.KanbanHandler._handle_sync_relink_orphan_email_snapshots)
        self.assertNotIn("LIKE '%", src)
        self.assertIn("strpos(content, 'email_snapshot')", src)


class TestUiWiring(unittest.TestCase):
    def test_button_route_and_confirm_flow(self):
        src = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        self.assertIn("sync-relink-orphan-snapshots-btn", src)
        self.assertIn("/api/v2/admin/sync/relink-orphan-email-snapshots", src)
        at = src.index('$("#sync-relink-orphan-snapshots-btn")')
        region = src[at:at + 3500]
        self.assertIn("dry_run", region)
        self.assertIn("confirmDialog", region)
        self.assertIn("include_sent", region)
        idx = open("/home/chapman-server/Projects/new-crm/public/index.html", encoding="utf-8").read()
        self.assertIn("sync-relink-orphan-snapshots-btn", idx)


if __name__ == "__main__":
    unittest.main(verbosity=1)
