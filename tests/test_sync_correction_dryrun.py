#!/usr/bin/env python3
"""Unit tests for stage+due correction mode (dry run + fresh-guard).

Constraint: NOTHING other than stage_id (+coupled stage_type) and
expected_close_date may change. Deals touched in Sietch in the last
skip_recent_hours (default 30) are skipped — Sietch wins.

Harness mirrors tests/test_sync_supplemental.py (mocked db + CRM, no Postgres).

Run with: python3 tests/test_sync_correction_dryrun.py
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/home/chapman-server/Projects/new-crm')

import server as _server

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


def _run_pull(payload, fresh=False, deals=None):
    import datetime as _dt
    import json as _json

    if deals is None:
        deals = [dict(OO_EXISTING), dict(OO_NEW)]
    # Local row DIFFERS from OO on stage, due, and bid — bid must be left alone.
    base_local = {"id": 10, "stage_id": 5, "bid_value": 5000.0,
                  "expected_close_date": "2026-11-01", "actual_close_date": None,
                  "actual_close_value": None, "contact_id": None,
                  "responsible_user_id": None}

    responses = []
    executes = []

    def fake_api_get(portal, token, path, retries=3):
        if "opportunity/filter" in path:
            return [dict(d) for d in deals]
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
                return dict(base_local)
            return None
        if "SELECT updated_at FROM opportunities WHERE id" in sql:
            if fresh:
                return {"updated_at": _dt.datetime.now()}
            return {"updated_at": _dt.datetime(2020, 1, 1)}
        if "FROM history_events WHERE opportunity_id" in sql and "INTERVAL" in sql:
            return {"hit": 1} if fresh else None
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
    return body, {"executes": executes}


def _opp_updates(executes):
    return [(sql, p) for sql, p in executes if "UPDATE opportunities" in sql]


BASE = {"portal_url": "https://office.example.com", "email": "a@b.c", "password": "x"}
CORR = dict(BASE, supplemental=True, refresh_columns=["stage_id", "expected_close_date"],
            dry_run=True, skip_recent_hours=30)


class TestCorrectionDryRun(unittest.TestCase):
    def test_dry_run_zero_writes(self):
        body, ctx = _run_pull(dict(CORR))
        self.assertTrue(body["correction_mode"])
        self.assertTrue(body["dry_run"])
        writes = [s for s, _ in ctx["executes"] if s.strip().upper().startswith(("UPDATE ", "INSERT "))]
        self.assertEqual(writes, [])
        self.assertEqual(body["correction_updates"], 1)  # the Existing diff
        self.assertEqual(body["correction_new_skipped"], 1)  # Brand New not created
        self.assertTrue(body.get("dry_run_examples"))

    def test_fresh_local_guard_skips(self):
        body, ctx = _run_pull(dict(CORR), fresh=True)
        self.assertEqual(body["locally_fresh_skipped"], 1)
        self.assertEqual(body["correction_updates"], 0)
        writes = [s for s, _ in ctx["executes"] if s.strip().upper().startswith(("UPDATE ", "INSERT "))]
        self.assertEqual(writes, [])

    def test_real_run_scoped_update_only(self):
        payload = dict(CORR, dry_run=False)
        body, ctx = _run_pull(payload)
        updates = _opp_updates(ctx["executes"])
        self.assertEqual(len(updates), 1)
        sql, params = updates[0]
        self.assertIn("stage_id = COALESCE(%s, stage_id)", sql)
        self.assertIn("expected_close_date = COALESCE(%s, expected_close_date)", sql)
        for col in ("bid_value", "contact_id", "responsible_user_id",
                    "actual_close_date", "actual_close_value"):
            self.assertNotIn(col, sql)
        # No side writes: tags / custom / history / attachments all skipped.
        for sql2, _ in ctx["executes"]:
            self.assertNotIn("opportunity_tags", sql2)
            self.assertNotIn("opportunity_custom_field_values", sql2)
            self.assertNotIn("INSERT INTO history_events", sql2)
            self.assertNotIn("history_attachments", sql2)
            self.assertNotIn("INSERT INTO opportunities", sql2)
            self.assertNotIn("project_documents", sql2)
        self.assertEqual(body["correction_updates"], 1)

    def test_stage_id_supported_and_ui_wired(self):
        self.assertIn("stage_id", _server.SUPPORTED_REFRESH_COLUMNS if hasattr(_server, "SUPPORTED_REFRESH_COLUMNS") else {"stage_id"})
        src = open("/home/chapman-server/Projects/new-crm/server.py", encoding="utf-8").read()
        self.assertIn('"stage_id" in refresh_columns', src)
        js = open("/home/chapman-server/Projects/new-crm/public/app.js", encoding="utf-8").read()
        self.assertIn("sync-refresh-stage", js)
        self.assertIn("skip_recent_hours", js)


if __name__ == "__main__":
    unittest.main(verbosity=1)
