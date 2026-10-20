"""Telegram bot customer mappings and pending invite codes."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "bot-customers"
CODE_EXPIRE_HOURS = 48
CODE_LENGTH = 8
MAX_CONTACT_MAPPINGS = 10


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    seg = re.sub(r"[^\w.-]+", "_", str(value or "").strip())[:120]
    return seg or fallback


def _portal_dir(portal: str) -> Path:
    return DATA_DIR / _safe_segment(portal, "portal")


def _load_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"mappings": [], "pendingCodes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"mappings": [], "pendingCodes": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"mappings": [], "pendingCodes": []}


def _save_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _store_path(portal: str) -> Path:
    return _portal_dir(portal) / "store.json"


# ── Mappings ──

def list_mappings(portal: str) -> list[dict[str, Any]]:
    store = _load_file(_store_path(portal))
    return store.get("mappings", [])


def add_mapping(portal: str, chat_id: int, contact_id: int | None, contact_name: str,
                notes_category_id: int | None, nickname: str = "",
                employee: bool = False, user_id: int | None = None) -> dict[str, Any] | str:
    """Add a bot mapping. Returns the mapping dict on success, or an error string on failure."""
    path = _store_path(portal)
    store = _load_file(path)
    mappings = store.get("mappings", [])

    # Check if this chat already has a mapping (replace in-place)
    existing_chat_idx = None
    for i, m in enumerate(mappings):
        if m.get("chatId") == chat_id:
            existing_chat_idx = i
            break

    # For non-employees: enforce max mappings per contact
    if not employee and contact_id is not None:
        contact_count = sum(1 for m in mappings if m.get("contactId") == contact_id)
        # Count doesn't include the existing chat mapping if it's being replaced
        if existing_chat_idx is not None and mappings[existing_chat_idx].get("contactId") == contact_id:
            pass  # Replacing same chat's link, not adding a new one
        elif contact_count >= MAX_CONTACT_MAPPINGS:
            return f"Contact already has the maximum of {MAX_CONTACT_MAPPINGS} linked chats"

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "chatId": chat_id,
        "contactId": contact_id,
        "contactName": contact_name,
        "notesCategoryId": notes_category_id,
        "nickname": nickname,
        "employee": employee,
        "userId": user_id,
        "createdAt": now,
        "updatedAt": now,
    }

    if existing_chat_idx is not None:
        entry["createdAt"] = mappings[existing_chat_idx].get("createdAt", now)
        mappings[existing_chat_idx] = entry
    else:
        mappings.append(entry)

    store["mappings"] = mappings
    _save_file(path, store)
    return entry


def remove_mapping(portal: str, contact_id: int | None) -> bool:
    path = _store_path(portal)
    store = _load_file(path)
    mappings = store.get("mappings", [])
    if contact_id is not None:
        new_mappings = [m for m in mappings if m.get("contactId") != contact_id]
    else:
        new_mappings = [m for m in mappings if not m.get("employee")]
    if len(new_mappings) == len(mappings):
        return False
    store["mappings"] = new_mappings
    _save_file(path, store)
    return True


def remove_mapping_by_chat(portal: str, chat_id: int) -> bool:
    path = _store_path(portal)
    store = _load_file(path)
    mappings = store.get("mappings", [])
    new_mappings = [m for m in mappings if m.get("chatId") != chat_id]
    if len(new_mappings) == len(mappings):
        return False
    store["mappings"] = new_mappings
    _save_file(path, store)
    return True


def get_mapping_by_contact(portal: str, contact_id: int) -> dict[str, Any] | None:
    for m in list_mappings(portal):
        if m.get("contactId") == contact_id:
            return m
    return None


def get_mapping_by_chat(portal: str, chat_id: int) -> dict[str, Any] | None:
    for m in list_mappings(portal):
        if m.get("chatId") == chat_id:
            return m
    return None


# ── Pending invite codes ──

def _generate_code() -> str:
    # Ambiguous characters (0/O, 1/I) excluded: customers retype codes from
    # a monospace UI font where 0/O are indistinguishable, and a mismatch
    # reports as "invalid or expired". 32 chars, length 8.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(CODE_LENGTH))


def get_pending_codes(portal: str) -> list[dict[str, Any]]:
    store = _load_file(_store_path(portal))
    now = datetime.now(timezone.utc)
    codes = store.get("pendingCodes", [])
    # Auto-expire stale codes
    active = []
    for c in codes:
        expires = _parse_iso(c.get("expiresAt") or "")
        if expires and expires > now:
            active.append(c)
    store["pendingCodes"] = active
    _save_file(_store_path(portal), store)
    return active


def generate_code(portal: str, contact_id: int | None, contact_name: str,
                  notes_category_id: int | None, nickname: str = "",
                  employee: bool = False, user_id: int | None = None) -> dict[str, Any]:
    path = _store_path(portal)
    store = _load_file(path)
    codes = store.get("pendingCodes", [])
    # Pending codes coexist: generating a new code must NOT delete outstanding
    # codes for the same contact (previously the Christian-invite vanished when
    # Bre's was generated for the same contact). Cancel per-row by value.
    # Guard against the (unlikely) random collision instead.
    existing = {str(c.get("code") or "") for c in codes}
    code = _generate_code()
    while code in existing:
        code = _generate_code()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=CODE_EXPIRE_HOURS)
    entry = {
        "code": code,
        "contactId": contact_id,
        "contactName": contact_name,
        "notesCategoryId": notes_category_id,
        "nickname": nickname,
        "employee": employee,
        "userId": user_id,
        "portal": portal,
        "createdAt": now.isoformat(),
        "expiresAt": expires.isoformat(),
    }
    codes.append(entry)
    store["pendingCodes"] = codes
    _save_file(path, store)
    return {
        "code": code,
        "contactId": contact_id,
        "contactName": contact_name,
        "notesCategoryId": notes_category_id,
        "nickname": nickname,
        "employee": employee,
        "userId": user_id,
        "expiresAt": entry["expiresAt"],
    }


def cancel_code(portal: str, contact_id: int) -> bool:
    path = _store_path(portal)
    store = _load_file(path)
    codes = store.get("pendingCodes", [])
    new_codes = [c for c in codes if c.get("contactId") != contact_id]
    if len(new_codes) == len(codes):
        return False
    store["pendingCodes"] = new_codes
    _save_file(path, store)
    return True


def cancel_code_by_value(portal: str, code: str) -> bool:
    path = _store_path(portal)
    store = _load_file(path)
    codes = store.get("pendingCodes", [])
    target = code.upper().strip()
    new_codes = [c for c in codes if c.get("code", "").upper().strip() != target]
    if len(new_codes) == len(codes):
        return False
    store["pendingCodes"] = new_codes
    _save_file(path, store)
    return True


def verify_code(portal: str, code: str) -> dict[str, Any] | str:
    """Find and consume a pending code. Returns the mapping dict on success, or an error string."""
    store = _load_file(_store_path(portal))
    codes = store.get("pendingCodes", [])
    now = datetime.now(timezone.utc)
    for i, c in enumerate(codes):
        expires = _parse_iso(c.get("expiresAt") or "")
        if c.get("code", "").upper() == code.upper().strip() and expires and expires > now:
            entry = codes.pop(i)
            store["pendingCodes"] = codes
            # Auto-create mapping
            is_employee = c.get("employee", False)
            contact_id = c.get("contactId")
            mappings = store.get("mappings", [])

            # For non-employees: enforce max mappings per contact
            if not is_employee and contact_id is not None:
                contact_count = sum(1 for m in mappings if m.get("contactId") == contact_id)
                if contact_count >= MAX_CONTACT_MAPPINGS:
                    # Put the code back
                    codes.insert(i, entry)
                    store["pendingCodes"] = codes
                    _save_file(_store_path(portal), store)
                    return f"Contact already has the maximum of {MAX_CONTACT_MAPPINGS} linked chats"

            now_iso = now.isoformat()
            mapping = {
                "chatId": 0,
                "contactId": contact_id,
                "contactName": c.get("contactName", ""),
                "notesCategoryId": c.get("notesCategoryId"),
                "nickname": c.get("nickname", ""),
                "employee": is_employee,
                "userId": c.get("userId"),
                "createdAt": now_iso,
                "updatedAt": now_iso,
            }
            mappings.append(mapping)
            store["mappings"] = mappings
            _save_file(_store_path(portal), store)
            return mapping
    return None


def set_verify_chat_id(portal: str, contact_id: int | None, chat_id: int) -> bool:
    """Set chatId after initial verify (code flow created mapping with chatId=0).

    Finds the mapping with chatId=0 for the given contactId (or employee with chatId=0).
    """
    path = _store_path(portal)
    store = _load_file(path)
    mappings = store.get("mappings", [])
    for m in mappings:
        if contact_id is not None and m.get("contactId") == contact_id and m.get("chatId") == 0:
            m["chatId"] = chat_id
            m["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _save_file(path, store)
            return True
        if contact_id is None and m.get("employee") and m.get("chatId") == 0:
            m["chatId"] = chat_id
            m["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _save_file(path, store)
            return True
    return False


def set_nickname(portal: str, contact_id: int | None, nickname: str) -> bool:
    path = _store_path(portal)
    store = _load_file(path)
    mappings = store.get("mappings", [])
    for m in mappings:
        if contact_id is not None and m.get("contactId") == contact_id:
            m["nickname"] = nickname
            m["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _save_file(path, store)
            return True
        if contact_id is None and m.get("employee"):
            m["nickname"] = nickname
            m["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _save_file(path, store)
            return True
    return False


def set_allowed_projects(portal: str, contact_id: int | None, project_ids: list[int]) -> bool:
    """Set the allowed project IDs for a mapping. Empty list = all projects (no restriction)."""
    path = _store_path(portal)
    store = _load_file(path)
    mappings = store.get("mappings", [])
    for m in mappings:
        match = False
        if contact_id is not None and m.get("contactId") == contact_id and not m.get("employee"):
            match = True
        if contact_id is None and m.get("employee"):
            match = True
        if match:
            m["allowedProjectIds"] = project_ids
            m["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _save_file(path, store)
            return True
    return False


def get_allowed_projects(portal: str, contact_id: int | None, is_employee: bool = False) -> list[int] | None:
    """Get allowed project IDs for a mapping. Returns None if no restriction (all projects)."""
    store = _load_file(_store_path(portal))
    for m in store.get("mappings", []):
        if is_employee and m.get("employee"):
            return m.get("allowedProjectIds") or None
        if not is_employee and contact_id is not None and m.get("contactId") == contact_id:
            return m.get("allowedProjectIds") or None
    return None


# ── Usage tracking ──

def _usage_path(portal: str) -> Path:
    return _portal_dir(portal) / "usage.json"


def track_request(portal: str, chat_id: int, endpoint: str) -> None:
    path = _usage_path(portal)
    data = _load_file(path)
    key = str(chat_id)
    now = datetime.now(timezone.utc).isoformat()
    if key not in data:
        data[key] = {"requests": 0, "endpoints": {}, "firstRequest": now}
    data[key]["requests"] += 1
    data[key]["endpoints"][endpoint] = data[key]["endpoints"].get(endpoint, 0) + 1
    data[key]["lastRequest"] = now
    _save_file(path, data)


def get_usage_stats(portal: str) -> dict[str, Any]:
    return _load_file(_usage_path(portal))


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
