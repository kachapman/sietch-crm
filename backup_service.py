"""Scheduled backup service for Sietch CRM.

Follows the notification_dispatcher.py pattern: threading.Thread + Event.
Handles pg_dump, data archival, remote transfer, rotation, and notifications.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sietch.backup")

# ── Configuration from env vars ──────────────────────────────────────────────
BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "false").lower() == "true"
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "86400"))  # seconds (24h)
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "data/backups"))

# Remote config defaults (overridden by DB system_settings)
BACKUP_REMOTE_TYPE = os.getenv("BACKUP_REMOTE_TYPE", "none")
BACKUP_REMOTE_HOST = os.getenv("BACKUP_REMOTE_HOST", "")
BACKUP_REMOTE_PORT = int(os.getenv("BACKUP_REMOTE_PORT", "22"))
BACKUP_REMOTE_USER = os.getenv("BACKUP_REMOTE_USER", "")
BACKUP_REMOTE_PATH = os.getenv("BACKUP_REMOTE_PATH", "")
BACKUP_REMOTE_KEY_FILE = os.getenv("BACKUP_REMOTE_KEY_FILE", "")

# Database connection (from env, same as db.py)
DB_HOST = os.getenv("DB_HOST", "db")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "sietch_crm")
DB_USER = os.getenv("DB_USER", "sietch")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# NOTE: BACKUP_DIR defaults inside data/ on purpose — in Docker, /app/data
# is the persistent volume, so backups survive container recreates. The tar
# command in _build_data_tar_command() always excludes BACKUP_DIR from the
# data archive (see fix for recursive-nesting audit finding), so keeping the
# default here is safe.

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
        "remote_type": config.get("remote_type", BACKUP_REMOTE_TYPE),
        "remote_host": config.get("remote_host", BACKUP_REMOTE_HOST),
        "remote_port": config.get("remote_port", BACKUP_REMOTE_PORT),
        "remote_user": config.get("remote_user", BACKUP_REMOTE_USER),
        "remote_path": config.get("remote_path", BACKUP_REMOTE_PATH),
        "remote_key": config.get("remote_key", BACKUP_REMOTE_KEY_FILE),
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
    """Execute a full backup: pg_dump + data archive + remote send + notifications."""
    if not _backup_lock.acquire(blocking=False):
        return {"ok": False, "error": "Backup already running"}

    start = time.time()
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "db_size": None,
        "data_size": None,
        "remote_status": None,
        "error": None,
    }

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # Unique per run: second-resolution slug + pid so overlapping or
        # rapid-succession runs can never collide on filenames (the lock
        # is now also held for the whole run — see below).
        ts_slug = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S") + f"-p{os.getpid()}"

        # 1. pg_dump
        db_path = BACKUP_DIR / f"sietch-backup-{ts_slug}.sql.gz"
        try:
            _run_pg_dump(db_path)
            entry["db_size"] = db_path.stat().st_size
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = f"pg_dump failed: {e}"
            _record_backup(entry)
            _notify_admins(False, f"Backup failed\n\nError: {e}\nDuration: {time.time() - start:.0f}s")
            return {"ok": False, "error": entry["error"]}
        # NOTE: no early lock release here — the lock is held for the
        # entire run and released once in the outer finally block.

        # 2. Data bundles (docs / history / state) — replaces the old
        # monolithic data tar so restores can pick bundles selectively.
        # Per-bundle failures are non-fatal (logged inside the helper).
        data_paths: list[Path] = []
        try:
            bundles = _run_data_bundles(BACKUP_DIR, ts_slug)
            for bundle, path in bundles:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                entry.setdefault("bundles", {})[bundle] = size
                data_paths.append(path)
            entry["data_size"] = sum(entry["bundles"].values())
        except Exception as e:
            logger.warning("Data bundles failed (non-fatal): %s", e)
            entry["data_size"] = 0

        # 3. Remote send
        config = _load_config_from_db()
        remote_type = config.get("remote_type", BACKUP_REMOTE_TYPE)
        if remote_type and remote_type != "none":
            try:
                _send_remote([db_path, *data_paths], config)
                entry["remote_status"] = "sent"
            except Exception as e:
                entry["remote_status"] = f"failed: {e}"
                logger.warning("Remote send failed: %s", e)

        # 4. Rotation
        _cleanup_old_backups()

        # 5. Record + notify
        entry["status"] = "success"
        entry["duration"] = time.time() - start
        _record_backup(entry)

        total_mb = ((entry.get("db_size") or 0) + (entry.get("data_size") or 0)) / 1024 / 1024
        detail_lines = [
            f"Database: {entry.get('db_size', 0) / 1024 / 1024:.1f} MB",
        ]
        for bundle, size in (entry.get("bundles") or {}).items():
            detail_lines.append(f"Data ({bundle}): {size / 1024 / 1024:.1f} MB")
        if not entry.get("bundles"):
            detail_lines.append(f"Data archive: {entry.get('data_size', 0) / 1024 / 1024:.1f} MB")
        detail_lines.append(f"Duration: {entry['duration']:.0f}s")
        if entry.get("remote_status"):
            detail_lines.append(f"Remote: {entry['remote_status']}")
        details = "\n".join(detail_lines)

        if entry.get("remote_status", "").startswith("failed"):
            _notify_admins(False, f"Backup OK but remote send failed\n\n{details}")
        else:
            _notify_admins(True, f"Backup completed successfully\n\n{details}")

        return {"ok": True, "size": entry.get("db_size", 0), "duration": entry["duration"]}

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


def _build_data_tar_command(
    output_path: Path,
    data_dir: Path,
    backup_dir: Path,
    subdirs: list[str],
    extra_sources: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Build the tar command for a data archive (pure helper, unit-tested).

    Excludes ``backup_dir`` when it lives inside the archived tree so prior
    artifacts are never nested into new archives (unbounded growth).
    ``subdirs`` are archived as ``<data-name>/<sub>`` members under one
    ``-C`` root; extra ``(parent, name)`` sources (e.g. scanner state)
    are appended as additional ``-C`` roots (parent resolved absolute —
    a relative second ``-C`` would resolve against the first one and
    break when the data root is absolute).
    """
    if backup_dir == data_dir:
        raise ValueError("BACKUP_DIR cannot be the data directory itself")
    cmd = ["tar", "czf", str(output_path)]
    try:
        inner = backup_dir.relative_to(data_dir)
        cmd.append(f"--exclude={data_dir.name}/{inner}")
    except ValueError:
        pass  # backup dir outside the archived tree — nothing to exclude
    cmd += ["-C", str(data_dir.parent)] + [f"{data_dir.name}/{s}" for s in subdirs]
    for parent, name in extra_sources or []:
        p = Path(parent)
        if (p / name).exists():
            cmd += ["-C", str(p.resolve()), name]
    return cmd


# Scheduled-archive bundles: (bundle-name, [data/ subdirs]). Mirrors the
# browser split parts 1:1 (docs/state; history blobs stay on the scheduled
# path only — never served over HTTP). scanner/data rides with "state".
DATA_BUNDLES: list[tuple[str, list[str]]] = [
    ("docs", ["documents", "photos", "avatars", "contact-avatars", "branding", "logos"]),
    ("history", ["history-attachments"]),
    ("state", ["user-profiles", "bot-customers", "presence-messages",
               "user-presence", "event-logs", "dashboard-notes"]),
]


def _run_data_bundles(
    backup_dir: Path,
    ts_slug: str,
    data_dir: Path = Path("data"),
) -> list[tuple[str, Path]]:
    """Write one tar.gz per bundle; return [(bundle-name, path)] successes.

    Per-bundle failures are logged and skipped (non-fatal) so one bad
    subtree can't sink the whole run. Missing subdirs are skipped silently.
    """
    done: list[tuple[str, Path]] = []
    if not data_dir.exists():
        return done
    for bundle, subdirs in DATA_BUNDLES:
        present = [s for s in subdirs if (data_dir / s).exists()]
        extra = (
            [("scanner", "data")]
            if bundle == "state" and Path("scanner/data").exists()
            else []
        )
        if not present and not extra:
            continue
        out = backup_dir / f"sietch-data-{bundle}-{ts_slug}.tar.gz"
        try:
            cmd = _build_data_tar_command(out, data_dir, backup_dir, present, extra)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError(f"tar exited {proc.returncode}: {proc.stderr[:500]}")
            done.append((bundle, out))
        except Exception as e:
            logger.warning("Data bundle %s failed (non-fatal): %s", bundle, e)
    return done


def _run_data_archive(output_path: Path) -> None:
    """Legacy single-tar entry point (kept for tests/callers).

    Prefer _run_data_bundles for scheduled runs.
    """
    data_dir = Path("data")
    if not data_dir.exists():
        return
    subdirs = [d for d in ("documents", "photos") if (data_dir / d).exists()]
    if not subdirs:
        return

    cmd = _build_data_tar_command(
        output_path,
        data_dir,
        BACKUP_DIR,
        subdirs,
        extra_sources=[("scanner", "data")],
    )
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tar exited {proc.returncode}: {proc.stderr[:500]}")


def _send_remote(artifacts: list[Path], config: dict) -> None:
    """Send backup files to remote server."""
    remote_type = config.get("remote_type", "none")
    host = config.get("remote_host", "")
    port = str(config.get("remote_port", 22))
    user = config.get("remote_user", "")
    path = config.get("remote_path", "")
    key = config.get("remote_key", "")

    if not host or not user:
        raise ValueError("Remote host and user are required")

    ssh_opts = ["-o", "StrictHostKeyChecking=no"]
    if key:
        ssh_opts.extend(["-i", key])

    for fpath in artifacts:
        if not fpath.exists():
            continue
        remote_url = f"{user}@{host}:{path}/{fpath.name}"

        if remote_type == "scp":
            cmd = ["scp", "-P", port] + ssh_opts + [str(fpath), remote_url]
        elif remote_type == "sftp":
            # Use sftp with batch mode
            cmd = ["sftp", "-P", port] + ssh_opts + ["-b", "-", user + "@" + host]
            # Would need stdin piping; fall back to scp
            cmd = ["scp", "-P", port] + ssh_opts + [str(fpath), remote_url]
        elif remote_type == "rsync":
            cmd = ["rsync", "-avz", "-e", f"ssh -p {port} " + " ".join(ssh_opts), str(fpath), remote_url]
        else:
            raise ValueError(f"Unsupported remote type: {remote_type}")

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"Remote send failed ({remote_type}): {proc.stderr[:300]}")


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
        for fpath in BACKUP_DIR.glob("sietch-backup-*"):
            if fpath.stat().st_mtime < cutoff:
                fpath.unlink()
        for fpath in BACKUP_DIR.glob("sietch-data-*"):
            if fpath.stat().st_mtime < cutoff:
                fpath.unlink()
    except Exception as e:
        logger.warning("Backup cleanup failed: %s", e)


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
