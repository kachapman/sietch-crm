#!/usr/bin/env python3
"""Offline tests for the live acceptance recipient hard-lock."""
import importlib.util, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_acceptance", ROOT / "scripts" / "live_mail_acceptance.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def test_allowed():
    assert m.validate_recipients("crm@vanguardadj.online") == []
    assert m.validate_recipients("CRM@vanguardadj.online") == []

def test_refuses_others():
    assert m.validate_recipients("kenc@vanguardadj.com") == ["kenc@vanguardadj.com"]
    assert m.validate_recipients("crm@vanguardadj.online", cc="x@example.invalid") == ["x@example.invalid"]
    assert m.validate_recipients("crm@vanguardadj.online", bcc="y@example.invalid") == ["y@example.invalid"]
    assert m.validate_recipients("a@b.c, crm@vanguardadj.online") == ["a@b.c"]

def test_payload_recipients():
    assert m.payload_recipients({"to": "crm@vanguardadj.online"}) == ["crm@vanguardadj.online"]
    assert m.payload_recipients({"to": "crm@vanguardadj.online", "cc": "x@example.invalid"}) == [
        "crm@vanguardadj.online", "x@example.invalid"]

def test_matrix_shape():
    rows = m.matrix_rows()
    assert len(rows) == 41, len(rows)
    assert len({r["id"] for r in rows}) == 41
    assert all(r["account_id"] in (2, 6, 8, 9, 10) for r in rows)

def test_dry_run_does_not_send(monkeypatch=None):
    assert m.main([]) == 0

if __name__ == "__main__":
    test_allowed(); test_refuses_others(); test_payload_recipients(); test_matrix_shape(); test_dry_run_does_not_send()
    print("live acceptance allowlist tests OK")
