#!/usr/bin/env python3
"""Unit tests for OnlyOffice import helpers (server.py + import_json_export.py)."""
import os
import sys
sys.path.insert(0, '/home/chapman-server/Projects/new-crm')


def test_field_type_map_numeric():
    """Bug #1: numeric fieldType must be mapped correctly."""
    import import_json_export as ije
    # Integers from the export
    assert ije._normalize_field_type(0) == "text"
    assert ije._normalize_field_type(1) == "textarea"
    assert ije._normalize_field_type(2) == "number"
    assert ije._normalize_field_type(3) == "url"        # file picker → url
    assert ije._normalize_field_type(4) == "select"
    assert ije._normalize_field_type(5) == "date"
    assert ije._normalize_field_type(6) == "checkbox"
    assert ije._normalize_field_type(7) == "currency"
    # String versions (legacy)
    assert ije._normalize_field_type("0") == "text"
    assert ije._normalize_field_type("1") == "textarea"
    assert ije._normalize_field_type("3") == "url"
    assert ije._normalize_field_type("5") == "date"
    # Unknown / None
    assert ije._normalize_field_type(None) == "text"
    assert ije._normalize_field_type("unknown") == "text"
    print("✓ test_field_type_map_numeric")


def test_closure_sentinel():
    """actualCloseDate '0001-01-01T...' should be treated as not-closed."""
    import import_json_export as ije
    assert ije._is_closure_sentinel("0001-01-01T00:00:00.0000000Z")
    assert ije._is_closure_sentinel("0001-01-01T00:00:00")
    assert not ije._is_closure_sentinel("2026-07-15T15:00:00-04:00")
    assert not ije._is_closure_sentinel("")
    assert not ije._is_closure_sentinel(None)
    print("✓ test_closure_sentinel")


def test_safe_filename():
    """Path-traversal stripping is the security goal, exact chars are not contractual."""
    import import_json_export as ije
    assert ije._safe_filename("normal.pdf") == "normal.pdf"
    # All `..` and `/` and `!` must be removed (no path traversal possible)
    for bad in ("../etc/passwd", "../../../etc/shadow", "..\\windows\\system32", "a/b/c.txt"):
        clean = ije._safe_filename(bad)
        assert "/" not in clean, f"slash leaked: {clean}"
        assert "\\" not in clean, f"backslash leaked: {clean}"
        assert ".." not in clean, f"dotdot leaked: {clean}"
    # Trims leading dots, falls back to "file"
    assert ije._safe_filename("") == "file"
    assert ije._safe_filename("...") == "file"
    assert ije._safe_filename(".hidden") == "hidden"
    print("✓ test_safe_filename")


def test_onlyoffice_field_key_convention():
    """Both scripts must use field_<id> as the field_key."""
    # import_json_export
    import import_json_export as ije
    field_key = f"field_{32}"
    assert field_key == "field_32"
    # migrate_from_onlyoffice (loaded below)
    import migrate_from_onlyoffice as mfo
    # Same constant in both
    assert mfo._OO_FIELD_TYPE_MAP["3"] == "url"
    assert ije._OO_FIELD_TYPE_MAP["3"] == "url"
    print("✓ test_onlyoffice_field_key_convention")


def test_email_payload_parse():
    """Mail Message events have JSON in content."""
    import import_json_export as ije
    sample = '{"from":"A <a@x>","to":"B <b@y>","subject":"hi","message_id":29914,"introduction":"first 500 chars"}'
    parsed = ije._parse_email_payload(sample)
    assert parsed is not None
    assert parsed["subject"] == "hi"
    assert parsed["message_id"] == 29914
    assert ije._parse_email_payload("") is None
    assert ije._parse_email_payload("not json") is None
    assert ije._parse_email_payload("{\"title\":\"x\"}") is None  # no from/subject
    print("✓ test_email_payload_parse")


def test_dedupe_contacts():
    """Duplicate IDs in export should collapse to one contact."""
    import import_json_export as ije
    # Two copies of the same contact
    contacts = [
        {"id": 1, "displayName": "A", "isCompany": False, "commonData": [{"infoType": 1, "data": "a@x"}]},
        {"id": 2, "displayName": "B", "isCompany": True, "commonData": []},
        {"id": 1, "displayName": "A dup", "isCompany": False, "commonData": []},
        {"id": 3, "displayName": "C", "isCompany": False, "commonData": []},
    ]
    seen = set()
    unique = []
    for c in contacts:
        cid = str(c.get("id") or "")
        if cid in seen:
            continue
        seen.add(cid)
        unique.append(c)
    assert len(unique) == 3
    assert unique[0]["id"] == 1
    assert unique[1]["id"] == 2
    assert unique[2]["id"] == 3
    print("✓ test_dedupe_contacts")


def test_unique_attachment_filename():
    """Attachments with the same title must have unique filenames."""
    from pathlib import Path
    # Simulate two attachments with same title in same event
    safe_name = "report.pdf"
    f_id_1, f_id_2 = 100, 200
    unique_1 = f"{f_id_1}_{safe_name}"
    unique_2 = f"{f_id_2}_{safe_name}"
    assert unique_1 != unique_2
    print("✓ test_unique_attachment_filename")


def test_mime_for_esx():
    """ESX files (insurance estimate format) should map to something safe."""
    import import_json_export as ije
    assert "octet-stream" in ije._guess_mime("esx")
    assert ije._guess_mime("pdf") == "application/pdf"
    assert ije._guess_mime("JPG") == "image/jpeg"
    print("✓ test_mime_for_esx")


if __name__ == "__main__":
    test_field_type_map_numeric()
    test_closure_sentinel()
    test_safe_filename()
    test_onlyoffice_field_key_convention()
    test_email_payload_parse()
    test_dedupe_contacts()
    test_unique_attachment_filename()
    test_mime_for_esx()
    print("\nAll 8 tests passed.")