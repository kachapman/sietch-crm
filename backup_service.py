"""Scheduled backup service for Sietch CRM.

Follows the notification_dispatcher.py pattern: threading.Thread + Event.
Emits per-part JSON export bundles on a schedule, records run history,
and notifies admins. Proton Drive / rclone picks artifacts up from
BACKUP_DIR externally.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sietch.backup")

# ── Configuration from env vars ──────────────────────────────────────────────
BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "true").lower() == "true"
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "86400"))  # seconds (24h)
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "data/backups"))
# Host-side path Proton Drive / rclone scans (defaults to the
# in-container path; set on the host to surface the bind-mounted
# absolute path in the UI). Falls back to BACKUP_DIR for non-Docker runs.
BACKUP_DIR_HOST = os.getenv("BACKUP_DIR_HOST", "") or str(BACKUP_DIR)

# Database connection (from env, same as db.py). Only used to read
# `backup_config` from system_settings (admin-console settings) and to
# record history + push backup events. Restore comes from the per-part
# JSON bundles emitted by this scheduler (see run_backup). The only
# pg_dump left is the pre-restore snapshot in _run_pg_dump, called from
# the SQL import handler.
DB_HOST = os.getenv("DB_HOST", "db")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "sietch_crm")
DB_USER = os.getenv("DB_USER", "sietch")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# NOTE: BACKUP_DIR defaults inside data/ on purpose — in Docker, /app/data
# is the persistent volume, so backups survive container recreates. The
# `_build_export_zip_to_path` helper writes via temp file + atomic rename
# so partially-written artifacts are never visible to Proton/rclone pickup.

# ── State ────────────────────────────────────────────────────────────────────
_backup_lock = threading.Lock()
_last_backup: dict[str, Any] = {"status": "none", "ts": None, "size": None, "error": None}


def _load_config_from_db() -> dict:
    """Load backup config from system_settings table."""
    try:
        import db
        row = db.query_one("SELECT value FROM system_settings WHERE key = %s", ("backup_config",))
        if row and row.get("value"):
            return json.loads(row["value"])
    except Exception:
        pass
    return {}


def _save_config_to_db(config: dict) -> None:
    """Save backup config to system_settings table."""
    import db
    db.execute(
        """INSERT INTO system_settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        ("backup_config", json.dumps(config)),
    )


def _get_notify_prefs() -> dict:
    """Load backup notification preferences."""
    try:
        import db
        rows = db.query_dicts(
            "SELECT notification_type, enabled FROM notification_preferences WHERE notification_type LIKE 'backup_%'"
        )
        prefs = {}
        for r in rows:
            prefs[r["notification_type"]] = r["enabled"]
        return prefs
    except Exception:
        return {}


def _save_notify_prefs(prefs: dict) -> None:
    """Save backup notification preferences."""
    import db
    for key, val in prefs.items():
        db.execute(
            """INSERT INTO notification_preferences (user_id, notification_type, enabled)
               VALUES (0, %s, %s)
               ON CONFLICT (user_id, notification_type) DO UPDATE SET enabled = EXCLUDED.enabled""",
            (key, val),
        )


def get_backup_status() -> dict:
    """Get current backup status for the admin UI."""
    config = _load_config_from_db()
    last = _last_backup.copy()
    next_ts = None
    if config.get("enabled") and last.get("ts"):
        next_ts = last["ts"] + config.get("interval", BACKUP_INTERVAL)
    return {
        "enabled": config.get("enabled", BACKUP_ENABLED),
        "interval": config.get("interval", BACKUP_INTERVAL),
        "retention_days": config.get("retention_days", BACKUP_RETENTION_DAYS),
        "json_parts": config.get("json_parts", DEFAULT_EXPORT_PARTS),
        "backup_dir": str(BACKUP_DIR),
        "backup_dir_host": BACKUP_DIR_HOST,
        "expected_export_parts": EXPECTED_EXPORT_PARTS,
        "last_backup": last,
        "next_backup_ts": next_ts,
    }


def get_backup_history(limit: int = 20) -> list[dict]:
    """Get recent backup history from infra ring buffer."""
    try:
        import db
        rows = db.query_dicts(
            "SELECT key, value FROM system_settings WHERE key LIKE 'backup_history_%' ORDER BY key DESC LIMIT %s",
            (limit,),
        )
        return [json.loads(r["value"]) for r in rows if r.get("value")]
    except Exception:
        return []


def _record_backup(entry: dict) -> None:
    """Record a backup entry in system_settings."""
    import db
    ts = entry.get("ts", datetime.now(timezone.utc).isoformat())
    key = f"backup_history_{ts[:19].replace('-', '').replace('T', '').replace(':', '')}"
    db.execute(
        """INSERT INTO system_settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, json.dumps(entry)),
    )
    # Keep only last 50 entries
    db.execute(
        """DELETE FROM system_settings WHERE key LIKE 'backup_history_%'
           AND key NOT IN (
               SELECT key FROM system_settings WHERE key LIKE 'backup_history_%' ORDER BY key DESC LIMIT 50
           )"""
    )


def _notify_admins(success: bool, details: str) -> None:
    """Send backup notifications to admin users via email and/or Telegram."""
    prefs = _get_notify_prefs()
    notify_email_success = prefs.get("backup_email_success", True)
    notify_email_failure = prefs.get("backup_email_failure", True)
    notify_tg_success = prefs.get("backup_telegram_success", True)
    notify_tg_failure = prefs.get("backup_telegram_failure", True)

    want_email = (success and notify_email_failure) or (not success and notify_email_failure)
    want_tg = (success and notify_tg_success) or (not success and notify_tg_failure)

    # Override: success notifications
    if success:
        want_email = notify_email_success
        want_tg = notify_tg_success

    try:
        import db
        admins = db.query_dicts("SELECT id, email FROM users WHERE is_admin = TRUE AND is_active = TRUE")
    except Exception:
        return

    for admin in admins:
        # Email notification
        if want_email and admin.get("email"):
            try:
                import smtp_client
                icon = "✅" if success else "❌"
                subject = f"[Sietch CRM] Backup {'Succeeded' if success else 'FAILED'}"
                body = (
                    f"<p>{icon} <b>Sietch CRM Backup {'OK' if success else 'FAILED'}</b></p>"
                    f"<pre style='font-size:0.85rem;'>{details}</pre>"
                )
                smtp_client.send_email(admin["email"], subject, body)
            except Exception as e:
                logger.warning("Failed to send backup email to %s: %s", admin.get("email"), e)

        # Telegram notification
        if want_tg:
            try:
                chat_id = _get_chat_id_for_user(admin["id"])
                if chat_id:
                    _send_tg(chat_id, details, success)
            except Exception as e:
                logger.warning("Failed to send backup Telegram to user %s: %s", admin.get("id"), e)


def _get_chat_id_for_user(user_id: int) -> int | None:
    """Get Telegram chat_id for a user from crm_bot_store."""
    try:
        from crm_bot_store import list_mappings
        mappings = list_mappings("sietch") or []
        for m in mappings:
            if m.get("userId") == user_id and m.get("chatId"):
                return int(m["chatId"])
    except Exception:
        pass
    return None


def _send_tg(chat_id: int, text: str, success: bool) -> None:
    """Send a Telegram message."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    import httpx
    icon = "✅" if success else "❌"
    msg = f"{icon} {text}"
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


# ── Backup execution ─────────────────────────────────────────────────────────

def run_backup() -> dict:
    """Execute one scheduled JSON export run: write the configured parts to disk,
    record history, push the admin-channel event for the live log, and notify.

    This is the **only** scheduled run. SQL pg_dump + data archives were
    removed — restore comes from the per-part JSON bundles emitted here.
    Proton Drive / rclone is expected to pick artifacts up from
    BACKUP_DIR externally (host-side path surfaced via BACKUP_DIR_HOST).
    """
    if not _backup_lock.acquire(blocking=False):
        return {"ok": False, "error": "Backup already running"}

    start = time.time()
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "artifacts": {},       # {part: {path, size, sha256, ok, error}}
        "size": 0,             # total bytes across all artifacts
        "duration": None,
        "error": None,
    }

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # Unique per run: second-resolution slug + pid so overlapping or
        # rapid-succession runs can never collide on filenames.
        ts_slug = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S") + f"-p{os.getpid()}"

        # Read the part list from the admin console config (DB), with a
        # safe default. validate() below is the single source of truth for
        # which parts are allowed.
        config = _load_config_from_db()
        parts = validate_parts(config.get("json_parts") or DEFAULT_EXPORT_PARTS)
        if not parts:
            entry["status"] = "failed"
            entry["error"] = "no export parts selected in backup config"
            _record_backup(entry)
            _notify_admins(False, "Backup failed\n\nError: no export parts selected")
            return {"ok": False, "error": entry["error"]}

        any_failed = False
        for part in parts:
            out_path = BACKUP_DIR / f"sietch-export-{part}-{ts_slug}.zip"
            try:
                size, sha = _build_export_zip_to_path(part, ts_slug, out_path)
                entry["artifacts"][part] = {
                    "path": str(out_path),
                    "size": size,
                    "sha256": sha,
                    "ok": True,
                    "error": None,
                }
            except Exception as e:
                any_failed = True
                entry["artifacts"][part] = {
                    "path": str(out_path),
                    "size": 0,
                    "sha256": None,
                    "ok": False,
                    "error": str(e),
                }
                logger.warning("JSON export part %s failed (non-fatal): %s", part, e)
            entry["size"] += entry["artifacts"][part]["size"]

        # Rotation: keep the most-recent N-day window on disk.
        try:
            _cleanup_old_backups(config.get("retention_days", BACKUP_RETENTION_DAYS))
        except Exception as e:
            logger.warning("Backup cleanup failed: %s", e)

        # Publish a "back-up completed" event on the admin channel for the
        # live log in the console. Only admins connect to this channel.
        try:
            from event_bus import event_bus
            event_bus.publish("backup:completed", entry, user_ids=None,
                              admin_only=True)
        except Exception as e:
            logger.debug("backup event publish failed: %s", e)

        entry["status"] = "success" if not any_failed else "partial"
        entry["duration"] = time.time() - start
        _record_backup(entry)
        _notify_admins(
            not any_failed,
            _summary_for(entry, ts_slug, parts),
        )

        return {
            "ok": not any_failed,
            "size": entry["size"],
            "duration": entry["duration"],
            "artifacts": entry["artifacts"],
        }

    except Exception as e:
        entry["status"] = "failed"
        entry["error"] = str(e)
        entry["duration"] = time.time() - start
        _record_backup(entry)
        _notify_admins(False, f"Backup failed\n\nError: {e}\nDuration: {entry['duration']:.0f}s")
        return {"ok": False, "error": str(e)}
    finally:
        if _backup_lock.locked():
            _backup_lock.release()


def _summary_for(entry: dict, ts_slug: str, parts: list[str]) -> str:
    """Format the admin email/Telegram summary from a run entry."""
    failed = [p for p, a in entry.get("artifacts", {}).items() if not a.get("ok")]
    size_mb = entry.get("size", 0) / 1024 / 1024
    lines = [
        f"Run: {ts_slug}",
        f"Parts: {', '.join(parts)}",
        f"Total: {size_mb:.1f} MB",
        f"Duration: {entry.get('duration', 0):.0f}s",
    ]
    if failed:
        lines.append(f"Failed: {', '.join(failed)}")
    if entry.get("error"):
        lines.append(f"Error: {entry['error']}")
    return "\n".join(lines)


def _run_pg_dump(output_path: Path) -> None:
    """Run pg_dump and compress output."""
    env = {**os.environ, "PGPASSWORD": DB_PASSWORD}
    proc = subprocess.run(
        [
            "pg_dump",
            "-h", DB_HOST,
            "-p", DB_PORT,
            "-U", DB_USER,
            "-d", DB_NAME,
            "--no-owner",
            "--no-privileges",
            "-Fp",  # plain SQL
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump exited {proc.returncode}: {proc.stderr[:500]}")

    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write(proc.stdout)


def sanitize_archive_member(name: str, prefix: str = "files/") -> str | None:
    """Validate a zip/tar member path for extraction (zip-slip guard).

    Returns the path relative to ``prefix``, or None if the member must
    be skipped: absolute paths, ``..`` components, drive letters, and
    empty remainders are all rejected. Pure helper, unit-tested.
    """
    if not name or not name.startswith(prefix):
        return None
    rel = name[len(prefix):]
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    if re.match(r"^[A-Za-z]:", rel):
        return None
    return rel


# Parts servable over HTTP by the universal export (?part=). history blobs
# stay on the scheduled tar path (see server._EXPORT_PART_FILES).
EXPECTED_EXPORT_PARTS = ["db-core", "files-documents", "files-state"]


def validate_parts(raw) -> list[str]:
    """Sanitize a configured part list. Returns a clean list, dropping
    unknown entries and the legacy "all" sentinel. Empty list = no parts.
    """
    if raw is None:
        return []
    items: list[str]
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, list):
        items = [str(p).strip() for p in raw if str(p).strip()]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for p in items:
        if p == "all":
            for allowed in EXPECTED_EXPORT_PARTS:
                if allowed not in seen:
                    seen.add(allowed)
                    out.append(allowed)
        elif p in EXPECTED_EXPORT_PARTS and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# Mirror of the per-part subdir map in server.py (kept here so the
# scheduler doesn't have to import the whole server module — the file
# lists are the public contract for the JSON export layout).
_PART_FILE_DIRS: dict[str, list[tuple[str, str]]] = {
    "files-documents": [
        ("documents", "documents"),
        ("photos", "photos"),
        ("avatars", "avatars"),
        ("contact-avatars", "contact-avatars"),
        ("branding", "branding"),
        ("logos", "logos"),
    ],
    "files-state": [
        ("user-profiles", "user-profiles"),
        ("bot-customers", "bot-customers"),
        ("presence-messages", "presence-messages"),
        ("user-presence", "user-presence"),
        ("event-logs", "event-logs"),
        ("dashboard-notes", "dashboard-notes"),
    ],
}


def _build_export_zip_to_path(part: str, ts_slug: str, out_path: Path) -> tuple[int, str]:
    """Build a single export ZIP for ``part`` straight to disk. Returns (size, sha256).

    Mirrors `_handle_export_universal` in server.py: emits a manifest.json,
    sensitive-column stripping on every table, zip-slip guard, scanner
    alias for files-state. Streams via a temp file and `shutil.move`s into
    place so the artifact is never partially visible to Proton/rclone
    pickup (atomic rename).
    """
    # Table list + per-part sensitive stripping: same shape the browser
    # export uses. Duplicated rather than imported to keep this module
    # importable from anywhere (server.py has import-time side effects).
    _EXPORT_TABLES = _get_export_tables()
    _SENSITIVE = {
        "password_hash", "password_salt", "password_encrypted",
        "smtp_password_encrypted", "oauth_access_token", "oauth_refresh_token",
        "oauth_graph_access_token", "oauth_graph_refresh_token",
        "caldav_password_encrypted", "webhook_secret", "token",
    }

    set_id = ts_slug.split("T")[0]  # YYYY-MM-DD
    manifest = {
        "version": "1.1",
        "part": part,
        "backup_set_id": set_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
        "files": {},
    }
    if part == "db-core":
        manifest["credential_notice"] = (
            f"Sensitive credential columns were excluded from this export "
            f"({sorted(_SENSITIVE)}). After restoring, re-enter mail account "
            "passwords / OAuth grants and reset user passwords. Tables "
            "'password_reset_tokens' and 'twofa_challenges' were not "
            "exported (ephemeral secrets). 'sessions' is exported empty."
        )

    import db
    data_dir = Path("data")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Tables
            for table_name, sql in _EXPORT_TABLES:
                if sql is None:
                    manifest["tables"][table_name] = {"row_count": 0, "excluded": True}
                    continue
                if part != "db-core":
                    continue  # only db-core carries tables
                try:
                    rows = db.query_dicts(sql)
                    clean_rows = []
                    for row in rows:
                        clean = {k: v for k, v in row.items() if k not in _SENSITIVE}
                        for k, v in clean.items():
                            if hasattr(v, "isoformat"):
                                clean[k] = v.isoformat()
                        clean_rows.append(clean)
                    manifest["tables"][table_name] = {"row_count": len(clean_rows)}
                    zf.writestr(f"{table_name}.json",
                                json.dumps(clean_rows, default=str, ensure_ascii=False))
                except Exception as e:
                    manifest["tables"][table_name] = {"row_count": 0, "error": str(e)}

            # Files (per-part)
            for subdir, file_list_key in _PART_FILE_DIRS.get(part, []):
                src_dir = data_dir / subdir
                manifest["files"].setdefault(file_list_key, [])
                if not src_dir.exists():
                    continue
                for fpath in src_dir.rglob("*"):
                    if fpath.is_file():
                        arc_name = f"files/{subdir}/{fpath.relative_to(src_dir)}"
                        zf.write(fpath, arc_name)
                        manifest["files"][file_list_key].append({
                            "zip_path": arc_name,
                            "filename": fpath.name,
                            "size": fpath.stat().st_size,
                        })
            # Scanner state (files-state only) under the alias
            if part == "files-state":
                scanner_dir = Path("scanner/data")
                if scanner_dir.exists():
                    manifest["files"].setdefault("scanner_data", [])
                    for fpath in scanner_dir.rglob("*"):
                        if fpath.is_file():
                            arc_name = f"files/__scanner_data__/{fpath.relative_to(scanner_dir)}"
                            zf.write(fpath, arc_name)
                            manifest["files"]["scanner_data"].append({
                                "zip_path": arc_name,
                                "filename": fpath.name,
                                "size": fpath.stat().st_size,
                            })

            zf.writestr("manifest.json",
                        json.dumps(manifest, indent=2, default=str, ensure_ascii=False))

        size = tmp_path.stat().st_size
        h = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        sha = h.hexdigest()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_path), str(out_path))
        return size, sha
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_export_tables() -> list[tuple[str, str | None]]:
    """Return the canonical (table_name, sql_or_None) list.

    Kept here so backup_service doesn't import server.py. Update this in
    lockstep with server.py:RequestHandler._EXPORT_TABLES.
    """
    return [
        ("users", "SELECT * FROM users ORDER BY id"),
        ("password_reset_tokens", None),  # ephemeral
        ("twofa_challenges", None),       # ephemeral
        ("user_roles", "SELECT * FROM user_roles"),
        ("stages", "SELECT * FROM stages ORDER BY id"),
        ("tag_definitions", "SELECT * FROM tag_definitions ORDER BY id"),
        ("task_labels", "SELECT * FROM task_labels ORDER BY id"),
        ("custom_field_definitions", "SELECT * FROM custom_field_definitions ORDER BY id"),
        ("custom_field_options", "SELECT * FROM custom_field_options ORDER BY id"),
        ("custom_field_groups", "SELECT * FROM custom_field_groups ORDER BY id"),
        ("history_categories", "SELECT * FROM history_categories ORDER BY id"),
        ("branding", "SELECT * FROM branding ORDER BY id"),
        ("system_settings", "SELECT * FROM system_settings ORDER BY key"),
        ("contacts", "SELECT * FROM contacts ORDER BY id"),
        ("contact_emails", "SELECT * FROM contact_emails ORDER BY id"),
        ("contact_phones", "SELECT * FROM contact_phones ORDER BY id"),
        ("contact_addresses", "SELECT * FROM contact_addresses ORDER BY id"),
        ("opportunities", "SELECT * FROM opportunities ORDER BY id"),
        ("opportunity_tags", "SELECT * FROM opportunity_tags"),
        ("opportunity_custom_field_values",
         "SELECT * FROM opportunity_custom_field_values ORDER BY id"),
        ("tasks", "SELECT * FROM tasks ORDER BY id"),
        ("history_events", "SELECT * FROM history_events ORDER BY id"),
        ("history_replies", "SELECT * FROM history_replies ORDER BY id"),
        ("history_attachments", "SELECT * FROM history_attachments ORDER BY id"),
        ("history_notify_users", "SELECT * FROM history_notify_users"),
        ("history_reply_notify_users", "SELECT * FROM history_reply_notify_users"),
        ("mail_accounts", "SELECT * FROM mail_accounts ORDER BY id"),
        ("mail_messages", "SELECT * FROM mail_messages ORDER BY id"),
        ("mail_deal_links", "SELECT * FROM mail_deal_links ORDER BY id"),
        ("mail_tag_assignments", "SELECT * FROM mail_tag_assignments ORDER BY id"),
        ("mail_outgoing", "SELECT * FROM mail_outgoing ORDER BY id"),
        ("mail_folders", "SELECT * FROM mail_folders ORDER BY id"),
        ("mail_attachments", "SELECT * FROM mail_attachments ORDER BY id"),
        ("mail_tags", "SELECT * FROM mail_tags ORDER BY id"),
        ("mail_templates", "SELECT * FROM mail_templates ORDER BY id"),
        ("mail_contacts", "SELECT * FROM mail_contacts ORDER BY id"),
        ("mail_contractors", "SELECT * FROM mail_contractors ORDER BY id"),
        ("mail_classification_rules",
         "SELECT * FROM mail_classification_rules ORDER BY id"),
        ("service_feeds", "SELECT * FROM service_feeds ORDER BY id"),
        ("bot_config", "SELECT * FROM bot_config"),
        ("bot_activity_log", "SELECT * FROM bot_activity_log ORDER BY id"),
        ("notifications", "SELECT * FROM notifications ORDER BY id"),
        ("notification_preferences", "SELECT * FROM notification_preferences"),
        ("telegram_notification_log",
         "SELECT * FROM telegram_notification_log ORDER BY id"),
        ("document_folders", "SELECT * FROM document_folders ORDER BY id"),
        ("project_documents", "SELECT * FROM project_documents ORDER BY id"),
        ("document_activity", "SELECT * FROM document_activity ORDER BY id"),
        ("document_templates", "SELECT * FROM document_templates ORDER BY id"),
        ("project_photos", "SELECT * FROM project_photos ORDER BY id"),
        ("project_photo_folders", "SELECT * FROM project_photo_folders ORDER BY id"),
        ("photo_exif_cache", "SELECT * FROM photo_exif_cache"),
        ("photo_report_templates",
         "SELECT * FROM photo_report_templates ORDER BY id"),
        ("user_profiles", "SELECT * FROM user_profiles"),
        ("personal_notes", "SELECT * FROM personal_notes ORDER BY id"),
        ("personal_note_links", "SELECT * FROM personal_note_links"),
        ("calendar_sources", "SELECT * FROM calendar_sources ORDER BY id"),
        ("calendar_events", "SELECT * FROM calendar_events ORDER BY id"),
        ("ics_feed_cache", "SELECT * FROM ics_feed_cache ORDER BY id"),
        ("minimized_modal_state", "SELECT * FROM minimized_modal_state"),
        ("sessions", "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 0"),
        ("mail_account_access", "SELECT * FROM mail_account_access"),
        ("mail_flag_queue", "SELECT * FROM mail_flag_queue"),
        ("opportunity_changes", "SELECT * FROM opportunity_changes ORDER BY id"),
        ("sync_watermarks", "SELECT * FROM sync_watermarks"),
        ("sync_errors", "SELECT * FROM sync_errors ORDER BY id"),
        ("email_classifications", "SELECT * FROM email_classifications ORDER BY id"),
        ("classifier_training_data",
         "SELECT * FROM classifier_training_data ORDER BY id"),
        ("automation_rules", "SELECT * FROM automation_rules ORDER BY id"),
        ("automation_logs", "SELECT * FROM automation_logs ORDER BY id"),
        ("time_entries", "SELECT * FROM time_entries ORDER BY id"),
    ]


def record_export_part(set_id: str, part: str) -> dict:
    """Record a landed export part for a backup set; return set status."""
    import db
    key = f"backup_parts_{set_id}"
    try:
        row = db.query_one("SELECT value FROM system_settings WHERE key = %s", (key,))
        landed = json.loads(row["value"]) if row and row.get("value") else {}
    except Exception:
        landed = {}
    landed[part] = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO system_settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, json.dumps(landed)),
    )
    # Prune part-tracking for old sets (keep last 10)
    try:
        db.execute(
            """DELETE FROM system_settings WHERE key LIKE 'backup_parts_%'
               AND key NOT IN (
                   SELECT key FROM system_settings WHERE key LIKE 'backup_parts_%'
                   ORDER BY key DESC LIMIT 10
               )"""
        )
    except Exception:
        pass
    missing = [p for p in EXPECTED_EXPORT_PARTS if p not in landed]
    return {"set_id": set_id, "landed": sorted(landed), "missing": missing}


def _cleanup_old_backups() -> None:
    """Remove backup files older than retention period."""
    try:
        import db
        config = _load_config_from_db()
        days = config.get("retention_days", BACKUP_RETENTION_DAYS)
        cutoff = time.time() - (days * 86400)
        # Per-part export zips are the only thing we keep on disk now.
        for fpath in BACKUP_DIR.glob("sietch-export-*-*.zip"):
            if fpath.stat().st_mtime < cutoff:
                fpath.unlink()
        # Stale legacy artifacts (older code may have produced these) —
        # clean them up too so they don't accumulate.
        for pattern in ("sietch-backup-*", "sietch-data-*"):
            for fpath in BACKUP_DIR.glob(pattern):
                if fpath.stat().st_mtime < cutoff:
                    fpath.unlink()
    except Exception as e:
        logger.warning("Backup cleanup failed: %s}", e)


# ── Scheduler ────────────────────────────────────────────────────────────────

def _backup_loop(stop_event: threading.Event) -> None:
    """Main backup scheduler loop."""
    logger.info("Backup scheduler started (interval=%ds)", BACKUP_INTERVAL)
    while not stop_event.is_set():
        try:
            config = _load_config_from_db()
            if config.get("enabled", BACKUP_ENABLED):
                result = run_backup()
                logger.info("Scheduled backup: %s", result)
        except Exception as e:
            logger.error("Backup loop error: %s", e)
        # Honor the admin-console interval (DB-backed), not the import-time
        # env default — otherwise the console setting is display-only.
        # Clamp to >=60s so a zero/negative value can't hot-loop.
        try:
            interval = int(config.get("interval", BACKUP_INTERVAL))
        except (NameError, ValueError, TypeError, AttributeError):
            interval = BACKUP_INTERVAL
        stop_event.wait(max(60, interval))


def start_backup_scheduler() -> threading.Event:
    """Start the backup scheduler thread. Returns a stop event."""
    stop_event = threading.Event()
    config = _load_config_from_db()
    if not config.get("enabled", BACKUP_ENABLED):
        logger.info("Backup scheduler disabled")
        return stop_event
    t = threading.Thread(target=_backup_loop, args=(stop_event,), daemon=True, name="sietch-backup")
    t.start()
    return stop_event


def stop_backup_scheduler(stop_event: threading.Event) -> None:
    """Stop the backup scheduler."""
    stop_event.set()
