"""Per-user dashboard profiles (portal + CRM user id)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "user-profiles"
LEGACY_NOTES_DIR = ROOT / "data" / "dashboard-notes"
PROFILE_VERSION = 2
HIDDEN_FEED_RETENTION_DAYS = 30


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    seg = re.sub(r"[^\w.-]+", "_", str(value or "").strip())[:120]
    return seg or fallback


def profile_file_path(portal: str, user_id: str) -> Path:
    portal_dir = DATA_DIR / _safe_segment(portal, "portal")
    return portal_dir / f"{_safe_segment(user_id, 'user')}.json"


def _empty_profile() -> dict[str, Any]:
    return {
        "version": PROFILE_VERSION,
        "updatedAt": "",
        "groups": [],
        "tileLayout": {"order": [], "widths": {}, "heights": {}, "collapsed": {}, "pinned": {}},
        "calendarTiles": [],
        "calendarDisplay": {"weekStart": "sun", "timeFormat": "24h"},
        "notesTiles": [],
        "groupTemplates": [],
        "hiddenFeedKeys": [],
        "feedKeywordFilter": "",
        "bookmarkedDeals": [],
        "localKanbanTiles": [],
        "minimizedSearchTabs": [],
    }


def _clean_notes_tiles(tiles: Any) -> list[dict[str, Any]]:
    if not isinstance(tiles, list):
        return []
    out = []
    for item in tiles:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        updated_at = str(item.get("updatedAt") or "").strip()
        if not updated_at:
            updated_at = datetime.now(timezone.utc).isoformat()
        default_vm = item.get("defaultViewMode")
        out.append(
            {
                "id": str(item["id"]),
                "name": str(item.get("name") or "Notes")[:200],
                "content": str(item.get("content") or ""),
                "viewMode": "preview" if item.get("viewMode") == "preview" else "edit",
                "defaultViewMode": "preview"
                if default_vm == "preview"
                else ("edit" if default_vm == "edit" else None),
                "accent": str(item.get("accent") or "")[:32],
                "archived": bool(item.get("archived")),
                "archivedAt": str(item.get("archivedAt") or "")[:64] or None,
                "updatedAt": updated_at,
            }
        )
    return out


def _clean_calendar_display(value: Any) -> dict[str, str]:
    """Display prefs shared by all calendar tiles (week start, clock format)."""
    week_start = "mon" if isinstance(value, dict) and str(value.get("weekStart") or "").lower() in ("mon", "monday") else "sun"
    time_format = "12h" if isinstance(value, dict) and str(value.get("timeFormat") or "").lower() in ("12h", "12") else "24h"
    return {"weekStart": week_start, "timeFormat": time_format}


def _clean_calendar_tiles(tiles: Any) -> list[dict[str, Any]]:
    if not isinstance(tiles, list):
        return []
    out = []
    for item in tiles:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        cleaned = {
            "id": str(item["id"]),
            "name": str(item.get("name") or "Calendar")[:200],
            "feedUrl": str(item.get("feedUrl") or "")[:4000],
            "timezone": str(item.get("timezone") or "")[:80],
            "viewYear": int(item.get("viewYear") or 0) or None,
            "viewMonth": int(item.get("viewMonth") or 0) or None,
            "viewDay": int(item.get("viewDay") or 0) or None,
        }
        if cleaned["viewYear"] is None:
            del cleaned["viewYear"]
        if cleaned["viewMonth"] is None:
            del cleaned["viewMonth"]
        if cleaned["viewDay"] is None:
            del cleaned["viewDay"]
        # Preserve calendar view mode and DB source reference
        view = str(item.get("view") or "").strip()
        if view and view in ("month", "week", "day", "agenda"):
            cleaned["view"] = view
        source_id = item.get("sourceId")
        if source_id is not None:
            cleaned["sourceId"] = int(source_id) if str(source_id).isdigit() else None
        out.append(cleaned)
    return out


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


def _hidden_feed_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=HIDDEN_FEED_RETENTION_DAYS)


def _clean_hidden_feed_snapshot(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    out: dict[str, Any] = {}
    if snapshot.get("id") is not None:
        out["id"] = snapshot["id"]
    title = str(snapshot.get("title") or "").strip()
    if title:
        out["title"] = title[:300]
    text = str(snapshot.get("text") or "").strip()
    if text:
        out["text"] = text[:500]
    author = str(snapshot.get("author") or "").strip()
    if author:
        out["author"] = author[:200]
    date = str(snapshot.get("date") or "").strip()
    if date:
        out["date"] = date[:80]
    return out or None


def _clean_hidden_feed_keys(hidden: Any) -> list[dict[str, Any]]:
    """Normalize hidden CRM notification entries; drop items older than retention."""
    if not isinstance(hidden, list):
        return []
    cutoff = _hidden_feed_cutoff()
    now = datetime.now(timezone.utc).isoformat()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in hidden:
        key = ""
        hidden_at = ""
        snapshot: dict[str, Any] | None = None
        if isinstance(item, str):
            key = item.strip()
            hidden_at = now
        elif isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            hidden_at = str(item.get("hiddenAt") or "").strip() or now
            snapshot = _clean_hidden_feed_snapshot(item.get("snapshot"))
        if not key or key in seen:
            continue
        hidden_dt = _parse_iso_datetime(hidden_at)
        if hidden_dt is None:
            hidden_at = now
            hidden_dt = _parse_iso_datetime(hidden_at)
        if hidden_dt and hidden_dt < cutoff:
            continue
        seen.add(key)
        row: dict[str, Any] = {"key": key[:500], "hiddenAt": hidden_at[:80]}
        if snapshot:
            row["snapshot"] = snapshot
        out.append(row)
    return out


def _clean_groups(groups: Any) -> list[dict[str, Any]]:
    if not isinstance(groups, list):
        return []
    out = []
    for g in groups:
        if not isinstance(g, dict) or not g.get("id"):
            continue
        row = {k: v for k, v in g.items() if k not in ("opportunities", "_el", "_setFiltersCollapsed")}
        row["id"] = str(row["id"])
        out.append(row)
    return out


def _migrate_legacy_notes(portal: str, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("notesTiles"):
        return profile
    legacy_path = LEGACY_NOTES_DIR / _safe_segment(portal, "portal") / f"{_safe_segment(user_id, 'user')}.json"
    if not legacy_path.is_file():
        return profile
    try:
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        tiles = data.get("tiles") if isinstance(data, dict) else []
        if isinstance(tiles, list) and tiles:
            profile["notesTiles"] = _clean_notes_tiles(tiles)
    except (json.JSONDecodeError, OSError):
        pass
    return profile


def load_user_profile(portal: str, user_id: str) -> dict[str, Any]:
    path = profile_file_path(portal, user_id)
    if not path.is_file():
        profile = _empty_profile()
        return _migrate_legacy_notes(portal, user_id, profile)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _migrate_legacy_notes(portal, user_id, _empty_profile())
    if not isinstance(data, dict):
        return _empty_profile()
    profile = _empty_profile()
    profile["groups"] = _clean_groups(data.get("groups"))
    layout = data.get("tileLayout")
    if isinstance(layout, dict):
        profile["tileLayout"] = {
            "order": list(layout.get("order") or []),
            "widths": dict(layout.get("widths") or {}),
            "heights": dict(layout.get("heights") or {}),
            "collapsed": dict(layout.get("collapsed") or {}),
            "pinned": dict(layout.get("pinned") or {}),
        }
        # Preserve tasksColumns mode
        if "tasksColumns" in layout:
            profile["tileLayout"]["tasksColumns"] = bool(layout["tasksColumns"])
    profile["calendarTiles"] = _clean_calendar_tiles(data.get("calendarTiles"))
    profile["calendarDisplay"] = _clean_calendar_display(data.get("calendarDisplay"))
    profile["notesTiles"] = _clean_notes_tiles(data.get("notesTiles"))
    templates = data.get("groupTemplates")
    profile["groupTemplates"] = templates if isinstance(templates, list) else []
    profile["hiddenFeedKeys"] = _clean_hidden_feed_keys(data.get("hiddenFeedKeys"))
    profile["feedKeywordFilter"] = str(data.get("feedKeywordFilter") or "")[:500]
    profile["bookmarkedDeals"] = data.get("bookmarkedDeals") if isinstance(data.get("bookmarkedDeals"), list) else []
    profile["localKanbanTiles"] = data.get("localKanbanTiles") if isinstance(data.get("localKanbanTiles"), list) else []
    profile["minimizedSearchTabs"] = data.get("minimizedSearchTabs") if isinstance(data.get("minimizedSearchTabs"), list) else []
    profile["updatedAt"] = str(data.get("updatedAt") or "")
    return _migrate_legacy_notes(portal, user_id, profile)


def save_user_profile(portal: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = profile_file_path(portal, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    updated_at = str(payload.get("updatedAt") or "").strip()
    if not updated_at:
        updated_at = datetime.now(timezone.utc).isoformat()
    cleaned = {
        "version": PROFILE_VERSION,
        "updatedAt": updated_at,
        "groups": _clean_groups(payload.get("groups")),
        "tileLayout": payload.get("tileLayout")
        if isinstance(payload.get("tileLayout"), dict)
        else _empty_profile()["tileLayout"],
        "calendarTiles": _clean_calendar_tiles(payload.get("calendarTiles")),
        "calendarDisplay": _clean_calendar_display(payload.get("calendarDisplay")),
        "notesTiles": _clean_notes_tiles(payload.get("notesTiles")),
        "groupTemplates": payload.get("groupTemplates")
        if isinstance(payload.get("groupTemplates"), list)
        else [],
        "hiddenFeedKeys": _clean_hidden_feed_keys(payload.get("hiddenFeedKeys")),
        "feedKeywordFilter": str(payload.get("feedKeywordFilter") or "")[:500],
        "bookmarkedDeals": payload.get("bookmarkedDeals") if isinstance(payload.get("bookmarkedDeals"), list) else [],
        "localKanbanTiles": payload.get("localKanbanTiles") if isinstance(payload.get("localKanbanTiles"), list) else [],
        "minimizedSearchTabs": payload.get("minimizedSearchTabs") if isinstance(payload.get("minimizedSearchTabs"), list) else [],
    }
    path.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    return cleaned