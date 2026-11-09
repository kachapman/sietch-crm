#!/usr/bin/env python3
"""DB-free tests for the carrier-estimate-lines read projection.

Uses only the synthetic fixture under tests/fixtures/carrier_estimate_lines/.
No real claim PDFs, no Postgres, no running server.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from carrier_estimate_lines import (  # noqa: E402
    build_carrier_lines_payload,
    is_likely_carrier_estimate,
    resolve_document_path,
    select_carrier_document,
    serialize_estimate,
    serialize_item,
)

FIXTURE = HERE / "fixtures" / "carrier_estimate_lines" / "synthetic_carrier_estimate.txt"
SERVER_SRC = (ROOT / "server.py").read_text(encoding="utf-8")


class TestHeuristics(unittest.TestCase):
    def test_carrier_titles(self):
        self.assertTrue(is_likely_carrier_estimate("Allstate Carrier Estimate.pdf"))
        self.assertTrue(is_likely_carrier_estimate("xactimate_estimate.txt"))
        self.assertFalse(is_likely_carrier_estimate("Vanguard Supplement Estimate.pdf"))

    def test_select_prefers_explicit_id(self):
        docs = [
            {"id": 1, "title": "Allstate Carrier Estimate.pdf", "filePath": "data/documents/1/a.pdf"},
            {"id": 2, "title": "photo.jpg", "filePath": "data/documents/1/b.jpg"},
        ]
        self.assertEqual(select_carrier_document(docs, document_id=2)["id"], 2)
        self.assertEqual(select_carrier_document(docs)["id"], 1)

    def test_resolve_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe.txt").write_text("ok", encoding="utf-8")
            with self.assertRaises(ValueError):
                resolve_document_path("../etc/passwd", root=root)
            with self.assertRaises(ValueError):
                resolve_document_path("/etc/passwd", root=root)


class TestSerializeAndParse(unittest.TestCase):
    def test_fixture_parses_two_lines(self):
        self.assertTrue(FIXTURE.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = Path("docs") / "synthetic_carrier_estimate.txt"
            dest = root / rel
            dest.parent.mkdir(parents=True)
            dest.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            docs = [
                {
                    "id": 42,
                    "title": "Allstate Carrier Estimate.txt",
                    "filePath": str(rel),
                    "mimeType": "text/plain",
                }
            ]
            payload = build_carrier_lines_payload(
                job_id=99, documents=docs, root=root, document_id=42
            )
        self.assertTrue(payload["available"])
        self.assertEqual(payload["job_id"], "99")
        self.assertEqual(payload["item_count"], 2)
        self.assertEqual(payload["meta"]["carrier"], "Allstate")
        self.assertEqual(payload["meta"]["claim_number"], "SYN-CLAIM-0001")
        self.assertEqual(payload["items"][0]["desc"], "Tear off laminate shingles")
        self.assertEqual(payload["items"][0]["rcv_cents"], 350000)
        # No filesystem leakage
        blob = json.dumps(payload)
        self.assertNotIn(str(root), blob)
        self.assertNotIn("filePath", blob)
        self.assertNotIn(str(rel), blob)

    def test_serialize_item_money_is_int_cents(self):
        row = serialize_item(
            {
                "no": 1,
                "desc": "Test",
                "qty": "1.00",
                "unit": "EA",
                "unit_price": 100,
                "tax": 0,
                "op": 0,
                "rcv": 100,
                "deprec": 0,
                "acv": 100,
                "ok": True,
            }
        )
        self.assertEqual(row["rcv_cents"], 100)
        self.assertIsInstance(row["rcv_cents"], int)


class TestServerWiringContract(unittest.TestCase):
    """Source guards so the route cannot silently disappear."""

    def test_route_registered(self):
        self.assertIn("carrier-estimate-lines", SERVER_SRC)
        self.assertIn("_handle_project_carrier_estimate_lines", SERVER_SRC)

    def test_import_helper_used(self):
        self.assertIn("build_carrier_lines_payload", SERVER_SRC)
        self.assertIn("carrier_estimate_lines", SERVER_SRC)


if __name__ == "__main__":
    unittest.main()
