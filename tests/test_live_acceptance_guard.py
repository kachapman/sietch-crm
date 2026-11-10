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

def test_dry_run_does_not_send(monkeypatch=None):
    assert m.main([]) == 0

if __name__ == "__main__":
    test_allowed(); test_refuses_others(); test_dry_run_does_not_send()
    print("live acceptance allowlist tests OK")
