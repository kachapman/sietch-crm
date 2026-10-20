#!/usr/bin/env python3
"""Behavioral tests for pull-deals supplemental (add-only) mode.

Proves the admin "Supplemental" checkbox is actually acknowledged end to end:
the flag must CHANGE the SQL the handler emits. Each test runs the real
_server.KanbanHandler._handle_sync_pull_deals with a mocked CRM API + db and
inspects the recorded statements — notably in BOTH directions, so a dead flag
(that parses but never branches) fails loudly.

Canned scenario: one existing deal (OO-1, local stage set + bid 5000) and one
new deal (OO-2). OO sends bid 6000 (a real overwrite candidate) and a due date
the local row lacks (a legitimate gap-fill).

Also covers: since/stage/responsible filters reaching the OO fetch (they were
parsed but ignored), and the UI read-chain (checkbox id -> payload key).

Run with: python3 tests/test_sync_supplemental.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

ADMIN = {"id": 7, "is_admin": True}

OO_EXISTING = {
    "id": "OO-1", "title": "Existing",
    "stage": {"id": 99, "title": "Bidding", "stageType": 0},
    "bidValue": 6000,
    "expectedCloseDate": "2026-12-01",
    "responsible": {"id": "u-9"},
    "contact": {"id": "c-1"},
}
OO_NEW = {
    "id": "OO-2", "title": "Brand New",
    "stage": {"title": "Bidding"},
    "bidValue": 100,
}
OO_HISTORY = [{
    "id": 9001, "content": "employee note", "created": "2026-10-01T10:00:00",
    "category": {"title": "Note"}, "createBy": {"id": "u-9"},
}]


class Shim:
    class headers:
        @staticmethod
        def get(key, default=None):
            return default

    class rfile:
        @staticmethod
        def read(n):
            return b""

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    wfile = MagicMock()


def _run_pull(payload, cf_insert_rowcount=1):
    """Run the handler with canned CRM + db. Returns (response_body, ctx)."""
    import json as _json

    responses = []
    executes = []
    fetch_paths = []
    oo_lookups = {}

    def fake_api_get(portal, token, path, retries=3):
        fetch_paths.append(path)
        if "opportunity/filter" in path:
            return [dict(OO_EXISTING), dict(OO_NEW)]
        if "/opportunity/tag/" in path:
            return [{"title": "Hot"}]
        if "customfield" in path:
            return [{"id": "5", "value": "Val"}]
        if "history/filter" in path:
            return [dict(OO_HISTORY[0])]
        return []

    def fake_query_one(sql, params=None, *a, **k):
        if "FROM opportunities WHERE onlyoffice_id" in sql:
            if params and params[0] == "OO-1":
                return {"id": 10, "stage_id": 3, "bid_value": 5000.0,
                        "expected_close_date": None, "actual_close_date": None,
                        "actual_close_value": None, "contact_id": None,
                        "responsible_user_id": None}
            # New deals: absent on first lookup, present on the post-insert
            # re-read (production always finds the row it just inserted).
            key = params[0] if params else None
            oo_lookups[key] = oo_lookups.get(key, 0) + 1
            return {"id": 99} if oo_lookups[key] > 1 else None
        if "FROM opportunities WHERE id" in sql:
            return None
        if "FROM contacts WHERE onlyoffice_id" in sql:
            return None
        if "FROM history_categories WHERE title" in sql:
            return {"id": 8}
        if "FROM tag_definitions WHERE title" in sql:
            return {"id": 1}
        return None

    def fake_query_dicts(sql, params=None, *a, **k):
        if "FROM tag_definitions" in sql:
            return []
        if "FROM custom_field_definitions" in sql:
            return [{"id": 7, "field_key": "field_5"}]
        if "FROM stages" in sql:
            return [{"id": 3, "title": "Bidding", "stage_type": 0}]
        if "FROM opportunities" in sql:
            return [{"id": 10, "title": "Existing"}]
        return []

    def fake_execute(sql, params=None, *a, **k):
        executes.append((sql, params))
        if "opportunity_custom_field_values" in sql and "DO NOTHING" in sql:
            return cf_insert_rowcount
        return 1

    mock_db = MagicMock()
    mock_db.query_one.side_effect = fake_query_one
    mock_db.query_dicts.side_effect = fake_query_dicts
    mock_db.execute.side_effect = fake_execute
    mock_db.query.side_effect = lambda *a, **k: []

    with patch.object(_server, "_crm_authenticate", lambda *a, **k: "tok"), \
        patch.object(_server, "_crm_api_get", fake_api_get), \
        patch.object(_server, "_crm_fetch_user_id_map", lambda *a, **k: {}), \
        patch.object(_server, "_read_body", lambda h: _json.dumps(payload).encode()), \
        patch.object(_server, "_json_response", lambda h, s, b: responses.append((s, b))), \
        patch.object(_server, "db", mock_db), \
        patch.object(_server, "logger"):
        _server.KanbanHandler._handle_sync_pull_deals(Shim())

    assert responses, "handler produced no response"
    status, body = responses[-1]
    assert status == 200, f"handler failed: {body}"
    return body, {"executes": executes, "fetch_paths": fetch_paths}


def _opp_updates(executes):
    return [sql for sql, _ in executes if "UPDATE opportunities" in sql]


BASE_PAYLOAD = {"portal_url": "https://office.example.com", "email": "a@b.c", "password": "x"}


class TestSupplementalFlagAcknowledged(unittest.TestCase):
    """The checkbox flag must change emitted SQL — in both directions."""

    def test_supplemental_never_overwrites(self):
        body, ctx = _run_pull(dict(BASE_PAYLOAD, supplemental=True))
        self.assertTrue(body["supplemental"])
        updates = _opp_updates(ctx["executes"])
        self.assertTrue(updates, "expected a gap-fill UPDATE for the existing deal")
        for sql in updates:
            # Fill-gaps form keeps the existing value first...
            self.assertIn("COALESCE(stage_id, %s)", sql)
            # ...and the overwrite form must be entirely absent.
            self.assertNotIn("COALESCE(%s, stage_id)", sql)
            self.assertNotIn("stage_type", sql)
        # Bid 6000 vs local 5000 = preserved; due date was empty = filled.
        self.assertEqual(body["fields_filled"], 1)
        self.assertEqual(body["fields_preserved"], 1)
        self.assertEqual(body.get("fields_overwritten", 0), 0)

    def test_full_mode_still_overwrites(self):
        # Same data without the flag: legacy overwrite SQL must appear, or the
        # flag test above proves nothing (both branches could emit one form).
        body, ctx = _run_pull(dict(BASE_PAYLOAD))
        self.assertFalse(body.get("supplemental"))
        updates = _opp_updates(ctx["executes"])
        self.assertTrue(updates)
        self.assertTrue(any("COALESCE(%s, stage_id)" in sql for sql in updates))
        self.assertEqual(body["fields_overwritten"], 1)

    def test_custom_fields_insert_only_in_supplemental(self):
        _body, ctx = _run_pull(dict(BASE_PAYLOAD, supplemental=True))
        cf_writes = [sql for sql, _ in ctx["executes"]
                     if "opportunity_custom_field_values" in sql]
        self.assertTrue(cf_writes)
        for sql in cf_writes:
            self.assertIn("DO NOTHING", sql)
            self.assertNotIn("DO UPDATE", sql)

    def test_custom_fields_upsert_in_full_mode(self):
        _body, ctx = _run_pull(dict(BASE_PAYLOAD))
        cf_writes = [sql for sql, _ in ctx["executes"]
                     if "opportunity_custom_field_values" in sql]
        self.assertTrue(any("DO UPDATE" in sql for sql in cf_writes))

    def test_new_deals_and_history_created_in_both_modes(self):
        for payload in (dict(BASE_PAYLOAD, supplemental=True), dict(BASE_PAYLOAD)):
            body, ctx = _run_pull(payload)
            self.assertEqual(body["created"], 1)
            hist = [sql for sql, _ in ctx["executes"] if "INSERT INTO history_events" in sql]
            self.assertEqual(len(hist), 2)  # one per deal, no dupes


class TestFiltersHonored(unittest.TestCase):
    """since/stage/responsible were parsed but ignored — prove they now scope."""

    def test_filters_reach_the_oo_fetch(self):
        _body, ctx = _run_pull(dict(BASE_PAYLOAD, supplemental=True, since="2026-10-01",
                                    stage_id=99, responsible_id="u-9"))
        fetch = [p for p in ctx["fetch_paths"] if "opportunity/filter" in p][0]
        self.assertIn("updatedAfter=2026-10-01", fetch)
        self.assertIn("stageId=99", fetch)
        self.assertIn("responsibleId=u-9", fetch)

    def test_responsible_filter_skips_locally(self):
        # "u-9" is the canned responsible; filtering to anyone else must skip
        # both deals without creating or touching anything.
        body, _ctx = _run_pull(dict(BASE_PAYLOAD, supplemental=True, responsible_id="someone-else"))
        self.assertEqual(body["filtered_out"], 2)
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["skipped"], 0)


class TestCheckboxWiring(unittest.TestCase):
    """The UI read-chain: checkbox element -> payload key -> handler branch."""

    def test_checkbox_exists_checked_and_read(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        html = (root / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="sync-supplemental"', html)
        self.assertIn('id="sync-supplemental" checked', html.replace("  ", " "))
        js = (root / "public" / "app.js").read_text(encoding="utf-8")
        region = js[js.index('$("#sync-deals-btn")'):]
        region = region[:region.index("sync-tags-btn")]
        self.assertIn('sync-supplemental', region)
        self.assertIn('supplemental', region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
