"""Per-user (and cross-user) presence / status / basic DMs for the dashboard (portal + CRM user id).

Modeled closely after user_profile_store.py for consistency (safe segmenting, per-portal dirs,
load/save, version, retention where applicable).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "user-presence"
MESSAGES_DIR = ROOT / "data" / "presence-messages"
PRESENCE_VERSION = 1

# Modest cap for DM history per conversation in v1 (keeps files small; we return the most recent N)
DM_HISTORY_LIMIT = 100


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    seg = re.sub(r"[^\w.-]+", "_", str(value or "").strip())[:120]
    return seg or fallback


def presence_file_path(portal: str, user_id: str) -> Path:
    portal_dir = DATA_DIR / _safe_segment(portal, "portal")
    return portal_dir / f"{_safe_segment(user_id, 'user')}.json"


def _conversation_key(a: str, b: str) -> str:
    """Canonical key for a conversation between two users (sorted)."""
    a = _safe_segment(str(a or ""), "u")
    b = _safe_segment(str(b or ""), "u")
    if a > b:
        a, b = b, a
    return f"{a}__{b}"


def conversation_file_path(portal: str, user_a: str, user_b: str) -> Path:
    key = _conversation_key(user_a, user_b)
    portal_dir = MESSAGES_DIR / _safe_segment(portal, "portal")
    return portal_dir / f"{key}.json"


def _empty_presence() -> dict[str, Any]:
    return {
        "version": PRESENCE_VERSION,
        "updatedAt": "",
        "status": "",  # template string or custom text (shown next to name)
        "inferred": False,  # True if status was auto-derived (not manually set)
        "autoStatus": "",  # auto-derived activity (e.g. "Working on: Deal X"), displayed separately
        "lastHeartbeat": "",  # last client heartbeat (for online / idle calc)
        "lastCrmActivity": "",  # last time a CRM-ish request was proxied for this user
        "lastDashboardActivity": "",  # last explicit dashboard activity / heartbeat
        "lastTabCount": 0,  # number of open dashboard tabs (for cross-tab close handling)
        "lastReadDms": {},  # { other_user_id: iso_or_ms_last_read_time, ... } for cross-device DM read state
    }


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_user_presence(portal: str, user_id: str) -> dict[str, Any]:
    path = presence_file_path(portal, user_id)
    if not path.is_file():
        return _empty_presence()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_presence()
    if not isinstance(data, dict):
        return _empty_presence()

    presence = _empty_presence()
    presence["status"] = str(data.get("status") or "")[:200]  # modest cap for display
    presence["inferred"] = bool(data.get("inferred") or False)
    presence["autoStatus"] = str(data.get("autoStatus") or "")[:200]
    presence["lastHeartbeat"] = str(data.get("lastHeartbeat") or "")[:80]
    presence["lastCrmActivity"] = str(data.get("lastCrmActivity") or "")[:80]
    presence["lastDashboardActivity"] = str(data.get("lastDashboardActivity") or "")[:80]
    presence["lastTabCount"] = int(data.get("lastTabCount") or 0)
    presence["updatedAt"] = str(data.get("updatedAt") or "")
    lrd = data.get("lastReadDms") or {}
    presence["lastReadDms"] = lrd if isinstance(lrd, dict) else {}
    return presence


def save_user_presence(portal: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = presence_file_path(portal, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    updated_at = str(payload.get("updatedAt") or "").strip()
    if not updated_at:
        updated_at = _now_iso()

    cleaned = {
        "version": PRESENCE_VERSION,
        "updatedAt": updated_at,
        "status": str(payload.get("status") or "")[:200],
        "inferred": bool(payload.get("inferred") or False),
        "autoStatus": str(payload.get("autoStatus") or "")[:200],
        "lastHeartbeat": str(payload.get("lastHeartbeat") or "")[:80],
        "lastCrmActivity": str(payload.get("lastCrmActivity") or "")[:80],
        "lastDashboardActivity": str(payload.get("lastDashboardActivity") or "")[:80],
        "lastTabCount": max(0, int(payload.get("lastTabCount") or 0)),
    }
    lrd = payload.get("lastReadDms") or {}
    if isinstance(lrd, dict):
        cleaned["lastReadDms"] = {str(k): str(v)[:80] for k, v in lrd.items() if k and v}
    else:
        cleaned["lastReadDms"] = {}
    path.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    return cleaned


def touch_heartbeat(portal: str, user_id: str, offline: bool = False, visible: bool = False, tabs: int | None = None) -> dict[str, Any]:
    """Update lastHeartbeat for the user. Called on client heartbeats.
    Only bumps lastDashboardActivity when visible=True (tab is focused),
    so auto-status expires naturally during background tab inactivity.
    Pass offline=True to clear the heartbeat (tab/window closed).
    Pass tabs (int >= 0) to track the open-tab count for cross-tab
    close handling — when tabs drops to 0 the user is treated as
    offline regardless of how recently a heartbeat landed."""
    existing = load_user_presence(portal, user_id)
    now = _now_iso()
    if offline:
        existing["lastHeartbeat"] = ""
    else:
        existing["lastHeartbeat"] = now
        if visible:
            existing["lastDashboardActivity"] = now
    if tabs is not None:
        existing["lastTabCount"] = max(0, int(tabs))
    return save_user_presence(portal, user_id, existing)


def touch_crm_activity(portal: str, user_id: str) -> dict[str, Any]:
    """Record that this user performed a CRM action (via the dashboard proxy)."""
    existing = load_user_presence(portal, user_id)
    now = _now_iso()
    existing["lastCrmActivity"] = now
    # Also bump dashboard activity since the action came through the dashboard
    existing["lastDashboardActivity"] = now
    return save_user_presence(portal, user_id, existing)


def set_status(portal: str, user_id: str, status: str = None, inferred: bool = False, autoStatus: str = None) -> dict[str, Any]:
    """Set the user's current status string (template or custom).
    Pass status=None to leave the existing status field unchanged.
    Pass status="" to clear the manual status (set to Online).
    Pass autoStatus (including "") to update the separate auto-derived status field.
    Pass autoStatus=None to leave the existing autoStatus unchanged.
    Bumps lastDashboardActivity so autoStatus expiry is reset on explicit user actions.
    """
    existing = load_user_presence(portal, user_id)
    now = _now_iso()
    if status is not None:
        existing["status"] = str(status or "")[:200]
        existing["inferred"] = bool(inferred)
    if autoStatus is not None:
        existing["autoStatus"] = str(autoStatus or "")[:200]
    existing["updatedAt"] = now
    existing["lastDashboardActivity"] = now
    return save_user_presence(portal, user_id, existing)


def clear_auto_status(portal: str, user_id: str) -> None:
    """Clear autoStatus without bumping lastDashboardActivity.
    Used by the server to clean up stale auto-status when filtering the response,
    so the stale value doesn't persist on disk across sessions."""
    existing = load_user_presence(portal, user_id)
    if existing.get("autoStatus"):
        existing["autoStatus"] = ""
        save_user_presence(portal, user_id, existing)


def clean_stale_presence_records(portal: str) -> None:
    """Iterate all presence records for this portal and clear lastHeartbeat
    + autoStatus for records with heartbeats older than 3 hours.
    Handles the case where the browser was closed without sending beforeunload."""
    from datetime import datetime, timezone, timedelta
    portal_dir = DATA_DIR / _safe_segment(portal, "portal")
    if not portal_dir.is_dir():
        return
    now = datetime.now(timezone.utc)
    stale = timedelta(hours=3)
    for p in portal_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            hb_str = data.get("lastHeartbeat") or ""
            if not hb_str:
                continue
            try:
                hb_dt = datetime.fromisoformat(hb_str)
            except (ValueError, TypeError):
                continue
            if now - hb_dt > stale:
                data["lastHeartbeat"] = ""
                data["autoStatus"] = ""
                p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            continue
def get_portal_presence_snapshot(portal: str) -> dict[str, dict[str, Any]]:
    """Return {user_id: presence_dict, ...} for all known presence records under this portal.
    Used by the /api/presence endpoint to build the live overlay.
    Scans the per-portal directory (small number of users).
    """
    portal_dir = DATA_DIR / _safe_segment(portal, "portal")
    result: dict[str, dict[str, Any]] = {}
    if not portal_dir.is_dir():
        return result
    for p in portal_dir.glob("*.json"):
        try:
            uid = p.stem  # the safe_segment(user) part; caller maps back if needed
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Return a clean view (no secrets)
                result[uid] = {
                    "status": str(data.get("status") or "")[:200],
                    "inferred": bool(data.get("inferred") or False),
                    "autoStatus": str(data.get("autoStatus") or "")[:200],
                    "lastHeartbeat": str(data.get("lastHeartbeat") or "")[:80],
                    "lastCrmActivity": str(data.get("lastCrmActivity") or "")[:80],
                    "lastDashboardActivity": str(data.get("lastDashboardActivity") or "")[:80],
                    "lastTabCount": max(0, int(data.get("lastTabCount") or 0)),
                    "updatedAt": str(data.get("updatedAt") or ""),
                }
        except (json.JSONDecodeError, OSError):
            continue
    return result


# ---------------- DM / conversation helpers (simple append-only for v1) ----------------

def _load_conversation(path: Path, limit: int = DM_HISTORY_LIMIT) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        msgs = data.get("messages") if isinstance(data, dict) else []
        if not isinstance(msgs, list):
            return []
        # Return the most recent N
        return msgs[-limit:]
    except (json.JSONDecodeError, OSError):
        return []


def _save_conversation(path: Path, messages: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep only the last DM_HISTORY_LIMIT to bound growth
    trimmed = messages[-DM_HISTORY_LIMIT:]
    payload = {"version": PRESENCE_VERSION, "messages": trimmed}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_dm(portal: str, from_id: str, to_id: str, text: str, reply_to: str = None, reply_text: str = None) -> dict[str, Any]:
    """Append a message to the conversation between from_id and to_id.
    Optional reply_to is the ts of the message being replied to.
    reply_text is an embedded snippet of the replied-to message (for self-contained quotes).
    Returns the saved message record.
    """
    if not from_id or not to_id or from_id == to_id:
        return {}
    path = conversation_file_path(portal, from_id, to_id)
    msgs = _load_conversation(path)

    # Robustness for reply context: if only a reply_to (ts pointer) was provided
    # (e.g. older clients or direct calls), resolve the text snippet right now
    # while the original message is still in the loaded recent list. This ensures
    # the stored record for the reply message *always* carries an embedded
    # reply_text, so the history renderer can show the quoted context reliably
    # even after history is trimmed or on later re-opens (including for sent
    # messages in the bubbles). Client-provided reply_text is preferred.
    if reply_to and not reply_text:
        for cand in reversed(msgs):
            if cand and str(cand.get("ts")) == str(reply_to):
                reply_text = str(cand.get("text") or "")[:100]
                break

    msg = {
        "from": str(from_id),
        "to": str(to_id),
        "text": str(text or "")[:2000],  # modest per-message cap
        "ts": _now_iso(),
        "reply_to": reply_to,
        "reply_text": reply_text,
        "read": False,
        "read_at": None,
    }
    msgs.append(msg)
    _save_conversation(path, msgs)
    return msg


def get_conversation(portal: str, user_a: str, user_b: str, limit: int = DM_HISTORY_LIMIT, offset: int = 0) -> tuple[list[dict[str, Any]], bool]:
    """Return a page of messages between user_a and user_b plus a has_more flag.

    offset=0 returns the most recent `limit` messages.
    offset=N skips the N newest messages and returns the next `limit`.
    Returns (page, has_more) where has_more is True if older messages exist.
    """
    if not user_a or not user_b:
        return [], False
    path = conversation_file_path(portal, user_a, user_b)
    msgs = _load_conversation(path)
    total = len(msgs)
    if offset >= total:
        return [], False
    end = total - offset
    start = max(0, end - limit)
    page = msgs[start:end]
    has_more = start > 0
    return page, has_more


def get_recent_dms_for_user(portal: str, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the latest message per conversation partner, sorted by recency.
    Ensures every conversation appears in the inbox even if one thread has many messages.
    """
    portal_dir = MESSAGES_DIR / _safe_segment(portal, "portal")
    if not portal_dir.is_dir():
        return []
    uid = str(user_id)
    per_other: dict[str, dict[str, Any]] = {}
    for p in portal_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            msgs = data.get("messages") if isinstance(data, dict) else []
            if isinstance(msgs, list):
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    f = m.get("from")
                    t = m.get("to")
                    if f == uid:
                        other = str(t) if t is not None else None
                    elif t == uid:
                        other = str(f) if f is not None else None
                    else:
                        continue
                    if other is None:
                        continue
                    ts = str(m.get("ts") or "")
                    prev = per_other.get(other)
                    if prev is None or ts > str(prev.get("ts") or ""):
                        per_other[other] = m
        except (json.JSONDecodeError, OSError):
            continue
    out = sorted(per_other.values(), key=lambda m: str(m.get("ts") or ""), reverse=True)
    return out[:limit]


def clear_conversation(portal: str, user_a: str, user_b: str) -> None:
    """Delete the persisted conversation history between the two users (both directions use same file)."""
    if not user_a or not user_b:
        return
    path = conversation_file_path(portal, user_a, user_b)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
    # Clear the reader's last-read marker for this peer so a cleared conversation doesn't retain stale cutoff on reload/other devices
    try:
        pres = load_user_presence(portal, user_a)
        if isinstance(pres.get("lastReadDms"), dict):
            pres["lastReadDms"].pop(str(user_b), None)
            pres["updatedAt"] = _now_iso()
            save_user_presence(portal, user_a, pres)
    except Exception:
        pass


def mark_messages_read(portal: str, reader_id: str, other_id: str) -> None:
    """Mark all incoming messages (from other_id) as read for the reader.
    Persists the change so the sender can see read receipts.
    """
    if not reader_id or not other_id:
        return
    path = conversation_file_path(portal, reader_id, other_id)
    msgs = _load_conversation(path)
    modified = False
    for m in msgs:
        if isinstance(m, dict) and str(m.get("from")) == str(other_id) and not m.get("read"):
            m["read"] = True
            m["read_at"] = _now_iso()
            modified = True
    if modified:
        _save_conversation(path, msgs)


def load_user_last_read_dms(portal: str, user_id: str) -> dict[str, str]:
    """Return the map of last-read timestamps (per other user) for DM read-state persistence.
    Values are stored as ISO or numeric strings; client normalizes to ms for comparison.
    """
    pres = load_user_presence(portal, user_id)
    d = pres.get("lastReadDms") or {}
    if not isinstance(d, dict):
        return {}
    return {str(k): str(v) for k, v in d.items() if k and v}


def set_last_read_dm(portal: str, user_id: str, other_id: str, at: str = None) -> dict[str, Any]:
    """Record that the user has read up to 'now' (or provided ts) for the conversation with other_id.
    This makes DM read/unread state (inbox shading, unread counts) survive across devices/logins.
    """
    if not user_id or not other_id:
        return {}
    existing = load_user_presence(portal, user_id)
    if not isinstance(existing.get("lastReadDms"), dict):
        existing["lastReadDms"] = {}
    ts = at or _now_iso()
    existing["lastReadDms"][str(other_id)] = str(ts)[:80]
    existing["updatedAt"] = _now_iso()
    return save_user_presence(portal, user_id, existing)

