#!/usr/bin/env python3
"""Tests for the Phase 1 reconciler service (draft runs + approval guard).

No database and no client PDFs are needed: ``build_run`` takes already-parsed
estimates through its ``parsed=`` seam, and the approval *policy* is a pure
function (``approval_guard``).  The DB-backed approve path is exercised by
``tests/test_reconciler_routes.py`` when a database is reachable.

Run with: python3 tests/test_reconciler_service.py
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import reconciler_service as S  # noqa: E402


def make_estimate(rcv, *, pretax=None, desc="Laminated shingles", printed=None, status="ok", reason=""):
    """A minimal estimate that genuinely ties out (one priced row)."""
    pretax = rcv if pretax is None else pretax
    item = {
        "no": 1,
        "desc": desc,
        "section": "Roof",
        "qty": Decimal("1"),
        "unit": "EA",
        "unit_price": rcv,
        "tax": 0,
        "op": 0,
        "rcv": rcv,
        "pretax": pretax,
        "deprec": 0,
        "acv": rcv,
        "acv_printed": False,
        "ok": True,
    }
    return {
        "status": status,
        "reason": reason,
        "kind": "carrier",
        "items": [] if status != "ok" else [item],
        "totals": {"rcv": rcv, "line_item_total": pretax, "tax": 0,
                   "overhead_profit": 0, "deductible": 0, "acv": rcv},
        "checks": {
            "line_item_totals_printed": [rcv if printed is None else printed],
            "items_match_printed_total": printed is None or printed == rcv,
        },
        "summaries": [{"name": "Coverage A", "line_item_total": pretax,
                       "tax_total": 0, "rcv": rcv, "deductible": 0}],
        "sections": [],
        "printed_totals": [],
        "warnings": [],
        "meta": {},
    }


DOCS = {
    "prior": {"docRef": "doc:1", "title": "prior.pdf", "path": "/nonexistent/prior.pdf"},
    "revised": {"docRef": "doc:2", "title": "revised.pdf", "path": "/nonexistent/revised.pdf"},
    "our": {"docRef": "doc:3", "title": "our.pdf", "path": "/nonexistent/our.pdf"},
}


class TestBuildRunAllowed(unittest.TestCase):
    def setUp(self):
        self.prior = make_estimate(1_000_000)
        self.revised = make_estimate(1_500_000)
        self.our = make_estimate(1_400_000)
        self.run = S.build_run(
            7, DOCS, parsed={"prior": self.prior, "revised": self.revised, "our": self.our}
        )

    def test_status_and_gate(self):
        self.assertTrue(self.run["gateAllowed"])
        self.assertEqual(self.run["status"], S.STATUS_DRAFTED)
        self.assertGreater(self.run["confidence"], 0.5)

    def test_draft_note_states_the_increase(self):
        self.assertIn("Claim increased by $5,000.00", self.run["draftNote"])
        self.assertNotIn("NEEDS REVIEW", self.run["draftNote"])

    def test_billing_summary_present(self):
        self.assertTrue(any("RCV" in ln for ln in self.run["billingSummary"]))

    def test_inputs_record_doc_refs_and_hashes(self):
        self.assertEqual(self.run["inputs"]["prior"]["docRef"], "doc:1")
        # a fake path has no hash - it is None, never invented
        self.assertIsNone(self.run["inputs"]["prior"]["sha256"])

    def test_result_is_json_serialisable(self):
        import json

        json.dumps(self.run["result"])  # Decimal qty must not leak through

    def test_result_carries_totals_and_explanations(self):
        est = self.run["result"]["estimates"]
        self.assertEqual(est["prior"]["totals"]["rcv"], 1_000_000)
        self.assertEqual(est["revised"]["totals"]["rcv"], 1_500_000)
        self.assertEqual(self.run["result"]["diff"]["summary"]["net_rcv"], 500_000)
        self.assertIn("gap", self.run["result"])

    def test_approval_guard_allows_a_drafted_run(self):
        guard = S.approval_guard({
            "status": S.STATUS_DRAFTED, "gateAllowed": True, "draftNote": "Claim increased by $5,000.00",
        })
        self.assertTrue(guard["allowed"])


class TestBuildRunBlocked(unittest.TestCase):
    def test_failed_parse_blocks_and_states_no_increase(self):
        # revised prints a total that its items do not sum to -> tie-out fails
        bad = make_estimate(1_500_000, printed=1_500_001)
        run = S.build_run(
            7, DOCS, parsed={"prior": make_estimate(1_000_000), "revised": bad,
                             "our": make_estimate(1_400_000)},
        )
        self.assertFalse(run["gateAllowed"])
        self.assertEqual(run["status"], S.STATUS_NEEDS_REVIEW)
        self.assertNotIn("increased by", run["draftNote"].lower())
        self.assertIn("NEEDS REVIEW", run["draftNote"])
        self.assertEqual(run["billingSummary"], [])

    def test_blocked_run_cannot_be_approved(self):
        bad = make_estimate(1_500_000, printed=1_500_001)
        run = S.build_run(
            7, DOCS, parsed={"prior": make_estimate(1_000_000), "revised": bad,
                             "our": make_estimate(1_400_000)},
        )
        guard = S.approval_guard({
            "status": run["status"],
            "gateAllowed": run["gateAllowed"],
            "draftNote": run["draftNote"],
            "result": run["result"],
        })
        self.assertFalse(guard["allowed"])
        self.assertEqual(guard["code"], 409)
        self.assertIn("tie-out gate", guard["reason"])

    def test_missing_our_estimate_still_drafts(self):
        our_missing = {"status": "error", "reason": "not supplied", "items": [], "totals": {}}
        run = S.build_run(
            7, DOCS, parsed={"prior": make_estimate(1_000_000),
                             "revised": make_estimate(1_500_000), "our": our_missing},
        )
        # the third slot is optional: the increase can draft, the gap cannot
        self.assertTrue(run["gateAllowed"])
        self.assertEqual(run["status"], S.STATUS_DRAFTED)
        self.assertIsNone(run["result"]["gap"])

    def test_scanned_our_estimate_does_not_block_the_increase(self):
        scan = make_estimate(0, status="needs_ocr", reason="PDF has no text layer")
        run = S.build_run(
            7, DOCS, parsed={"prior": make_estimate(1_000_000),
                             "revised": make_estimate(1_500_000), "our": scan},
        )
        self.assertTrue(run["gateAllowed"])
        self.assertEqual(run["status"], S.STATUS_DRAFTED)
        self.assertIsNone(run["result"]["gap"])
        self.assertIn("our", run["result"]["needsOcr"])

    def test_scanned_document_is_needs_ocr_with_a_clear_message(self):
        scan = make_estimate(0, status="needs_ocr", reason="PDF has no text layer")
        run = S.build_run(
            7, DOCS, parsed={"prior": make_estimate(1_000_000), "revised": scan,
                             "our": make_estimate(1_400_000)},
        )
        self.assertEqual(run["status"], S.STATUS_NEEDS_OCR)
        self.assertIn("revised", run["result"]["needsOcr"])
        text = " ".join(run["result"]["messages"])
        self.assertIn("OCR", text)
        self.assertIn("scanned estimate needs", text)
        self.assertFalse(run["gateAllowed"])


class TestApprovalGuard(unittest.TestCase):
    def test_missing_run(self):
        self.assertEqual(S.approval_guard(None)["code"], 404)

    def test_approved_is_idempotent(self):
        guard = S.approval_guard({"status": S.STATUS_APPROVED, "gateAllowed": True})
        self.assertTrue(guard["allowed"])
        self.assertTrue(guard["alreadyApproved"])

    def test_rejected_cannot_post(self):
        guard = S.approval_guard({"status": S.STATUS_REJECTED, "gateAllowed": True})
        self.assertFalse(guard["allowed"])

    def test_empty_note_cannot_post(self):
        guard = S.approval_guard({"status": S.STATUS_DRAFTED, "gateAllowed": True, "draftNote": "   "})
        self.assertFalse(guard["allowed"])

    def test_gate_false_blocks_even_with_a_note(self):
        guard = S.approval_guard({
            "status": S.STATUS_DRAFTED,
            "gateAllowed": False,
            "draftNote": "Claim increased by $999.00",
        })
        self.assertFalse(guard["allowed"])


class TestSourceGuards(unittest.TestCase):
    """Pin the invariants a live HTTP test would otherwise have to exercise."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "reconciler_service.py").read_text(encoding="utf-8")
        cls.server = (ROOT / "server.py").read_text(encoding="utf-8")

    def test_approve_never_writes_custom_fields(self):
        start = self.src.index("def approve_run(")
        end = self.src.index("def reject_run(")
        body = self.src[start:end]
        self.assertNotIn("custom_field", body.replace("custom fields", ""))
        self.assertNotIn("custom-fields", body)

    def test_approve_removes_only_the_reconciler_tag(self):
        start = self.src.index("def approve_run(")
        end = self.src.index("def reject_run(")
        body = self.src[start:end]
        self.assertIn("TAG_TITLE", body)
        self.assertIn("DELETE FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s", body)

    def test_approve_checks_the_gate(self):
        start = self.src.index("def approve_run(")
        end = self.src.index("def reject_run(")
        body = self.src[start:end]
        self.assertIn("approval_guard(run)", body)
        self.assertIn("gateAllowed", self.src)

    def test_route_handlers_use_the_expected_gates(self):
        for handler, gate in (
            ("_handle_reconciler_run_create", "_require_auth(self)"),
            ("_handle_reconciler_run_get", "_require_auth(self)"),
            ("_handle_project_reconciler_runs_get", "_require_auth(self)"),
            ("_handle_reconciler_run_patch", "_require_auth(self)"),
            ("_handle_reconciler_run_approve", "_require_admin(self)"),
            ("_handle_reconciler_run_reject", "_require_auth(self)"),
        ):
            start = self.server.index(f"def {handler}(")
            body = self.server[start:start + 1200]
            self.assertIn(gate, body, f"{handler} must gate on {gate}")

    def test_routes_are_registered(self):
        self.assertIn('api_path == "/api/v2/reconciler/runs" and method == "POST"', self.server)
        self.assertIn(r'runs/(\d+)/approve$', self.server)
        self.assertIn(r'runs/(\d+)/reject$', self.server)
        self.assertIn(r'api_path == "/api/v2/reconciler/health"', self.server)

    def test_no_ocr_in_the_request_path(self):
        """The service parses with ocr=False: production must not need PaddleOCR."""
        self.assertIn("ocr=False", self.src)
        self.assertNotIn("ocr=True", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
