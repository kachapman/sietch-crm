#!/usr/bin/env python3
"""Sietch CRM v3.0 — Standalone server with PostgreSQL backend.

Replaces OnlyOffice CRM proxy with direct database queries.
All data owned locally. Zero external API calls.
"""

from __future__ import annotations

import collections
import base64
import gzip
import shutil
import tempfile
import subprocess
import sys
import threading
import errno
import traceback
import hashlib
import hmac
import io
import json
import os
import queue
import re
import socket
import ssl
import time
import urllib.error
import zipfile
import urllib.request
from datetime import datetime, timezone
from email.utils import make_msgid
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlencode

from event_bus import event_bus

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow is optional in dev, present in Docker
    Image = None

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sietch_server")

# ── Infrastructure ring buffer (shared module, rolling 500 events) ─────────────
from infra_log import (  # noqa: E402
    log_infra_event,
    infra_events,
    infra_uptime,
    infra_start_time,
)

log_infra_event("info", "Server started", source="app")

# ── OnlyOffice Sync State ──────────────────────────────────────────────────────
_sync_log: collections.deque = collections.deque(maxlen=500)
_sync_running = False

def _append_sync_log(level: str, msg: str) -> None:
    """Append a message to the sync log."""
    _sync_log.append({"ts": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg})
    logger.info("SYNC %s: %s", level, msg)
    if level == "error":
        log_infra_event("error", f"Sync: {msg}", source="sync")

def _crm_api_get(portal: str, token: str, path: str, retries: int = 3) -> Any:
    """GET from OnlyOffice CRM API with retry."""
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "Authorization": token},
                method="GET",
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                time.sleep(0.1)
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                delay = 1.0 * (2 ** attempt)
                _append_sync_log("warn", f"HTTP {exc.code} on {path}, retry {attempt+1}")
                time.sleep(delay)
                continue
            raise
    return None

def _crm_authenticate(portal: str, email: str, password: str) -> str | None:
    """Authenticate to OnlyOffice CRM and return token."""
    auth_body = json.dumps({"userName": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{portal.rstrip('/')}/api/2.0/authentication.json",
        data=auth_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            auth_data = json.loads(resp.read().decode("utf-8"))
            return auth_data.get("response", {}).get("token")
    except Exception as e:
        _append_sync_log("error", f"Authentication failed: {e}")
        return None

def _fetch_onlyoffice_mail_body(portal: str, token: str, message_id) -> tuple[str, str]:
    """Fetch full email body from OnlyOffice filehandler.ashx.

    Returns (body_html, body_text).  Either or both may be empty on failure.
    """
    mid = str(message_id).strip()
    if not mid or not mid.isdigit():
        return "", ""
    url = f"{portal.rstrip('/')}/Products/CRM/HttpHandlers/filehandler.ashx?action=mailmessage&message_id={mid}"
    req = urllib.request.Request(url, headers={
        "Authorization": token,
        "Accept": "application/json, text/html, */*",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return "", ""
    # Handle JSON response (mail message object with htmlBody / textBody)
    if "application/json" in ctype or raw.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return "", ""
        if isinstance(data, dict):
            # Unwrap OnlyOffice envelope if present
            data = data.get("response", data)
        if isinstance(data, dict):
            html = (data.get("htmlBody") or data.get("HtmlBody")
                    or data.get("bodyHtml") or data.get("BodyHtml")
                    or data.get("body") or "")
            text = (data.get("textBody") or data.get("TextBody")
                    or data.get("plainText") or data.get("PlainText")
                    or "")
            if html or text:
                return str(html), str(text)
    # Handle raw HTML response
    if raw.strip() and not raw.strip().startswith("marker file"):
        return raw, ""
    return "", ""


def _unwrap(data: Any) -> Any:
    """Unwrap OnlyOffice API response envelope."""
    if isinstance(data, dict):
        if "response" in data:
            return data["response"]
        if "result" in data:
            return data["result"]
    return data


# ── OnlyOffice import helpers (added 2026-10 for migration v2 audit) ─────────

HISTORY_ATTACHMENT_DIR_LIVE = Path(__file__).parent / "data" / "history-attachments"
CONTACT_AVATAR_DIR_LIVE = Path(__file__).parent / "data" / "contact-avatars"
HISTORY_ATTACHMENT_DIR_LIVE.mkdir(parents=True, exist_ok=True)
CONTACT_AVATAR_DIR_LIVE.mkdir(parents=True, exist_ok=True)

# Tabler icon pool for user avatar picker (must match public/app.js TABLER_ICON_OPTIONS).
_TABLER_ICON_OPTIONS = {
    'user', 'users', 'user-circle', 'user-plus', 'user-check', 'user-minus', 'user-x',
    'user-pin', 'ghost', 'robot', 'mood-smile', 'mood-sad',
    'cat', 'dog', 'fish', 'horse', 'butterfly', 'paw', 'spider', 'bug',
    'leaf', 'flower', 'cloud', 'cloud-rain', 'sun', 'moon', 'flame', 'droplet',
    'snowflake', 'wind',
    'building', 'building-bank', 'building-hospital', 'building-store', 'building-warehouse',
    'building-factory', 'home', 'home-2', 'building-skyscraper', 'map-pin',
    'car', 'truck', 'bus', 'bike', 'plane', 'plane-departure', 'ship', 'anchor',
    'music', 'disc', 'video', 'microphone', 'headphones', 'radio',
    'camera', 'photo', 'movie', 'book',
    'cup', 'coffee', 'beer', 'pizza', 'bread', 'apple', 'cherry', 'lemon',
    'device-laptop', 'device-desktop', 'device-mobile', 'device-tablet',
    'cpu', 'antenna', 'bluetooth', 'wifi', 'router', 'satellite',
    'briefcase', 'mail', 'mail-opened', 'inbox', 'send', 'rocket', 'at',
    'phone', 'calendar', 'chart-bar',
    'shield', 'shield-check', 'lock', 'key', 'badge', 'certificate', 'fingerprint', 'license',
    'crown', 'diamond', 'gift', 'medal', 'trophy', 'star', 'sparkles', 'heart',
}

OO_FIELD_TYPE_MAP_LIVE = {
    0: "text", 1: "textarea", 2: "number", 3: "url",
    4: "select", 5: "date", 6: "checkbox", 7: "currency",
    8: "text",
}

MIME_LIVE = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "csv": "text/csv",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "webp": "image/webp",
    "esx": "application/octet-stream",
}


def _crm_normalize_field_type(raw: Any) -> str:
    """Map OnlyOffice numeric fieldType (0/1/3/5 etc.) to Sietch string."""
    if raw is None:
        return "text"
    try:
        return OO_FIELD_TYPE_MAP_LIVE.get(int(raw), "text")
    except (TypeError, ValueError):
        return "text"


def _crm_is_closure_sentinel(raw: Any) -> bool:
    """OnlyOffice .NET min-value sent for 'not closed' actualCloseDate."""
    s = str(raw or "").strip()
    return s.startswith("0001-01-01T00:00:00")


def _crm_safe_filename(name: str) -> str:
    out = re.sub(r"[^\w.\-]+", "_", name or "").strip("._")
    return out or "file"


def _crm_guess_mime(ext: str) -> str:
    return MIME_LIVE.get(ext.lower(), "application/octet-stream")


def _crm_get_raw_bytes(url: str, headers: dict | None = None, timeout: int = 60) -> bytes | None:
    """Download raw bytes with retry (used for attachments + avatars)."""
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (2 ** attempt))
    if last:
        _append_sync_log("warn", f"download failed: {url}: {last}")
    return None


def _fetch_onlyoffice_file(url: str, timeout: int = 30) -> bytes | None:
    """Fetch a file from OnlyOffice's filehandler.ashx download endpoint.

    Works for any user with read access to the underlying CRM entity (the bot
    account has access to all deals + emails). Returns None on 404, network
    error, or non-2xx. Used as the primary source for mail attachments so
    years-old emails stay viewable even after IMAP auto-archive.
    """
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # 404/410 are terminal — file genuinely not there, no retry
            if e.code in (404, 410):
                return None
            last = e
            time.sleep(0.5 * (2 ** attempt))
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (2 ** attempt))
    if last:
        logger.debug("OnlyOffice file fetch failed (will fall back to IMAP): %s: %s", url, last)
    return None


# --- OnlyOffice mail archive (Elasticsearch) body fetcher ----------------------
# The CRM's filehandler.ashx?action=mailmessage only serves RECENT message ids
# (it 500s for anything older). The complete archive lives in the OnlyOffice
# mail_mail Elasticsearch index on the OnlyOffice droplet, reachable only from
# inside that droplet's docker network — so we reach it over SSH.
OO_MAIL_ES_SSH_KEY = os.environ.get("OO_MAIL_ES_SSH_KEY", "/app/data/keys/id_ed25519_sietch_to_oo")
OO_MAIL_ES_HOST = os.environ.get("OO_MAIL_ES_HOST", "office.publicadjustermidwest.com")
_OO_MAIL_ES_CACHE: dict[int, str] = {}


def _fetch_onlyoffice_mail_body(message_id: int) -> str | None:
    """Fetch an email body from OnlyOffice's mail_mail ES index.

    Returns the raw HTML body, or None if the message isn't in the archive
    (pre-2024 emails) or the archive is unreachable.

    Uses a small in-process cache because the same message id is often
    requested repeatedly (every history event that references it).
    """
    if not message_id or message_id <= 0:
        return None
    cached = _OO_MAIL_ES_CACHE.get(message_id)
    if cached is not None:
        return cached
    key_path = OO_MAIL_ES_SSH_KEY
    if not os.path.exists(key_path):
        logger.debug("OnlyOffice mail ES key missing at %s — cannot fetch bodies", key_path)
        return None
    import base64 as _b64
    import subprocess as _sp
    try:
        cmd = [
            "ssh", "-i", key_path, "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=20",
            f"root@{OO_MAIL_ES_HOST}",
            "docker exec onlyoffice-elasticsearch curl -fsS -m 20 "
            f"'http://onlyoffice-elasticsearch:9200/mail_mail/_doc/{int(message_id)}' 2>/dev/null",
        ]
        out = _sp.run(cmd, capture_output=True, timeout=45).stdout
        if not out:
            return None
        d = json.loads(out.decode("utf-8", errors="replace"))
        if not d.get("found"):
            return None
        raw = ((d.get("_source") or {}).get("document") or {}).get("data") or ""
        if not raw:
            return None
        html = _b64.b64decode(raw).decode("utf-8", errors="replace")
        if len(_OO_MAIL_ES_CACHE) > 500:
            _OO_MAIL_ES_CACHE.clear()
        _OO_MAIL_ES_CACHE[int(message_id)] = html
        return html
    except Exception as e:
        logger.debug("OnlyOffice mail body fetch failed for message_id=%s: %s", message_id, e)
        return None


def _crm_fetch_user_id_map(portal: str, token: str) -> dict[str, int]:
    """Map OnlyOffice user UUID -> Sietch user.id (matched by email)."""
    oo_users = []
    for path in ("/api/2.0/crm/user/filter?count=500", "/api/2.0/people"):
        try:
            data = _unwrap(_crm_api_get(portal, token, path))
            if isinstance(data, list):
                oo_users = data
                break
        except Exception:
            continue
    sietch_by_email: dict[str, int] = {}
    for row in db.query_dicts("SELECT id, email FROM users"):
        em = (row.get("email") or "").strip().lower()
        if em:
            sietch_by_email[em] = row["id"]
    out: dict[str, int] = {}
    for u in oo_users:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or u.get("ID") or "")
        em = str(u.get("email") or u.get("Email") or "").strip().lower()
        if uid and em and em in sietch_by_email:
            out[uid] = sietch_by_email[em]
    return out


def _ssl_ctx():
    """SSL context that always bypasses verification (OnlyOffice uses self-signed)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_oo_user_owner_map(portal: str, token: str) -> dict:
    """Map OnlyOffice user IDs → Sietch user IDs via email, for task ownership."""
    oo_users = []
    try:
        data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/user/filter?count=500"))
        if isinstance(data, list):
            oo_users = data
    except Exception:
        oo_users = []
    if not oo_users:
        try:
            data = _unwrap(_crm_api_get(portal, token, "/api/2.0/people"))
            if isinstance(data, list):
                oo_users = data
        except Exception:
            oo_users = []
    sietch_by_email = {}
    for u in db.query_dicts("SELECT id, email FROM users"):
        em = (u.get("email") or "").strip().lower()
        if em:
            sietch_by_email[em] = u["id"]
    owner_map = {}
    for u in oo_users:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or u.get("Id") or u.get("ID") or "")
        em = str(u.get("email") or u.get("Email") or "").strip().lower()
        if uid and em and em in sietch_by_email:
            owner_map[uid] = sietch_by_email[em]
    return owner_map


def _oo_task_owner(task: dict, owner_map: dict, fallback_id: int) -> int:
    """Resolve owner id for an OnlyOffice task (responsible → author → fallback)."""
    for key in ("responsibleId", "responsible_id", "ResponsibleId", "authorId", "author_id", "AuthorId"):
        val = task.get(key)
        if val is not None:
            owner_id = owner_map.get(str(val))
            if owner_id:
                return owner_id
    return fallback_id

from ics_calendar import _MAX_ICS_BYTES, is_allowed_calendar_url, parse_ics_calendar

# ── Version ────────────────────────────────────────────────────────────────────
APP_VERSION = "dev"
try:
    version_path = Path(__file__).parent / "VERSION"
    if version_path.exists():
        APP_VERSION = version_path.read_text().strip()
except Exception:
    pass

# ── Local store imports (unchanged from v2) ────────────────────────────────────
from user_profile_store import load_user_profile, save_user_profile
from event_log_store import append_event_log, load_event_log, list_users_with_logs
from crm_bot_store import (
    add_mapping, cancel_code, cancel_code_by_value,
    generate_code, get_mapping_by_chat, get_pending_codes,
    get_usage_stats, list_mappings, remove_mapping,
    remove_mapping_by_chat, set_allowed_projects, set_nickname, set_verify_chat_id,
    track_request, verify_code,
)
from presence_store import (
    append_dm, clear_conversation, clean_stale_presence_records,
    clear_auto_status, get_conversation, get_portal_presence_snapshot,
    get_recent_dms_for_user, load_user_presence, load_user_last_read_dms,
    mark_messages_read, save_user_presence, set_last_read_dm,
    set_status, touch_crm_activity, touch_heartbeat,
)
from notification_dispatcher import start_dispatcher, stop_dispatcher
from calendar_sync import start_calendar_sync, stop_calendar_sync

# ── Config ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()
import oauth_providers

# mail_scanner imports oauth_providers (and reads its env-derived constants),
# so it MUST be imported AFTER _load_env_file() has populated the environment.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "scanner"))
    import mail_scanner
except Exception:  # pragma: no cover - scanner module may not be installed
    mail_scanner = None  # type: ignore[assignment]

PORT = int(os.environ.get("PORT", "8766"))
SESSION_COOKIE = "sietch_session"
DATA_DIR = ROOT / "data"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
PRESENCE_AUTO_STATUS_TIMEOUT_S = 300
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PHOTO_STORAGE_PATH = Path(os.getenv("PHOTO_STORAGE_PATH", str(DATA_DIR / "photos")))
DOCUMENT_STORAGE_PATH = Path(os.getenv("DOCUMENT_STORAGE_PATH", str(DATA_DIR / "documents")))
AVATAR_STORAGE_PATH = Path(os.getenv("AVATAR_STORAGE_PATH", str(DATA_DIR / "avatars")))
CONTACT_PHOTO_STORAGE_PATH = Path(os.getenv("CONTACT_PHOTO_STORAGE_PATH", str(DATA_DIR / "contact-photos")))
BRANDING_STORAGE_PATH = Path(os.getenv("BRANDING_STORAGE_PATH", str(DATA_DIR / "branding")))
DOCS_JWT_SECRET = os.environ.get("DOCS_JWT_SECRET", "")
DOCS_INTERNAL_URL = os.environ.get("DOCS_INTERNAL_URL", "").rstrip("/")
DOCS_PUBLIC_URL = os.environ.get("DOCS_PUBLIC_URL", "").rstrip("/")
CRM_PUBLIC_URL = os.environ.get("CRM_PUBLIC_URL", "").rstrip("/") or DOCS_PUBLIC_URL
# OnlyOffice Community Server (separate droplet — file archive, CRM DB).
# Used as the primary source for mail attachment files (Bug fix 2026-10-19):
# filehandler.ashx works for years-old emails regardless of live IMAP state.
OO_PORTAL_URL = os.environ.get("ONLYOFFICE_PORTAL_URL", "").rstrip("/")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "sietch-crm")
DOCSERVER_CONTAINER_NAME = os.environ.get("DOCSERVER_CONTAINER_NAME", "onlyoffice-docserver")

OAUTH_MICROSOFT_CLIENT_ID = os.environ.get("OAUTH_MICROSOFT_CLIENT_ID", "")
OAUTH_MICROSOFT_CLIENT_SECRET = os.environ.get("OAUTH_MICROSOFT_CLIENT_SECRET", "")
OAUTH_MICROSOFT_TENANT = os.environ.get("OAUTH_MICROSOFT_TENANT", "common")
OAUTH_GOOGLE_CLIENT_ID = os.environ.get("OAUTH_GOOGLE_CLIENT_ID", "")
OAUTH_GOOGLE_CLIENT_SECRET = os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

# In-memory OAuth state store: state -> (user_id, provider, expires_at)
_oauth_states: dict[str, tuple[int, str, float, bool]] = {}
# Separate state store for the Graph sendMail OAuth flow: (user_id, account_id, expiry).
# Distinct from _oauth_states because the Graph flow is bound to an existing
# account (not a brand-new one being created).
_oauth_graph_states: dict[str, tuple[int, int, float]] = {}

# ── Bot token helper (DB-first with env fallback, 60s cache) ─────────────────
_bot_token_cache: str = ""
_bot_config_cache: dict | None = None
_bot_config_cache_ts: float = 0
_BOT_CONFIG_CACHE_TTL = 60  # seconds

def _resolve_project_id(raw: str | int) -> int | None:
    """Resolve a project identifier (id or project_number) to the internal opportunities.id.
    
    Accepts either:
    - A database id (integer)
    - A project_number (integer)
    Returns the internal opportunities.id, or None if not found.
    """
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return None
    # First try as direct id
    row = db.query_one("SELECT id FROM opportunities WHERE id = %s", (val,))
    if row:
        return val
    # Try as project_number
    row = db.query_one("SELECT id FROM opportunities WHERE project_number = %s", (val,))
    if row:
        return row["id"]
    return None

def _get_bot_token() -> str:
    """Return bot token from bot_config DB table (cached 60s), falling back to .env."""
    global _bot_token_cache, _bot_config_cache, _bot_config_cache_ts
    import time as _time
    now = _time.time()
    if _bot_config_cache and now - _bot_config_cache_ts < _BOT_CONFIG_CACHE_TTL:
        return _bot_token_cache
    try:
        row = db.query_one("SELECT value FROM bot_config WHERE key = 'settings'")
        if row and row.get("value"):
            cfg = row["value"] if isinstance(row["value"], dict) else json.loads(str(row["value"]))
            _bot_config_cache = cfg
            _bot_config_cache_ts = now
            token = cfg.get("token", "")
            if token:
                _bot_token_cache = token
                return token
    except Exception:
        pass
    # Fallback to env
    fallback = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _bot_token_cache = fallback
    return fallback


def _get_bot_config() -> dict:
    """Return full bot config from DB (cached 60s), falling back to defaults."""
    global _bot_config_cache, _bot_config_cache_ts
    import time as _time
    now = _time.time()
    if _bot_config_cache and now - _bot_config_cache_ts < _BOT_CONFIG_CACHE_TTL:
        return _bot_config_cache or {}
    try:
        row = db.query_one("SELECT value FROM bot_config WHERE key = 'settings'")
        if row and row.get("value"):
            cfg = row["value"] if isinstance(row["value"], dict) else json.loads(str(row["value"]))
            # Fill empty fields from env vars
            if not cfg.get("token"):
                cfg["token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            _bot_config_cache = cfg
            _bot_config_cache_ts = now
            return cfg
    except Exception:
        pass
    return {
        "token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "botUsername": "", "botLink": "", "maxFileSizeMb": 20,
        "pollInterval": 5, "enabled": True, "allowCustomerUploads": False,
        "welcomeMessage": "Welcome!", "configVersion": 0,
    }


def _invalidate_bot_config_cache() -> None:
    """Force next _get_bot_token()/_get_bot_config() to re-read from DB."""
    global _bot_config_cache, _bot_config_cache_ts
    _bot_config_cache = None
    _bot_config_cache_ts = 0

# ── DB init ────────────────────────────────────────────────────────────────────
import db
import auth as auth_mod

db.init_db()

# ── Schema migrations ──────────────────────────────────────────────────────────
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS monitored_folders TEXT DEFAULT 'INBOX'")
except Exception:
    pass  # table may not exist yet

# Migration 021: document_templates table (Invoice + COC PDF generator)
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS document_templates (
            id                       SERIAL PRIMARY KEY,
            name                     TEXT NOT NULL,
            logo_path                TEXT,
            company_name             TEXT NOT NULL DEFAULT '',
            company_address          TEXT NOT NULL DEFAULT '',
            company_city_state_zip   TEXT NOT NULL DEFAULT '',
            company_phone            TEXT NOT NULL DEFAULT '',
            company_email            TEXT NOT NULL DEFAULT '',
            primary_color            TEXT NOT NULL DEFAULT '#1a1a1a',
            invoice_title            TEXT NOT NULL DEFAULT 'INVOICE',
            coc_title                TEXT NOT NULL DEFAULT 'CERTIFICATE OF COMPLETION',
            default_terms            TEXT NOT NULL DEFAULT 'Due on receipt',
            default_sales_rep        TEXT NOT NULL DEFAULT '',
            default_line_items       JSONB NOT NULL DEFAULT '[]'::jsonb,
            show_signature           BOOLEAN NOT NULL DEFAULT TRUE,
            signature_name           TEXT NOT NULL DEFAULT '',
            signature_path           TEXT,
            show_branding_footer     BOOLEAN NOT NULL DEFAULT TRUE,
            is_default               BOOLEAN NOT NULL DEFAULT FALSE,
            created_at               TIMESTAMP DEFAULT NOW(),
            updated_at               TIMESTAMP DEFAULT NOW()
        )
    """)
except Exception:
    pass
# Additive column for databases that created the table before show_branding_footer existed.
try:
    db.execute("ALTER TABLE document_templates ADD COLUMN IF NOT EXISTS show_branding_footer BOOLEAN NOT NULL DEFAULT TRUE")
except Exception:
    pass

# Migration 022: service_feeds table (third-party status / webhook → notifications)
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS service_feeds (
            id                  SERIAL PRIMARY KEY,
            name                TEXT NOT NULL,
            kind                TEXT NOT NULL DEFAULT 'rss',
            url                 TEXT,
            webhook_secret      TEXT,
            poll_interval_sec   INTEGER NOT NULL DEFAULT 300,
            last_polled_at      TIMESTAMP,
            last_item_id        TEXT,
            enabled             BOOLEAN NOT NULL DEFAULT TRUE,
            target_mode         TEXT NOT NULL DEFAULT 'all',
            target_user_ids     INTEGER[] NOT NULL DEFAULT '{}'::integer[],
            accent_color        TEXT NOT NULL DEFAULT '#e6a700',
            created_at          TIMESTAMP DEFAULT NOW(),
            updated_at          TIMESTAMP DEFAULT NOW()
        )
    """)
except Exception:
    pass
try:
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_service_feeds_webhook_secret ON service_feeds(webhook_secret) WHERE webhook_secret IS NOT NULL")
except Exception:
    pass
try:
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_photo_report_templates_one_default ON photo_report_templates (is_default) WHERE is_default = TRUE")
except Exception:
    pass
try:
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_document_templates_one_default ON document_templates (is_default) WHERE is_default = TRUE")
except Exception:
    pass

# Migration 023: convert service_feeds timestamps to TIMESTAMPTZ so the user's
# browser correctly interprets the ISO string and converts to local time on
# display. Idempotent ALTER TYPE statements.
try:
    db.execute("ALTER TABLE service_feeds ALTER COLUMN last_polled_at TYPE TIMESTAMPTZ USING last_polled_at AT TIME ZONE 'UTC'")
except Exception:
    pass
try:
    db.execute("ALTER TABLE service_feeds ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'")
except Exception:
    pass
try:
    db.execute("ALTER TABLE service_feeds ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC'")
except Exception:
    pass

# Migration 031: history_reply_notify_users table — tagged (mentioned) users
# on history replies. Populated by _handle_history_reply_create when the
# rich-text reply editor posts @-mentions; used to create 'reply_tagged'
# notification rows mirroring the event-side 'note_tagged' flow. Idempotent
# CREATE TABLE IF NOT EXISTS so safe on restart.
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS history_reply_notify_users (
            reply_id INTEGER REFERENCES history_replies(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id),
            PRIMARY KEY (reply_id, user_id)
        )
    """)
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_history_reply_notify_users_reply ON history_reply_notify_users(reply_id)")
except Exception:
    pass

# Seed server_timezone default (Eastern) so the system always has a baseline.
try:
    db.execute(
        """INSERT INTO system_settings (key, value, description)
           VALUES ('server_timezone', 'America/New_York',
                   'Server timezone for scheduled-task math. User-facing timestamps render in each viewer local timezone.')
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, description = EXCLUDED.description"""
    )
except Exception as exc:
    print(f"server_timezone seed error: {exc}")

try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS display_name TEXT")
except Exception:
    pass

try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS auto_bcc_addr TEXT")
except Exception:
    pass

try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS auto_delete_days INTEGER DEFAULT 0")
except Exception:
    pass

try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS tab_icon TEXT DEFAULT 'user'")
except Exception:
    pass

try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS tab_color TEXT DEFAULT 'var(--accent)'")
except Exception:
    pass

# ── Sietch Tasks revamp: private tasks (owner-scoped) + checklist/todo fields ──
for _col in [
    "owner_user_id INTEGER REFERENCES users(id)",
    "items JSONB DEFAULT '[]'::jsonb",
    "labels TEXT DEFAULT ''",
    "repeat TEXT DEFAULT 'none'",
    "color TEXT DEFAULT ''",
    "pinned BOOLEAN DEFAULT FALSE",
    "archived BOOLEAN DEFAULT FALSE",
    "sort_order INTEGER DEFAULT 0",
    "updated_at TIMESTAMP DEFAULT NOW()",
    "remind_at TIMESTAMP",
    "reminder_fired_at TIMESTAMP",
    "category_id INTEGER REFERENCES history_categories(id)",
]:
    try:
        db.execute(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {_col}")
    except Exception:
        pass

# Backfill owner: owner = responsible user, else creator, else first admin.
try:
    db.execute(
        "UPDATE tasks SET owner_user_id = COALESCE(responsible_user_id, created_by) "
        "WHERE owner_user_id IS NULL AND (responsible_user_id IS NOT NULL OR created_by IS NOT NULL)"
    )
except Exception:
    pass
try:
    db.execute(
        "UPDATE tasks SET owner_user_id = (SELECT id FROM users WHERE is_admin = TRUE ORDER BY id LIMIT 1) "
        "WHERE owner_user_id IS NULL"
    )
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_user_id)")
except Exception:
    pass
try:
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_reminders ON tasks(owner_user_id, remind_at) WHERE remind_at IS NOT NULL"
    )
except Exception:
    pass

# ── Personal notes: PostgreSQL storage + entity linking ────────────────────────
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS personal_notes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner INTEGER NOT NULL REFERENCES users(id),
            title VARCHAR(200) NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            content_prev TEXT,
            content_prev2 TEXT,
            content_prev_at TIMESTAMPTZ,
            content_prev2_at TIMESTAMPTZ,
            color VARCHAR(32) NOT NULL DEFAULT '',
            pinned BOOLEAN NOT NULL DEFAULT false,
            archived BOOLEAN NOT NULL DEFAULT false,
            archived_at TIMESTAMPTZ,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
except Exception:
    pass
# Add version history columns to existing databases
for _col in [
    "content_prev TEXT",
    "content_prev2 TEXT",
    "content_prev_at TIMESTAMPTZ",
    "content_prev2_at TIMESTAMPTZ",
]:
    try:
        db.execute(f"ALTER TABLE personal_notes ADD COLUMN IF NOT EXISTS {_col}")
    except Exception:
        pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS ix_personal_notes_owner ON personal_notes(owner, archived, sort_order)")
except Exception:
    pass
try:
    db.execute("""
        CREATE INDEX IF NOT EXISTS ix_personal_notes_fts ON personal_notes USING gin(
            to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,''))
        )
    """)
except Exception:
    pass
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS personal_note_links (
            note_id UUID NOT NULL REFERENCES personal_notes(id) ON DELETE CASCADE,
            entity_type VARCHAR NOT NULL,
            entity_id VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (note_id, entity_type, entity_id)
        )
    """)
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS ix_personal_note_links_entity ON personal_note_links(entity_type, entity_id)")
except Exception:
    pass

# ── Migrate notesTiles from profile JSON to personal_notes table ──────────────
try:
    from pathlib import Path as _Path
    _profiles_dir = _Path(__file__).resolve().parent / "data" / "user-profiles"
    if _profiles_dir.is_dir():
        _migrated_count = 0
        for _portal_dir in _profiles_dir.iterdir():
            if not _portal_dir.is_dir():
                continue
            for _profile_file in _portal_dir.glob("*.json"):
                try:
                    _data = json.loads(_profile_file.read_text(encoding="utf-8"))
                    _tiles = _data.get("notesTiles") or []
                    if not _tiles:
                        continue
                    # Find the user ID from the filename (without .json extension)
                    _user_email = _profile_file.stem
                    _user_row = db.query_one("SELECT id FROM users WHERE email = %s", (_user_email,))
                    if not _user_row:
                        continue
                    _user_id = _user_row[0]
                    # Check if notes already exist for this user
                    _existing = db.query_one("SELECT 1 FROM personal_notes WHERE owner = %s LIMIT 1", (_user_id,))
                    if _existing:
                        continue
                    # Insert each tile as a personal note
                    for _tile in _tiles:
                        _title = (_tile.get("name") or "Notes")[:200]
                        _content = _tile.get("content") or ""
                        _color = (_tile.get("accent") or "")[:32]
                        _archived = bool(_tile.get("archived", False))
                        _archived_at = _tile.get("archivedAt")
                        db.execute(
                            "INSERT INTO personal_notes (owner, title, content, color, archived, archived_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (_user_id, _title, _content, _color, _archived, _archived_at),
                        )
                        _migrated_count += 1
                    # Clear notesTiles from the profile JSON
                    _data["notesTiles"] = []
                    _profile_file.write_text(json.dumps(_data, indent=2), encoding="utf-8")
                except Exception:
                    continue
        if _migrated_count:
            print(f"Notes migration: migrated {_migrated_count} notes from profile JSON to PostgreSQL")
except Exception as e:
    print(f"Notes migration skipped: {e}")

# Classifier training data schema
try:
    db.execute("ALTER TABLE classifier_training_data ADD COLUMN IF NOT EXISTS message_id INTEGER REFERENCES mail_messages(id)")
except Exception:
    pass
try:
    db.execute("ALTER TABLE classifier_training_data ADD COLUMN IF NOT EXISTS reviewer_id INTEGER REFERENCES users(id)")
except Exception:
    pass
try:
    db.execute("ALTER TABLE classifier_training_data ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP")
except Exception:
    pass

# Rename auto_delete_days → retention_days (merged sync + retention setting)
try:
    db.execute("ALTER TABLE mail_accounts RENAME COLUMN auto_delete_days TO retention_days")
except Exception:
    pass
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS retention_days INTEGER DEFAULT 90")
except Exception:
    pass
# Graph sendMail OAuth token columns (migration 019). Separate audience from
# the existing Exchange-audience oauth_* columns; used to bypass SMTP AUTH
# disable (5.7.139 "SmtpClientAuthentication is disabled") on consumer
# Outlook accounts that Microsoft support refuses to re-enable.
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_graph_access_token TEXT")
except Exception:
    pass
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_graph_refresh_token TEXT")
except Exception:
    pass
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_graph_token_expires TIMESTAMP")
except Exception:
    pass
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_graph_scope TEXT")
except Exception:
    pass
try:
    db.execute("ALTER TABLE history_events ADD COLUMN IF NOT EXISTS contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE")
except Exception:
    pass

# History category id for email link/import events ("Email" in
# history_categories, seeded by init.sql at ids 8-17 matching production).
# Was 16 ("Customer Update") — a stale value from before the taxonomy reseed
# that filed linked/imported emails as customer updates (and leaked raw email
# dumps into the Telegram customer-updates feed, which filters on 16).
EMAIL_HISTORY_CATEGORY_ID = 13

# ── Calendar system (migration 010) ──────────────────────────────────────────
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS calendar_sources (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type IN ('ics', 'google_oauth', 'microsoft_oauth', 'caldav')),
            oauth_provider TEXT,
            oauth_access_token TEXT,
            oauth_refresh_token TEXT,
            oauth_token_expires_at TIMESTAMPTZ,
            oauth_email TEXT,
            caldav_url TEXT,
            caldav_username TEXT,
            caldav_password_encrypted TEXT,
            ics_url TEXT,
            last_synced_at TIMESTAMPTZ,
            sync_cursor TEXT,
            color TEXT DEFAULT '#5b8abf',
            visible BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_cal_sources_user ON calendar_sources(user_id)")
except Exception:
    pass
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            source_id INTEGER REFERENCES calendar_sources(id) ON DELETE CASCADE,
            uid TEXT NOT NULL,
            summary TEXT,
            description TEXT,
            location TEXT,
            dtstart TIMESTAMPTZ NOT NULL,
            dtend TIMESTAMPTZ,
            all_day BOOLEAN DEFAULT FALSE,
            rrule TEXT,
            color TEXT,
            status TEXT DEFAULT 'confirmed',
            external_id TEXT,
            last_modified TIMESTAMPTZ,
            reminder_minutes INTEGER,
            reminder_sent BOOLEAN DEFAULT FALSE,
            recurrence_id TEXT,
            is_occurrence BOOLEAN DEFAULT FALSE,
            transparency TEXT,
            url TEXT,
            attendees TEXT,
            timezone TEXT,
            snoozed_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(user_id, uid)
        )
    """)
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_cal_events_user_time ON calendar_events(user_id, dtstart, dtend)")
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_cal_events_source ON calendar_events(source_id) WHERE source_id IS NOT NULL")
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_cal_events_reminder ON calendar_events(user_id, dtstart) WHERE reminder_sent = FALSE AND reminder_minutes IS NOT NULL")
except Exception:
    pass
try:
    # Migration 034: recurrence occurrence rows (expand at sync).
    db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS recurrence_id TEXT")
except Exception:
    pass
try:
    db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS is_occurrence BOOLEAN DEFAULT FALSE")
except Exception:
    pass
try:
    db.execute("CREATE INDEX IF NOT EXISTS idx_cal_events_recurrence ON calendar_events(user_id, recurrence_id) WHERE recurrence_id IS NOT NULL")
except Exception:
    pass
for _col in ("transparency", "url", "attendees", "timezone"):
    try:
        db.execute(f"ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS {_col} TEXT")
    except Exception:
        pass
try:
    db.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ")
except Exception:
    pass
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS ics_feed_cache (
            id SERIAL PRIMARY KEY,
            url_hash TEXT NOT NULL UNIQUE,
            raw_ics TEXT,
            parsed_events JSONB,
            fetched_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            calendar_name TEXT,
            timezone TEXT
        )
    """)
except Exception:
    pass

# ── Helpers ────────────────────────────────────────────────────────────────────


def _json_default(obj: Any) -> str:
    """Serialize objects for JSON. Datetimes are emitted as UTC ISO-8601 with offset.

    PostgreSQL TIMESTAMP columns store naive UTC values; treat them as UTC so the
    frontend's timeZone conversion (e.g. America/New_York) produces the correct wall time.
    """
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            return obj.replace(tzinfo=timezone.utc).isoformat()
        return obj.astimezone(timezone.utc).isoformat()
    return str(obj)


def _json_response(handler: SimpleHTTPRequestHandler, status: int, payload: dict | list) -> None:
    body = json.dumps(payload, default=_json_default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: SimpleHTTPRequestHandler) -> bytes:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return b""
    return handler.rfile.read(length)


# ── Import conflict targeting ────────────────────────────────────────────────
# A bare `ON CONFLICT DO NOTHING` swallows *every* unique violation, not just
# the primary key. During the 2026-10-17 droplet migration that silently
# discarded the production `history_categories` rows (their titles collided
# with init.sql's seed rows) and left 6,597 history events failing an FK
# check while the endpoint still reported `{"ok": true}`. We therefore always
# name the conflict target explicitly, and we resolve each table's PK at
# runtime because the exported tables are heterogeneous: most are keyed on
# `id`, but several use composite keys (`opportunity_tags`,
# `notification_preferences`) or a natural key (`system_settings.key`,
# `sessions.token`, `user_profiles.user_id`).
_PK_CACHE: dict[str, list[str]] = {}


def _table_primary_key(table: str) -> list[str]:
    """Primary-key column list for `table`, cached. Empty if it has no PK."""
    if table in _PK_CACHE:
        return _PK_CACHE[table]
    rows = db.query(
        """SELECT a.attname
             FROM pg_constraint con
             JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
             JOIN pg_attribute a
               ON a.attrelid = con.conrelid AND a.attnum = k.attnum
            WHERE con.conrelid = to_regclass(%s) AND con.contype = 'p'
            ORDER BY k.ord""",
        (table,),
    )
    cols = [r[0] for r in rows] if rows else []
    _PK_CACHE[table] = cols
    return cols


def _conflict_target(table: str) -> str:
    """` ON CONFLICT (<pk>) DO NOTHING`, or bare DO NOTHING when there is no PK."""
    cols = _table_primary_key(table)
    if not cols:
        return " ON CONFLICT DO NOTHING"
    return " ON CONFLICT (" + ", ".join(cols) + ") DO NOTHING"


# ── Import jsonb adaptation ──────────────────────────────────────────────────
# psycopg2 cannot adapt a Python dict/list to a `jsonb` column on its own: it
# either raises "can't adapt type 'dict'" or, for a list, silently renders a
# Postgres ARRAY and fails with "column X is of type jsonb but expression is of
# type text[]". The per-bundle export stores such columns as native JSON, so
# every affected row was being rejected. In the 2026-10-17 restore that silently
# cost 753,627 of 816,863 `notifications` rows (the `payload` column) plus rows
# in 14 other tables. Wrap those values in psycopg2.extras.Json.
_JSONB_CACHE: dict[str, set[str]] = {}


def _table_jsonb_columns(table: str) -> set[str]:
    """jsonb column names for `table`, cached. Empty if none."""
    if table in _JSONB_CACHE:
        return _JSONB_CACHE[table]
    rows = db.query(
        """SELECT column_name
             FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND data_type = 'jsonb'""",
        (table,),
    )
    cols = {r[0] for r in rows} if rows else set()
    _JSONB_CACHE[table] = cols
    return cols


def _adapt_row_values(table: str, columns: list[str], values: tuple) -> tuple:
    """Wrap dict/list values destined for jsonb columns so psycopg2 can bind them."""
    jsonb_cols = _table_jsonb_columns(table)
    if not jsonb_cols:
        return values
    from psycopg2.extras import Json

    out = list(values)
    for i, col in enumerate(columns):
        v = out[i]
        if col in jsonb_cols and isinstance(v, (dict, list)):
            out[i] = Json(v)
    return tuple(out)


def _session_token(handler: SimpleHTTPRequestHandler) -> str | None:
    jar = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
    tok = jar.get(SESSION_COOKIE)
    return tok.value if tok else None


def _require_auth(handler: SimpleHTTPRequestHandler) -> dict | None:
    """Return user dict or None after sending error response."""
    token = _session_token(handler)
    if not token:
        _json_response(handler, 401, {"error": "Not authenticated"})
        return None
    user = auth_mod.get_session_user(token)
    if not user:
        _json_response(handler, 401, {"error": "Invalid or expired session"})
        return None
    return user


def _require_admin(handler: SimpleHTTPRequestHandler) -> dict | None:
    """Return admin user dict or None after sending error response.
    Checks is_admin (which includes admin and manager roles via migration backfill)."""
    user = _require_auth(handler)
    if not user:
        return None
    if not user.get("is_admin"):
        _json_response(handler, 403, {"error": "Forbidden"})
        return None
    return user


# NOTE: a role-based admin gate (`_require_role` / `_require_strict_admin`) used to
# guard the two password-reset endpoints while the other 109 admin endpoints used
# `_require_admin` (the is_admin boolean). That split let an account with
# is_admin=true but role='member' render the whole admin console yet receive
# "Forbidden: requires one of roles: admin" on password reset. There was no
# intentional extra strictness, so admin gating is now uniformly `_require_admin`
# and `role` is a descriptive/feature field only (badges, the `limited` CRM-Mail
# restriction). Removed deliberately so a divergent gate can't creep back in.


def _humanize_file_size(size_bytes) -> str:
    """Render a byte count as a short human label (e.g. '142 KB', '2.3 MB')."""
    try:
        n = int(size_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# Cache the system "Action" category id (init.sql ships it with title='Action'
# and sort_order=15; id is data-driven because rows may be inserted in any
# order across environments). -1 means "not yet resolved / not present".
_ACTION_CATEGORY_ID: int = -1


def _resolve_action_category_id() -> int:
    """Return the id of the system 'Action' history category, or -1 if absent."""
    global _ACTION_CATEGORY_ID
    if _ACTION_CATEGORY_ID > 0:
        return _ACTION_CATEGORY_ID
    try:
        row = db.query_one(
            "SELECT id FROM history_categories WHERE title = 'Action' LIMIT 1"
        )
        if row and row.get("id"):
            _ACTION_CATEGORY_ID = int(row["id"])
            return _ACTION_CATEGORY_ID
    except Exception:
        pass
    return -1


def _doc_pdf_safe(text) -> str:
    """Sanitize text for fpdf2's built-in Helvetica (Latin-1 only).

    Maps common Unicode punctuation that Helvetica can't render to ASCII
    equivalents, drops ASCII control chars, and strips anything above U+00FF.
    Without this, strings like "Smith — Family Trust" or any text containing
    a smart-quote / em-dash crash PDF generation with::

        Character "—" at index 0 in text is outside the range of characters
        supported by the font used: "helvetica".
    """
    if text is None:
        return ""
    s = str(text)
    # 1) Map unsupported-but-common Unicode glyphs to ASCII equivalents.
    _unicode_map = {
        "\u2014": "-",   # em dash
        "\u2013": "-",   # en dash
        "\u2012": "-",   # figure dash
        "\u2015": "-",   # horizontal bar
        "\u2212": "-",   # minus sign
        "\u2026": "...", # horizontal ellipsis
        "\u2018": "'",   # left single quote
        "\u2019": "'",   # right single quote / apostrophe
        "\u201A": "'",   # single low-9 quotation mark
        "\u201C": '"',   # left double quote
        "\u201D": '"',   # right double quote
        "\u201E": '"',   # double low-9 quotation mark
        "\u00AB": "<<",  # left guillemet
        "\u00BB": ">>",  # right guillemet
        "\u2022": "*",   # bullet
        "\u25E6": "*",   # white bullet
        "\u2023": ">",   # triangular bullet
        "\u2043": "-",   # hyphen bullet
        "\u2192": "->",  # rightwards arrow
        "\u2190": "<-",  # leftwards arrow
        "\u2191": "^",   # upwards arrow
        "\u2193": "v",   # downwards arrow
        "\u00A0": " ",   # non-breaking space -> regular space
        "\u2007": " ",   # figure space
        "\u2028": "\n",  # line separator
        "\u2029": "\n",  # paragraph separator
    }
    for src, dst in _unicode_map.items():
        s = s.replace(src, dst)
    # 2) Drop ASCII control chars except newline / tab.
    s = "".join(ch for ch in s if (ch == "\n" or ch == "\t" or ord(ch) >= 32))
    # 3) Belt-and-suspenders: anything still above U+00FF (Latin-1 boundary)
    #    gets stripped rather than crash the renderer.
    return "".join(ch for ch in s if ord(ch) <= 0xFF)


def _money(value) -> float:
    """Coerce a user-entered amount string to float; returns 0 on failure."""
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        try:
            return float(value)
        except Exception:
            return 0.0


def _normalize_line_items(items) -> list:
    """Filter to {description, amount} dicts; drop empty rows."""
    out = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        desc = (it.get("description") or "").strip()
        amt = _money(it.get("amount"))
        if not desc and amt == 0:
            continue
        out.append({"description": desc, "amount": amt})
    return out


# Cached server timezone (IANA name). Read once from system_settings.key=
# 'server_timezone', validated via zoneinfo. Defaults to America/New_York so
# the system always has an Eastern baseline for server-side date math
# (automation triggers, daily-quote rollover, task reminders, mail retention).
# User-facing displays do NOT use this — they render in the browser's local TZ.
_SERVER_TZ_CACHE: str | None = None
_SERVER_TZ_DEFAULT = "America/New_York"


def _server_timezone() -> str:
    """Return the configured server timezone (IANA name). Cached after first call."""
    global _SERVER_TZ_CACHE
    if _SERVER_TZ_CACHE is not None:
        return _SERVER_TZ_CACHE
    try:
        row = db.query_one("SELECT value FROM system_settings WHERE key = 'server_timezone'")
        if row and row.get("value"):
            tz = str(row["value"]).strip()
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)  # raises if invalid
                _SERVER_TZ_CACHE = tz
                return tz
            except Exception:
                pass
    except Exception:
        pass
    _SERVER_TZ_CACHE = _SERVER_TZ_DEFAULT
    return _SERVER_TZ_CACHE


def _server_now() -> "datetime":
    """Current time in the configured server timezone (naive, for SQL NOW() parity).

    Use this anywhere the server needs a "wall clock" timestamp for date math
    (e.g., is it past 5pm today?). Strip the tzinfo before INSERT because our
    timestamp columns are TIMESTAMP WITHOUT TIME ZONE stored as the server's
    local wall clock.
    """
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(_server_timezone())
        return datetime.now(tz).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def _insert_feed_notification(feed: dict, entry: dict) -> None:
    """Insert a notification row for every user targeted by the feed.

    Module-level wrapper so the background poller (which runs in its own
    thread context) can call us directly without needing a KanbanHandler
    instance. The class method on KanbanHandler just delegates here.
    """
    feed_id = feed.get("id") if isinstance(feed, dict) else feed["id"]
    feed_name = (feed.get("name") if isinstance(feed, dict) else feed.get("name")) or "Service"
    accent = (feed.get("accent_color") if isinstance(feed, dict) else feed.get("accent_color")) or "#e6a700"
    title = ((entry.get("title") if isinstance(entry, dict) else entry["title"]) or "(no title)").strip()
    snippet = ((entry.get("summary") if isinstance(entry, dict) else entry.get("summary")) or "").strip()
    import re as _re
    snippet_plain = _re.sub(r"<[^>]+>", " ", snippet).strip()
    url = ((entry.get("link") if isinstance(entry, dict) else entry.get("link")) or "").strip()
    message = f"{feed_name}: {title}"
    if len(message) > 200:
        message = message[:197] + "..."
    payload = {
        "event_title": title,
        "snippet": snippet_plain[:240],
        "url": url,
        "color": accent,
        "feed_id": feed_id,
        "feed_name": feed_name,
    }
    target_user_ids = _resolve_feed_target_user_ids(feed if isinstance(feed, dict) else {
        "target_mode": feed.get("target_mode"),
        "target_user_ids": feed.get("target_user_ids"),
    })
    if not target_user_ids:
        return
    try:
        for uid in target_user_ids:
            try:
                db.execute(
                    """INSERT INTO notifications
                       (type, opportunity_id, actor_user_id, message, payload)
                       VALUES (%s, NULL, NULL, %s, %s::jsonb)""",
                    ("service_feed", message, json.dumps(payload)),
                )
            except Exception:
                pass
    except Exception:
        pass


def _normalize_target_user_ids(raw) -> str:
    """Coerce a target_user_ids payload (list / JSON-encoded string / None) into a Postgres int[] literal."""
    if raw is None:
        return "{}"
    if isinstance(raw, str):
        # Accept JSON-encoded list.
        try:
            raw = json.loads(raw)
        except Exception:
            return "{}"
    if not isinstance(raw, list):
        return "{}"
    parts: list[str] = []
    for v in raw:
        try:
            parts.append(str(int(v)))
        except (TypeError, ValueError):
            pass
    return "{" + ",".join(parts) + "}"


def _resolve_feed_target_user_ids(feed: dict) -> list[int]:
    """Resolve the list of user IDs to notify for a service feed.

    target_mode='all'      → every active user.
    target_mode='specific' → only users whose id is in feed.target_user_ids.
    """
    mode = feed.get("target_mode") or "all"
    if mode == "specific":
        raw = feed.get("target_user_ids")
        if raw is None:
            return []
        if isinstance(raw, str):
            # psycopg2 returns PG arrays as "{1,2,3}" strings when no
            # typecaster is registered. Parse to a list of ints.
            try:
                return [int(x) for x in raw.strip("{}").split(",") if x.strip()]
            except Exception:
                return []
        if isinstance(raw, list):
            try:
                return [int(x) for x in raw if str(x).strip()]
            except Exception:
                return []
        return []
    # 'all' — every active user (including admins).
    try:
        rows = db.query("SELECT id FROM users WHERE is_active = TRUE")
        return [int(r[0]) for r in rows] if rows else []
    except Exception:
        return []


def _resolve_document_template(template_id) -> dict | None:
    """Fetch a document template by id, or fall back to is_default=TRUE."""
    if template_id:
        try:
            row = db.query_one(
                "SELECT * FROM document_templates WHERE id = %s",
                (int(template_id),),
            )
            if row:
                return row
        except Exception:
            pass
    try:
        row = db.query_one(
            "SELECT * FROM document_templates WHERE is_default = TRUE ORDER BY id LIMIT 1"
        )
        if row:
            return row
    except Exception:
        pass
    # Final fallback: first row
    try:
        row = db.query_one(
            "SELECT * FROM document_templates ORDER BY id LIMIT 1"
        )
        if row:
            return row
    except Exception:
        pass
    # No templates at all — return a hardcoded default so generation still works.
    return {
        "id": None,
        "company_name": "",
        "company_address": "",
        "company_city_state_zip": "",
        "company_phone": "",
        "company_email": "",
        "primary_color": "#1a1a1a",
        "invoice_title": "INVOICE",
        "coc_title": "CERTIFICATE OF COMPLETION",
        "default_terms": "Due on receipt",
        "default_sales_rep": "",
        "default_line_items": "[]",
        "show_signature": False,
        "signature_name": "",
        "signature_path": None,
        "logo_path": None,
    }


def _doc_draw_invoice_table_header(pdf, margin_l, usable_w, header_h, r_c, g_c, b_c):
    """Render the invoice line-items table header row (used after page breaks)."""
    pdf.set_fill_color(240, 240, 240)
    pdf.set_xy(margin_l, 0.7)
    pdf.cell(usable_w, header_h, "", fill=True, ln=1)
    pdf.set_xy(margin_l, 0.7)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(r_c, g_c, b_c)
    pdf.cell(usable_w - 1.3, header_h, "DESCRIPTION", align="L")
    pdf.cell(1.3, header_h, "AMOUNT", align="R")
    pdf.set_text_color(0, 0, 0)


def _is_limited(user: dict) -> bool:
    """True if user has the limited role (own deals only, no CRM mail)."""
    return user.get("role") == "limited"


def _mail_visible_accounts_sql(user_id: int) -> str:
    """SQL predicate fragment: accounts visible to user (company inbox, own, or shared)."""
    return (
        "(is_crm_mail = TRUE OR owner_user_id = %s "
        "OR id IN (SELECT account_id FROM mail_account_access WHERE user_id = %s))"
    )


def _mail_account_accessible(user: dict, account_id: int) -> bool:
    """True if user can view/message an account: company inbox, owner, admin, or shared."""
    row = db.query_one(
        "SELECT is_crm_mail, owner_user_id FROM mail_accounts WHERE id = %s", (account_id,)
    )
    if not row:
        return False
    if row["is_crm_mail"]:
        return True
    if user.get("is_admin"):
        return True
    if row["owner_user_id"] == user["id"]:
        return True
    granted = db.query_one(
        "SELECT 1 FROM mail_account_access WHERE account_id = %s AND user_id = %s",
        (account_id, user["id"]),
    )
    return granted is not None


def _mail_account_manageable(user: dict, account_id: int) -> bool:
    """True if user can update/delete/share an account: owner or admin."""
    if user.get("is_admin"):
        return True
    row = db.query_one("SELECT owner_user_id FROM mail_accounts WHERE id = %s", (account_id,))
    return row is not None and row["owner_user_id"] == user["id"]


def _get_system_smtp_account() -> dict | None:
    """Find the best SMTP config for system emails (password resets, 2FA, notifications).
    Priority: 1) Alternate SMTP if enabled, 2) CRM mail account.
    Returns dict with SMTP settings or None if nothing configured."""
    # Check alternate SMTP first
    try:
        alt_row = db.query_one("SELECT value FROM system_settings WHERE key = 'alternate_smtp'")
        if alt_row and alt_row["value"]:
            alt = json.loads(alt_row["value"])
            if alt.get("enabled") and alt.get("host"):
                return {
                    "email": alt.get("from_email", ""),
                    "smtp_host": alt["host"],
                    "smtp_port": int(alt.get("port", 587)),
                    "smtp_user": alt.get("user", ""),
                    "smtp_password_encrypted": alt.get("password", ""),
                    "oauth_provider": None,
                    "oauth_access_token": None,
                }
    except Exception:
        logger.debug("Failed to load alternate SMTP config", exc_info=True)
    # Fall back to CRM mail account
    row = db.query_one(
        """SELECT id, email, smtp_host, smtp_port, smtp_user, smtp_password_encrypted,
                  oauth_provider, oauth_access_token, oauth_refresh_token, oauth_token_expires
           FROM mail_accounts WHERE is_crm_mail = TRUE AND smtp_host IS NOT NULL
           ORDER BY id LIMIT 1"""
    )
    if not row:
        return None
    acct = dict(row)
    # Refresh OAuth token if expired
    if acct.get("oauth_provider") and acct.get("oauth_refresh_token"):
        try:
            expires = acct.get("oauth_token_expires")
            if expires and expires < datetime.now(timezone.utc):
                prov = oauth_providers.get_provider(acct["oauth_provider"])
                tok = prov.refresh_token(acct["oauth_refresh_token"])
                if tok and tok.get("access_token"):
                    acct["oauth_access_token"] = tok["access_token"]
                    new_refresh = tok.get("refresh_token", acct["oauth_refresh_token"])
                    new_expires = tok.get("expires_in", 3600)
                    db.execute(
                        "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, "
                        "oauth_token_expires = to_timestamp(%s) WHERE id = %s",
                        (tok["access_token"], new_refresh, int(time.time()) + new_expires, acct["id"]),
                    )
        except Exception:
            logger.exception("Failed to refresh OAuth token for system SMTP")
    return acct


def _send_system_email(to_addr: str, subject: str, html_body: str) -> bool:
    """Send email via CRM mail account. Returns True on success, False if no CRM account or send failed."""
    from smtp_client import send_email_from_account
    acct = _get_system_smtp_account()
    if not acct:
        logger.warning("No CRM mail account configured for system email to %s", to_addr)
        return False
    use_oauth = bool(acct.get("oauth_provider") and acct.get("oauth_access_token")
                     and not acct.get("smtp_password_encrypted"))
    ok, err = send_email_from_account(
        smtp_host=acct.get("smtp_host") or "",
        smtp_port=int(acct.get("smtp_port") or 587),
        smtp_user=acct.get("smtp_user") or acct.get("email", ""),
        smtp_password=acct.get("smtp_password_encrypted") or "",
        from_name="Sietch CRM",
        from_addr=acct.get("email", ""),
        to_addr=to_addr,
        subject=subject,
        html_body=html_body,
        oauth_provider=acct["oauth_provider"] if use_oauth else None,
        oauth_access_token=acct["oauth_access_token"] if use_oauth else None,
    )
    if not ok:
        logger.warning("System email to %s failed via %s: %s", to_addr, acct.get("email"), err)
    return ok


def _mail_message_accessible(user: dict, message_id: int) -> bool:
    """True if user can act on a specific message (its account is accessible)."""
    row = db.query_one("SELECT account_id FROM mail_messages WHERE id = %s", (message_id,))
    if not row:
        return False
    return _mail_account_accessible(user, row["account_id"])


def _mail_draft_accessible(user: dict, draft_id: int) -> bool:
    """True if user can act on a specific outgoing draft (its account is accessible)."""
    row = db.query_one("SELECT account_id FROM mail_outgoing WHERE id = %s", (draft_id,))
    if not row:
        return False
    return _mail_account_accessible(user, row["account_id"])


# ── IMAP sync helpers ──────────────────────────────────────────────────────────
# Best-effort: if IMAP fails, the DB is still updated; we log and continue.

def _imap_connect(account_id: int):
    """Connect to IMAP for the given account. Returns MailBox or None."""
    from imap_tools import MailBox, A
    acct = db.query_one(
        "SELECT email, imap_host, imap_port, password_encrypted, oauth_provider, oauth_access_token "
        "FROM mail_accounts WHERE id = %s",
        (account_id,),
    )
    if not acct or not acct.get("imap_host"):
        return None
    port = int(acct.get("imap_port") or 993)
    mb = MailBox(acct["imap_host"], port=port)
    if acct.get("oauth_provider"):
        token = acct.get("oauth_access_token") or ""
        if not token:
            return None
        mb.xoauth2(acct["email"], token)
    else:
        pwd = acct.get("password_encrypted") or ""
        if not pwd:
            return None
        mb.login(acct["email"], pwd)
    return mb


def _is_synthetic_uid(uid: str | None) -> bool:
    """Return True for locally-generated UIDs that do not exist on the IMAP server."""
    return bool(uid) and str(uid).startswith("sent:outgoing:")


def _imap_set_seen(account_id: int, folder: str, uid: str, seen: bool) -> bool:
    """Set or unset \\Seen flag on IMAP (mark read/unread)."""
    if _is_synthetic_uid(uid):
        return True
    try:
        mb = _imap_connect(account_id)
        if not mb:
            return False
        mb.folder.set(folder)
        mb.flag([uid], "\\Seen", seen)
        mb.logout()
        return True
    except Exception as e:
        logger.warning("IMAP set_seen failed (account %d, folder %s, uid %s): %s", account_id, folder, uid, e)
        return False


def _imap_set_flagged(account_id: int, folder: str, uid: str, flagged: bool) -> bool:
    """Set or unset \\Flagged flag on IMAP (star)."""
    if _is_synthetic_uid(uid):
        return True
    try:
        mb = _imap_connect(account_id)
        if not mb:
            return False
        mb.folder.set(folder)
        mb.flag([uid], "\\Flagged", flagged)
        mb.logout()
        return True
    except Exception as e:
        logger.warning("IMAP set_flagged failed (account %d, uid %s): %s", account_id, uid, e)
        return False


def _imap_move(account_id: int, folder: str, uid: str, dest_folder: str) -> bool:
    """Move a message between IMAP folders."""
    if _is_synthetic_uid(uid):
        return True
    try:
        mb = _imap_connect(account_id)
        if not mb:
            return False
        mb.folder.set(folder)
        mb.move([uid], dest_folder)
        mb.logout()
        return True
    except Exception as e:
        logger.warning("IMAP move failed (account %d, uid %s → %s): %s", account_id, uid, dest_folder, e)
        return False


def _imap_delete(account_id: int, folder: str, uid: str) -> bool:
    """Permanently delete a message from IMAP."""
    if _is_synthetic_uid(uid):
        return True
    try:
        mb = _imap_connect(account_id)
        if not mb:
            return False
        mb.folder.set(folder)
        mb.delete([uid])
        mb.logout()
        return True
    except Exception as e:
        logger.warning("IMAP delete failed (account %d, uid %s): %s", account_id, uid, e)
        return False


def _parse_iso_datetime(value: str):
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
    except Exception:
        return None


# ── JWT helpers (HS256, minimal stdlib implementation) ─────────────────────────


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _sign_jwt(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    enc_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    enc_payload = _b64url_encode(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    signing_input = f"{enc_header}.{enc_payload}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{enc_header}.{enc_payload}.{_b64url_encode(sig)}"


def _verify_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        signing_input = f"{parts[0]}.{parts[1]}".encode("utf-8")
        sig = _b64url_decode(parts[2])
        expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        return payload
    except Exception:
        return None


def _detect_container_runtime() -> str:
    """Best-effort detection of the container runtime we're running inside.

    Returns one of "docker", "kubernetes", "podman", or "" (host / unknown).

    Checks several signals because no single one is reliable:
      - `/.dockerenv` is present inside Docker containers (not on bare hosts).
      - `/run/.containerenv` is the Podman equivalent.
      - `KUBERNETES_SERVICE_HOST` is injected into every pod.
      - cgroup paths: v1 lines look like `12:memory:/docker/<id>`; cgroup v2
        collapses to a single `0::/` line on many hosts, so a substring match
        on "docker" alone is not sufficient. We look for the path segments
        that runtimes actually use.

    The previous implementation checked only /proc/1/cgroup for the literal
    substring "docker", and (because it called f.read() twice on the same
    handle) never matched "kubepods" at all — the second read returned "".
    """
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.path.exists("/run/.containerenv"):
        return "podman"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    # cgroup inspection as a fallback. Read each candidate file ONCE — a
    # second read on the same handle returns "" at EOF.
    for cgroup_path in ("/proc/1/cgroup", "/proc/self/cgroup"):
        try:
            with open(cgroup_path, "r") as f:
                content = f.read()
        except OSError:
            continue
        if not content:
            continue
        if "kubepods" in content:
            return "kubernetes"
        # cgroup v1 exposes the runtime as a path segment; v2 does not, so
        # these substrings only fire on hosts that still use v1 — which is
        # exactly the fallback we want.
        if "/docker/" in content or "docker-" in content:
            return "docker"
        if "libpod" in content or "/podman/" in content:
            return "podman"
    return ""


def _is_running_in_docker() -> bool:
    """True when running inside any container runtime (docker/k8s/podman)."""
    return _detect_container_runtime() != ""


def _effective_docs_internal_url() -> str:
    """Return the URL the CRM should use to reach the Document Server.

    Priority order:
      1. DOCS_INTERNAL_URL – if set to a real URL (not the old "http://docserver:8080"
         placeholder), return it directly.  This can be a Docker service hostname
         ("https://onlyoffice-docserver:443"), a local loopback
         ("https://127.0.0.1:9443"), or any other address the CRM can reach.
      2. DOCS_PUBLIC_URL – fallback for legacy setups or when the server runs on
         the host but no internal URL was configured.

    In development with the dashboard running on the host and the docserver in a
    Docker container, set DOCS_INTERNAL_URL to https://127.0.0.1:9443 so the CRM
    talks directly to the container's mapped port without going through the
    localtonet tunnel (faster, less latency).

    In production Docker Compose, set DOCS_INTERNAL_URL to the service name,
    e.g. http://docserver:80 or https://onlyoffice-docserver:443. The old
    placeholder "http://docserver:8080" is ignored.
    """
    if DOCS_INTERNAL_URL and not DOCS_INTERNAL_URL.startswith("http://docserver"):
        return DOCS_INTERNAL_URL
    return DOCS_PUBLIC_URL or DOCS_INTERNAL_URL


def _proxy_document_server(method: str, ds_path: str, body: bytes | None = None, headers: dict | None = None, timeout: int = 30) -> tuple:
    """Forward a request to the internal OnlyOffice Document Server. Returns (status, body, content_type).

    NOTE: This function bypasses SSL certificate verification because the local
    OnlyOffice Document Server typically uses a self-signed certificate (both in
    local dev and production). The same approach is used in
    _download_from_docserver().  If the Document Server ever gets a proper
    certificate from a trusted CA, this bypass can be removed.
    """
    ds_url = _effective_docs_internal_url()
    if not ds_url:
        return 503, b'{"error": "Document Server not configured"}', "application/json"
    url = f"{ds_url}{ds_path}"
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers, unverifiable=True)
    ctx = None
    if ds_url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "application/json")
    except Exception:
        log_infra_event("error", f"Document Server unreachable: {ds_path}", source="docserver", method=method, path=ds_path)
        return 502, b'{"error": "Document Server unreachable"}', "application/json"


def _download_from_docserver(url: str, timeout: int = 60) -> bytes:
    """Download a file from OnlyOffice Document Server URL with JWT auth and self-signed cert bypass."""
    token = _sign_jwt({"ts": int(time.time())}, DOCS_JWT_SECRET)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


# ── Infrastructure helpers ─────────────────────────────────────────────────────

DB_CONTAINER_NAME = os.environ.get("DB_CONTAINER_NAME", "sietch-db")
REDIS_CONTAINER_NAME = os.environ.get("REDIS_CONTAINER_NAME", "sietch-redis")


def _probe_docserver() -> tuple:
    """Probe the Document Server /healthcheck endpoint.

    Returns (reachable: bool, latency_ms: float | None, error: str | None).
    Logs to the infra ring only on reachability state transitions to avoid noise.
    """
    import infra_log
    ds_url = _effective_docs_internal_url()
    if not ds_url:
        return False, None, "Document Server not configured"
    url = f"{ds_url.rstrip('/')}/healthcheck"
    ctx = None
    if ds_url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        start = time.monotonic()
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=5, context=ctx) as resp:
            latency_ms = round((time.monotonic() - start) * 1000, 1)
        ok = resp.status == 200
        if ok != infra_log._last_docserver_ok:
            if ok:
                log_infra_event("info", f"Document Server healthy ({latency_ms}ms)", source="docserver", path="/healthcheck")
            else:
                log_infra_event("warn", f"Document Server /healthcheck returned HTTP {resp.status}", source="docserver", path="/healthcheck")
            infra_log._last_docserver_ok = ok
        if not ok:
            return False, latency_ms, f"HTTP {resp.status}"
        return True, latency_ms, None
    except Exception as exc:
        if infra_log._last_docserver_ok is not False:
            log_infra_event("error", f"Document Server unreachable: {exc}", source="docserver", path="/healthcheck")
            infra_log._last_docserver_ok = False
        return False, None, str(exc)


def _docker_containers() -> list[dict]:
    """Return status for the known Compose siblings when the docker CLI is available."""
    names = [
        (CONTAINER_NAME, "Dashboard"),
        (DB_CONTAINER_NAME, "Database"),
        (DOCSERVER_CONTAINER_NAME, "Document Server"),
        (REDIS_CONTAINER_NAME, "Redis"),
    ]
    out: list[dict] = []
    for name, label in names:
        if not name:
            continue
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}} {{.RestartCount}}", name],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue  # container not present on this host
        parts = result.stdout.strip().split()
        # RestartCount renders as "N/A" for containers running under a
        # non-Docker runtime (Podman/K8s via the docker shim), which would
        # raise ValueError and take out the whole container list.
        try:
            restart_count = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            restart_count = None
        out.append({
            "name": name,
            "label": label,
            "status": parts[0] if parts else "unknown",
            "restartCount": restart_count,
        })
    return out


def _disk_usage() -> list[dict]:
    """Disk usage for the app's data/upload directories (via shutil, no deps).

    Entries are deduplicated by *filesystem*, not by path string. In Docker
    the app root, data dir and uploads dir are typically the same overlay/volume,
    so reporting all three shows the same numbers three times. Previously the
    cwd was also included, which in a host-run deployment is wherever the
    process happened to be launched from — a meaningless entry.
    """
    candidates = [
        os.environ.get("DATA_DIR", ""),
        os.environ.get("UPLOAD_DIR", ""),
        "/app/data",
        "/app/uploads",
        # Host-run dev: data lives next to the repo rather than under /app.
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
    ]
    paths: list[str] = []
    seen_fs: set[tuple] = set()
    for p in candidates:
        if not p or not os.path.isdir(p):
            continue
        p = os.path.abspath(p)
        try:
            st = os.stat(p)
        except OSError:
            continue
        key = (st.st_dev,)
        if key in seen_fs:
            continue
        seen_fs.add(key)
        paths.append(p)
    out = []
    for p in paths:
        try:
            usage = shutil.disk_usage(p)
            out.append({
                "path": p,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            })
        except Exception:
            continue
    return out


def _mem_usage() -> dict:
    """Parse /proc/meminfo into {total, available, used} in bytes.

    MemAvailable only exists on Linux 3.14+. On an older kernel it is
    absent, so the previous code's `data.get("MemAvailable", 0)` returned 0
    and reported used == total (100%) for a perfectly healthy box. Fall
    back to the classic MemFree + Buffers + Cached approximation, which is
    available on every kernel.
    """
    try:
        with open("/proc/meminfo") as f:
            data: dict[str, int] = {}
            for line in f:
                key, _, val = line.partition(":")
                parts = val.split()
                if not parts:
                    continue
                try:
                    data[key.strip()] = int(parts[0]) * 1024  # kB → bytes
                except ValueError:
                    continue
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", 0)
        if available == 0:
            # Pre-3.14 approximation: reclaimable = free + buffers + cache.
            available = (
                data.get("MemFree", 0)
                + data.get("Buffers", 0)
                + data.get("Cached", 0)
                + data.get("SReclaimable", 0)
            )
        available = min(available, total) if total else available
        return {
            "total": total,
            "available": available,
            "used": max(total - available, 0),
        }
    except Exception:
        return {}


def _load_avg() -> list | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except Exception:
        return None


def _infra_error_stats() -> dict:
    """Count errors/warns from the ring buffer and top recurring error messages."""
    now = time.time()
    last1h = 0
    last24h = 0
    total_errors = 0
    total_warns = 0
    recent_msgs: dict[str, int] = {}
    for e in infra_events():
        level = e.get("level", "info")
        if level == "error":
            total_errors += 1
        elif level == "warn":
            total_warns += 1
        try:
            age_s = now - datetime.fromisoformat(e.get("ts", "")).timestamp()
        except Exception:
            continue
        if level == "error":
            if age_s <= 3600:
                last1h += 1
            if age_s <= 86400:
                last24h += 1
                msg = e.get("msg", "")
                if len(msg) > 120:
                    # Use the same ellipsis character as the rest of the app
                    # (the bot formatters, the task list, the changelog) rather
                    # than three dots.
                    msg = msg[:119] + "…"
                recent_msgs[msg] = recent_msgs.get(msg, 0) + 1
    top = sorted(recent_msgs.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "last1h": last1h,
        "last24h": last24h,
        "totalErrors": total_errors,
        "totalWarns": total_warns,
        "top": [{"msg": m, "count": c} for m, c in top],
    }


def _backup_summary() -> dict:
    try:
        import backup_service
        status = backup_service.get_backup_status()
        last = status.get("last_backup") or {}
        return {
            "enabled": status.get("enabled", False),
            "lastStatus": last.get("status", "none"),
            "lastTs": last.get("ts"),
            "lastSize": last.get("size"),
            "lastError": last.get("error"),
            "nextBackupTs": status.get("next_backup_ts"),
        }
    except Exception as exc:
        return {"enabled": False, "lastStatus": "none", "lastTs": None, "lastError": f"backup_service error: {exc}"}


def _docker_status() -> dict:
    """Combined Docker access + container status. Falls back to probes when no CLI."""
    accessible = shutil.which("docker") is not None
    runtime = _detect_container_runtime()
    in_docker = bool(runtime)
    containers = _docker_containers() if accessible else []
    base = {
        "accessible": accessible,
        "inDocker": in_docker,
        "runtime": runtime or None,
        "containerName": socket.gethostname() if in_docker else None,
        "containers": containers,
    }
    if in_docker and not accessible:
        # We ARE in a container but can't reach the Docker socket. Surface a
        # runtime-accurate note so the admin UI doesn't claim we're on a
        # bare host when we're actually in Kubernetes or Podman.
        runtime_label = {"kubernetes": "Kubernetes", "podman": "Podman"}.get(runtime, "Docker")
        base["note"] = (
            f"{runtime_label} CLI/socket not available inside this container. "
            "Install the docker CLI and mount /var/run/docker.sock to see "
            "container status and use restart controls."
        )
        return base
    return {**base, "note": ""}


# Field types accepted for a custom field definition. Keep in sync with the
# <select id="admin-cf-type"> options in public/index.html — 'textarea' and
# 'url' are omitted from neither, and existing records use all of these.
CUSTOM_FIELD_TYPES = frozenset({
    "text", "textarea", "url", "number", "date", "select", "checkbox",
})

# Image extensions accepted for template logos / signatures. SVGs are
# deliberately excluded: they can carry <script> and <foreignObject>,
# and a "logo.png" that's actually an SVG would still bypass a
# Content-Type-only browser sniff. The avatar upload also rejects them.
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Maximum logo / signature file size. Matches the avatar upload cap.
# Server-side cap is the only one - browsers honouring a client cap is
# advisory.
MAX_LOGO_BYTES = 5 * 1024 * 1024


def _validate_logo_upload(parsed_file: dict) -> tuple[bytes | None, str | None]:
    """Apply size / content-type / extension allowlist to an uploaded logo.

    Returns (data, None) on success or (None, error_message) on any
    rejection. Centralised so the photo, document and signature upload
    handlers all use the same rules - the previous version had a 5 MB
    cap and image/* check on the avatar upload but not on these, which
    let an admin upload arbitrary content (HTML, SVG, executables) as a
    "logo" and the server stored it under data/logos/.
    """
    if not parsed_file:
        return None, "file field required"
    data = parsed_file["data"]
    if not isinstance(data, (bytes, bytearray)):
        return None, "file field must be binary"
    if len(data) > MAX_LOGO_BYTES:
        return None, f"File too large (max {MAX_LOGO_BYTES // (1024*1024)} MB)"
    mime = (parsed_file.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return None, f"Only image files are supported (got {mime or 'no content-type'})"
    filename = parsed_file.get("filename") or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return None, f"File extension {ext!r} not allowed (use {', '.join(sorted(ALLOWED_LOGO_EXTENSIONS))})"
    return bytes(data), None


def _safe_logo_filename(raw_name: str) -> str:
    """Strip everything except word chars, dots and dashes.

    The allowed extension list is enforced separately; this is purely a
    filesystem-safety filter (no path separators, no spaces, no NULs).
    """
    return re.sub(r"[^\w.\-]", "_", raw_name)


_EXT_TO_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "csv": "text/csv",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "heic": "image/heic",
    "heif": "image/heif",
}

# Reverse lookup for generating extensions from stored MIME types.
# Only the first entry is kept when multiple extensions map to the same MIME.
_MIME_TO_EXT = {mime: f".{ext}" for ext, mime in _EXT_TO_MIME.items()}


def _guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lstrip(".").lower()
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


def _extract_exif(img) -> dict | None:
    """Return a JSON-serializable subset of EXIF data from a Pillow image."""
    if not hasattr(img, "getexif"):
        return None
    exif = img.getexif()
    if not exif:
        return None
    out = {}
    # Selected EXIF tags: Make, Model, DateTime, DateTimeOriginal, GPSInfo, FNumber, ISOSpeedRatings, FocalLength, ExposureTime
    tags = {
        0x010F: "make",
        0x0110: "model",
        0x0132: "dateTime",
        0x9003: "dateTimeOriginal",
        0x829D: "fNumber",
        0x8827: "isoSpeedRatings",
        0x920A: "focalLength",
        0x829A: "exposureTime",
    }
    for code, key in tags.items():
        val = exif.get(code)
        if val:
            out[key] = str(val)
    gps = exif.get(0x8825)
    if gps:
        gps_out = {}
        for code, key in {1: "gpsLatitudeRef", 2: "gpsLatitude", 3: "gpsLongitudeRef", 4: "gpsLongitude", 6: "gpsAltitude"}.items():
            val = gps.get(code)
            if val:
                gps_out[key] = str(val)
        if gps_out:
            out["gps"] = gps_out
    return out if out else None


def _document_type_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in ("docx", "doc", "txt", "rtf", "odt"):
        return "word"
    if ext in ("xlsx", "xls", "csv", "ods"):
        return "cell"
    if ext in ("pptx", "ppt", "odp"):
        return "slide"
    if ext in ("pdf", "djvu", "xps", "oxps"):
        return "pdf"
    return "word"


def _record_document_activity(doc_id: int, user_id: int | None, action: str, meta: dict | None = None, dedup_seconds: int = 0) -> None:
    """Best-effort insert into document_activity. Never raises.

    dedup_seconds: when > 0, skip insert if same (doc_id, user_id, action) row
    exists within the window. Used to keep 'opened' writes sane when the preview
    iframe reloads every few seconds.
    """
    try:
        import json
        if dedup_seconds > 0 and user_id is not None:
            recent = db.query(
                "SELECT 1 FROM document_activity WHERE document_id=%s AND user_id=%s AND action=%s AND created_at > NOW() - make_interval(secs => %s) LIMIT 1",
                (doc_id, user_id, action, dedup_seconds),
                fetch="one",
            )
            if recent:
                return
        params = [doc_id, user_id, action]
        meta_sql = ""
        if meta is not None:
            meta_sql = ", meta"
            params.append(json.dumps(meta))
        db.execute(
            f"INSERT INTO document_activity (document_id, user_id, action{meta_sql}) VALUES (%s, %s, %s{', %s' if meta_sql else ''})",
            tuple(params),
        )
    except Exception as exc:
        logger.debug("document_activity insert failed: %s", exc)


def _build_onlyoffice_customization(brand_label: str = "Sietch CRM") -> dict:
    """Shared customization block for both document and mail-attachment editor configs.

    Pulled out so any change to the Sietch branding or feature toggles happens in
    one place. Values are conservative defaults; user-level toggles (track changes
    on/off, modes) still flow through OnlyOffice's own editor toolbar.
    """
    public_base = (CRM_PUBLIC_URL or DOCS_PUBLIC_URL or "").rstrip("/")
    www = public_base.replace("https://", "").replace("http://", "").strip("/")
    return {
        "chat": True,
        "comments": True,
        "spellcheck": True,
        "autosave": True,
        "forcesave": True,
        "compactHeader": False,
        "toolbarNoTabs": False,
        "hideRightMenu": False,
        "review": {
            "showReviewChanges": True,
            "reviewDisplay": "markup",
        },
        "logo": {
            "image": f"{public_base}/ship-logo-white.png" if public_base else "",
            "imageDark": f"{public_base}/ship-logo-white.png" if public_base else "",
            "url": public_base or None,
        },
        "customer": {
            "name": brand_label,
            "www": www,
        },
    }


def _build_onlyoffice_permissions(can_edit: bool) -> dict:
    """Document permissions block. Always lets users download/print/copy; review
    and fillForms are edit-only because they don't make sense on a read-only doc."""
    return {
        "download": True,
        "edit": can_edit,
        "print": True,
        "copy": True,
        "comment": True,
        "chat": True,
        "fillForms": can_edit,
        "modifyFilter": can_edit,
        "modifyContentControl": can_edit,
        "review": can_edit,
        "protect": True,
    }


def _parse_view_mode_override(qs_value: str | None) -> str | None:
    """Return 'view' if the client asked for view-only, None otherwise.

    Used by the preview pane iframe; ignored when called from a regular editor
    tab so existing open-in-new-tab flows are untouched.
    """
    if qs_value and str(qs_value).lower() in ("view", "1", "true"):
        return "view"
    return None


def _blank_docx_bytes() -> bytes:
    """Return a minimal valid .docx file (OOXML zip with empty document)."""
    import zipfile, io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        zf.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        zf.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t></w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def _blank_xlsx_bytes() -> bytes:
    """Return a minimal valid .xlsx file (OOXML zip with empty workbook)."""
    import zipfile, io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>')
    return buf.getvalue()


def _parse_multipart(body: bytes, content_type: str) -> dict:
    """Minimal multipart/form-data parser. Returns {fieldName: {filename, data, content-type}}."""
    # Parse boundary
    ct_parts = [p.strip() for p in content_type.split(";")]
    boundary = None
    for part in ct_parts:
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
            break
    if not boundary:
        raise ValueError("boundary missing")
    b_boundary = ("--" + boundary).encode("latin-1")
    b_end = ("--" + boundary + "--").encode("latin-1")
    parts = body.split(b_boundary)
    result = {}
    for part in parts[1:]:
        part = part.lstrip(b"\r\n")
        if part.startswith(b_end):
            continue
        try:
            header_end = part.index(b"\r\n\r\n")
        except ValueError:
            continue
        headers_raw = part[:header_end].decode("latin-1")
        data = part[header_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        # parse Content-Disposition
        cd_match = re.search(r'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]+)")?', headers_raw, re.IGNORECASE)
        if not cd_match:
            continue
        name = cd_match.group(1)
        filename = cd_match.group(2)
        ct_match = re.search(r'Content-Type:\s*([^\r\n]+)', headers_raw, re.IGNORECASE)
        content_type_part = ct_match.group(1).strip() if ct_match else None
        if filename:
            result[name] = {"filename": filename, "data": data, "content-type": content_type_part}
        else:
            result[name] = {"data": data}
    return result


# ── Request Handler ────────────────────────────────────────────────────────────


_action_cat_id_cache = None

def _resolve_change_display(field, val):
    """Resolve a field value to a human-readable display string for history events."""
    if val is None or val == "" or val == 0:
        return "\u2014"
    if field == "stageId":
        row = db.query_one("SELECT title FROM stages WHERE id = %s", (val,))
        return row["title"] if row else str(val)
    if field == "contactId":
        row = db.query_one("SELECT first_name, last_name FROM contacts WHERE id = %s", (val,))
        if row:
            name = f"{row[0] or ''} {row[1] or ''}".strip()
            return name if name else str(val)
        return str(val)
    if field == "responsibleUserId":
        row = db.query_one("SELECT display_name FROM users WHERE id = %s", (val,))
        return row["display_name"] if row else str(val)
    if field == "bidValue":
        try:
            return f"${float(val):,.2f}"
        except (TypeError, ValueError):
            return str(val)
    if field == "isPrivate":
        return "Yes" if val else "No"
    return str(val)


def _ics_dump(events: list[dict]) -> str:
    """Serialize calendar events back to an .ics file (UTC). Pure."""
    def _esc(s) -> str:
        s = "" if s is None else str(s)
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def _fmt(dt, all_day: bool) -> str:
        if all_day:
            return dt.strftime("%Y%m%d")
        if dt.tzinfo is None:
            return dt.strftime("%Y%m%dT%H%M%S")
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Sietch CRM//Calendar//EN"]
    for ev in events or []:
        uid = ev.get("uid") or f"export-{int(time.time()*1000)}"
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{_esc(uid)}")
        lines.append(f"DTSTAMP:{_esc(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))}")
        dtstart = ev.get("dtstart")
        if dtstart:
            lines.append(f"DTSTART:{_esc(_fmt(dtstart, bool(ev.get('all_day'))))}")
        dtend = ev.get("dtend")
        if dtend:
            lines.append(f"DTEND:{_esc(_fmt(dtend, bool(ev.get('all_day'))))}")
        elif dtstart:
            lines.append(f"DTEND:{_esc(_fmt(dtstart, bool(ev.get('all_day'))))}")
        if ev.get("summary"):
            lines.append(f"SUMMARY:{_esc(ev.get('summary'))}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{_esc(ev.get('description'))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{_esc(ev.get('location'))}")
        if ev.get("rrule"):
            lines.append(f"RRULE:{_esc(ev.get('rrule'))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class KanbanHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        if args and isinstance(args[0], str) and args[0].startswith("GET /api/"):
            return
        super().log_message(fmt, *args)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _run_api_handler(self, fn) -> None:
        """Run an API handler, logging any unhandled exception to the infra ring."""
        try:
            fn()
        except Exception as exc:
            logger.exception("Unhandled error handling %s %s", self.command, self.path)
            user_email = ""
            try:
                tok = _session_token(self)
                if tok:
                    u = auth_mod.get_session_user(tok)
                    if u:
                        user_email = u.get("email", "")
            except Exception:
                pass
            log_infra_event(
                "error",
                f"Unhandled error: {type(exc).__name__}: {exc}",
                source="api", method=self.command, path=urlparse(self.path).path, user=user_email,
            )
            try:
                _json_response(self, 500, {"error": "Internal server error"})
            except Exception:
                pass

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._run_api_handler(self._handle_api_get)
            return
        # Rewrite /project/{id} → /project.html?id={id}
        # Must redirect so the browser URL has ?id= (JS reads window.location.search)
        import re as _re
        proj_match = _re.match(r"^/project/(\d+)(?:\?.*)?$", urlparse(self.path).path)
        if proj_match:
            proj_id = proj_match.group(1)
            qs = urlparse(self.path).query
            new_path = f"/project.html?id={proj_id}" + (f"&{qs}" if qs else "")
            self.send_response(302)
            self.send_header("Location", new_path)
            self.end_headers()
            return
        # Rewrite /reset → /reset.html (password reset landing page).
        # Same redirect pattern as /project/{id} so the token stays in
        # the URL query (?token=) where reset.html's script reads it.
        # The reset email links here; without this the link 404s (the
        # static root only serves files under public/).
        if urlparse(self.path).path == "/reset":
            qs = urlparse(self.path).query
            new_path = "/reset.html" + (f"?{qs}" if qs else "")
            self.send_response(302)
            self.send_header("Location", new_path)
            self.end_headers()
            return
        super().do_GET()

    def send_head(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return super().send_head()

        fs_path = self.translate_path(self.path)
        if os.path.isdir(fs_path):
            for index in ("index.html", "index.htm"):
                candidate = os.path.join(fs_path, index)
                if os.path.exists(candidate):
                    fs_path = candidate
                    break
            else:
                return super().send_head()
        elif not os.path.exists(fs_path):
            return super().send_head()

        ctype = self.guess_type(fs_path)
        try:
            with open(fs_path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return None

        accept = self.headers.get("Accept-Encoding", "")
        should_gzip = "gzip" in accept and len(data) > 1024
        if should_gzip:
            data = gzip.compress(data)

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if should_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        return io.BytesIO(data)

    def end_headers(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/v2/auth/login":
            self._handle_login()
            return
        if self.path == "/api/v2/auth/logout":
            self._handle_logout()
            return
        if self.path == "/api/v2/auth/twofa/verify":
            self._handle_2fa_verify()
            return
        if self.path == "/api/v2/auth/twofa/send":
            self._handle_2fa_resend()
            return
        if self.path == "/api/v2/auth/twofa/setup":
            self._handle_2fa_setup()
            return
        if self.path == "/api/v2/auth/twofa/verify-setup":
            self._handle_2fa_verify_setup()
            return
        if self.path == "/api/v2/auth/twofa/disable":
            self._handle_2fa_disable()
            return
        if self.path.startswith("/api/"):
            self._run_api_handler(lambda: self._handle_api_post_put("POST"))
            return
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:
        if self.path.startswith("/api/"):
            self._run_api_handler(lambda: self._handle_api_post_put("PUT"))
            return
        self.send_error(405)

    def do_DELETE(self) -> None:
        if self.path.startswith("/api/"):
            self._run_api_handler(lambda: self._handle_api_post_put("DELETE"))
            return
        self.send_error(405)

    def do_PATCH(self) -> None:
        if self.path.startswith("/api/"):
            self._run_api_handler(lambda: self._handle_api_post_put("PATCH"))
            return
        self.send_error(405)

    # ── Auth ────────────────────────────────────────────────────────────────

    def _handle_login(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return

        email = str(payload.get("email") or payload.get("userName") or "").strip()
        password = str(payload.get("password") or "")
        if not email or not password:
            _json_response(self, 400, {"error": "Email and password are required"})
            return

        user = auth_mod.authenticate_user(email, password)
        if not user:
            log_infra_event("warn", f"Login failed for {email}", source="auth", path="/api/v2/auth/login")
            _json_response(self, 401, {"error": "Invalid email or password"})
            return

        # ── 2FA: if enabled, send challenge instead of session ──
        twofa_method = user.get("twofa_method")
        if twofa_method:
            # Check that the user can actually receive codes via their chosen method
            if twofa_method == "telegram":
                from notification_dispatcher import _get_employee_chat_id
                chat_id = _get_employee_chat_id(user["id"])
                if not chat_id:
                    # Fall back to email if Telegram chat not linked
                    twofa_method = "email"

            challenge_token, code = auth_mod.create_2fa_challenge(user["id"], twofa_method)

            # Send the code
            if twofa_method == "email":
                if not _send_system_email(
                    user["email"],
                    "Your Sietch CRM Login Code",
                    f"<p>Your 2FA verification code is:</p><h2>{code}</h2><p>This code expires in 5 minutes.</p>",
                ):
                    _json_response(self, 500, {"error": "No CRM mail account configured. Add one in Admin → CRM Mail."})
                    return
            elif twofa_method == "telegram":
                from notification_dispatcher import _get_employee_chat_id, _send_telegram_message
                chat_id = _get_employee_chat_id(user["id"])
                if chat_id:
                    _send_telegram_message(chat_id, f"Your Sietch CRM login code: <b>{code}</b>\nExpires in 5 minutes.")

            _json_response(self, 200, {
                "ok": False,
                "requiresTwoFactor": True,
                "challenge": challenge_token,
                "twofaMethod": twofa_method,
            })
            return

        # ── No 2FA: create session directly ──
        ip = self.client_address[0] if self.client_address else ""
        token = auth_mod.create_session(user["id"], ip)

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        cookie = cookies.SimpleCookie()
        cookie[SESSION_COOKIE] = token
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Lax"
        if COOKIE_SECURE:
            cookie[SESSION_COOKIE]["secure"] = True
        self.send_header("Set-Cookie", cookie.output(header="").strip())
        body_out = json.dumps({
            "ok": True,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "displayName": user["display_name"],
                "isAdmin": user["is_admin"],
                "mustChangePassword": user["must_change_password"],
                "role": user.get("role", "member"),
            },
        }).encode("utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_2fa_verify(self) -> None:
        """Verify 2FA code during login. Returns session cookie."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        challenge = str(payload.get("challenge") or "")
        code = str(payload.get("code") or "")
        if not challenge or not code:
            _json_response(self, 400, {"error": "challenge and code are required"})
            return
        user_id = auth_mod.verify_2fa_challenge(challenge, code)
        if not user_id:
            _json_response(self, 401, {"error": "Invalid or expired code"})
            return
        ip = self.client_address[0] if self.client_address else ""
        token = auth_mod.create_session(user_id, ip)
        # Fetch user for response
        user_row = db.query_one(
            "SELECT id, email, display_name, is_admin, must_change_password, role FROM users WHERE id = %s",
            (user_id,),
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        cookie = cookies.SimpleCookie()
        cookie[SESSION_COOKIE] = token
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Lax"
        if COOKIE_SECURE:
            cookie[SESSION_COOKIE]["secure"] = True
        self.send_header("Set-Cookie", cookie.output(header="").strip())
        body_out = json.dumps({
            "ok": True,
            "user": {
                "id": user_row["id"],
                "email": user_row["email"],
                "displayName": user_row["display_name"],
                "isAdmin": user_row["is_admin"],
                "mustChangePassword": user_row["must_change_password"],
                "role": user_row.get("role", "member"),
            },
        }).encode("utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_2fa_resend(self) -> None:
        """Resend 2FA code for an existing challenge."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        challenge = str(payload.get("challenge") or "")
        if not challenge:
            _json_response(self, 400, {"error": "challenge is required"})
            return
        result = auth_mod.resend_2fa_challenge(challenge)
        if not result:
            _json_response(self, 401, {"error": "Invalid or expired challenge"})
            return
        new_token, code = result
        # Look up user to send code
        row = db.query_one(
            "SELECT u.id, u.email, u.twofa_method FROM twofa_challenges tc JOIN users u ON tc.user_id = u.id WHERE tc.token = %s",
            (new_token,),
        )
        if row:
            method = row["twofa_method"] or "email"
            if method == "email":
                if not _send_system_email(
                    row["email"],
                    "Your Sietch CRM Login Code",
                    f"<p>Your 2FA verification code is:</p><h2>{code}</h2><p>This code expires in 5 minutes.</p>",
                ):
                    _json_response(self, 500, {"error": "No CRM mail account configured. Add one in Admin → CRM Mail."})
                    return
            elif method == "telegram":
                from notification_dispatcher import _get_employee_chat_id, _send_telegram_message
                chat_id = _get_employee_chat_id(row["id"])
                if chat_id:
                    _send_telegram_message(chat_id, f"Your Sietch CRM login code: <b>{code}</b>\nExpires in 5 minutes.")
        _json_response(self, 200, {"ok": True, "challenge": new_token})

    def _handle_2fa_setup(self) -> None:
        """Initiate 2FA setup (send verification code)."""
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        method = str(payload.get("method") or "").strip()
        if method not in ("email", "telegram"):
            _json_response(self, 400, {"error": "method must be 'email' or 'telegram'"})
            return
        # Validate Telegram has linked chat
        if method == "telegram":
            from notification_dispatcher import _get_employee_chat_id
            chat_id = _get_employee_chat_id(user["id"])
            if not chat_id:
                _json_response(self, 400, {"error": "Link your Telegram account via the bot first"})
                return
        challenge_token, code = auth_mod.create_2fa_challenge(user["id"], method)
        # Send code
        if method == "email":
            if not _send_system_email(
                user["email"],
                "Your Sietch CRM 2FA Setup Code",
                f"<p>Your 2FA verification code is:</p><h2>{code}</h2><p>This code expires in 5 minutes.</p>",
            ):
                _json_response(self, 500, {"error": "No CRM mail account configured. Add one in Admin → CRM Mail."})
                return
        elif method == "telegram":
            from notification_dispatcher import _get_employee_chat_id, _send_telegram_message
            chat_id = _get_employee_chat_id(user["id"])
            if chat_id:
                _send_telegram_message(chat_id, f"Your Sietch CRM 2FA setup code: <b>{code}</b>\nExpires in 5 minutes.")
        _json_response(self, 200, {"ok": True, "challenge": challenge_token, "method": method})

    def _handle_2fa_verify_setup(self) -> None:
        """Verify 2FA setup code and enable 2FA."""
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        challenge = str(payload.get("challenge") or "")
        code = str(payload.get("code") or "")
        if not challenge or not code:
            _json_response(self, 400, {"error": "challenge and code are required"})
            return
        # Verify challenge belongs to this user
        row = db.query_one(
            "SELECT user_id, method FROM twofa_challenges WHERE token = %s AND code = %s AND expires_at > NOW()",
            (challenge, code),
        )
        if not row or row["user_id"] != user["id"]:
            _json_response(self, 401, {"error": "Invalid or expired code"})
            return
        # Enable 2FA
        auth_mod.set_2fa_method(user["id"], row["method"])
        auth_mod.verify_2fa_challenge(challenge, code)  # consume the challenge
        _json_response(self, 200, {"ok": True, "method": row["method"]})

    def _handle_2fa_disable(self) -> None:
        """Disable 2FA (requires password confirmation)."""
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        password = str(payload.get("password") or "")
        if not password:
            _json_response(self, 400, {"error": "Password is required to disable 2FA"})
            return
        # Verify password
        row = db.query_one(
            "SELECT password_hash, password_salt FROM users WHERE id = %s",
            (user["id"],),
        )
        if not row or not auth_mod.verify_password(password, row["password_hash"], row["password_salt"]):
            _json_response(self, 401, {"error": "Invalid password"})
            return
        auth_mod.set_2fa_method(user["id"], None)
        _json_response(self, 200, {"ok": True})

    def _handle_logout(self) -> None:
        token = _session_token(self)
        if token:
            auth_mod.destroy_session(token)
        self.send_response(200)
        cookie = cookies.SimpleCookie()
        cookie[SESSION_COOKIE] = ""
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["max-age"] = "0"
        self.send_header("Set-Cookie", cookie.output(header="").strip())
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _handle_password_reset_request(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            _json_response(self, 400, {"error": "Email is required"})
            return
        user = auth_mod.get_user_by_email(email)
        if user:
            token = auth_mod.create_reset_token(user["id"])
            from smtp_client import send_password_reset_email
            reset_url = f"{CRM_PUBLIC_URL}/reset?token={token}"
            smtp_acct = _get_system_smtp_account()
            send_password_reset_email(email, reset_url, smtp_config=smtp_acct)
        # Always return success to prevent email enumeration
        _json_response(self, 200, {"ok": True})

    def _handle_password_reset(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        token = str(payload.get("token") or "").strip()
        new_password = str(payload.get("password") or "")
        if not token or not new_password:
            _json_response(self, 400, {"error": "Token and password are required"})
            return
        user_id = auth_mod.verify_reset_token(token)
        if not user_id:
            _json_response(self, 400, {"error": "Invalid or expired reset token"})
            return
        auth_mod.set_password(user_id, new_password)
        auth_mod.consume_reset_token(token)
        _json_response(self, 200, {"ok": True})

    def _handle_reset_request_telegram(self) -> None:
        """Send 6-digit reset code via Telegram for self-service password reset."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            _json_response(self, 400, {"error": "Email is required"})
            return
        user = auth_mod.get_user_by_email(email)
        if not user:
            # Always return success to prevent email enumeration
            _json_response(self, 200, {"ok": True})
            return
        # Check if user has linked Telegram
        from notification_dispatcher import _get_employee_chat_id, _send_telegram_message
        chat_id = _get_employee_chat_id(user["id"])
        if not chat_id:
            _json_response(self, 200, {"ok": True, "telegram": False})
            return
        # Generate 6-digit code and create challenge
        challenge_token, code = auth_mod.create_2fa_challenge(user["id"], "telegram")
        # Send via Telegram
        _send_telegram_message(chat_id, f"Your Sietch CRM password reset code: <b>{code}</b>\nExpires in 5 minutes.")
        _json_response(self, 200, {"ok": True, "telegram": True, "challenge": challenge_token})

    def _handle_reset_verify_telegram(self) -> None:
        """Verify Telegram reset code and return token for setting new password."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        challenge = str(payload.get("challenge") or "")
        code = str(payload.get("code") or "")
        if not challenge or not code:
            _json_response(self, 400, {"error": "challenge and code are required"})
            return
        user_id = auth_mod.verify_2fa_challenge(challenge, code)
        if not user_id:
            _json_response(self, 401, {"error": "Invalid or expired code"})
            return
        # Create a password reset token for the user
        reset_token = auth_mod.create_reset_token(user_id)
        _json_response(self, 200, {"ok": True, "token": reset_token})

    def _handle_change_password(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        current = str(payload.get("currentPassword") or "")
        new_password = str(payload.get("newPassword") or "")
        if not current or not new_password:
            _json_response(self, 400, {"error": "Current and new password are required"})
            return
        row = db.query(
            "SELECT password_hash, password_salt FROM users WHERE id = %s",
            (user["id"],), fetch="one",
        )
        if not row or not row[0]:
            _json_response(self, 400, {"error": "No password set"})
            return
        if not auth_mod.verify_password(current, row[0], row[1]):
            _json_response(self, 401, {"error": "Current password is incorrect"})
            return
        auth_mod.set_password(user["id"], new_password)
        _json_response(self, 200, {"ok": True})

    # ── API GET Router ──────────────────────────────────────────────────────

    def _handle_api_get(self) -> None:
        api_path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        # ── SSE Event Stream ──
        if api_path == "/api/events":
            self._handle_sse_stream()
            return

        # ── Config / Info ──
        if api_path == "/api/config":
            _json_response(self, 200, {"version": APP_VERSION})
            return
        m = re.match(r"^/api/branding/(logo|watermark|favicon)$", api_path)
        if m:
            self._handle_branding_image_get(m.group(1))
            return
        if api_path == "/api/branding":
            try:
                row = db.query_one(
                    """SELECT company_name, logo_path, watermark_path, login_title,
                              header_eyebrow, header_title, primary_color, favicon_path,
                              email_header_color, email_footer_text, email_from_name
                       FROM branding ORDER BY id LIMIT 1"""
                )
                if row:
                    _json_response(self, 200, {
                        "companyName": row["company_name"] or "Sietch CRM",
                        "logoPath": row["logo_path"] or "/assets/sietch-logo-2-nobg2.png",
                        "watermarkPath": row["watermark_path"],
                        "loginTitle": row["login_title"] or "Sietch CRM",
                        # Return raw NULLs for eyebrow/title so the client can
                        # fall back to companyName (a stored stale "Sietch CRM"
                        # used to shadow it forever).
                        "headerEyebrow": row["header_eyebrow"] or None,
                        "headerTitle": row["header_title"] or "Workspace <em>dashboard</em>",
                        "primaryColor": row["primary_color"] or "#3b82f6",
                        "faviconPath": row["favicon_path"] or "/favicon.ico",
                        "emailHeaderColor": row["email_header_color"],
                        "emailFooterText": row["email_footer_text"],
                        "emailFromName": row["email_from_name"],
                    })
                else:
                    _json_response(self, 200, {
                        "companyName": "Sietch CRM",
                        "logoPath": "/assets/sietch-logo-2-nobg2.png",
                        "watermarkPath": None,
                        "loginTitle": "Sietch CRM",
                        "headerEyebrow": "Sietch CRM",
                        "headerTitle": "Workspace <em>dashboard</em>",
                        "primaryColor": "#3b82f6",
                        "faviconPath": "/favicon.ico",
                    })
            except Exception:
                logger.exception("Failed to load branding")
                _json_response(self, 200, {
                    "companyName": "Sietch CRM",
                    "logoPath": "/assets/sietch-logo-2-nobg2.png",
                    "watermarkPath": None,
                    "loginTitle": "Sietch CRM",
                    "headerEyebrow": "Sietch CRM",
                    "headerTitle": "Workspace <em>dashboard</em>",
                    "primaryColor": "#3b82f6",
                    "faviconPath": "/favicon.ico",
                })
            return
        if api_path == "/api/changelog":
            cl = Path(__file__).parent / "CHANGELOG.md"
            body = cl.read_text("utf-8") if cl.exists() else "# Changelog\n\nNo changelog available."
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        if api_path == "/api/session":
            user = None
            token = _session_token(self)
            if token:
                user = auth_mod.get_session_user(token)
            _json_response(self, 200, {"authenticated": user is not None, "user": user})
            return
        if api_path == "/api/health":
            try:
                row = db.query("SELECT 1", fetch="one")
                _json_response(self, 200, {"dbReachable": bool(row)})
            except Exception:
                _json_response(self, 200, {"dbReachable": False})
            return

        # ── Infrastructure (admin-only) ──
        if api_path == "/api/v2/admin/infra-log":
            user = _require_admin(self)
            if not user:
                return
            _json_response(self, 200, {"events": infra_events(), "uptime": infra_uptime()})
            return
        if api_path == "/api/v2/admin/docker-health":
            user = _require_admin(self)
            if not user:
                return
            self._handle_docker_health()
            return
        if api_path == "/api/v2/admin/infrastructure":
            user = _require_admin(self)
            if not user:
                return
            self._handle_infrastructure()
            return
        if api_path == "/api/v2/admin/sync/status":
            user = _require_admin(self)
            if not user:
                return
            _json_response(self, 200, {"running": _sync_running, "log": list(_sync_log)})
            return

        # ── Backup status/config/history (GET) ──
        if api_path == "/api/v2/admin/backup/status":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_status()
            return
        if api_path == "/api/v2/admin/backup/config":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_config_get()
            return
        if api_path == "/api/v2/admin/alternate-smtp":
            user = _require_admin(self)
            if not user:
                return
            self._handle_alternate_smtp_get()
            return
        if api_path == "/api/v2/admin/daily-quote":
            user = _require_admin(self)
            if not user:
                return
            self._handle_daily_quote_get()
            return
        if api_path == "/api/v2/daily-quote/live":
            self._handle_daily_quote_live()
            return
        if api_path == "/api/v2/admin/backup/history":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_history()
            return
        if api_path == "/api/v2/admin/backup/history-dir-size":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_history_dir_size()
            return
        m = re.match(r"^/api/v2/admin/backup/history/(backup_history_\d+)$", api_path)
        if m:
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_history_one(m.group(1))
            return

        # ── Presence / Team (local store, unchanged) ──
        if api_path.startswith("/api/presence"):
            if api_path == "/api/presence/users":
                self._handle_presence_users()
                return
            if api_path == "/api/presence":
                self._handle_presence_get()
                return
            if api_path == "/api/presence/dm":
                self._handle_presence_dm_get()
                return
            self.send_error(404)
            return

        # ── Event log (local store, unchanged) ──
        if api_path == "/api/event-log":
            self._handle_event_log_get()
            return
        if api_path == "/api/event-log/users":
            self._handle_event_log_users()
            return
        if api_path == "/api/event-log/all":
            self._handle_event_log_admin_get()
            return

        # ── User profile (local store, unchanged) ──
        if api_path == "/api/user-profile":
            self._handle_user_profile_get()
            return
        if api_path == "/api/dashboard-notes":
            self._handle_dashboard_notes_get()
            return

        # ── Persistent minimized modal state (cross-device) ──
        if api_path == "/api/v2/minimized-state":
            self._handle_minimized_state_get()
            return

        # ── Calendar feed (local handler, unchanged) ──
        if api_path == "/api/calendar/feed":
            self._handle_calendar_feed()
            return

        # ── Calendar v2 API (DB-backed) ──
        if api_path == "/api/v2/calendar/sources":
            self._handle_calendar_sources_get()
            return
        if api_path == "/api/v2/calendar/events":
            self._handle_calendar_events_get(qs)
            return
        if api_path == "/api/v2/calendar/settings":
            self._handle_calendar_settings_get()
            return
        m = re.match(r"^/api/v2/calendar/sources/(\d+)/sync$", api_path)
        if m:
            self._handle_calendar_source_sync(int(m.group(1)))
            return
        if api_path == "/api/v2/calendar/sync-all":
            self._handle_calendar_sync_all()
            return

        # ── Admin check ──
        if api_path == "/api/check-admin":
            self._handle_check_admin()
            return

        # ── Bot customers (admin, unchanged) ──
        if api_path == "/api/bot-customers":
            self._handle_bot_customers_list()
            return
        if api_path == "/api/v2/admin/bot-settings":
            self._handle_admin_bot_settings_get()
            return
        # The bot authenticates with `Authorization: Bearer <token>` but the
        # admin settings endpoint above requires an admin SESSION, so the bot
        # could never read its own config — every call returned 401 and
        # botUsername/botLink auto-population silently never worked (the admin
        # console showed empty fields). This endpoint speaks the bot's auth
        # and deliberately omits the token.
        if api_path == "/api/bot/settings":
            bt = _get_bot_token()
            if not bt or self.headers.get("Authorization", "").strip() != f"Bearer {bt}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            cfg = {k: v for k, v in _get_bot_config().items() if k != "token"}
            cfg["configVersion"] = cfg.get("configVersion", 0)
            _json_response(self, 200, cfg)
            return
        if api_path == "/api/v2/admin/bot-status":
            self._handle_admin_bot_status_get()
            return
        if api_path == "/api/v2/admin/bot-activity":
            self._handle_admin_bot_activity_get(qs)
            return
        if api_path.startswith("/api/bot/"):
            self._handle_bot_api_get()
            return

        # ── Automation (admin) ──
        if api_path == "/api/v2/admin/automation/rules":
            self._handle_automation_rules_get()
            return
        if api_path == "/api/v2/admin/automation/logs":
            self._handle_automation_logs_get(qs)
            return
        if api_path == "/api/v2/admin/photo-report-templates":
            self._handle_photo_report_templates_get()
            return
        # /api/v2/admin/logos/<rest...> — proxy endpoint that streams files
        # from data/logos/ (or data/sigs/) with a strict content-type
        # allowlist. Replaces the admin previews' broken <img src="/data/...
        # references: the static-file server's root is public/, not the
        # project root, so /data/logos/* would 404 otherwise. The path is
        # normalised to data/logos/ or data/sigs/ - any traversal attempt
        # (../, absolute paths, symlinks pointing elsewhere) gets 400.
        m = re.match(r"^/api/v2/admin/logos/(.+)$", api_path)
        if m:
            self._handle_logo_serve(m.group(1))
            return
        if api_path == "/api/v2/admin/document-templates":
            self._handle_document_templates_get()
            return
        if api_path == "/api/v2/admin/service-feeds":
            self._handle_service_feeds_get()
            return
        if api_path == "/api/v2/admin/timezone":
            self._handle_server_timezone_get()
            return

        # ── v2 API: Projects ──
        if api_path == "/api/v2/projects/count":
            self._handle_projects_count(qs)
            return
        if api_path == "/api/v2/projects":
            self._handle_projects_list(qs)
            return
        m = re.match(r"^/api/v2/projects/(\d+)$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_get(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/tags$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_tags_get(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/custom-fields$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_custom_fields_get(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_history_get(opp_id, qs)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photos$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_photos_get(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photo-folders$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_photo_folders_get(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history/(\d+)/replies$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_history_replies_get(opp_id, int(m.group(2)))
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/documents$", api_path)
        if m:
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_documents_get(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return

        # History attachment download (serves the file from data/history-attachments/)
        m = re.match(r"^/api/v2/history-attachments/(\d+)/download$", api_path)
        if m:
            self._handle_history_attachment_download(int(m.group(1)))
            return

        # ── v2 API: Other resources ──
        if api_path == "/api/v2/stages":
            self._handle_stages_get()
            return
        if api_path == "/api/v2/tags":
            self._handle_tags_get()
            return
        if api_path == "/api/v2/custom-fields":
            self._handle_custom_fields_get()
            return
        if api_path == "/api/v2/field-groups":
            self._handle_field_groups_get()
            return
        if api_path == "/api/v2/task-labels":
            self._handle_task_labels_get()
            return
        if api_path == "/api/v2/admin/automation/settings":
            self._handle_automation_settings_get()
            return
        if api_path == "/api/v2/contacts":
            self._handle_contacts_get(qs)
            return
        if api_path == "/api/v2/contacts/count":
            self._handle_contacts_count(qs)
            return
        if api_path == "/api/v2/tasks":
            self._handle_tasks_get(qs)
            return
        if api_path == "/api/v2/notes":
            self._handle_notes_get(qs)
            return
        m = re.match(r"^/api/v2/notes/([^/]+)/versions$", api_path)
        if m:
            self._handle_note_versions(m.group(1))
            return
        if api_path == "/api/v2/users":
            self._handle_users_get()
            return
        if api_path == "/api/v2/me":
            self._handle_me_get()
            return
        if api_path == "/api/v2/my/avatar":
            self._handle_avatar_get()
            return
        if api_path == "/api/v2/my/avatar-style":
            self._handle_my_avatar_style_get()
            return
        if api_path == "/api/v2/users/avatar-styles":
            self._handle_users_avatar_style_get()
            return
        # Serve a downloaded OnlyOffice contact avatar by basename
        m = re.match(r"^/api/v2/contact-avatars/([^/]+)$", api_path)
        if m:
            self._handle_contact_avatar_serve(m.group(1))
            return
        m = re.match(r"^/api/v2/users/(\d+)/avatar$", api_path)
        if m:
            self._handle_user_avatar_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)$", api_path)
        if m:
            self._handle_user_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)/sessions$", api_path)
        if m:
            self._handle_user_sessions_get(int(m.group(1)))
            return
        if api_path == "/api/v2/me/notification-prefs":
            self._handle_notification_prefs_get()
            return
        if api_path == "/api/v2/notifications":
            self._handle_notifications_get(qs)
            return
        if api_path == "/api/v2/notifications/unread-count":
            self._handle_notifications_unread_count()
            return
        m = re.match(r"^/api/v2/contacts/(\d+)/history$", api_path)
        if m:
            self._handle_contact_history_get(int(m.group(1)), qs)
            return
        if api_path == "/api/v2/contacts/export":
            self._handle_contacts_export()
            return
        m = re.match(r"^/api/v2/contacts/(\d+)/photo$", api_path)
        if m:
            self._handle_contact_photo_get(int(m.group(1)), qs)
            return
        if api_path == "/api/v2/history-categories":
            self._handle_history_categories_get()
            return
        if api_path == "/api/v2/documents/personal":
            self._handle_documents_personal()
            return
        if api_path == "/api/v2/documents/company":
            self._handle_documents_company()
            return
        if api_path == "/api/v2/documents/search":
            self._handle_documents_search(qs)
            return
        if api_path == "/api/v2/projects/simple":
            self._handle_projects_simple()
            return
        if api_path == "/api/v2/documents/folders":
            self._handle_document_folders_list(qs)
            return
        if api_path == "/api/v2/documents/folders/tree":
            self._handle_document_folders_tree(qs)
            return
        m = re.match(r"^/api/v2/documents/(\d+)$", api_path)
        if m:
            self._handle_document_download(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/photos/(\d+)$", api_path)
        if m:
            self._handle_photo_download(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/documents/(\d+)/editor-config$", api_path)
        if m:
            self._handle_document_editor_config(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/documents/(\d+)/activity$", api_path)
        if m:
            self._handle_document_activity(int(m.group(1)), qs)
            return

        # ── Batch tags ──
        if api_path == "/api/batch-opportunity-tags":
            self._handle_batch_opportunity_tags()
            return

        # ── Mail OAuth ──
        if api_path == "/api/v2/mail/oauth/authorize":
            self._handle_oauth_authorize()
            return
        if api_path == "/api/v2/mail/oauth/callback":
            self._handle_oauth_callback()
            return
        m = re.match(r"^/api/v2/mail/oauth/refresh/(\d+)$", api_path)
        if m:
            self._handle_oauth_refresh(int(m.group(1)))
            return
        # Graph sendMail OAuth (separate audience from Exchange OAuth)
        if api_path == "/api/v2/mail/oauth/microsoft/graph/authorize":
            qs = parse_qs(urlparse(self.path).query)
            try:
                acct_id = int((qs.get("account_id") or [""])[0])
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "account_id required"})
                return
            self._handle_oauth_graph_authorize(acct_id)
            return
        if api_path == "/api/v2/mail/oauth/microsoft/graph/callback":
            self._handle_oauth_graph_callback()
            return
        m = re.match(r"^/api/v2/mail/oauth/microsoft/graph/disconnect/(\d+)$", api_path)
        if m:
            self._handle_oauth_graph_disconnect(int(m.group(1)))
            return

        # ── Mail Scanner ──
        if api_path == "/api/v2/mail/inbox":
            self._handle_mail_inbox()
            return
        if api_path == "/api/v2/mail/messages":
            self._handle_mail_messages()
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)$", api_path)
        if m:
            self._handle_mail_message(int(m.group(1)))
            return
        if api_path == "/api/v2/mail/tags":
            self._handle_mail_tags()
            return
        m = re.match(r"^/api/v2/mail/tags/(\d+)$", api_path)
        if m:
            self._handle_mail_tag_get(int(m.group(1)))
            return
        if api_path == "/api/v2/mail/templates":
            self._handle_mail_templates()
            return
        m = re.match(r"^/api/v2/mail/templates/(\d+)$", api_path)
        if m:
            self._handle_mail_template_get(int(m.group(1)))
            return
        if api_path == "/api/v2/mail/accounts":
            self._handle_mail_accounts()
            return
        m = re.match(r"^/api/v2/mail/accounts/(\d+)/share$", api_path)
        if m:
            self._handle_mail_account_shares(int(m.group(1)))
            return
        if api_path == "/api/v2/mail/unread-count":
            self._handle_mail_unread_count()
            return
        if api_path == "/api/v2/mail/outgoing":
            self._handle_mail_outgoing()
            return
        if api_path == "/api/v2/mail/status":
            self._handle_mail_status()
            return
        if api_path == "/api/v2/mail/log":
            self._handle_mail_log()
            return
        if api_path == "/api/v2/mail/config":
            self._handle_mail_config()
            return
        # Star/Archive/Move/Reply/Forward
        m = re.match(r"^/api/v2/mail/messages/(\d+)/star$", api_path)
        if m:
            self._handle_mail_message_star(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/reply$", api_path)
        if m:
            self._handle_mail_message_reply(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/forward$", api_path)
        if m:
            self._handle_mail_message_forward(int(m.group(1)))
            return
        # Contacts
        if api_path == "/api/v2/mail/contacts":
            self._handle_mail_contacts()
            return
        if api_path == "/api/v2/mail/contacts/search":
            self._handle_mail_contacts_search()
            return
        if api_path == "/api/v2/mail/contacts/export":
            self._handle_mail_contacts_export()
            return
        m = re.match(r"^/api/v2/mail/contacts/(\d+)$", api_path)
        if m:
            self._handle_mail_contact_get(int(m.group(1)))
            return
        # Signature
        m = re.match(r"^/api/v2/mail/accounts/(\d+)/signature$", api_path)
        if m:
            self._handle_mail_account_signature_get(int(m.group(1)))
            return
        # Attachments
        m = re.match(r"^/api/v2/mail/messages/(\d+)/attachments$", api_path)
        if m:
            self._handle_mail_message_attachments(int(m.group(1)))
            return
        # Attachment download
        m = re.match(r"^/api/v2/mail/messages/(\d+)/attachments/(\d+)/download$", api_path)
        if m:
            self._handle_mail_attachment_download(int(m.group(1)), int(m.group(2)))
            return
        # Attachment editor config (OnlyOffice)
        m = re.match(r"^/api/v2/mail/messages/(\d+)/attachments/(\d+)/editor-config$", api_path)
        if m:
            self._handle_mail_attachment_editor_config(int(m.group(1)), int(m.group(2)))
            return
        # Mail attachment serve (JWT-authenticated, for Document Server)
        if api_path == "/api/v2/mail/attachments/serve":
            self._handle_mail_attachment_serve()
            return
        # Headers
        m = re.match(r"^/api/v2/mail/messages/(\d+)/headers$", api_path)
        if m:
            self._handle_mail_message_headers(int(m.group(1)))
            return
        # Threads
        if api_path == "/api/v2/mail/threads":
            self._handle_mail_threads()
            return
        if api_path == "/api/v2/mail/trash-count":
            self._handle_mail_trash_count()
            return
        if api_path == "/api/v2/mail/drafts":
            self._handle_mail_drafts_get()
            return
        m = re.match(r"^/api/v2/mail/drafts/(\d+)$", api_path)
        if m:
            self._handle_mail_draft_get(int(m.group(1)))
            return
        if api_path == "/api/v2/mail/folders":
            self._handle_mail_folders()
            return
        # Contractors
        if api_path == "/api/v2/mail/contractors":
            self._handle_mail_contractors()
            return
        m = re.match(r"^/api/v2/mail/contractors/(\d+)$", api_path)
        if m:
            self._handle_mail_contractor_get(int(m.group(1)))
            return
        # Classification rules
        if api_path == "/api/v2/mail/classification-rules":
            self._handle_mail_classification_rules()
            return
        m = re.match(r"^/api/v2/mail/classification-rules/(\d+)$", api_path)
        if m:
            self._handle_mail_classification_rule_get(int(m.group(1)))
            return
        # Feedback
        if api_path == "/api/v2/mail/feedback":
            self._handle_mail_feedback()
            return

        # ── Timesheet ──
        if api_path == "/api/timesheet/active":
            self._handle_timesheet_active()
            return
        if api_path == "/api/timesheet/history":
            self._handle_timesheet_history(qs)
            return
        if api_path == "/api/timesheet/daily":
            self._handle_timesheet_daily(qs)
            return
        if api_path == "/api/timesheet/admin/summary":
            self._handle_timesheet_admin_summary(qs)
            return
        if api_path == "/api/timesheet/admin/open-shifts":
            self._handle_timesheet_admin_open_shifts()
            return
        if api_path == "/api/timesheet/export":
            self._handle_timesheet_export_csv(qs)
            return
        if api_path == "/api/timesheet/export-pdf":
            self._handle_timesheet_export_pdf(qs)
            return

        self.send_error(404)

    # ── SSE Event Stream ────────────────────────────────────────────────────

    def _handle_sse_stream(self) -> None:
        """Long-lived SSE connection for real-time event push."""
        user = _require_auth(self)
        if not user:
            return

        user_id = str(user["id"])
        q = event_bus.subscribe(user_id)

        # Tag this connection with a unique id so the unsubscribe path can
        # tell whether the queue still belongs to us. Without this, when
        # a user opens a 2nd tab, the old tab's eventually-break calls
        # event_bus.unsubscribe() which pops the NEW tab's queue out of
        # the subscribers map — leaving the new tab reading from a queue
        # no publisher is pushing to. The connection_id in the queue
        # itself lets us verify before unsubscribing.
        connection_id = object()  # unique per request

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            self.wfile.write(b"event: connected\ndata: {}\n\n")
            self.wfile.flush()

            last_sweep_ts = time.time()
            last_heartbeat_ts = time.time()

            while True:
                try:
                    msg = q.get(timeout=2)  # 2s check interval for sweep responsiveness
                    if msg is None:
                        # Sentinel — either our queue was replaced (a new
                        # tab connected for the same user) or the connection
                        # is being torn down. Either way, exit.
                        break
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    pass
                # Sweep for DB-triggered notifications every 5s
                now = time.time()
                if now - last_sweep_ts >= 5:
                    _sse_publish_recent_notifications(user_id, last_sweep_ts)
                    last_sweep_ts = now
                # Heartbeat every 25s
                if now - last_heartbeat_ts >= 25:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    last_heartbeat_ts = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected — nothing actionable. Logged at DEBUG
            # to avoid log spam in production (every tab close = one line).
            pass
        finally:
            # Only remove our queue if it's still the registered one
            # (a newer tab may have replaced it).
            event_bus.unsubscribe(user_id, expected_q=q, expected_id=connection_id)

    # ── API POST/PUT/DELETE Router ──────────────────────────────────────────

    def _handle_api_post_put(self, method: str) -> None:
        api_path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        # ── Presence (local, unchanged) ──
        if api_path.startswith("/api/presence"):
            if api_path == "/api/presence/heartbeat" and method == "POST":
                self._handle_presence_heartbeat()
                return
            if api_path == "/api/presence/status" and method == "POST":
                self._handle_presence_status()
                return
            if api_path == "/api/presence/last-read" and method == "POST":
                self._handle_presence_last_read()
                return
            if api_path == "/api/presence/dm" and method == "POST":
                self._handle_presence_dm_post()
                return
            if api_path == "/api/presence/dm" and method == "DELETE":
                self._handle_presence_dm_clear()
                return
            self.send_error(404)
            return

        # ── Event log (local) ──
        if api_path == "/api/event-log" and method == "PUT":
            self._handle_event_log_put()
            return

        # ── User profile (local) ──
        if api_path == "/api/user-profile" and method in ("PUT", "POST"):
            self._handle_user_profile_put()
            return

        # ── Persistent minimized modal state (cross-device) ──
        if api_path == "/api/v2/minimized-state" and method == "PUT":
            self._handle_minimized_state_put()
            return
        if api_path == "/api/dashboard-notes" and method == "PUT":
            self._handle_dashboard_notes_put()
            return

        # ── Password reset ──
        if api_path == "/api/v2/auth/reset-request" and method == "POST":
            self._handle_password_reset_request()
            return
        if api_path == "/api/v2/auth/reset" and method == "POST":
            self._handle_password_reset()
            return
        if api_path == "/api/v2/auth/change-password" and method == "POST":
            self._handle_change_password()
            return
        if api_path == "/api/v2/auth/reset-request-telegram" and method == "POST":
            self._handle_reset_request_telegram()
            return
        if api_path == "/api/v2/auth/reset-verify-telegram" and method == "POST":
            self._handle_reset_verify_telegram()
            return

        # ── Profile (own) ──
        if api_path == "/api/v2/me" and method == "PUT":
            self._handle_me_put()
            return
        if api_path == "/api/v2/my/avatar" and method == "POST":
            self._handle_avatar_upload()
            return
        if api_path == "/api/v2/my/avatar" and method == "DELETE":
            self._handle_avatar_delete()
            return
        if api_path == "/api/v2/my/avatar-style" and method in ("PUT", "POST"):
            self._handle_my_avatar_style_put()
            return
        if api_path == "/api/v2/me/notification-prefs" and method == "PUT":
            self._handle_notification_prefs_put()
            return

        # ── Branding (admin) ──
        m = re.match(r"^/api/branding/(logo|watermark|favicon)$", api_path)
        if m and method == "POST":
            self._handle_branding_image_upload(m.group(1))
            return
        if m and method == "DELETE":
            self._handle_branding_image_delete(m.group(1))
            return
        if api_path == "/api/branding" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            try:
                existing = db.query_one("SELECT id FROM branding LIMIT 1")
                # Empty string means "clear back to default" for the nullable
                # text fields; None (key absent) means "leave unchanged".
                # Previously the form sent null for empty, and COALESCE kept
                # the stale value forever — a stored "Sietch CRM" eyebrow could
                # never be cleared, shadowing Company Name.
                def _clearable(key):
                    if key not in payload:
                        return "__keep__"
                    v = payload.get(key)
                    if isinstance(v, str) and not v.strip():
                        return None
                    return v

                _ce = _clearable("headerEyebrow")
                _ct = _clearable("headerTitle")
                _lt = _clearable("loginTitle")
                _cn = _clearable("companyName")
                if existing:
                    db.execute(
                        """UPDATE branding SET
                            company_name = CASE WHEN %s = '__keep__' THEN company_name ELSE %s END,
                            logo_path = COALESCE(%s, logo_path),
                            watermark_path = %s,
                            login_title = CASE WHEN %s = '__keep__' THEN login_title ELSE %s END,
                            header_eyebrow = CASE WHEN %s = '__keep__' THEN header_eyebrow ELSE %s END,
                            header_title = CASE WHEN %s = '__keep__' THEN header_title ELSE %s END,
                            primary_color = COALESCE(%s, primary_color),
                            favicon_path = COALESCE(%s, favicon_path),
                            email_header_color = COALESCE(%s, email_header_color),
                            email_footer_text = COALESCE(%s, email_footer_text),
                            email_from_name = COALESCE(%s, email_from_name),
                            updated_at = NOW()
                        WHERE id = %s""",
                        (_cn, _cn,
                         payload.get("logoPath"),
                         payload.get("watermarkPath"), _lt, _lt, _ce, _ce,
                         _ct, _ct,
                         payload.get("primaryColor"), payload.get("faviconPath"),
                         payload.get("emailHeaderColor"), payload.get("emailFooterText"),
                         payload.get("emailFromName"),
                         existing["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO branding (company_name, logo_path, watermark_path,
                            login_title, header_eyebrow, header_title, primary_color, favicon_path,
                            email_header_color, email_footer_text, email_from_name)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (payload.get("companyName", "Sietch CRM"),
                         payload.get("logoPath", "/assets/sietch-logo-2-nobg2.png"),
                         payload.get("watermarkPath"),
                         payload.get("loginTitle", "Sietch CRM"),
                         payload.get("headerEyebrow", "Sietch CRM"),
                         payload.get("headerTitle", "Workspace <em>dashboard</em>"),
                         payload.get("primaryColor", "#3b82f6"),
                         payload.get("faviconPath", "/favicon.ico"),
                         payload.get("emailHeaderColor"),
                         payload.get("emailFooterText"),
                         payload.get("emailFromName")),
                    )
                _json_response(self, 200, {"ok": True})
            except Exception:
                logger.exception("Failed to update branding")
                _json_response(self, 500, {"error": "Failed to update branding"})
            return

        # ── Infrastructure restart (admin-only) ──
        if api_path == "/api/v2/admin/restart-server" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            log_infra_event("info", f"Server restart requested by {user.get('email', 'unknown')}", source="admin", method="POST", path=api_path)
            _json_response(self, 200, {"ok": True, "message": "Server restarting…"})
            # Push the 200 out to the wire NOW. Without this the body can
            # still be sitting in the socket buffer when we os._exit() a
            # moment later, and the admin's browser sees a truncated
            # response instead of "Restarting…".
            try:
                self.wfile.flush()
            except Exception:
                pass
            import subprocess
            import threading
            def _respawn():
                try:
                    # Spawn a replacement process detached from this one, then
                    # exit. The replacement retries its bind (see main()), so
                    # it doesn't matter whether it wins the race for the
                    # listening socket against this process.
                    proc = subprocess.Popen([sys.executable] + sys.argv, start_new_session=True)
                    log_infra_event("info", f"Replacement server spawned (pid {proc.pid}); old process exiting", source="admin")
                except Exception:
                    logger.exception("Failed to spawn replacement server process")
                    log_infra_event("error", "Failed to spawn replacement server process; original process staying alive", source="admin")
                    return  # stay alive — better a stale process than none
                os._exit(0)
            threading.Timer(1.0, _respawn).start()
            return
        if api_path == "/api/v2/admin/restart-container" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            log_infra_event("info", f"Docker container restart requested by {user.get('email', 'unknown')}", source="admin", method="POST", path=api_path)
            try:
                import subprocess
                # In Docker, socket.gethostname() returns the container id (not the
                # Compose container_name). Use the configured CONTAINER_NAME so the
                # restart command resolves correctly both inside and outside Docker.
                result = subprocess.run(["docker", "restart", CONTAINER_NAME], capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    _json_response(self, 200, {"ok": True, "message": "Container restart triggered"})
                else:
                    err = result.stderr.strip() or result.stdout.strip() or "docker restart returned non-zero"
                    log_infra_event("error", f"Docker restart failed: {err}", source="admin", method="POST", path=api_path)
                    _json_response(self, 500, {"error": f"Docker restart failed: {err}"})
            except Exception as exc:
                logger.exception("Docker restart failed")
                log_infra_event("error", f"Docker restart exception: {exc}", source="admin", method="POST", path=api_path)
                _json_response(self, 500, {"error": str(exc)})
            return
        if api_path == "/api/v2/admin/restart-docserver" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            log_infra_event("info", f"Document Server restart requested by {user.get('email', 'unknown')}", source="admin", method="POST", path=api_path)
            try:
                import subprocess
                # In production the Document Server may live on a separate droplet,
                # in which case this container will not exist here and the restart
                # will return a clear error. Set DOCSERVER_CONTAINER_NAME to match
                # the local container name when co-located (default: onlyoffice-docserver).
                result = subprocess.run(["docker", "restart", DOCSERVER_CONTAINER_NAME], capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    _json_response(self, 200, {"ok": True, "message": "Document Server restart triggered"})
                else:
                    err = result.stderr.strip() or result.stdout.strip() or "docker restart returned non-zero"
                    log_infra_event("error", f"Document Server restart failed: {err}", source="admin", method="POST", path=api_path)
                    _json_response(self, 500, {"error": f"Document Server restart failed: {err}"})
            except Exception as exc:
                logger.exception("Document Server restart failed")
                log_infra_event("error", f"Document Server restart exception: {exc}", source="admin", method="POST", path=api_path)
                _json_response(self, 500, {"error": str(exc)})
            return

        # ── OnlyOffice Sync (admin-only) ──
        if api_path == "/api/v2/admin/sync/test" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_test()
            return
        if api_path == "/api/v2/admin/sync/pull-tags" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_pull_tags()
            return
        if api_path == "/api/v2/admin/sync/pull-tasks" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_pull_tasks()
            return
        if api_path == "/api/v2/admin/sync/status":
            user = _require_admin(self)
            if not user:
                return
            _json_response(self, 200, {"running": _sync_running, "log": list(_sync_log)})
            return
        if api_path == "/api/v2/admin/sync/pull-deal-tags" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_pull_deal_tags()
            return
        if api_path == "/api/v2/admin/sync/pull-deals" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_pull_deals()
            return
        if api_path == "/api/v2/admin/sync/full-reconcile" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_full_reconcile()
            return
        if api_path == "/api/v2/admin/sync/pull-email-bodies" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_pull_email_bodies()
            return
        if api_path == "/api/v2/admin/sync/pull-contacts" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_sync_pull_contacts()
            return

        # ── Export / Import / Backup (POST) ──
        if api_path == "/api/v2/admin/export/universal" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_export_universal()
            return
        if api_path == "/api/v2/admin/export/backup" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_export_backup()
            return
        if api_path == "/api/v2/admin/import/json" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_import_json()
            return
        if api_path == "/api/v2/admin/import/sql" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_import_sql()
            return
        if api_path == "/api/v2/admin/backup/run" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_run()
            return
        if api_path == "/api/v2/admin/backup/export-now" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_export_now()
            return
        if api_path == "/api/v2/admin/backup/config" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_config_post()
            return
        if api_path == "/api/v2/admin/alternate-smtp" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_alternate_smtp_post()
            return
        if api_path == "/api/v2/admin/daily-quote" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_daily_quote_post()
            return
        if api_path == "/api/v2/admin/alternate-smtp/test" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_alternate_smtp_test()
            return
        if api_path == "/api/v2/admin/backup/notify-prefs" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            self._handle_backup_notify_prefs_post()
            return

        # ── Bot customers (admin) ──
        if api_path.startswith("/api/bot-customers"):
            self._handle_bot_customers_post_put(method)
            return
        if api_path == "/api/v2/admin/bot-settings" and method == "PUT":
            self._handle_admin_bot_settings_put()
            return
        if api_path == "/api/v2/admin/bot-restart" and method == "POST":
            self._handle_admin_bot_restart()
            return
        if api_path == "/api/v2/admin/bot/send-document" and method == "POST":
            self._handle_bot_send_document()
            return
        if api_path.startswith("/api/bot/"):
            self._handle_bot_api_post(method)
            return

        # ── Automation (admin) ──
        if api_path == "/api/v2/admin/automation/rules" and method == "POST":
            self._handle_automation_rule_create()
            return
        m = re.match(r"^/api/v2/admin/automation/rules/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_automation_rule_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_automation_rule_delete(int(m.group(1)))
            return
        m2 = re.match(r"^/api/v2/admin/automation/rules/(\d+)/test$", api_path)
        if m2 and method == "POST":
            self._handle_automation_rule_test(int(m2.group(1)))
            return
        m = re.match(r"^/api/v2/admin/photo-report-templates/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_photo_report_template_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_photo_report_template_delete(int(m.group(1)))
            return

        # ── v2 API: Projects ──
        if api_path == "/api/v2/projects" and method == "POST":
            self._handle_project_create()
            return
        m = re.match(r"^/api/v2/projects/(\d+)$", api_path)
        if m and method in ("PUT", "PATCH"):
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_update(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)$", api_path)
        if m and method == "DELETE":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_delete(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/tags$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_tag_add(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/tags/(\d+)$", api_path)
        if m and method == "DELETE":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_tag_remove(opp_id, int(m.group(2)))
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/custom-fields$", api_path)
        if m and method == "PUT":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_custom_fields_update(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_history_create(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photos$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_photo_upload(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photos/markup$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_photo_markup(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photo-folders$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_photo_folder_add(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photo-report$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_photo_report(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/admin/photo-report-templates/(\d+)/logo$", api_path)
        if m and method == "POST":
            self._handle_photo_report_template_logo_upload(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/admin/photo-report-templates$", api_path)
        if m and method == "POST":
            self._handle_photo_report_template_create()
            return
        # ── Document Templates (Invoice + COC) ─────────────────────────────
        m = re.match(r"^/api/v2/admin/document-templates/(\d+)/logo$", api_path)
        if m and method == "POST":
            self._handle_document_template_logo_upload(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/admin/document-templates/(\d+)/signature$", api_path)
        if m and method == "POST":
            self._handle_document_template_signature_upload(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/admin/document-templates/(\d+)$", api_path)
        if m:
            if method == "PUT":
                self._handle_document_template_update(int(m.group(1)))
            elif method == "DELETE":
                self._handle_document_template_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/admin/document-templates$", api_path)
        if m:
            if method == "GET":
                self._handle_document_templates_get()
            elif method == "POST":
                self._handle_document_template_create()
            return
        # ── Service Feeds (admin) — POST/PUT/DELETE only; GET handled in _handle_api_get ──
        m = re.match(r"^/api/v2/admin/service-feeds/(\d+)/refresh$", api_path)
        if m and method == "POST":
            self._handle_service_feed_refresh(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/admin/service-feeds/(\d+)$", api_path)
        if m:
            if method == "PUT":
                self._handle_service_feed_update(int(m.group(1)))
            elif method == "DELETE":
                self._handle_service_feed_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/admin/service-feeds$", api_path)
        if m and method == "POST":
            self._handle_service_feed_create()
            return
        if api_path == "/api/v2/admin/timezone" and method == "POST":
            self._handle_server_timezone_post()
            return
        # ── Invoice / COC generation ───────────────────────────────────────
        m = re.match(r"^/api/v2/projects/(\d+)/invoice$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_invoice_generate(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/coc$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_coc_generate(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        # Standalone (project-less) invoice/COC generation — saves to user's
        # personal Documents scope with opportunity_id=NULL.
        m = re.match(r"^/api/v2/documents/invoice$", api_path)
        if m and method == "POST":
            self._handle_standalone_invoice_generate()
            return
        m = re.match(r"^/api/v2/documents/coc$", api_path)
        if m and method == "POST":
            self._handle_standalone_coc_generate()
            return
        # ── Service-feed webhook receiver (no auth — secret-token-in-URL) ────
        m = re.match(r"^/api/v2/service-feeds/webhook/([A-Za-z0-9_\-]+)$", api_path)
        if m and method == "POST":
            self._handle_service_feed_webhook(m.group(1))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history/(\d+)/replies$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_history_reply_create(opp_id, int(m.group(2)))
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/projects/(\d+)/documents$", api_path)
        if m and method == "POST":
            opp_id = _resolve_project_id(m.group(1))
            if opp_id:
                self._handle_project_document_upload(opp_id)
            else:
                _json_response(self, 404, {"error": "Project not found"})
            return
        m = re.match(r"^/api/v2/documents/(\d+)/callback$", api_path)
        if m and method == "POST":
            self._handle_document_callback(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/documents/(\d+)/copy$", api_path)
        if m and method == "POST":
            self._handle_document_copy(int(m.group(1)))
            return
        if api_path == "/api/v2/documents/batch-delete" and method == "POST":
            self._handle_documents_batch_delete()
            return
        if api_path == "/api/v2/documents/batch-move" and method == "POST":
            self._handle_documents_batch_move()
            return
        if api_path == "/api/v2/documents/batch-copy" and method == "POST":
            self._handle_documents_batch_copy()
            return
        if api_path == "/api/v2/photos/batch-download" and method == "POST":
            self._handle_photos_batch_download()
            return
        if api_path == "/api/v2/photos/batch-move" and method == "POST":
            self._handle_photos_batch_move()
            return
        if api_path == "/api/v2/photos/batch-copy" and method == "POST":
            self._handle_photos_batch_copy()
            return
        if api_path == "/api/v2/photos/batch-delete" and method == "POST":
            self._handle_photos_batch_delete()
            return
        if api_path == "/api/v2/documents/personal/upload" and method == "POST":
            self._handle_document_upload_personal()
            return
        if api_path == "/api/v2/documents/company/upload" and method == "POST":
            self._handle_document_upload_company()
            return
        if api_path == "/api/v2/documents/folders" and method == "POST":
            self._handle_document_folder_create()
            return
        if api_path == "/api/v2/documents/create" and method == "POST":
            self._handle_document_create_blank()
            return
        m = re.match(r"^/api/v2/documents/(\d+)/command$", api_path)
        if m and method == "POST":
            self._handle_document_command(int(m.group(1)))
            return
        if api_path == "/api/v2/documents/save-as" and method == "POST":
            self._handle_document_save_as()
            return
        m = re.match(r"^/api/v2/documents/folders/(\d+)$", api_path)
        if m and method in ("PUT", "PATCH"):
            self._handle_document_folder_rename(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_document_folder_delete(int(m.group(1)))
            return

        # ── v2 API: Resources ──
        if api_path == "/api/v2/stages" and method == "POST":
            self._handle_stage_create()
            return
        if api_path == "/api/v2/stages/reorder" and method == "POST":
            self._handle_stages_reorder()
            return
        m = re.match(r"^/api/v2/stages/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_stage_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_stage_delete(int(m.group(1)))
            return
        if api_path == "/api/v2/tags" and method == "POST":
            self._handle_tag_create()
            return
        m = re.match(r"^/api/v2/tags/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_tag_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_tag_delete(int(m.group(1)))
            return
        if api_path == "/api/v2/custom-fields" and method == "POST":
            self._handle_custom_field_create()
            return
        m = re.match(r"^/api/v2/custom-fields/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_custom_field_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_custom_field_delete(int(m.group(1)))
            return

        # ── Field groups (admin) ──
        if api_path == "/api/v2/field-groups" and method == "POST":
            self._handle_field_group_create()
            return
        if api_path == "/api/v2/field-groups/reorder" and method == "POST":
            self._handle_field_groups_reorder()
            return
        m = re.match(r"^/api/v2/field-groups/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_field_group_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_field_group_delete(int(m.group(1)))
            return

        # ── History categories (admin) ──
        if api_path == "/api/v2/history-categories" and method == "POST":
            self._handle_history_category_create()
            return
        m = re.match(r"^/api/v2/history-categories/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_history_category_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_history_category_delete(int(m.group(1)))
            return

        # ── Task labels (admin) ──
        if api_path == "/api/v2/task-labels" and method == "POST":
            self._handle_task_label_create()
            return
        m = re.match(r"^/api/v2/task-labels/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_task_label_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_task_label_delete(int(m.group(1)))
            return

        # ── Automation settings (admin) ──
        if api_path == "/api/v2/admin/automation/settings" and method in ("POST", "PUT"):
            self._handle_automation_settings_put()
            return

        if api_path == "/api/v2/contacts" and method == "POST":
            self._handle_contact_create()
            return
        if api_path == "/api/v2/contacts/import" and method == "POST":
            self._handle_contacts_import()
            return
        m = re.match(r"^/api/v2/contacts/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_contact_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_contact_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/contacts/(\d+)/history$", api_path)
        if m and method == "POST":
            self._handle_contact_history_create(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/contacts/(\d+)/photo$", api_path)
        if m and method == "POST":
            self._handle_contact_photo_upload(int(m.group(1)))
            return
        if api_path == "/api/v2/tasks" and method == "POST":
            self._handle_task_create()
            return
        if api_path == "/api/v2/tasks/reorder" and method == "POST":
            self._handle_tasks_reorder()
            return
        m = re.match(r"^/api/v2/tasks/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_task_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_task_delete(int(m.group(1)))
            return
        if api_path == "/api/v2/notes" and method == "POST":
            self._handle_note_create()
            return
        if api_path == "/api/v2/notes/reorder" and method == "POST":
            self._handle_notes_reorder()
            return
        m = re.match(r"^/api/v2/notes/([^/]+)$", api_path)
        if m and method == "PUT":
            self._handle_note_update(m.group(1))
            return
        if m and method == "DELETE":
            self._handle_note_delete(m.group(1))
            return
        m = re.match(r"^/api/v2/notes/([^/]+)/links$", api_path)
        if m and method == "POST":
            self._handle_note_link_add(m.group(1))
            return
        m = re.match(r"^/api/v2/notes/([^/]+)/links/([^/]+)/([^/]+)$", api_path)
        if m and method == "DELETE":
            self._handle_note_link_remove(m.group(1), m.group(2), m.group(3))
            return
        m = re.match(r"^/api/v2/notes/([^/]+)/restore$", api_path)
        if m and method == "POST":
            self._handle_note_restore(m.group(1))
            return
        if api_path == "/api/v2/users" and method == "POST":
            self._handle_user_create()
            return
        m = re.match(r"^/api/v2/users/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_user_update(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)/reset-password$", api_path)
        if m and method == "POST":
            self._handle_user_reset_password(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)/reset-password/send$", api_path)
        if m and method == "POST":
            self._handle_user_reset_password_send(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)/reactivate$", api_path)
        if m and method == "POST":
            self._handle_user_reactivate(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)/sessions$", api_path)
        if m and method == "DELETE":
            self._handle_user_sessions_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_user_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/users/(\d+)/delete-personal-data$", api_path)
        if m and method == "POST":
            self._handle_user_delete_personal_data(int(m.group(1)))
            return

        # ── DELETE handlers ──
        m = re.match(r"^/api/v2/history/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_history_event_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/history-replies/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_history_reply_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/photos/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_photo_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/photo-folders/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_photo_folder_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/documents/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_document_delete(int(m.group(1)))
            return
        if m and method in ("PUT", "PATCH"):
            self._handle_document_update(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/notifications/(\d+)/read$", api_path)
        if m and method == "PUT":
            self._handle_notification_mark_read(int(m.group(1)))
            return
        if api_path == "/api/v2/notifications/read-all" and method == "PUT":
            self._handle_notifications_mark_all_read()
            return

        # ── Mail OAuth ──
        if api_path == "/api/v2/mail/oauth/refresh" and method == "POST":
            self._handle_oauth_refresh_manual()
            return

        # ── Mail Scanner ──
        if api_path == "/api/v2/mail/messages" and method == "POST":
            self._handle_mail_message_create()
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/read$", api_path)
        if m and method == "PUT":
            self._handle_mail_message_mark_read(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/unread$", api_path)
        if m and method == "PUT":
            self._handle_mail_message_mark_unread(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_mail_message_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/tags$", api_path)
        if m and method == "POST":
            self._handle_mail_message_add_tag(int(m.group(1)))
            return
        # Tag CRUD
        if api_path == "/api/v2/mail/tags" and method == "POST":
            self._handle_mail_tag_create()
            return
        m = re.match(r"^/api/v2/mail/tags/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_tag_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_tag_delete(int(m.group(1)))
            return
        # Message tag remove
        m = re.match(r"^/api/v2/mail/messages/(\d+)/tags/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_mail_message_remove_tag(int(m.group(1)), int(m.group(2)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/link$", api_path)
        if m and method == "POST":
            self._handle_mail_link(int(m.group(1)))
            return
        if api_path == "/api/v2/mail/reprocess" and method == "POST":
            self._handle_mail_reprocess()
            return
        if api_path == "/api/v2/mail/retrain" and method == "POST":
            self._handle_mail_retrain()
            return
        if api_path == "/api/v2/mail/config" and method == "PUT":
            self._handle_mail_config_put()
            return
        if api_path == "/api/v2/mail/log/correct" and method == "POST":
            self._handle_mail_log_correct()
            return
        if api_path == "/api/v2/mail/trigger-test" and method == "POST":
            self._handle_mail_trigger_test()
            return
        # Account CRUD
        if api_path == "/api/v2/mail/accounts" and method == "POST":
            self._handle_mail_account_create()
            return
        if api_path == "/api/v2/mail/accounts/test" and method == "POST":
            self._handle_mail_account_test()
            return
        m = re.match(r"^/api/v2/mail/accounts/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_account_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_account_delete(int(m.group(1)))
            return
        # Account sharing
        m = re.match(r"^/api/v2/mail/accounts/(\d+)/share$", api_path)
        if m and method == "POST":
            self._handle_mail_account_share(int(m.group(1)))
            return
        if m and method == "GET":
            self._handle_mail_account_shares(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/accounts/(\d+)/share/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_mail_account_unshare(int(m.group(1)), int(m.group(2)))
            return
        # Send email
        if api_path == "/api/v2/mail/send" and method == "POST":
            self._handle_mail_send()
            return
        if api_path == "/api/v2/mail/send/undo" and method == "POST":
            self._handle_mail_send_undo()
            return
        # Drafts
        if api_path == "/api/v2/mail/drafts" and method == "POST":
            self._handle_mail_draft_save()
            return
        m = re.match(r"^/api/v2/mail/drafts/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_draft_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_draft_delete(int(m.group(1)))
            return
        # Template CRUD
        if api_path == "/api/v2/mail/templates" and method == "POST":
            self._handle_mail_template_create()
            return
        m = re.match(r"^/api/v2/mail/templates/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_template_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_template_delete(int(m.group(1)))
            return
        # Star/Archive/Move
        m = re.match(r"^/api/v2/mail/messages/(\d+)/star$", api_path)
        if m and method == "PUT":
            self._handle_mail_message_toggle_star(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/archive$", api_path)
        if m and method == "PUT":
            self._handle_mail_message_archive(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/mail/messages/(\d+)/move$", api_path)
        if m and method == "PUT":
            self._handle_mail_message_move(int(m.group(1)))
            return
        # Contacts
        if api_path == "/api/v2/mail/contacts" and method == "POST":
            self._handle_mail_contact_create()
            return
        if api_path == "/api/v2/mail/contacts/import" and method == "POST":
            self._handle_mail_contacts_import()
            return
        m = re.match(r"^/api/v2/mail/contacts/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_contact_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_contact_delete(int(m.group(1)))
            return
        # Signature
        m = re.match(r"^/api/v2/mail/accounts/(\d+)/signature$", api_path)
        if m and method == "PUT":
            self._handle_mail_account_signature_put(int(m.group(1)))
            return
        # Empty trash
        if api_path == "/api/v2/mail/empty-trash" and method == "POST":
            self._handle_mail_empty_trash()
            return
        # Folders
        if api_path == "/api/v2/mail/folders" and method == "POST":
            self._handle_mail_folder_create()
            return
        m = re.match(r"^/api/v2/mail/folders/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_folder_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_folder_delete(int(m.group(1)))
            return
        # Contractors
        if api_path == "/api/v2/mail/contractors" and method == "POST":
            self._handle_mail_contractor_create()
            return
        m = re.match(r"^/api/v2/mail/contractors/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_contractor_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_contractor_delete(int(m.group(1)))
            return
        # Classification rules
        if api_path == "/api/v2/mail/classification-rules" and method == "POST":
            self._handle_mail_classification_rule_create()
            return
        m = re.match(r"^/api/v2/mail/classification-rules/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_mail_classification_rule_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_mail_classification_rule_delete(int(m.group(1)))
            return
        # Feedback review
        m = re.match(r"^/api/v2/mail/feedback/(\d+)/review$", api_path)
        if m and method == "POST":
            self._handle_mail_feedback_review(int(m.group(1)))
            return

        # ── Timesheet ──
        if api_path == "/api/timesheet/clock-in" and method == "POST":
            self._handle_timesheet_clock_in()
            return
        if api_path == "/api/timesheet/clock-out" and method == "POST":
            self._handle_timesheet_clock_out()
            return
        if api_path == "/api/timesheet/break/start" and method == "POST":
            self._handle_timesheet_break_start()
            return
        if api_path == "/api/timesheet/break/end" and method == "POST":
            self._handle_timesheet_break_end()
            return
        m = re.match(r"^/api/timesheet/entries/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_timesheet_entry_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_timesheet_entry_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/timesheet/admin/close/(\d+)$", api_path)
        if m and method == "POST":
            self._handle_timesheet_admin_close_entry(int(m.group(1)))
            return

        # ── Calendar v2 API (DB-backed) ──
        if api_path == "/api/v2/calendar/sources" and method == "POST":
            self._handle_calendar_source_create()
            return
        m = re.match(r"^/api/v2/calendar/sources/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_calendar_source_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_calendar_source_delete(int(m.group(1)))
            return
        if api_path == "/api/v2/calendar/events" and method == "POST":
            self._handle_calendar_event_create()
            return
        m = re.match(r"^/api/v2/calendar/events/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_calendar_event_update(int(m.group(1)))
            return
        if m and method == "DELETE":
            self._handle_calendar_event_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/calendar/events/(\d+)/snooze$", api_path)
        if m and method == "POST":
            self._handle_calendar_event_snooze(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/calendar/events/(\d+)/dismiss-reminder$", api_path)
        if m and method == "POST":
            self._handle_calendar_event_dismiss_reminder(int(m.group(1)))
            return
        if api_path == "/api/v2/calendar/settings" and method == "PUT":
            self._handle_calendar_settings_put()
            return
        if api_path == "/api/v2/calendar/import" and method == "POST":
            self._handle_calendar_import()
            return
        if api_path == "/api/v2/calendar/search" and method == "GET":
            self._handle_calendar_search(qs)
            return
        if api_path == "/api/v2/calendar/export" and method == "GET":
            self._handle_calendar_export(qs)
            return

        self.send_error(404)

    # ════════════════════════════════════════════════════════════════════════
    # v2 API ENDPOINT HANDLERS
    # ════════════════════════════════════════════════════════════════════════

    # ── Projects ────────────────────────────────────────────────────────────

    def _projects_query_val(self, qs: dict, *keys):
        """Return first non-empty query value for any of the given keys."""
        for k in keys:
            if k in qs and qs[k]:
                return qs[k][0] if isinstance(qs[k], (list, tuple)) else qs[k]
        return None

    def _build_projects_where(self, qs: dict, user: dict | None = None) -> tuple[list[str], list]:
        """Build WHERE clauses and params for project list/count queries."""
        qval = self._projects_query_val
        where = ["1=1"]
        params: list = []

        # Limited role: only own deals
        if user and _is_limited(user):
            where.append("(o.responsible_user_id = %s OR o.created_by = %s)")
            params.extend([user["id"], user["id"]])

        search = qval(qs, "search", "filterValue")
        if search:
            like = f"%{search}%"
            where.append("""(
                o.title ILIKE %s
                OR o.description ILIKE %s
                OR EXISTS (
                    SELECT 1 FROM contacts c
                    WHERE c.id = o.contact_id
                    AND (c.first_name ILIKE %s OR c.last_name ILIKE %s OR c.company ILIKE %s)
                )
                OR EXISTS (
                    SELECT 1 FROM opportunity_custom_field_values cfv
                    WHERE cfv.opportunity_id = o.id AND cfv.field_value ILIKE %s
                )
            )""")
            params.extend([like, like, like, like, like, like])

        st = qval(qs, "stage_type", "stageType")
        if st is not None and str(st).strip() != "":
            where.append("o.stage_type = %s")
            params.append(int(st))

        sid = qval(qs, "stageId", "opportunityStagesid")
        if sid:
            where.append("o.stage_id = %s")
            params.append(int(sid))

        cid = qval(qs, "contact_id", "contactid")
        if cid:
            where.append("o.contact_id = %s")
            params.append(int(cid))

        rid = qval(qs, "responsible_user_id", "responsibleUserId")
        if rid:
            where.append("o.responsible_user_id = %s")
            params.append(int(rid))

        # Filter by a specific set of opportunity ids. Used by the search
        # modal's "Export selected" to fetch a cross-page selection
        # (the current page might only hold a subset of the selected
        # deals). Comma-separated integers; the integer array is bound as
        # a single parameter and matched with = ANY(%s).
        ids_raw = qval(qs, "ids")
        if ids_raw:
            try:
                parsed_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
                if parsed_ids:
                    where.append("o.id = ANY(%s)")
                    params.append(parsed_ids)
            except ValueError:
                pass

        tag_id = qval(qs, "tag_id", "tagId")
        if tag_id:
            where.append("o.id IN (SELECT opportunity_id FROM opportunity_tags WHERE tag_id = %s)")
            params.append(int(tag_id))

        cf_filters_raw = qval(qs, "customFieldFilters")
        if cf_filters_raw:
            try:
                cf_filters = json.loads(cf_filters_raw)
                if isinstance(cf_filters, list):
                    for cf in cf_filters:
                        field_id = cf.get("fieldId") or cf.get("field_id") or cf.get("id")
                        value = cf.get("value")
                        operator = str(cf.get("operator", "equals")).lower()
                        if field_id is None or value is None or str(value).strip() == "":
                            continue
                        field_id = int(field_id)
                        if operator == "contains":
                            where.append("o.id IN (SELECT opportunity_id FROM opportunity_custom_field_values WHERE field_id = %s AND field_value ILIKE %s)")
                            params.extend([field_id, f"%{value}%"])
                        else:
                            where.append("o.id IN (SELECT opportunity_id FROM opportunity_custom_field_values WHERE field_id = %s AND LOWER(field_value) = LOWER(%s))")
                            params.extend([field_id, str(value)])
            except (json.JSONDecodeError, ValueError):
                pass

        return where, params

    def _handle_projects_count(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        where, params = self._build_projects_where(qs, user)
        where_sql = " AND ".join(where)
        row = db.query(
            f"""SELECT COUNT(o.id)
                FROM opportunities o
                WHERE {where_sql}""",
            tuple(params),
            fetch="one",
        )
        _json_response(self, 200, {"count": row[0] if row else 0})

    def _handle_projects_list(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        where, params = self._build_projects_where(qs, user)

        count = int(qs.get("count", ["500"])[0])
        start = int(qs.get("startIndex", ["0"])[0])
        sort_by = qs.get("sort_by", ["date_created"])[0]
        sort_order = qs.get("sort_order", ["descending"])[0]
        order = "DESC" if sort_order == "descending" else "ASC"
        sort_col = {
            "date_created": "o.created_at",
            "title": "o.title",
            "bid_value": "o.bid_value",
            "stage": "s.title",
        }.get(sort_by, "o.created_at")

        where_sql = " AND ".join(where)
        rows = db.query(
            f"""SELECT o.id, o.title, o.description, o.stage_id, o.stage_type, o.bid_value,
                       o.expected_close_date, o.probability, o.contact_id, o.responsible_user_id,
                       o.is_private, o.created_at, o.created_by, o.updated_at,
                       s.title as stage_title, s.color as stage_color,
                       c.first_name, c.last_name, c.company
                FROM opportunities o
                LEFT JOIN stages s ON o.stage_id = s.id
                LEFT JOIN contacts c ON o.contact_id = c.id
                WHERE {where_sql}
                ORDER BY {sort_col} {order}
                LIMIT %s OFFSET %s""",
            (*params, count, start),
        )
        projects = []
        for r in rows:
            projects.append({
                "id": r[0], "title": r[1], "description": r[2],
                "stageId": r[3], "stageType": r[4], "bidValue": float(r[5]) if r[5] else None,
                "expectedCloseDate": str(r[6]) if r[6] else None, "probability": r[7],
                "contactId": r[8], "responsibleUserId": r[9], "isPrivate": r[10],
                "created": r[11].isoformat() if r[11] else None,
                "stage": {"title": r[14], "color": r[15]} if r[14] else None,
                "contact": {"displayName": f"{r[16] or ''} {r[17] or ''}".strip(), "company": r[18]} if r[16] or r[17] else None,
            })
        _json_response(self, 200, projects)

    def _handle_project_get(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        row = db.query(
            """SELECT o.id, o.title, o.description, o.stage_id, o.stage_type, o.bid_value,
                      o.expected_close_date, o.probability, o.contact_id, o.responsible_user_id,
                      o.is_private, o.created_at, o.created_by, o.updated_at,
                      s.title, s.color,
                      u1.display_name, u2.display_name,
                      c.first_name, c.last_name, c.company, c.email, c.phone,
                      o.project_number,
                      c.address_street, c.address_city, c.address_state, c.address_zip
               FROM opportunities o
               LEFT JOIN stages s ON o.stage_id = s.id
               LEFT JOIN users u1 ON o.created_by = u1.id
               LEFT JOIN users u2 ON o.responsible_user_id = u2.id
               LEFT JOIN contacts c ON o.contact_id = c.id
               WHERE o.id = %s""",
            (opp_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Project not found"})
            return
        # Limited role: only access own deals
        if _is_limited(user):
            responsible_id = row[9]
            created_by_id = row[12]
            if responsible_id != user["id"] and created_by_id != user["id"]:
                _json_response(self, 403, {"error": "Forbidden"})
                return
        _json_response(self, 200, {
            "id": row[0], "title": row[1], "description": row[2],
            "stageId": row[3], "stageType": row[4], "bidValue": float(row[5]) if row[5] else None,
            "expectedCloseDate": str(row[6]) if row[6] else None, "probability": row[7],
            "contactId": row[8], "responsibleUserId": row[9], "isPrivate": row[10],
            "created": row[11].isoformat() if row[11] else None,
            "createdBy": row[12], "updatedAt": row[13].isoformat() if row[13] else None,
            "stage": {"title": row[14], "color": row[15]} if row[14] else None,
            "responsible": {"displayName": row[17]} if row[17] else None,
            "contact": {"displayName": (f"{row[18] or ''} {row[19] or ''}".strip() or row[20] or f"Contact {row[8]}"), "company": row[20], "email": row[21], "phone": row[22], "addressStreet": row[24], "addressCity": row[25], "addressState": row[26], "addressZip": row[27]} if row[8] else None,
            "project_number": row[23],
        })

    # ── Automation rules (admin) ──────────────────────────────────────────

    def _handle_automation_rules_get(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        rows = db.query_dicts("SELECT * FROM automation_rules ORDER BY id")
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
            if r.get("updated_at"):
                r["updated_at"] = str(r["updated_at"])
            if r.get("last_run_at"):
                r["last_run_at"] = str(r["last_run_at"])
        _json_response(self, 200, {"rules": rows})

    def _handle_automation_rule_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        name = str(payload.get("name") or "").strip()
        trigger_type = str(payload.get("triggerType") or "").strip()
        action_type = str(payload.get("actionType") or "").strip()
        if not name or not trigger_type or not action_type:
            _json_response(self, 400, {"error": "name, triggerType, and actionType are required"})
            return
        rid = db.insert_returning(
            """INSERT INTO automation_rules (name, enabled, dry_run, trigger_type, trigger_config, action_type, action_config)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                name,
                payload.get("enabled", True),
                payload.get("dryRun", False),
                trigger_type,
                json.dumps(payload.get("triggerConfig") or {}),
                action_type,
                json.dumps(payload.get("actionConfig") or {}),
            ),
        )
        _json_response(self, 201, {"id": rid, "ok": True})

    def _handle_automation_rule_update(self, rule_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        sets = ["updated_at = NOW()"]
        params: list = []
        for field, col in [("name", "name"), ("enabled", "enabled"), ("dryRun", "dry_run"),
                           ("triggerType", "trigger_type"), ("actionType", "action_type")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        for field, col in [("triggerConfig", "trigger_config"), ("actionConfig", "action_config")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(json.dumps(payload[field]))
        params.append(rule_id)
        db.execute(f"UPDATE automation_rules SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    def _handle_automation_rule_delete(self, rule_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        db.execute("DELETE FROM automation_rules WHERE id = %s", (rule_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_automation_rule_test(self, rule_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        rule = db.query_one("SELECT * FROM automation_rules WHERE id = %s", (rule_id,))
        if not rule:
            _json_response(self, 404, {"error": "Rule not found"})
            return
        # Use the targeted single-rule evaluator. The previous
        # implementation called all 6 bulk evaluators and then
        # filtered by rule_id, which fetched up to 50 matching opps
        # per rule across the entire enabled-rule set. For a CRM
        # with many rules this was wasteful; the test endpoint is
        # admin-triggered and one-shot, but the targeted path is
        # still cleaner.
        from automation_engine import (
            _evaluate_single_rule, _build_template_context, _execute_action,
        )
        matches = _evaluate_single_rule(rule)
        previews = []
        for m in matches[:10]:
            opp = m["opp"]
            ctx = _build_template_context(opp, opp.get("stage_title") or "", m.get("days_in_stage", 0), m.get("days_with_tag", 0))
            result = _execute_action(rule, opp, ctx, dry_run=True)
            previews.append({
                "opportunity_id": opp["id"],
                "title": opp.get("title", ""),
                "days_in_stage": m.get("days_in_stage", 0),
                "preview": result,
            })
        _json_response(self, 200, {
            "previews": previews,
            "total_matches": len(matches),
            "dry_run": True,
        })

    def _handle_automation_logs_get(self, qs: dict) -> None:
        user = _require_admin(self)
        if not user:
            return
        rule_id = (qs.get("ruleId") or [None])[0]
        page = max(1, int((qs.get("page") or [1])[0]))
        per_page = min(100, max(1, int((qs.get("perPage") or [50])[0])))
        offset = (page - 1) * per_page
        where = ""
        params: list = []
        if rule_id:
            where = "WHERE l.rule_id = %s"
            params.append(int(rule_id))
        count_row = db.query_one(f"SELECT COUNT(*) as cnt FROM automation_logs l {where}", (*params,) if params else None)
        total = count_row["cnt"] if count_row else 0
        params.extend([per_page, offset])
        rows = db.query_dicts(
            f"""SELECT l.*, r.name as rule_name, o.title as project_title, o.project_number
                FROM automation_logs l
                LEFT JOIN automation_rules r ON r.id = l.rule_id
                LEFT JOIN opportunities o ON o.id = l.opportunity_id
                {where}
                ORDER BY l.created_at DESC LIMIT %s OFFSET %s""",
            (*params,),
        )
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
        _json_response(self, 200, {"logs": rows, "total": total, "page": page, "perPage": per_page})

    def _handle_project_create(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = str(payload.get("title") or "").strip()
        if not title:
            _json_response(self, 400, {"error": "Title is required"})
            return
        stage_id = payload.get("stageId")
        stage_type = payload.get("stageType") or 0
        bid_value = payload.get("bidValue")
        contact_id = payload.get("contactId")
        responsible_user_id = payload.get("responsibleUserId") or user["id"]
        description = str(payload.get("description") or "").strip() or None
        expected_close = str(payload.get("expectedCloseDate") or "").strip() or None
        is_private = bool(payload.get("isPrivate"))

        opp_id = db.insert_returning(
            """INSERT INTO opportunities (title, description, stage_id, stage_type, bid_value,
                   expected_close_date, contact_id, responsible_user_id, created_by, is_private,
                   stage_entered_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING id""",
            (title, description, stage_id, stage_type, bid_value, expected_close, contact_id, responsible_user_id, user["id"], is_private),
        )
        _json_response(self, 201, {"id": opp_id, "ok": True})
        # Publish project_created event for automation engine
        event_bus.publish("project_created", {"opportunity_id": opp_id, "title": title})

    def _handle_project_update(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        try:
            # Fetch current values for change tracking
            old_row = db.query_one(
                """SELECT stage_id, bid_value, contact_id, responsible_user_id,
                          expected_close_date, is_private, description
                   FROM opportunities WHERE id = %s""", (opp_id,)
            )
            TRACKED_FIELDS = {
                "stageId": ("stage_id", "Stage"),
                "bidValue": ("bid_value", "Value"),
                "contactId": ("contact_id", "Contact"),
                "responsibleUserId": ("responsible_user_id", "Responsible"),
                "expectedCloseDate": ("expected_close_date", "Follow-up Due Date"),
                "isPrivate": ("is_private", "Private"),
            }
            OLD_VALUES = {}
            if old_row:
                OLD_VALUES = {
                    "stageId": old_row["stage_id"],
                    "bidValue": float(old_row["bid_value"]) if old_row["bid_value"] else None,
                    "contactId": old_row["contact_id"],
                    "responsibleUserId": old_row["responsible_user_id"],
                    "expectedCloseDate": str(old_row["expected_close_date"]) if old_row["expected_close_date"] else None,
                    "isPrivate": old_row["is_private"],
                }

            sets = ["updated_at = NOW()", "updated_by = %s"]
            params: list = [user["id"]]
            for field, col in [("title", "title"), ("description", "description"), ("stageId", "stage_id"),
                               ("bidValue", "bid_value"), ("contactId", "contact_id"),
                               ("responsibleUserId", "responsible_user_id"), ("isPrivate", "is_private"),
                               ("expectedCloseDate", "expected_close_date"), ("probability", "probability"),
                               ("stageType", "stage_type")]:
                if field in payload:
                    val = payload[field]
                    if field in ("contactId", "responsibleUserId") and (val == 0 or val == "0" or val == ""):
                        val = None
                    sets.append(f"{col} = %s")
                    params.append(val)
            if "stageId" in payload:
                sets.append("stage_entered_at = NOW()")
            params.append(opp_id)
            db.execute(f"UPDATE opportunities SET {', '.join(sets)} WHERE id = %s", (*params,))
            # Publish stage_changed event for automation engine
            if "stageId" in payload:
                event_bus.publish("stage_changed", {"opportunity_id": opp_id, "stage_id": payload["stageId"]})
            # Always publish project_updated so automation rules with
            # trigger_type='project_updated' actually fire. Without this
            # the trigger type was registered in the SSE map but had no
            # upstream publisher — rules only fired via the polling
            # sweep's last_run_at cutoff, which is much weaker than the
            # SSE-driven design intended.
            try:
                event_bus.publish("project_updated", {"opportunity_id": opp_id})
            except Exception:
                pass

            # Create history events for field changes
            logger.info("Change tracking: payload=%s, OLD_VALUES=%s", list(payload.keys()), OLD_VALUES)
            for field, (col, label) in TRACKED_FIELDS.items():
                if field not in payload:
                    continue
                new_val = payload[field]
                old_val = OLD_VALUES.get(field)
                if field in ("contactId", "responsibleUserId") and (new_val == 0 or new_val == "0" or new_val == ""):
                    new_val = None
                if field == "bidValue" and new_val is not None:
                    try: new_val = float(new_val)
                    except (TypeError, ValueError): pass
                if field == "isPrivate":
                    new_val = bool(new_val) if new_val is not None else None
                    old_val = bool(old_val) if old_val is not None else None
                if str(new_val) == str(old_val) or (new_val is None and old_val is None):
                    continue
                try:
                    # Resolve display values
                    old_display = _resolve_change_display(field, old_val)
                    new_display = _resolve_change_display(field, new_val)
                    if new_val is None or new_val == "" or new_val == 0:
                        content = f"{label} cleared"
                    elif old_val is None or old_val == "" or old_val == 0:
                        content = f"{label} set to {new_display}"
                    else:
                        content = f"{label} changed from {old_display} to {new_display}"
                    # Look up Action category ID (cached at module level)
                    global _action_cat_id_cache
                    if _action_cat_id_cache is None:
                        row = db.query_one("SELECT id FROM history_categories WHERE title = 'Action'")
                        _action_cat_id_cache = row["id"] if row else None
                    cat_id = _action_cat_id_cache
                    db.insert_returning(
                        """INSERT INTO history_events (opportunity_id, category_id, content, created_by, created_at)
                           VALUES (%s, %s, %s, %s, NOW()) RETURNING id""",
                        (opp_id, cat_id, content, user["id"]),
                    )
                    logger.info("Created action event for project %s: %s", opp_id, content)
                except Exception as hist_err:
                    logger.warning("Failed to create change history for project %s field %s: %s", opp_id, field, hist_err)

            # Handle customFieldList if present
            custom_field_list = payload.get("customFieldList")
            if isinstance(custom_field_list, list):
                for cf in custom_field_list:
                    try:
                        field_id = cf.get("fieldId") or cf.get("id")
                        value = cf.get("fieldValue") or cf.get("value") or ""
                        if field_id is not None:
                            db.execute(
                                """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                                   VALUES (%s, %s, %s)
                                   ON CONFLICT (opportunity_id, field_id)
                                   DO UPDATE SET field_value = EXCLUDED.field_value""",
                                (opp_id, str(field_id), str(value)),
                            )
                    except Exception as cf_err:
                        logger.warning("Failed to save custom field %s for project %s: %s", field_id, opp_id, cf_err)

            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to update project %s", opp_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_project_delete(self, opp_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        db.execute("DELETE FROM opportunities WHERE id = %s", (opp_id,))
        _json_response(self, 200, {"ok": True})

    # ── Project Tags ────────────────────────────────────────────────────────

    def _handle_project_tags_get(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT t.id, t.title, t.color, t.is_system FROM tag_definitions t
               JOIN opportunity_tags ot ON t.id = ot.tag_id
               WHERE ot.opportunity_id = %s""",
            (opp_id,),
        )
        _json_response(self, 200, [{"id": r[0], "title": r[1], "color": r[2], "isSystem": r[3]} for r in rows])

    def _handle_project_tag_add(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        tag_id = payload.get("tagId")
        tag_title = str(payload.get("title") or "").strip()
        if not tag_id and tag_title:
            row = db.query("SELECT id FROM tag_definitions WHERE title = %s", (tag_title,), fetch="one")
            if row:
                tag_id = row[0]
            else:
                tag_id = db.insert_returning(
                    "INSERT INTO tag_definitions (title) VALUES (%s) RETURNING id", (tag_title,)
                )
        if not tag_id:
            _json_response(self, 400, {"error": "tagId or title is required"})
            return
        db.execute(
            "INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (opportunity_id, tag_id) DO UPDATE SET assigned_at = NOW()",
            (opp_id, tag_id),
        )
        # Publish tag_added event for automation engine
        event_bus.publish("tag_added", {"opportunity_id": opp_id, "tag_id": tag_id})
        _json_response(self, 200, {"ok": True})

    def _handle_project_tag_remove(self, opp_id: int, tag_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute(
            "DELETE FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
            (opp_id, tag_id),
        )
        _json_response(self, 200, {"ok": True})

    # ── Custom Fields ───────────────────────────────────────────────────────

    def _handle_custom_fields_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT id, field_key, label, field_type, is_required, default_value,
                      sort_order, group_id
               FROM custom_field_definitions WHERE is_active = TRUE ORDER BY sort_order, id"""
        )
        fields = []
        for r in rows:
            opts = db.query(
                "SELECT option_value, option_label FROM custom_field_options WHERE field_id = %s ORDER BY sort_order",
                (r[0],),
            )
            # show_on_create / show_on_edit are deliberately NOT returned. They
            # exist as columns defaulting to TRUE but no handler has ever
            # written them and no frontend reads them, so exposing them in the
            # API contract implied a capability that does not exist.
            fields.append({
                "id": r[0], "key": r[1], "label": r[2], "type": r[3],
                "isRequired": r[4], "defaultValue": r[5], "sortOrder": r[6],
                "groupId": r[7],
                "options": [{"value": o[0], "label": o[1]} for o in opts],
            })
        _json_response(self, 200, fields)

    def _handle_custom_field_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        label = str(payload.get("label") or "").strip()
        if not label:
            _json_response(self, 400, {"error": "Label is required"})
            return
        key = str(payload.get("key") or "").strip()
        if not key:
            key = label.lower().replace(" ", "_").replace("-", "_")
            key = "".join(c for c in key if c.isalnum() or c == "_")
        field_type = str(payload.get("type") or "text").strip()
        if field_type not in CUSTOM_FIELD_TYPES:
            _json_response(self, 400, {"error": f"Unknown field type '{field_type}'. Expected one of: {', '.join(sorted(CUSTOM_FIELD_TYPES))}"})
            return
        is_required = bool(payload.get("isRequired"))
        default_value = payload.get("defaultValue")
        # field_key is UNIQUE. The key is auto-slugged from the label, so two
        # fields with the same label (or a hand-edited key) collide. Without
        # this check the raw UniqueViolation escaped to _run_api_handler and
        # surfaced as a generic 500 "Internal server error" with a stack
        # trace — useless to an admin who only typed a duplicate label.
        existing = db.query_one(
            "SELECT id, label FROM custom_field_definitions WHERE field_key = %s",
            (key,),
        )
        if existing:
            _json_response(self, 409, {
                "error": f"A field with the key '{key}' already exists "
                         f"(“{existing['label']}”). Choose a different field key.",
            })
            return
        max_sort = db.query_one("SELECT COALESCE(MAX(sort_order), 0) as mx FROM custom_field_definitions")["mx"]
        fid = db.insert_returning(
            """INSERT INTO custom_field_definitions (field_key, label, field_type, is_required, default_value, sort_order, group_id, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (key, label, field_type, is_required, default_value, max_sort + 1,
             payload.get("groupId") if payload.get("groupId") not in ("", None) else None,
             user["id"]),
        )
        options = payload.get("options") or []
        for i, opt in enumerate(options):
            val = opt.get("value") or opt.get("label") or ""
            lbl = opt.get("label") or val
            if val:
                db.execute(
                    "INSERT INTO custom_field_options (field_id, option_value, option_label, sort_order) VALUES (%s, %s, %s, %s)",
                    (fid, val, lbl, i),
                )
        _json_response(self, 201, {"id": fid, "ok": True})

    def _handle_custom_field_update(self, field_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if "type" in payload:
            new_type = str(payload.get("type") or "").strip()
            if new_type not in CUSTOM_FIELD_TYPES:
                _json_response(self, 400, {"error": f"Unknown field type '{new_type}'. Expected one of: {', '.join(sorted(CUSTOM_FIELD_TYPES))}"})
                return
        sets, params = [], []
        for field, col in [("label", "label"), ("type", "field_type"), ("isRequired", "is_required"),
                           ("defaultValue", "default_value"), ("sortOrder", "sort_order"),
                           ("groupId", "group_id")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        if sets:
            params.append(field_id)
            db.execute(f"UPDATE custom_field_definitions SET {', '.join(sets)} WHERE id = %s", (*params,))
        if "options" in payload and isinstance(payload["options"], list):
            # DELETE + re-INSERT in ONE transaction. These were separate
            # db.execute() calls, each committing on its own, so a failure
            # part-way through the loop left the field with a truncated (or
            # empty) option list and no way to recover the original values.
            opts = []
            for i, opt in enumerate(payload["options"]):
                if not isinstance(opt, dict):
                    continue
                val = opt.get("value") or opt.get("label") or ""
                lbl = opt.get("label") or val
                if val:
                    opts.append((field_id, val, lbl, i))
            with db.db_cursor() as cur:
                cur.execute("DELETE FROM custom_field_options WHERE field_id = %s", (field_id,))
                for row in opts:
                    cur.execute(
                        "INSERT INTO custom_field_options (field_id, option_value, option_label, sort_order) VALUES (%s, %s, %s, %s)",
                        row,
                    )
        _json_response(self, 200, {"ok": True})

    def _handle_custom_field_delete(self, field_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        count = db.query_one("SELECT COUNT(*) FROM opportunity_custom_field_values WHERE field_id = %s", (field_id,))["count"]
        if count:
            _json_response(self, 409, {"error": f"Cannot delete: {count} project(s) have values for this field"})
            return
        db.execute("DELETE FROM custom_field_options WHERE field_id = %s", (field_id,))
        db.execute("DELETE FROM custom_field_definitions WHERE id = %s", (field_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_field_groups_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT id, label, sort_order, is_collapsed_default, standard_fields
               FROM custom_field_groups ORDER BY sort_order, id"""
        )
        groups = []
        for r in rows:
            std_fields = r[4] if isinstance(r[4], list) else []
            groups.append({
                "id": r[0], "label": r[1], "sortOrder": r[2],
                "isCollapsedDefault": r[3], "standardFields": std_fields,
            })
        _json_response(self, 200, groups)

    def _handle_field_group_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        label = str(payload.get("label") or "").strip()
        if not label:
            _json_response(self, 400, {"error": "Label is required"})
            return
        max_sort = db.query_one("SELECT COALESCE(MAX(sort_order), 0) as mx FROM custom_field_groups")["mx"]
        gid = db.insert_returning(
            """INSERT INTO custom_field_groups (label, sort_order, is_collapsed_default, standard_fields)
               VALUES (%s, %s, %s, %s::jsonb) RETURNING id""",
            (label, max_sort + 1, bool(payload.get("isCollapsedDefault", False)),
             json.dumps(payload.get("standardFields") or [])),
        )
        _json_response(self, 201, {"id": gid, "ok": True})

    def _handle_field_group_update(self, group_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        sets = []
        params = []
        if "label" in payload:
            sets.append("label = %s")
            params.append(str(payload["label"]).strip())
        if "sortOrder" in payload:
            sets.append("sort_order = %s")
            params.append(int(payload["sortOrder"]))
        if "isCollapsedDefault" in payload:
            sets.append("is_collapsed_default = %s")
            params.append(bool(payload["isCollapsedDefault"]))
        if "standardFields" in payload:
            sets.append("standard_fields = %s::jsonb")
            params.append(json.dumps(payload["standardFields"] or []))
        if not sets:
            _json_response(self, 400, {"error": "No fields to update"})
            return
        params.append(group_id)
        db.execute(f"UPDATE custom_field_groups SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    def _handle_field_group_delete(self, group_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        db.execute("UPDATE custom_field_definitions SET group_id = NULL WHERE group_id = %s", (group_id,))
        db.execute("DELETE FROM custom_field_groups WHERE id = %s", (group_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_field_groups_reorder(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = payload.get("ids")
        if not isinstance(ids, list):
            _json_response(self, 400, {"error": "ids array required"})
            return
        for i, gid in enumerate(ids):
            db.execute("UPDATE custom_field_groups SET sort_order = %s WHERE id = %s", (i, int(gid)))
        _json_response(self, 200, {"ok": True})

    def _handle_project_custom_fields_get(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT cfv.field_id, cfv.field_value, cfd.field_key, cfd.label, cfd.field_type
               FROM opportunity_custom_field_values cfv
               JOIN custom_field_definitions cfd ON cfv.field_id = cfd.id
               WHERE cfv.opportunity_id = %s""",
            (opp_id,),
        )
        _json_response(self, 200, [
            {"fieldId": r[0], "value": r[1], "key": r[2], "label": r[3], "type": r[4]}
            for r in rows
        ])

    def _handle_project_custom_fields_update(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        fields = payload.get("fields", payload.get("customFieldList", []))
        for f in fields:
            field_id = f.get("fieldId") or f.get("id")
            value = f.get("value") or f.get("fieldValue") or ""
            if field_id is not None:
                db.execute(
                    """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (opportunity_id, field_id) DO UPDATE SET field_value = EXCLUDED.field_value""",
                    (opp_id, field_id, str(value)),
                )
        _json_response(self, 200, {"ok": True})

    # ── History ─────────────────────────────────────────────────────────────

    def _handle_project_history_get(self, opp_id: int, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        count = int(qs.get("count", ["10"])[0])
        start = int(qs.get("startIndex", ["0"])[0])
        rows = db.query(
            """SELECT h.id, h.category_id, hc.title, h.title, h.content,
                      h.created_by, h.created_at, h.backdated_created_at,
                      u.display_name
               FROM history_events h
               LEFT JOIN history_categories hc ON h.category_id = hc.id
               LEFT JOIN users u ON h.created_by = u.id
               WHERE h.opportunity_id = %s
               ORDER BY h.created_at DESC
               LIMIT %s OFFSET %s""",
            (opp_id, count, start),
        )
        event_ids = [r[0] for r in rows]
        attachments_by_event: dict[int, list] = {}
        if event_ids:
            att_rows = db.query(
                """SELECT ha.id, ha.event_id, ha.filename, ha.file_path, ha.file_size,
                          ha.mime_type, ha.uploaded_by, ha.crm_file_id,
                          pd.id AS project_documents_id
                   FROM history_attachments ha
                   LEFT JOIN project_documents pd
                          ON pd.file_path = ha.file_path
                          AND pd.opportunity_id = (SELECT opportunity_id FROM history_events WHERE id = ha.event_id)
                   WHERE ha.event_id = ANY(%s)""",
                (event_ids,),
            )
            for ar in att_rows:
                attachments_by_event.setdefault(ar[1], []).append({
                    "id": ar[0], "filename": ar[2], "filePath": ar[3],
                    "fileSize": ar[4], "mimeType": ar[5], "uploadedBy": ar[6],
                    "crmFileId": ar[7],
                    "projectDocumentsId": ar[8],
                })
        events = []
        for r in rows:
            events.append({
                "id": r[0], "categoryId": r[1],                 "category": {"id": r[1], "title": r[2]},
                "title": r[3], "content": r[4], "createdBy": r[5],
                "created": r[6].isoformat() if r[6] else None,
                "backdatedCreated": r[7].isoformat() if r[7] else None,
                "author": r[8] or "",
                "attachments": attachments_by_event.get(r[0], []),
            })
        _json_response(self, 200, events)

    def _handle_project_history_create(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        content = str(payload.get("content") or "").strip()
        if not content:
            _json_response(self, 400, {"error": "Note content is required"})
            return
        try:
            category_id = int(payload.get("categoryId") or 1)
        except (TypeError, ValueError):
            category_id = 1
        backdated = str(payload.get("created") or "").strip() or None
        try:
            event_id = db.insert_returning(
                """INSERT INTO history_events (opportunity_id, category_id, content, created_by, backdated_created_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (opp_id, category_id, content, user["id"], backdated),
            )
        except Exception as e:
            print(f"[ERROR] Failed to insert history event for opp {opp_id}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            _json_response(self, 500, {"error": f"Failed to save note: {e}"})
            return
        # Insert notify users
        notify_list = payload.get("notifyUserList") or []
        for uid in notify_list:
            try:
                db.execute(
                    "INSERT INTO history_notify_users (event_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (event_id, int(uid)),
                )
            except (TypeError, ValueError):
                pass
        # Create notifications for tagged users (replaces the old DB trigger
        # which had a timing race — it fired during insert_returning() before
        # history_notify_users was populated).
        if notify_list and int(category_id) in (1, 8, 10):  # Note or Comment (1/8 old OnlyOffice, 10=new DB)
            import re, html as _html_mod
            snippet = _html_mod.unescape(re.sub(r'<[^>]+>', '', content))[:120].strip()
            for uid in notify_list:
                try:
                    db.execute(
                        """INSERT INTO notifications (user_id, type, opportunity_id, actor_user_id, message, payload)
                           SELECT %s, 'note_tagged', %s, %s,
                                  u.display_name || ' tagged you on ' || p.title,
                                  jsonb_build_object('event_id', %s, 'event_category', 'note', 'snippet', %s)
                           FROM users u, opportunities p WHERE p.id = %s AND u.id = %s""",
                        (int(uid), opp_id, user["id"], event_id, snippet, opp_id, user["id"]),
                    )
                    event_bus.publish("notification:new", {
                        "type": "note_tagged", "message": f"{user.get('display_name', 'Someone')} tagged you",
                        "payload": {"event_id": event_id, "snippet": snippet},
                        "opportunity_id": opp_id, "actor_user_id": user["id"],
                    }, user_ids=[str(uid)])
                except Exception as e:
                    print(f"[WARN] Failed to create notification for user {uid}: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
        # Link uploaded document IDs as history attachments.
        # fileIds are project_documents ids — scope the lookup to THIS
        # opportunity so a wrong id can never attach another project's file
        # (photo ids once flowed in here and linked random PDFs cross-opp).
        file_ids = payload.get("fileIds") or []
        for fid in file_ids:
            try:
                fid_int = int(fid)
            except (TypeError, ValueError):
                continue
            doc = db.query(
                "SELECT id, title, file_path, file_size, mime_type, uploaded_by FROM project_documents WHERE id = %s AND opportunity_id = %s AND is_deleted = FALSE",
                (fid_int, opp_id), fetch="one",
            )
            if not doc:
                logger.warning("Note fileId %d does not belong to opp %d — not linked", fid_int, opp_id)
                continue
            db.execute(
                """INSERT INTO history_attachments (event_id, filename, file_path, file_size, mime_type, uploaded_by)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (event_id, doc[1], doc[2], doc[3], doc[4], doc[5]),
            )
        _json_response(self, 201, {"id": event_id, "ok": True})

    def _handle_history_event_delete(self, event_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute("DELETE FROM history_events WHERE id = %s", (event_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_history_attachment_download(self, attachment_id: int) -> None:
        """Serve a history-attachment file by its history_attachments.id.

        Restores the pre-regression behavior (commit 40e0b61) where clicking
        a history attachment downloads the file from the local cache. The file
        lives at data/history-attachments/<opp>/<event>/<crm_file_id>_<safe>
        and we serve it with the original filename + correct Content-Type so
        the browser saves it as e.g. "Schemel Xact estimate.pdf" instead of
        "File 17814.pdf".
        """
        user = _require_auth(self)
        if not user:
            return
        row = db.query_one(
            "SELECT filename, file_path, mime_type, file_size FROM history_attachments WHERE id = %s",
            (attachment_id,),
        )
        if not row:
            self.send_error(404)
            return
        filename = row.get("filename") or "attachment"
        rel_path = row.get("file_path") or ""
        # file_path is stored relative to project root (e.g. data/history-attachments/...)
        # Path-traversal-safe: the row's file_path was written by the importer
        # with safe characters, but we re-validate against the canonical base.
        base = (Path(__file__).parent / "data" / "history-attachments").resolve()
        try:
            full_path = (Path(__file__).parent / rel_path).resolve()
        except (OSError, ValueError):
            self.send_error(400)
            return
        # Must be inside the history-attachments directory tree
        try:
            full_path.relative_to(base)
        except ValueError:
            self.send_error(400)
            return
        if not full_path.is_file():
            self.send_error(404)
            return
        data = full_path.read_bytes()
        ctype = row.get("mime_type") or "application/octet-stream"
        # Sanitize filename for Content-Disposition header
        safe_name = re.sub(r"[^\w.\- ]+", "_", filename).strip("._") or "attachment"
        disposition = f"attachment; filename=\"{safe_name}\""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    # ── Threaded Replies ────────────────────────────────────────────────────

    def _handle_history_replies_get(self, opp_id: int, event_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT r.id, r.parent_reply_id, r.content, r.created_by, r.created_at,
                      r.is_deleted, u.display_name
               FROM history_replies r
               LEFT JOIN users u ON r.created_by = u.id
               WHERE r.event_id = %s
               ORDER BY r.created_at ASC""",
            (event_id,),
        )
        # One fetch for the tagged-user IDs across every reply on this event.
        # Indexed on reply_id (see idx_history_reply_notify_users_reply,
        # migration 031).
        reply_ids = [r[0] for r in rows]
        notify_by_reply: dict[int, list[int]] = {}
        if reply_ids:
            nrows = db.query(
                "SELECT reply_id, user_id FROM history_reply_notify_users WHERE reply_id = ANY(%s)",
                (reply_ids,),
            )
            for nr in nrows:
                notify_by_reply.setdefault(int(nr[0]), []).append(int(nr[1]))
        replies = []
        for r in rows:
            rid = int(r[0])
            replies.append({
                "id": rid, "parentReplyId": r[1], "content": "" if r[5] else r[2],
                "createdBy": r[3], "created": r[4].isoformat() if r[4] else None,
                "isDeleted": r[5], "author": r[6] or "",
                # Tagged (mentioned) user IDs on this reply — parallel to the
                # notifyUserList the client sends on POST. Useful for future
                # UI work (e.g. "Tagged: Alice, Bob" indicator under a
                # reply); additive change, no behavior change today.
                "notifyUserIds": notify_by_reply.get(rid, []),
            })
        _json_response(self, 200, replies)

    def _handle_history_reply_create(self, opp_id: int, event_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        bot_token = _get_bot_token()
        auth_header = self.headers.get("Authorization", "").strip()
        if bot_token and auth_header == f"Bearer {bot_token}":
            # Bot auth — no session cookie (checked BEFORE _require_auth,
            # which sends a 401 immediately when the cookie is absent).
            # Attribute to the chat's linked CRM user: a hardcoded id posted
            # every bot reply as the wrong person.
            bot_user_id = None
            chat_id = payload.get("chatId")
            if chat_id:
                try:
                    mapping = get_mapping_by_chat("sietch", int(chat_id))
                    if mapping and mapping.get("userId"):
                        bot_user_id = int(mapping["userId"])
                except (TypeError, ValueError):
                    pass
            if not bot_user_id:
                _json_response(self, 400, {"error": "Chat is not linked to a CRM user"})
                return
            user = {"id": bot_user_id, "display_name": "Bot"}
        else:
            user = _require_auth(self)
            if not user:
                return
        content = str(payload.get("content") or "").strip()
        if not content:
            _json_response(self, 400, {"error": "Content is required"})
            return
        parent_reply_id = payload.get("parentReplyId")
        reply_id = db.insert_returning(
            """INSERT INTO history_replies (event_id, parent_reply_id, content, created_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (event_id, parent_reply_id, content, user["id"]),
        )
        # Insert notify users (extracted from @-mentions by the rich-text
        # reply editor) and create notification rows for each. Mirrors the
        # event-side flow at lines 5357-5389 for history_notify_users +
        # 'note_tagged' notifications, but uses a new dedicated table
        # (history_reply_notify_users) and notification type ('reply_tagged')
        # so reply tags and event tags remain independent. Migration 031.
        notify_list = payload.get("notifyUserList") or []
        # Don't notify the actor about their own reply.
        actor_id = int(user["id"])
        notify_list = [int(uid) for uid in notify_list if uid and int(uid) != actor_id]
        for uid in notify_list:
            try:
                db.execute(
                    "INSERT INTO history_reply_notify_users (reply_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (reply_id, int(uid)),
                )
            except (TypeError, ValueError):
                pass
        if notify_list:
            import re as _re, html as _html_mod
            # Strip tags to a 120-char preview for the notification body.
            snippet = _html_mod.unescape(_re.sub(r'<[^>]+>', '', content))[:120].strip()
            for uid in notify_list:
                try:
                    # Reuse the same notifications table; payload carries
                    # both event_id and reply_id so the client can deep-link
                    # directly to the reply.
                    db.execute(
                        """INSERT INTO notifications (user_id, type, opportunity_id, actor_user_id, message, payload)
                           SELECT %s, 'reply_tagged', %s, %s,
                                  u.display_name || ' replied on ' || p.title,
                                  jsonb_build_object('event_id', %s, 'reply_id', %s, 'snippet', %s)
                           FROM users u, opportunities p WHERE p.id = %s AND u.id = %s""",
                        (int(uid), opp_id, actor_id, event_id, reply_id, snippet, opp_id, int(uid)),
                    )
                    event_bus.publish("notification:new", {
                        "type": "reply_tagged",
                        "message": f"{user.get('display_name', 'Someone')} replied to a note you're tagged on",
                        "payload": {"event_id": event_id, "reply_id": reply_id, "snippet": snippet},
                        "opportunity_id": opp_id,
                        "actor_user_id": actor_id,
                    }, user_ids=[str(int(uid))])
                except Exception as e:
                    print(f"[WARN] Failed to create reply notification for user {uid}: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
        _json_response(self, 201, {"id": reply_id, "ok": True})

    def _handle_history_reply_delete(self, reply_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute(
            "UPDATE history_replies SET is_deleted = TRUE, deleted_by = %s, deleted_at = NOW() WHERE id = %s",
            (user["id"], reply_id),
        )
        _json_response(self, 200, {"ok": True})

    # ── Stages ──────────────────────────────────────────────────────────────

    def _handle_stages_get(self) -> None:
        # Accept either session auth or bot token auth
        bot_token = _get_bot_token()
        auth_header = self.headers.get("Authorization", "").strip()
        if bot_token and auth_header == f"Bearer {bot_token}":
            pass  # Bot auth OK
        else:
            user = _require_auth(self)
            if not user:
                return
        rows = db.query(
            """SELECT s.id, s.title, s.color, s.sort_order, s.stage_type, s.probability, s.is_active,
                      COUNT(o.id) as project_count
               FROM stages s
               LEFT JOIN opportunities o ON o.stage_id = s.id
               GROUP BY s.id
               ORDER BY s.sort_order, s.id"""
        )
        _json_response(self, 200, [
            {"id": r[0], "title": r[1], "color": r[2], "sortOrder": r[3],
             "stageType": r[4], "probability": r[5], "isActive": r[6], "projectCount": r[7]}
            for r in rows
        ])

    def _handle_stage_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = str(payload.get("title") or "").strip()
        if not title:
            _json_response(self, 400, {"error": "Title is required"})
            return
        sid = db.insert_returning(
            """INSERT INTO stages (title, color, sort_order, stage_type, probability, is_active)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, payload.get("color"), payload.get("sortOrder", 0), payload.get("stageType", 0),
             payload.get("probability", 0), bool(payload.get("isActive", True))),
        )
        _json_response(self, 201, {"id": sid, "ok": True})

    def _handle_stage_update(self, stage_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        sets, params = [], []
        for field, col in [("title", "title"), ("color", "color"), ("sortOrder", "sort_order"),
                           ("stageType", "stage_type"), ("probability", "probability"), ("isActive", "is_active")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        if sets:
            params.append(stage_id)
            db.execute(f"UPDATE stages SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    def _handle_stage_delete(self, stage_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        count = db.query_one("SELECT COUNT(*) FROM opportunities WHERE stage_id = %s", (stage_id,))["count"]
        if count:
            _json_response(self, 409, {"error": f"Cannot delete: {count} project(s) use this stage"})
            return
        db.execute("DELETE FROM stages WHERE id = %s", (stage_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_stages_reorder(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = payload.get("ids")
        if not isinstance(ids, list):
            _json_response(self, 400, {"error": "ids array required"})
            return
        for i, sid in enumerate(ids):
            db.execute("UPDATE stages SET sort_order = %s WHERE id = %s", (i, int(sid)))
        _json_response(self, 200, {"ok": True})

    # ── Tags ────────────────────────────────────────────────────────────────

    def _handle_tags_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query("SELECT id, title, color, is_system FROM tag_definitions ORDER BY title")
        _json_response(self, 200, [{"id": r[0], "title": r[1], "color": r[2], "isSystem": r[3]} for r in rows])

    def _handle_tag_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = str(payload.get("title") or "").strip()
        if not title:
            _json_response(self, 400, {"error": "Title is required"})
            return
        tid = db.insert_returning(
            "INSERT INTO tag_definitions (title, color) VALUES (%s, %s) RETURNING id",
            (title, payload.get("color")),
        )
        _json_response(self, 201, {"id": tid, "ok": True})

    def _handle_tag_update(self, tag_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if "title" in payload:
            db.execute("UPDATE tag_definitions SET title = %s WHERE id = %s", (payload["title"], tag_id))
        if "color" in payload:
            db.execute("UPDATE tag_definitions SET color = %s WHERE id = %s", (payload["color"], tag_id))
        _json_response(self, 200, {"ok": True})

    def _handle_tag_delete(self, tag_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        tag = db.query_one("SELECT is_system FROM tag_definitions WHERE id = %s", (tag_id,))
        if not tag:
            _json_response(self, 404, {"error": "Tag not found"})
            return
        if tag.get("is_system"):
            _json_response(self, 409, {"error": "System tags cannot be deleted"})
            return
        count = db.query_one("SELECT COUNT(*) FROM opportunity_tags WHERE tag_id = %s", (tag_id,))["count"]
        if count:
            _json_response(self, 409, {"error": f"Cannot delete: {count} project(s) use this tag"})
            return
        db.execute("DELETE FROM tag_definitions WHERE id = %s", (tag_id,))
        _json_response(self, 200, {"ok": True})

    # ── History categories CRUD ──────────────────────────────────────────

    def _handle_history_category_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = str(payload.get("title") or "").strip()
        if not title:
            _json_response(self, 400, {"error": "Title is required"})
            return
        color = payload.get("color") or "#6c757d"
        icon_key = payload.get("iconKey") or "note"
        icon_style = payload.get("iconStyle") or "stroke"
        max_order = db.query_one("SELECT COALESCE(MAX(sort_order), 0) as m FROM history_categories")
        sort_order = (max_order["m"] if max_order else 0) + 1
        cid = db.insert_returning(
            """INSERT INTO history_categories (title, display_color, icon_key, icon_style, is_system, sort_order)
               VALUES (%s, %s, %s, %s, FALSE, %s) RETURNING id""",
            (title, color, icon_key, icon_style, sort_order),
        )
        _json_response(self, 201, {"id": cid, "ok": True})

    def _handle_history_category_update(self, cat_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        cat = db.query_one("SELECT is_system FROM history_categories WHERE id = %s", (cat_id,))
        if not cat:
            _json_response(self, 404, {"error": "Category not found"})
            return
        sets = []
        params: list = []
        for field, col in [("title", "title"), ("color", "display_color"), ("iconKey", "icon_key"), ("iconStyle", "icon_style"), ("isSystem", "is_system")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        if not sets:
            _json_response(self, 400, {"error": "No fields to update"})
            return
        params.append(cat_id)
        db.execute(f"UPDATE history_categories SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    def _handle_history_category_delete(self, cat_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        cat = db.query_one("SELECT is_system, title FROM history_categories WHERE id = %s", (cat_id,))
        if not cat:
            _json_response(self, 404, {"error": "Category not found"})
            return
        if cat.get("is_system"):
            _json_response(self, 409, {"error": "System categories cannot be deleted"})
            return
        count = db.query_one("SELECT COUNT(*) as cnt FROM history_events WHERE category_id = %s", (cat_id,))
        if count and count.get("cnt", 0) > 0:
            _json_response(self, 409, {"error": f"Cannot delete: {count['cnt']} events use this category"})
            return
        db.execute("DELETE FROM history_categories WHERE id = %s", (cat_id,))
        _json_response(self, 200, {"ok": True})

    # ── Task labels CRUD ─────────────────────────────────────────────────

    def _handle_task_labels_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query("SELECT id, title, color, sort_order FROM task_labels ORDER BY sort_order, title")
        _json_response(self, 200, [{"id": r[0], "title": r[1], "color": r[2], "sortOrder": r[3]} for r in rows])

    def _handle_task_label_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = str(payload.get("title") or "").strip().lower()
        if not title:
            _json_response(self, 400, {"error": "Title is required"})
            return
        color = payload.get("color")
        max_order = db.query_one("SELECT COALESCE(MAX(sort_order), 0) as m FROM task_labels")
        sort_order = (max_order["m"] if max_order else 0) + 1
        lid = db.insert_returning(
            """INSERT INTO task_labels (title, color, sort_order) VALUES (%s, %s, %s) RETURNING id""",
            (title, color, sort_order),
        )
        _json_response(self, 201, {"id": lid, "ok": True})

    def _handle_task_label_update(self, label_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        sets = []
        params: list = []
        for field, col in [("title", "title"), ("color", "color"), ("sortOrder", "sort_order")]:
            if field in payload:
                val = payload[field]
                if field == "title":
                    val = str(val).strip().lower()
                sets.append(f"{col} = %s")
                params.append(val)
        if not sets:
            _json_response(self, 400, {"error": "No fields to update"})
            return
        params.append(label_id)
        db.execute(f"UPDATE task_labels SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    def _handle_task_label_delete(self, label_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        db.execute("DELETE FROM task_labels WHERE id = %s", (label_id,))
        _json_response(self, 200, {"ok": True})

    # ── Automation settings (admin) ────────────────────────────────────────

    def _handle_automation_settings_get(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        interval = 3600
        try:
            row = db.query_one("SELECT value FROM system_settings WHERE key = 'automation_poll_interval'")
            if row and row.get("value"):
                val = row["value"]
                interval = int(val) if isinstance(val, (int, float)) else int(str(val))
        except Exception:
            pass
        _json_response(self, 200, {"pollInterval": interval})

    def _handle_automation_settings_put(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        interval = int(payload.get("pollInterval", 3600))
        interval = max(60, min(86400, interval))  # clamp: 60s to 24h
        db.execute(
            """INSERT INTO system_settings (key, value) VALUES ('automation_poll_interval', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (json.dumps(interval),),
        )
        _json_response(self, 200, {"pollInterval": interval, "ok": True})

    # ── Branding image upload ──────────────────────────────────────────────

    BRANDING_DEFAULTS = {
        "logo": "/assets/sietch-logo-2-nobg2.png",
        "watermark": "/assets/default-watermark.png",
        "favicon": "/favicon.ico",
    }
    BRANDING_EXT_MAP = {"logo": "png", "watermark": "png", "favicon": "ico"}

    def _branding_image_path(self, img_type: str) -> Path:
        return BRANDING_STORAGE_PATH / f"{img_type}.{self.BRANDING_EXT_MAP.get(img_type, 'png')}"

    def _handle_branding_image_get(self, img_type: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        path = self._branding_image_path(img_type)
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        ct = "image/png"
        if img_type == "favicon":
            ct = "image/x-icon" if path.suffix == ".ico" else "image/png"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def _handle_branding_image_upload(self, img_type: str) -> None:
        user = _require_admin(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        boundary = content_type.split("boundary=")[-1].encode()
        body = _read_body(self)
        if not body:
            _json_response(self, 400, {"error": "Empty body"})
            return
        parts = body.split(b"--" + boundary)
        file_data = None
        for part in parts:
            if b"filename=" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            data = part[header_end + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]
            file_data = data
            break
        if not file_data:
            _json_response(self, 400, {"error": "No file uploaded"})
            return
        if len(file_data) > 5 * 1024 * 1024:
            _json_response(self, 400, {"error": "File too large (max 5MB)"})
            return
        is_png = file_data[:4] == b"\x89PNG"
        is_jpeg = file_data[:2] == b"\xff\xd8"
        is_ico = file_data[:4] == b"\x00\x00\x01\x00"
        is_svg = file_data[:5] == b"<svg " or file_data[:4] == b"<?xml"
        if not (is_png or is_jpeg or is_ico or is_svg):
            _json_response(self, 400, {"error": "Only PNG, JPG, ICO, or SVG files are supported"})
            return
        BRANDING_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
        dest = self._branding_image_path(img_type)
        if img_type == "favicon" and not is_ico:
            try:
                from PIL import Image as PILImage
                import io
                img = PILImage.open(io.BytesIO(file_data))
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGBA")
                else:
                    img = img.convert("RGB")
                img = img.resize((32, 32), PILImage.LANCZOS)
                ico_path = dest.with_suffix(".ico")
                img.save(str(ico_path), format="ICO", sizes=[(32, 32)])
                dest = ico_path
            except ImportError:
                dest.write_bytes(file_data)
        elif img_type == "watermark":
            try:
                from PIL import Image as PILImage
                import io
                img = PILImage.open(io.BytesIO(file_data)).convert("RGBA")
                bbox = img.getbbox()
                if bbox:
                    img = img.crop(bbox)
                size = max(img.size)
                canvas = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
                offset = ((size - img.width) // 2, (size - img.height) // 2)
                canvas.paste(img, offset, img)
                alpha = canvas.split()[3]
                alpha = alpha.point(lambda p: int(p * 0.25))
                canvas.putalpha(alpha)
                canvas.thumbnail((800, 800), PILImage.LANCZOS)
                buf = io.BytesIO()
                canvas.save(buf, format="PNG")
                dest.write_bytes(buf.getvalue())
            except ImportError:
                dest.write_bytes(file_data)
        else:
            dest.write_bytes(file_data)
        db_col = {"logo": "logo_path", "watermark": "watermark_path", "favicon": "favicon_path"}[img_type]
        db_url = f"/api/branding/{img_type}"
        existing = db.query_one("SELECT id FROM branding LIMIT 1")
        if existing:
            db.execute(f"UPDATE branding SET {db_col} = %s WHERE id = %s", (db_url, existing["id"]))
        else:
            db.execute(f"INSERT INTO branding ({db_col}) VALUES (%s)", (db_url,))
        _json_response(self, 200, {"ok": True, "url": db_url})

    def _handle_branding_image_delete(self, img_type: str) -> None:
        user = _require_admin(self)
        if not user:
            return
        path = self._branding_image_path(img_type)
        if path.exists():
            path.unlink(missing_ok=True)
        if img_type == "favicon":
            ico_path = BRANDING_STORAGE_PATH / "favicon.ico"
            if ico_path.exists():
                ico_path.unlink(missing_ok=True)
        db_col = {"logo": "logo_path", "watermark": "watermark_path", "favicon": "favicon_path"}[img_type]
        default = self.BRANDING_DEFAULTS.get(img_type)
        existing = db.query_one("SELECT id FROM branding LIMIT 1")
        if existing:
            db.execute(f"UPDATE branding SET {db_col} = %s WHERE id = %s", (default, existing["id"]))
        _json_response(self, 200, {"ok": True, "url": default})

    # ── Contacts ────────────────────────────────────────────────────────────

    def _handle_contacts_get(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        where, params = ["1=1"], []
        if "q" in qs:
            where.append("(first_name ILIKE %s OR last_name ILIKE %s OR company ILIKE %s OR email ILIKE %s OR about ILIKE %s OR website ILIKE %s OR contact_type ILIKE %s)")
            q = f"%{qs['q'][0]}%"
            params.extend([q, q, q, q, q, q, q])
        if "type" in qs:
            t = qs["type"][0].lower()
            if t == "company":
                where.append("is_company = TRUE")
            elif t == "person":
                where.append("is_company = FALSE")
        if "contact_type" in qs:
            where.append("contact_type = %s")
            params.append(qs["contact_type"][0])
        if user.get("role") == "limited":
            where.append("created_by = %s")
            params.append(user["id"])
        limit = min(int(qs.get("count", ["200"])[0]), 500)
        offset = int(qs.get("startIndex", ["0"])[0])
        rows = db.query(
            f"""SELECT id, first_name, last_name, email, phone, company, is_company, company_id,
                       contact_type, website, industry, currency, about, address_street, address_city,
                       address_state, address_zip, address_country, job_title, onlyoffice_id,
                       contact_status, avatar_url, mobile_phone, created_at
                FROM contacts WHERE {' AND '.join(where)}
                ORDER BY is_company DESC, last_name, first_name
                LIMIT %s OFFSET %s""",
            (*params, limit, offset),
        )
        _json_response(self, 200, [
            {"id": r[0], "firstName": r[1], "lastName": r[2], "email": r[3], "phone": r[4],
             "company": r[5], "isCompany": r[6], "companyId": r[7],
             "contactType": r[8], "website": r[9], "industry": r[10],
             "currency": r[11], "about": r[12], "addressStreet": r[13],
             "addressCity": r[14], "addressState": r[15], "addressZip": r[16],
             "addressCountry": r[17], "jobTitle": r[18], "onlyofficeId": r[19],
             "contactStatus": r[20], "avatarUrl": f"/api/v2/contacts/{r[0]}/photo" if r[21] else None, "mobilePhone": r[22],
             "createdAt": r[23].isoformat() if r[23] else None,
             "displayName": (r[5] if r[6] else f"{r[1] or ''} {r[2] or ''}".strip()) or r[3] or "Contact"}
            for r in rows
        ])

    def _handle_contact_create(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        is_company = bool(payload.get("isCompany"))
        cid = db.insert_returning(
            """INSERT INTO contacts (first_name, last_name, email, phone, mobile_phone, company, company_id,
               job_title, is_company, contact_type, website, industry, currency, about,
               address_street, address_city, address_state, address_zip, address_country, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (payload.get("firstName"), payload.get("lastName"), payload.get("email"),
             payload.get("phone"), payload.get("mobilePhone"), payload.get("company"),
             payload.get("companyId"), payload.get("jobTitle"), is_company,
             payload.get("contactType"), payload.get("website"), payload.get("industry"),
             payload.get("currency"), payload.get("about"),
             payload.get("addressStreet"), payload.get("addressCity"),
             payload.get("addressState"), payload.get("addressZip"),
             payload.get("addressCountry"), user["id"]),
        )
        _json_response(self, 201, {"id": cid, "ok": True})

    def _handle_contact_update(self, contact_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        sets, params = [], []
        for field, col in [("firstName", "first_name"), ("lastName", "last_name"), ("email", "email"),
                           ("phone", "phone"), ("mobilePhone", "mobile_phone"),
                           ("company", "company"), ("companyId", "company_id"),
                           ("jobTitle", "job_title"), ("isCompany", "is_company"),
                           ("contactType", "contact_type"), ("website", "website"),
                           ("industry", "industry"), ("currency", "currency"),
                           ("about", "about"), ("addressStreet", "address_street"),
                           ("addressCity", "address_city"), ("addressState", "address_state"),
                           ("addressZip", "address_zip"), ("addressCountry", "address_country"),
                           ("contactStatus", "contact_status"), ("avatarUrl", "avatar_url")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        if sets:
            params.append(contact_id)
            db.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    def _handle_contact_delete(self, contact_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        # Check FK references
        opp_count = db.query_one("SELECT COUNT(*) FROM opportunities WHERE contact_id = %s", (contact_id,))["count"]
        if opp_count:
            _json_response(self, 409, {"error": f"Cannot delete: {opp_count} deal(s) reference this contact"})
            return
        person_count = db.query_one("SELECT COUNT(*) FROM contacts WHERE company_id = %s", (contact_id,))["count"]
        if person_count:
            _json_response(self, 409, {"error": f"Cannot delete: {person_count} person(s) linked to this company"})
            return
        db.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_contacts_count(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        where, params = ["1=1"], []
        if "q" in qs:
            where.append("(first_name ILIKE %s OR last_name ILIKE %s OR company ILIKE %s OR email ILIKE %s OR about ILIKE %s OR website ILIKE %s OR contact_type ILIKE %s)")
            q = f"%{qs['q'][0]}%"
            params.extend([q, q, q, q, q, q, q])
        if "type" in qs:
            t = qs["type"][0].lower()
            if t == "company":
                where.append("is_company = TRUE")
            elif t == "person":
                where.append("is_company = FALSE")
        if "contact_type" in qs:
            where.append("contact_type = %s")
            params.append(qs["contact_type"][0])
        if user.get("role") == "limited":
            where.append("created_by = %s")
            params.append(user["id"])
        row = db.query_one(f"SELECT COUNT(*) FROM contacts WHERE {' AND '.join(where)}", (*params,))
        _json_response(self, 200, {"count": row["count"] if row else 0})

    def _handle_contacts_export(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT id, is_company, first_name, last_name, company, contact_type, email, phone,
                      mobile_phone, website, industry, currency, about, contact_status,
                      address_street, address_city, address_state, address_zip, address_country,
                      job_title, onlyoffice_id, created_at
               FROM contacts ORDER BY is_company DESC, last_name, first_name"""
        )
        import io, csv
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "is_company", "first_name", "last_name", "company", "contact_type",
                         "email", "phone", "mobile_phone", "website", "industry", "currency",
                         "about", "contact_status", "address_street", "address_city",
                         "address_state", "address_zip", "address_country", "job_title",
                         "onlyoffice_id", "created_at"])
        for r in rows:
            writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                             r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16],
                             r[17], r[18], r[19], r[20],
                             r[21].isoformat() if r[21] else ""])
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=\"contacts_export.csv\"")
        self.end_headers()
        self.wfile.write(buf.getvalue().encode("utf-8"))

    def _handle_contacts_import(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        csv_text = str(payload.get("csv") or "").strip()
        if not csv_text:
            _json_response(self, 400, {"error": "No CSV data provided"})
            return
        import io, csv
        reader = csv.DictReader(io.StringIO(csv_text))
        created = 0
        skipped = 0
        errors = []
        for idx, row in enumerate(reader):
            try:
                # Auto-detect column mappings
                first = row.get("First Name") or row.get("first_name") or ""
                last = row.get("Last Name") or row.get("last_name") or ""
                company = row.get("Company Name") or row.get("company") or ""
                email = (row.get("Email (work)") or row.get("Email (home)") or row.get("Email (other)")
                         or row.get("email") or "")
                phone = (row.get("Phone (work)") or row.get("Phone (mobile)") or row.get("Phone (home)")
                         or row.get("phone") or "")
                contact_type = row.get("Contact Type") or row.get("contact_type") or ""
                is_company = bool(company.strip()) and not first.strip() and not last.strip()
                first = first.strip() or None
                last = last.strip() or None
                company = company.strip() or None
                email = email.strip() or None
                phone = phone.strip() or None
                contact_type = contact_type.strip() or None
                db.insert_returning(
                    """INSERT INTO contacts (first_name, last_name, company, email, phone, contact_type,
                       is_company, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (first, last, company, email, phone, contact_type, is_company, user["id"]),
                )
                created += 1
            except Exception as e:
                errors.append(f"Row {idx+1}: {str(e)}")
        _json_response(self, 200, {"created": created, "skipped": skipped, "errors": errors})

    def _handle_contact_history_get(self, contact_id: int, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        limit = min(int(qs.get("count", ["50"])[0]), 200)
        offset = int(qs.get("startIndex", ["0"])[0])
        rows = db.query(
            """SELECT he.id, he.category_id, hc.title, hc.display_color, he.title, he.content,
                      he.created_by, he.created_at, he.backdated_created_at, u.display_name
               FROM history_events he
               LEFT JOIN history_categories hc ON he.category_id = hc.id
               LEFT JOIN users u ON he.created_by = u.id
               WHERE he.contact_id = %s
               ORDER BY COALESCE(he.backdated_created_at, he.created_at) DESC
               LIMIT %s OFFSET %s""",
            (contact_id, limit, offset),
        )
        events = []
        for r in rows:
            events.append({
                "id": r[0], "categoryId": r[1],
                "category": {"title": r[2], "color": r[3]} if r[2] else None,
                "title": r[4], "content": r[5],
                "createdBy": r[6],
                "created": r[7].isoformat() if r[7] else None,
                "backdatedCreated": r[8].isoformat() if r[8] else None,
                "author": r[9],
            })
        _json_response(self, 200, events)

    def _handle_contact_history_create(self, contact_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        content = str(payload.get("content") or "").strip()
        category_id = payload.get("categoryId") or 1
        backdated = str(payload.get("created") or "").strip() or None
        event_id = db.insert_returning(
            """INSERT INTO history_events (contact_id, category_id, content, created_by, backdated_created_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (contact_id, category_id, content, user["id"], backdated),
        )
        # Insert notify users
        notify_list = payload.get("notifyUserList") or []
        for uid in notify_list:
            try:
                db.execute(
                    "INSERT INTO history_notify_users (event_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (event_id, int(uid)),
                )
            except (TypeError, ValueError):
                pass
        _json_response(self, 201, {"id": event_id, "ok": True})

    def _handle_contact_photo_get(self, contact_id: int, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        # Audit 2026-10-14: prefer the locally-downloaded avatar at
        # avatar_local_path (set by _handle_sync_pull_contacts after
        # downloading the OnlyOffice avatar to data/contact-avatars/).
        # The previous code derived the path from avatar_url — which for
        # synced contacts points at the remote OnlyOffice URL, not a
        # local file — so 404'd every time.
        row = db.query_one(
            "SELECT avatar_url, avatar_local_path FROM contacts WHERE id = %s",
            (contact_id,),
        )
        if not row:
            self.send_response(404)
            self.end_headers()
            return
        is_thumb = qs.get("thumbnail", ["0"])[0] == "1"
        photo_path = None
        if row.get("avatar_local_path"):
            local = (Path(__file__).resolve().parent / row["avatar_local_path"]).resolve()
            # Path traversal guard: must stay under data/contact-avatars/
            try:
                local.relative_to((Path(__file__).resolve().parent / "data" / "contact-avatars").resolve())
            except ValueError:
                local = None
            if local and local.exists() and local.is_file():
                photo_path = local
        if not photo_path and row.get("avatar_url"):
            base = Path(row["avatar_url"]).parent
            filename = "thumb_photo.jpg" if is_thumb else "photo.jpg"
            candidate = base / filename
            if candidate.exists():
                photo_path = candidate
            else:
                candidate = base / "photo.jpg"
                if candidate.exists():
                    photo_path = candidate
        if not photo_path:
            self.send_response(404)
            self.end_headers()
            return
        # Guess content type by extension.
        ext = photo_path.suffix.lower()
        ctype = "image/jpeg"
        if ext == ".png":
            ctype = "image/png"
        elif ext == ".gif":
            ctype = "image/gif"
        elif ext == ".webp":
            ctype = "image/webp"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(photo_path.read_bytes())

    def _handle_contact_avatar_serve(self, filename: str) -> None:
        """Serve a downloaded OnlyOffice contact avatar from data/contact-avatars/.

        Used by the contact-list UI to render avatar_local_path entries. Auth required
        (this is a CRM, not public). Path traversal-safe: filename must be a basename.
        """
        user = _require_auth(self)
        if not user:
            return
        # Strip any path components — only the basename
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename:
            self.send_response(400)
            self.end_headers()
            return
        avatar_path = ROOT / "data" / "contact-avatars" / safe_name
        if not avatar_path.exists() or not avatar_path.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ext = avatar_path.suffix.lower()
        ctype = "image/jpeg"
        if ext == ".png":
            ctype = "image/png"
        elif ext == ".gif":
            ctype = "image/gif"
        elif ext == ".webp":
            ctype = "image/webp"
        data = avatar_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _handle_my_avatar_style_get(self) -> None:
        """Return the current user's avatar style (icon name or null)."""
        user = _require_auth(self)
        if not user:
            return
        row = db.query_one("SELECT avatar_icon FROM users WHERE id = %s", (user["id"],))
        icon = row.get("avatar_icon") if row else None
        _json_response(self, 200, {"avatar_icon": icon})

    def _handle_my_avatar_style_put(self) -> None:
        """Set the current user's avatar style. Body: {"avatar_icon": "cat"} | {"avatar_icon": null}"""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON"})
            return
        icon = body.get("avatar_icon")
        if icon is not None and not isinstance(icon, str):
            _json_response(self, 400, {"error": "avatar_icon must be a string or null"})
            return
        # Validate against known Tabler icon pool
        allowed = set(_TABLER_ICON_OPTIONS)
        if icon is not None and icon not in allowed:
            _json_response(self, 400, {"error": f"Unknown icon '{icon}'"})
            return
        db.execute("UPDATE users SET avatar_icon = %s WHERE id = %s", (icon or None, user["id"]))
        _json_response(self, 200, {"ok": True, "avatar_icon": icon})

    def _handle_users_avatar_style_get(self) -> None:
        """Public avatar style list (any auth user can fetch)."""
        user = _require_auth(self)
        if not user:
            return
        rows = db.query_dicts("SELECT id, avatar_icon FROM users WHERE avatar_icon IS NOT NULL")
        out = {str(r["id"]): r["avatar_icon"] for r in rows}
        _json_response(self, 200, out)

    def _handle_contact_photo_upload(self, contact_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        boundary = content_type.split("boundary=")[-1].encode()
        body = _read_body(self)
        if not body:
            _json_response(self, 400, {"error": "Empty body"})
            return
        # Parse multipart manually (simple approach)
        parts = body.split(b"--" + boundary)
        file_data = None
        filename = ""
        for part in parts:
            if b"filename=" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            header = part[:header_end].decode("utf-8", errors="replace")
            data = part[header_end + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]
            # Extract filename
            for token in header.split(";"):
                if "filename=" in token:
                    filename = token.split("filename=")[-1].strip('" ')
            if filename:
                file_data = data
                break
        if not file_data:
            _json_response(self, 400, {"error": "No file uploaded"})
            return
        # Validate size (5MB max)
        if len(file_data) > 5 * 1024 * 1024:
            _json_response(self, 400, {"error": "File too large (max 5MB)"})
            return
        # Validate type by checking magic bytes
        is_jpeg = file_data[:2] == b"\xff\xd8"
        is_png = file_data[:4] == b"\x89PNG"
        if not is_jpeg and not is_png:
            _json_response(self, 400, {"error": "Only JPG and PNG files are supported"})
            return
        # Save to disk
        photo_dir = CONTACT_PHOTO_STORAGE_PATH / str(contact_id)
        photo_dir.mkdir(parents=True, exist_ok=True)
        photo_path = photo_dir / "photo.jpg"
        thumb_path = photo_dir / "thumb_photo.jpg"
        try:
            from PIL import Image as PILImage
            import io
            img = PILImage.open(io.BytesIO(file_data))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB", palette=PILImage.ADAPTIVE)
            elif img.mode != "RGB":
                img = img.convert("RGB")
            # Full size (max 400x400)
            img.thumbnail((400, 400), PILImage.LANCZOS)
            img.save(str(photo_path), "JPEG", quality=92)
            # Thumbnail (96x96)
            thumb = img.copy()
            thumb.thumbnail((96, 96), PILImage.LANCZOS)
            thumb.save(str(thumb_path), "JPEG", quality=88)
        except ImportError:
            # Pillow not available — save raw
            photo_path.write_bytes(file_data)
        # Update DB
        rel_path = str(photo_path.relative_to(ROOT))
        db.execute("UPDATE contacts SET avatar_url = %s WHERE id = %s", (rel_path, contact_id))
        _json_response(self, 200, {"ok": True, "avatarUrl": f"/api/v2/contacts/{contact_id}/photo"})

    def _handle_sync_pull_contacts(self) -> None:
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            _append_sync_log("info", "Authenticating to OnlyOffice CRM...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return

            # Build onlyoffice_id → local_id map for existing contacts
            existing_oo_ids = {}
            for row in db.query("SELECT id, onlyoffice_id FROM contacts WHERE onlyoffice_id IS NOT NULL"):
                existing_oo_ids[str(row[1])] = row[0]

            _append_sync_log("info", "Fetching contacts from OnlyOffice...")
            start = 0
            page_size = 500
            all_contacts = []
            while True:
                sep = "&" if "?" in "/api/2.0/crm/contact/filter" else "?"
                path = f"/api/2.0/crm/contact/filter{sep}count={page_size}&startIndex={start}"
                data = _unwrap(_crm_api_get(portal, token, path))
                if not isinstance(data, list) or not data:
                    break
                all_contacts.extend(data)
                if len(data) < page_size:
                    break
                start += page_size

            _append_sync_log("info", f"Total contacts to sync: {len(all_contacts)}")
            created = 0
            skipped = 0
            errors = 0
            start_time = time.time()

            # First pass: upsert all contacts with multi-value + avatar
            oo_id_to_local = {}
            multi_emails = 0
            multi_phones = 0
            multi_addrs = 0
            for c in all_contacts:
                if not isinstance(c, dict):
                    continue
                oo_id = str(c.get("id") or "")
                if not oo_id:
                    continue
                is_company = bool(c.get("isCompany", False))
                first_name = str(c.get("firstName") or "").strip() or None
                last_name = str(c.get("lastName") or "").strip() or None
                company_name = str(c.get("companyName") or "").strip() or None

                # Multi-value commonData
                common_data = c.get("commonData") or []
                email = None
                phone = None
                website = None
                for item in common_data:
                    if not isinstance(item, dict):
                        continue
                    info_type = item.get("infoType")
                    data = str(item.get("data") or "").strip()
                    is_primary = bool(item.get("isPrimary"))
                    if info_type == 1 and is_primary and not email:
                        email = data or None
                    if info_type == 0 and is_primary and not phone:
                        phone = data or None
                    if info_type == 2 and not website:
                        website = data or None
                if not email:
                    for item in common_data:
                        if isinstance(item, dict) and item.get("infoType") == 1:
                            email = str(item.get("data") or "").strip() or None
                            break
                if not phone:
                    for item in common_data:
                        if isinstance(item, dict) and item.get("infoType") == 0:
                            phone = str(item.get("data") or "").strip() or None
                            break

                # Multi-value addresses
                addresses = c.get("addresses") or []
                primary_addr = None
                for a in addresses:
                    if isinstance(a, dict) and a.get("isPrimary"):
                        primary_addr = a
                        break
                if not primary_addr and addresses and isinstance(addresses[0], dict):
                    primary_addr = addresses[0]

                about = str(c.get("about") or "").strip() or None
                industry = str(c.get("industry") or "").strip() or None
                currency = str(c.get("currency") or "").strip() or None
                contact_status = None
                status_id = None
                cs = c.get("contactStatus")
                if isinstance(cs, dict):
                    contact_status = str(cs.get("title") or "").strip() or None
                    status_id = cs.get("id")
                task_count = int(c.get("taskCount") or 0) if c.get("taskCount") is not None else None
                avatar_url = str(c.get("smallFotoUrl") or "").strip() or None

                # Download avatar (Bug #19)
                avatar_local = None
                if avatar_url:
                    full_url = avatar_url if avatar_url.startswith("http") else f"{portal.rstrip('/')}{avatar_url}"
                    raw = _crm_get_raw_bytes(full_url, headers={"Authorization": token}, timeout=15)
                    if raw:
                        ext = "jpg"
                        if "png" in full_url.lower():
                            ext = "png"
                        avatar_path = CONTACT_AVATAR_DIR_LIVE / f"{oo_id}.{ext}"
                        avatar_path.parent.mkdir(parents=True, exist_ok=True)
                        avatar_path.write_bytes(raw)
                        avatar_local = str(avatar_path.relative_to(Path(__file__).resolve().parent))

                # Creator
                create_by = c.get("createBy") or {}
                creator_local = None
                if isinstance(create_by, dict):
                    crm_creator = str(create_by.get("id") or "")
                    creator_local = _crm_fetch_user_id_map(portal, token).get(crm_creator)

                existing_local_id = existing_oo_ids.get(oo_id)
                try:
                    if existing_local_id:
                        # Bug #9: UPDATE existing contact
                        db.execute(
                            """UPDATE contacts SET
                                   first_name = COALESCE(%s, first_name),
                                   last_name = COALESCE(%s, last_name),
                                   company = COALESCE(%s, company),
                                   email = COALESCE(%s, email),
                                   phone = COALESCE(%s, phone),
                                   website = COALESCE(%s, website),
                                   industry = COALESCE(%s, industry),
                                   about = COALESCE(%s, about),
                                   contact_status = COALESCE(%s, contact_status),
                                   avatar_url = COALESCE(%s, avatar_url),
                                   avatar_local_path = COALESCE(%s, avatar_local_path),
                                   address_street = COALESCE(%s, address_street),
                                   address_city = COALESCE(%s, address_city),
                                   address_state = COALESCE(%s, address_state),
                                   address_zip = COALESCE(%s, address_zip),
                                   address_country = COALESCE(%s, address_country),
                                   onlyoffice_task_count = COALESCE(%s, onlyoffice_task_count),
                                   onlyoffice_status_id = COALESCE(%s, onlyoffice_status_id),
                                   raw_about = %s, raw_industry = %s
                               WHERE id = %s""",
                            (first_name, last_name, company_name, email, phone, website,
                             industry, about, contact_status, avatar_url, avatar_local,
                             str(primary_addr.get("street") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("city") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("state") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("zip") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("country") or "").strip() or None if primary_addr else None,
                             task_count, status_id, about, industry, existing_local_id),
                        )
                        local_id = existing_local_id
                        skipped += 1
                    else:
                        # INSERT new contact
                        new_id = db.insert_returning(
                            """INSERT INTO contacts
                                   (first_name, last_name, company, email, phone, is_company,
                                    website, industry, currency, about, contact_status, avatar_url,
                                    onlyoffice_id, address_street, address_city, address_state, address_zip,
                                    address_country, created_by,
                                    onlyoffice_task_count, onlyoffice_status_id,
                                    raw_about, raw_industry, avatar_local_path)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               RETURNING id""",
                            (first_name, last_name, company_name, email, phone, is_company,
                             website, industry, currency, about, contact_status, avatar_url,
                             oo_id,
                             str(primary_addr.get("street") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("city") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("state") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("zip") or "").strip() or None if primary_addr else None,
                             str(primary_addr.get("country") or "").strip() or None if primary_addr else None,
                             creator_local,
                             task_count, status_id, about, industry, avatar_local),
                        )
                        local_id = new_id
                        created += 1
                    oo_id_to_local[oo_id] = local_id
                except Exception as e:
                    _append_sync_log("error", f"Failed to upsert contact {oo_id}: {e}")
                    errors += 1
                    continue

                # Multi-value commonData rows (Bug #10)
                if not existing_local_id:
                    for it in common_data:
                        if not isinstance(it, dict):
                            continue
                        data = str(it.get("data") or "").strip()
                        if not data:
                            continue
                        info_type = it.get("infoType")
                        if info_type == 1:  # email
                            db.execute(
                                """INSERT INTO contact_emails
                                       (contact_id, email, category, category_id, is_primary, onlyoffice_id)
                                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                (local_id, data, str(it.get("categoryName") or "") or None,
                                 it.get("category"), bool(it.get("isPrimary")),
                                 int(it.get("id") or 0) or None),
                            )
                            multi_emails += 1
                        elif info_type == 0:  # phone
                            db.execute(
                                """INSERT INTO contact_phones
                                       (contact_id, phone, category, category_id, is_primary, onlyoffice_id)
                                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                (local_id, data, str(it.get("categoryName") or "") or None,
                                 it.get("category"), bool(it.get("isPrimary")),
                                 int(it.get("id") or 0) or None),
                            )
                            multi_phones += 1

                    # Multi-value addresses
                    for a in addresses:
                        if not isinstance(a, dict):
                            continue
                        db.execute(
                            """INSERT INTO contact_addresses
                                   (contact_id, street, city, state, zip, country,
                                    category, category_id, is_primary, onlyoffice_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING""",
                            (local_id,
                             str(a.get("street") or "").strip() or None,
                             str(a.get("city") or "").strip() or None,
                             str(a.get("state") or "").strip() or None,
                             str(a.get("zip") or "").strip() or None,
                             str(a.get("country") or "").strip() or None,
                             str(a.get("categoryName") or "") or None,
                             a.get("category"),
                             bool(a.get("isPrimary")),
                             a.get("id")),
                        )
                        multi_addrs += 1

            # Second pass: resolve person→company linkage
            for c in all_contacts:
                if not isinstance(c, dict):
                    continue
                if c.get("isCompany"):
                    continue
                oo_id = str(c.get("id") or "")
                local_id = oo_id_to_local.get(oo_id)
                if not local_id:
                    continue
                company_obj = c.get("company")
                if isinstance(company_obj, dict):
                    comp_oo_id = str(company_obj.get("id") or "")
                    comp_local_id = oo_id_to_local.get(comp_oo_id)
                    if comp_local_id:
                        db.execute("UPDATE contacts SET company_id = %s, company = COALESCE(company, %s) WHERE id = %s",
                                   (comp_local_id, str(company_obj.get("displayName") or "").strip() or None, local_id))

            # Third pass: pull contact history events from OnlyOffice.
            # Audit 2026-10-14: the contact sync previously created/updated
            # contacts but never fetched their history events, so synced
            # contacts always showed "No history yet." in the search
            # modal. The deal sync already does this via
            # `/api/2.0/crm/history/filter?entityType=opportunity&entityId=...`
            # — mirror that pattern for contacts (entityType=contact).
            history_count = 0
            history_errors = 0
            try:
                user_id_map = _crm_fetch_user_id_map(portal, token)
                for oo_id, local_id in oo_id_to_local.items():
                    try:
                        hist_data = _unwrap(_crm_api_get(
                            portal, token,
                            f"/api/2.0/crm/history/filter?entityType=contact&entityId={oo_id}&count=500",
                        ))
                        hist_list = hist_data if isinstance(hist_data, list) else []
                        if not hist_list:
                            continue

                        # Dedup: crm_event_id-based when present (faster, exact).
                        existing_event_ids = set()
                        for row in db.query_dicts(
                            "SELECT crm_event_id FROM history_events WHERE contact_id = %s AND crm_event_id IS NOT NULL",
                            (local_id,),
                        ):
                            if row.get("crm_event_id") is not None:
                                existing_event_ids.add(int(row["crm_event_id"]))
                        # Fallback dedup for events without crm_event_id.
                        existing_keys = set()
                        if not existing_event_ids:
                            for ev in db.query_dicts(
                                "SELECT category_id, content, created_at FROM history_events WHERE contact_id = %s",
                                (local_id,),
                            ):
                                content_prefix = (ev.get("content") or "")[:200]
                                created_key = str(ev.get("created_at") or "")[:19]
                                existing_keys.add((ev.get("category_id"), content_prefix, created_key))

                        for h in hist_list:
                            if not isinstance(h, dict):
                                continue
                            cat_data = h.get("category") or h.get("Category") or {}
                            if isinstance(cat_data, dict):
                                cat_title = str(cat_data.get("title") or cat_data.get("Title") or "Note").strip() or "Note"
                            else:
                                cat_title = "Note"
                            cat_row = db.query_one("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
                            if not cat_row:
                                db.execute(
                                    "INSERT INTO history_categories (title, is_system) VALUES (%s, FALSE) RETURNING id",
                                    (cat_title,),
                                )
                                cat_row = db.query_one("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
                            cat_id = cat_row["id"]

                            event_content = str(h.get("content") or h.get("Content") or "").strip() or None
                            event_created = h.get("created") or h.get("Created") or h.get("date") or None
                            create_by = h.get("createBy") or h.get("CreateBy") or h.get("createdBy") or {}
                            crm_author_id = str(create_by.get("id") or create_by.get("ID") or "") if isinstance(create_by, dict) else ""
                            v3_author_id = user_id_map.get(crm_author_id)

                            crm_event_id = int(h.get("id") or 0) or None

                            if crm_event_id and crm_event_id in existing_event_ids:
                                continue
                            if not crm_event_id:
                                content_prefix = (event_content or "")[:200]
                                created_key = str(event_created)[:19] if event_created else ""
                                if (cat_id, content_prefix, created_key) in existing_keys:
                                    continue

                            db.execute(
                                """INSERT INTO history_events
                                       (contact_id, category_id, content, created_by, created_at,
                                        backdated_created_at, crm_event_id, crm_entity_id)
                                   VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s)""",
                                (local_id, cat_id, event_content, v3_author_id, event_created, event_created,
                                 crm_event_id, int(h.get("entityId") or 0) or None),
                            )
                            history_count += 1
                            if crm_event_id:
                                existing_event_ids.add(crm_event_id)
                            if not crm_event_id:
                                existing_keys.add((cat_id, (event_content or "")[:200],
                                                   str(event_created)[:19] if event_created else ""))
                    except Exception as he:
                        history_errors += 1
                        _append_sync_log("error", f"Contact history sync failed for {oo_id}: {he}")
                        continue
            except Exception as e:
                _append_sync_log("error", f"Contact history sync pass failed: {e}")

            elapsed = round(time.time() - start_time, 1)
            _append_sync_log("info", f"Contacts sync complete: {created} created, {skipped} updated, {errors} errors, {multi_emails} emails, {multi_phones} phones, {multi_addrs} addresses, {history_count} history events in {elapsed}s")
            _json_response(self, 200, {"created": created, "updated": skipped, "errors": errors,
                                       "multi_emails": multi_emails, "multi_phones": multi_phones,
                                       "multi_addresses": multi_addrs, "elapsed": elapsed})
        except Exception as e:
            _append_sync_log("error", f"Sync failed: {e}")
            _json_response(self, 500, {"error": str(e)})
        finally:
            _sync_running = False

    # ── Tasks ───────────────────────────────────────────────────────────────

    def _handle_tasks_get(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        closed = qs.get("closed", ["false"])[0].lower() == "true"
        archived = qs.get("archived", ["false"])[0].lower() == "true"
        where = ["t.owner_user_id = %s", "t.is_closed = %s", "t.archived = %s"]
        params: list = [user["id"], closed, archived]
        if "responsible_user_id" in qs:
            where.append("t.responsible_user_id = %s")
            params.append(int(qs["responsible_user_id"][0]))
        if "opportunity_id" in qs:
            where.append("t.opportunity_id = %s")
            params.append(int(qs["opportunity_id"][0]))
        rows = db.query(
            f"""SELECT t.id, t.title, t.description, t.opportunity_id, t.responsible_user_id,
                       t.due_date, t.priority, t.is_closed, t.closed_at, t.created_at,
                       u.display_name, o.title, t.items, t.labels, t.repeat, t.color, t.pinned,
                       t.archived, t.sort_order, t.updated_at, t.owner_user_id, t.remind_at,
                       t.category_id, hc.title, hc.display_color, hc.icon_key
                FROM tasks t
                LEFT JOIN users u ON t.responsible_user_id = u.id
                LEFT JOIN opportunities o ON t.opportunity_id = o.id
                LEFT JOIN history_categories hc ON t.category_id = hc.id
                WHERE {' AND '.join(where)}
                ORDER BY t.sort_order ASC, t.due_date ASC NULLS LAST, t.created_at DESC""",
            (*params,),
        )
        _json_response(self, 200, [
            {"id": r[0], "title": r[1], "description": r[2], "opportunityId": r[3],
             "responsibleUserId": r[4], "dueDate": r[5].isoformat() if r[5] else None,
             "priority": r[6], "isClosed": r[7], "closedAt": r[8].isoformat() if r[8] else None,
             "created": r[9].isoformat() if r[9] else None,
             "responsible": {"id": r[4], "displayName": r[10]} if r[10] else None,
             "opportunity": {"title": r[11]} if r[11] else None,
             "entity": {"entityType": "opportunity", "entityId": r[3], "entityTitle": r[11]} if r[11] else None,
             "items": r[12] or [], "labels": r[13] or "", "repeat": r[14] or "none",
             "color": r[15] or "", "pinned": bool(r[16]), "archived": bool(r[17]),
             "sortOrder": r[18] or 0, "updatedAt": r[19].isoformat() if r[19] else None,
             "ownerUserId": r[20], "remindAt": r[21].isoformat() if r[21] else None,
             "categoryId": r[22],
             "category": {"id": r[22], "title": r[23], "color": r[24], "iconKey": r[25]} if r[22] else None}
            for r in rows
        ])

    def _handle_task_create(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = str(payload.get("title") or "").strip()
        if not title:
            _json_response(self, 400, {"error": "Title is required"})
            return
        tid = db.insert_returning(
            """INSERT INTO tasks (title, description, opportunity_id, responsible_user_id, due_date, priority, created_by,
                                 owner_user_id, items, labels, repeat, color, sort_order, remind_at, category_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, payload.get("description"), payload.get("opportunityId"),
             payload.get("responsibleUserId", user["id"]),
             payload.get("dueDate"), payload.get("priority", 0), user["id"],
             user["id"], json.dumps(payload.get("items") or []), payload.get("labels") or "",
             payload.get("repeat") or "none", payload.get("color") or "",
             payload.get("sortOrder", 0), payload.get("remindAt"),
             payload.get("categoryId")),
        )
        _json_response(self, 201, {"id": tid, "ok": True})
        # Publish task:created for dashboard auto-refresh
        event_bus.publish("task:created", {
            "task_id": tid, "title": title,
            "opportunity_id": payload.get("opportunityId"),
        })

    def _handle_task_update(self, task_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        owned = db.query_one(
            "SELECT id FROM tasks WHERE id = %s AND owner_user_id = %s",
            (task_id, user["id"]),
        )
        if not owned:
            _json_response(self, 404, {"error": "Task not found"})
            return
        if "isClosed" in payload:
            if payload["isClosed"]:
                db.execute(
                    "UPDATE tasks SET is_closed = TRUE, closed_at = NOW(), closed_by = %s, updated_at = NOW() WHERE id = %s",
                    (user["id"], task_id),
                )
            else:
                db.execute(
                    "UPDATE tasks SET is_closed = FALSE, closed_at = NULL, closed_by = NULL, updated_at = NOW() WHERE id = %s",
                    (task_id,),
                )
        fields = [
            ("title", "title"), ("description", "description"), ("dueDate", "due_date"),
            ("priority", "priority"), ("responsibleUserId", "responsible_user_id"),
            ("opportunityId", "opportunity_id"), ("labels", "labels"), ("repeat", "repeat"),
            ("color", "color"), ("pinned", "pinned"), ("archived", "archived"),
            ("sortOrder", "sort_order"), ("remindAt", "remind_at"), ("items", "items"),
            ("categoryId", "category_id"),
        ]
        for field, col in fields:
            if field not in payload:
                continue
            val = payload[field]
            if field == "items":
                val = json.dumps(val or [])
            if field in ("pinned", "archived"):
                val = bool(val)
            if field == "remindAt" and not val:
                val = None
            db.execute(
                f"UPDATE tasks SET {col} = %s, updated_at = NOW() WHERE id = %s",
                (val, task_id),
            )
        _json_response(self, 200, {"ok": True})
        # Publish task:updated for dashboard auto-refresh
        event_bus.publish("task:updated", {"task_id": task_id})

    def _handle_task_delete(self, task_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        task = db.query_one(
            "SELECT id, title, opportunity_id FROM tasks WHERE id = %s AND owner_user_id = %s",
            (task_id, user["id"]),
        )
        if not task:
            _json_response(self, 404, {"error": "Task not found"})
            return
        db.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        _json_response(self, 200, {"ok": True})
        # Publish task:deleted for dashboard auto-refresh
        event_bus.publish("task:deleted", {
            "task_id": task_id, "title": task.get("title", ""),
            "opportunity_id": task.get("opportunity_id"),
        })

    def _handle_tasks_reorder(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = payload.get("ids") or []
        count = 0
        for i, tid in enumerate(ids):
            try:
                n = db.execute(
                    "UPDATE tasks SET sort_order = %s, updated_at = NOW() WHERE id = %s AND owner_user_id = %s",
                    (i, int(tid), user["id"]),
                )
            except (TypeError, ValueError):
                continue
            if n:
                count += 1
        _json_response(self, 200, {"ok": True, "count": count})

    # ── Personal Notes ────────────────────────────────────────────────────────

    def _handle_notes_get(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        archived = qs.get("archived", [""])[0]
        color = qs.get("color", [""])[0]
        search_q = qs.get("q", [""])[0]
        deal_id = qs.get("deal_id", [""])[0]

        conditions = ["owner = %s"]
        params: list = [user["id"]]

        if archived == "true":
            conditions.append("archived = TRUE")
        else:
            conditions.append("archived = FALSE")

        if color:
            conditions.append("color = %s")
            params.append(color)

        if search_q:
            conditions.append(
                "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')) @@ plainto_tsquery('english', %s)"
            )
            params.append(search_q)

        where = " AND ".join(conditions)

        if deal_id:
            rows = db.query_dicts(
                f"SELECT DISTINCT pn.* FROM personal_notes pn "
                f"JOIN personal_note_links pnl ON pnl.note_id = pn.id "
                f"WHERE {where} AND pnl.entity_type = 'deal' AND pnl.entity_id = %s "
                f"ORDER BY pn.pinned DESC, pn.sort_order ASC, pn.updated_at DESC",
                (*params, str(deal_id)),
            )
        else:
            rows = db.query_dicts(
                f"SELECT * FROM personal_notes WHERE {where} "
                f"ORDER BY pinned DESC, sort_order ASC, updated_at DESC",
                tuple(params),
            )

        # Attach entity links
        note_ids = [r["id"] for r in rows]
        links_map: dict = {}
        if note_ids:
            placeholders = ",".join(["%s"] * len(note_ids))
            link_rows = db.query(
                f"SELECT note_id, entity_type, entity_id FROM personal_note_links WHERE note_id IN ({placeholders})",
                tuple(note_ids),
            )
            for lr in link_rows:
                nid = str(lr[0])
                if nid not in links_map:
                    links_map[nid] = []
                links_map[nid].append({"entityType": lr[1], "entityId": lr[2]})

        for r in rows:
            r["id"] = str(r["id"])
            r["links"] = links_map.get(r["id"], [])
            # Serialize timestamps
            for key in ("created_at", "updated_at", "archived_at"):
                if r.get(key):
                    r[key] = r[key].isoformat() if hasattr(r[key], "isoformat") else str(r[key])

        _json_response(self, 200, {"notes": rows})

    def _handle_note_create(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = (payload.get("title") or "")[:200]
        content = payload.get("content") or ""
        color = (payload.get("color") or "")[:32]
        pinned = bool(payload.get("pinned", False))

        note_id = db.insert_returning(
            "INSERT INTO personal_notes (owner, title, content, color, pinned) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user["id"], title, content, color, pinned),
        )

        _json_response(self, 201, {"id": str(note_id), "ok": True})

    def _handle_note_update(self, note_id: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        owned = db.query_one(
            "SELECT id FROM personal_notes WHERE id = %s AND owner = %s",
            (note_id, user["id"]),
        )
        if not owned:
            _json_response(self, 404, {"error": "Note not found"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return

        # Handle content version rotation separately
        if "content" in payload:
            new_content = payload["content"] or ""
            current = db.query_one(
                "SELECT content FROM personal_notes WHERE id = %s", (note_id,)
            )
            old_content = (current.get("content") or "") if current else ""
            if new_content != old_content:
                # Rotate: current → prev, prev → prev2
                db.execute(
                    "UPDATE personal_notes SET "
                    "content_prev2 = content_prev, content_prev2_at = content_prev_at, "
                    "content_prev = content, content_prev_at = NOW(), "
                    "content = %s, updated_at = NOW() "
                    "WHERE id = %s",
                    (new_content, note_id),
                )
            else:
                # Content unchanged, just update timestamp
                db.execute(
                    "UPDATE personal_notes SET updated_at = NOW() WHERE id = %s",
                    (note_id,),
                )

        fields = [
            ("title", "title"), ("color", "color"),
            ("pinned", "pinned"), ("archived", "archived"), ("sortOrder", "sort_order"),
        ]
        for field, col in fields:
            if field not in payload:
                continue
            val = payload[field]
            if field == "title":
                val = (val or "")[:200]
            if field == "color":
                val = (val or "")[:32]
            if field in ("pinned", "archived"):
                val = bool(val)
                if field == "archived" and val:
                    db.execute(
                        f"UPDATE personal_notes SET {col} = %s, archived_at = NOW(), updated_at = NOW() WHERE id = %s",
                        (val, note_id),
                    )
                    continue
                if field == "archived" and not val:
                    db.execute(
                        f"UPDATE personal_notes SET {col} = %s, archived_at = NULL, updated_at = NOW() WHERE id = %s",
                        (val, note_id),
                    )
                    continue
            db.execute(
                f"UPDATE personal_notes SET {col} = %s, updated_at = NOW() WHERE id = %s",
                (val, note_id),
            )

        _json_response(self, 200, {"ok": True})

    def _handle_note_delete(self, note_id: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        owned = db.query_one(
            "SELECT id FROM personal_notes WHERE id = %s AND owner = %s",
            (note_id, user["id"]),
        )
        if not owned:
            _json_response(self, 404, {"error": "Note not found"})
            return
        db.execute("DELETE FROM personal_notes WHERE id = %s", (note_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_notes_reorder(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = payload.get("ids") or []
        count = 0
        for i, nid in enumerate(ids):
            n = db.execute(
                "UPDATE personal_notes SET sort_order = %s, updated_at = NOW() WHERE id = %s AND owner = %s",
                (i, str(nid), user["id"]),
            )
            if n:
                count += 1
        _json_response(self, 200, {"ok": True, "count": count})

    def _handle_note_link_add(self, note_id: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        owned = db.query_one(
            "SELECT id FROM personal_notes WHERE id = %s AND owner = %s",
            (note_id, user["id"]),
        )
        if not owned:
            _json_response(self, 404, {"error": "Note not found"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        entity_type = (payload.get("entityType") or "").strip()
        entity_id = (payload.get("entityId") or "").strip()
        if not entity_type or not entity_id:
            _json_response(self, 400, {"error": "entityType and entityId required"})
            return
        try:
            db.execute(
                "INSERT INTO personal_note_links (note_id, entity_type, entity_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (note_id, entity_type, entity_id),
            )
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})
            return
        _json_response(self, 201, {"ok": True})

    def _handle_note_link_remove(self, note_id: str, entity_type: str, entity_id: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        owned = db.query_one(
            "SELECT id FROM personal_notes WHERE id = %s AND owner = %s",
            (note_id, user["id"]),
        )
        if not owned:
            _json_response(self, 404, {"error": "Note not found"})
            return
        db.execute(
            "DELETE FROM personal_note_links WHERE note_id = %s AND entity_type = %s AND entity_id = %s",
            (note_id, entity_type, entity_id),
        )
        _json_response(self, 200, {"ok": True})

    def _handle_note_versions(self, note_id: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        row = db.query_one(
            "SELECT id, content, content_prev, content_prev2, content_prev_at, content_prev2_at "
            "FROM personal_notes WHERE id = %s AND owner = %s",
            (note_id, user["id"]),
        )
        if not row:
            _json_response(self, 404, {"error": "Note not found"})
            return
        versions = [{"label": "Current", "content": row.get("content") or "", "at": None}]
        if row.get("content_prev") is not None:
            at = row["content_prev_at"].isoformat() if hasattr(row.get("content_prev_at"), "isoformat") else str(row.get("content_prev_at")) if row.get("content_prev_at") else None
            versions.append({"label": "Previous", "content": row["content_prev"] or "", "at": at})
        if row.get("content_prev2") is not None:
            at = row["content_prev2_at"].isoformat() if hasattr(row.get("content_prev2_at"), "isoformat") else str(row.get("content_prev2_at")) if row.get("content_prev2_at") else None
            versions.append({"label": "Older", "content": row["content_prev2"] or "", "at": at})
        _json_response(self, 200, {"versions": versions})

    def _handle_note_restore(self, note_id: str) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        version = payload.get("version", "")
        if version not in ("prev", "prev2"):
            _json_response(self, 400, {"error": "version must be 'prev' or 'prev2'"})
            return
        row = db.query_one(
            "SELECT id, content, content_prev, content_prev2 FROM personal_notes WHERE id = %s AND owner = %s",
            (note_id, user["id"]),
        )
        if not row:
            _json_response(self, 404, {"error": "Note not found"})
            return
        if version == "prev":
            restore_content = row.get("content_prev")
            if restore_content is None:
                _json_response(self, 400, {"error": "No previous version available"})
                return
            # Rotate: current → prev2, prev → current
            db.execute(
                "UPDATE personal_notes SET content = content_prev, content_prev2 = content, "
                "content_prev2_at = content_prev_at, content_prev_at = NOW(), updated_at = NOW() "
                "WHERE id = %s",
                (note_id,),
            )
        else:  # prev2
            restore_content = row.get("content_prev2")
            if restore_content is None:
                _json_response(self, 400, {"error": "No older version available"})
                return
            # Rotate: current → prev, prev2 → current
            db.execute(
                "UPDATE personal_notes SET content = content_prev2, content_prev = content, "
                "content_prev_at = content_prev2_at, content_prev2_at = NOW(), updated_at = NOW() "
                "WHERE id = %s",
                (note_id,),
            )
        _json_response(self, 200, {"ok": True, "content": restore_content or ""})

    # ── Users ───────────────────────────────────────────────────────────────

    def _handle_users_get(self) -> None:
        # Accept either session auth or bot token auth
        bot_token = _get_bot_token()
        auth_header = self.headers.get("Authorization", "").strip()
        if not (bot_token and auth_header == f"Bearer {bot_token}"):
            user = _require_auth(self)
            if not user:
                return
        rows = db.query(
            """SELECT id, email, display_name, first_name, last_name, is_admin, is_active,
                      avatar_url, avatar_icon, role, twofa_method, created_at, last_login, must_change_password
               FROM users ORDER BY display_name"""
        )
        _json_response(self, 200, [
            {"id": r[0], "email": r[1], "displayName": r[2], "firstName": r[3],
             "lastName": r[4], "isAdmin": r[5], "isActive": r[6],
             "avatarUrl": f"/api/v2/users/{r[0]}/avatar" if r[7] else None,
             "thumbnailUrl": f"/api/v2/users/{r[0]}/avatar?thumbnail=1" if r[7] else None,
             "avatarIcon": r[8],
             "role": r[9] or "member",
             "twofaMethod": r[10],
             "createdAt": r[11].isoformat() if r[11] else None,
             "lastLogin": r[12].isoformat() if r[12] else None,
             "mustChangePassword": r[13]}
            for r in rows
        ])

    def _handle_me_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        # Fetch avatar_url + avatar_icon from DB (not in session cache)
        avatar_row = db.query_one("SELECT avatar_url, avatar_icon FROM users WHERE id = %s", (user["id"],))
        avatar_url = avatar_row.get("avatar_url") if avatar_row else None
        avatar_icon = avatar_row.get("avatar_icon") if avatar_row else None
        _json_response(self, 200, {
            "id": user.get("id"),
            "email": user.get("email"),
            "displayName": user.get("display_name"),
            "firstName": user.get("first_name"),
            "lastName": user.get("last_name"),
            "isAdmin": user.get("is_admin", False),
            "mustChangePassword": user.get("must_change_password", False),
            "role": user.get("role", "member"),
            "twofaMethod": user.get("twofa_method"),
            "avatarUrl": f"/api/v2/my/avatar" if avatar_url else None,
            "thumbnailUrl": f"/api/v2/my/avatar?thumbnail=1" if avatar_url else None,
            "avatarIcon": avatar_icon,
        })

    def _handle_me_put(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        sets, params = [], []
        for field, col in [("displayName", "display_name"), ("firstName", "first_name"), ("lastName", "last_name")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(str(payload[field])[:100] if payload[field] is not None else None)
        if sets:
            params.append(user["id"])
            db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", tuple(params))
        _json_response(self, 200, {"ok": True})

    def _handle_avatar_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        want_thumb = (qs.get("thumbnail") or [""])[0].lower() in ("1", "true")
        row = db.query_one("SELECT avatar_url FROM users WHERE id = %s", (user["id"],))
        avatar_url = row.get("avatar_url") if row else None
        if not avatar_url:
            self.send_error(404)
            return
        # Containment: avatar_url is server-written today, but resolve +
        # pin it under AVATAR_STORAGE_PATH anyway so one tainted row
        # can't turn this into an arbitrary file read.
        try:
            avatar_path = (ROOT / avatar_url).resolve()
            base = AVATAR_STORAGE_PATH.resolve()
            if base not in avatar_path.parents:
                self.send_error(404)
                return
        except Exception:
            self.send_error(404)
            return
        if want_thumb:
            thumb_path = avatar_path.parent / f"thumb_{avatar_path.name}"
            if thumb_path.exists():
                avatar_path = thumb_path
        if not avatar_path.exists():
            self.send_error(404)
            return
        data = avatar_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _handle_user_avatar_get(self, target_user_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        want_thumb = (qs.get("thumbnail") or [""])[0].lower() in ("1", "true")
        row = db.query_one("SELECT avatar_url FROM users WHERE id = %s", (target_user_id,))
        avatar_url = row.get("avatar_url") if row else None
        if not avatar_url:
            self.send_error(404)
            return
        # Same containment as _handle_avatar_get above.
        try:
            avatar_path = (ROOT / avatar_url).resolve()
            base = AVATAR_STORAGE_PATH.resolve()
            if base not in avatar_path.parents:
                self.send_error(404)
                return
        except Exception:
            self.send_error(404)
            return
        if want_thumb:
            thumb_path = avatar_path.parent / f"thumb_{avatar_path.name}"
            if thumb_path.exists():
                avatar_path = thumb_path
        if not avatar_path.exists():
            self.send_error(404)
            return
        data = avatar_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _handle_avatar_upload(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        if not file_info:
            _json_response(self, 400, {"error": "file field required"})
            return
        data = file_info["data"]
        if len(data) > 5 * 1024 * 1024:
            _json_response(self, 400, {"error": "File too large (max 5 MB)"})
            return
        mime_type = file_info.get("content-type") or ""
        if not mime_type.startswith("image/"):
            _json_response(self, 400, {"error": "Only image files are supported"})
            return
        user_dir = AVATAR_STORAGE_PATH / str(user["id"])
        user_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = user_dir / "avatar.jpg"
        thumb_path = user_dir / "thumb_avatar.jpg"
        if Image:
            try:
                with Image.open(io.BytesIO(data)) as img:
                    if img.mode in ("RGBA", "P"):
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                    # Save full-size (max 400x400)
                    full = img.copy()
                    full.thumbnail((400, 400), Image.Resampling.LANCZOS)
                    full.save(avatar_path, "JPEG", quality=92)
                    # Thumbnail 96x96
                    thumb = img.copy()
                    thumb.thumbnail((96, 96), Image.Resampling.LANCZOS)
                    thumb.save(thumb_path, "JPEG", quality=88)
            except Exception as exc:
                _json_response(self, 400, {"error": f"Failed to process image: {exc}"})
                return
        else:
            avatar_path.write_bytes(data)
            thumb_path = None
        rel_path = str(avatar_path.relative_to(ROOT))
        db.execute("UPDATE users SET avatar_url = %s WHERE id = %s", (rel_path, user["id"]))
        _json_response(self, 200, {
            "ok": True,
            "avatarUrl": "/api/v2/my/avatar",
            "thumbnailUrl": "/api/v2/my/avatar?thumbnail=1",
        })

    def _handle_avatar_delete(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        row = db.query_one("SELECT avatar_url FROM users WHERE id = %s", (user["id"],))
        avatar_url = row.get("avatar_url") if row else None
        if avatar_url:
            old_path = ROOT / avatar_url
            try:
                old_path.unlink(missing_ok=True)
                thumb = old_path.parent / f"thumb_{old_path.name}"
                thumb.unlink(missing_ok=True)
            except Exception:
                pass
        db.execute("UPDATE users SET avatar_url = NULL WHERE id = %s", (user["id"],))
        _json_response(self, 200, {"ok": True})

    def _handle_notification_prefs_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            "SELECT notification_type, enabled FROM notification_preferences WHERE user_id = %s",
            (user["id"],),
        )
        prefs = {r[0]: r[1] for r in rows}
        email_val = prefs.get("email_digest")
        if email_val is True:
            email_digest = "immediate"
        elif email_val is False:
            email_digest = "disabled"
        else:
            email_digest = "disabled"
        result = {
            "inDashboard": prefs.get("in_dashboard", True),
            "telegram": prefs.get("telegram", True),
            "emailDigest": email_digest,
        }
        # Per-type Telegram prefs
        for ntype in ("note_tagged", "task_assigned", "task_reminder", "calendar_reminder"):
            key = f"telegram_{ntype}"
            val = prefs.get(key)
            result[f"tg_{ntype}"] = val if val is not None else True
        _json_response(self, 200, result)

    def _handle_notification_prefs_put(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        for key, json_key in [("in_dashboard", "inDashboard"), ("telegram", "telegram")]:
            if json_key in payload:
                val = bool(payload[json_key])
                db.execute(
                    """INSERT INTO notification_preferences (user_id, notification_type, enabled)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, notification_type) DO UPDATE SET enabled = %s""",
                    (user["id"], key, val, val),
                )
        if "emailDigest" in payload:
            val = str(payload["emailDigest"])[:20]
            enabled = val != "disabled"
            db.execute(
                """INSERT INTO notification_preferences (user_id, notification_type, enabled)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, notification_type) DO UPDATE SET enabled = %s""",
                (user["id"], "email_digest", enabled, enabled),
            )
        # Per-type Telegram prefs
        for ntype in ("note_tagged", "task_assigned", "task_reminder", "calendar_reminder"):
            json_key = f"tg_{ntype}"
            if json_key in payload:
                val = bool(payload[json_key])
                db.execute(
                    """INSERT INTO notification_preferences (user_id, notification_type, enabled)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, notification_type) DO UPDATE SET enabled = %s""",
                    (user["id"], f"telegram_{ntype}", val, val),
                )
        _json_response(self, 200, {"ok": True})

    def _handle_user_create(self) -> None:
        admin = _require_admin(self)
        if not admin:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            _json_response(self, 400, {"error": "Email is required"})
            return
        # users.email is UNIQUE. Without this check a duplicate goes
        # straight to INSERT and surfaces as a generic 500 (raw
        # UniqueViolation via _run_api_handler). The update handler
        # already returns 409 for this; create now matches.
        dup = db.query_one("SELECT id FROM users WHERE email = %s", (email,))
        if dup:
            _json_response(self, 409, {"error": "Email already in use"})
            return
        role = payload.get("role", "member")
        if role not in ("admin", "manager", "member", "limited"):
            role = "member"
        temp_password = str(payload.get("password") or "changeme")
        pw_hash, salt = auth_mod.hash_password(temp_password)
        is_admin = role in ("admin", "manager")
        uid = db.insert_returning(
            """INSERT INTO users (email, password_hash, password_salt, display_name, first_name, last_name, is_admin, must_change_password, role)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id""",
            (email, pw_hash, salt, payload.get("displayName"), payload.get("firstName"),
             payload.get("lastName"), is_admin, role),
        )
        # Set default notification preferences for new user
        db.execute(
            "INSERT INTO notification_preferences (user_id, notification_type, enabled) VALUES (%s, 'email_digest', TRUE)",
            (uid,),
        )
        _json_response(self, 201, {"id": uid, "ok": True})

    def _handle_user_update(self, user_id: int) -> None:
        admin = _require_admin(self)
        if not admin:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return

        # ── Hardened guards ──
        # Self-demotion block: admin cannot remove their own admin status
        if user_id == admin["id"]:
            if "isAdmin" in payload and not payload["isAdmin"]:
                _json_response(self, 409, {"error": "You cannot remove your own admin privileges"})
                return
            if "role" in payload and payload["role"] not in ("admin", "manager"):
                _json_response(self, 409, {"error": "You cannot remove your own admin privileges"})
                return

        # Self-deactivation block: admin cannot deactivate themselves
        if user_id == admin["id"] and "isActive" in payload and not payload["isActive"]:
            _json_response(self, 409, {"error": "You cannot deactivate your own account"})
            return

        # Build update sets
        # NOTE: isAdmin/role are handled explicitly below rather than in this
        # generic loop, because they must always move together.
        sets, params = [], []
        for field, col in [("email", "email"), ("displayName", "display_name"), ("firstName", "first_name"),
                           ("lastName", "last_name"), ("isActive", "is_active"),
                           ("twofaMethod", "twofa_method")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])

        # Email uniqueness check
        if "email" in payload and payload["email"]:
            new_email = payload["email"].lower().strip()
            dup = db.query_one("SELECT id FROM users WHERE email = %s AND id != %s", (new_email, user_id))
            if dup:
                _json_response(self, 409, {"error": "Email already in use"})
                return

        # `role` is authoritative for admin-ness; is_admin is always derived from
        # it. Writing is_admin on its own (which the generic loop used to allow
        # whenever a payload carried isAdmin but no role) could leave the two
        # columns disagreeing -- e.g. is_admin=false with role='admin'. That
        # inconsistent row is what produced a half-admin account: full admin
        # console (is_admin gate) but 403 on role-gated endpoints.
        role_write: str | None = None
        if "role" in payload:
            role_write = payload["role"]
            if role_write not in ("admin", "manager", "member", "limited"):
                _json_response(self, 400, {"error": "Invalid role"})
                return
        elif "isAdmin" in payload:
            # Legacy callers may still send isAdmin alone. Honor it, but move
            # role with it so the columns can't drift.
            row = db.query_one("SELECT role FROM users WHERE id = %s", (user_id,))
            current_role = (row or {}).get("role") or "member"
            if payload["isAdmin"]:
                if current_role not in ("admin", "manager"):
                    role_write = "admin"
            elif current_role in ("admin", "manager"):
                role_write = "member"

        if role_write is not None:
            sets.append("role = %s")
            params.append(role_write)
            sets.append("is_admin = %s")
            params.append(role_write in ("admin", "manager"))

        if "password" in payload and payload["password"]:
            pw_hash, salt = auth_mod.hash_password(payload["password"])
            sets.extend(["password_hash = %s", "password_salt = %s", "must_change_password = FALSE"])
            params.extend([pw_hash, salt])

        if sets:
            params.append(user_id)
            db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", (*params,))

        # Last-admin guard: ensure at least one admin remains
        if "isAdmin" in payload and not payload["isAdmin"] or \
           "role" in payload and payload["role"] not in ("admin", "manager"):
            admin_count = db.query_one("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'manager')")["count"]
            if admin_count == 0:
                # Rollback: restore admin
                db.execute(
                    "UPDATE users SET is_admin = TRUE, role = 'admin' WHERE id = %s",
                    (user_id,),
                )
                _json_response(self, 409, {"error": "Cannot remove the last admin"})
                return

        _json_response(self, 200, {"ok": True})

    def _handle_user_reset_password(self, user_id: int) -> None:
        admin = _require_admin(self)
        if not admin:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        method = str(payload.get("method") or "").strip()
        if method == "temp":
            password = str(payload.get("password") or "")
            if not password:
                import secrets as _secrets
                password = _secrets.token_urlsafe(9)[:12]
            pw_hash, salt = auth_mod.hash_password(password)
            db.execute(
                "UPDATE users SET password_hash = %s, password_salt = %s, must_change_password = TRUE WHERE id = %s",
                (pw_hash, salt, user_id),
            )
            auth_mod.destroy_all_user_sessions(user_id)
            _json_response(self, 200, {"ok": True, "tempPassword": password})
        elif method == "invite":
            row = db.query_one("SELECT email FROM users WHERE id = %s", (user_id,))
            if not row:
                _json_response(self, 404, {"error": "User not found"})
                return
            smtp_acct = _get_system_smtp_account()
            if not smtp_acct:
                _json_response(self, 500, {"error": "No CRM mail account configured. Add one in Admin → CRM Mail."})
                return
            token = auth_mod.create_reset_token(user_id)
            from smtp_client import send_password_reset_email
            reset_url = f"{CRM_PUBLIC_URL}/reset?token={token}"
            sent = send_password_reset_email(row["email"], reset_url, smtp_config=smtp_acct)
            if not sent:
                _json_response(self, 500, {"error": "Failed to send password reset email. Check CRM mail account SMTP settings."})
                return
            _json_response(self, 200, {"ok": True})
        else:
            _json_response(self, 400, {"error": "method must be 'temp' or 'invite'"})

    def _handle_user_reset_password_send(self, user_id: int) -> None:
        """Send temp password to user via email or Telegram."""
        admin = _require_admin(self)
        if not admin:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        method = str(payload.get("method") or "").strip()
        password = str(payload.get("password") or "")
        if not password:
            _json_response(self, 400, {"error": "password is required"})
            return
        if method == "email":
            row = db.query_one("SELECT email FROM users WHERE id = %s", (user_id,))
            if not row:
                _json_response(self, 404, {"error": "User not found"})
                return
            smtp_acct = _get_system_smtp_account()
            if not smtp_acct:
                _json_response(self, 500, {"error": "No CRM mail account configured"})
                return
            from smtp_client import send_email_from_account
            use_oauth = bool(smtp_acct.get("oauth_provider") and smtp_acct.get("oauth_access_token")
                             and not smtp_acct.get("smtp_password_encrypted"))
            ok, err = send_email_from_account(
                smtp_host=smtp_acct.get("smtp_host") or "",
                smtp_port=int(smtp_acct.get("smtp_port") or 587),
                smtp_user=smtp_acct.get("smtp_user") or smtp_acct.get("email", ""),
                smtp_password=smtp_acct.get("smtp_password_encrypted") or "",
                from_name="Sietch CRM",
                from_addr=smtp_acct.get("email", ""),
                to_addr=row["email"],
                subject="Your Sietch CRM Temporary Password",
                html_body=f"<p>Your temporary password is: <b>{password}</b></p><p>You will be required to change it on first login.</p>",
                oauth_provider=smtp_acct["oauth_provider"] if use_oauth else None,
                oauth_access_token=smtp_acct["oauth_access_token"] if use_oauth else None,
            )
            if ok:
                _json_response(self, 200, {"ok": True})
            else:
                _json_response(self, 500, {"error": f"Failed to send email: {err}"})
        elif method == "telegram":
            from notification_dispatcher import _get_employee_chat_id, _send_telegram_message
            chat_id = _get_employee_chat_id(user_id)
            if not chat_id:
                _json_response(self, 400, {"error": "User has no linked Telegram account"})
                return
            _send_telegram_message(chat_id, f"Your Sietch CRM temporary password: <b>{password}</b>\nYou will be required to change it on first login.")
            _json_response(self, 200, {"ok": True})
        else:
            _json_response(self, 400, {"error": "method must be 'email' or 'telegram'"})

    def _handle_user_sessions_get(self, user_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        if user["id"] != user_id and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Forbidden"})
            return
        rows = db.query(
            "SELECT token, created_at, expires_at, ip_address FROM sessions WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        _json_response(self, 200, [
            {"token": r[0][:8] + "...", "createdAt": r[1].isoformat() if r[1] else None,
             "expiresAt": r[2].isoformat() if r[2] else None, "ipAddress": r[3]}
            for r in rows
        ])

    def _handle_user_sessions_delete(self, user_id: int) -> None:
        admin = _require_admin(self)
        if not admin:
            return
        count = db.query_one("SELECT COUNT(*) FROM sessions WHERE user_id = %s", (user_id,))
        auth_mod.destroy_all_user_sessions(user_id)
        _json_response(self, 200, {"ok": True, "revoked": count["count"] if count else 0})

    def _handle_user_reactivate(self, user_id: int) -> None:
        admin = _require_admin(self)
        if not admin:
            return
        db.execute("UPDATE users SET is_active = TRUE WHERE id = %s", (user_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_user_delete(self, user_id: int) -> None:
        admin = _require_admin(self)
        if not admin:
            return
        if user_id == admin["id"]:
            _json_response(self, 409, {"error": "You cannot delete your own account"})
            return
        db.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (user_id,))
        auth_mod.destroy_all_user_sessions(user_id)
        _json_response(self, 200, {"ok": True})

    def _handle_user_get(self, user_id: int) -> None:
        # GET /api/v2/users/{id} — admin (or the user themselves) fetches a
        # full user record for the admin user-profile modal. Returns the
        # public-facing fields only (no password hash, no twofa secret).
        user = _require_auth(self)
        if not user:
            return
        if not (user.get("is_admin") or int(user["id"]) == int(user_id)):
            _json_response(self, 403, {"error": "Forbidden"})
            return
        row = db.query_one(
            "SELECT id, email, display_name, first_name, last_name, is_admin, "
            "is_active, avatar_url, avatar_icon, role, must_change_password, "
            "created_at, last_login "
            "FROM users WHERE id = %s",
            (user_id,),
        )
        if not row:
            _json_response(self, 404, {"error": "User not found"})
            return
        _json_response(self, 200, {
            "id": row["id"],
            "email": row["email"] or "",
            "displayName": row["display_name"] or "",
            "firstName": row["first_name"] or "",
            "lastName": row["last_name"] or "",
            "isAdmin": bool(row["is_admin"]),
            "isActive": bool(row["is_active"]),
            "avatarUrl": row["avatar_url"] or "",
            "avatarIcon": row["avatar_icon"] or "",
            "role": row["role"] or "member",
            "mustChangePassword": bool(row["must_change_password"]),
            "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
            "lastLogin": row["last_login"].isoformat() if row["last_login"] else None,
        })

    def _handle_user_delete_personal_data(self, user_id: int) -> None:
        # POST /api/v2/users/{id}/delete-personal-data — admin action that
        # clears the user's PII + sessions + presence + notification prefs +
        # avatar file, and deactivates the account. The user row itself
        # (and its id) is preserved so existing references in history_events,
        # tasks, mail, etc. continue to work — the foreign keys remain valid.
        # Admin can later reset their password (sends a reset link) and
        # reactivate the account.
        admin = _require_admin(self)
        if not admin:
            return
        if int(user_id) == int(admin["id"]):
            _json_response(self, 409, {"error": "You cannot delete your own personal data"})
            return
        # Read the avatar path BEFORE the transaction so we can unlink the
        # file after the DB write commits.
        avatar_row = db.query_one("SELECT avatar_url, email FROM users WHERE id = %s", (user_id,))
        if not avatar_row:
            _json_response(self, 404, {"error": "User not found"})
            return
        avatar_path = (avatar_row.get("avatar_url") if avatar_row else None) or ""
        original_email = (avatar_row.get("email") if avatar_row else None) or ""
        placeholder_email = f"deleted-{user_id}@removed.local"

        try:
            with db.db_cursor() as cur:
                # 1. Wipe PII on the user row. The id stays. We set a
                #    placeholder email because the column is UNIQUE NOT NULL.
                cur.execute(
                    "UPDATE users SET "
                    "email = %s, "
                    "password_hash = NULL, password_salt = NULL, "
                    "first_name = NULL, last_name = NULL, display_name = NULL, "
                    "avatar_url = NULL, avatar_icon = NULL, "
                    "must_change_password = TRUE, "
                    "is_active = FALSE "
                    "WHERE id = %s",
                    (placeholder_email, user_id),
                )
                # 2. Revoke every session for this user.
                cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
                # 3. Wipe notification preferences + any 2FA rows.
                cur.execute("DELETE FROM notification_preferences WHERE user_id = %s", (user_id,))
        except Exception as exc:
            print(f"[ERROR] delete-personal-data failed for user {user_id}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            _json_response(self, 500, {"error": f"Failed to clear personal data: {exc}"})
            return

        # 4. Unlink the on-disk avatar file (best-effort, after commit).
        files_removed = []
        if avatar_path:
            try:
                rel = avatar_path
                # Stored value is a relative path like "data/avatars/123/avatar.jpg".
                abs_path = (ROOT / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
                base = AVATAR_STORAGE_PATH.resolve()
                # Only unlink files inside AVATAR_STORAGE_PATH (path-traversal guard).
                if base in abs_path.resolve().parents and abs_path.exists():
                    abs_path.unlink()
                    files_removed.append(str(abs_path))
                    thumb = abs_path.parent / f"thumb_{abs_path.name}"
                    if thumb.exists():
                        thumb.unlink()
                        files_removed.append(str(thumb))
            except Exception as exc:
                print(f"[WARN] failed to unlink avatar file {avatar_path} for user {user_id}: {exc}", file=sys.stderr)

        # 5. Wipe per-user presence file (best-effort).
        try:
            from presence_store import presence_file_path
            pres_path = presence_file_path("sietch", str(user_id))
            if pres_path.exists():
                pres_path.unlink()
        except Exception as exc:
            print(f"[WARN] failed to unlink presence file for user {user_id}: {exc}", file=sys.stderr)

        # 6. Print a summary line to stderr (no new audit_log table for v1).
        print(
            f"[AUDIT] delete-personal-data admin_id={admin['id']} target_user_id={user_id} "
            f"original_email={original_email!r} files_removed={files_removed}",
            file=sys.stderr,
        )

        _json_response(self, 200, {
            "ok": True,
            "userId": int(user_id),
            "filesRemoved": files_removed,
        })

    # ── Notifications ───────────────────────────────────────────────────────

    def _handle_notifications_get(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        unread_only = qs.get("unread", ["false"])[0].lower() == "true"
        where = ["n.user_id = %s"]
        params: list = [user["id"]]
        if unread_only:
            where.append("n.is_read = FALSE")
        rows = db.query(
            f"""SELECT n.id, n.type, n.opportunity_id, n.message, n.payload, n.is_read, n.created_at,
                       u.display_name, o.title
                FROM notifications n
                LEFT JOIN users u ON n.actor_user_id = u.id
                LEFT JOIN opportunities o ON n.opportunity_id = o.id
                WHERE {' AND '.join(where)}
                ORDER BY n.created_at DESC LIMIT 100""",
            (*params,),
        )
        _json_response(self, 200, [
            {"id": r[0], "type": r[1], "opportunityId": r[2], "message": r[3],
             "payload": r[4], "isRead": r[5], "created": r[6].isoformat() if r[6] else None,
             "actor": r[7], "projectTitle": r[8],
             "snippet": (r[4] or {}).get("snippet", "") if isinstance(r[4], dict) else ""}
            for r in rows
        ])

    def _handle_notifications_unread_count(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND is_read = FALSE",
            (user["id"],),
        )
        count = rows[0][0] if rows else 0
        _json_response(self, 200, {"count": count})

    def _handle_notification_mark_read(self, notif_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute(
            "UPDATE notifications SET is_read = TRUE WHERE id = %s AND user_id = %s",
            (notif_id, user["id"]),
        )
        _json_response(self, 200, {"ok": True})

    def _handle_notifications_mark_all_read(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE", (user["id"],))
        _json_response(self, 200, {"ok": True})

    # ── History Categories ──────────────────────────────────────────────────

    def _handle_history_categories_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            "SELECT id, title, display_color, icon_key, icon_style, is_system, sort_order FROM history_categories ORDER BY sort_order, id"
        )
        _json_response(self, 200, [
            {"id": r[0], "title": r[1], "color": r[2], "iconKey": r[3], "iconStyle": r[4], "isSystem": r[5], "sortOrder": r[6]}
            for r in rows
        ])

    # ── Photos ──────────────────────────────────────────────────────────────

    def _handle_project_photos_get(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT id, filename, file_path, file_size, mime_type, exif_data,
                      thumbnail_path, alt_description, uploaded_by, uploaded_at
               FROM project_photos WHERE opportunity_id = %s AND is_deleted = FALSE
               ORDER BY uploaded_at DESC""",
            (opp_id,),
        )
        photos = []
        for r in rows:
            photo_id = r[0]
            photos.append({
                "id": photo_id, "filename": r[1], "filePath": r[2],
                "fileSize": r[3], "mimeType": r[4], "exifData": r[5],
                "thumbnailPath": r[6], "altDescription": r[7],
                "uploadedBy": r[8], "uploadedAt": r[9].isoformat() if r[9] else None,
                "url": f"/api/v2/photos/{photo_id}",
                "thumbnailUrl": f"/api/v2/photos/{photo_id}?thumbnail=1",
            })
        # Total size for quota display
        total = db.query(
            "SELECT COALESCE(SUM(file_size), 0)::bigint FROM project_photos WHERE opportunity_id = %s AND is_deleted = FALSE",
            (opp_id,), fetch="one",
        )
        _json_response(self, 200, {"photos": photos, "totalSize": int(total[0]) if total else 0, "quota": 157286400})

    def _handle_project_photo_upload(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        if not file_info:
            _json_response(self, 400, {"error": "file field required"})
            return
        filename = file_info["filename"]
        data = file_info["data"]
        mime_type = file_info.get("content-type") or _guess_mime(filename)
        if not mime_type.startswith("image/"):
            _json_response(self, 400, {"error": "Only image files are supported"})
            return
        safe_name = re.sub(r'[^\w.\-]', '_', filename)
        if not safe_name:
            safe_name = "image.jpg"

        # Deduplication: compute hash and check for existing photo
        import hashlib
        content_hash = hashlib.md5(data).hexdigest()
        existing = db.query_one(
            "SELECT id FROM project_photos WHERE opportunity_id = %s AND content_hash = %s AND is_deleted = FALSE",
            (opp_id, content_hash),
        )
        if existing:
            photo_id = existing["id"]
            row = db.query_one("SELECT id, filename, mime_type FROM project_photos WHERE id = %s", (photo_id,))
            _json_response(self, 200, {
                "id": photo_id, "filename": row["filename"], "fileSize": len(data),
                "mimeType": row["mime_type"],
                "url": f"/api/v2/photos/{photo_id}",
                "thumbnailUrl": f"/api/v2/photos/{photo_id}?thumbnail=1",
                "uploadedBy": user["id"],
                "duplicate": True,
            })
            return

        opp_dir = PHOTO_STORAGE_PATH / str(opp_id)
        opp_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{int(time.time())}_{safe_name}"
        file_path = opp_dir / unique_name
        thumb_name = f"thumb_{unique_name}"
        thumb_path = opp_dir / thumb_name
        file_path.write_bytes(data)
        file_size = len(data)
        exif_data: dict | None = None
        # Generate thumbnail and extract EXIF using Pillow
        if Image:
            try:
                with Image.open(io.BytesIO(data)) as img:
                    exif_data = _extract_exif(img)
                    img.thumbnail((400, 400), Image.Resampling.LANCZOS)
                    # Save thumbnail as JPEG for consistency (preserve transparency as white bg)
                    if img.mode in ("RGBA", "P"):
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(thumb_path, "JPEG", quality=85)
            except Exception as exc:
                logger.warning("Photo thumbnail/EXIF failed for %s: %s", filename, exc)
                thumb_path = None
        else:
            thumb_path = None
        try:
            photo_id = db.insert_returning(
                """INSERT INTO project_photos (opportunity_id, filename, file_path, file_size, mime_type, exif_data, thumbnail_path, uploaded_by, content_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (opp_id, filename, str(file_path.relative_to(ROOT)), file_size, mime_type,
                 json.dumps(exif_data) if exif_data else None,
                 str(thumb_path.relative_to(ROOT)) if thumb_path else None,
                 user["id"], content_hash),
            )
        except Exception as exc:
            # DB trigger may reject quota; clean up saved files
            try:
                file_path.unlink(missing_ok=True)
                if thumb_path:
                    thumb_path.unlink(missing_ok=True)
            except Exception:
                pass
            log_infra_event("error", f"Photo upload DB insert failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": f"Upload failed: {exc}"})
            return
        _json_response(self, 201, {
            "id": photo_id, "filename": filename, "fileSize": file_size,
            "mimeType": mime_type, "exifData": exif_data,
            "url": f"/api/v2/photos/{photo_id}",
            "thumbnailUrl": f"/api/v2/photos/{photo_id}?thumbnail=1",
            "uploadedBy": user["id"], "uploadedAt": datetime.now(timezone.utc).isoformat(),
        })

    def _handle_project_photo_markup(self, opp_id: int) -> None:
        """Accept an annotated (marked-up) photo from the canvas editor and save as a new photo."""
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        if not file_info:
            _json_response(self, 400, {"error": "file field required"})
            return
        data = file_info["data"]
        mime_type = file_info.get("content-type") or "image/png"
        if not mime_type.startswith("image/"):
            _json_response(self, 400, {"error": "Only image files are supported"})
            return
        source_id = (parsed.get("source_photo_id") or {}).get("data", b"").decode("utf-8", errors="ignore").strip()
        source_name = "markup"
        if source_id:
            try:
                src_row = db.query_one("SELECT filename FROM project_photos WHERE id = %s", (int(source_id),))
                if src_row:
                    base = src_row["filename"]
                    source_name = base.rsplit(".", 1)[0] if "." in base else base
            except Exception:
                pass
        safe_name = re.sub(r'[^\w.\-]', '_', source_name)
        ts = int(time.time())
        filename = f"{safe_name}_markup_{ts}.png"
        opp_dir = PHOTO_STORAGE_PATH / str(opp_id)
        opp_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{ts}_markup_{safe_name}.png"
        file_path = opp_dir / unique_name
        thumb_name = f"thumb_{unique_name}"
        thumb_path = opp_dir / thumb_name
        file_path.write_bytes(data)
        file_size = len(data)
        thumb_path_out = None
        if Image:
            try:
                with Image.open(io.BytesIO(data)) as img:
                    img.thumbnail((400, 400), Image.Resampling.LANCZOS)
                    if img.mode in ("RGBA", "P"):
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(thumb_path, "JPEG", quality=85)
                    thumb_path_out = thumb_path
            except Exception as exc:
                logger.warning("Markup photo thumbnail failed: %s", exc)
        try:
            photo_id = db.insert_returning(
                """INSERT INTO project_photos (opportunity_id, filename, file_path, file_size, mime_type, thumbnail_path, uploaded_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (opp_id, filename, str(file_path.relative_to(ROOT)), file_size, "image/png",
                 str(thumb_path_out.relative_to(ROOT)) if thumb_path_out else None, user["id"]),
            )
        except Exception as exc:
            try:
                file_path.unlink(missing_ok=True)
                if thumb_path_out:
                    thumb_path_out.unlink(missing_ok=True)
            except Exception:
                pass
            log_infra_event("error", f"Markup photo DB insert failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": f"Upload failed: {exc}"})
            return
        _json_response(self, 201, {
            "id": photo_id, "filename": filename, "fileSize": file_size,
            "mimeType": "image/png",
            "url": f"/api/v2/photos/{photo_id}",
            "thumbnailUrl": f"/api/v2/photos/{photo_id}?thumbnail=1",
            "uploadedBy": user["id"], "uploadedAt": datetime.now(timezone.utc).isoformat(),
        })

    # ── Photo Report Templates (admin) ───────────────────────────────────

    def _handle_photo_report_templates_get(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        rows = db.query_dicts("SELECT * FROM photo_report_templates ORDER BY is_default DESC, name")
        for r in rows:
            for k in ("created_at", "updated_at"):
                if r.get(k):
                    r[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else str(r[k])
        _json_response(self, 200, rows)

    def _handle_logo_serve(self, requested_path: str) -> None:
        """Stream a logo / signature file from data/logos/ or data/sigs/.

        The admin previews reference logos via /data/logos/... and
        /data/sigs/... (a path-style stored in the DB). Those paths are
        NOT under public/, so the static server can't serve them. This
        proxy bridges that: admin-gated, strict allowlist, no path
        traversal, no arbitrary content-type.

        Only two top-level directories are reachable: data/logos/ and
        data/sigs/. Anything else (a "secret" path that tries to escape,
        an absolute path, a symlink that resolves outside) gets 400.
        """
        user = _require_admin(self)
        if not user:
            return
        # Strip any leading slashes; reject anything that's not a relative
        # path or contains null bytes / traversal.
        requested = (requested_path or "").lstrip("/")
        if not requested or "\x00" in requested:
            _json_response(self, 400, {"error": "Invalid logo path"})
            return
        # Normalise: the stored paths are "data/logos/..." or "data/sigs/...".
        # Allow the caller to pass either the full "data/logos/..." or just
        # "logos/..." (the latter is what an <img src="/api/v2/admin/logos/logos/foo">
        # request would carry).
        if requested.startswith("data/"):
            rel = requested[len("data/"):]
        else:
            rel = requested
        # Restrict to the two top-level dirs the upload handlers write to.
        if not (rel.startswith("logos/") or rel.startswith("sigs/")):
            _json_response(self, 400, {"error": "Logo path must be under data/logos/ or data/sigs/"})
            return
        # Resolve and ensure the final path is still under the allowed root
        # (defeats "../" traversal and any symlink pointing elsewhere). The
        # stored paths are "data/logos/..." or "data/sigs/..." - the "data/"
        # prefix was already stripped into `rel`, so we reattach it here.
        allowed_root = (ROOT / "data").resolve()
        candidate = (ROOT / "data" / rel).resolve()
        try:
            candidate.relative_to(allowed_root)
        except ValueError:
            _json_response(self, 400, {"error": "Logo path escapes data/"})
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        # Strict content-type allowlist based on extension. SVGs are rejected
        # even though PIL could read them - SVG can carry <script> and
        # <foreignObject>, and a "logo.png" that's actually an SVG would
        # still bypass the browser's image-only Content-Type sniff. The
        # upload handler also rejects these extensions.
        ext = candidate.suffix.lower()
        ctype_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        ctype = ctype_map.get(ext)
        if ctype is None:
            _json_response(self, 415, {"error": f"Unsupported image type: {ext}"})
            return
        try:
            data = candidate.read_bytes()
        except OSError:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Cache aggressively - logos are immutable for a given template id.
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _handle_photo_report_template_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not data or not data.get("name"):
            _json_response(self, 400, {"error": "name is required"})
            return
        if data.get("is_default"):
            db.execute("UPDATE photo_report_templates SET is_default = FALSE")
        row = db.query_one(
            """INSERT INTO photo_report_templates
               (name, company_name, address, city_state_zip, phone, primary_color,
                text_logo_lines, text_logo_font_size, text_logo_tagline_size,
                max_image_width_inches, image_alignment, show_photo_date,
                title_includes_claim, show_captions, is_default)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (data["name"], data.get("company_name", ""), data.get("address", ""),
             data.get("city_state_zip", ""), data.get("phone", ""),
             data.get("primary_color", "#1a1a1a"),
             json.dumps(data.get("text_logo_lines", ["Company"])),
             data.get("text_logo_font_size", 16), data.get("text_logo_tagline_size", 8),
             data.get("max_image_width_inches", 6.5), data.get("image_alignment", "center"),
             data.get("show_photo_date", True), data.get("title_includes_claim", True),
             data.get("show_captions", True), data.get("is_default", False)),
        )
        _json_response(self, 201, {"id": row["id"]})

    def _handle_photo_report_template_update(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not data:
            _json_response(self, 400, {"error": "No data"})
            return
        existing = db.query_one("SELECT id, is_default FROM photo_report_templates WHERE id = %s", (template_id,))
        if not existing:
            _json_response(self, 404, {"error": "Template not found"})
            return
        if data.get("is_default") and not existing["is_default"]:
            db.execute("UPDATE photo_report_templates SET is_default = FALSE")
        fields = []
        params = []
        for key in ("name", "company_name", "address", "city_state_zip", "phone", "primary_color",
                     "text_logo_font_size", "text_logo_tagline_size", "max_image_width_inches",
                     "image_alignment", "show_photo_date", "title_includes_claim", "show_captions", "is_default"):
            if key in data:
                fields.append(f"{key} = %s")
                params.append(data[key])
        if "text_logo_lines" in data:
            fields.append("text_logo_lines = %s")
            params.append(json.dumps(data["text_logo_lines"]))
        if fields:
            fields.append("updated_at = NOW()")
            params.append(template_id)
            db.execute(f"UPDATE photo_report_templates SET {', '.join(fields)} WHERE id = %s", tuple(params))
        _json_response(self, 200, {"ok": True})

    def _handle_photo_report_template_delete(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        existing = db.query_one("SELECT id, is_default FROM photo_report_templates WHERE id = %s", (template_id,))
        if not existing:
            _json_response(self, 404, {"error": "Template not found"})
            return
        if existing["is_default"]:
            _json_response(self, 400, {"error": "Cannot delete the default template"})
            return
        db.execute("DELETE FROM photo_report_templates WHERE id = %s", (template_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_photo_report_template_logo_upload(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        data, err = _validate_logo_upload(file_info)
        if err is not None:
            _json_response(self, 400, {"error": err})
            return
        filename = file_info.get("filename") or ""
        safe_name = _safe_logo_filename(filename)
        logos_dir = ROOT / "data" / "logos"
        logos_dir.mkdir(parents=True, exist_ok=True)
        # Delete the previous logo file for this template, if any. The
        # signature upload handler already does this; the logo handler
        # didn't, which left orphans in data/logos/ on every re-upload.
        prev = db.query_one(
            "SELECT logo_path FROM photo_report_templates WHERE id = %s",
            (template_id,),
        )
        new_path = logos_dir / f"template_{template_id}_{safe_name}"
        if prev and prev.get("logo_path"):
            try:
                _old = ROOT / prev["logo_path"]
                if _old.exists() and _old != new_path:
                    _old.unlink()
            except OSError:
                pass
        new_path.write_bytes(data)
        rel = str(new_path.relative_to(ROOT))
        db.execute("UPDATE photo_report_templates SET logo_path = %s, updated_at = NOW() WHERE id = %s",
                   (rel, template_id))
        _json_response(self, 200, {"ok": True, "logoPath": rel})

    # ── Document Templates (admin) ───────────────────────────────────────

    def _handle_document_templates_get(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        rows = db.query_dicts("SELECT * FROM document_templates ORDER BY is_default DESC, name")
        for r in rows:
            for k in ("created_at", "updated_at"):
                if r.get(k):
                    r[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else str(r[k])
            if r.get("default_line_items") is None:
                r["default_line_items"] = []
        _json_response(self, 200, rows)

    def _handle_document_template_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not data or not data.get("name"):
            _json_response(self, 400, {"error": "name is required"})
            return
        if data.get("is_default"):
            db.execute("UPDATE document_templates SET is_default = FALSE")
        line_items = data.get("default_line_items", [])
        if not isinstance(line_items, list):
            line_items = []
        try:
            line_items_json = json.dumps(line_items)
        except Exception:
            line_items_json = "[]"
        row = db.query_one(
            """INSERT INTO document_templates
               (name, company_name, company_address, company_city_state_zip,
                company_phone, company_email, primary_color,
                invoice_title, coc_title, default_terms, default_sales_rep,
                default_line_items, show_signature, signature_name, is_default)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (data["name"],
             data.get("company_name", ""), data.get("company_address", ""),
             data.get("company_city_state_zip", ""), data.get("company_phone", ""),
             data.get("company_email", ""), data.get("primary_color", "#1a1a1a"),
             data.get("invoice_title", "INVOICE"),
             data.get("coc_title", "CERTIFICATE OF COMPLETION"),
             data.get("default_terms", "Due on receipt"),
             data.get("default_sales_rep", ""),
             line_items_json,
             data.get("show_signature", True),
             data.get("signature_name", ""),
             data.get("is_default", False)),
        )
        _json_response(self, 201, {"id": row["id"]})

    def _handle_document_template_update(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not data:
            _json_response(self, 400, {"error": "No data"})
            return
        existing = db.query_one("SELECT id, is_default FROM document_templates WHERE id = %s", (template_id,))
        if not existing:
            _json_response(self, 404, {"error": "Template not found"})
            return
        if data.get("is_default") and not existing["is_default"]:
            db.execute("UPDATE document_templates SET is_default = FALSE")
        fields = []
        params = []
        for key in ("name", "company_name", "company_address", "company_city_state_zip",
                     "company_phone", "company_email", "primary_color",
                     "invoice_title", "coc_title", "default_terms", "default_sales_rep",
                     "show_signature", "signature_name", "show_branding_footer", "is_default"):
            if key in data:
                fields.append(f"{key} = %s")
                params.append(data[key])
        if "default_line_items" in data:
            li = data["default_line_items"]
            if not isinstance(li, list):
                li = []
            try:
                fields.append("default_line_items = %s")
                params.append(json.dumps(li))
            except Exception:
                pass
        if fields:
            fields.append("updated_at = NOW()")
            params.append(template_id)
            db.execute(f"UPDATE document_templates SET {', '.join(fields)} WHERE id = %s", tuple(params))
        _json_response(self, 200, {"ok": True})

    def _handle_document_template_delete(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        existing = db.query_one("SELECT id, is_default FROM document_templates WHERE id = %s", (template_id,))
        if not existing:
            _json_response(self, 404, {"error": "Template not found"})
            return
        if existing["is_default"]:
            _json_response(self, 400, {"error": "Cannot delete the default template"})
            return
        db.execute("DELETE FROM document_templates WHERE id = %s", (template_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_document_template_logo_upload(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        data, err = _validate_logo_upload(file_info)
        if err is not None:
            _json_response(self, 400, {"error": err})
            return
        filename = file_info.get("filename") or ""
        safe_name = _safe_logo_filename(filename)
        logos_dir = ROOT / "data" / "logos"
        logos_dir.mkdir(parents=True, exist_ok=True)
        # Mirror the signature handler: delete the previous logo for
        # this template so re-uploads don't leave orphans in
        # data/logos/. The previous code did not.
        prev = db.query_one(
            "SELECT logo_path FROM document_templates WHERE id = %s",
            (template_id,),
        )
        new_path = logos_dir / f"doctemplate_{template_id}_{safe_name}"
        if prev and prev.get("logo_path"):
            try:
                _old = ROOT / prev["logo_path"]
                if _old.exists() and _old != new_path:
                    _old.unlink()
            except OSError:
                pass
        new_path.write_bytes(data)
        rel = str(new_path.relative_to(ROOT))
        db.execute("UPDATE document_templates SET logo_path = %s, updated_at = NOW() WHERE id = %s",
                   (rel, template_id))
        _json_response(self, 200, {"ok": True, "logoPath": rel})

    def _handle_document_template_signature_upload(self, template_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        data, err = _validate_logo_upload(file_info)
        if err is not None:
            _json_response(self, 400, {"error": err})
            return
        filename = file_info.get("filename") or ""
        safe_name = _safe_logo_filename(filename)
        sigs_dir = ROOT / "data" / "logos"
        sigs_dir.mkdir(parents=True, exist_ok=True)
        # Use a distinct filename prefix so signatures don't collide with logos.
        new_path = sigs_dir / f"docsig_{template_id}_{safe_name}"
        # Replace any existing signature file for this template.
        try:
            existing = db.query_one(
                "SELECT signature_path FROM document_templates WHERE id = %s",
                (template_id,),
            )
            if existing and existing.get("signature_path"):
                _old = ROOT / existing["signature_path"]
                if _old.exists() and _old != new_path:
                    try:
                        _old.unlink()
                    except Exception:
                        pass
        except Exception:
            pass
        new_path.write_bytes(data)
        rel = str(new_path.relative_to(ROOT))
        db.execute(
            "UPDATE document_templates SET signature_path = %s, updated_at = NOW() WHERE id = %s",
            (rel, template_id),
        )
        _json_response(self, 200, {"ok": True, "signaturePath": rel})

    # ── Service Feeds (third-party status / webhook → notifications) ───────

    def _handle_server_timezone_get(self) -> None:
        """Return the server's configured timezone (IANA name).

        Affects server-side date math only (automation triggers, daily-quote
        rollover, task reminders). User-facing timestamps are rendered in each
        viewer's local timezone by their browser — not by this setting.
        """
        user = _require_auth(self)
        if not user:
            return
        tz = _server_timezone()
        _json_response(self, 200, {"timezone": tz, "default": _SERVER_TZ_DEFAULT})

    def _handle_server_timezone_post(self) -> None:
        """Set the server's timezone. Validates the value against zoneinfo."""
        user = _require_auth(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        tz = (data.get("timezone") or "").strip()
        if not tz:
            _json_response(self, 400, {"error": "timezone is required"})
            return
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Unknown timezone: {tz}"})
            return
        global _SERVER_TZ_CACHE
        _SERVER_TZ_CACHE = tz  # invalidate cache
        try:
            db.execute(
                """INSERT INTO system_settings (key, value, description)
                   VALUES ('server_timezone', %s, 'Server timezone for scheduled-task math. User-facing timestamps render in each viewer local timezone.')
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (tz,),
            )
        except Exception as exc:
            _json_response(self, 500, {"error": f"Failed to save timezone: {exc}"})
            return
        _json_response(self, 200, {"ok": True, "timezone": tz})

    def _handle_service_feeds_get(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        rows = db.query_dicts(
            """SELECT id, name, kind, url, poll_interval_sec, last_polled_at, last_item_id,
                      enabled, target_mode, target_user_ids, accent_color, created_at, updated_at
               FROM service_feeds ORDER BY name"""
        )
        for r in rows:
            for k in ("last_polled_at", "created_at", "updated_at"):
                if r.get(k):
                    r[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else str(r[k])
        _json_response(self, 200, rows)

    def _handle_service_feed_create(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not data.get("name"):
            _json_response(self, 400, {"error": "name is required"})
            return
        kind = data.get("kind", "rss")
        if kind not in ("rss", "webhook"):
            _json_response(self, 400, {"error": "kind must be 'rss' or 'webhook'"})
            return
        # Generate webhook secret on create so the URL is available immediately.
        webhook_secret = None
        if kind == "webhook":
            import service_feed_poller
            webhook_secret = service_feed_poller.generate_webhook_secret()
        row = db.query_one(
            """INSERT INTO service_feeds
               (name, kind, url, webhook_secret, poll_interval_sec, enabled,
                target_mode, target_user_ids, accent_color)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (data["name"].strip(), kind,
             (data.get("url") or "").strip() or None,
             webhook_secret,
             max(60, int(data.get("poll_interval_sec") or 300)),
             bool(data.get("enabled", True)),
             data.get("target_mode", "all") if data.get("target_mode") in ("all", "specific") else "all",
             _normalize_target_user_ids(data.get("target_user_ids")),
             (data.get("accent_color") or "#e6a700").strip() or "#e6a700",
            ),
        )
        _json_response(self, 201, {"id": row["id"], "webhookSecret": webhook_secret})

    def _handle_service_feed_update(self, feed_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        try:
            data = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not data:
            _json_response(self, 400, {"error": "No data"})
            return
        existing = db.query_one(
            "SELECT id, kind FROM service_feeds WHERE id = %s", (feed_id,)
        )
        if not existing:
            _json_response(self, 404, {"error": "Feed not found"})
            return
        fields = []
        params: list = []
        for key in ("name", "url", "poll_interval_sec", "enabled", "accent_color"):
            if key in data:
                if key == "poll_interval_sec":
                    fields.append(f"{key} = %s")
                    params.append(max(60, int(data[key])))
                elif key == "enabled":
                    fields.append(f"{key} = %s")
                    params.append(bool(data[key]))
                else:
                    fields.append(f"{key} = %s")
                    params.append(data[key] if data[key] is not None else None)
        if "target_mode" in data and data["target_mode"] in ("all", "specific"):
            fields.append("target_mode = %s")
            params.append(data["target_mode"])
        if "target_user_ids" in data:
            fields.append("target_user_ids = %s")
            params.append(_normalize_target_user_ids(data["target_user_ids"]))
        if fields:
            fields.append("updated_at = NOW()")
            params.append(feed_id)
            db.execute(f"UPDATE service_feeds SET {', '.join(fields)} WHERE id = %s", tuple(params))
        _json_response(self, 200, {"ok": True})

    def _handle_service_feed_delete(self, feed_id: int) -> None:
        user = _require_admin(self)
        if not user:
            return
        existing = db.query_one("SELECT id FROM service_feeds WHERE id = %s", (feed_id,))
        if not existing:
            _json_response(self, 404, {"error": "Feed not found"})
            return
        db.execute("DELETE FROM service_feeds WHERE id = %s", (feed_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_service_feed_refresh(self, feed_id: int) -> None:
        """Force an immediate poll of one RSS feed (admin only)."""
        user = _require_admin(self)
        if not user:
            return
        feed = db.query_one(
            "SELECT id, name, kind, url, poll_interval_sec, last_polled_at, last_item_id, enabled, accent_color FROM service_feeds WHERE id = %s",
            (feed_id,),
        )
        if not feed:
            _json_response(self, 404, {"error": "Feed not found"})
            return
        if feed.get("kind") != "rss" or not feed.get("url"):
            _json_response(self, 400, {"error": "Feed is not an RSS feed with a URL"})
            return
        # Reset last_polled_at to force the poller to pick it up immediately.
        try:
            db.execute(
                "UPDATE service_feeds SET last_polled_at = NULL, updated_at = NOW() WHERE id = %s",
                (feed_id,),
            )
        except Exception:
            pass
        # Best-effort immediate poll so the admin sees results without waiting.
        try:
            import service_feed_poller
            service_feed_poller._poll_one_feed(sys.modules[__name__], feed)
        except Exception as exc:
            log_infra_event("warn", f"Manual feed refresh failed for {feed_id}: {exc}", source="service-feed")
        # Re-fetch the feed so the response reflects the updated last_polled_at.
        fresh = db.query_one(
            "SELECT id, name, last_polled_at, last_item_id FROM service_feeds WHERE id = %s",
            (feed_id,),
        )
        if fresh and fresh.get("last_polled_at"):
            fresh["last_polled_at"] = fresh["last_polled_at"].isoformat() if hasattr(fresh["last_polled_at"], "isoformat") else str(fresh["last_polled_at"])
        _json_response(self, 200, {"ok": True, "feed": fresh})

    def _handle_service_feed_webhook(self, secret: str) -> None:
        """Receive a webhook POST from a third-party service.

        No auth (secret token in URL is the auth). Returns 200 for both
        success and unknown-secret to prevent probing.
        """
        try:
            data = json.loads(_read_body(self) or b"{}")
        except Exception:
            data = {}
        feed = db.query_one(
            "SELECT id, name, kind, enabled, accent_color FROM service_feeds WHERE webhook_secret = %s",
            (secret,),
        )
        if not feed:
            # Return 200 anyway to avoid leaking which secrets exist.
            _json_response(self, 200, {"ok": True})
            return
        if not feed.get("enabled"):
            _json_response(self, 200, {"ok": True})
            return
        # Accept multiple shapes: Proton status (event/title/body),
        # generic {title, message, url}, or anything else where we can find a
        # title-ish field.
        title = (
            (data.get("event") or {}).get("title")
            or data.get("title")
            or data.get("subject")
            or data.get("name")
            or data.get("incident_name")
            or "(no title)"
        )
        body = (
            (data.get("event") or {}).get("body")
            or data.get("message")
            or data.get("summary")
            or data.get("description")
            or data.get("body")
            or ""
        )
        url = (
            (data.get("event") or {}).get("shortlink")
            or (data.get("event") or {}).get("url")
            or data.get("url")
            or data.get("link")
            or ""
        )
        entry = {"id": data.get("id") or data.get("event_id") or url or title, "title": title, "summary": body, "link": url}
        try:
            self._insert_feed_notification(feed, entry)
        except Exception as exc:
            log_infra_event("error", f"Webhook insert failed for feed {feed.get('id')}: {exc}", source="service-feed")
        _json_response(self, 200, {"ok": True})

    def _insert_feed_notification(self, feed: dict, entry: dict) -> None:
        """Delegate to the module-level helper so the background poller can
        call it without needing a KanbanHandler instance."""
        _insert_feed_notification(feed, entry)

    # ── Photo Report Generation ──────────────────────────────────────────

    def _handle_project_photo_report(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            from fpdf import FPDF
        except ImportError:
            _json_response(self, 500, {"error": "fpdf2 not installed"})
            return
        data = json.loads(_read_body(self) or b"{}")
        if not data:
            _json_response(self, 400, {"error": "No data"})
            return
        photo_ids = data.get("photo_ids", [])
        if not photo_ids:
            _json_response(self, 400, {"error": "photo_ids required"})
            return
        template_id = data.get("template_id")
        template = None
        if template_id:
            template = db.query_one("SELECT * FROM photo_report_templates WHERE id = %s", (template_id,))
        if not template:
            template = db.query_one("SELECT * FROM photo_report_templates WHERE is_default = TRUE")
        if not template:
            template = {"company_name": "Company", "address": "", "city_state_zip": "", "phone": "",
                        "primary_color": "#1a1a1a", "text_logo_lines": '["Company"]',
                        "text_logo_font_size": 16, "text_logo_tagline_size": 8,
                        "logo_path": None, "max_image_width_inches": 6.5, "image_alignment": "center",
                        "show_photo_date": True, "title_includes_claim": True, "show_captions": True}
        report_title = data.get("report_title", "Photo Report")
        file_name = data.get("file_name", report_title)
        customer_name = data.get("customer_name", "")
        claim_number = data.get("claim_number", "")
        project_number = data.get("project_number", "")
        address = data.get("address", "")
        summary = data.get("summary", "")
        summary_html = data.get("summary_html", "") or ""
        photos_per_page = max(1, min(4, int(data.get("photos_per_page", 1))))
        max_size_mb = max(1, min(50, float(data.get("max_size_mb", 20))))
        captions = data.get("captions", {})
        photo_order = data.get("photo_order", "selection")
        placeholders = ",".join(["%s"] * len(photo_ids))
        if photo_order == "date":
            order_clause = "ORDER BY p.uploaded_at ASC"
        else:
            order_clause = "ORDER BY array_position(ARRAY[" + placeholders + "]::int[], p.id)"
        photo_rows = db.query_dicts(
            f"""SELECT p.id, p.filename, p.file_path, p.file_size, p.mime_type, p.exif_data, p.uploaded_at
                FROM project_photos p
                WHERE p.id IN ({placeholders}) AND p.is_deleted = FALSE {order_clause}""",
            (*photo_ids, *photo_ids) if photo_order != "date" else tuple(photo_ids),
        )
        if not photo_rows:
            _json_response(self, 404, {"error": "No photos found"})
            return
        logo_path = None
        if template.get("logo_path"):
            lp = ROOT / template["logo_path"]
            if lp.exists():
                logo_path = str(lp)
        text_logo_lines = template.get("text_logo_lines", '["Company"]')
        if isinstance(text_logo_lines, str):
            try:
                text_logo_lines = json.loads(text_logo_lines)
            except Exception:
                text_logo_lines = ["Company"]
        max_img_w = float(template.get("max_image_width_inches", 6.5))
        img_align = template.get("image_alignment", "center")
        show_date = template.get("show_photo_date", True)
        show_claim = template.get("title_includes_claim", True)
        show_captions = template.get("show_captions", True)
        primary_color = template.get("primary_color", "#1a1a1a")
        company_name = template.get("company_name", "Company")
        comp_address = template.get("address", "")
        comp_city = template.get("city_state_zip", "")
        comp_phone = template.get("phone", "")
        title_font_size = int(template.get("text_logo_font_size", 10))
        tagline_font_size = int(template.get("text_logo_tagline_size", 8))
        try:
            import colorsys
            r_c = int(primary_color[1:3], 16)
            g_c = int(primary_color[3:5], 16)
            b_c = int(primary_color[5:7], 16)
        except Exception:
            r_c, g_c, b_c = 26, 26, 26
        output_dir = ROOT / "data" / "documents" / str(opp_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = re.sub(r'[^\w.\-]', '_', file_name)[:100]
        output_path = str(output_dir / f"{int(time.time())}_{safe_filename}.pdf")
        quality = 85
        success = False
        while quality >= 30:
            tmp_dir = tempfile.mkdtemp()
            try:
                processed = []
                for idx, pr in enumerate(photo_rows):
                    fp = ROOT / pr["file_path"]
                    if not fp.exists():
                        continue
                    try:
                        with Image.open(fp) as img:
                            if img.mode in ("RGBA", "P"):
                                img = img.convert("RGB")
                            max_dim = 1800
                            if img.width > max_dim or img.height > max_dim:
                                ratio = min(max_dim / img.width, max_dim / img.height)
                                new_size = (int(img.width * ratio), int(img.height * ratio))
                                img = img.resize(new_size, Image.LANCZOS)
                            out = os.path.join(tmp_dir, f"img_{idx:04d}.jpg")
                            img.save(out, "JPEG", quality=quality, optimize=True)
                            processed.append((out, pr["filename"], str(fp), pr["uploaded_at"], captions.get(str(pr["id"]), "")))
                    except Exception:
                        continue
                if not processed:
                    _json_response(self, 500, {"error": "No valid photos found"})
                    return
                self._create_photo_report_pdf(
                    output_path, processed, report_title, customer_name, claim_number,
                    project_number, address, summary, summary_html, company_name, comp_address, comp_city,
                    comp_phone, logo_path, text_logo_lines, title_font_size, tagline_font_size,
                    r_c, g_c, b_c, max_img_w, img_align, show_date, show_claim, show_captions,
                    photos_per_page,
                )
                file_size = os.path.getsize(output_path)
                if file_size <= max_size_mb * 1024 * 1024:
                    success = True
                    break
                quality -= 10
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        if not success:
            _json_response(self, 500, {"error": "Could not generate PDF within size limit"})
            return
        file_size = os.path.getsize(output_path)
        rel_path = os.path.relpath(output_path, ROOT)
        pdf_title = f"{safe_filename}.pdf"
        photo_id_result = db.insert_returning(
            """INSERT INTO project_documents (opportunity_id, title, file_path, file_size, mime_type, uploaded_by)
               VALUES (%s, %s, %s, %s, 'application/pdf', %s) RETURNING id""",
            (opp_id, pdf_title, rel_path, file_size, user["id"]),
        )
        # System action note (matches invoice/COC behavior). category_id 15 =
        # the "Action" category shipped in init.sql (sort_order 15, icon_key
        # 'pencil'). Keep this consistent with the invoice/COC handlers.
        _size_label = _humanize_file_size(file_size)
        _action_content = f"Photo report generated: {pdf_title} ({_size_label}"
        if claim_number:
            _action_content += f", Claim #{claim_number}"
        _action_content += ")"
        try:
            db.execute(
                """INSERT INTO history_events
                   (opportunity_id, category_id, title, content, created_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (opp_id, _resolve_action_category_id(), "Photo Report generated", _action_content, user["id"]),
            )
        except Exception:
            pass
        _json_response(self, 201, {
            "id": photo_id_result, "title": pdf_title, "fileSize": file_size,
            "mimeType": "application/pdf",
        })

    def _create_photo_report_pdf(self, output_path, photos, report_title, customer_name,
                                  claim_number, project_number, address, summary, summary_html,
                                  company_name, comp_address, comp_city, comp_phone,
                                  logo_path, text_logo_lines, title_font_size, tagline_font_size,
                                  r_c, g_c, b_c, max_img_w, img_align, show_date, show_claim,
                                  show_captions, photos_per_page):
        from fpdf import FPDF
        class PhotoReportPDF(FPDF):
            pass
        pdf = PhotoReportPDF(orientation="P", unit="in", format="Letter")
        pdf.set_auto_page_break(auto=False)
        pdf.set_margins(0.75, 0.5, 0.75)
        page_w = 8.5
        page_h = 11.0
        margin_l = 0.75
        margin_r = 0.75
        margin_t = 0.5
        margin_b = 0.5
        usable_w = page_w - margin_l - margin_r
        def _pdf_safe(text):
            if not text:
                return ""
            return text.encode("latin-1", errors="replace").decode("latin-1")
        def _pdf_safe_html(html_text):
            if not html_text:
                return ""
            safe = html_text.encode("latin-1", errors="replace").decode("latin-1")
            stripped = re.sub(r"<(script|style|iframe|object|embed|link|meta|base)\b[^>]*>.*?</\1>", "", safe, flags=re.DOTALL | re.IGNORECASE)
            stripped = re.sub(r"<(script|style|iframe|object|embed|link|meta|base)\b[^>]*/?>", "", stripped, flags=re.IGNORECASE)
            stripped = re.sub(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", stripped, flags=re.IGNORECASE)
            return stripped
        def _draw_header(pdf_obj):
            y = margin_t
            logo_w = 0
            logo_h = 0
            max_logo_h = 0.5
            if logo_path:
                try:
                    with Image.open(logo_path) as logo_img:
                        lw, lh = logo_img.size
                        aspect = lh / lw
                        logo_w = min(1.5, usable_w * 0.3)
                        logo_h = logo_w * aspect
                        if logo_h > max_logo_h:
                            logo_h = max_logo_h
                            logo_w = logo_h / aspect
                        pdf_obj.image(logo_path, x=margin_l, y=y, w=logo_w, h=logo_h)
                except Exception:
                    pass
            text_x = margin_l + logo_w + (0.15 if logo_w > 0 else 0)
            text_w = usable_w * 0.45 if logo_w > 0 else usable_w * 0.5
            if text_logo_lines and text_logo_lines[0]:
                pdf_obj.set_text_color(r_c, g_c, b_c)
                pdf_obj.set_font("Helvetica", "B", title_font_size)
                pdf_obj.set_xy(text_x, y)
                pdf_obj.cell(text_w, title_font_size / 72 + 0.1, _pdf_safe(text_logo_lines[0]), align="L")
                if len(text_logo_lines) > 1 and text_logo_lines[1]:
                    pdf_obj.set_font("Helvetica", "", tagline_font_size)
                    pdf_obj.set_xy(text_x, y + title_font_size / 72 + 0.05)
                    pdf_obj.cell(text_w, tagline_font_size / 72 + 0.05, _pdf_safe(text_logo_lines[1]), align="L")
                pdf_obj.set_text_color(0, 0, 0)
            info_x = margin_l + usable_w * 0.5
            info_w = usable_w * 0.5
            info_y = y + 0.05
            pdf_obj.set_font("Helvetica", "B", 9)
            pdf_obj.set_xy(info_x, info_y)
            pdf_obj.cell(info_w, 0.18, _pdf_safe(company_name), align="R")
            pdf_obj.set_font("Helvetica", "", 8)
            if comp_address:
                info_y += 0.18
                pdf_obj.set_xy(info_x, info_y)
                pdf_obj.cell(info_w, 0.15, _pdf_safe(comp_address), align="R")
            if comp_city:
                info_y += 0.15
                pdf_obj.set_xy(info_x, info_y)
                pdf_obj.cell(info_w, 0.15, _pdf_safe(comp_city), align="R")
            if comp_phone:
                info_y += 0.15
                pdf_obj.set_xy(info_x, info_y)
                pdf_obj.cell(info_w, 0.15, _pdf_safe(comp_phone), align="R")
            header_bottom = max(y + 0.6, info_y + 0.2)
            pdf_obj.set_draw_color(180, 180, 180)
            pdf_obj.line(margin_l, header_bottom, page_w - margin_r, header_bottom)
            return header_bottom
        def _draw_footer(pdf_obj, page_num, total_pages):
            y = page_h - margin_b - 0.2
            pdf_obj.set_draw_color(180, 180, 180)
            pdf_obj.line(margin_l, y - 0.05, page_w - margin_r, y - 0.05)
            pdf_obj.set_font("Helvetica", "", 8)
            left_txt = f"Project #{project_number}" if project_number else customer_name
            pdf_obj.set_xy(margin_l, y)
            pdf_obj.cell(usable_w * 0.5, 0.15, _pdf_safe(str(left_txt)), align="L")
            center_txt = f"Page {page_num} of {total_pages}"
            pdf_obj.set_xy(margin_l, y)
            pdf_obj.cell(usable_w, 0.15, _pdf_safe(center_txt), align="C")
            right_txt = f"Location: {address}" if address else ""
            pdf_obj.set_xy(margin_l + usable_w * 0.5, y)
            pdf_obj.cell(usable_w * 0.5, 0.15, _pdf_safe(right_txt), align="R")
        def _format_photo_date(uploaded_at):
            if not uploaded_at:
                return ""
            try:
                if isinstance(uploaded_at, str):
                    dt = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
                else:
                    dt = uploaded_at
                return dt.strftime("%B %d, %Y %I:%M %p")
            except Exception:
                return str(uploaded_at)
        has_summary = bool(summary and summary.strip())
        total_photos = len(photos)
        if photos_per_page == 1:
            pages_needed = total_photos
        else:
            pages_needed = (total_photos + photos_per_page - 1) // photos_per_page
        if has_summary:
            pages_needed += 1
        photo_idx = 0
        def _draw_photo_batch(pdf_obj, batch, y_start, available_h):
            n = len(batch)
            if n == 0:
                return
            gap = 0.15
            info_h = 0.35 if show_captions else 0.2
            if n == 4:
                cols, rows = 2, 2
                total_gap_w = gap * (cols - 1)
                total_gap_h = gap * (rows - 1)
                cell_w = (usable_w - total_gap_w) / cols
                cell_h = (available_h - total_gap_h) / rows
                for i, (img_path, orig_name, orig_path, uploaded_at, caption) in enumerate(batch):
                    col = i % cols
                    row = i // cols
                    cx = margin_l + col * (cell_w + gap)
                    cy = y_start + row * (cell_h + gap)
                    img_h = cell_h - info_h
                    if img_h < 0.3:
                        img_h = cell_h
                        info_h_cell = 0
                    else:
                        info_h_cell = info_h
                    if img_h <= 0:
                        continue
                    try:
                        with Image.open(img_path) as img:
                            iw, ih = img.size
                            max_w = min(max_img_w, cell_w) * 72
                            max_h_px = img_h * 72
                            scale = min(max_w / iw, max_h_px / ih, 1.0)
                            draw_w = iw * scale / 72
                            draw_h = ih * scale / 72
                            x_off = cx + (cell_w - draw_w) / 2
                            pdf_obj.image(img_path, x=x_off, y=cy, w=draw_w, h=draw_h)
                            pdf_obj.set_draw_color(180, 180, 180)
                            pdf_obj.set_line_width(0.5 / 72)
                            pdf_obj.rect(x_off, cy, draw_w, draw_h)
                            pdf_obj.set_line_width(0.2 / 72)
                    except Exception:
                        pass
                    if info_h_cell > 0:
                        info_y = cy + draw_h + 0.05
                        pdf_obj.set_font("Helvetica", "B", 9)
                        pdf_obj.set_xy(cx, info_y)
                        pdf_obj.multi_cell(cell_w, 0.15, _pdf_safe(orig_name), align="L")
                        if show_captions and caption:
                            pdf_obj.set_font("Helvetica", "", 9)
                            pdf_obj.set_xy(cx, pdf_obj.get_y())
                            pdf_obj.multi_cell(cell_w, 0.15, _pdf_safe(caption), align="L")
                        if show_date:
                            date_str = _format_photo_date(uploaded_at)
                            if date_str:
                                pdf_obj.set_font("Helvetica", "", 8)
                                pdf_obj.set_xy(cx, pdf_obj.get_y())
                                pdf_obj.cell(cell_w, 0.12, f"Photo Date: {_pdf_safe(date_str)}", align="L")
            else:
                total_gap = gap * (n - 1)
                photo_h = (available_h - total_gap) / n
                for i, (img_path, orig_name, orig_path, uploaded_at, caption) in enumerate(batch):
                    py = y_start + i * (photo_h + gap)
                    img_h = photo_h - info_h
                    if img_h < 0.3:
                        img_h = photo_h
                        info_h_single = 0
                    else:
                        info_h_single = info_h
                    if img_h <= 0:
                        continue
                    try:
                        with Image.open(img_path) as img:
                            iw, ih = img.size
                            max_w = min(max_img_w, usable_w) * 72
                            max_h_px = img_h * 72
                            scale = min(max_w / iw, max_h_px / ih, 1.0)
                            draw_w = iw * scale / 72
                            draw_h = ih * scale / 72
                            if img_align == "center":
                                x_off = margin_l + (usable_w - draw_w) / 2
                            else:
                                x_off = margin_l
                            pdf_obj.image(img_path, x=x_off, y=py, w=draw_w, h=draw_h)
                            pdf_obj.set_draw_color(180, 180, 180)
                            pdf_obj.set_line_width(0.5 / 72)
                            pdf_obj.rect(x_off, py, draw_w, draw_h)
                            pdf_obj.set_line_width(0.2 / 72)
                    except Exception:
                        pass
                    if info_h_single > 0:
                        info_y = py + draw_h + 0.05
                        pdf_obj.set_font("Helvetica", "B", 9)
                        pdf_obj.set_xy(margin_l, info_y)
                        pdf_obj.multi_cell(usable_w, 0.15, _pdf_safe(orig_name), align="L")
                        if show_captions and caption:
                            pdf_obj.set_font("Helvetica", "", 9)
                            pdf_obj.set_xy(margin_l, pdf_obj.get_y())
                            pdf_obj.multi_cell(usable_w, 0.15, _pdf_safe(caption), align="L")
                        if show_date:
                            date_str = _format_photo_date(uploaded_at)
                            if date_str:
                                pdf_obj.set_font("Helvetica", "", 8)
                                pdf_obj.set_xy(margin_l, info_y + (0.3 if caption and show_captions else 0.15))
                                pdf_obj.cell(usable_w, 0.12, f"Photo Date: {_pdf_safe(date_str)}", align="L")
        for page_num in range(pages_needed):
            pdf.add_page()
            header_bottom = _draw_header(pdf)
            _draw_footer(pdf, page_num + 1, pages_needed)
            content_top = header_bottom + 0.15
            content_bottom = page_h - margin_b - 0.35
            content_h = content_bottom - content_top
            if page_num == 0 and has_summary:
                title_y = content_top
                full_title = report_title
                if show_claim and claim_number:
                    full_title += f" - Claim #{claim_number}"
                pdf.set_font("Helvetica", "B", 14)
                pdf.set_xy(margin_l, title_y)
                pdf.cell(usable_w, 0.3, _pdf_safe(full_title), align="L")
                summary_y = title_y + 0.4
                pdf.set_xy(margin_l, summary_y)
                pdf.set_font("Helvetica", "", 10)
                if summary_html and summary_html.strip():
                    safe_html = _pdf_safe_html(summary_html)
                    pdf.write_html(safe_html)
                else:
                    pdf.multi_cell(usable_w, 0.18, _pdf_safe(summary), align="L")
            else:
                if page_num == 0 and not has_summary:
                    title_y = content_top
                    full_title = report_title
                    if show_claim and claim_number:
                        full_title += f" - Claim #{claim_number}"
                    pdf.set_font("Helvetica", "B", 14)
                    pdf.set_xy(margin_l, title_y)
                    pdf.cell(usable_w, 0.3, _pdf_safe(full_title), align="L")
                    content_top += 0.4
                    content_h = content_bottom - content_top
                actual_page = page_num if not has_summary else page_num - 1
                batch = photos[photo_idx:photo_idx + photos_per_page]
                if batch:
                    _draw_photo_batch(pdf, batch, content_top, content_h)
                    photo_idx += len(batch)
        pdf.output(output_path)

    # ── Invoice / Certificate of Completion Generation ───────────────────

    def _handle_project_invoice_generate(self, opp_id: int) -> None:
        """Generate an Invoice PDF scoped to a project, save to its Documents,
        and insert a system action note on the project's history timeline."""
        user = _require_auth(self)
        if not user:
            return
        try:
            from fpdf import FPDF
        except Exception:
            _json_response(self, 500, {"error": "fpdf2 not installed"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        template = _resolve_document_template(payload.get("template_id"))
        if not template:
            _json_response(self, 400, {"error": "No document template configured"})
            return
        # Build line items: payload overrides template defaults.
        line_items = _normalize_line_items(payload.get("line_items"))
        if not line_items:
            tmpl_lines = template.get("default_line_items") or []
            if isinstance(tmpl_lines, str):
                try:
                    tmpl_lines = json.loads(tmpl_lines)
                except Exception:
                    tmpl_lines = []
            line_items = _normalize_line_items(tmpl_lines)
        customer_name = (payload.get("customer_name") or "").strip()
        if not customer_name:
            # Try to resolve from project's primary contact.
            try:
                _proj = db.query_one("SELECT title FROM opportunities WHERE id = %s", (opp_id,))
                customer_name = (_proj or {}).get("title") or "Customer"
            except Exception:
                customer_name = "Customer"
        base_amount = _money(payload.get("base_amount"))
        invoice_number = (payload.get("invoice_number") or "").strip()
        if not invoice_number:
            try:
                _proj = db.query_one("SELECT project_number FROM opportunities WHERE id = %s", (opp_id,))
                invoice_number = str((_proj or {}).get("project_number") or "")
            except Exception:
                invoice_number = ""
        invoice_date = (payload.get("invoice_date") or "").strip() or datetime.now().strftime("%m/%d/%Y")
        terms = (payload.get("terms") or template.get("default_terms") or "Due on receipt").strip()
        sales_rep = (payload.get("sales_rep") or template.get("default_sales_rep") or "").strip()
        claim_number = (payload.get("claim_number") or "").strip()
        project_number = (payload.get("project_number") or "").strip()
        street = (payload.get("street") or "").strip()
        city_state_zip = (payload.get("city_state_zip") or "").strip()
        notes = (payload.get("notes") or "").strip()
        include_signature = bool(payload.get("include_signature", template.get("show_signature", True)))
        signature_name = (payload.get("signature_name") or template.get("signature_name") or sales_rep).strip()
        signature_path = template.get("signature_path") or None
        # Output path
        safe_stem = re.sub(r'[^\w.\-]', '_', (payload.get("file_name") or f"Invoice_{customer_name}_{invoice_number}").strip()) or "Invoice"
        safe_stem = safe_stem[:80]
        target_dir = (ROOT / "data" / "documents" / str(opp_id))
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{int(time.time())}_{safe_stem}.pdf"
        try:
            self._create_invoice_pdf(
                output_path=str(output_path),
                invoice_number=invoice_number, invoice_date=invoice_date,
                terms=terms, customer_name=customer_name,
                claim_number=claim_number, project_number=project_number,
                street=street, city_state_zip=city_state_zip,
                notes=notes, sales_rep=sales_rep,
                line_items=line_items, base_amount=base_amount,
                template=template,
                include_signature=include_signature,
                signature_name=signature_name,
                signature_path=signature_path,
            )
        except Exception as exc:
            log_infra_event("error", f"PDF generation failed for opp {opp_id}: {exc}", source="doc-invoice")
            _json_response(self, 500, {"error": f"PDF generation failed: {exc}"})
            return
        file_size = os.path.getsize(output_path)
        rel_path = os.path.relpath(output_path, ROOT)
        pdf_title = f"{safe_stem}.pdf"
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (opportunity_id, title, file_path, file_size, mime_type, uploaded_by)
               VALUES (%s, %s, %s, %s, 'application/pdf', %s) RETURNING id""",
            (opp_id, pdf_title, rel_path, file_size, user["id"]),
        )
        _size_label = _humanize_file_size(file_size)
        _action_content = f"Invoice generated: {pdf_title} ({_size_label}"
        if claim_number:
            _action_content += f", Claim #{claim_number}"
        _action_content += ")"
        try:
            db.execute(
                """INSERT INTO history_events
                   (opportunity_id, category_id, title, content, created_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (opp_id, _resolve_action_category_id(), "Invoice generated", _action_content, user["id"]),
            )
        except Exception:
            pass
        _json_response(self, 201, {
            "id": doc_id, "title": pdf_title, "fileSize": file_size,
            "mimeType": "application/pdf", "historyNoteCreated": True,
        })

    def _handle_project_coc_generate(self, opp_id: int) -> None:
        """Generate a Certificate of Completion PDF scoped to a project."""
        user = _require_auth(self)
        if not user:
            return
        try:
            from fpdf import FPDF
        except Exception:
            _json_response(self, 500, {"error": "fpdf2 not installed"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        template = _resolve_document_template(payload.get("template_id"))
        if not template:
            _json_response(self, 400, {"error": "No document template configured"})
            return
        customer_name = (payload.get("customer_name") or "").strip()
        if not customer_name:
            try:
                _proj = db.query_one("SELECT title FROM opportunities WHERE id = %s", (opp_id,))
                customer_name = (_proj or {}).get("title") or "Customer"
            except Exception:
                customer_name = "Customer"
        claim_number = (payload.get("claim_number") or "").strip()
        project_number = (payload.get("project_number") or "").strip()
        street = (payload.get("street") or "").strip()
        city_state_zip = (payload.get("city_state_zip") or "").strip()
        sales_rep = (payload.get("sales_rep") or template.get("default_sales_rep") or "").strip()
        note = (payload.get("note") or "").strip()
        include_signature = bool(payload.get("include_signature", template.get("show_signature", True)))
        signature_name = (payload.get("signature_name") or template.get("signature_name") or sales_rep).strip()
        signature_path = template.get("signature_path") or None
        safe_stem = re.sub(r'[^\w.\-]', '_', (payload.get("file_name") or f"Certificate_of_Completion_{customer_name}").strip()) or "Certificate_of_Completion"
        safe_stem = safe_stem[:80]
        target_dir = (ROOT / "data" / "documents" / str(opp_id))
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{int(time.time())}_{safe_stem}.pdf"
        try:
            self._create_coc_pdf(
                output_path=str(output_path),
                customer_name=customer_name,
                claim_number=claim_number, project_number=project_number,
                street=street, city_state_zip=city_state_zip,
                sales_rep=sales_rep, note=note,
                template=template,
                include_signature=include_signature,
                signature_name=signature_name,
                signature_path=signature_path,
            )
        except Exception as exc:
            log_infra_event("error", f"PDF generation failed for opp {opp_id}: {exc}", source="doc-coc")
            _json_response(self, 500, {"error": f"PDF generation failed: {exc}"})
            return
        file_size = os.path.getsize(output_path)
        rel_path = os.path.relpath(output_path, ROOT)
        pdf_title = f"{safe_stem}.pdf"
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (opportunity_id, title, file_path, file_size, mime_type, uploaded_by)
               VALUES (%s, %s, %s, %s, 'application/pdf', %s) RETURNING id""",
            (opp_id, pdf_title, rel_path, file_size, user["id"]),
        )
        _size_label = _humanize_file_size(file_size)
        _action_content = f"Certificate of Completion generated: {pdf_title} ({_size_label}"
        if claim_number:
            _action_content += f", Claim #{claim_number}"
        _action_content += ")"
        try:
            db.execute(
                """INSERT INTO history_events
                   (opportunity_id, category_id, title, content, created_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (opp_id, _resolve_action_category_id(), "Certificate of Completion generated", _action_content, user["id"]),
            )
        except Exception:
            pass
        _json_response(self, 201, {
            "id": doc_id, "title": pdf_title, "fileSize": file_size,
            "mimeType": "application/pdf", "historyNoteCreated": True,
        })

    def _handle_standalone_invoice_generate(self) -> None:
        """Generate an Invoice PDF without a project context. The PDF is saved
        to the user's personal Documents scope with opportunity_id=NULL."""
        user = _require_auth(self)
        if not user:
            return
        try:
            from fpdf import FPDF
        except Exception:
            _json_response(self, 500, {"error": "fpdf2 not installed"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        template = _resolve_document_template(payload.get("template_id"))
        if not template:
            _json_response(self, 400, {"error": "No document template configured"})
            return
        line_items = _normalize_line_items(payload.get("line_items"))
        if not line_items:
            tmpl_lines = template.get("default_line_items") or []
            if isinstance(tmpl_lines, str):
                try:
                    tmpl_lines = json.loads(tmpl_lines)
                except Exception:
                    tmpl_lines = []
            line_items = _normalize_line_items(tmpl_lines)
        customer_name = (payload.get("customer_name") or "").strip() or "Customer"
        base_amount = _money(payload.get("base_amount"))
        invoice_number = (payload.get("invoice_number") or "").strip()
        invoice_date = (payload.get("invoice_date") or "").strip() or datetime.now().strftime("%m/%d/%Y")
        terms = (payload.get("terms") or template.get("default_terms") or "Due on receipt").strip()
        sales_rep = (payload.get("sales_rep") or template.get("default_sales_rep") or "").strip()
        claim_number = (payload.get("claim_number") or "").strip()
        project_number = (payload.get("project_number") or "").strip()
        street = (payload.get("street") or "").strip()
        city_state_zip = (payload.get("city_state_zip") or "").strip()
        notes = (payload.get("notes") or "").strip()
        include_signature = bool(payload.get("include_signature", template.get("show_signature", True)))
        signature_name = (payload.get("signature_name") or template.get("signature_name") or sales_rep).strip()
        signature_path = template.get("signature_path") or None
        safe_stem = re.sub(r'[^\w.\-]', '_', (payload.get("file_name") or f"Invoice_{customer_name}_{invoice_number}").strip()) or "Invoice"
        safe_stem = safe_stem[:80]
        scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
        scope_dir.mkdir(parents=True, exist_ok=True)
        output_path = scope_dir / f"{int(time.time())}_{safe_stem}.pdf"
        try:
            self._create_invoice_pdf(
                output_path=str(output_path),
                invoice_number=invoice_number, invoice_date=invoice_date,
                terms=terms, customer_name=customer_name,
                claim_number=claim_number, project_number=project_number,
                street=street, city_state_zip=city_state_zip,
                notes=notes, sales_rep=sales_rep,
                line_items=line_items, base_amount=base_amount,
                template=template,
                include_signature=include_signature,
                signature_name=signature_name,
                signature_path=signature_path,
            )
        except Exception as exc:
            log_infra_event("error", f"PDF generation failed for user {user['id']}: {exc}", source="doc-invoice-standalone")
            _json_response(self, 500, {"error": f"PDF generation failed: {exc}"})
            return
        file_size = os.path.getsize(output_path)
        rel_path = os.path.relpath(output_path, ROOT)
        pdf_title = f"{safe_stem}.pdf"
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope)
               VALUES (%s, %s, %s, %s, 'application/pdf', %s, NULL, FALSE) RETURNING id""",
            (pdf_title, rel_path, file_size, user["id"], None, False),
        )
        _json_response(self, 201, {
            "id": doc_id, "title": pdf_title, "fileSize": file_size,
            "mimeType": "application/pdf",
        })

    def _handle_standalone_coc_generate(self) -> None:
        """Generate a Certificate of Completion PDF without a project context.
        Saved to the user's personal Documents scope with opportunity_id=NULL."""
        user = _require_auth(self)
        if not user:
            return
        try:
            from fpdf import FPDF
        except Exception:
            _json_response(self, 500, {"error": "fpdf2 not installed"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        template = _resolve_document_template(payload.get("template_id"))
        if not template:
            _json_response(self, 400, {"error": "No document template configured"})
            return
        customer_name = (payload.get("customer_name") or "").strip() or "Customer"
        claim_number = (payload.get("claim_number") or "").strip()
        project_number = (payload.get("project_number") or "").strip()
        street = (payload.get("street") or "").strip()
        city_state_zip = (payload.get("city_state_zip") or "").strip()
        sales_rep = (payload.get("sales_rep") or template.get("default_sales_rep") or "").strip()
        note = (payload.get("note") or "").strip()
        include_signature = bool(payload.get("include_signature", template.get("show_signature", True)))
        signature_name = (payload.get("signature_name") or template.get("signature_name") or sales_rep).strip()
        signature_path = template.get("signature_path") or None
        safe_stem = re.sub(r'[^\w.\-]', '_', (payload.get("file_name") or f"Certificate_of_Completion_{customer_name}").strip()) or "Certificate_of_Completion"
        safe_stem = safe_stem[:80]
        scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
        scope_dir.mkdir(parents=True, exist_ok=True)
        output_path = scope_dir / f"{int(time.time())}_{safe_stem}.pdf"
        try:
            self._create_coc_pdf(
                output_path=str(output_path),
                customer_name=customer_name,
                claim_number=claim_number, project_number=project_number,
                street=street, city_state_zip=city_state_zip,
                sales_rep=sales_rep, note=note,
                template=template,
                include_signature=include_signature,
                signature_name=signature_name,
                signature_path=signature_path,
            )
        except Exception as exc:
            log_infra_event("error", f"PDF generation failed for user {user['id']}: {exc}", source="doc-coc-standalone")
            _json_response(self, 500, {"error": f"PDF generation failed: {exc}"})
            return
        file_size = os.path.getsize(output_path)
        rel_path = os.path.relpath(output_path, ROOT)
        pdf_title = f"{safe_stem}.pdf"
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope)
               VALUES (%s, %s, %s, %s, 'application/pdf', %s, NULL, FALSE) RETURNING id""",
            (pdf_title, rel_path, file_size, user["id"], None, False),
        )
        _json_response(self, 201, {
            "id": doc_id, "title": pdf_title, "fileSize": file_size,
            "mimeType": "application/pdf",
        })

    def _create_invoice_pdf(self, output_path, invoice_number, invoice_date,
                             terms, customer_name, claim_number, project_number,
                             street, city_state_zip, notes, sales_rep,
                             line_items, base_amount, template,
                             include_signature, signature_name, signature_path):
        """Render an Invoice PDF (Letter portrait) using fpdf2."""
        from fpdf import FPDF
        # Parse primary color
        try:
            hex_color = (template.get("primary_color") or "#1a1a1a").lstrip("#")
            r_c, g_c, b_c = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        except Exception:
            r_c, g_c, b_c = 26, 26, 26
        company_name = template.get("company_name") or ""
        comp_address = template.get("company_address") or ""
        comp_city = template.get("company_city_state_zip") or ""
        comp_phone = template.get("company_phone") or ""
        comp_email = template.get("company_email") or ""
        invoice_title = template.get("invoice_title") or "INVOICE"
        logo_path = template.get("logo_path")
        # Compute grand total
        grand_total = base_amount or 0.0
        for li in line_items:
            try:
                grand_total += float(li.get("amount") or 0)
            except Exception:
                pass
        pdf = FPDF(orientation="P", unit="in", format="Letter")
        pdf.set_auto_page_break(False)
        pdf.set_margins(left=0.5, top=0.5, right=0.5)
        pdf.add_page()
        usable_w = 8.5 - 0.5 - 0.5  # 7.5
        margin_l = 0.5
        margin_r = 0.5
        page_w = 8.5
        page_h = 11.0
        # ── Header ────────────────────────────────────────────
        # Logo (left)
        if logo_path:
            try:
                _lp = ROOT / logo_path if not os.path.isabs(logo_path) else Path(logo_path)
                if _lp.exists():
                    # Fit within 1.5" x 0.5"
                    from PIL import Image as _PILImage
                    with _PILImage.open(_lp) as _img:
                        iw, ih = _img.size
                    max_w, max_h = 1.5, 0.5
                    scale = min(max_w / (iw / 96), max_h / (ih / 96), 1.0)
                    draw_w = (iw / 96) * scale
                    draw_h = (ih / 96) * scale
                    pdf.image(str(_lp), x=margin_l, y=0.5, w=draw_w, h=draw_h)
            except Exception:
                pass
        # Big title
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.set_font("Helvetica", "B", 24)
        pdf.set_xy(page_w - margin_r - 3.0, 0.5)
        pdf.cell(3.0, 0.4, _doc_pdf_safe(invoice_title), align="R")
        pdf.set_text_color(0, 0, 0)
        # Company info (right column)
        pdf.set_xy(page_w - margin_r - 2.5, 0.95)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(2.5, 0.18, _doc_pdf_safe(company_name), align="R", ln=1)
        pdf.set_font("Helvetica", "", 9)
        if comp_address:
            pdf.set_x(page_w - margin_r - 2.5)
            pdf.cell(2.5, 0.15, _doc_pdf_safe(comp_address), align="R", ln=1)
        if comp_city:
            pdf.set_x(page_w - margin_r - 2.5)
            pdf.cell(2.5, 0.15, _doc_pdf_safe(comp_city), align="R", ln=1)
        if comp_phone:
            pdf.set_x(page_w - margin_r - 2.5)
            pdf.cell(2.5, 0.15, _doc_pdf_safe(comp_phone), align="R", ln=1)
        if comp_email:
            pdf.set_x(page_w - margin_r - 2.5)
            pdf.cell(2.5, 0.15, _doc_pdf_safe(comp_email), align="R", ln=1)
        # Divider
        pdf.set_draw_color(180, 180, 180)
        pdf.set_line_width(0.01)
        pdf.line(margin_l, 1.85, page_w - margin_r, 1.85)
        # ── Bill-to + meta block ──────────────────────────────
        bill_to_y = 2.05
        pdf.set_xy(margin_l, bill_to_y)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(2.0, 0.18, "BILL TO", ln=1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_x(margin_l)
        pdf.cell(3.5, 0.22, _doc_pdf_safe(customer_name), ln=1)
        pdf.set_font("Helvetica", "", 10)
        if street:
            pdf.set_x(margin_l)
            pdf.cell(3.5, 0.18, _doc_pdf_safe(street), ln=1)
        if city_state_zip:
            pdf.set_x(margin_l)
            pdf.cell(3.5, 0.18, _doc_pdf_safe(city_state_zip), ln=1)
        # Meta (right): Invoice # / Date / Job # / Claim # / Terms
        meta_x = page_w - margin_r - 2.7
        meta_w = 2.7
        meta_pairs = [
            ("Invoice #", invoice_number),
            ("Date", invoice_date),
            ("Job #", project_number),
            ("Claim #", claim_number),
            ("Terms", terms),
        ]
        for i, (label, value) in enumerate(meta_pairs):
            yy = bill_to_y + i * 0.22
            pdf.set_xy(meta_x, yy)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(r_c, g_c, b_c)
            pdf.cell(0.9, 0.18, label, ln=0)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(meta_w - 0.9, 0.18, _doc_pdf_safe(value or "—"), ln=1)
        # ── Line items table ──────────────────────────────────
        table_top = 3.30
        header_h = 0.28
        row_h = 0.22
        # Header row (gray fill)
        pdf.set_fill_color(240, 240, 240)
        pdf.set_xy(margin_l, table_top)
        pdf.cell(usable_w, header_h, "", fill=True, ln=1)
        pdf.set_xy(margin_l, table_top)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(usable_w - 1.3, header_h, "DESCRIPTION", align="L")
        pdf.cell(1.3, header_h, "AMOUNT", align="R")
        pdf.set_text_color(0, 0, 0)
        # Base charge row (if any)
        y = table_top + header_h
        # First row: insurance claim info if present
        first_line_added = False
        if base_amount and base_amount > 0:
            pdf.set_xy(margin_l, y)
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(usable_w - 1.3, row_h, _doc_pdf_safe(terms or "Services"), align="L")
            pdf.cell(1.3, row_h, f"${base_amount:,.2f}", align="R")
            y += row_h
            first_line_added = True
        elif claim_number:
            pdf.set_xy(margin_l, y)
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(usable_w - 1.3, row_h, _doc_pdf_safe(f"Insurance Claim # {claim_number}"), align="L")
            pdf.cell(1.3, row_h, "", align="R")
            y += row_h
            first_line_added = True
        # Line items
        for li in line_items:
            desc = (li.get("description") or "").strip()
            amt = float(li.get("amount") or 0)
            if not desc and amt == 0:
                continue
            # Page break check
            if y + row_h + 0.6 > page_h - 0.5:
                # Footer on previous page implicitly via header below; just break
                pdf.add_page()
                y = 0.7
                _doc_draw_invoice_table_header(pdf, margin_l, usable_w, header_h, r_c, g_c, b_c)
                y = 0.7 + header_h
            pdf.set_xy(margin_l, y)
            pdf.set_font("Helvetica", "", 10)
            # Wrap description if too long
            _desc_cell_w = usable_w - 1.3
            _safe_desc = _doc_pdf_safe(desc)
            if pdf.get_string_width(_safe_desc) > _desc_cell_w:
                pdf.multi_cell(_desc_cell_w, row_h, _safe_desc, align="L")
                pdf.set_xy(margin_l + _desc_cell_w, y)
                pdf.cell(1.3, row_h, f"${amt:,.2f}" if amt >= 0 else f"-${abs(amt):,.2f}", align="R")
                y = pdf.get_y()
            else:
                pdf.cell(_desc_cell_w, row_h, _safe_desc, align="L")
                pdf.cell(1.3, row_h, f"${amt:,.2f}" if amt >= 0 else f"-${abs(amt):,.2f}", align="R")
                y += row_h
        # ── Grand total ──────────────────────────────────────
        if y + 0.6 > page_h - 0.5:
            pdf.add_page()
            y = 1.0
        y += 0.1
        pdf.set_fill_color(240, 240, 240)
        pdf.rect(margin_l, y, usable_w, 0.35, style="F")
        pdf.set_xy(margin_l + 0.1, y + 0.05)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(usable_w - 1.5, 0.25, "Grand Total", align="L")
        pdf.cell(1.4, 0.25, f"${grand_total:,.2f}", align="R")
        pdf.set_text_color(0, 0, 0)
        y += 0.45
        # ── Notes ─────────────────────────────────────────────
        if notes:
            if y + 0.6 > page_h - 0.5:
                pdf.add_page()
                y = 1.0
            pdf.set_xy(margin_l, y)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(r_c, g_c, b_c)
            pdf.cell(usable_w, 0.18, "Notes:", ln=1)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_x(margin_l)
            pdf.multi_cell(usable_w, 0.16, _doc_pdf_safe(notes), align="L")
        # ── Signature ─────────────────────────────────────────
        if include_signature and signature_path:
            try:
                _sp = ROOT / signature_path if not os.path.isabs(signature_path) else Path(signature_path)
                if _sp.exists():
                    if y + 0.8 > page_h - 0.5:
                        pdf.add_page()
                        y = 1.0
                    sig_w, sig_h = 1.5, 0.5
                    sig_x = page_w - margin_r - sig_w
                    sig_y = y + 0.1
                    pdf.image(str(_sp), x=sig_x, y=sig_y, w=sig_w, h=sig_h)
                    pdf.set_draw_color(180, 180, 180)
                    pdf.line(sig_x, sig_y + sig_h + 0.05, sig_x + sig_w, sig_y + sig_h + 0.05)
                    pdf.set_xy(sig_x, sig_y + sig_h + 0.1)
                    pdf.set_font("Helvetica", "", 8)
                    pdf.cell(sig_w, 0.15, _doc_pdf_safe(f"By {signature_name or sales_rep or 'Representative'}"), align="L")
                    pdf.set_x(sig_x)
                    pdf.cell(sig_w, 0.15, f"Date: {invoice_date}", align="L")
            except Exception:
                pass
        # ── Footer (last page) ────────────────────────────────
        pdf.set_draw_color(180, 180, 180)
        pdf.set_line_width(0.01)
        pdf.line(margin_l, page_h - 0.4, page_w - margin_r, page_h - 0.4)
        pdf.set_xy(margin_l, page_h - 0.35)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(100, 100, 100)
        left_txt = f"Job #{project_number}" if project_number else f"Invoice #{invoice_number}"
        pdf.cell(usable_w * 0.5, 0.15, _doc_pdf_safe(left_txt), align="L")
        pdf.cell(usable_w * 0.5, 0.15, f"Page {pdf.page_no()} of {{{{nb}}}}", align="R")
        pdf.set_text_color(0, 0, 0)
        pdf.output(output_path)

    def _create_coc_pdf(self, output_path, customer_name, claim_number,
                         project_number, street, city_state_zip,
                         sales_rep, note, template,
                         include_signature, signature_name, signature_path):
        """Render a Certificate of Completion PDF (Letter portrait) using fpdf2."""
        from fpdf import FPDF
        try:
            hex_color = (template.get("primary_color") or "#1a1a1a").lstrip("#")
            r_c, g_c, b_c = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        except Exception:
            r_c, g_c, b_c = 26, 26, 26
        company_name = template.get("company_name") or ""
        comp_address = template.get("company_address") or ""
        comp_city = template.get("company_city_state_zip") or ""
        comp_phone = template.get("company_phone") or ""
        comp_email = template.get("company_email") or ""
        coc_title = template.get("coc_title") or "CERTIFICATE OF COMPLETION"
        logo_path = template.get("logo_path")
        today = datetime.now().strftime("%m/%d/%Y")
        pdf = FPDF(orientation="P", unit="in", format="Letter")
        pdf.set_auto_page_break(False)
        pdf.set_margins(left=0.75, top=0.75, right=0.75)
        pdf.add_page()
        margin_l = 0.75
        margin_r = 0.75
        page_w = 8.5
        page_h = 11.0
        usable_w = page_w - margin_l - margin_r
        # ── Header: logo (centered) + company info (right) ───
        if logo_path:
            try:
                _lp = ROOT / logo_path if not os.path.isabs(logo_path) else Path(logo_path)
                if _lp.exists():
                    from PIL import Image as _PILImage
                    with _PILImage.open(_lp) as _img:
                        iw, ih = _img.size
                    max_w, max_h = 2.0, 0.6
                    scale = min(max_w / (iw / 96), max_h / (ih / 96), 1.0)
                    draw_w = (iw / 96) * scale
                    draw_h = (ih / 96) * scale
                    # Centered horizontally
                    logo_x = (page_w - draw_w) / 2
                    pdf.image(str(_lp), x=logo_x, y=0.75, w=draw_w, h=draw_h)
            except Exception:
                pass
        # Company info right-aligned
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.set_xy(margin_l, 1.55)
        pdf.set_font("Helvetica", "", 10)
        if comp_address:
            pdf.cell(usable_w, 0.16, _doc_pdf_safe(comp_address), align="R", ln=1)
            pdf.set_x(margin_l)
        if comp_city:
            pdf.cell(usable_w, 0.16, _doc_pdf_safe(comp_city), align="R", ln=1)
            pdf.set_x(margin_l)
        if comp_email:
            pdf.cell(usable_w, 0.16, _doc_pdf_safe(comp_email), align="R", ln=1)
            pdf.set_x(margin_l)
        if comp_phone:
            pdf.cell(usable_w, 0.16, _doc_pdf_safe(comp_phone), align="R", ln=1)
        pdf.set_text_color(0, 0, 0)
        # ── Title ─────────────────────────────────────────────
        pdf.set_xy(margin_l, 2.55)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(r_c, g_c, b_c)
        # Center the title
        _safe_coc_title = _doc_pdf_safe(coc_title)
        title_w = pdf.get_string_width(_safe_coc_title)
        pdf.set_x((page_w - title_w) / 2)
        pdf.cell(title_w, 0.3, _safe_coc_title, ln=1)
        # Underline beneath title
        pdf.set_draw_color(r_c, g_c, b_c)
        pdf.set_line_width(0.015)
        title_y = pdf.get_y() + 0.02
        pdf.line((page_w - title_w) / 2, title_y, (page_w - title_w) / 2 + title_w, title_y)
        pdf.set_text_color(0, 0, 0)
        pdf.set_line_width(0.01)
        # ── Fields ────────────────────────────────────────────
        fields_y = title_y + 0.45
        address_one = ", ".join(filter(None, [street, city_state_zip])).strip()
        fields = [
            ("Customer:", customer_name),
            ("Address:", address_one),
            ("Job #:", project_number),
            ("Claim #:", claim_number),
            ("Sales Rep:", sales_rep),
            ("Completion Date:", today),
        ]
        label_w = 1.6
        value_x = margin_l + label_w
        value_w = usable_w - label_w
        line_h = 0.30
        for i, (label, value) in enumerate(fields):
            yy = fields_y + i * line_h
            pdf.set_xy(margin_l, yy)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(r_c, g_c, b_c)
            pdf.cell(label_w, 0.22, label)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 11)
            pdf.cell(value_w, 0.22, _doc_pdf_safe(value or "—"))
        # Note (free text, wrapped)
        note_y = fields_y + len(fields) * line_h + 0.15
        pdf.set_xy(margin_l, note_y)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(r_c, g_c, b_c)
        pdf.cell(label_w, 0.22, "Note:")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_xy(value_x, note_y)
        pdf.multi_cell(value_w, 0.22, _doc_pdf_safe(note or "Work completed per agreement."), align="L")
        # ── Signature ─────────────────────────────────────────
        if include_signature and signature_path:
            try:
                _sp = ROOT / signature_path if not os.path.isabs(signature_path) else Path(signature_path)
                if _sp.exists():
                    sig_w, sig_h = 1.7, 0.55
                    sig_x = page_w - margin_r - sig_w
                    sig_y = page_h - 2.0
                    pdf.image(str(_sp), x=sig_x, y=sig_y, w=sig_w, h=sig_h)
                    pdf.set_draw_color(180, 180, 180)
                    pdf.set_line_width(0.01)
                    pdf.line(sig_x, sig_y + sig_h + 0.05, sig_x + sig_w, sig_y + sig_h + 0.05)
                    pdf.set_xy(sig_x, sig_y + sig_h + 0.1)
                    pdf.set_font("Helvetica", "", 8)
                    pdf.cell(sig_w, 0.14, _doc_pdf_safe(f"By {signature_name or sales_rep or 'Representative'}"), align="L")
                    pdf.set_x(sig_x)
                    pdf.cell(sig_w, 0.14, f"Date: {today}", align="L")
            except Exception:
                pass
        # ── Footer ────────────────────────────────────────────
        pdf.set_draw_color(180, 180, 180)
        pdf.set_line_width(0.01)
        pdf.line(margin_l, page_h - 0.4, page_w - margin_r, page_h - 0.4)
        pdf.set_xy(margin_l, page_h - 0.35)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(100, 100, 100)
        if template.get("show_branding_footer", True):
            pdf.cell(usable_w * 0.5, 0.15, "Generated by Sietch CRM", align="L")
        else:
            pdf.cell(usable_w * 0.5, 0.15, "", align="L")
        pdf.cell(usable_w * 0.5, 0.15, f"Page {pdf.page_no()} of {{{{nb}}}}", align="R")
        pdf.set_text_color(0, 0, 0)
        pdf.output(output_path)

    def _handle_project_photo_folders_get(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            """SELECT id, folder_type, label, external_url, external_provider, created_at
               FROM project_photo_folders WHERE opportunity_id = %s""",
            (opp_id,),
        )
        _json_response(self, 200, [
            {"id": r[0], "folderType": r[1], "label": r[2], "externalUrl": r[3],
             "externalProvider": r[4], "createdAt": r[5].isoformat() if r[5] else None}
            for r in rows
        ])

    def _handle_project_photo_folder_add(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        url = str(payload.get("externalUrl") or "").strip()
        if not url:
            _json_response(self, 400, {"error": "externalUrl is required"})
            return
        fid = db.insert_returning(
            """INSERT INTO project_photo_folders (opportunity_id, folder_type, label, external_url, external_provider, created_by)
               VALUES (%s, 'external', %s, %s, %s, %s) RETURNING id""",
            (opp_id, payload.get("label"), url, payload.get("externalProvider"), user["id"]),
        )
        _json_response(self, 201, {"id": fid, "ok": True})

    def _handle_photo_delete(self, photo_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute(
            "UPDATE project_photos SET is_deleted = TRUE, deleted_at = NOW() WHERE id = %s",
            (photo_id,),
        )
        _json_response(self, 200, {"ok": True})

    def _handle_photos_batch_delete(self) -> None:
        """Soft-delete multiple photos."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        if not ids:
            _json_response(self, 400, {"error": "ids required"})
            return
        is_admin = user.get("is_admin")
        rows = db.query(
            "SELECT id, uploaded_by FROM project_photos WHERE id = ANY(%s) AND is_deleted = FALSE",
            (ids,),
        )
        found = {r[0]: r[1] for r in rows}
        for pid in ids:
            if pid not in found:
                _json_response(self, 404, {"error": f"Photo {pid} not found"})
                return
            if found[pid] != user["id"] and not is_admin:
                _json_response(self, 403, {"error": f"Not authorized to delete photo {pid}"})
                return
        db.execute(
            "UPDATE project_photos SET is_deleted = TRUE, deleted_at = NOW() WHERE id = ANY(%s)",
            (ids,),
        )
        _json_response(self, 200, {"ok": True, "count": len(ids)})

    def _handle_photo_folder_delete(self, folder_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute("DELETE FROM project_photo_folders WHERE id = %s", (folder_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_photo_download(self, photo_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        want_thumb = (qs.get("thumbnail") or [""])[0].lower() in ("1", "true")
        row = db.query(
            "SELECT opportunity_id, filename, file_path, file_size, mime_type, thumbnail_path FROM project_photos WHERE id = %s AND is_deleted = FALSE",
            (photo_id,), fetch="one",
        )
        if not row:
            self.send_error(404)
            return
        opp_id, filename, file_path, file_size, mime_type, thumbnail_path = row
        if want_thumb:
            file_path = thumbnail_path or file_path
        full_path = ROOT / file_path
        if not full_path.exists():
            self.send_error(404)
            return
        data = full_path.read_bytes()
        self.send_response(200)
        # Thumbnails are always JPEG; original uses stored mime type
        ct = "image/jpeg" if want_thumb else (mime_type or "application/octet-stream")
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        want_download = (qs.get("download") or [""])[0].lower() in ("1", "true")
        disposition = "attachment" if want_download else "inline"
        self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _handle_photos_batch_download(self) -> None:
        """Return a ZIP of selected full-size photos."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        if not ids:
            _json_response(self, 400, {"error": "ids required"})
            return
        rows = db.query(
            """SELECT id, opportunity_id, filename, file_path, file_size, mime_type
               FROM project_photos WHERE id = ANY(%s) AND is_deleted = FALSE""",
            (ids,),
        )
        if not rows:
            _json_response(self, 404, {"error": "No photos found"})
            return
        buf = io.BytesIO()
        seen_names: dict[str, int] = {}
        common_opp_id = rows[0][1]
        all_same_opp = all(r[1] == common_opp_id for r in rows)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in rows:
                photo_id, opp_id, filename, file_path, file_size, mime_type = r
                src = ROOT / file_path
                if not src.exists():
                    continue
                name = filename or f"photo_{photo_id}"
                ext = Path(name).suffix or (_MIME_TO_EXT.get(mime_type, "") or ".jpg")
                if not name.endswith(ext):
                    name = name + ext
                if name in seen_names:
                    seen_names[name] += 1
                    stem = Path(name).stem
                    name = f"{stem} ({seen_names[name]}){ext}"
                else:
                    seen_names[name] = 0
                zf.writestr(name, src.read_bytes())
        zip_data = buf.getvalue()
        zip_name = f"project-{common_opp_id}-photos.zip" if all_same_opp else "selected-photos.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(zip_data)))
        self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
        self.end_headers()
        self.wfile.write(zip_data)

    def _handle_photos_batch_move(self) -> None:
        """Move selected photos to another project."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        new_opp_id = body.get("opportunity_id")
        if not ids or not new_opp_id:
            _json_response(self, 400, {"error": "ids and opportunity_id required"})
            return
        opp = db.query("SELECT id FROM opportunities WHERE id = %s", (new_opp_id,), fetch="one")
        if not opp:
            _json_response(self, 404, {"error": "Destination project not found"})
            return
        is_admin = user.get("is_admin")
        rows = db.query(
            "SELECT id, uploaded_by FROM project_photos WHERE id = ANY(%s) AND is_deleted = FALSE",
            (ids,),
        )
        found = {r[0]: r[1] for r in rows}
        for pid in ids:
            if pid not in found:
                _json_response(self, 404, {"error": f"Photo {pid} not found"})
                return
            if found[pid] != user["id"] and not is_admin:
                _json_response(self, 403, {"error": f"Not authorized to move photo {pid}"})
                return
        db.execute(
            """UPDATE project_photos
               SET opportunity_id = %s, folder_id = NULL
               WHERE id = ANY(%s)""",
            (new_opp_id, ids),
        )
        _json_response(self, 200, {"ok": True, "count": len(ids)})

    def _handle_photos_batch_copy(self) -> None:
        """Copy selected photos to another project. Current user becomes uploaded_by."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        new_opp_id = body.get("opportunity_id")
        if not ids or not new_opp_id:
            _json_response(self, 400, {"error": "ids and opportunity_id required"})
            return
        opp = db.query("SELECT id FROM opportunities WHERE id = %s", (new_opp_id,), fetch="one")
        if not opp:
            _json_response(self, 404, {"error": "Destination project not found"})
            return
        rows = db.query(
            """SELECT id, filename, file_path, file_size, mime_type, thumbnail_path
               FROM project_photos WHERE id = ANY(%s) AND is_deleted = FALSE""",
            (ids,),
        )
        if not rows:
            _json_response(self, 404, {"error": "No photos found"})
            return
        opp_dir = PHOTO_STORAGE_PATH / str(new_opp_id)
        opp_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for r in rows:
            photo_id, filename, file_path, file_size, mime_type, thumbnail_path = r
            src = ROOT / file_path
            if not src.exists():
                continue
            safe_name = re.sub(r'[^\w.\-]', '_', filename) or "image.jpg"
            unique_name = f"{int(time.time())}_{photo_id}_{safe_name}"
            dst = opp_dir / unique_name
            dst.write_bytes(src.read_bytes())
            new_thumb_path = None
            if thumbnail_path:
                thumb_src = ROOT / thumbnail_path
                if thumb_src.exists():
                    thumb_name = f"thumb_{unique_name}"
                    thumb_dst = opp_dir / thumb_name
                    thumb_dst.write_bytes(thumb_src.read_bytes())
                    new_thumb_path = str(thumb_dst.relative_to(ROOT))
            try:
                db.insert_returning(
                    """INSERT INTO project_photos (opportunity_id, filename, file_path, file_size, mime_type, thumbnail_path, uploaded_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (new_opp_id, filename, str(dst.relative_to(ROOT)), file_size, mime_type,
                     new_thumb_path, user["id"]),
                )
                copied += 1
            except Exception as exc:
                # Quota or DB error; clean up copied file
                try:
                    dst.unlink(missing_ok=True)
                    if new_thumb_path:
                        (ROOT / new_thumb_path).unlink(missing_ok=True)
                except Exception:
                    pass
                log_infra_event("error", f"Photo copy failed for {photo_id}: {exc}", source="documents", method=self.command, path=api_path)
                _json_response(self, 500, {"error": f"Copy failed for photo {photo_id}: {exc}"})
                return
        _json_response(self, 200, {"ok": True, "copied": copied})

    # ── Documents ─────────────────────────────────────────────────────────────

    def _handle_project_documents_get(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            rows = db.query(
                """SELECT id, title, file_path, file_size, mime_type, uploaded_by, uploaded_at, company_scope, folder_id
                   FROM project_documents WHERE opportunity_id = %s AND is_deleted = FALSE
                   ORDER BY uploaded_at DESC""",
                (opp_id,),
            )
            docs = []
            seen_paths = set()
            for r in rows:
                seen_paths.add(r[2])
                docs.append({
                    "id": r[0], "title": r[1], "filePath": r[2],
                    "fileSize": r[3], "mimeType": r[4], "uploadedBy": r[5],
                    "uploadedAt": r[6].isoformat() if r[6] else None,
                    "companyScope": r[7],
                    "editUrl": f"/doc-editor.html?id={r[0]}",
                })
            # Fallback: attachments that exist in history_attachments but have no
            # project_documents mirror. The Documents tab used to show "0 documents"
            # for a deal whose files were synced but never mirrored. Surface them
            # rather than hiding them. project_documents rows win on file_path.
            try:
                extra = db.query(
                    """SELECT ha.id, ha.filename, ha.file_path, ha.file_size, ha.mime_type,
                              ha.uploaded_by, ha.uploaded_at, he.id
                         FROM history_attachments ha
                         JOIN history_events he ON he.id = ha.event_id
                        WHERE he.opportunity_id = %s
                          AND NOT EXISTS (
                              SELECT 1 FROM project_documents pd
                               WHERE pd.file_path = ha.file_path AND pd.is_deleted = FALSE
                          )
                        ORDER BY ha.uploaded_at DESC""",
                    (opp_id,),
                )
                for r in extra:
                    if r[2] in seen_paths:
                        continue
                    seen_paths.add(r[2])
                    docs.append({
                        "id": r[0], "title": r[1], "filePath": r[2],
                        "fileSize": r[3], "mimeType": r[4], "uploadedBy": r[5],
                        "uploadedAt": r[6].isoformat() if r[6] else None,
                        "companyScope": False,
                        "editUrl": f"/doc-editor.html?id={r[0]}",
                        "fromHistoryAttachment": True,
                    })
            except Exception:
                pass
            _json_response(self, 200, {"documents": docs})
        except Exception as exc:
            logger.exception("projects/{id}/documents failed")
            log_infra_event("error", f"projects/{opp_id}/documents failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to load project documents"})

    def _handle_project_document_upload(self, opp_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        if not file_info:
            _json_response(self, 400, {"error": "file field required"})
            return
        filename = file_info["filename"]
        data = file_info["data"]
        mime_type = file_info.get("content-type") or _guess_mime(filename)
        safe_name = re.sub(r'[^\w.\-]', '_', filename)
        if not safe_name:
            safe_name = "document"
        opp_dir = DOCUMENT_STORAGE_PATH / str(opp_id)
        opp_dir.mkdir(parents=True, exist_ok=True)
        # avoid collisions
        unique_name = f"{int(time.time())}_{safe_name}"
        file_path = opp_dir / unique_name
        file_path.write_bytes(data)
        file_size = len(data)
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (opportunity_id, title, file_path, file_size, mime_type, uploaded_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (opp_id, filename, str(file_path.relative_to(ROOT)), file_size, mime_type, user["id"]),
        )
        _json_response(self, 201, {
            "id": doc_id, "title": filename, "fileSize": file_size,
            "mimeType": mime_type, "editUrl": f"/doc-editor.html?id={doc_id}",
        })

    def _handle_docker_health(self) -> None:
        """Return Docker container status if running inside Docker."""
        info = {"containerName": None, "status": "unknown", "restartCount": None, "inDocker": False}
        runtime = _detect_container_runtime()
        info["runtime"] = runtime or None
        info["inDocker"] = bool(runtime)
        if not runtime:
            _json_response(self, 200, info)
            return
        try:
            hostname = socket.gethostname()
            info["containerName"] = hostname
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}} {{.RestartCount}}", hostname],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                info["status"] = parts[0] if parts else "unknown"
                try:
                    info["restartCount"] = int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    info["restartCount"] = None
            else:
                info["status"] = "inspect-failed"
        except Exception as exc:
            info["status"] = f"error: {exc}"
        _json_response(self, 200, info)

    def _handle_infrastructure(self) -> None:
        """GET consolidated infrastructure status for the admin Infrastructure tab."""
        # Authz first. The handler runs expensive probes (DB query,
        # docserver HTTP request, docker subprocess × N containers,
        # disk stat) — unauthenticated callers must not reach them.
        # Other infra endpoints (infra-log, docker-health, restart-*)
        # already check _require_admin; this one was missing it.
        user = _require_admin(self)
        if not user:
            return
        # The probes below are independent and several of them can block:
        # _probe_docserver has a 5s HTTP timeout, _docker_status shells out
        # to the docker CLI (5s per container), and the DB query can stall on
        # a saturated connection pool. Run them concurrently so a single slow
        # probe can't stall the whole response — the admin tab polls this
        # endpoint every 15s, so serial probes pile up.
        import concurrent.futures

        def _probe_db() -> tuple[bool, str | None]:
            try:
                return bool(db.query("SELECT 1", fetch="one")), None
            except Exception as exc:
                return False, str(exc)

        def _probe_docker() -> dict:
            try:
                return _docker_status()
            except Exception as exc:
                return {"accessible": False, "inDocker": False, "containerName": None,
                        "containers": [], "note": f"docker probe failed: {exc}"}

        def _probe_resources() -> dict:
            return {
                "disk": _disk_usage(),
                "memory": _mem_usage(),
                "loadavg": _load_avg(),
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="infra") as pool:
            fut_db = pool.submit(_probe_db)
            fut_docker = pool.submit(_probe_docker)
            fut_resources = pool.submit(_probe_resources)
            # Docserver is probed on the calling thread so its own 5s timeout
            # bounds the wait; the pool still runs the rest concurrently.
            ds_ok, ds_latency, ds_err = _probe_docserver()
            db_ok, db_err = fut_db.result()
            docker = fut_docker.result()
            resources = fut_resources.result()
        payload = {
            "server": {
                "version": APP_VERSION,
                "uptime": round(infra_uptime(), 1),
                "startTime": datetime.fromtimestamp(infra_start_time(), timezone.utc).isoformat(),
                "pid": os.getpid(),
                "inDocker": docker.get("inDocker", False),
            },
            "db": {"reachable": db_ok, "error": db_err},
            "docserver": {
                "reachable": ds_ok,
                "latencyMs": ds_latency,
                "error": ds_err,
                "internalUrl": _effective_docs_internal_url(),
                "publicUrl": DOCS_PUBLIC_URL,
            },
            "docker": docker,
            "resources": resources,
            "errors": _infra_error_stats(),
            "backup": _backup_summary(),
            "health": {"dbReachable": db_ok},
            "events": infra_events(),
        }
        _json_response(self, 200, payload)

    # ── OnlyOffice Sync Handlers ────────────────────────────────────────────────

    def _handle_sync_test(self) -> None:
        """Test OnlyOffice CRM connection."""
        global _sync_log
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                return
            _append_sync_log("info", f"Testing connection to {portal}...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed", "log": list(_sync_log)})
                return
            # Try to fetch something to verify access — use stages (works for all accounts)
            try:
                stages_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/opportunity/stage"))
                stage_count = len(stages_data) if isinstance(stages_data, list) else 0
                _append_sync_log("info", f"Connection successful. Found {stage_count} pipeline stages.")
                _json_response(self, 200, {"ok": True, "stage_count": stage_count, "log": list(_sync_log)})
            except Exception as e:
                _append_sync_log("error", f"API test failed: {e}")
                _json_response(self, 400, {"error": f"API test failed: {e}", "log": list(_sync_log)})
        except Exception as e:
            _append_sync_log("error", f"Test failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})

    def _handle_sync_pull_tags(self) -> None:
        """Pull tags from OnlyOffice CRM."""
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            _append_sync_log("info", "Authenticating to OnlyOffice CRM...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return
            _append_sync_log("info", "Fetching tags...")
            tags_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/opportunity/tag"))
            tags = tags_data if isinstance(tags_data, list) else []
            count = 0
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                title = str(tag.get("title") or tag.get("name") or tag.get("Title") or tag.get("Name") or "").strip()
                color = str(tag.get("color") or tag.get("Color") or "#6c757d").strip()
                if not title:
                    continue
                existing = db.query_one("SELECT id FROM tag_definitions WHERE title = %s", (title,))
                if not existing:
                    db.execute("INSERT INTO tag_definitions (title, color) VALUES (%s, %s)", (title, color))
                    count += 1
                    _append_sync_log("info", f"Created tag: {title}")
            _append_sync_log("info", f"Tags sync complete: {count} new tags created")
            _json_response(self, 200, {"ok": True, "created": count, "total": len(tags), "log": list(_sync_log)})
        except Exception as e:
            _append_sync_log("error", f"Tags sync failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})
        finally:
            _sync_running = False

    def _handle_sync_pull_tasks(self) -> None:
        """Pull tasks from OnlyOffice CRM with opportunity linkage."""
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            _append_sync_log("info", "Authenticating to OnlyOffice CRM...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return

            # Build OnlyOffice ID → Sietch ID mapping from existing deals
            _append_sync_log("info", "Mapping opportunities...")
            existing_deals = db.query_dicts("SELECT id, title FROM opportunities")
            deal_by_title = {d["title"].lower().strip(): d["id"] for d in existing_deals}

            # Fetch OnlyOffice opportunities to build crm_id → v3_id map
            opp_id_map = {}
            try:
                start = 0
                while True:
                    path = f"/api/2.0/crm/opportunity/filter?count=500&startIndex={start}"
                    data = _unwrap(_crm_api_get(portal, token, path))
                    if not isinstance(data, list) or not data:
                        break
                    for o in data:
                        crm_id = str(o.get("id") or o.get("ID") or "")
                        title = str(o.get("title") or o.get("Title") or "").strip()
                        v3_id = deal_by_title.get(title.lower().strip())
                        if crm_id and v3_id:
                            opp_id_map[crm_id] = v3_id
                    if len(data) < 500:
                        break
                    start += 500
            except Exception as e:
                _append_sync_log("warn", f"Could not load opportunity map: {e}")

            _append_sync_log("info", "Fetching tasks (open + closed)...")
            try:
                # Bug #8: correct endpoint for current OnlyOffice version is /api/2.0/projects/task/
                open_tasks = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=false")) or []
                closed_tasks = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=true")) or []
                all_tasks = list(open_tasks) + list(closed_tasks)
                if not isinstance(open_tasks, list):
                    open_tasks = []
                if not isinstance(closed_tasks, list):
                    closed_tasks = []
            except Exception as e:
                _append_sync_log("warn", f"Tasks API not available: {e}")
                all_tasks = []

            count = 0
            linked_count = 0
            _admin = _require_admin(self)
            fallback_owner = _admin["id"] if _admin else 1
            owner_map = _fetch_oo_user_owner_map(portal, token)
            for task in all_tasks:
                if not isinstance(task, dict):
                    continue
                title = str(task.get("title") or task.get("Title") or "").strip()
                if not title:
                    continue
                owner_id = _oo_task_owner(task, owner_map, fallback_owner)
                existing = db.query_one(
                    "SELECT id FROM tasks WHERE title = %s AND owner_user_id = %s", (title, owner_id)
                )
                if existing:
                    continue
                desc = str(task.get("description") or task.get("Description") or "").strip() or None
                due = task.get("dueDate") or task.get("DueDate")
                if due:
                    due = str(due)[:10]
                is_closed = bool(task.get("isClosed") or task.get("IsClosed") or False)

                # Extract opportunity linkage from entity field
                entity = task.get("entity") or {}
                opportunity_id = None
                if entity.get("entityType") == "opportunity" and entity.get("entityId"):
                    crm_opp_id = str(entity["entityId"])
                    opportunity_id = opp_id_map.get(crm_opp_id)

                db.execute(
                    "INSERT INTO tasks (title, description, due_date, is_closed, opportunity_id, owner_user_id, responsible_user_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
                    (title, desc, due, is_closed, opportunity_id, owner_id, owner_id),
                )
                if opportunity_id:
                    linked_count += 1
                count += 1
                if count % 50 == 0:
                    _append_sync_log("info", f"Progress: {count} tasks synced, {linked_count} linked...")

            _append_sync_log("info", f"Tasks sync complete: {count} new tasks created, {linked_count} linked to projects")
            _json_response(self, 200, {"ok": True, "created": count, "linked": linked_count, "total": len(all_tasks), "log": list(_sync_log)})
        except Exception as e:
            _append_sync_log("error", f"Tasks sync failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})
        finally:
            _sync_running = False

    def _handle_sync_pull_deal_tags(self) -> None:
        """Sync tags and custom fields for existing deals from OnlyOffice."""
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            _append_sync_log("info", "Authenticating to OnlyOffice CRM...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return

            # Get existing tag and CF maps
            tag_map = {}
            for row in db.query_dicts("SELECT id, title FROM tag_definitions"):
                tag_map[row["title"].lower()] = row["id"]
            cf_map = {}
            for row in db.query_dicts("SELECT id, field_key FROM custom_field_definitions"):
                # field_key is "field_26", OnlyOffice ID is "26"
                numeric_id = row["field_key"].replace("field_", "")
                cf_map[numeric_id] = row["id"]

            # Get existing deals with their OnlyOffice titles
            _append_sync_log("info", "Loading existing deals...")
            existing_deals = db.query_dicts("SELECT id, title FROM opportunities")
            deal_by_title = {d["title"].lower().strip(): d["id"] for d in existing_deals}
            _append_sync_log("info", f"Found {len(deal_by_title)} existing deals")
            start_time = time.time()

            # Clear existing tags and custom field values for deals that have
            # onlyoffice_id (i.e., came from this sync). Bug #13: was DELETEing
            # everything, which would wipe local edits on partial failure.
            _append_sync_log("info", "Clearing existing tags/CFs for OnlyOffice-imported deals only...")
            db.execute(
                """DELETE FROM opportunity_tags
                   WHERE opportunity_id IN (
                       SELECT id FROM opportunities WHERE onlyoffice_id IS NOT NULL
                   )"""
            )
            db.execute(
                """DELETE FROM opportunity_custom_field_values
                   WHERE opportunity_id IN (
                       SELECT id FROM opportunities WHERE onlyoffice_id IS NOT NULL
                   )"""
            )
            _append_sync_log("info", "Cleared tags/CFs for OnlyOffice-imported deals")

            # Fetch all deals from OnlyOffice and sync tags/CFs for matching ones
            _append_sync_log("info", "Fetching deals from OnlyOffice to sync tags...")
            start = 0
            page_size = 500
            total_fetched = 0
            deals_updated = 0
            tags_synced = 0
            cfs_synced = 0

            while True:
                sep = "&" if "?" in "/api/2.0/crm/opportunity/filter" else "?"
                # Apply filters to the OnlyOffice endpoint when present.
                # The community server filter accepts these as direct query params.
                extra = []
                if since_date:
                    extra.append(f"updatedAfter={since_date}")
                if filter_stage_id is not None:
                    extra.append(f"stageId={filter_stage_id}")
                if filter_responsible_id:
                    extra.append(f"responsibleId={filter_responsible_id}")
                extra_q = ("&" + "&".join(extra)) if extra else ""
                path = f"/api/2.0/crm/opportunity/filter{sep}count={page_size}&startIndex={start}{extra_q}"
                data = _unwrap(_crm_api_get(portal, token, path))
                if not isinstance(data, list) or not data:
                    break
                total_fetched += len(data)

                for o in data:
                    if not isinstance(o, dict):
                        continue
                    crm_id = str(o.get("id") or o.get("ID") or "")
                    title = str(o.get("title") or o.get("Title") or "").strip()
                    v3_opp_id = deal_by_title.get(title.lower().strip())

                    if not v3_opp_id:
                        continue  # Deal doesn't exist locally

                    deals_updated += 1
                    elapsed = time.time() - start_time
                    rate = deals_updated / elapsed if elapsed > 0 else 0
                    remaining = (len(deal_by_title) - deals_updated) / rate if rate > 0 else 0
                    _append_sync_log("info", f"[{deals_updated}/{len(deal_by_title)}] {title[:40]}... ({int(remaining)}s remaining)")

                    # Sync tags
                    try:
                        tags_data = _unwrap(_crm_api_get(portal, token, f"/api/2.0/crm/opportunity/tag/{crm_id}"))
                        tag_list = tags_data if isinstance(tags_data, list) else []
                        for t in tag_list:
                            tag_title = ""
                            if isinstance(t, dict):
                                tag_title = str(t.get("title") or t.get("name") or t.get("Title") or "").strip()
                            elif isinstance(t, str):
                                tag_title = t.strip()
                            if tag_title:
                                tag_id = tag_map.get(tag_title.lower())
                                if not tag_id:
                                    db.execute("INSERT INTO tag_definitions (title, color) VALUES (%s, %s)", (tag_title, "#6c757d"))
                                    tag_id = db.query_one("SELECT id FROM tag_definitions WHERE title = %s", (tag_title,))["id"]
                                    tag_map[tag_title.lower()] = tag_id
                                db.execute(
                                    "INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                                    (v3_opp_id, tag_id),
                                )
                                tags_synced += 1
                    except Exception:
                        pass

                    # Sync custom fields (from full deal details, not separate endpoint)
                    try:
                        deal_data = _unwrap(_crm_api_get(portal, token, f"/api/2.0/crm/opportunity/{crm_id}"))
                        cf_list = deal_data.get("customFields", []) if isinstance(deal_data, dict) else []
                        for cf in cf_list:
                            if not isinstance(cf, dict):
                                continue
                            cf_crm_id = str(cf.get("id") or cf.get("ID") or "")
                            cf_value = cf.get("value") or cf.get("Value") or cf.get("fieldValue") or ""
                            if isinstance(cf_value, dict):
                                cf_value = cf_value.get("title") or cf_value.get("text") or cf_value.get("value") or ""
                            cf_value = str(cf_value).strip()
                            v3_field_id = cf_map.get(cf_crm_id)
                            if v3_field_id and cf_value:
                                db.execute(
                                    """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                                       VALUES (%s, %s, %s) ON CONFLICT (opportunity_id, field_id) DO UPDATE SET field_value = EXCLUDED.field_value""",
                                    (v3_opp_id, v3_field_id, cf_value),
                                )
                                cfs_synced += 1
                    except Exception:
                        pass

                    if deals_updated % 50 == 0:
                        _append_sync_log("info", f"Progress: {deals_updated} deals synced, {tags_synced} tags, {cfs_synced} custom fields...")

                if len(data) < page_size:
                    break
                start += page_size
                _append_sync_log("info", f"Fetched {total_fetched} deals total...")

            _append_sync_log("info", f"Deal tags sync complete: {deals_updated} deals updated, {tags_synced} tags linked, {cfs_synced} custom field values")
            _json_response(self, 200, {"ok": True, "deals_updated": deals_updated, "tags_linked": tags_synced, "custom_fields": cfs_synced, "total_fetched": total_fetched, "log": list(_sync_log)})
        except Exception as e:
            _append_sync_log("error", f"Deal tags sync failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})
        finally:
            _sync_running = False

    def _handle_sync_pull_deals(self) -> None:
        """Pull deals (opportunities) from OnlyOffice CRM with tags, custom fields, and history.

        For new deals: creates the deal and pulls all tags, custom fields, and history.
        For existing deals (matched by title): pulls missing history events.

        Optional filters in payload:
            since: YYYY-MM-DD — OnlyOffice deals updated after this date
            stage_id: int — OnlyOffice stage id filter
            responsible_id: str — OnlyOffice user UUID
        """
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            since_date = payload.get("since", "").strip() or None
            filter_stage_id = payload.get("stage_id")
            filter_responsible_id = payload.get("responsible_id")
            # Supplemental (add-only) mode: create new rows and fill gaps, but
            # never overwrite an existing non-empty value. The admin checkbox
            # sends this flag — it MUST be read here (an unread flag is the
            # exact failure this mode exists to prevent).
            supplemental = bool(payload.get("supplemental"))
            # Explicit overwrite allowlist for supplemental mode: only these
            # columns may use the overwrite form; everything else stays
            # fill-gaps. Unknown names are ignored loudly, never applied.
            SUPPORTED_REFRESH_COLUMNS = {"expected_close_date"}
            requested_refresh = payload.get("refresh_columns") or []
            if not isinstance(requested_refresh, list):
                requested_refresh = [requested_refresh]
            refresh_columns = [c for c in requested_refresh if c in SUPPORTED_REFRESH_COLUMNS]
            unknown_refresh = [c for c in requested_refresh if c not in SUPPORTED_REFRESH_COLUMNS]
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            if supplemental:
                _append_sync_log("info", "Supplemental (add-only) mode: existing values are never overwritten.")
                if refresh_columns:
                    _append_sync_log("info", f"Refresh allowlist (explicit overwrite): {', '.join(sorted(refresh_columns))}")
                if unknown_refresh:
                    _append_sync_log("warn", f"Ignoring unknown refresh_columns entries: {', '.join(map(str, unknown_refresh))}")
            else:
                _append_sync_log("info", "Full mode: OnlyOffice values overwrite existing deal fields.")
                if requested_refresh:
                    _append_sync_log("info", "refresh_columns has no effect in full mode (everything overwrites already).")
            _append_sync_log("info", "Authenticating to OnlyOffice CRM...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return

            # Get existing ID maps
            tag_map = {}
            for row in db.query_dicts("SELECT id, title FROM tag_definitions"):
                tag_map[row["title"].lower()] = row["id"]
            cf_map = {}
            for row in db.query_dicts("SELECT id, field_key FROM custom_field_definitions"):
                numeric_id = row["field_key"].replace("field_", "")
                cf_map[numeric_id] = row["id"]
            stage_map = {}
            for row in db.query_dicts("SELECT id, title, stage_type FROM stages"):
                # Key by TITLE, not by id. The OnlyOffice stage ids and the
                # Sietch stage ids are DIFFERENT namespaces (OO goes to 21+,
                # Sietch to 19), so the only stable join key is the title.
                #
                # The old code keyed by title.lower() but LOOKED UP by
                # crm_stage_id.lower() — so the lookup never matched and every
                # newly-synced deal got stage_id = NULL. (Bug 2026-10-19: 13
                # deals had no stage, no owner, no close date, no bid value.)
                key = str(row["title"] or "").strip().lower()
                if key:
                    stage_map[key] = {
                        "id": row["id"],
                        "stage_type": row.get("stage_type") or 0,
                    }

            # Build title → id map for existing Sietch deals
            existing_deals = db.query_dicts("SELECT id, title FROM opportunities")
            existing_by_title = {d["title"].lower().strip(): d["id"] for d in existing_deals}

            _append_sync_log("info", "Fetching deals from OnlyOffice...")
            # Fetch all opportunities (paginated). since/stage/responsible
            # filters were previously parsed above and then IGNORED (the run
            # always covered every deal). They are honored here now, mirroring
            # the pull-deal-tags handler's parameter names.
            start = 0
            page_size = 500
            all_opps = []
            while True:
                sep = "&" if "?" in "/api/2.0/crm/opportunity/filter" else "?"
                path = f"/api/2.0/crm/opportunity/filter{sep}count={page_size}&startIndex={start}"
                if since_date:
                    path += f"&updatedAfter={since_date}"
                if filter_stage_id is not None:
                    path += f"&stageId={filter_stage_id}"
                if filter_responsible_id:
                    path += f"&responsibleId={filter_responsible_id}"
                data = _unwrap(_crm_api_get(portal, token, path))
                if not isinstance(data, list) or not data:
                    break
                all_opps.extend(data)
                if len(data) < page_size:
                    break
                start += page_size
                _append_sync_log("info", f"Fetched {len(all_opps)} deals so far...")

            _append_sync_log("info", f"Total deals to sync: {len(all_opps)}")
            created = 0
            history_synced = 0
            history_skipped = 0
            skipped = 0
            history_count = 0
            tag_count = 0
            cf_count = 0
            attachments_synced = 0
            # Supplemental-mode reporting: fills vs preservedExisting, plus
            # overwrite counts for full mode and filter skips. Provenance for
            # every number the UI shows at the end of the run.
            sup_fields_filled = 0
            sup_fields_preserved = 0
            full_fields_overwritten = 0
            cf_preserved = 0
            filtered_out = 0
            deals_without_date_key = 0
            start_time = time.time()

            def _classify_field(cur_val, oo_val):
                """Reporting helper: 'fill' (empty locally, OO has a value),
                'diff' (both set and different), or 'noop' (nothing to do)."""
                if oo_val is None:
                    return "noop"
                if cur_val is None:
                    return "fill"
                return "diff" if str(cur_val) != str(oo_val) else "noop"

            # Build user UUID -> Sietch user.id map (Bug #15: author mapping)
            user_id_map = _crm_fetch_user_id_map(portal, token)
            _append_sync_log("info", f"User map: {len(user_id_map)} OnlyOffice users mapped to local")

            for idx, o in enumerate(all_opps):
                if not isinstance(o, dict):
                    continue
                crm_id = str(o.get("id") or o.get("ID") or "")
                title = str(o.get("title") or o.get("Title") or "Untitled").strip()

                # Stage mapping — join on the stage TITLE.
                # The OnlyOffice stage id and the Sietch stage id are different
                # namespaces, so the title is the only reliable join key.
                # stage_type comes from the stage row so the kanban column
                # grouping stays correct (0 = open, 1 = closed, 2 = lost).
                stage_data = o.get("stage") or o.get("Stage") or {}
                stage_title = str(
                    stage_data.get("title") or stage_data.get("Title") or ""
                ).strip().lower()
                stage_info = stage_map.get(stage_title) if stage_title else None
                v3_stage_id = stage_info["id"] if stage_info else None
                v3_stage_type = 0
                if stage_info:
                    try:
                        v3_stage_type = int(stage_info.get("stage_type") or 0)
                    except (TypeError, ValueError):
                        v3_stage_type = 0
                # The CRM's stage payload also carries stageType directly; use
                # it when the local stages row hasn't been annotated yet.
                if stage_data:
                    try:
                        v3_stage_type = int(stage_data.get("stageType") or stage_data.get("StageType") or v3_stage_type)
                    except (TypeError, ValueError):
                        pass

                # Bid value
                bid_value = None
                for bv_key in ("bidValue", "BidValue", "value", "Value"):
                    raw = o.get(bv_key)
                    if raw is not None:
                        try:
                            bid_value = float(raw)
                        except (TypeError, ValueError):
                            pass
                        break

                # Description
                description = str(o.get("description") or o.get("Description") or "").strip() or None

                # Expected close date (due date) — the CRM sends it as an ISO
                # string. The sync never wrote this column (Bug 2026-10-19:
                # 13 newly-synced deals had no expected_close_date).
                expected_close_date = None
                oo_date_keys_present = False
                for dd_key in ("expectedCloseDate", "ExpectedCloseDate", "dueDate"):
                    raw = o.get(dd_key)
                    if raw:
                        oo_date_keys_present = True
                        try:
                            expected_close_date = str(raw)[:10]
                        except Exception:
                            expected_close_date = None
                        if expected_close_date:
                            break
                if not oo_date_keys_present:
                    # Visibility, not silence: a payload shape change upstream
                    # would otherwise look exactly like "no date changes".
                    deals_without_date_key += 1
                    if deals_without_date_key <= 5:
                        _append_sync_log("warn", f"No recognizable due-date key in OO payload for deal '{title}' (crm id {crm_id or '?'})")

                # Responsible user — the CRM's responsible.id is a user UUID.
                # user_id_map (built above) maps OnlyOffice UUID -> Sietch
                # users.id. The sync never wrote this column either.
                v3_responsible_id = None
                resp_data = o.get("responsible") or o.get("Responsible") or {}
                if isinstance(resp_data, dict):
                    resp_crm_id = str(resp_data.get("id") or resp_data.get("ID") or "")
                    if resp_crm_id:
                        v3_responsible_id = user_id_map.get(resp_crm_id)

                # Actual close date — ignore the .NET sentinel
                actual_close_date = None
                actual_close_value = None
                acd = o.get("actualCloseDate")
                if acd and not _crm_is_closure_sentinel(acd):
                    try:
                        actual_close_date = str(acd)[:10]
                    except Exception:
                        actual_close_date = None
                for k in ("actualCloseValue", "ActualCloseValue"):
                    if k in o and o[k] is not None:
                        try:
                            actual_close_value = float(o[k])
                        except Exception:
                            actual_close_value = None
                        break

                # Contact — resolve OnlyOffice contact.id → Sietch contacts.id
                # Bug fix 2026-10-19: sync previously dropped contact_id entirely,
                # so every pulled deal showed no Contact in the UI.
                v3_contact_id = None
                contact_data = o.get("contact") or o.get("Contact") or {}
                if isinstance(contact_data, dict):
                    crm_contact_id = str(contact_data.get("id") or contact_data.get("ID") or "")
                    if crm_contact_id:
                        row = db.query_one(
                            "SELECT id FROM contacts WHERE onlyoffice_id = %s",
                            (crm_contact_id,),
                        )
                        if row:
                            v3_contact_id = row["id"]

                # Find or create opportunity. Prefer onlyoffice_id, fall back to title.
                # The current row's mutable columns ride along so supplemental
                # mode can fill gaps (and both modes can report overwrites).
                # Title fallback re-reads the row: without it every field
                # would look "empty" and supplemental mode would write them all.
                v3_opp_id = None
                current_row = None
                if crm_id:
                    row = db.query_one(
                        "SELECT id, stage_id, bid_value, expected_close_date, actual_close_date, "
                        "actual_close_value, contact_id, responsible_user_id "
                        "FROM opportunities WHERE onlyoffice_id = %s", (crm_id,))
                    if row:
                        v3_opp_id = row["id"]
                        current_row = row
                if not v3_opp_id:
                    v3_opp_id = existing_by_title.get(title.lower().strip())
                    if v3_opp_id:
                        current_row = db.query_one(
                            "SELECT id, stage_id, bid_value, expected_close_date, actual_close_date, "
                            "actual_close_value, contact_id, responsible_user_id "
                            "FROM opportunities WHERE id = %s", (v3_opp_id,))
                # Honor stage/responsible filters locally too: the OO API is
                # expected to filter server-side per the params above (as the
                # sibling pull-deal-tags handler assumes), and this guarantees
                # it regardless of what the remote end honors.
                if filter_stage_id is not None:
                    oo_stage_id = str(stage_data.get("id") or stage_data.get("ID") or "")
                    if oo_stage_id != str(filter_stage_id):
                        filtered_out += 1
                        continue
                if filter_responsible_id:
                    if resp_crm_id != str(filter_responsible_id):
                        filtered_out += 1
                        continue
                is_new = v3_opp_id is None

                if is_new:
                    # Insert new opportunity with the full set of standard deal
                    # fields. Bug fix 2026-10-19: the old INSERT wrote only 7
                    # columns and dropped stage_id / stage_type /
                    # responsible_user_id / expected_close_date, so 13 deals
                    # synced on 2026-10-19 came out with no stage, no owner,
                    # no close date and no bid value.
                    db.execute(
                        """INSERT INTO opportunities
                               (title, description, stage_id, stage_type, bid_value,
                                expected_close_date, actual_close_date, actual_close_value,
                                contact_id, responsible_user_id,
                                created_at, onlyoffice_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                           RETURNING id""",
                        (title, description, v3_stage_id, v3_stage_type, bid_value,
                         expected_close_date, actual_close_date, actual_close_value,
                         v3_contact_id, v3_responsible_id, crm_id or None),
                    )
                    v3_opp_id = db.query_one("SELECT id FROM opportunities WHERE onlyoffice_id = %s", (crm_id,))["id"]
                    existing_by_title[title.lower().strip()] = v3_opp_id
                    created += 1
                else:
                    skipped += 1
                    # Field pairs compared for reporting in both modes.
                    field_pairs = [
                        ("actual_close_date", actual_close_date),
                        ("actual_close_value", actual_close_value),
                        ("stage_id", v3_stage_id),
                        ("bid_value", bid_value),
                        ("expected_close_date", expected_close_date),
                        ("contact_id", v3_contact_id),
                        ("responsible_user_id", v3_responsible_id),
                    ]
                    cur = current_row or {}
                    if supplemental:
                        # Fill gaps only, except explicitly allowlisted
                        # columns which use the overwrite form. stage_type is
                        # deliberately never written here — its 0 default
                        # makes "empty" undetectable, so any write could clobber.
                        set_clauses = []
                        set_params = []
                        for _col, _oo in field_pairs:
                            outcome = _classify_field(cur.get(_col), _oo)
                            if _col in refresh_columns:
                                if outcome == "diff":
                                    full_fields_overwritten += 1
                                elif outcome == "fill":
                                    sup_fields_filled += 1
                                set_clauses.append(f"{_col} = COALESCE(%s, {_col})")
                            else:
                                if outcome == "fill":
                                    sup_fields_filled += 1
                                elif outcome == "diff":
                                    sup_fields_preserved += 1
                                set_clauses.append(f"{_col} = COALESCE({_col}, %s)")
                            set_params.append(_oo)
                        set_params.append(v3_opp_id)
                        try:
                            db.execute(
                                "UPDATE opportunities SET " + ", ".join(set_clauses) + " WHERE id = %s",
                                tuple(set_params),
                            )
                        except Exception:
                            pass
                    else:
                        # Full mode: legacy overwrite, but count real overwrites
                        # so the log shows what changed.
                        for _col, _oo in field_pairs:
                            if _classify_field(cur.get(_col), _oo) == "diff":
                                full_fields_overwritten += 1
                        # Always update the standard deal fields for existing ones
                        # (idempotent). Every field is COALESCE'd so a sync that
                        # returns a deal without a value doesn't wipe what we have.
                        try:
                            db.execute(
                                """UPDATE opportunities
                                   SET actual_close_date = COALESCE(%s, actual_close_date),
                                       actual_close_value = COALESCE(%s, actual_close_value),
                                       stage_id = COALESCE(%s, stage_id),
                                       stage_type = CASE WHEN %s IS NULL THEN stage_type ELSE %s END,
                                       bid_value = COALESCE(%s, bid_value),
                                       expected_close_date = COALESCE(%s, expected_close_date),
                                       contact_id = COALESCE(%s, contact_id),
                                       responsible_user_id = COALESCE(%s, responsible_user_id)
                                   WHERE id = %s""",
                                (actual_close_date, actual_close_value, v3_stage_id,
                                 v3_stage_type, v3_stage_type,
                                 bid_value, expected_close_date,
                                 v3_contact_id, v3_responsible_id, v3_opp_id),
                            )
                        except Exception:
                            pass

                # ── Tags: BOTH new and existing deals (Bug #4 fix) ──
                try:
                    tags_data = _unwrap(_crm_api_get(portal, token, f"/api/2.0/crm/opportunity/tag/{crm_id}"))
                    tag_list = tags_data if isinstance(tags_data, list) else []
                    for t in tag_list:
                        tag_title = ""
                        if isinstance(t, dict):
                            tag_title = str(t.get("title") or t.get("name") or t.get("Title") or "").strip()
                        elif isinstance(t, str):
                            tag_title = t.strip()
                        if tag_title:
                            tag_id = tag_map.get(tag_title.lower())
                            if not tag_id:
                                db.execute("INSERT INTO tag_definitions (title, color) VALUES (%s, %s)", (tag_title, "#6c757d"))
                                tag_id = db.query_one("SELECT id FROM tag_definitions WHERE title = %s", (tag_title,))["id"]
                                tag_map[tag_title.lower()] = tag_id
                                tag_count += 1
                            db.execute(
                                "INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                                (v3_opp_id, tag_id),
                            )
                except Exception:
                    pass

                # ── Custom fields: BOTH new and existing deals (Bug #4 fix) ──
                try:
                    cf_data = _unwrap(_crm_api_get(portal, token, f"/api/2.0/crm/opportunity/{crm_id}/customfield"))
                    cf_list = cf_data if isinstance(cf_data, list) else []
                    for cf in cf_list:
                        if not isinstance(cf, dict):
                            continue
                        cf_crm_id = str(cf.get("id") or cf.get("ID") or "")
                        cf_value = cf.get("value") or cf.get("Value") or cf.get("fieldValue") or ""
                        if isinstance(cf_value, dict):
                            cf_value = cf_value.get("title") or cf_value.get("text") or cf_value.get("value") or ""
                        cf_value = str(cf_value).strip()
                        v3_field_id = cf_map.get(cf_crm_id)
                        if v3_field_id and cf_value:
                            if supplemental:
                                # Insert-only: an existing value is never
                                # overwritten. rowcount distinguishes the two.
                                try:
                                    cf_rc = db.execute(
                                        """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                                        (v3_opp_id, v3_field_id, cf_value),
                                    )
                                except Exception:
                                    cf_rc = 0
                                if cf_rc:
                                    cf_count += 1
                                else:
                                    cf_preserved += 1
                            else:
                                db.execute(
                                    """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                                       VALUES (%s, %s, %s) ON CONFLICT (opportunity_id, field_id) DO UPDATE SET field_value = EXCLUDED.field_value""",
                                    (v3_opp_id, v3_field_id, cf_value),
                                )
                                cf_count += 1
                except Exception:
                    pass

                # ── History events for BOTH new and existing deals ──
                try:
                    hist_data = _unwrap(_crm_api_get(portal, token, f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={crm_id}&count=500"))
                    hist_list = hist_data if isinstance(hist_data, list) else []

                    # Dedup: use crm_event_id when present (faster, exact)
                    existing_event_ids = set()
                    for row in db.query_dicts(
                        "SELECT crm_event_id FROM history_events WHERE opportunity_id = %s AND crm_event_id IS NOT NULL",
                        (v3_opp_id,),
                    ):
                        if row.get("crm_event_id") is not None:
                            existing_event_ids.add(int(row["crm_event_id"]))
                    # Fallback dedup for events that lack crm_event_id
                    existing_keys = set()
                    if not existing_event_ids:
                        for ev in db.query_dicts(
                            "SELECT category_id, content, created_at FROM history_events WHERE opportunity_id = %s",
                            (v3_opp_id,),
                        ):
                            content_prefix = (ev["content"] or "")[:200]
                            created_key = str(ev["created_at"])[:19] if ev["created_at"] else ""
                            existing_keys.add((ev["category_id"], content_prefix, created_key))

                    for h in hist_list:
                        if not isinstance(h, dict):
                            continue
                        cat_data = h.get("category") or h.get("Category") or {}
                        cat_title = str(cat_data.get("title") or cat_data.get("Title") or "Note").strip() if isinstance(cat_data, dict) else "Note"
                        cat_row = db.query_one("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
                        if not cat_row:
                            db.execute("INSERT INTO history_categories (title, is_system) VALUES (%s, FALSE) RETURNING id", (cat_title,))
                            cat_id = db.query_one("SELECT id FROM history_categories WHERE title = %s", (cat_title,))["id"]
                        else:
                            cat_id = cat_row["id"]
                        event_content = str(h.get("content") or h.get("Content") or "").strip() or None
                        event_created = h.get("created") or h.get("Created") or h.get("date") or None
                        create_by = h.get("createBy") or h.get("CreateBy") or h.get("createdBy") or {}
                        crm_author_id = str(create_by.get("id") or create_by.get("ID") or "") if isinstance(create_by, dict) else ""
                        v3_author_id = user_id_map.get(crm_author_id)

                        # crm_event_id-based dedup
                        crm_event_id = int(h.get("id") or 0) or None
                        crm_entity_id = int(h.get("entityId") or 0) or None

                        if crm_event_id and crm_event_id in existing_event_ids:
                            history_skipped += 1
                            continue
                        if not crm_event_id:
                            # Fallback dedup
                            content_prefix = (event_content or "")[:200]
                            created_key = str(event_created)[:19] if event_created else ""
                            event_key = (cat_id, content_prefix, created_key)
                            if event_key in existing_keys:
                                history_skipped += 1
                                continue

                        db.execute(
                            """INSERT INTO history_events
                                   (opportunity_id, category_id, content, created_by, created_at, backdated_created_at,
                                    crm_event_id, crm_entity_id)
                               VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s)""",
                            (v3_opp_id, cat_id, event_content, v3_author_id, event_created, event_created,
                             crm_event_id, crm_entity_id),
                        )
                        history_count += 1
                        if crm_event_id:
                            existing_event_ids.add(crm_event_id)
                        if not is_new:
                            history_synced += 1
                            if not crm_event_id:
                                existing_keys.add((cat_id, (event_content or "")[:200],
                                                   str(event_created)[:19] if event_created else ""))
                except Exception:
                    pass

                # ── History attachments (Bug #5 fix): DOWNLOAD for new events ──
                try:
                    # Find events for this opp that have crm_event_id. We used to
                    # restrict this to events with NO attachments at all, which meant
                    # an event that already had some files was skipped entirely on
                    # later runs -- so its remaining files were downloaded to disk
                    # but never recorded. Select every event and let the per-file
                    # ON CONFLICT below decide what still needs inserting.
                    events_with_files = []
                    # We just inserted them above; query back to find their IDs
                    event_rows = db.query_dicts(
                        """SELECT he.id, he.crm_event_id FROM history_events he
                           WHERE he.opportunity_id = %s AND he.crm_event_id IS NOT NULL""",
                        (v3_opp_id,),
                    )
                    if event_rows:
                        event_id_by_crm = {int(r["crm_event_id"]): r["id"] for r in event_rows}
                        for h in hist_list:
                            if not isinstance(h, dict):
                                continue
                            crm_event_id = int(h.get("id") or 0) or None
                            if not crm_event_id:
                                continue
                            v3_event_id = event_id_by_crm.get(crm_event_id)
                            if not v3_event_id:
                                continue
                            files = h.get("files") or []
                            if not isinstance(files, list):
                                continue
                            for f_item in files:
                                if not isinstance(f_item, dict):
                                    continue
                                f_id = int(f_item.get("id") or 0)
                                if not f_id:
                                    continue
                                f_title = str(f_item.get("title") or f"file_{f_id}").strip()
                                file_size = int(f_item.get("pureContentLength") or f_item.get("size") or 0)
                                file_ext = Path(f_title).suffix.lstrip(".").lower()
                                mime_type = _crm_guess_mime(file_ext)
                                view_url = f_item.get("viewUrl") or f_item.get("webUrl") or ""

                                # Owner
                                owner = f_item.get("createdBy") or f_item.get("CreatedBy") or {}
                                owner_crm_id = str(owner.get("id") or owner.get("ID") or "") if isinstance(owner, dict) else ""
                                v3_uploader_id = user_id_map.get(owner_crm_id) or v3_author_id

                                # Download
                                rel_dir = Path("data") / "history-attachments" / str(v3_opp_id) / str(v3_event_id)
                                safe_name = _crm_safe_filename(f_title)
                                unique_name = f"{f_id}_{safe_name}"
                                local_path = HISTORY_ATTACHMENT_DIR_LIVE / str(v3_opp_id) / str(v3_event_id) / unique_name
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                actual_size = file_size
                                # OnlyOffice exposes files via filehandler.ashx (the
                                # /api/2.0/files/{id} route is admin-only and 403s for bots).
                                url = f"{portal.rstrip('/')}/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={f_id}"
                                raw = _crm_get_raw_bytes(url, headers={"Authorization": token}, timeout=60)
                                if raw:
                                    local_path.write_bytes(raw)
                                    actual_size = len(raw)

                                rel_path = f"data/history-attachments/{v3_opp_id}/{v3_event_id}/{unique_name}"
                                db.execute(
                                    """INSERT INTO history_attachments
                                           (event_id, filename, file_path, file_size, mime_type, uploaded_by,
                                            original_url, crm_file_id)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                       ON CONFLICT (event_id, crm_file_id) WHERE crm_file_id IS NOT NULL DO NOTHING""",
                                    (v3_event_id, f_title, rel_path, actual_size, mime_type,
                                     v3_uploader_id, view_url, f_id),
                                )
                                # Mirror into project_documents. The Documents tab
                                # (GET /api/v2/projects/{id}/documents) reads ONLY
                                # project_documents, so without this row the file is on
                                # disk and in history_attachments but the tab shows
                                # "0 documents". The two tables are 1:1 mirrors of each
                                # other keyed on file_path.
                                db.execute(
                                    """INSERT INTO project_documents
                                           (opportunity_id, title, file_path, file_size, mime_type,
                                            uploaded_by, uploaded_at)
                                       VALUES (%s,%s,%s,%s,%s,%s,NOW())
                                       ON CONFLICT DO NOTHING""",
                                    (v3_opp_id, f_title, rel_path, actual_size, mime_type,
                                     v3_uploader_id),
                                )
                                attachments_synced += 1
                except Exception as e:
                    _append_sync_log("warn", f"Attachment sync for opp {crm_id} failed: {e}")

                # Progress logging every 10 deals
                if (idx + 1) % 10 == 0:
                    elapsed = time.time() - start_time
                    _append_sync_log("info", f"[{idx + 1}/{len(all_opps)}] {created} created, {skipped} existing, {history_count} history events, {attachments_synced} attachments ({int(elapsed)}s elapsed)")

            elapsed = time.time() - start_time
            _append_sync_log("info", f"Deals sync complete: {created} created, {skipped} existing, {history_count} history events ({history_synced} new for existing deals, {history_skipped} deduped), {tag_count} tags, {cf_count} custom fields, {attachments_synced} attachments downloaded ({int(elapsed)}s)")
            if supplemental:
                _append_sync_log("info", f"Supplemental report: {sup_fields_filled} fields gap-filled, {sup_fields_preserved} existing values preserved, {cf_preserved} custom fields preserved, {filtered_out} deals filtered out")
            elif full_fields_overwritten:
                _append_sync_log("info", f"Full mode overwrote {full_fields_overwritten} existing field values")
            if filtered_out:
                _append_sync_log("info", f"{filtered_out} deals skipped by stage/responsible filter")
            if deals_without_date_key:
                _append_sync_log("warn", f"{deals_without_date_key} OO deals carried no recognizable due-date key (expectedCloseDate/ExpectedCloseDate/dueDate)")
            _json_response(self, 200, {"ok": True, "created": created, "skipped": skipped, "tags": tag_count, "custom_fields": cf_count, "history": history_count, "history_synced": history_synced, "history_skipped": history_skipped, "attachments": attachments_synced, "total": len(all_opps), "elapsed_seconds": int(elapsed), "log": list(_sync_log),
                                       "supplemental": supplemental, "refresh_columns": sorted(refresh_columns), "fields_filled": sup_fields_filled, "fields_preserved": sup_fields_preserved, "fields_overwritten": full_fields_overwritten, "custom_fields_preserved": cf_preserved, "filtered_out": filtered_out, "deals_without_date_key": deals_without_date_key})
        except Exception as e:
            _append_sync_log("error", f"Deals sync failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})
        finally:
            _sync_running = False

    def _handle_sync_pull_email_bodies(self) -> None:
        """Import OnlyOffice deal-linked emails as Sietch history events.

        Imported emails are stored as self-contained history notes, not as
        mail_messages rows. They are visible to any user who can open the
        project, and they do not depend on the OnlyOffice filehandler endpoint.
        """
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            _append_sync_log("info", "Authenticating to OnlyOffice CRM...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return

            # Remove any leftover orphan rows from the old wrong import model.
            # Imported emails now live in history_events, not mail_messages.
            try:
                orphans = db.query_dicts(
                    "DELETE FROM mail_messages WHERE account_id IS NULL AND imap_uid LIKE 'onlyoffice:%' RETURNING id"
                )
                if orphans:
                    _append_sync_log("info", f"Cleaned up {len(orphans)} orphan mail_messages rows from old import model")
            except Exception as e:
                _append_sync_log("warn", f"Cleanup of orphan mail_messages rows failed: {e}")

            # Map existing Sietch deals by title
            _append_sync_log("info", "Loading existing deals...")
            existing_deals = db.query_dicts("SELECT id, title FROM opportunities")
            deal_by_title = {d["title"].lower().strip(): d["id"] for d in existing_deals}
            _append_sync_log("info", f"Found {len(deal_by_title)} existing deals")

            # Build index of existing imported email history events for dedup/upsert.
            # We key by (opportunity_id, OnlyOffice message_id). Existing events may be
            # raw OnlyOffice JSON from a previous pull-deals sync, or a prior run of this
            # import already normalized into an email_snapshot.
            _append_sync_log("info", "Indexing existing imported email history events...")
            existing_import_index = {}
            for row in db.query_dicts(
                "SELECT id, opportunity_id, content FROM history_events "
                "WHERE category_id IN (11, 16) AND content LIKE '%message_id%' AND content LIKE '%message_url%'"
            ):
                try:
                    content = json.loads(row["content"] or "{}") if row["content"] else {}
                    if not isinstance(content, dict):
                        continue
                    crm_id = (
                        content.get("crm_message_id")
                        or content.get("message_id")
                        or content.get("messageId")
                        or content.get("MessageId")
                        or content.get("id")
                        or content.get("ID")
                        or ""
                    )
                    opp_id = row["opportunity_id"]
                    if crm_id and opp_id:
                        existing_import_index[(opp_id, str(crm_id))] = row["id"]
                except Exception:
                    continue
            _append_sync_log("info", f"Found {len(existing_import_index)} existing imported email history events")

            start_time = time.time()
            emails_imported = 0
            emails_updated = 0
            emails_with_body = 0
            emails_body_fetch_failed = 0
            email_attachments_downloaded = 0
            deals_processed = 0
            emails_this_batch = 0

            # Fetch all deals from OnlyOffice
            _append_sync_log("info", "Fetching deals from OnlyOffice to import email bodies (with full body fetch)...")
            start = 0
            page_size = 500

            while True:
                sep = "&" if "?" in "/api/2.0/crm/opportunity/filter" else "?"
                path = f"/api/2.0/crm/opportunity/filter{sep}count={page_size}&startIndex={start}"
                data = _unwrap(_crm_api_get(portal, token, path))
                if not isinstance(data, list) or not data:
                    break

                for o in data:
                    if not isinstance(o, dict):
                        continue
                    crm_id = str(o.get("id") or o.get("ID") or "")
                    title = str(o.get("title") or o.get("Title") or "").strip()
                    v3_opp_id = deal_by_title.get(title.lower().strip())

                    if not v3_opp_id:
                        continue

                    deals_processed += 1

                    if deals_processed % 10 == 0:
                        elapsed = time.time() - start_time
                        _append_sync_log("info", f"[{deals_processed}/{len(deal_by_title)}] Processing deals... ({int(elapsed)}s elapsed)")

                    # Fetch history events for this deal
                    email_count_before = emails_imported + emails_updated
                    try:
                        hist_path = f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={crm_id}&count=500"
                        hist_data = _unwrap(_crm_api_get(portal, token, hist_path))
                        hist_list = hist_data if isinstance(hist_data, list) else []

                        email_events_in_deal = 0
                        for h in hist_list:
                            if not isinstance(h, dict):
                                continue
                            content_str = h.get("content") or h.get("Content") or ""
                            if not content_str:
                                continue
                            try:
                                content = json.loads(content_str) if isinstance(content_str, str) else content_str
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if not isinstance(content, dict):
                                continue

                            # Only process items that look like OnlyOffice email snapshots
                            crm_message_id = (
                                content.get("message_id")
                                or content.get("messageId")
                                or content.get("MessageId")
                                or content.get("id")
                                or content.get("ID")
                                or ""
                            )
                            if not crm_message_id:
                                continue

                            email_events_in_deal += 1

                            # Extract email fields (handle both raw mail object and snapshot formats)
                            from_addr = str(content.get("from") or content.get("From") or "").strip()
                            from_addr = from_addr.strip('"').strip("'").strip()
                            to_addr = str(content.get("to") or content.get("To") or "").strip()
                            cc_addr = str(content.get("cc") or content.get("Cc") or "").strip()
                            bcc_addr = str(content.get("bcc") or content.get("Bcc") or "").strip()
                            subject = str(content.get("subject") or content.get("Subject") or "").strip()
                            body_html = str(content.get("body_html") or content.get("htmlBody") or content.get("HtmlBody") or content.get("BodyHtml") or "")
                            body_text = str(content.get("body_text") or content.get("textBody") or content.get("TextBody") or content.get("plainText") or "")
                            introduction = str(content.get("introduction") or content.get("Introduction") or content.get("preview") or content.get("Preview") or "")
                            date_sent = str(content.get("date_created") or content.get("dateCreated") or content.get("date_sent") or content.get("dateSent") or "")

                            if not subject and not body_text and not body_html and not introduction:
                                continue

                            # Fetch full body from filehandler.ashx if we don't already have one
                            if not body_html and not body_text:
                                try:
                                    fh_html, fh_text = _fetch_onlyoffice_mail_body(portal, token, crm_message_id)
                                    if fh_html or fh_text:
                                        body_html = body_html or fh_html
                                        body_text = body_text or fh_text
                                        emails_with_body += 1
                                    else:
                                        emails_body_fetch_failed += 1
                                except Exception as e:
                                    emails_body_fetch_failed += 1

                            # Normalize attachments — email attachments live in
                            # h["files"] (sibling of content), NOT in content["attachments"].
                            # Bug #6 fix: also download the file bytes and store them as
                            # history_attachments rows so the email bodies don't lose their files.
                            attachments = []
                            att_list = h.get("files") or []  # sibling of content, NOT content.get("attachments")
                            downloaded_files = []
                            if isinstance(att_list, list):
                                for a in att_list:
                                    if not isinstance(a, dict):
                                        continue
                                    f_id = int(a.get("id") or 0)
                                    f_title = str(a.get("title") or a.get("name") or (f"file_{f_id}" if f_id else "file")).strip()
                                    file_size = int(a.get("pureContentLength") or a.get("size") or 0)
                                    file_ext = Path(f_title).suffix.lstrip(".").lower()
                                    mime_type = _crm_guess_mime(file_ext)
                                    view_url = a.get("viewUrl") or a.get("webUrl") or ""

                                    attachments.append({
                                        "filename": f_title,
                                        "size_bytes": file_size,
                                        "mime_type": mime_type,
                                        "crm_file_id": f_id,
                                        "view_url": view_url,
                                    })

                                    # Download the file bytes (Bug #6)
                                    if f_id:
                                        # OnlyOffice filehandler.ashx is the bot-accessible
                                        # download endpoint (/api/2.0/files/{id} is admin-only).
                                        url = f"{portal.rstrip('/')}/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={f_id}"
                                        raw = _crm_get_raw_bytes(url, headers={"Authorization": token}, timeout=60)
                                        if raw:
                                            downloaded_files.append({
                                                "crm_file_id": f_id,
                                                "filename": f_title,
                                                "raw": raw,
                                                "size": len(raw),
                                                "mime_type": mime_type,
                                                "view_url": view_url,
                                            })

                            # Build a clean Sietch email_snapshot without any OnlyOffice URL dependency
                            snapshot = {
                                "type": "email_snapshot",
                                "source": "onlyoffice_import",
                                "crm_message_id": str(crm_message_id),
                                "message_id": None,
                                "from": from_addr,
                                "to": to_addr,
                                "cc": cc_addr,
                                "bcc": bcc_addr,
                                "subject": subject,
                                "date_sent": date_sent,
                                "body_html": body_html,
                                "body_text": body_text or introduction,
                                "introduction": introduction,
                                "attachments": attachments,
                            }

                            existing_event_id = existing_import_index.get((v3_opp_id, str(crm_message_id)))
                            v3_event_id = existing_event_id
                            try:
                                if existing_event_id:
                                    db.execute(
                                        "UPDATE history_events SET category_id = %s, content = %s, created_at = COALESCE(%s, created_at), backdated_created_at = %s WHERE id = %s",
                                        (EMAIL_HISTORY_CATEGORY_ID, json.dumps(snapshot), date_sent or None, date_sent or None, existing_event_id),
                                    )
                                    emails_updated += 1
                                else:
                                    v3_event_id = db.insert_returning(
                                        """INSERT INTO history_events (opportunity_id, category_id, content, created_by, created_at, backdated_created_at)
                                           VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()), %s)
                                           RETURNING id""",
                                        (v3_opp_id, EMAIL_HISTORY_CATEGORY_ID, json.dumps(snapshot), None, date_sent or None, date_sent or None),
                                    )
                                    emails_imported += 1
                            except Exception as e:
                                _append_sync_log("warn", f"Failed to import email for deal {crm_id}: {e}")

                            # Persist downloaded email attachments (Bug #6)
                            if v3_event_id and downloaded_files:
                                for df in downloaded_files:
                                    try:
                                        # Avoid duplicates per (event, file) — the same file MAY
                                        # appear on multiple events, so the check must include event_id.
                                        existing_att = db.query_one(
                                            "SELECT id FROM history_attachments WHERE event_id = %s AND crm_file_id = %s",
                                            (v3_event_id, df["crm_file_id"],),
                                        )
                                        if existing_att:
                                            continue
                                        safe_name = _crm_safe_filename(df["filename"])
                                        unique_name = f"{df['crm_file_id']}_{safe_name}"
                                        rel_dir = Path("data") / "history-attachments" / str(v3_opp_id) / str(v3_event_id)
                                        local_path = HISTORY_ATTACHMENT_DIR_LIVE / str(v3_opp_id) / str(v3_event_id) / unique_name
                                        local_path.parent.mkdir(parents=True, exist_ok=True)
                                        local_path.write_bytes(df["raw"])
                                        rel_path = str(rel_dir / unique_name)
                                        db.execute(
                                            """INSERT INTO history_attachments
                                                   (event_id, filename, file_path, file_size, mime_type, uploaded_by,
                                                    original_url, crm_file_id)
                                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                               ON CONFLICT (event_id, crm_file_id) WHERE crm_file_id IS NOT NULL DO NOTHING""",
                                            (v3_event_id, df["filename"], rel_path, df["size"], df["mime_type"],
                                             None, df["view_url"], df["crm_file_id"]),
                                        )
                                        email_attachments_downloaded += 1
                                    except Exception as exc:
                                        _append_sync_log("warn", f"Failed to record email attachment {df.get('crm_file_id')} for deal {crm_id}: {exc}")

                        emails_this_batch = (emails_imported + emails_updated) - email_count_before
                        if email_events_in_deal > 0 and deals_processed % 5 == 0:
                            _append_sync_log("info", f"  Deal #{crm_id} '{title[:35]}': {email_events_in_deal} email events, {emails_this_batch} saved (total: {emails_imported + emails_updated})")

                    except Exception as e:
                        _append_sync_log("warn", f"History fetch failed for deal {crm_id}: {e}")

                    if deals_processed % 25 == 0:
                        elapsed = time.time() - start_time
                        total_done = emails_imported + emails_updated
                        rate = total_done / elapsed if elapsed > 0 else 0
                        _append_sync_log("info", f"Progress: {deals_processed} deals, {total_done} emails imported/updated ({emails_with_body} with full body, {emails_body_fetch_failed} fetch failed, {int(elapsed)}s elapsed)")

                if len(data) < page_size:
                    break
                start += page_size

            elapsed = time.time() - start_time
            _append_sync_log("info", f"Email body import complete: {deals_processed} deals processed, {emails_imported} imported, {emails_updated} updated, {emails_with_body} with full body fetched, {emails_body_fetch_failed} body fetches failed, {email_attachments_downloaded} attachments downloaded ({int(elapsed)}s total)")
            _json_response(self, 200, {"ok": True, "deals_processed": deals_processed, "emails_imported": emails_imported, "emails_updated": emails_updated, "emails_with_body": emails_with_body, "emails_body_fetch_failed": emails_body_fetch_failed, "email_attachments_downloaded": email_attachments_downloaded, "elapsed_seconds": int(elapsed), "log": list(_sync_log)})
        except Exception as e:
            _append_sync_log("error", f"Email body import failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})
        finally:
            _sync_running = False

    def _handle_sync_full_reconcile(self) -> None:
        """Run a complete sync pass from OnlyOffice CRM.

        Bug #12: previously this was a misleading read-only sweep that only
        touched tags/tasks/cf-defs. Now it runs the full chain:
            users → stages → tags → cf-defs → contacts → opportunities +
            history + attachments → tasks.
        Use this for first-time migration. For incremental deltas, run the
        individual /pull-* handlers.
        """
        global _sync_log, _sync_running
        if _sync_running:
            _json_response(self, 409, {"error": "Sync already running"})
            return
        _sync_running = True
        _sync_log.clear()
        try:
            payload = json.loads(_read_body(self) or b"{}")
            portal = payload.get("portal_url", "").strip()
            email = payload.get("email", "").strip()
            password = payload.get("password", "").strip()
            if not portal or not email or not password:
                _json_response(self, 400, {"error": "portal_url, email, and password required"})
                _sync_running = False
                return
            _append_sync_log("info", "Starting full reconcile (users → stages → tags → cf-defs → deals+history → tasks → contacts)...")
            token = _crm_authenticate(portal, email, password)
            if not token:
                _json_response(self, 400, {"error": "Authentication failed"})
                _sync_running = False
                return
            results = {}

            # Users (Bug #12: this was missing)
            _append_sync_log("info", "Syncing users...")
            users_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/people"))
            users_list = users_data if isinstance(users_data, list) else []
            user_count = 0
            for u in users_list:
                if not isinstance(u, dict):
                    continue
                u_email = str(u.get("email") or u.get("Email") or "").strip().lower()
                if not u_email:
                    continue
                u_disp = str(u.get("displayName") or u.get("DisplayName") or "").strip()
                u_first = str(u.get("firstName") or u.get("FirstName") or "").strip()
                u_last = str(u.get("lastName") or u.get("LastName") or "").strip()
                u_admin = bool(u.get("isAdmin") or u.get("IsAdmin"))
                u_id = str(u.get("id") or u.get("ID") or "")
                try:
                    db.execute(
                        """INSERT INTO users (email, external_user_id, display_name, first_name, last_name, is_admin, must_change_password)
                           VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                           ON CONFLICT (email) DO UPDATE SET
                               external_user_id = EXCLUDED.external_user_id,
                               display_name = EXCLUDED.display_name,
                               is_admin = EXCLUDED.is_admin
                           RETURNING id""",
                        (u_email, u_id or None, u_disp or None, u_first or None, u_last or None, u_admin),
                    )
                    user_count += 1
                except Exception:
                    pass
            results["users_upserted"] = user_count
            _append_sync_log("info", f"Users: {user_count} upserted")

            # Stages
            _append_sync_log("info", "Syncing stages...")
            stages_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/opportunity/stage"))
            stages_list = stages_data if isinstance(stages_data, list) else []
            stage_count = 0
            for s in stages_list:
                if not isinstance(s, dict):
                    continue
                s_title = str(s.get("title") or s.get("Title") or "").strip()
                if not s_title:
                    continue
                s_color = str(s.get("color") or s.get("Color") or "#999999").strip()
                s_sort = int(s.get("sortOrder") or s.get("SortOrder") or 0)
                s_type = int(s.get("stageType") or s.get("StageType") or 0)
                s_prob = int(s.get("probability") or s.get("Probability") or s.get("successProbability") or 0)
                try:
                    db.execute(
                        """INSERT INTO stages (title, color, sort_order, stage_type, probability)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (title) DO UPDATE SET color=EXCLUDED.color, sort_order=EXCLUDED.sort_order""",
                        (s_title, s_color, s_sort, s_type, s_prob),
                    )
                    stage_count += 1
                except Exception:
                    pass
            results["stages_upserted"] = stage_count
            _append_sync_log("info", f"Stages: {stage_count} upserted")

            # Tags
            _append_sync_log("info", "Syncing tags...")
            tags_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/opportunity/tag"))
            tags = tags_data if isinstance(tags_data, list) else []
            tag_count = 0
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                title = str(tag.get("title") or tag.get("name") or tag.get("Title") or tag.get("Name") or "").strip()
                color = str(tag.get("color") or tag.get("Color") or "#6c757d").strip()
                if not title:
                    continue
                existing = db.query_one("SELECT id FROM tag_definitions WHERE title = %s", (title,))
                if not existing:
                    db.execute("INSERT INTO tag_definitions (title, color) VALUES (%s, %s)", (title, color))
                    tag_count += 1
            results["tags_created"] = tag_count
            _append_sync_log("info", f"Tags: {tag_count} new")
            # Tasks
            _append_sync_log("info", "Syncing tasks...")
            open_tasks = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=false")) or []
            closed_tasks = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=true")) or []
            all_tasks = (open_tasks if isinstance(open_tasks, list) else []) + (closed_tasks if isinstance(closed_tasks, list) else [])
            task_count = 0
            _admin = _require_admin(self)
            fallback_owner = _admin["id"] if _admin else 1
            owner_map = _fetch_oo_user_owner_map(portal, token)
            for task in all_tasks:
                if not isinstance(task, dict):
                    continue
                title = str(task.get("title") or task.get("Title") or "").strip()
                if not title:
                    continue
                owner_id = _oo_task_owner(task, owner_map, fallback_owner)
                existing = db.query_one(
                    "SELECT id FROM tasks WHERE title = %s AND owner_user_id = %s", (title, owner_id)
                )
                if existing:
                    continue
                desc = str(task.get("description") or task.get("Description") or "").strip() or None
                due = task.get("dueDate") or task.get("DueDate")
                if due:
                    due = str(due)[:10]
                is_closed = bool(task.get("isClosed") or task.get("IsClosed") or False)
                db.execute(
                    "INSERT INTO tasks (title, description, due_date, is_closed, owner_user_id, responsible_user_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())",
                    (title, desc, due, is_closed, owner_id, owner_id),
                )
                task_count += 1
            results["tasks_created"] = task_count
            _append_sync_log("info", f"Tasks: {task_count} new")
            # Opportunities (read-only count)
            _append_sync_log("info", "Counting opportunities...")
            opps_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/opportunity/filter?count=5"))
            opp_count = len(opps_data) if isinstance(opps_data, list) else 0
            results["opportunities_sample"] = opp_count
            _append_sync_log("info", f"Opportunities sample: {opp_count} returned")
            # Custom field definitions
            _append_sync_log("info", "Syncing custom field definitions...")
            cf_data = _unwrap(_crm_api_get(portal, token, "/api/2.0/crm/opportunity/customfield/definitions"))
            cfs = cf_data if isinstance(cf_data, list) else []
            cf_count = 0
            for cf in cfs:
                if not isinstance(cf, dict):
                    continue
                label = str(cf.get("label") or cf.get("title") or cf.get("name") or cf.get("Title") or "").strip()
                field_key = str(cf.get("key") or cf.get("fieldKey") or cf.get("Key") or "").strip()
                if not label or not field_key:
                    continue
                existing = db.query_one("SELECT id FROM custom_field_definitions WHERE field_key = %s", (field_key,))
                if not existing:
                    db.execute(
                        "INSERT INTO custom_field_definitions (field_key, label, field_type) VALUES (%s, %s, %s)",
                        (field_key, label, "text"),
                    )
                    cf_count += 1
            results["custom_fields_created"] = cf_count
            _append_sync_log("info", f"Custom fields: {cf_count} new")
            _append_sync_log("info", "Full reconcile complete")
            _json_response(self, 200, {"ok": True, "results": results, "log": list(_sync_log)})
        except Exception as e:
            _append_sync_log("error", f"Full reconcile failed: {e}")
            _json_response(self, 500, {"error": str(e), "log": list(_sync_log)})
        finally:
            _sync_running = False

    # ── Export / Import / Backup handlers ────────────────────────────────────

    # Tables to export (ordered by FK dependencies)
    _EXPORT_TABLES = [
        ("users", "SELECT * FROM users ORDER BY id"),
        ("password_reset_tokens", None),  # deliberately excluded: short-lived secrets, restoring extends validity
        ("twofa_challenges", None),  # deliberately excluded: ephemeral single-use challenges
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
        ("opportunity_custom_field_values", "SELECT * FROM opportunity_custom_field_values ORDER BY id"),
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
        ("mail_classification_rules", "SELECT * FROM mail_classification_rules ORDER BY id"),
        ("service_feeds", "SELECT * FROM service_feeds ORDER BY id"),
        ("bot_config", "SELECT * FROM bot_config"),
        ("bot_activity_log", "SELECT * FROM bot_activity_log ORDER BY id"),
        ("notifications", "SELECT * FROM notifications ORDER BY id"),
        ("notification_preferences", "SELECT * FROM notification_preferences"),
        ("telegram_notification_log", "SELECT * FROM telegram_notification_log ORDER BY id"),
        ("document_folders", "SELECT * FROM document_folders ORDER BY id"),
        ("project_documents", "SELECT * FROM project_documents ORDER BY id"),
        ("document_activity", "SELECT * FROM document_activity ORDER BY id"),
        ("document_templates", "SELECT * FROM document_templates ORDER BY id"),
        ("project_photos", "SELECT * FROM project_photos ORDER BY id"),
        ("project_photo_folders", "SELECT * FROM project_photo_folders ORDER BY id"),
        ("photo_exif_cache", "SELECT * FROM photo_exif_cache"),
        ("photo_report_templates", "SELECT * FROM photo_report_templates ORDER BY id"),
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
        ("classifier_training_data", "SELECT * FROM classifier_training_data ORDER BY id"),
        ("automation_rules", "SELECT * FROM automation_rules ORDER BY id"),
        ("automation_logs", "SELECT * FROM automation_logs ORDER BY id"),
        ("time_entries", "SELECT * FROM time_entries ORDER BY id"),
    ]
    # Tables mapped to None above are deliberately excluded from export
    # (ephemeral secrets); the export/import loops skip None entries.

    # Sensitive columns to exclude from universal export.
    # NOTE: exports therefore cannot fully restore authentication — see
    # the credential_notice in the manifest; admins must re-enter mail
    # account passwords/OAuth grants and user passwords after a restore.
    _SENSITIVE_COLUMNS = {
        "password_hash", "password_salt", "password_encrypted",
        "smtp_password_encrypted", "oauth_access_token", "oauth_refresh_token",
        "oauth_graph_access_token", "oauth_graph_refresh_token",
        "caldav_password_encrypted", "webhook_secret",
        "token",
    }

    # Universal-export parts (?part= on the export endpoint). Each part is a
    # self-describing ZIP with its own manifest + shared backup_set_id (UTC
    # date), so parts are independently restorable in any order. history
    # blobs (history-attachments/, ~33 GB) are NOT servable over HTTP —
    # use the scheduled sietch-data-*.tar.gz artifact for those.
    EXPECTED_EXPORT_PARTS = ["db-core", "files-documents", "files-state"]
    _EXPORT_PART_FILES: dict[str, list[tuple[str, str]]] = {
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
    # scanner/data lives outside data/ and is archived under the
    # __scanner_data__ alias (files-state part only), mapped back on import.
    # data/backups is deliberately absent everywhere (never nest artifacts).

    def _handle_export_universal(self) -> None:
        """Export a universal ZIP part (?part=db-core|files-documents|files-state|all).

        Default (no/invalid part) = all servable parts. history-attachments
        is never served here — use the scheduled sietch-data-*.tar.gz.
        The ZIP streams from a temp file so large file parts don't OOM.
        """
        try:
            qs = parse_qs(urlparse(self.path).query)
            part = (qs.get("part") or ["all"])[0]
            if part == "files-history":
                _json_response(self, 400, {
                    "error": "history-attachments is too large to serve over HTTP — "
                             "restore it from a scheduled sietch-data-*.tar.gz artifact.",
                })
                return
            if part not in ("all", "db-core", "files-documents", "files-state"):
                part = "all"
            want_tables = part in ("all", "db-core")
            want_files = (
                list(self._EXPORT_PART_FILES.keys())
                if part == "all"
                else ([part] if part in self._EXPORT_PART_FILES else [])
            )

            set_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            manifest: dict[str, Any] = {
                "version": "1.1",
                "part": part,
                "backup_set_id": set_id,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "tables": {},
                "files": {},
                "excluded_areas": [
                    "files-history (history-attachments/): restore from a scheduled "
                    "sietch-data-*.tar.gz artifact, not via browser download.",
                ],
            }
            if want_tables:
                manifest["credential_notice"] = (
                    "Sensitive credential columns were excluded from this export "
                    f"({sorted(self._SENSITIVE_COLUMNS)}). After restoring, re-enter "
                    "mail account passwords / OAuth grants and reset user passwords. "
                    "Tables 'password_reset_tokens' and 'twofa_challenges' were not "
                    "exported (ephemeral secrets). 'sessions' is exported empty."
                )

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
                    tmp_path = tmp.name
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    # Export each table (None entries are deliberately excluded)
                    if want_tables:
                        for table_name, sql in self._EXPORT_TABLES:
                            if sql is None:
                                manifest["tables"][table_name] = {"row_count": 0, "excluded": True}
                                continue
                            try:
                                rows = db.query_dicts(sql)
                                # Strip sensitive columns
                                clean_rows = []
                                for row in rows:
                                    clean = {k: v for k, v in row.items() if k not in self._SENSITIVE_COLUMNS}
                                    # Convert datetime objects to strings for JSON serialization
                                    for k, v in clean.items():
                                        if isinstance(v, datetime):
                                            clean[k] = v.isoformat()
                                    clean_rows.append(clean)
                                manifest["tables"][table_name] = {"row_count": len(clean_rows)}
                                zf.writestr(f"{table_name}.json", json.dumps(clean_rows, default=str, ensure_ascii=False))
                            except Exception as e:
                                manifest["tables"][table_name] = {"row_count": 0, "error": str(e)}

                    # Export linked files from the real on-disk layout.
                    data_dir = Path("data")
                    for file_part in want_files:
                        for subdir, file_list_key in self._EXPORT_PART_FILES[file_part]:
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
                    # Scanner state lives outside data/ — archive under an alias
                    # (files-state part only).
                    if "files-state" in want_files:
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

                    zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str, ensure_ascii=False))

                size = os.path.getsize(tmp_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="sietch-export-{part}-{datetime.now().strftime("%Y-%m-%d")}.zip"')
                self.end_headers()
                with open(tmp_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=1024 * 1024)
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except Exception as e:
            logger.exception("Export failed")
            _json_response(self, 500, {"error": str(e)})

    def _handle_export_backup(self) -> None:
        """Export database as SQL dump via pg_dump, streamed with chunked encoding.

        The whole dump is never buffered in RAM: pg_dump's stdout is read
        in 1 MB chunks and written straight to the response as
        `Transfer-Encoding: chunked` (a 1 MB initial buffer holds errors
        until the first chunk is read, so a 500 is still reachable if
        pg_dump dies before producing any output). Symmetric to the import
        side which already spools to a temp file then `psql -f`s.
        """
        try:
            env = {**os.environ, "PGPASSWORD": os.getenv("DB_PASSWORD", "")}
            proc = subprocess.Popen(
                [
                    "pg_dump",
                    "-h", os.getenv("DB_HOST", "db"),
                    "-p", os.getenv("DB_PORT", "5432"),
                    "-U", os.getenv("DB_USER", "sietch"),
                    "-d", os.getenv("DB_NAME", "sietch_crm"),
                    "--no-owner", "--no-privileges", "-Fp",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            _json_response(self, 500, {"error": "pg_dump not found. Install postgresql-client."})
            return
        except Exception as e:
            logger.exception("SQL export setup failed")
            _json_response(self, 500, {"error": str(e)})
            return

        assert proc.stdout is not None and proc.stderr is not None
        try:
            # Read the first chunk before committing to 200, so a pg_dump
            # failure that produces no output still surfaces as 500.
            READ_CHUNK = 1024 * 1024
            first_chunk = proc.stdout.read(READ_CHUNK)
            if proc.poll() is not None and not first_chunk:
                # pg_dump died and produced nothing — drain stderr and fail.
                err = proc.stderr.read().decode("utf-8", errors="replace")
                _json_response(self, 500, {"error": f"pg_dump failed (exit {proc.returncode}): {err[:500]}"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/sql")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Disposition", f'attachment; filename="sietch-backup-{datetime.now().strftime("%Y-%m-%d")}.sql"')
            self.end_headers()

            def _send_chunk(data: bytes) -> None:
                if not data:
                    return
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            _send_chunk(first_chunk)
            while True:
                nxt = proc.stdout.read(READ_CHUNK)
                if not nxt:
                    break
                _send_chunk(nxt)
            proc.stdout.close()
            # Final 0-length chunk to terminate chunked encoding.
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            # Drain stderr in the background; surface only if pg_dump
            # exited non-zero (the caller has already got the body, so we
            # can only log).
            err_remaining = proc.stderr.read()
            rc = proc.wait(timeout=10)
            if rc != 0 and err_remaining:
                logger.warning("pg_dump exit %s stderr: %s", rc, err_remaining[:500])
        except BrokenPipeError:
            # Client disconnected mid-stream — kill pg_dump and bail.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        except Exception:
            logger.exception("SQL export failed")
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _handle_import_json(self) -> None:
        """Import from a JSON export ZIP."""
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                _json_response(self, 400, {"error": "Expected multipart/form-data"})
                return

            # Parse multipart form data
            boundary = content_type.split("boundary=")[1].strip()
            raw = _read_body(self)
            parts = self._parse_multipart(raw, boundary)

            file_data = None
            for name, value in parts.items():
                if name == "file" and isinstance(value, bytes):
                    file_data = value
                    break

            if not file_data:
                _json_response(self, 400, {"error": "No file uploaded"})
                return

            # Extract and process ZIP
            buf = io.BytesIO(file_data)
            results: dict[str, Any] = {
                "tables_imported": 0,
                "rows_imported": 0,
                # Rows the DB already had. Expected and harmless on a re-import.
                "rows_duplicate": 0,
                # Rows that failed for a reason OTHER than already-exists (FK
                # violation, missing column, ...). Any of these means the restore
                # is incomplete — see `complete` below.
                "rows_failed": 0,
                # Forward references that succeeded on the second pass.
                "rows_deferred_recovered": 0,
                # Back-compat alias = rows_duplicate + rows_failed.
                "rows_skipped": 0,
                "files_copied": 0,
                "errors": [],
                "failure_samples": [],
            }
            # Rows that failed on a foreign-key violation. These are usually
            # forward references: the parent row is inserted later in the same
            # table (contacts.company_id self-references contacts.id) or in a
            # later table. Held here and retried once after every table has
            # been processed, then anything still unresolved is a real failure.
            deferred_fk: list[tuple[str, str, str, tuple]] = []

            with zipfile.ZipFile(buf, "r") as zf:
                # Read manifest
                if "manifest.json" not in zf.namelist():
                    _json_response(self, 400, {"error": "Invalid export: no manifest.json"})
                    return
                manifest = json.loads(zf.read("manifest.json"))

                # Import tables in order (None entries are deliberately excluded)
                for table_name, sql in self._EXPORT_TABLES:
                    if sql is None:
                        continue
                    json_name = f"{table_name}.json"
                    if json_name not in zf.namelist():
                        continue
                    try:
                        rows = json.loads(zf.read(json_name))
                        if not rows:
                            continue
                        columns = list(rows[0].keys())
                        placeholders = ", ".join(["%s"] * len(columns))
                        col_names = ", ".join(columns)
                        conflict = _conflict_target(table_name)
                        inserted = 0
                        duplicate = 0
                        failed = 0
                        for row in rows:
                            raw_values = tuple(row.get(c) for c in columns)
                            try:
                                values = _adapt_row_values(table_name, columns, raw_values)
                                # ON CONFLICT DO NOTHING doesn't raise when it
                                # skips a row, so counting statements here would
                                # report every row as "imported" even on a
                                # no-op re-import. Use the real rowcount.
                                if db.execute(
                                    f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders}){conflict}",
                                    values,
                                ):
                                    inserted += 1
                                else:
                                    duplicate += 1
                            except Exception as exc:
                                # A unique violation means the row is genuinely
                                # already present (expected on a re-import). Any
                                # *other* failure — FK violation, missing column,
                                # type mismatch — means the restore is incomplete
                                # and must not be reported as a clean success.
                                sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
                                if sqlstate == "23505":  # unique_violation
                                    duplicate += 1
                                elif sqlstate == "23503":  # foreign_key_violation
                                    # Likely a forward reference; retry later.
                                    deferred_fk.append((table_name, col_names, placeholders, raw_values))
                                else:
                                    failed += 1
                                    if len(results["failure_samples"]) < 20:
                                        results["failure_samples"].append(
                                            f"{table_name}: sqlstate={sqlstate} {str(exc).strip()[:200]}"
                                        )
                        results["tables_imported"] += 1
                        results["rows_imported"] += inserted
                        results["rows_duplicate"] += duplicate
                        results["rows_failed"] += failed
                        # Back-compat: callers that predate the split still see a
                        # single "skipped" number, now equal to dupes + failures.
                        results["rows_skipped"] += duplicate + failed
                    except Exception as e:
                        results["errors"].append(f"{table_name}: {e}")

                # ── Deferred FK retry ──────────────────────────────────────
                # Rows deferred as forward references (e.g. contacts whose
                # company_id points at a company row that appeared later in
                # the same file) get one second attempt now that every table
                # has been loaded. Whatever still fails is a genuine problem
                # and is counted as rows_failed.
                deferred_recovered = 0
                for table_name, col_names, placeholders, raw_values in deferred_fk:
                    try:
                        db.execute(
                            f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})"
                            + _conflict_target(table_name),
                            _adapt_row_values(table_name, col_names.split(", "), raw_values),
                        )
                        deferred_recovered += 1
                        results["rows_imported"] += 1
                    except Exception as exc:
                        sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
                        if sqlstate == "23505":
                            results["rows_duplicate"] += 1
                        else:
                            results["rows_failed"] += 1
                            if len(results["failure_samples"]) < 20:
                                results["failure_samples"].append(
                                    f"{table_name} (deferred): sqlstate={sqlstate} {str(exc).strip()[:200]}"
                                )
                if deferred_fk:
                    results["rows_deferred_recovered"] = deferred_recovered

                # Copy files (zip-slip guarded; scanner alias mapped back)
                import backup_service
                data_dir = Path("data")
                for info in zf.infolist():
                    if not info.filename.startswith("files/"):
                        continue
                    rel = backup_service.sanitize_archive_member(info.filename)
                    if rel is None:
                        results["errors"].append(f"skipped unsafe path: {info.filename[:120]}")
                        continue
                    if rel.startswith("__scanner_data__/"):
                        target = Path("scanner/data") / rel[len("__scanner_data__/"):]
                    else:
                        target = data_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    results["files_copied"] += 1

                # Advance serial sequences past restored MAX(id) so new rows
                # don't collide with imported keys. Tables without a serial
                # id (uuid/composite/text PKs) raise here and are skipped.
                sequences_reset = 0
                for table_name, sql in self._EXPORT_TABLES:
                    if sql is None:
                        continue
                    try:
                        db.execute(
                            f'SELECT setval(pg_get_serial_sequence(\'"{table_name}"\', \'id\'), '
                            f'(SELECT COALESCE(MAX(id), 1) FROM "{table_name}"))'
                        )
                        sequences_reset += 1
                    except Exception:
                        pass
                results["sequences_reset"] = sequences_reset

                # Surface the export's credential notice so admins know auth
                # secrets must be re-entered after a restore.
                if isinstance(manifest, dict) and manifest.get("credential_notice"):
                    results.setdefault("warnings", []).append(manifest["credential_notice"])

                # Track landed parts per backup set so the console can show
                # which parts of a set are still missing. A db-only restore
                # is fully supported standalone — the missing list is purely
                # informational (file rows will dangle until their parts land).
                if isinstance(manifest, dict):
                    set_id = manifest.get("backup_set_id")
                    part = manifest.get("part", "all-legacy")
                    if set_id:
                        try:
                            results["backup_set"] = backup_service.record_export_part(set_id, part)
                            missing = results["backup_set"]["missing"]
                            if missing:
                                results.setdefault("warnings", []).append(
                                    f"Backup set {set_id} is missing parts: {', '.join(missing)}. "
                                    "File references from restored rows will dangle until those "
                                    "parts are imported (or restore the scheduled data tar)."
                                )
                        except Exception as e:
                            results.setdefault("warnings", []).append(
                                f"Could not record landed part: {e}"
                            )

            # `ok` means "the request completed", NOT "the restore was
            # complete". A non-zero rows_failed means rows were rejected for
            # reasons other than already-exists (FK violations, missing
            # columns, type mismatches), which silently truncated the 2026-10-17
            # droplet restore while still reporting ok:true. `complete` is the
            # flag callers should gate on.
            results["complete"] = results["rows_failed"] == 0 and not results["errors"]
            if not results["complete"]:
                results.setdefault("warnings", []).append(
                    f"{results['rows_failed']} row(s) FAILED to import (not duplicates). "
                    "The restore is INCOMPLETE — see failure_samples. Common causes: "
                    "a seed row from init.sql colliding with production ids, or a "
                    "missing migration."
                )
            _json_response(self, 200, {"ok": True, **results})
        except Exception as e:
            logger.exception("JSON import failed")
            _json_response(self, 500, {"error": str(e)})

    def _handle_import_sql(self) -> None:
        """Import from a SQL backup file.

        Strictness: psql runs with ON_ERROR_STOP=1 and any nonzero exit
        is a failure (a half-applied dump must never report success).
        A pre-restore pg_dump snapshot is taken first so a bad import is
        reversible; its path is returned in the response. The upload is
        spooled to a temp file and fed via `psql -f` instead of decoding
        the whole dump into a Python string (bounds peak RAM — the HTTP
        request body itself is still buffered by _read_body, which is a
        deeper change tracked separately).
        """
        upload_path = None
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                _json_response(self, 400, {"error": "Expected multipart/form-data"})
                return

            boundary = content_type.split("boundary=")[1].strip()
            raw = _read_body(self)
            parts = self._parse_multipart(raw, boundary)

            file_data = None
            for name, value in parts.items():
                if name == "file" and isinstance(value, bytes):
                    file_data = value
                    break

            if not file_data:
                _json_response(self, 400, {"error": "No file uploaded"})
                return

            import backup_service

            # Spool upload to disk in chunks instead of decoding it all.
            with tempfile.NamedTemporaryFile(delete=False, suffix=".sql") as tmp:
                upload_path = tmp.name
                for off in range(0, len(file_data), 1024 * 1024):
                    tmp.write(file_data[off:off + 1024 * 1024])
            file_data = None  # release the upload bytes ASAP

            # Pre-restore snapshot — abort rather than run destructive SQL
            # with no way back. Named sietch-backup-* so the normal
            # retention rotation covers it.
            snap_slug = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
            snapshot_path = Path("data/backups") / f"sietch-backup-restore-snapshot-{snap_slug}.sql.gz"
            try:
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                backup_service._run_pg_dump(snapshot_path)
            except Exception as e:
                _json_response(self, 500, {
                    "error": f"Pre-restore snapshot failed, import aborted (DB untouched): {e}",
                })
                return

            # Execute SQL via psql from the spooled file.
            env = {**os.environ, "PGPASSWORD": os.getenv("DB_PASSWORD", "")}
            proc = subprocess.run(
                [
                    "psql",
                    "-h", os.getenv("DB_HOST", "db"),
                    "-p", os.getenv("DB_PORT", "5432"),
                    "-U", os.getenv("DB_USER", "sietch"),
                    "-d", os.getenv("DB_NAME", "sietch_crm"),
                    "-v", "ON_ERROR_STOP=1",
                    "-f", upload_path,
                ],
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )

            if proc.returncode != 0:
                _json_response(self, 500, {
                    "error": f"psql failed (exit {proc.returncode}): {proc.stderr[:500]}",
                    "snapshot": str(snapshot_path),
                    "hint": "Database may be partially modified — restore the snapshot to roll back.",
                })
                return

            _json_response(self, 200, {"ok": True, "output": proc.stdout[-2000:] if proc.stdout else "", "errors": proc.stderr[-1000:] if proc.stderr else "", "snapshot": str(snapshot_path)})
        except FileNotFoundError:
            _json_response(self, 500, {"error": "psql not found. Install postgresql-client."})
        except Exception as e:
            logger.exception("SQL import failed")
            _json_response(self, 500, {"error": str(e)})
        finally:
            if upload_path:
                try:
                    os.unlink(upload_path)
                except OSError:
                    pass

    def _handle_backup_status(self) -> None:
        """GET backup status."""
        import backup_service
        _json_response(self, 200, backup_service.get_backup_status())

    def _handle_backup_history(self) -> None:
        """GET backup history (newest first)."""
        import backup_service
        qs = parse_qs(urlparse(self.path).query)
        limit = int(qs.get("limit", ["50"])[0] or 50)
        _json_response(self, 200, {"history": backup_service.get_backup_history(limit=limit)})

    def _handle_backup_history_one(self, key: str) -> None:
        """GET one history entry (for the per-run detail drawer)."""
        import backup_service
        # key shape: "backup_history_YYYYMMDDHHMMSS" — raw stored value is JSON.
        try:
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key = %s", (key,)
            )
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})
            return
        if not row or not row.get("value"):
            _json_response(self, 404, {"error": "Not found"})
            return
        try:
            _json_response(self, 200, {"key": key, "entry": json.loads(row["value"])})
        except Exception as e:
            _json_response(self, 500, {"error": f"malformed entry: {e}"})

    def _handle_backup_history_dir_size(self) -> None:
        """GET /api/v2/admin/backup/history-dir-size — cheap size hint
        for the history-attachments dir, so the admin console can warn
        the operator about the ~30 GB blob before triggering a run.
        Bounded with a quick ceiling on files walked so a stale NFS
        doesn't tie up a request thread for minutes.
        """
        from backup_service import HISTORY_ATTACHMENTS_SUBDIR
        hist = Path("data") / HISTORY_ATTACHMENTS_SUBDIR
        total = 0
        if hist.exists():
            for f in hist.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        # Stale handle / perms issue — skip, don't fail the
                        # whole size request. Worst case the hint is just
                        # under-counted.
                        continue
        _json_response(self, 200, {"size": total, "subdir": HISTORY_ATTACHMENTS_SUBDIR})

    def _handle_backup_export_now(self) -> None:
        """POST trigger a JSON export run right now."""
        import backup_service
        try:
            result = backup_service.run_backup()
            _json_response(self, 200, result)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_backup_config_get(self) -> None:
        """GET backup config."""
        import backup_service
        _json_response(self, 200, backup_service.get_backup_status())

    def _handle_backup_config_post(self) -> None:
        """POST save backup config."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
            import backup_service
            # Allow either a list of parts or a comma-separated string.
            raw_parts = payload.get("json_parts")
            if isinstance(raw_parts, str):
                parts = [p.strip() for p in raw_parts.split(",") if p.strip()]
            elif isinstance(raw_parts, list):
                parts = [str(p).strip() for p in raw_parts if str(p).strip()]
            else:
                parts = []
            config = {
                "enabled": bool(payload.get("enabled", True)),
                "interval": int(payload.get("interval", 86400)),
                "retention_days": int(payload.get("retention_days", 7)),
                "json_parts": backup_service.validate_parts(parts),
            }
            backup_service._save_config_to_db(config)
            _json_response(self, 200, {"ok": True, "config": config})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_alternate_smtp_get(self) -> None:
        """GET alternate SMTP config."""
        try:
            row = db.query_one("SELECT value FROM system_settings WHERE key = 'alternate_smtp'")
            config = json.loads(row["value"]) if row and row["value"] else {}
            # Mask password for security
            if config.get("password"):
                config["password"] = "••••••••"
            _json_response(self, 200, config)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_alternate_smtp_post(self) -> None:
        """POST save alternate SMTP config."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
            config = {
                "enabled": bool(payload.get("enabled")),
                "host": str(payload.get("host") or "").strip(),
                "port": int(payload.get("port") or 587),
                "user": str(payload.get("user") or "").strip(),
                "password": str(payload.get("password") or ""),
                "from_name": str(payload.get("from_name") or "").strip(),
                "from_email": str(payload.get("from_email") or "").strip(),
            }
            # Only update password if a new one was provided (not masked)
            if config["password"] == "••••••••":
                row = db.query_one("SELECT value FROM system_settings WHERE key = 'alternate_smtp'")
                if row and row["value"]:
                    old = json.loads(row["value"])
                    config["password"] = old.get("password", "")
                else:
                    config["password"] = ""
            db.execute(
                """INSERT INTO system_settings (key, value, description)
                   VALUES ('alternate_smtp', %s, 'Alternate SMTP config for system emails')
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (json.dumps(config),),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_alternate_smtp_test(self) -> None:
        """POST send test email via alternate SMTP."""
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
            # Use form values if provided, else fall back to saved config
            if payload.get("host"):
                config = payload
            else:
                row = db.query_one("SELECT value FROM system_settings WHERE key = 'alternate_smtp'")
                config = json.loads(row["value"]) if row and row["value"] else {}
            if not config.get("host"):
                _json_response(self, 400, {"error": "Alternate SMTP is not configured. Enter SMTP settings first."})
                return
            from smtp_client import send_email_from_account
            ok, err = send_email_from_account(
                smtp_host=config["host"],
                smtp_port=int(config.get("port", 587)),
                smtp_user=config.get("user", ""),
                smtp_password=config.get("password", ""),
                from_name=config.get("from_name", "Sietch CRM"),
                from_addr=config.get("from_email", ""),
                to_addr=user["email"],
                subject="Test Email — Sietch CRM",
                html_body="""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:40px 20px;">
<tr><td align="center">
<table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
<tr><td style="padding:32px 40px;text-align:center;border-bottom:1px solid #eeeeee;">
  <img src="https://dashboard.publicadjustermidwest.com/assets/sietch-logo-2-nobg2.png" alt="Sietch CRM" height="40" style="display:block;margin:0 auto 16px;">
  <h2 style="margin:0;font-size:20px;color:#333333;">Test Email</h2>
</td></tr>
<tr><td style="padding:32px 40px;">
  <p style="margin:0;font-size:15px;color:#555555;">If you received this, your alternate SMTP configuration is working correctly.</p>
</td></tr>
<tr><td style="padding:20px 40px;background:#f9f9f9;text-align:center;border-top:1px solid #eeeeee;">
  <p style="margin:0;font-size:12px;color:#aaaaaa;">Sietch CRM</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>""",
            )
            if ok:
                _json_response(self, 200, {"ok": True})
            else:
                _json_response(self, 500, {"error": f"SMTP send failed: {err}"})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_daily_quote_get(self) -> None:
        """GET daily quote config."""
        try:
            row = db.query_one("SELECT value FROM system_settings WHERE key = 'daily_quote_config'")
            config = json.loads(row["value"]) if row and row["value"] else {}
            _json_response(self, 200, config)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_daily_quote_post(self) -> None:
        """POST save daily quote config."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
            config = {
                "enabled": bool(payload.get("enabled", True)),
                "source": str(payload.get("source") or "preset"),
                "active_presets": payload.get("active_presets") or [],
                "custom_quotes": payload.get("custom_quotes") or [],
                "imported_quotes": payload.get("imported_quotes") or [],
                "feed_url": str(payload.get("feed_url") or "").strip(),
                "bible_version": str(payload.get("bible_version") or "KJV").strip(),
            }
            db.execute(
                """INSERT INTO system_settings (key, value, description)
                   VALUES ('daily_quote_config', %s, 'Daily quote display configuration')
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (json.dumps(config),),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_daily_quote_live(self) -> None:
        """GET live quote for external sources (API/RSS/Bible). Returns cached quote, refreshes daily."""
        try:
            row = db.query_one("SELECT value FROM system_settings WHERE key = 'daily_quote_config'")
            config = json.loads(row["value"]) if row and row["value"] else {}
            source = config.get("source", "preset")

            # Check cache freshness
            cache_row = db.query_one("SELECT value FROM system_settings WHERE key = 'daily_quote_cache'")
            cache = json.loads(cache_row["value"]) if cache_row and cache_row["value"] else {}
            cached_source = cache.get("source")
            cached_ts = cache.get("timestamp", 0)
            cache_age_hours = (time.time() - cached_ts) / 3600 if cached_ts else 999

            # Return cache if fresh (<24h) and same source
            if cache_age_hours < 24 and cached_source == source and cache.get("quote"):
                _json_response(self, 200, cache["quote"])
                return

            # Fetch fresh quote
            quote = None
            if source == "api":
                quote = self._fetch_zenquotes_quote()
            elif source == "rss":
                quote = self._fetch_rss_quote(config.get("feed_url", ""))
            elif source == "bible":
                quote = self._fetch_bible_quote(config.get("bible_version", "KJV"))

            if quote:
                # Update cache
                new_cache = {"source": source, "quote": quote, "timestamp": time.time()}
                db.execute(
                    """INSERT INTO system_settings (key, value, description)
                       VALUES ('daily_quote_cache', %s, 'Daily quote external fetch cache')
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    (json.dumps(new_cache),),
                )
                _json_response(self, 200, quote)
            else:
                _json_response(self, 200, {"text": "The best time to plant a tree was twenty years ago. The second best time is now.", "author": "Chinese Proverb"})
        except Exception as e:
            _json_response(self, 200, {"text": "The journey of a thousand miles begins with a single step.", "author": "Lao Tzu"})

    def _fetch_zenquotes_quote(self) -> dict | None:
        """Fetch a random quote from zenquotes.io."""
        try:
            req = urllib.request.Request("https://zenquotes.io/api/random", headers={"User-Agent": "SietchCRM/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data and isinstance(data, list) and len(data) > 0:
                    return {"text": data[0].get("q", ""), "author": data[0].get("a", "Unknown")}
        except Exception:
            pass
        return None

    def _fetch_rss_quote(self, feed_url: str) -> dict | None:
        """Fetch a quote from an RSS/Atom feed URL."""
        if not feed_url:
            return None
        try:
            import xml.etree.ElementTree as ET
            req = urllib.request.Request(feed_url, headers={"User-Agent": "SietchCRM/3.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_data = resp.read().decode("utf-8", errors="replace")
            root = ET.fromstring(xml_data)
            # Try RSS 2.0 first
            items = root.findall(".//item")
            if not items:
                # Try Atom
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                items = root.findall(".//atom:entry", ns)
            if items:
                import random
                item = random.choice(items)
                title = item.findtext("title") or ""
                desc = item.findtext("description") or ""
                # Strip HTML tags from description
                desc = re.sub(r"<[^>]+>", "", desc).strip()
                # Try to split "Quote — Author"
                if " — " in desc:
                    parts = desc.split(" — ", 1)
                    return {"text": parts[0].strip(), "author": parts[1].strip()}
                elif " - " in desc:
                    parts = desc.split(" - ", 1)
                    return {"text": parts[0].strip(), "author": parts[1].strip()}
                return {"text": desc or title, "author": "Unknown"}
        except Exception:
            pass
        return None

    def _fetch_bible_quote(self, version: str = "KJV") -> dict | None:
        """Fetch a random Bible verse (Proverbs or Philippians) from bible-api.com."""
        try:
            books = ["Proverbs", "Philippians", "Psalms"]
            import random
            book = random.choice(books)
            # Pick a reasonable chapter range
            max_ch = {"Proverbs": 31, "Philippians": 4, "Psalms": 150}
            chapter = random.randint(1, max_ch.get(book, 31))
            # Get chapter, then pick a random verse
            url = f"https://bible-api.com/{book}+{chapter}?translation={version}"
            req = urllib.request.Request(url, headers={"User-Agent": "SietchCRM/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            verses = data.get("verses", [])
            if verses:
                v = random.choice(verses)
                text = v.get("text", "").strip()
                ref = f"{v.get('book_name', book)} {v.get('chapter', chapter)}:{v.get('verse', '')}"
                return {"text": text, "author": ref}
        except Exception:
            pass
        return None

    def _handle_backup_run(self) -> None:
        """POST trigger immediate backup."""
        try:
            import backup_service
            result = backup_service.run_backup()
            _json_response(self, 200, result)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_backup_notify_prefs_post(self) -> None:
        """POST save backup notification preferences."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
            import backup_service
            prefs = {
                "backup_email_success": bool(payload.get("email_success", True)),
                "backup_email_failure": bool(payload.get("email_failure", True)),
                "backup_telegram_success": bool(payload.get("telegram_success", True)),
                "backup_telegram_failure": bool(payload.get("telegram_failure", True)),
            }
            backup_service._save_notify_prefs(prefs)
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    @staticmethod
    def _parse_multipart(raw: bytes, boundary: str) -> dict:
        """Minimal multipart/form-data parser."""
        result = {}
        sep = f"--{boundary}".encode()
        parts = raw.split(sep)
        for part in parts:
            if not part or part == b"--\r\n" or part == b"--":
                continue
            if b"\r\n\r\n" not in part:
                continue
            header_data, body = part.split(b"\r\n\r\n", 1)
            if body.endswith(b"\r\n"):
                body = body[:-2]
            headers = header_data.decode("utf-8", errors="replace")
            name_match = re.search(r'name="([^"]+)"', headers)
            filename_match = re.search(r'filename="([^"]+)"', headers)
            if name_match:
                name = name_match.group(1)
                if filename_match:
                    result[name] = body
                else:
                    result[name] = body.decode("utf-8", errors="replace")
        return result

    def _handle_document_download(self, doc_id: int) -> None:
        row = db.query_one(
            "SELECT opportunity_id, title, file_path, file_size, mime_type FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,),
        )
        if not row:
            try:
                log_infra_event(
                    "warn",
                    f"Document download 404: doc_id={doc_id} not found",
                    source="documents",
                    method=self.command,
                    path=api_path if 'api_path' in dir() else '/api/v2/documents/{id}',
                )
            except Exception:
                pass
            self.send_error(404)
            return
        file_path = row["file_path"]
        title = row.get("title", "document")
        mime_type = row.get("mime_type")
        full_path = ROOT / file_path
        if not full_path.exists():
            try:
                log_infra_event(
                    "error",
                    f"Document download 404: doc_id={doc_id} db_path={file_path} abs={full_path} exists=False (file missing on disk)",
                    source="documents",
                    method=self.command,
                    path=f"/api/v2/documents/{doc_id}",
                )
            except Exception:
                pass
            self.send_error(404)
            return
        data = full_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{title}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_document_editor_config(self, doc_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        row = db.query(
            "SELECT opportunity_id, title, file_path, file_size, mime_type FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            self.send_error(404)
            return
        opp_id, title, file_path, file_size, mime_type = row
        if not DOCS_PUBLIC_URL:
            _json_response(self, 503, {"error": "Document Server public URL not configured"})
            return
        if not DOCS_JWT_SECRET:
            _json_response(self, 503, {"error": "Document Server JWT secret not configured"})
            return
        file_ext = Path(title).suffix.lstrip(".").lower() or "docx"
        doc_type = _document_type_from_ext(file_ext)
        # Stable key per document id so multiple users opening the same file join
        # the same editing session (OnlyOffice keys the session off document.key).
        # Stable keys + coEditing.mode:"fast" = real-time collaboration working.
        doc_key = f"sietch-doc-{doc_id}"
        # The download token stays short-lived (JWT expiry handled by the verifier)
        # so a leaked key can't be reused indefinitely.
        download_token = _sign_jwt({"id": doc_id, "path": file_path, "ts": int(time.time())}, DOCS_JWT_SECRET)
        # Public URL that Document Server will use to download the file
        download_url = f"{CRM_PUBLIC_URL}/api/v2/documents/{doc_id}?token={download_token}"
        # Diagnostic log so we can correlate "Download failed" in the OnlyOffice
        # editor with what we actually told the Document Server to fetch.
        try:
            log_infra_event(
                "info",
                f"Editor config: doc_id={doc_id} ext={file_ext} size={file_size} url={download_url}",
                source="docserver",
                method=self.command,
                path=f"/api/v2/documents/{doc_id}/editor-config",
            )
        except Exception:
            pass
        editor_mode = "view" if doc_type == "pdf" else "edit"
        # Preview pane iframe forces view-only. Other callers pass nothing.
        qs = parse_qs(urlparse(self.path).query)
        view_override = _parse_view_mode_override(qs.get("mode", [None])[0])
        if view_override == "view":
            editor_mode = "view"
        config = {
            "document": {
                "fileType": file_ext,
                "key": doc_key,
                "title": title,
                "url": download_url,
                "permissions": _build_onlyoffice_permissions(can_edit=(editor_mode == "edit")),
            },
            "documentType": doc_type,
            "editorConfig": {
                "callbackUrl": f"{CRM_PUBLIC_URL}/api/v2/documents/{doc_id}/callback",
                "mode": editor_mode,
                "coEditing": {
                    "mode": "fast",
                    "change": True,
                },
                "lang": "en",
                "region": "en-US",
                "user": {
                    "id": str(user["id"]),
                    "name": user.get("display_name") or user.get("email") or "User",
                },
                "customization": _build_onlyoffice_customization(),
            },
        }
        config["token"] = _sign_jwt(config, DOCS_JWT_SECRET)
        config["docsApiUrl"] = f"{DOCS_PUBLIC_URL}/web-apps/apps/api/documents/api.js"
        # Record activity best-effort. Deduped server-side (60s) so a user reloading
        # the preview iframe doesn't spam the table.
        _record_document_activity(doc_id, user["id"], "opened", dedup_seconds=60)
        _json_response(self, 200, config)

    def _handle_document_activity(self, doc_id: int, qs: dict) -> None:
        """Return recent document activity (last N events of any kind) for the preview pane.

        Query params:
            limit  — max rows (default 20, cap 100)
            action — optional filter ('opened', 'saved', 'renamed', etc.)
            since  — optional ISO timestamp; only events after this time
        """
        user = _require_auth(self)
        if not user:
            return
        try:
            limit_raw = qs.get("limit", ["20"])[0]
            limit = max(1, min(100, int(limit_raw)))
        except Exception:
            limit = 20
        action_filter = qs.get("action", [None])[0]
        since_filter = qs.get("since", [None])[0]
        # Confirm the doc exists + user has access (mirrors the editor config check,
        # so we don't leak IDs to unauthorized callers).
        row = db.query(
            "SELECT 1 FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            self.send_error(404)
            return
        try:
            params = [doc_id]
            action_sql = ""
            since_sql = ""
            if action_filter:
                action_sql = "AND a.action = %s"
                params.append(action_filter)
            if since_filter:
                since_sql = "AND a.created_at >= %s"
                params.append(since_filter)
            params.append(limit)
            rows = db.query_dicts(
                f"""SELECT a.id, a.document_id, a.user_id, a.action, a.meta, a.created_at,
                           u.display_name, u.email, u.avatar_url
                    FROM document_activity a
                    LEFT JOIN users u ON u.id = a.user_id
                    WHERE a.document_id = %s {action_sql} {since_sql}
                    ORDER BY a.created_at DESC
                    LIMIT %s""",
                tuple(params),
            )
        except Exception as exc:
            logger.exception("document activity fetch failed")
            log_infra_event("error", f"document_activity fetch failed: {exc}", source="documents", method=self.command, path="/activity")
            _json_response(self, 500, {"error": "Failed to load activity"})
            return
        events = []
        for r in rows:
            meta = r.get("meta")
            if isinstance(meta, str):
                try:
                    import json as _json
                    meta = _json.loads(meta)
                except Exception:
                    pass
            events.append({
                "id": r["id"],
                "userId": r.get("user_id"),
                "userName": r.get("display_name") or (r.get("email") or "").split("@")[0] or "Unknown",
                "userEmail": r.get("email"),
                "userAvatar": r.get("avatar_url"),
                "action": r["action"],
                "meta": meta,
                "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
            })
        _json_response(self, 200, {"events": events, "count": len(events)})


    def _handle_document_delete(self, doc_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        db.execute(
            "UPDATE project_documents SET is_deleted = TRUE, deleted_at = NOW() WHERE id = %s",
            (doc_id,),
        )
        _json_response(self, 200, {"ok": True})

    def _handle_document_update(self, doc_id: int) -> None:
        """PATCH equivalent: rename, update notes, move to project, or move to folder."""
        user = _require_auth(self)
        if not user:
            return
        row = db.query(
            "SELECT uploaded_by, opportunity_id, company_scope FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Document not found"})
            return
        uploaded_by, opp_id, company_scope = row
        is_owner = uploaded_by == user["id"]
        is_admin = user.get("is_admin")
        if not is_owner and not is_admin:
            _json_response(self, 403, {"error": "Not authorized"})
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        title = body.get("title")
        new_opp_id = body.get("opportunity_id")
        folder_id = body.get("folder_id")
        scope = body.get("scope")  # "personal" | "company" to move out of project
        updates = []
        params = []
        if title is not None:
            updates.append("title = %s")
            params.append(title)
        if new_opp_id is not None:
            updates.append("opportunity_id = %s")
            params.append(new_opp_id)
            updates.append("company_scope = FALSE")
            updates.append("folder_id = NULL")
        if scope is not None:
            if scope == "company":
                updates.append("opportunity_id = NULL")
                updates.append("company_scope = TRUE")
            else:
                updates.append("opportunity_id = NULL")
                updates.append("company_scope = FALSE")
            updates.append("uploaded_by = %s")
            params.append(user["id"])
        if folder_id is not None and new_opp_id is None and scope is None:
            updates.append("folder_id = %s")
            params.append(folder_id)
        if not updates:
            _json_response(self, 400, {"error": "Nothing to update"})
            return
        params.append(doc_id)
        db.execute(f"UPDATE project_documents SET {', '.join(updates)} WHERE id = %s", tuple(params))
        _json_response(self, 200, {"ok": True})

    def _handle_documents_personal(self) -> None:
        """List current user's personal documents in a folder (or root)."""
        user = _require_auth(self)
        if not user:
            return
        try:
            qs = parse_qs(urlparse(self.path).query)
            folder_id = qs.get("folder_id", [None])[0]
            if folder_id:
                folder_id = int(folder_id)
            # Audit 2026-10-14: added limit/offset pagination (default
            # limit=200) so the documents modal doesn't dump every
            # document in the CRM at once. Page is offset-based to
            # match the rest of the app's pagination.
            try:
                limit = int(qs.get("limit", ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            try:
                offset = int(qs.get("offset", ["0"])[0])
            except (TypeError, ValueError):
                offset = 0
            limit = max(1, min(limit, 500))
            offset = max(0, offset)
            # List folders at this level
            if folder_id:
                folder_rows = db.query(
                    """SELECT id, name FROM document_folders
                       WHERE parent_id = %s AND scope = 'personal' AND uploaded_by = %s AND is_deleted = FALSE
                       ORDER BY name""",
                    (folder_id, user["id"]),
                )
            else:
                folder_rows = db.query(
                    """SELECT id, name FROM document_folders
                       WHERE parent_id IS NULL AND scope = 'personal' AND uploaded_by = %s AND is_deleted = FALSE
                       ORDER BY name""",
                    (user["id"],),
                )
            folders = [{"id": r[0], "name": r[1]} for r in folder_rows]
            # List documents at this level — total first (for the
            # pager), then the page.
            count_params = ()
            if folder_id:
                count_params = (user["id"], folder_id)
                count_row = db.query_one(
                    """SELECT COUNT(*) AS cnt FROM project_documents
                       WHERE uploaded_by = %s AND is_deleted = FALSE AND opportunity_id IS NULL AND company_scope = FALSE AND folder_id = %s""",
                    count_params,
                )
            else:
                count_params = (user["id"],)
                count_row = db.query_one(
                    """SELECT COUNT(*) AS cnt FROM project_documents
                       WHERE uploaded_by = %s AND is_deleted = FALSE AND opportunity_id IS NULL AND company_scope = FALSE AND folder_id IS NULL""",
                    count_params,
                )
            total = int(count_row["cnt"]) if count_row else 0
            if folder_id:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE uploaded_by = %s AND is_deleted = FALSE AND opportunity_id IS NULL AND company_scope = FALSE AND folder_id = %s
                       ORDER BY uploaded_at DESC LIMIT %s OFFSET %s""",
                    (user["id"], folder_id, limit, offset),
                )
            else:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE uploaded_by = %s AND is_deleted = FALSE AND opportunity_id IS NULL AND company_scope = FALSE AND folder_id IS NULL
                       ORDER BY uploaded_at DESC LIMIT %s OFFSET %s""",
                    (user["id"], limit, offset),
                )
            docs = [self._doc_row(r) for r in rows]
            _json_response(self, 200, {
                "documents": docs,
                "folders": folders,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as exc:
            logger.exception("documents/personal failed")
            log_infra_event("error", f"documents/personal failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to load personal documents"})

    def _handle_documents_company(self) -> None:
        """List all company-scoped documents in a folder (or root)."""
        user = _require_auth(self)
        if not user:
            return
        try:
            qs = parse_qs(urlparse(self.path).query)
            folder_id = qs.get("folder_id", [None])[0]
            if folder_id:
                folder_id = int(folder_id)
            # Audit 2026-10-14: limit/offset pagination (default 200).
            try:
                limit = int(qs.get("limit", ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            try:
                offset = int(qs.get("offset", ["0"])[0])
            except (TypeError, ValueError):
                offset = 0
            limit = max(1, min(limit, 500))
            offset = max(0, offset)
            # List folders at this level
            if folder_id:
                folder_rows = db.query(
                    """SELECT id, name FROM document_folders
                       WHERE parent_id = %s AND scope = 'company' AND is_deleted = FALSE
                       ORDER BY name""",
                    (folder_id,),
                )
            else:
                folder_rows = db.query(
                    """SELECT id, name FROM document_folders
                       WHERE parent_id IS NULL AND scope = 'company' AND is_deleted = FALSE
                       ORDER BY name""",
                )
            folders = [{"id": r[0], "name": r[1]} for r in folder_rows]
            # Total first, then the page.
            if folder_id:
                count_row = db.query_one(
                    """SELECT COUNT(*) AS cnt FROM project_documents
                       WHERE company_scope = TRUE AND is_deleted = FALSE AND folder_id = %s""",
                    (folder_id,),
                )
            else:
                count_row = db.query_one(
                    """SELECT COUNT(*) AS cnt FROM project_documents
                       WHERE company_scope = TRUE AND is_deleted = FALSE AND folder_id IS NULL""",
                )
            total = int(count_row["cnt"]) if count_row else 0
            if folder_id:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE company_scope = TRUE AND is_deleted = FALSE AND folder_id = %s
                       ORDER BY uploaded_at DESC LIMIT %s OFFSET %s""",
                    (folder_id, limit, offset),
                )
            else:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE company_scope = TRUE AND is_deleted = FALSE AND folder_id IS NULL
                       ORDER BY uploaded_at DESC LIMIT %s OFFSET %s""",
                    (limit, offset),
                )
            docs = [self._doc_row(r) for r in rows]
            _json_response(self, 200, {
                "documents": docs,
                "folders": folders,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as exc:
            logger.exception("documents/company failed")
            log_infra_event("error", f"documents/company failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to load company documents"})

    def _handle_documents_search(self, qs: dict) -> None:
        """Search all non-deleted project documents. Returns results grouped by project."""
        user = _require_auth(self)
        if not user:
            return
        try:
            q = (qs.get("q", [""])[0]).strip().lower()
            project_id = qs.get("project_id", [None])[0]
            if project_id:
                project_id = int(project_id)
            # Audit 2026-10-15: added limit/offset pagination. The frontend
            # already sends them and reads `data.total` (matches the
            # personal/company scope pagination in 12965/13015). The
            # previous version ignored them and returned every matching
            # row, so the projects scope "scrolled infinitely" while
            # the pager never triggered.
            try:
                limit = int(qs.get("limit", ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            try:
                offset = int(qs.get("offset", ["0"])[0])
            except (TypeError, ValueError):
                offset = 0
            limit = max(1, min(limit, 500))
            offset = max(0, offset)

            # ── WHERE / ORDER BY / params for each of the 4 branches ──
            if q and project_id:
                where_clause = "d.opportunity_id = %s AND d.is_deleted = FALSE AND LOWER(d.title) LIKE %s"
                order_clause = "d.uploaded_at DESC"
                base_params = (project_id, f"%{q}%")
            elif q and not project_id:
                where_clause = "d.opportunity_id IS NOT NULL AND d.is_deleted = FALSE AND LOWER(d.title) LIKE %s"
                order_clause = "o.title, d.uploaded_at DESC"
                base_params = (f"%{q}%",)
            elif not q and project_id:
                where_clause = "d.opportunity_id = %s AND d.is_deleted = FALSE"
                order_clause = "d.uploaded_at DESC"
                base_params = (project_id,)
            else:
                where_clause = "d.opportunity_id IS NOT NULL AND d.is_deleted = FALSE"
                order_clause = "o.title, d.uploaded_at DESC"
                base_params = ()

            # ── Total count (unfiltered by pagination) for the pager ──
            count_row = db.query_one(
                f"""SELECT COUNT(*) AS cnt
                      FROM project_documents d
                      JOIN opportunities o ON o.id = d.opportunity_id
                     WHERE {where_clause}""",
                base_params,
            )
            total = int(count_row["cnt"]) if count_row else 0

            # ── Page of rows with LIMIT/OFFSET applied ──
            rows = db.query(
                f"""SELECT d.id, d.title, d.file_path, d.file_size, d.mime_type, d.uploaded_at,
                          d.opportunity_id, d.company_scope, o.title AS opp_title
                     FROM project_documents d
                     JOIN opportunities o ON o.id = d.opportunity_id
                     WHERE {where_clause}
                     ORDER BY {order_clause}
                     LIMIT %s OFFSET %s""",
                tuple(list(base_params) + [limit, offset]),
            )
            # Group by project
            grouped = {}
            for r in rows:
                opp_title = r[-1]
                if opp_title not in grouped:
                    grouped[opp_title] = {"project": opp_title, "projectId": r[7], "documents": []}
                grouped[opp_title]["documents"].append(self._doc_row(r[:-1]))
            _json_response(self, 200, {
                "results": list(grouped.values()),
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as exc:
            logger.exception("documents/search failed")
            log_infra_event("error", f"documents/search failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to search documents"})

    def _handle_projects_simple(self) -> None:
        """Lightweight project list for document move/copy picker. Recent 20."""
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q", [""])[0] or "").strip().lower()
        if q:
            rows = db.query(
                """SELECT o.id, o.title, s.title AS stage_title
                   FROM opportunities o
                   LEFT JOIN stages s ON s.id = o.stage_id
                   WHERE LOWER(o.title) LIKE %s
                   ORDER BY o.title ASC
                   LIMIT 20""",
                (f"%{q}%",),
            )
        else:
            rows = db.query(
                """SELECT o.id, o.title, s.title AS stage_title
                   FROM opportunities o
                   LEFT JOIN stages s ON s.id = o.stage_id
                   ORDER BY o.id DESC
                   LIMIT 20""",
            )
        projects = [{"id": r[0], "title": r[1], "stage": r[2]} for r in rows]
        _json_response(self, 200, {"projects": projects})

    def _handle_document_copy(self, doc_id: int) -> None:
        """Copy a document to a new scope (project, personal, or company)."""
        user = _require_auth(self)
        if not user:
            return
        row = db.query(
            "SELECT title, file_path, file_size, mime_type FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Document not found"})
            return
        title, file_path, file_size, mime_type = row
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        new_opp_id = body.get("opportunity_id")
        new_company = body.get("company_scope", False)
        # Determine storage subdir
        if new_opp_id is not None:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "project" / str(new_opp_id)
            company_scope = False
            uploaded_by = None
        elif new_company:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "company"
            company_scope = True
            uploaded_by = None
        else:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
            company_scope = False
            uploaded_by = user["id"]
        scope_dir.mkdir(parents=True, exist_ok=True)
        src = ROOT / file_path
        safe_name = re.sub(r'[^\w.\-]', '_', title)
        unique_name = f"{int(time.time())}_{safe_name}"
        dst = scope_dir / unique_name
        try:
            dst.write_bytes(src.read_bytes())
        except OSError:
            _json_response(self, 500, {"error": "Failed to copy file"})
            return
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, str(dst.relative_to(ROOT)), file_size, mime_type, uploaded_by, new_opp_id, company_scope),
        )
        _json_response(self, 201, {"id": doc_id, "title": title})

    def _handle_documents_batch_delete(self) -> None:
        """Soft-delete multiple documents."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        if not ids:
            _json_response(self, 400, {"error": "ids required"})
            return
        is_admin = user.get("is_admin")
        # Verify user owns all docs they're trying to delete (unless admin)
        rows = db.query(
            "SELECT id, uploaded_by, company_scope FROM project_documents WHERE id = ANY(%s) AND is_deleted = FALSE",
            (ids,),
        )
        for r in rows:
            doc_id, uploaded_by, company_scope = r[0], r[1], r[2]
            if company_scope and uploaded_by != user["id"] and not is_admin:
                _json_response(self, 403, {"error": f"Not authorized to delete document {doc_id}"})
                return
            if not company_scope and uploaded_by != user["id"] and not is_admin:
                _json_response(self, 403, {"error": f"Not authorized to delete document {doc_id}"})
                return
        db.execute(
            "UPDATE project_documents SET is_deleted = TRUE, deleted_at = NOW() WHERE id = ANY(%s)",
            (ids,),
        )
        _json_response(self, 200, {"ok": True, "count": len(ids)})

    def _handle_documents_batch_move(self) -> None:
        """Move multiple documents to a project, personal, or company scope."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        new_opp_id = body.get("opportunity_id")
        company_scope = body.get("company_scope", False)
        if not ids:
            _json_response(self, 400, {"error": "ids required"})
            return
        is_admin = user.get("is_admin")
        rows = db.query(
            "SELECT id, uploaded_by, company_scope FROM project_documents WHERE id = ANY(%s) AND is_deleted = FALSE",
            (ids,),
        )
        for r in rows:
            doc_id, uploaded_by, cs = r[0], r[1], r[2]
            if uploaded_by != user["id"] and not is_admin:
                _json_response(self, 403, {"error": f"Not authorized to move document {doc_id}"})
                return
        for doc_id in ids:
            row = db.query(
                "SELECT file_path, title FROM project_documents WHERE id = %s AND is_deleted = FALSE",
                (doc_id,), fetch="one",
            )
            if not row:
                continue
            old_path, title = row
            src = ROOT / old_path
            if new_opp_id is not None:
                new_dir = DOCUMENT_STORAGE_PATH / "shared" / "project" / str(new_opp_id)
                new_dir.mkdir(parents=True, exist_ok=True)
                safe_name = re.sub(r'[^\w.\-]', '_', title)
                new_path = new_dir / f"{int(time.time())}_{safe_name}"
                if src.exists():
                    new_path.write_bytes(src.read_bytes())
                db.execute(
                    """UPDATE project_documents
                       SET opportunity_id = %s, company_scope = FALSE, uploaded_by = NULL, file_path = %s, folder_id = NULL
                       WHERE id = %s""",
                    (new_opp_id, str(new_path.relative_to(ROOT)), doc_id),
                )
            else:
                # Move to personal or company (no file move needed — same storage area)
                new_opp_val = "NULL"
                db.execute(
                    """UPDATE project_documents
                       SET opportunity_id = NULL, company_scope = %s, uploaded_by = %s, folder_id = NULL
                       WHERE id = %s""",
                    (company_scope, user["id"], doc_id),
                )
        _json_response(self, 200, {"ok": True, "count": len(ids)})

    def _handle_documents_batch_copy(self) -> None:
        """Copy multiple documents to a new scope."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        ids = body.get("ids", [])
        new_opp_id = body.get("opportunity_id")
        new_company = body.get("company_scope", False)
        if not ids:
            _json_response(self, 400, {"error": "ids required"})
            return
        copied = 0
        for doc_id in ids:
            # Use the single copy handler logic inline
            row = db.query(
                "SELECT title, file_path, file_size, mime_type FROM project_documents WHERE id = %s AND is_deleted = FALSE",
                (doc_id,), fetch="one",
            )
            if not row:
                continue
            title, file_path, file_size, mime_type = row
            if new_opp_id is not None:
                scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "project" / str(new_opp_id)
                company_scope, uploaded_by = False, None
            elif new_company:
                scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "company"
                company_scope, uploaded_by = True, None
            else:
                scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
                company_scope, uploaded_by = False, user["id"]
            scope_dir.mkdir(parents=True, exist_ok=True)
            src = ROOT / file_path
            safe_name = re.sub(r'[^\w.\-]', '_', title)
            unique_name = f"{int(time.time())}_{safe_name}"
            dst = scope_dir / unique_name
            try:
                dst.write_bytes(src.read_bytes())
            except OSError:
                continue
            db.execute(
                """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (title, str(dst.relative_to(ROOT)), file_size, mime_type, uploaded_by, new_opp_id, company_scope),
            )
            copied += 1
        _json_response(self, 200, {"ok": True, "copied": copied})

    def _handle_document_upload_personal(self) -> None:
        """Upload a personal document for the current user."""
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        if not file_info:
            _json_response(self, 400, {"error": "file field required"})
            return
        title = file_info["filename"]
        data = file_info["data"]
        mime_type = file_info.get("content-type") or _guess_mime(title)
        safe_name = re.sub(r'[^\w.\-]', '_', title)
        if not safe_name:
            safe_name = "document"
        scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
        scope_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{int(time.time())}_{safe_name}"
        file_path = scope_dir / unique_name
        file_path.write_bytes(data)
        file_size = len(data)
        # Extract optional folder_id from form data
        folder_id = None
        folder_field = parsed.get("folder_id")
        if folder_field and folder_field.get("data"):
            try:
                folder_id = int(folder_field["data"].decode("utf-8").strip())
            except (ValueError, UnicodeDecodeError):
                folder_id = None
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope, folder_id)
               VALUES (%s, %s, %s, %s, %s, NULL, FALSE, %s) RETURNING id""",
            (title, str(file_path.relative_to(ROOT)), file_size, mime_type, user["id"], folder_id),
        )
        _json_response(self, 201, {
            "id": doc_id, "title": title, "fileSize": file_size,
            "mimeType": mime_type, "editUrl": f"/doc-editor.html?id={doc_id}",
        })

    def _handle_document_upload_company(self) -> None:
        """Upload a company-shared document. Any authenticated user can upload."""
        user = _require_auth(self)
        if not user:
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            _json_response(self, 400, {"error": "Expected multipart/form-data"})
            return
        try:
            parsed = _parse_multipart(_read_body(self), content_type)
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid multipart body: {exc}"})
            return
        file_info = parsed.get("file")
        if not file_info:
            _json_response(self, 400, {"error": "file field required"})
            return
        title = file_info["filename"]
        data = file_info["data"]
        mime_type = file_info.get("content-type") or _guess_mime(title)
        safe_name = re.sub(r'[^\w.\-]', '_', title)
        if not safe_name:
            safe_name = "document"
        scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "company"
        scope_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{int(time.time())}_{safe_name}"
        file_path = scope_dir / unique_name
        file_path.write_bytes(data)
        file_size = len(data)
        # Extract optional folder_id from form data
        folder_id = None
        folder_field = parsed.get("folder_id")
        if folder_field and folder_field.get("data"):
            try:
                folder_id = int(folder_field["data"].decode("utf-8").strip())
            except (ValueError, UnicodeDecodeError):
                folder_id = None
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope, folder_id)
               VALUES (%s, %s, %s, %s, %s, NULL, TRUE, %s) RETURNING id""",
            (title, str(file_path.relative_to(ROOT)), file_size, mime_type, user["id"], folder_id),
        )
        _json_response(self, 201, {
            "id": doc_id, "title": title, "fileSize": file_size,
            "mimeType": mime_type, "editUrl": f"/doc-editor.html?id={doc_id}",
        })

    def _doc_row(self, r: tuple) -> dict:
        """Build document response dict from a project_documents row."""
        # r may include opp_title as last element (for search grouped results)
        doc_id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope = r[:8]
        folder_id = r[8] if len(r) > 8 else None
        result = {
            "id": doc_id,
            "title": title,
            "filePath": file_path,
            "fileSize": file_size,
            "mimeType": mime_type,
            "uploadedAt": uploaded_at.isoformat() if uploaded_at else None,
            "opportunityId": opportunity_id,
            "companyScope": company_scope,
            "folderId": folder_id,
            "editUrl": f"/doc-editor.html?id={doc_id}",
        }
        return result

    def _handle_document_folders_list(self, qs: dict) -> None:
        """List folders. scope=personal|company, optional folder_id for nesting."""
        user = _require_auth(self)
        if not user:
            return
        try:
            scope = qs.get("scope", ["personal"])[0]
            folder_id = qs.get("folder_id", [None])[0]
            if folder_id:
                folder_id = int(folder_id)
            if scope == "personal":
                if folder_id:
                    rows = db.query(
                        """SELECT id, name, parent_id FROM document_folders
                           WHERE parent_id = %s AND scope = 'personal' AND uploaded_by = %s AND is_deleted = FALSE
                           ORDER BY name""",
                        (folder_id, user["id"]),
                    )
                else:
                    rows = db.query(
                        """SELECT id, name, parent_id FROM document_folders
                           WHERE parent_id IS NULL AND scope = 'personal' AND uploaded_by = %s AND is_deleted = FALSE
                           ORDER BY name""",
                        (user["id"],),
                    )
            else:
                if folder_id:
                    rows = db.query(
                        """SELECT id, name, parent_id FROM document_folders
                           WHERE parent_id = %s AND scope = 'company' AND is_deleted = FALSE
                           ORDER BY name""",
                        (folder_id,),
                    )
                else:
                    rows = db.query(
                        """SELECT id, name, parent_id FROM document_folders
                           WHERE parent_id IS NULL AND scope = 'company' AND is_deleted = FALSE
                           ORDER BY name""",
                    )
            folders = [{"id": r[0], "name": r[1], "parentId": r[2]} for r in rows]
            _json_response(self, 200, {"folders": folders})
        except Exception as exc:
            logger.exception("documents/folders failed")
            log_infra_event("error", f"documents/folders failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to load folders"})

    def _handle_document_folders_tree(self, qs: dict) -> None:
        """Return all folders for a scope as a flat list (for building a sidebar tree)."""
        user = _require_auth(self)
        if not user:
            return
        try:
            scope = qs.get("scope", ["personal"])[0]
            if scope == "personal":
                rows = db.query(
                    """SELECT id, name, parent_id FROM document_folders
                       WHERE scope = 'personal' AND uploaded_by = %s AND is_deleted = FALSE
                       ORDER BY name""",
                    (user["id"],),
                )
            else:
                rows = db.query(
                    """SELECT id, name, parent_id FROM document_folders
                       WHERE scope = 'company' AND is_deleted = FALSE
                       ORDER BY name""",
                )
            folders = [{"id": r[0], "name": r[1], "parentId": r[2]} for r in rows]
            _json_response(self, 200, {"folders": folders})
        except Exception as exc:
            logger.exception("documents/folders/tree failed")
            log_infra_event("error", f"documents/folders/tree failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to load folder tree"})

    def _handle_document_folder_create(self) -> None:
        """Create a new folder."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        name = (body.get("name") or "").strip()
        scope = body.get("scope", "personal")
        parent_id = body.get("parent_id")
        if not name:
            _json_response(self, 400, {"error": "Folder name required"})
            return
        if scope not in ("personal", "company"):
            _json_response(self, 400, {"error": "Scope must be personal or company"})
            return
        if scope == "personal":
            folder_id = db.insert_returning(
                """INSERT INTO document_folders (name, scope, uploaded_by, parent_id)
                   VALUES (%s, 'personal', %s, %s) RETURNING id""",
                (name, user["id"], parent_id),
            )
        else:
            folder_id = db.insert_returning(
                """INSERT INTO document_folders (name, scope, parent_id)
                   VALUES (%s, 'company', %s) RETURNING id""",
                (name, parent_id),
            )
        _json_response(self, 201, {"id": folder_id, "name": name})

    def _handle_document_folder_rename(self, folder_id: int) -> None:
        """Rename a folder."""
        user = _require_auth(self)
        if not user:
            return
        row = db.query(
            "SELECT uploaded_by, scope FROM document_folders WHERE id = %s AND is_deleted = FALSE",
            (folder_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Folder not found"})
            return
        uploaded_by, scope = row
        is_admin = user.get("is_admin")
        if scope == "personal" and uploaded_by != user["id"] and not is_admin:
            _json_response(self, 403, {"error": "Not authorized"})
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        name = (body.get("name") or "").strip()
        if not name:
            _json_response(self, 400, {"error": "Folder name required"})
            return
        db.execute("UPDATE document_folders SET name = %s WHERE id = %s", (name, folder_id))
        _json_response(self, 200, {"ok": True})

    def _handle_document_folder_delete(self, folder_id: int) -> None:
        """Delete a folder and all its contents (recursive soft-delete)."""
        user = _require_auth(self)
        if not user:
            return
        row = db.query(
            "SELECT uploaded_by, scope FROM document_folders WHERE id = %s AND is_deleted = FALSE",
            (folder_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Folder not found"})
            return
        uploaded_by, scope = row
        is_admin = user.get("is_admin")
        if scope == "personal" and uploaded_by != user["id"] and not is_admin:
            _json_response(self, 403, {"error": "Not authorized"})
            return
        try:
            # Recursively soft-delete all descendant folders using CTE
            db.execute("""
                WITH RECURSIVE folder_tree AS (
                    SELECT id FROM document_folders WHERE id = %s
                    UNION ALL
                    SELECT df.id FROM document_folders df
                    JOIN folder_tree ft ON df.parent_id = ft.id
                )
                UPDATE document_folders SET is_deleted = TRUE WHERE id IN (SELECT id FROM folder_tree)
            """, (folder_id,))
            # Soft-delete all documents in this folder and its subfolders
            db.execute("""
                WITH RECURSIVE folder_tree AS (
                    SELECT id FROM document_folders WHERE id = %s
                    UNION ALL
                    SELECT df.id FROM document_folders df
                    JOIN folder_tree ft ON df.parent_id = ft.id
                )
                UPDATE project_documents SET is_deleted = TRUE, deleted_at = NOW()
                WHERE folder_id IN (SELECT id FROM folder_tree) AND is_deleted = FALSE
            """, (folder_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as exc:
            logger.exception("documents/folders/%d delete failed", folder_id)
            log_infra_event("error", f"documents/folders/{folder_id} delete failed: {exc}", source="documents", method=self.command, path=api_path)
            _json_response(self, 500, {"error": "Failed to delete folder"})

    def _handle_document_create_blank(self) -> None:
        """Create a blank document from a template (Word or Excel)."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        doc_type = body.get("type", "word")
        title = (body.get("title") or "").strip()
        scope = body.get("scope", "personal")
        folder_id = body.get("folder_id")
        new_opp_id = body.get("opportunity_id")
        if not title:
            _json_response(self, 400, {"error": "Document title required"})
            return
        if doc_type == "word":
            ext = "docx"
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            data = _blank_docx_bytes()
        elif doc_type == "excel":
            ext = "xlsx"
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            data = _blank_xlsx_bytes()
        else:
            _json_response(self, 400, {"error": "type must be word or excel"})
            return
        full_title = f"{title}.{ext}"
        # Determine storage directory
        if new_opp_id is not None:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "project" / str(new_opp_id)
            company_scope = False
            uploaded_by = None
            folder_id = None
        elif scope == "company":
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "company"
            company_scope = True
            uploaded_by = None
        else:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
            company_scope = False
            uploaded_by = user["id"]
        scope_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^\w.\-]', '_', full_title)
        unique_name = f"{int(time.time())}_{safe_name}"
        file_path = scope_dir / unique_name
        file_path.write_bytes(data)
        file_size = len(data)
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope, folder_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (full_title, str(file_path.relative_to(ROOT)), file_size, mime, uploaded_by, new_opp_id, company_scope, folder_id),
        )
        # Diagnostic log: confirms the file is on disk at the path the download
        # handler will read from. Useful when OnlyOffice reports "Download failed"
        # or the editor hangs on a freshly-created blank doc.
        try:
            log_infra_event(
                "info",
                f"Blank doc created id={doc_id} type={doc_type} path={file_path} exists={file_path.exists()} bytes={file_size}",
                source="documents",
                method="POST",
                path="/api/v2/documents/create",
            )
        except Exception:
            pass
        _json_response(self, 201, {
            "id": doc_id, "title": full_title, "fileSize": file_size,
            "mimeType": mime, "editUrl": f"/doc-editor.html?id={doc_id}",
        })

    def _handle_document_command(self, doc_id: int) -> None:
        """Proxy a command (e.g. meta rename) to OnlyOffice Command Service."""
        user = _require_auth(self)
        if not user:
            return
        if not DOCS_PUBLIC_URL or not DOCS_JWT_SECRET:
            _json_response(self, 503, {"error": "Document Server not configured"})
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        row = db.query(
            "SELECT file_path, title FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Document not found"})
            return
        file_path, old_title = row
        key = _sign_jwt({"id": doc_id, "path": file_path, "ts": int(time.time())}, DOCS_JWT_SECRET)
        command = dict(body)
        command["key"] = key
        token = _sign_jwt(command, DOCS_JWT_SECRET)
        command["token"] = token
        try:
            cmd_url = f"{DOCS_PUBLIC_URL}/coauthoring/CommandService.ashx"
            req = urllib.request.Request(
                cmd_url,
                data=json.dumps(command).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read() or b"{}")
            # Only record rename activity when the actual rename command succeeds.
            # Meta command (c=='meta') is what OnlyOffice fires for title changes
            # via the editor's "Rename..." menu (also via doc-editor.html onRequestRename).
            if not result.get("error") and command.get("c") == "meta":
                new_title = (command.get("meta") or {}).get("title")
                if new_title and new_title != old_title:
                    _record_document_activity(
                        doc_id,
                        user["id"],
                        "renamed",
                        meta={"from": old_title, "to": new_title},
                    )
            _json_response(self, 200, result)
        except Exception as exc:
            logger.exception("Document command proxy failed")
            _json_response(self, 502, {"error": f"Command Service error: {exc}"})

    def _handle_document_save_as(self) -> None:
        """Handle Save As from OnlyOffice editor — download file and create new doc."""
        user = _require_auth(self)
        if not user:
            return
        try:
            body = json.loads(_read_body(self))
        except Exception:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        file_url = body.get("url")
        title = (body.get("title") or "").strip()
        source_doc_id = body.get("source_doc_id")
        scope = body.get("scope")
        folder_id = body.get("folder_id")
        new_opp_id = body.get("opportunity_id")
        if not file_url or not title:
            _json_response(self, 400, {"error": "url and title required"})
            return
        # If no explicit scope/opportunity, look up from the source document
        if source_doc_id and not scope and not new_opp_id:
            src = db.query(
                "SELECT opportunity_id, company_scope, folder_id, uploaded_by FROM project_documents WHERE id = %s AND is_deleted = FALSE",
                (source_doc_id,), fetch="one",
            )
            if src:
                new_opp_id = src[0]
                scope = "company" if src[1] else "personal"
                folder_id = src[2] if not new_opp_id else None
        if not scope:
            scope = "personal"
        try:
            data = _download_from_docserver(file_url, timeout=120)
        except Exception as exc:
            _json_response(self, 502, {"error": f"Failed to download file: {exc}"})
            return
        file_ext = Path(title).suffix.lstrip(".").lower() or "docx"
        mime_map = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pdf": "application/pdf",
        }
        mime = mime_map.get(file_ext, "application/octet-stream")
        if new_opp_id is not None:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "project" / str(new_opp_id)
            company_scope = False
            uploaded_by = None
            folder_id = None
        elif scope == "company":
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "company"
            company_scope = True
            uploaded_by = None
        else:
            scope_dir = DOCUMENT_STORAGE_PATH / "shared" / "personal" / str(user["id"])
            company_scope = False
            uploaded_by = user["id"]
        scope_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^\w.\-]', '_', title)
        unique_name = f"{int(time.time())}_{safe_name}"
        file_path = scope_dir / unique_name
        file_path.write_bytes(data)
        file_size = len(data)
        doc_id = db.insert_returning(
            """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id, company_scope, folder_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, str(file_path.relative_to(ROOT)), file_size, mime, uploaded_by, new_opp_id, company_scope, folder_id),
        )
        _json_response(self, 201, {
            "id": doc_id, "title": title, "fileSize": file_size,
            "editUrl": f"/doc-editor.html?id={doc_id}",
        })

    def _handle_document_callback(self, doc_id: int) -> None:
        """OnlyOffice Document Server save callback. Saves updated file if provided."""
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 200, {"error": 0})
            return
        status = payload.get("status")
        # OnlyOffice passes the acting user via payload['users'] (list) or payload['userdata'].
        # We best-effort resolve to a CRM user; falls through to None if we can't.
        acting_user_id = None
        users_field = payload.get("users")
        if isinstance(users_field, list) and users_field:
            candidate = str(users_field[0].get("id") if isinstance(users_field[0], dict) else users_field[0])
            if candidate.isdigit():
                acting_user_id = int(candidate)
        if status in (1, 2, 4):
            url = payload.get("url")
            if url:
                try:
                    data = _download_from_docserver(url)
                    row = db.query(
                        "SELECT file_path FROM project_documents WHERE id = %s AND is_deleted = FALSE",
                        (doc_id,), fetch="one",
                    )
                    if row:
                        file_path = ROOT / row[0]
                        file_path.write_bytes(data)
                        db.execute(
                            "UPDATE project_documents SET file_size = %s WHERE id = %s",
                            (len(data), doc_id),
                        )
                        log_infra_event("info", f"Callback saved doc {doc_id}: {len(data)} bytes (status={status})", source="docserver", method="POST", path="/callback")
                except Exception as exc:
                    log_infra_event("error", f"Callback download failed for doc {doc_id}: {exc}", source="docserver", method="POST", path="/callback")
            # Record 'saved' (status 2 = ready, 4 = saved/copy save).
            # status 1 is "editing" — too noisy; skip.
            if status in (2, 4):
                # Dedup "saved" rows from rapid autosaves: only insert if no save in last 30s.
                # Forcesave also fires this so users do see continuous entries during co-edit;
                # the 30s window keeps that sane while still showing each user's session.
                _record_document_activity(doc_id, acting_user_id, "saved", dedup_seconds=30)
            elif status == 3:
                _record_document_activity(doc_id, acting_user_id, "closed", dedup_seconds=30)
        _json_response(self, 200, {"error": 0})

    # ── Batch Tags ──────────────────────────────────────────────────────────

    def _handle_batch_opportunity_tags(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        ids_raw = (qs.get("ids") or [""])[0]
        if not ids_raw:
            _json_response(self, 400, {"error": "ids parameter required"})
            return
        try:
            ids = [int(x.strip()) for x in ids_raw.split(",") if x.strip()]
        except ValueError:
            _json_response(self, 400, {"error": "Invalid ids"})
            return
        result = {}
        for opp_id in ids:
            rows = db.query(
                """SELECT t.id, t.title, t.color, t.is_system FROM tag_definitions t
                   JOIN opportunity_tags ot ON t.id = ot.tag_id WHERE ot.opportunity_id = %s""",
                (opp_id,),
            )
            result[str(opp_id)] = [{"id": r[0], "title": r[1], "color": r[2], "isSystem": r[3]} for r in rows]
        _json_response(self, 200, result)

    # ── Admin check ─────────────────────────────────────────────────────────

    def _handle_check_admin(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        _json_response(self, 200, {"isAdmin": user.get("is_admin", False)})

    # ── Calendar feed (unchanged) ───────────────────────────────────────────

    def _handle_calendar_feed(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        feed_url = (qs.get("url") or [""])[0].strip()
        if not feed_url:
            _json_response(self, 400, {"error": "url query parameter is required"})
            return
        if not is_allowed_calendar_url(feed_url):
            _json_response(self, 400, {"error": "Invalid or disallowed calendar URL"})
            return
        import urllib.request, urllib.error
        req = urllib.request.Request(
            feed_url,
            headers={"Accept": "text/calendar", "User-Agent": "Sietch-CRM-Calendar/3.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read(_MAX_ICS_BYTES + 1)
        except urllib.error.HTTPError as exc:
            _json_response(self, exc.code, {"error": "Could not fetch calendar feed"})
            return
        except urllib.error.URLError as exc:
            _json_response(self, 502, {"error": str(exc.reason)})
            return
        if len(raw) > _MAX_ICS_BYTES:
            _json_response(self, 413, {"error": "Calendar feed too large"})
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        try:
            payload = parse_ics_calendar(text)
        except Exception as exc:
            _json_response(self, 422, {"error": f"Could not parse calendar: {exc}"})
            return
        # Expand recurring masters so legacy ICS tiles show every instance.
        try:
            from ics_calendar import (
                EXPANSION_FUTURE_DAYS,
                EXPANSION_PAST_DAYS,
                expand_calendar_events,
            )
            _today = datetime.now(timezone.utc).date()
            payload["events"] = expand_calendar_events(
                payload.get("events", []),
                _today - timedelta(days=EXPANSION_PAST_DAYS),
                _today + timedelta(days=EXPANSION_FUTURE_DAYS),
            )
        except Exception as exc:
            print(f"[WARN] ICS expansion failed: {exc}", file=sys.stderr)
        _json_response(self, 200, payload)

    # ── User Profile (local store, unchanged) ───────────────────────────────

    def _handle_user_profile_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        profile = load_user_profile("sietch", str(user["id"]))
        _json_response(self, 200, profile)

    def _handle_user_profile_put(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if not isinstance(payload, dict):
            _json_response(self, 400, {"error": "Profile object is required"})
            return
        profile = save_user_profile("sietch", str(user["id"]), payload)
        _json_response(self, 200, {"ok": True, **profile})

    def _handle_dashboard_notes_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        profile = load_user_profile("sietch", str(user["id"]))
        _json_response(self, 200, {"tiles": profile.get("notesTiles", [])})

    def _handle_dashboard_notes_put(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        tiles = payload.get("tiles")
        if not isinstance(tiles, list):
            _json_response(self, 400, {"error": "tiles array is required"})
            return
        existing = load_user_profile("sietch", str(user["id"]))
        existing["notesTiles"] = tiles
        save_user_profile("sietch", str(user["id"]), existing)
        _json_response(self, 200, {"ok": True, "tiles": tiles})

    # ── Event Log (local store, unchanged) ──────────────────────────────────

    def _handle_event_log_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        events = load_event_log("sietch", str(user["id"]))
        _json_response(self, 200, {"events": events})

    def _handle_event_log_put(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        entries = payload.get("events")
        if not isinstance(entries, list):
            _json_response(self, 400, {"error": "events array is required"})
            return
        events = append_event_log("sietch", str(user["id"]), entries)
        _json_response(self, 200, {"ok": True, "events": events})

    def _handle_event_log_users(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        users = list_users_with_logs("sietch")
        _json_response(self, 200, {"users": users})

    def _handle_event_log_admin_get(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        target_user = (qs.get("userId") or [""])[0]
        if not target_user:
            _json_response(self, 400, {"error": "userId is required"})
            return
        events = load_event_log("sietch", target_user)
        _json_response(self, 200, {"events": events})

    # ── Bot Endpoints (DB-backed, no CRM proxy needed) ──────────────────────

    # ── Admin Bot Settings ─────────────────────────────────────────────────

    def _handle_admin_bot_settings_get(self) -> None:
        """GET /api/v2/admin/bot-settings — return bot config (token masked)."""
        user = _require_admin(self)
        if not user:
            return
        cfg = _get_bot_config()
        safe = dict(cfg)
        if safe.get("token"):
            safe["token"] = "••••••" + safe["token"][-4:] if len(safe["token"]) > 4 else "••••••"
        _json_response(self, 200, safe)

    def _handle_admin_bot_settings_put(self) -> None:
        """PUT /api/v2/admin/bot-settings — save bot config, increment configVersion."""
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        try:
            existing = db.query_one("SELECT value FROM bot_config WHERE key = 'settings'")
            if existing and existing.get("value"):
                cfg = existing["value"] if isinstance(existing["value"], dict) else json.loads(str(existing["value"]))
            else:
                cfg = {}
            # Merge incoming fields (don't overwrite token with mask)
            for k in ("botUsername", "botLink", "maxFileSizeMb", "pollInterval", "enabled", "allowCustomerUploads", "welcomeMessage"):
                if k in payload:
                    cfg[k] = payload[k]
            if "token" in payload and payload["token"] and not payload["token"].startswith("•••"):
                cfg["token"] = payload["token"]
            cfg["configVersion"] = cfg.get("configVersion", 0) + 1
            db.execute(
                """INSERT INTO bot_config (key, value, updated_by)
                   VALUES ('settings', %s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by""",
                (json.dumps(cfg), user.get("id")),
            )
            _invalidate_bot_config_cache()
            _json_response(self, 200, {"ok": True, "configVersion": cfg["configVersion"]})
        except Exception:
            logger.exception("Failed to save bot settings")
            _json_response(self, 500, {"error": "Failed to save bot settings"})

    def _handle_admin_bot_status_get(self) -> None:
        """GET /api/v2/admin/bot-status — bot health status."""
        user = _require_admin(self)
        if not user:
            return
        cfg = _get_bot_config()
        # Check if bot process is running (look for heartbeat in activity log)
        running = False
        last_heartbeat = None
        error_count = 0
        recent_errors = []
        try:
            hb = db.query_one(
                "SELECT created_at FROM bot_activity_log WHERE action = 'heartbeat' ORDER BY created_at DESC LIMIT 1"
            )
            if hb:
                last_heartbeat = hb["created_at"].isoformat() if hb.get("created_at") else None
                # Consider running if heartbeat within last 120s
                # created_at is TIMESTAMP (naive), so compare with naive utcnow
                from datetime import datetime, timedelta
                if hb.get("created_at") and hb["created_at"] > datetime.utcnow() - timedelta(seconds=120):
                    running = True
            err_rows = db.query(
                "SELECT COUNT(*) FROM bot_activity_log WHERE is_error = TRUE AND created_at > NOW() - INTERVAL '24 hours'"
            )
            error_count = err_rows[0][0] if err_rows else 0
            recent = db.query(
                """SELECT action, detail, created_at FROM bot_activity_log
                   WHERE is_error = TRUE ORDER BY created_at DESC LIMIT 10"""
            )
            recent_errors = [
                {"action": e[0], "detail": e[1], "time": e[2].isoformat() if e[2] else ""}
                for e in recent
            ]
        except Exception:
            pass
        _json_response(self, 200, {
            "running": running and cfg.get("enabled", True),
            "enabled": cfg.get("enabled", True),
            "lastHeartbeat": last_heartbeat,
            "errorCount": error_count,
            "recentErrors": recent_errors,
            "configVersion": cfg.get("configVersion", 0),
            "botUsername": cfg.get("botUsername", ""),
        })

    def _handle_admin_bot_activity_get(self, qs: dict) -> None:
        """GET /api/v2/admin/bot-activity — paginated activity log."""
        user = _require_admin(self)
        if not user:
            return
        limit = min(int((qs.get("limit") or ["50"])[0]), 200)
        offset = int((qs.get("offset") or ["0"])[0])
        action_filter = (qs.get("action") or [""])[0].strip()
        where = []
        params: list = []
        if action_filter:
            if action_filter == "errors":
                where.append("is_error = TRUE")
            elif action_filter == "uploads":
                where.append("action = 'file_uploaded'")
            elif action_filter == "user":
                where.append("action IN ('search', 'note_added', 'file_uploaded', 'code_verified', 'command')")
            else:
                # Direct action match (e.g. "note_added", "heartbeat", "start", etc.)
                where.append("action = %s")
                params.append(action_filter)
            where_sql = "WHERE " + " AND ".join(where)
        else:
            # Default: exclude heartbeats from the log (status bar shows last heartbeat)
            where_sql = "WHERE action != 'heartbeat'"
        try:
            rows = db.query(
                f"""SELECT chat_id, user_name, action, detail, project_id, is_error, created_at
                    FROM bot_activity_log {where_sql}
                    ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            activity = [
                {
                    "chatId": r[0], "userName": r[1] or "", "action": r[2],
                    "detail": r[3] or "", "projectId": r[4],
                    "isError": bool(r[5]), "time": r[6].isoformat() if r[6] else "",
                }
                for r in rows
            ]
            _json_response(self, 200, {"activity": activity, "offset": offset + len(activity)})
        except Exception:
            logger.exception("Failed to load bot activity")
            _json_response(self, 500, {"error": "Failed to load activity"})

    def _handle_admin_bot_restart(self) -> None:
        """POST /api/v2/admin/bot-restart — signal bot to restart."""
        user = _require_admin(self)
        if not user:
            return
        try:
            # Increment configVersion to trigger bot self-restart on next poll
            existing = db.query_one("SELECT value FROM bot_config WHERE key = 'settings'")
            if existing and existing.get("value"):
                cfg = existing["value"] if isinstance(existing["value"], dict) else json.loads(str(existing["value"]))
            else:
                cfg = {}
            cfg["configVersion"] = cfg.get("configVersion", 0) + 1
            cfg["restartRequested"] = True
            db.execute(
                """INSERT INTO bot_config (key, value) VALUES ('settings', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (json.dumps(cfg),),
            )
            _invalidate_bot_config_cache()
            # Also log the restart request
            try:
                db.execute(
                    "INSERT INTO bot_activity_log (action, detail) VALUES ('config_changed', 'Admin requested bot restart')"
                )
            except Exception:
                pass
            # Try systemctl restart (works when server runs as same user with sudo, or as root)
            restarted = False
            try:
                import subprocess
                result = subprocess.run(
                    ["systemctl", "restart", "crm-telegram-bot"],
                    capture_output=True, text=True, timeout=10,
                )
                restarted = result.returncode == 0
            except Exception:
                pass
            msg = "Bot restarted via systemctl." if restarted else "Restart signal sent. Bot will restart on next config poll."
            _json_response(self, 200, {"ok": True, "message": msg, "restarted": restarted})
        except Exception:
            logger.exception("Failed to signal bot restart")
            _json_response(self, 500, {"error": "Failed to signal restart"})

    def _handle_bot_send_document(self) -> None:
        """POST /api/v2/admin/bot/send-document — send document to Telegram user."""
        user = _require_admin(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        doc_id = payload.get("docId")
        chat_id = payload.get("chatId")
        caption = str(payload.get("caption") or "").strip()
        if not doc_id or not chat_id:
            _json_response(self, 400, {"error": "docId and chatId are required"})
            return
        try:
            doc = db.query_one(
                "SELECT id, title, file_path, mime_type, file_size, opportunity_id FROM project_documents WHERE id = %s AND is_deleted = FALSE",
                (int(doc_id),),
            )
            if not doc:
                _json_response(self, 404, {"error": "Document not found"})
                return
            file_path = doc.get("file_path", "")
            if not file_path or not os.path.isfile(file_path):
                _json_response(self, 404, {"error": "Document file not found on disk"})
                return
            file_size = os.path.getsize(file_path)
            if file_size > 50 * 1024 * 1024:
                _json_response(self, 400, {"error": "File exceeds 50 MB Telegram limit"})
                return
            import httpx as _httpx
            bot_token = _get_bot_token()
            if not bot_token:
                _json_response(self, 500, {"error": "TELEGRAM_BOT_TOKEN not configured"})
                return
            url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
            file_title = doc.get("title", "document")
            with open(file_path, "rb") as f:
                resp = _httpx.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption or file_title},
                    files={"document": (file_title, f, doc.get("mime_type", "application/octet-stream"))},
                    timeout=30,
                )
            data = resp.json()
            if data.get("ok"):
                try:
                    db.execute(
                        "INSERT INTO bot_activity_log (chat_id, user_name, action, detail, project_id) VALUES (%s, %s, 'document_sent', %s, %s)",
                        (chat_id, user.get("display_name", ""), f"Sent {file_title}", doc.get("opportunity_id")),
                    )
                except Exception:
                    pass
                _json_response(self, 200, {"ok": True, "messageId": data["result"]["message_id"]})
            else:
                _json_response(self, 502, {"error": data.get("description", "Telegram API error")})
        except Exception as exc:
            logger.exception("Failed to send Telegram document")
            _json_response(self, 500, {"error": str(exc)})

    def _handle_bot_customers_list(self) -> None:
        user = _require_admin(self)
        if not user:
            return
        mappings = list_mappings("sietch")
        pending = get_pending_codes("sietch")
        _json_response(self, 200, {"mappings": mappings, "pendingCodes": pending})

    def _handle_bot_customers_post_put(self, method: str) -> None:
        api_path = urlparse(self.path).path
        if api_path == "/api/bot-customers/generate-code" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            contact_id = payload.get("contactId")
            contact_name = str(payload.get("contactName") or "").strip()
            notes_category_id = payload.get("notesCategoryId")
            nickname = str(payload.get("nickname") or "").strip()
            employee = payload.get("employee", False)
            user_id = payload.get("userId")
            if not employee and not contact_id:
                _json_response(self, 400, {"error": "contactId is required"})
                return
            result = generate_code("sietch", int(contact_id) if contact_id else None,
                                   contact_name, int(notes_category_id) if notes_category_id else None,
                                   nickname, employee=bool(employee),
                                   user_id=int(user_id) if user_id else None)
            _json_response(self, 200, result)
            return
        if api_path == "/api/bot-customers/cancel-code" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            contact_id = payload.get("contactId")
            code = str(payload.get("code") or "").strip()
            if contact_id:
                ok = cancel_code("sietch", int(contact_id))
            elif code:
                ok = cancel_code_by_value("sietch", code)
            else:
                _json_response(self, 400, {"error": "contactId or code is required"})
                return
            _json_response(self, 200, {"ok": ok})
            return
        if api_path == "/api/bot-customers/verify-code" and method == "POST":
            bot_token = _get_bot_token()
            if not bot_token:
                _json_response(self, 503, {"error": "TELEGRAM_BOT_TOKEN not configured"})
                return
            auth_header = self.headers.get("Authorization", "").strip()
            if auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            code = str(payload.get("code") or "").strip()
            chat_id = payload.get("chatId")
            portal = str(payload.get("portal") or "sietch").strip()
            if not code or not chat_id:
                _json_response(self, 400, {"error": "code and chatId are required"})
                return
            mapping = verify_code(portal, code)
            if mapping is None:
                _json_response(self, 404, {"error": "Invalid or expired code"})
                return
            if isinstance(mapping, str):
                _json_response(self, 400, {"error": mapping})
                return
            set_verify_chat_id(portal, mapping["contactId"], int(chat_id))
            _json_response(self, 200, mapping)
            return
        if api_path == "/api/bot-customers/mapping" and method == "DELETE":
            user = _require_admin(self)
            if not user:
                return
            qs = parse_qs(urlparse(self.path).query)
            chat_id_raw = (qs.get("chatId") or [""])[0]
            if chat_id_raw:
                try:
                    ok = remove_mapping_by_chat("sietch", int(chat_id_raw))
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "Invalid chatId"})
                    return
                _json_response(self, 200, {"ok": ok})
                return
            _json_response(self, 400, {"error": "chatId is required"})
            return
        if api_path == "/api/bot-customers/nickname" and method == "PUT":
            user = _require_admin(self)
            if not user:
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            contact_id = payload.get("contactId")
            nickname = str(payload.get("nickname") or "").strip()
            ok = set_nickname("sietch", int(contact_id) if contact_id else None, nickname)
            _json_response(self, 200, {"ok": ok})
            return
        # ── Bot: set allowed projects for a customer mapping ───────────
        if api_path == "/api/bot-customers/projects" and method == "PUT":
            user = _require_admin(self)
            if not user:
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            contact_id = payload.get("contactId")
            project_ids = payload.get("projectIds", [])
            if contact_id is None:
                _json_response(self, 400, {"error": "contactId is required"})
                return
            ok = set_allowed_projects("sietch", int(contact_id), [int(p) for p in project_ids])
            _json_response(self, 200, {"ok": ok})
            return
        self.send_error(404)

    def _handle_bot_api_get(self) -> None:
        api_path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        bot_token = _get_bot_token()
        auth_header = self.headers.get("Authorization", "").strip()
        if not bot_token or auth_header != f"Bearer {bot_token}":
            _json_response(self, 403, {"error": "Forbidden"})
            return

        if api_path == "/api/bot/me":
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            track_request("sietch", int(raw_chat), "me")
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping:
                _json_response(self, 404, {"error": "Not found"})
                return
            _json_response(self, 200, mapping)
            return

        if api_path == "/api/bot/deals":
            # SECURITY: employee/customer is decided HERE, from the stored chatId
            # mapping, never from a client-supplied flag. The bot token authenticates
            # the bot *process*, not an individual chat, so it is not an authorization
            # boundary. Previously ?employee=true and ?contactId= were trusted from the
            # wire, which let a crafted request drop the contact filter and return every
            # opportunity across every contact. A caller still sending employee=true
            # (older bot build) is simply ignored -- the mapping is authoritative.
            raw_chat_deals = (qs.get("chatId") or [""])[0]
            if not raw_chat_deals:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            track_request("sietch", int(raw_chat_deals), "deals")
            deals_mapping = get_mapping_by_chat("sietch", int(raw_chat_deals))
            if not deals_mapping:
                _json_response(self, 403, {"error": "No linked account for this chat"})
                return
            is_employee = bool(deals_mapping.get("employee"))
            # Customers are scoped to their own contact; the id comes from the
            # mapping so a client cannot ask for somebody else's projects.
            contact_id = deals_mapping.get("contactId") if not is_employee else None
            if not is_employee and not contact_id:
                _json_response(self, 400, {"error": "Account is not fully set up"})
                return

            # Query PostgreSQL with enriched fields
            where = ["o.stage_type = 0"]
            params: list = []
            if contact_id:
                where.append("o.contact_id = %s")
                params.append(contact_id)
            search = (qs.get("search") or [""])[0].strip().lower()
            if search:
                where.append("o.title ILIKE %s")
                params.append(f"%{search}%")
            tag = (qs.get("tag") or [""])[0].strip()
            if tag:
                where.append("o.id IN (SELECT ot.opportunity_id FROM opportunity_tags ot JOIN tag_definitions t ON t.id = ot.tag_id WHERE t.title = %s)")
                params.append(tag)

            rows = db.query(
                f"""SELECT o.id, o.title, o.bid_value, o.description, o.created_at,
                           o.expected_close_date, o.is_private, o.project_number,
                           s.title, c.first_name, c.last_name, c.company,
                           u.display_name, o.responsible_user_id, o.contact_id
                    FROM opportunities o
                    LEFT JOIN stages s ON o.stage_id = s.id
                    LEFT JOIN contacts c ON c.id = o.contact_id
                    LEFT JOIN users u ON o.responsible_user_id = u.id
                    WHERE {' AND '.join(where)} ORDER BY o.created_at DESC LIMIT 100""",
                (*params,),
            )
            # Filter by allowedProjectIds if customer mapping has restrictions.
            # Reuse the already-resolved mapping rather than re-reading the store.
            allowed_ids = None
            if not is_employee:
                from crm_bot_store import get_allowed_projects
                allowed_ids = get_allowed_projects("sietch", contact_id, is_employee=False)
            deals = []
            for r in rows:
                opp_id = r[0]
                # Skip if customer has project restrictions and this project isn't allowed
                if allowed_ids is not None and opp_id not in allowed_ids:
                    continue
                deal = {
                    "id": opp_id, "title": r[1],
                    "amount": float(r[2]) if r[2] else 0,
                    "description": r[3] or "",
                    "created": r[4].isoformat() if r[4] else "",
                    "dueDate": r[5].isoformat() if r[5] else "",
                    "isPrivate": bool(r[6]),
                    "project_number": r[7],
                    "stage": r[8] or "",
                    "contact": f"{r[9] or ''} {r[10] or ''}".strip(),
                    "contactCompany": r[11] or "",
                    "responsible": r[12] or "",
                }
                # Enrich with tags
                try:
                    tag_rows = db.query(
                        "SELECT t.title FROM tag_definitions t JOIN opportunity_tags ot ON t.id = ot.tag_id WHERE ot.opportunity_id = %s",
                        (opp_id,),
                    )
                    deal["tags"] = [tr[0] for tr in tag_rows]
                except Exception:
                    deal["tags"] = []
                # Enrich with user fields (custom fields)
                try:
                    cf_rows = db.query(
                        """SELECT cfd.label, cfd.field_key, ocfv.field_value
                           FROM opportunity_custom_field_values ocfv
                           JOIN custom_field_definitions cfd ON cfd.id = ocfv.field_id
                           WHERE ocfv.opportunity_id = %s""",
                        (opp_id,),
                    )
                    uf = {}
                    for cf in cf_rows:
                        label = cf[0] or cf[1] or ""
                        if label and cf[2]:
                            uf[label] = cf[2]
                    deal["userFields"] = uf
                except Exception:
                    deal["userFields"] = {}
                # Enrich with contact phone/address for call+address commands
                try:
                    if r[13]:  # contact_id
                        c_row = db.query_one(
                            "SELECT phone, mobile_phone, address_street, address_city, address_state, address_zip FROM contacts WHERE id = %s",
                            (r[13],),
                        )
                        if c_row:
                            deal["contactPhone"] = c_row[0] or ""
                            deal["contactMobile"] = c_row[1] or ""
                            addr_parts = [c_row[2] or "", c_row[3] or "", c_row[4] or "", c_row[5] or ""]
                            deal["address"] = ", ".join(p for p in addr_parts if p)
                except Exception:
                    deal["contactPhone"] = ""
                    deal["contactMobile"] = ""
                    deal["address"] = ""
                # Enrich with latest update (most recent history event)
                try:
                    ev_row = db.query_one(
                        """SELECT he.content, he.created_at, hc.title
                           FROM history_events he
                           LEFT JOIN history_categories hc ON hc.id = he.category_id
                           WHERE he.opportunity_id = %s
                           ORDER BY he.created_at DESC LIMIT 1""",
                        (opp_id,),
                    )
                    if ev_row:
                        deal["latestUpdate"] = {
                            "content": ev_row[0] or "",
                            "created": ev_row[1].isoformat() if ev_row[1] else "",
                            "categoryName": ev_row[2] or "",
                        }
                    else:
                        deal["latestUpdate"] = None
                except Exception:
                    deal["latestUpdate"] = None
                # Enrich with recent updates filtered to the pairing's update
                # type (e.g. Customer Update). The customer card shows the 3
                # most recent; without the filter customers see whatever event
                # happens to be newest (or nothing when its content is empty).
                try:
                    upd_cat_id = deals_mapping.get("notesCategoryId")
                    if not upd_cat_id:
                        fallback_row = db.query_one(
                            "SELECT id FROM history_categories WHERE title ILIKE 'customer update'"
                        )
                        upd_cat_id = fallback_row[0] if fallback_row else None
                    if upd_cat_id:
                        upd_rows = db.query(
                            """SELECT he.content, he.created_at, hc.title
                               FROM history_events he
                               LEFT JOIN history_categories hc ON hc.id = he.category_id
                               WHERE he.opportunity_id = %s AND he.category_id = %s
                               ORDER BY he.created_at DESC LIMIT 3""",
                            (opp_id, int(upd_cat_id)),
                        )
                    else:
                        upd_rows = db.query(
                            """SELECT he.content, he.created_at, hc.title
                               FROM history_events he
                               LEFT JOIN history_categories hc ON hc.id = he.category_id
                               WHERE he.opportunity_id = %s
                               ORDER BY he.created_at DESC LIMIT 3""",
                            (opp_id,),
                        )
                    deal["recentUpdates"] = [
                        {
                            "content": ur[0] or "",
                            "created": ur[1].isoformat() if ur[1] else "",
                            "categoryName": ur[2] or "",
                        }
                        for ur in upd_rows
                    ]
                except Exception:
                    deal["recentUpdates"] = []
                # Enrich with last 10 events (employee only)
                if is_employee:
                    try:
                        ev_rows = db.query(
                            """SELECT he.content, he.created_at, hc.title, u.display_name
                               FROM history_events he
                               LEFT JOIN history_categories hc ON hc.id = he.category_id
                               LEFT JOIN users u ON u.id = he.created_by
                               WHERE he.opportunity_id = %s
                               ORDER BY he.created_at DESC LIMIT 10""",
                            (opp_id,),
                        )
                        deal["events"] = [
                            {
                                "content": ev[0] or "",
                                "created": ev[1].isoformat() if ev[1] else "",
                                "categoryName": ev[2] or "",
                                "author": ev[3] or "",
                            }
                            for ev in ev_rows
                        ]
                    except Exception:
                        deal["events"] = []
                deals.append(deal)
            _json_response(self, 200, {"deals": deals})
            return

        if api_path == "/api/bot/categories":
            # Accept either session auth or bot token auth
            bot_token = _get_bot_token()
            auth_header = self.headers.get("Authorization", "").strip()
            if not (bot_token and auth_header == f"Bearer {bot_token}"):
                user = _require_auth(self)
                if not user:
                    return
            rows = db.query("SELECT id, title FROM history_categories ORDER BY sort_order")
            _json_response(self, 200, [{"id": r[0], "title": r[1]} for r in rows])
            return

        if api_path == "/api/bot/tags":
            rows = db.query("SELECT DISTINCT t.title FROM tag_definitions t JOIN opportunity_tags ot ON t.id = ot.tag_id")
            _json_response(self, 200, [r[0] for r in rows])
            return

        if api_path == "/api/bot/usage":
            user = _require_admin(self)
            if not user:
                return
            stats = get_usage_stats("sietch")
            _json_response(self, 200, stats)
            return

        # ── Bot: list tasks for employee ─────────────────────────────────
        if api_path == "/api/bot/tasks":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            if not user_id:
                _json_response(self, 200, [])
                return
            show_closed = (qs.get("showClosed") or ["false"])[0].lower() == "true"
            if show_closed:
                rows = db.query(
                    """SELECT t.id, t.title, t.description, t.due_date, t.priority, t.is_closed,
                              t.closed_at, t.created_at, t.labels, t.color,
                              o.title AS project_title, t.opportunity_id
                       FROM tasks t
                       LEFT JOIN opportunities o ON o.id = t.opportunity_id
                       WHERE t.responsible_user_id = %s
                       ORDER BY t.is_closed, t.due_date NULLS LAST, t.priority DESC""",
                    (user_id,),
                )
            else:
                rows = db.query(
                    """SELECT t.id, t.title, t.description, t.due_date, t.priority, t.is_closed,
                              t.closed_at, t.created_at, t.labels, t.color,
                              o.title AS project_title, t.opportunity_id
                       FROM tasks t
                       LEFT JOIN opportunities o ON o.id = t.opportunity_id
                       WHERE t.responsible_user_id = %s AND t.is_closed = FALSE
                       ORDER BY t.due_date NULLS LAST, t.priority DESC""",
                    (user_id,),
                )
            tasks = []
            for r in rows:
                tasks.append({
                    "id": r[0], "title": r[1], "description": r[2] or "",
                    "dueDate": r[3].isoformat() if r[3] else None,
                    "priority": r[4], "isClosed": r[5],
                    "closedAt": r[6].isoformat() if r[6] else None,
                    "createdAt": r[7].isoformat() if r[7] else None,
                    "labels": r[8] or "", "color": r[9] or "",
                    "projectTitle": r[10] or None,
                    "opportunityId": r[11],
                })
            _json_response(self, 200, tasks)
            return

        # ── Bot: my-projects (employee's assigned projects) ──────────────
        if api_path == "/api/bot/my-projects":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            rows = db.query(
                """SELECT o.id, o.title, o.bid_value, s.title,
                          o.expected_close_date, o.created_at, o.contact_id,
                          COALESCE(c.first_name || ' ' || c.last_name, c.company, '') as contact_name
                   FROM opportunities o
                   LEFT JOIN stages s ON o.stage_id = s.id
                   LEFT JOIN contacts c ON c.id = o.contact_id
                   WHERE o.stage_type = 0 AND o.responsible_user_id = %s
                   ORDER BY o.created_at DESC""",
                (user_id,),
            )
            projects = []
            for r in rows:
                projects.append({
                    "id": r[0], "title": r[1], "amount": float(r[2] or 0),
                    "stage": r[3] or "",
                    "dueDate": r[4].isoformat() if r[4] else None,
                    "created": r[5].isoformat() if r[5] else None,
                    "contact": r[7] or "",
                })
            _json_response(self, 200, projects)
            return

        # ── Bot: overdue projects ────────────────────────────────────────
        if api_path == "/api/bot/overdue":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            rows = db.query(
                """SELECT o.id, o.title, o.bid_value, s.title,
                          o.expected_close_date, o.contact_id,
                          COALESCE(c.first_name || ' ' || c.last_name, c.company, '') as contact_name
                   FROM opportunities o
                   LEFT JOIN stages s ON o.stage_id = s.id
                   LEFT JOIN contacts c ON c.id = o.contact_id
                   WHERE o.stage_type = 0 AND o.responsible_user_id = %s
                     AND o.expected_close_date < NOW() AND o.expected_close_date IS NOT NULL
                   ORDER BY o.expected_close_date ASC""",
                (user_id,),
            )
            projects = []
            for r in rows:
                projects.append({
                    "id": r[0], "title": r[1], "amount": float(r[2] or 0),
                    "stage": r[3] or "",
                    "dueDate": r[4].isoformat() if r[4] else None,
                    "contact": r[6] or "",
                })
            _json_response(self, 200, projects)
            return

        # ── Bot: due this week ───────────────────────────────────────────
        if api_path == "/api/bot/due-this-week":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            rows = db.query(
                """SELECT o.id, o.title, o.bid_value, s.title,
                          o.expected_close_date, o.contact_id,
                          COALESCE(c.first_name || ' ' || c.last_name, c.company, '') as contact_name
                   FROM opportunities o
                   LEFT JOIN stages s ON o.stage_id = s.id
                   LEFT JOIN contacts c ON c.id = o.contact_id
                   WHERE o.stage_type = 0 AND o.responsible_user_id = %s
                     AND o.expected_close_date >= NOW()
                     AND o.expected_close_date < NOW() + INTERVAL '7 days'
                   ORDER BY o.expected_close_date ASC""",
                (user_id,),
            )
            projects = []
            for r in rows:
                projects.append({
                    "id": r[0], "title": r[1], "amount": float(r[2] or 0),
                    "stage": r[3] or "",
                    "dueDate": r[4].isoformat() if r[4] else None,
                    "contact": r[6] or "",
                })
            _json_response(self, 200, projects)
            return

        # ── Bot: pipeline stats ──────────────────────────────────────────
        if api_path == "/api/bot/pipeline":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            rows = db.query(
                """SELECT s.title, COUNT(*) as cnt, COALESCE(SUM(o.bid_value), 0) as total
                   FROM opportunities o
                   LEFT JOIN stages s ON o.stage_id = s.id
                   WHERE o.stage_type = 0
                   GROUP BY s.title ORDER BY total DESC"""
            )
            stages = [{"stage": r[0] or "Unknown", "count": r[1], "total": float(r[2])} for r in rows]
            _json_response(self, 200, stages)
            return

        # ── Bot: calendar events ─────────────────────────────────────────
        if api_path == "/api/bot/calendar":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            rows = db.query(
                """SELECT id, summary, description, location, dtstart, dtend
                   FROM calendar_events
                   WHERE user_id = %s AND dtstart >= NOW() - INTERVAL '1 day'
                   ORDER BY dtstart ASC LIMIT 20""",
                (user_id,),
            )
            events = []
            for r in rows:
                events.append({
                    "id": r[0], "summary": r[1] or "Event",
                    "description": r[2] or "", "location": r[3] or "",
                    "dtstart": r[4].isoformat() if r[4] else None,
                    "dtend": r[5].isoformat() if r[5] else None,
                })
            _json_response(self, 200, events)
            return

        # ── Bot: today summary ───────────────────────────────────────────
        if api_path == "/api/bot/today":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            tasks = db.query(
                """SELECT t.id, t.title, t.due_date, o.title as project_title, t.opportunity_id
                   FROM tasks t LEFT JOIN opportunities o ON o.id = t.opportunity_id
                   WHERE t.responsible_user_id = %s AND t.is_closed = FALSE
                     AND (t.due_date IS NULL OR t.due_date::date = NOW()::date)
                   ORDER BY t.priority DESC""",
                (user_id,),
            )
            events = db.query(
                """SELECT id, summary, dtstart, dtend
                   FROM calendar_events
                   WHERE user_id = %s AND dtstart::date = NOW()::date
                   ORDER BY dtstart ASC""",
                (user_id,),
            )
            _json_response(self, 200, {
                "tasks": [{"id": r[0], "title": r[1], "dueDate": r[2].isoformat() if r[2] else None,
                           "projectTitle": r[3], "opportunityId": r[4]} for r in tasks],
                "events": [{"id": r[0], "summary": r[1],
                            "dtstart": r[2].isoformat() if r[2] else None,
                            "dtend": r[3].isoformat() if r[3] else None} for r in events],
            })
            return

        # ── Bot: team members ────────────────────────────────────────────
        if api_path == "/api/bot/team":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            rows = db.query(
                """SELECT id, display_name, email, first_name, last_name
                   FROM users WHERE is_active = TRUE ORDER BY display_name"""
            )
            team = [{"id": r[0], "displayName": r[1] or f"{r[3] or ''} {r[4] or ''}".strip(),
                      "email": r[2]} for r in rows]
            _json_response(self, 200, team)
            return

        # ── Bot: recent activity ─────────────────────────────────────────
        if api_path == "/api/bot/recent":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            raw_chat = (qs.get("chatId") or [""])[0]
            if not raw_chat:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(raw_chat))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            rows = db.query(
                """SELECT he.id, he.created_at, he.content, o.title as project_title,
                          o.id as opp_id, hc.title as category
                   FROM history_events he
                   LEFT JOIN opportunities o ON o.id = he.opportunity_id
                   LEFT JOIN history_categories hc ON hc.id = he.category_id
                   WHERE he.created_at > NOW() - INTERVAL '7 days'
                   ORDER BY he.created_at DESC LIMIT 20"""
            )
            events = []
            for r in rows:
                events.append({
                    "id": r[0], "createdAt": r[1].isoformat() if r[1] else None,
                    "content": r[2] or "", "projectTitle": r[3] or "",
                    "opportunityId": r[4], "category": r[5] or "",
                })
            _json_response(self, 200, events)
            return

        # ── Bot: stats overview ──────────────────────────────────────────
        if api_path == "/api/bot/stats":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            total = db.query_one("SELECT COUNT(*) as cnt FROM opportunities WHERE stage_type = 0")
            total_value = db.query_one("SELECT COALESCE(SUM(bid_value), 0) as total FROM opportunities WHERE stage_type = 0")
            closed_won = db.query_one("""SELECT COUNT(*) as cnt FROM opportunities o JOIN stages s ON o.stage_id = s.id WHERE o.stage_type = 0 AND s.title ILIKE '%won%'""")
            open_tasks = db.query_one("SELECT COUNT(*) as cnt FROM tasks WHERE is_closed = FALSE")
            _json_response(self, 200, {
                "totalProjects": total["cnt"] if total else 0,
                "totalValue": float(total_value["total"]) if total_value else 0,
                "closedWon": closed_won["cnt"] if closed_won else 0,
                "openTasks": open_tasks["cnt"] if open_tasks else 0,
            })
            return

        if api_path == "/api/bot/notification-by-message":
            raw_chat = (qs.get("chatId") or [""])[0]
            raw_msg = (qs.get("messageId") or [""])[0]
            if not raw_chat or not raw_msg:
                _json_response(self, 400, {"error": "chatId and messageId are required"})
                return
            try:
                row = db.query_one(
                    """SELECT n.id AS notification_id, n.opportunity_id, n.actor_user_id,
                              o.title AS project_title, n.payload
                       FROM telegram_notification_log tnl
                       JOIN notifications n ON n.id = tnl.notification_id
                       LEFT JOIN opportunities o ON o.id = n.opportunity_id
                       WHERE tnl.chat_id = %s AND tnl.message_id = %s""",
                    (int(raw_chat), int(raw_msg)),
                )
                if row:
                    payload_data = row["payload"] if isinstance(row["payload"], dict) else {}
                    _json_response(self, 200, {
                        "notificationId": row["notification_id"],
                        "opportunityId": row["opportunity_id"],
                        "actorUserId": row["actor_user_id"],
                        "projectTitle": row.get("project_title") or "Unknown Project",
                        "eventId": payload_data.get("event_id"),
                    })
                else:
                    _json_response(self, 404, {"error": "Notification not found"})
            except Exception:
                logger.exception("Failed to look up notification by message")
                _json_response(self, 500, {"error": "Internal error"})
            return

        self.send_error(404)

    def _handle_bot_api_post(self, method: str) -> None:
        api_path = urlparse(self.path).path
        bot_token = _get_bot_token()
        auth_header = self.headers.get("Authorization", "").strip()

        if api_path == "/api/bot/note" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            opp_id = payload.get("opportunityId") or payload.get("dealId")
            content = str(payload.get("content") or "").strip()
            category_id = int(payload.get("categoryId") or 1)
            created_by = payload.get("createdBy")
            # Auto-resolve created_by from chatId mapping if not provided
            if not created_by:
                chat_id = payload.get("chatId")
                if chat_id:
                    mapping = get_mapping_by_chat("sietch", int(chat_id))
                    if mapping and mapping.get("userId"):
                        created_by = mapping["userId"]
            if not opp_id or not content:
                _json_response(self, 400, {"error": "opportunityId and content are required"})
                return
            event_id = db.insert_returning(
                """INSERT INTO history_events (opportunity_id, category_id, content, created_by)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (int(opp_id), category_id, content, int(created_by) if created_by else None),
            )
            # Process notifyUserList (same as web endpoint)
            notify_list = payload.get("notifyUserList") or []
            for uid in notify_list:
                try:
                    db.execute(
                        "INSERT INTO history_notify_users (event_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (event_id, int(uid)),
                    )
                except (TypeError, ValueError):
                    pass
            if notify_list and category_id in (1, 8, 10):
                import re as _re
                snippet = _re.sub(r'<[^>]+>', '', content)[:120].strip()
                for uid in notify_list:
                    try:
                        db.execute(
                            """INSERT INTO notifications (user_id, type, opportunity_id, actor_user_id, message, payload)
                               SELECT %s, 'note_tagged', %s, %s,
                                      u.display_name || ' tagged you on ' || p.title,
                                      jsonb_build_object('event_id', %s, 'event_category', 'note', 'snippet', %s)
                               FROM users u, opportunities p WHERE p.id = %s AND u.id = %s""",
                            (int(uid), int(opp_id), int(created_by) if created_by else None,
                             event_id, snippet, int(opp_id), int(created_by) if created_by else None),
                        )
                    except Exception:
                        pass
            _json_response(self, 200, {"ok": True, "eventId": event_id})
            return

        if api_path == "/api/bot/reset-password" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            chat_id = payload.get("chatId")
            if not chat_id:
                _json_response(self, 400, {"error": "chatId is required"})
                return
            mapping = get_mapping_by_chat("sietch", int(chat_id))
            if not mapping:
                _json_response(self, 404, {"error": "No user found for this chat"})
                return
            if not mapping.get("employee"):
                _json_response(self, 403, {"error": "User is not an employee"})
                return
            user_id = mapping.get("userId")
            if not user_id:
                _json_response(self, 404, {"error": "No user ID associated with this mapping"})
                return
            import secrets as _secrets
            password = _secrets.token_urlsafe(9)[:12]
            pw_hash, salt = auth_mod.hash_password(password)
            db.execute(
                "UPDATE users SET password_hash = %s, password_salt = %s, must_change_password = TRUE WHERE id = %s",
                (pw_hash, salt, int(user_id)),
            )
            auth_mod.destroy_all_user_sessions(int(user_id))
            _json_response(self, 200, {"ok": True, "tempPassword": password})
            return

        if api_path == "/api/bot/send-message" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            chat_id = payload.get("chatId")
            text = str(payload.get("text") or "").strip()
            if not chat_id or not text:
                _json_response(self, 400, {"error": "chatId and text are required"})
                return
            reply_to = payload.get("replyToMessageId")
            parse_mode = payload.get("parseMode", "HTML")
            try:
                import httpx as _httpx
                bot_token = _get_bot_token()
                if not bot_token:
                    _json_response(self, 500, {"error": "TELEGRAM_BOT_TOKEN not configured"})
                    return
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                # Handle "all" — send to every linked user
                if chat_id == "all":
                    from crm_bot_store import list_mappings
                    mappings = list_mappings("sietch")
                    targets = [m["chatId"] for m in mappings if m.get("chatId")]
                    sent = 0
                    errors = 0
                    for cid in targets:
                        body: dict = {"chat_id": cid, "text": text, "parse_mode": parse_mode}
                        try:
                            resp = _httpx.post(url, json=body, timeout=10)
                            if resp.json().get("ok"):
                                sent += 1
                            else:
                                errors += 1
                        except Exception:
                            errors += 1
                    _json_response(self, 200, {"ok": True, "sent": sent, "errors": errors})
                else:
                    body = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
                    if reply_to:
                        body["reply_to_message_id"] = reply_to
                    resp = _httpx.post(url, json=body, timeout=10)
                    data = resp.json()
                    if data.get("ok"):
                        _json_response(self, 200, {"ok": True, "messageId": data["result"]["message_id"]})
                    else:
                        _json_response(self, 502, {"error": data.get("description", "Telegram API error")})
            except Exception as exc:
                logger.exception("Failed to send Telegram message")
                _json_response(self, 500, {"error": str(exc)})
            return

        # ── Bot: send document to Telegram user ───────────────────────
        if api_path == "/api/v2/admin/bot/send-document" and method == "POST":
            self._handle_bot_send_document()
            return

        # ── Bot: task-done ───────────────────────────────────────────────
        if api_path == "/api/bot/task-done" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            task_id = payload.get("taskId")
            chat_id = payload.get("chatId")
            if not task_id or not chat_id:
                _json_response(self, 400, {"error": "taskId and chatId are required"})
                return
            mapping = get_mapping_by_chat("sietch", int(chat_id))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            task = db.query_one("SELECT id, title FROM tasks WHERE id = %s AND responsible_user_id = %s",
                                (int(task_id), user_id))
            if not task:
                _json_response(self, 404, {"error": "Task not found or not assigned to you"})
                return
            db.execute("UPDATE tasks SET is_closed = TRUE, closed_at = NOW(), closed_by = %s WHERE id = %s",
                       (user_id, int(task_id)))
            _json_response(self, 200, {"ok": True, "title": task["title"]})
            event_bus.publish("task:updated", {"task_id": int(task_id), "title": task["title"]})
            return

        # ── Bot: task-create ─────────────────────────────────────────────
        if api_path == "/api/bot/task-create" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            chat_id = payload.get("chatId")
            title = str(payload.get("title") or "").strip()
            if not chat_id or not title:
                _json_response(self, 400, {"error": "chatId and title are required"})
                return
            mapping = get_mapping_by_chat("sietch", int(chat_id))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            user_id = mapping.get("userId")
            opp_id = int(payload["opportunityId"]) if payload.get("opportunityId") else None
            description = str(payload.get("description") or "").strip()
            due_date = payload.get("dueDate")
            task_id = db.insert_returning(
                """INSERT INTO tasks (title, description, responsible_user_id, owner_user_id,
                                      opportunity_id, due_date, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (title, description, user_id, user_id, opp_id, due_date, user_id),
            )
            _json_response(self, 200, {"ok": True, "taskId": task_id, "title": title})
            event_bus.publish("task:created", {
                "task_id": task_id, "title": title, "opportunity_id": opp_id,
            })
            return

        # ── Bot: stage update ────────────────────────────────────────────
        if api_path == "/api/bot/stage" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            chat_id = payload.get("chatId")
            opp_id = payload.get("opportunityId") or payload.get("dealId")
            new_stage = str(payload.get("stage") or "").strip()
            stage_id = payload.get("stageId")
            if not chat_id or not opp_id:
                _json_response(self, 400, {"error": "chatId and opportunityId are required"})
                return
            mapping = get_mapping_by_chat("sietch", int(chat_id))
            if not mapping or not mapping.get("employee"):
                _json_response(self, 403, {"error": "Employee access required"})
                return
            if stage_id:
                stage_row = db.query_one("SELECT id, title FROM stages WHERE id = %s", (int(stage_id),))
            elif new_stage:
                stage_row = db.query_one("SELECT id, title FROM stages WHERE title ILIKE %s", (new_stage,))
            else:
                _json_response(self, 400, {"error": "stage or stageId is required"})
                return
            if not stage_row:
                _json_response(self, 400, {"error": f"Stage not found"})
                return
            stage_title = stage_row["title"] if isinstance(stage_row, dict) else stage_row[1]
            stage_id_val = stage_row["id"] if isinstance(stage_row, dict) else stage_row[0]
            db.execute("UPDATE opportunities SET stage_id = %s, stage_entered_at = NOW() WHERE id = %s",
                       (stage_id_val, int(opp_id)))
            event_bus.publish("stage_changed", {"opportunity_id": int(opp_id), "stage_id": stage_id_val})
            _json_response(self, 200, {"ok": True, "stage": stage_title})
            return

        # ── Bot: heartbeat ────────────────────────────────────────────────
        if api_path == "/api/bot/heartbeat" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                db.execute(
                    "INSERT INTO bot_activity_log (chat_id, action, detail) VALUES (0, 'heartbeat', 'OK')"
                )
            except Exception:
                pass
            cfg = _get_bot_config()
            _json_response(self, 200, {"ok": True, "configVersion": cfg.get("configVersion", 0)})
            return

        # ── Bot: publish its own identity ───────────────────────────────
        # POST accepts ONLY botUsername/botLink — the bot cannot rewrite the
        # token, the enabled flag or anything else, so this grants no
        # privilege beyond letting the bot publish its own name. The GET
        # counterpart lives in do_GET.
        if api_path == "/api/bot/settings" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON body"})
                return
            cfg = dict(_get_bot_config())
            # Whitelist: identity fields only. Everything else the caller
            # sends is ignored, including any attempt to set `token`.
            for key in ("botUsername", "botLink"):
                if key in payload:
                    cfg[key] = str(payload.get(key) or "")[:200]
            cfg["configVersion"] = int(cfg.get("configVersion", 0) or 0) + 1
            cfg["updatedAt"] = datetime.now(timezone.utc).isoformat()
            try:
                db.execute(
                    """INSERT INTO bot_config (key, value, updated_at, updated_by)
                       VALUES ('settings', %s, NOW(), NULL)
                       ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
                    (json.dumps(cfg),),
                )
                _invalidate_bot_config_cache()
                _json_response(self, 200, {"ok": True, "configVersion": cfg["configVersion"]})
            except Exception:
                logger.exception("Failed to save bot identity settings")
                _json_response(self, 500, {"error": "Failed to save bot settings"})
            return

        # ── Bot: config check ────────────────────────────────────────────
        if api_path == "/api/bot/config-check" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            client_version = payload.get("configVersion", 0)
            cfg = _get_bot_config()
            server_version = cfg.get("configVersion", 0)
            changed = client_version != server_version
            _json_response(self, 200, {"changed": changed, "configVersion": server_version})
            return

        # ── Bot: upload file to project ──────────────────────────────────
        if api_path == "/api/bot/upload" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    _json_response(self, 400, {"error": "multipart/form-data required"})
                    return
                # Parse multipart form data
                import cgi
                import tempfile
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                    },
                )
                file_field = form["file"]
                opp_id = int(form.getfirst("oppId", "0"))
                chat_id = int(form.getfirst("chatId", "0"))
                file_type = form.getfirst("fileType", "document")
                if not opp_id:
                    _json_response(self, 400, {"error": "oppId is required"})
                    return
                # Verify employee
                mapping = get_mapping_by_chat("sietch", chat_id)
                if not mapping or not mapping.get("employee"):
                    _json_response(self, 403, {"error": "Employee access required"})
                    return
                # Check file size
                cfg = _get_bot_config()
                max_size_mb = cfg.get("maxFileSizeMb", 20)
                file_data = file_field.file.read()
                if len(file_data) > max_size_mb * 1024 * 1024:
                    _json_response(self, 400, {"error": f"File exceeds {max_size_mb}MB limit"})
                    return
                filename = file_field.filename or "upload"
                mime_type = file_field.type or "application/octet-stream"
                # Route to photo or document storage
                if file_type == "photo" or mime_type.startswith("image/") or mime_type.startswith("video/"):
                    storage_dir = PHOTO_STORAGE_PATH / str(opp_id)
                    storage_dir.mkdir(parents=True, exist_ok=True)
                    # Sanitize filename
                    safe_name = re.sub(r'[^\w.\-]', '_', filename)
                    timestamp = int(time.time())
                    save_name = f"{timestamp}_{safe_name}"
                    save_path = storage_dir / save_name
                    with open(save_path, "wb") as f:
                        f.write(file_data)
                    # Generate thumbnail and extract EXIF using Pillow
                    thumb_path = None
                    exif_data = None
                    if Image:
                        try:
                            with Image.open(io.BytesIO(file_data)) as img:
                                exif_data = _extract_exif(img)
                                img.thumbnail((400, 400), Image.Resampling.LANCZOS)
                                if img.mode in ("RGBA", "P"):
                                    bg = Image.new("RGB", img.size, (255, 255, 255))
                                    if img.mode == "P":
                                        img = img.convert("RGBA")
                                    bg.paste(img, mask=img.split()[3])
                                    img = bg
                                elif img.mode != "RGB":
                                    img = img.convert("RGB")
                                thumb_name = f"thumb_{save_name}"
                                thumb_path = storage_dir / thumb_name
                                img.save(thumb_path, "JPEG", quality=85)
                        except Exception:
                            logger.warning("Thumbnail/EXIF failed for %s", filename)
                            thumb_path = None
                    # Fix MIME type: detect from extension if generic
                    if mime_type == "application/octet-stream":
                        ext = os.path.splitext(filename)[1].lower()
                        mime_type = {
                            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".png": "image/png", ".gif": "image/gif",
                            ".webp": "image/webp", ".bmp": "image/bmp",
                        }.get(ext, "image/jpeg")
                    # Insert into project_photos
                    file_id = db.insert_returning(
                        """INSERT INTO project_photos (opportunity_id, filename, file_path, file_size, mime_type, exif_data, thumbnail_path, uploaded_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (opp_id, filename, str(save_path.relative_to(ROOT)), len(file_data), mime_type,
                         json.dumps(exif_data) if exif_data else None,
                         str(thumb_path.relative_to(ROOT)) if thumb_path else None,
                         mapping.get("userId")),
                    )
                    _json_response(self, 200, {"ok": True, "fileId": file_id, "filename": filename, "type": "photo"})
                else:
                    storage_dir = DOCUMENT_STORAGE_PATH / str(opp_id)
                    storage_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r'[^\w.\-]', '_', filename)
                    timestamp = int(time.time())
                    save_name = f"{timestamp}_{safe_name}"
                    save_path = storage_dir / save_name
                    with open(save_path, "wb") as f:
                        f.write(file_data)
                    # Insert into project_documents
                    file_id = db.insert_returning(
                        """INSERT INTO project_documents (title, file_path, file_size, mime_type, uploaded_by, opportunity_id)
                           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                        (filename, str(save_path.relative_to(ROOT)), len(file_data), mime_type, mapping.get("userId"), opp_id),
                    )
                    _json_response(self, 200, {"ok": True, "fileId": file_id, "filename": filename, "type": "document"})
                # Log activity
                try:
                    db.execute(
                        "INSERT INTO bot_activity_log (chat_id, user_name, action, detail, project_id) VALUES (%s, %s, 'file_uploaded', %s, %s)",
                        (chat_id, mapping.get("contactName") or mapping.get("nickname") or "Employee", filename, opp_id),
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("Failed to process bot file upload")
                _json_response(self, 500, {"error": "Upload failed"})
            return

        # ── Bot: log activity ────────────────────────────────────────────
        if api_path == "/api/bot/activity" and method == "POST":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            try:
                payload = json.loads(_read_body(self) or b"{}")
                action = str(payload.get("action", "")).strip()
                if not action:
                    _json_response(self, 400, {"error": "action is required"})
                    return
                db.execute(
                    """INSERT INTO bot_activity_log (chat_id, user_name, action, detail, project_id, is_error)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        payload.get("chatId"),
                        payload.get("userName", ""),
                        action,
                        payload.get("detail", ""),
                        payload.get("projectId"),
                        payload.get("isError", False),
                    ),
                )
                _json_response(self, 200, {"ok": True})
            except Exception:
                logger.exception("Failed to log bot activity")
                _json_response(self, 500, {"error": "Failed to log activity"})
            return

    # ── Presence (local store, ported from v2 — unchanged) ──────────────────

    def _handle_presence_users(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        # Return users from DB instead of CRM API
        rows = db.query(
            "SELECT id, email, display_name, first_name, last_name, is_admin, avatar_icon FROM users WHERE is_active = TRUE"
        )
        people = [{"id": r[0], "email": r[1],
                    "displayName": r[2] or f"{r[3] or ''} {r[4] or ''}".strip(),
                    "firstName": r[3] or "",
                    "lastName": r[4] or "",
                    "isAdmin": bool(r[5]),
                    "avatarIcon": r[6] or ""} for r in rows]
        _json_response(self, 200, people)

    def _handle_presence_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        portal = "sietch"
        overlays = get_portal_presence_snapshot(portal)
        clean_stale_presence_records(portal)
        now = datetime.now(timezone.utc)

        # Online / AFD thresholds (audit round 6 item #7):
        # The previous values (10 min / 3 h) were too generous — a user
        # who closed their laptop on Friday at 6pm would still show
        # as "AFD" with a stale "Reviewing: Project X" auto-status on
        # Monday at 9am. The new values match the heartbeat cadence:
        # visible = every 30s, background-tick = every 2 min. If a tab
        # hasn't checked in for 3 minutes, it's almost certainly dead.
        # AFD = 15 minutes catches the laptop-sleep / overnight case
        # without leaving stale "Reviewing" statuses on the team modal
        # all weekend.
        PRESENCE_ONLINE_SECONDS = 180      # was 600
        PRESENCE_AFD_SECONDS = 900         # was 10800
        rows = db.query(
            "SELECT id, email, display_name, first_name, last_name, avatar_icon FROM users WHERE is_active = TRUE"
        )
        out_users = []
        for r in rows:
            uid = str(r[0])
            ov = overlays.get(uid) or {}
            hb = _parse_iso_datetime(ov.get("lastHeartbeat") or "")
            tab_count = int(ov.get("lastTabCount") or 0)
            # Cross-tab close handling (audit round 6 item #7): if no
            # tabs are open, the user is offline regardless of how
            # recently a heartbeat landed. This prevents the "closed
            # one of two tabs → user appears offline to everyone for
            # 30s" UX bug from the original implementation, where the
            # lastHeartbeat field was shared across all of the user's
            # tabs and the offline beacon from tab A blanked the
            # heartbeat that tab B was keeping alive.
            tabs_open = tab_count > 0
            online = tabs_open and bool(hb and (now - hb).total_seconds() < PRESENCE_ONLINE_SECONDS)
            afd = tabs_open and bool(hb and not online and (now - hb).total_seconds() < PRESENCE_AFD_SECONDS)
            auto_status = ov.get("autoStatus") or ""
            if auto_status and not online:
                auto_status = ""

            out_users.append({
                "id": uid,
                "displayName": r[2] or f"{r[3] or ''} {r[4] or ''}".strip(),
                "email": r[1] or "",
                "firstName": r[3] or "",
                "lastName": r[4] or "",
                "avatarIcon": r[5] or "",
                "online": online,
                "afd": afd,
                "status": ov.get("status", ""),
                "inferred": bool(ov.get("inferred")),
                "autoStatus": auto_status,
            })

        my_presence = load_user_presence(portal, str(user["id"]))
        recent_dms = get_recent_dms_for_user(portal, str(user["id"]))
        last_read_dms = load_user_last_read_dms(portal, str(user["id"]))
        _json_response(self, 200, {
            "users": out_users,
            "me": {"id": user["id"], "email": user["email"], "status": my_presence.get("status", ""), "inferred": bool(my_presence.get("inferred"))},
            "isAdmin": user.get("is_admin", False),
            "myRecentDms": recent_dms,
            "lastReadDms": last_read_dms,
        })

    def _handle_presence_heartbeat(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        offline, visible = False, False
        # tabs is an integer (>= 1) representing how many of this user's
        # dashboard tabs are currently open. Each tab increments on load
        # and decrements on beforeunload. When tabs drops to 0 the user
        # is treated as offline regardless of how recently a heartbeat
        # landed — this fixes the cross-tab close UX (closing one of two
        # tabs previously blanked the shared lastHeartbeat for up to 30s,
        # making the user appear offline to everyone even though the
        # other tab was still alive).
        tabs = None
        try:
            body = _read_body(self)
            if body:
                payload = json.loads(body)
                offline = bool(payload.get("offline"))
                visible = bool(payload.get("visible"))
                if "tabs" in payload:
                    try:
                        tabs_candidate = int(payload.get("tabs") or 0)
                        if tabs_candidate >= 0:
                            tabs = tabs_candidate
                    except (TypeError, ValueError):
                        tabs = None
        except (json.JSONDecodeError, ValueError):
            pass
        touch_heartbeat("sietch", str(user["id"]), offline=offline, visible=visible, tabs=tabs)
        _json_response(self, 200, {"ok": True})

    def _handle_presence_status(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        has_status = "status" in payload
        status_text = str(payload.get("status") or "")[:200] if has_status else None
        auto_status = payload.get("autoStatus")
        inferred = bool(payload.get("inferred"))
        rec = set_status("sietch", str(user["id"]), status_text, inferred=inferred, autoStatus=auto_status)
        # Real-time fanout: tell the other open tabs/users that this
        # user's status changed so the team modal and tile update
        # without waiting for the next poll. We only publish the
        # minimum the frontend needs to update the row in-place; the
        # frontend's handlePresenceUpdate merges these into the
        # existing clientPresenceCache + re-renders.
        try:
            event_bus.publish("presence:update", {
                "user_id": str(user["id"]),
                "status": rec.get("status", ""),
                "inferred": rec.get("inferred", False),
                "autoStatus": rec.get("autoStatus", ""),
            })
        except Exception:
            pass
        _json_response(self, 200, {"ok": True, "status": rec.get("status", ""), "inferred": rec.get("inferred", False)})

    def _handle_presence_last_read(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        other = str(payload.get("with") or "").strip()
        at = str(payload.get("at") or "")
        if not other:
            _json_response(self, 400, {"error": "with=<userId> is required"})
            return
        # Two side-effects on read:
        #   1) set_last_read_dm: stores the per-user last-read timestamp
        #      in the user's presence file. Powers the inbox unread
        #      badge and the "Recent messages" ordering across devices.
        #   2) mark_messages_read: flips the per-message `read` flag in
        #      the conversation file. Powers the read receipts the
        #      sender sees on their sent messages ("read 2m ago").
        # Both are now explicit (the implicit mark-on-GET was removed
        # because it over-marked on Load-More clicks).
        set_last_read_dm("sietch", str(user["id"]), other, at or None)
        mark_messages_read("sietch", str(user["id"]), other)
        # Now that the read-marker is explicit, the dm:read SSE fanout
        # also needs to be explicit. We publish here, not on the GET,
        # so the sender's thread only updates its read receipt when
        # the recipient actually saw the thread. Not on every page load.
        try:
            event_bus.publish("dm:read", {
                "with_user": other,
                "by_user": str(user["id"]),
                "at": datetime.now(timezone.utc).isoformat(),
            }, user_ids=[other])
        except Exception:
            # A failed publish must not break the response.
            pass
        _json_response(self, 200, {"ok": True})

    def _handle_presence_dm_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        with_id = (qs.get("with") or [""])[0]
        if not with_id:
            _json_response(self, 400, {"error": "with=<userId> is required"})
            return
        # offset=0 (default) returns the most recent page. offset=N
        # skips the N newest messages and returns the next page (used
        # by the "Load Earlier Messages" button in the DM thread).
        # We pass it through to get_conversation so Load-More works.
        offset_raw = (qs.get("offset") or ["0"])[0]
        try:
            offset = max(0, int(offset_raw))
        except (TypeError, ValueError):
            offset = 0
        # Note: the previous version of this handler called
        # mark_messages_read() here, on every GET. That meant a user
        # who clicked "Load Earlier Messages" would mark ALL unread
        # messages as read on the first call (because the GET returns
        # the most recent page, which the auto-mark applied to). The
        # read-marker is now explicit: the frontend calls
        # POST /api/presence/last-read when it actually wants to mark
        # the thread as read (in openPresenceDMThread, openDMPopup, and
        # openDMInline). Load-More calls are now pure reads with no
        # side effects on the other party.
        msgs, has_more = get_conversation("sietch", str(user["id"]), with_id, offset=offset)
        _json_response(self, 200, {"messages": msgs, "has_more": has_more})

    def _handle_presence_dm_post(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        to_id = str(payload.get("to") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not to_id or not text:
            _json_response(self, 400, {"error": "to and text are required"})
            return
        msg = append_dm("sietch", str(user["id"]), to_id, text, payload.get("reply_to"), payload.get("reply_text"))
        # Real-time fanout: notify the recipient via SSE so the new DM
        # appears in their inbox / thread without waiting for the next
        # poll. The sender's UI updates locally from the POST response.
        # We only publish to the recipient; the sender's frontend already
        # knows the message was sent (response.ok, no need to round-trip).
        try:
            from_name = user.get("display_name") or user.get("email") or ""
            event_bus.publish("dm:new", {
                "from": str(user["id"]),
                "fromName": from_name,
                "to": to_id,
                "text": msg.get("text", ""),
                "ts": msg.get("ts", ""),
                "message": msg,
            }, user_ids=[to_id])
        except Exception:
            # A failed publish must not roll back the message save.
            pass
        _json_response(self, 200, {"ok": True, "message": msg})

    def _handle_presence_dm_clear(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        with_id = (qs.get("with") or [""])[0]
        if not with_id:
            _json_response(self, 400, {"error": "with=<userId> is required"})
            return
        clear_conversation("sietch", str(user["id"]), with_id)
        _json_response(self, 200, {"ok": True})

    # ── Timesheet handlers ──

    def _handle_timesheet_clock_in(self) -> None:
        user = _require_auth(self)
        if not user: return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        existing = db.query_dicts(
            "SELECT id FROM time_entries WHERE user_id = %s AND clock_out IS NULL",
            (user["id"],),
        )
        if existing:
            _json_response(self, 400, {"error": "Already clocked in. Clock out first."})
            return
        opp_id = payload.get("opportunity_id")
        notes = str(payload.get("notes") or "").strip()
        entry_id = db.insert_returning(
            "INSERT INTO time_entries (user_id, clock_in, opportunity_id, notes) VALUES (%s, NOW(), %s, %s) RETURNING id",
            (user["id"], opp_id if opp_id else None, notes),
        )
        entry = db.query_dicts("SELECT * FROM time_entries WHERE id = %s", (entry_id,))[0]
        _json_response(self, 200, {"ok": True, "entry": entry})

    def _handle_timesheet_clock_out(self) -> None:
        user = _require_auth(self)
        if not user: return
        entries = db.query_dicts(
            "SELECT id, break_start FROM time_entries WHERE user_id = %s AND clock_out IS NULL",
            (user["id"],),
        )
        if not entries:
            _json_response(self, 400, {"error": "Not currently clocked in"})
            return
        entry = entries[0]
        # Symmetric with _handle_timesheet_break_end: refuse to clock out
        # while a break is in progress. The user must end the break first;
        # the frontend can then clock out normally. This prevents silently
        # adding the in-progress break to break_minutes and closing the shift.
        if entry.get("break_start"):
            _json_response(self, 400, {"error": "End your break before clocking out"})
            return
        db.execute(
            "UPDATE time_entries SET clock_out = NOW(), updated_at = NOW() WHERE id = %s",
            (entry["id"],),
        )
        updated = db.query_dicts("SELECT * FROM time_entries WHERE id = %s", (entry["id"],))
        _json_response(self, 200, {"ok": True, "entry": updated[0] if updated else None})

    def _handle_timesheet_break_start(self) -> None:
        user = _require_auth(self)
        if not user: return
        entries = db.query_dicts(
            "SELECT id, break_start FROM time_entries WHERE user_id = %s AND clock_out IS NULL",
            (user["id"],),
        )
        if not entries:
            _json_response(self, 400, {"error": "Not currently clocked in"})
            return
        entry = entries[0]
        if entry.get("break_start"):
            _json_response(self, 400, {"error": "Already on break"})
            return
        db.execute(
            "UPDATE time_entries SET break_start = NOW(), updated_at = NOW() WHERE id = %s",
            (entry["id"],),
        )
        _json_response(self, 200, {"ok": True})

    def _handle_timesheet_break_end(self) -> None:
        user = _require_auth(self)
        if not user: return
        entries = db.query_dicts(
            "SELECT id, break_start FROM time_entries WHERE user_id = %s AND clock_out IS NULL",
            (user["id"],),
        )
        if not entries:
            _json_response(self, 400, {"error": "Not currently clocked in"})
            return
        entry = entries[0]
        if not entry.get("break_start"):
            _json_response(self, 400, {"error": "Not currently on break"})
            return
        bs = _parse_iso_datetime(str(entry["break_start"]))
        break_mins = int((datetime.now(timezone.utc) - bs).total_seconds() / 60) if bs else 0
        db.execute(
            "UPDATE time_entries SET break_minutes = break_minutes + %s, break_start = NULL, updated_at = NOW() WHERE id = %s",
            (break_mins, entry["id"]),
        )
        _json_response(self, 200, {"ok": True, "break_minutes": break_mins})

    def _handle_timesheet_active(self) -> None:
        user = _require_auth(self)
        if not user: return
        entries = db.query_dicts(
            """SELECT te.*, o.title as opportunity_title FROM time_entries te
               LEFT JOIN opportunities o ON o.id = te.opportunity_id
               WHERE te.user_id = %s AND te.clock_out IS NULL""",
            (user["id"],),
        )
        _json_response(self, 200, {"entry": entries[0] if entries else None})

    def _handle_timesheet_history(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user: return
        uid = (qs.get("user_id") or [""])[0]
        from_date = (qs.get("from") or [""])[0]
        to_date = (qs.get("to") or [""])[0]
        target_user = int(uid) if uid.isdigit() else user["id"]
        # Non-admin users may only read their own data. A request that asks
        # for a different user_id must be rejected explicitly so the caller
        # cannot silently coerce the result to their own data.
        if uid.isdigit() and target_user != user["id"] and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required to view other users' timesheets"})
            return
        conditions = ["te.user_id = %s"]
        params: list = [target_user]
        if from_date:
            conditions.append("te.clock_in >= %s")
            params.append(from_date)
        if to_date:
            conditions.append("te.clock_in < (%s::date + interval '1 day')")
            params.append(to_date)
        where = " AND ".join(conditions)
        rows = db.query_dicts(
            f"""SELECT te.*, o.title as opportunity_title, u.display_name as user_name
                FROM time_entries te
                LEFT JOIN opportunities o ON o.id = te.opportunity_id
                LEFT JOIN users u ON u.id = te.user_id
                WHERE {where}
                ORDER BY te.clock_in DESC""",
            tuple(params),
        )
        _json_response(self, 200, {"entries": rows})

    def _handle_timesheet_daily(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user: return
        uid = (qs.get("user_id") or [""])[0]
        from_date = (qs.get("from") or [""])[0]
        to_date = (qs.get("to") or [""])[0]
        target_user = int(uid) if uid.isdigit() else user["id"]
        # Non-admin users may only read their own data. A request that asks
        # for a different user_id must be rejected explicitly so the caller
        # cannot silently coerce the result to their own data.
        if uid.isdigit() and target_user != user["id"] and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required to view other users' timesheets"})
            return
        conditions = ["user_id = %s"]
        params: list = [target_user]
        if from_date:
            conditions.append("work_date >= %s")
            params.append(from_date)
        if to_date:
            conditions.append("work_date <= %s")
            params.append(to_date)
        where = " AND ".join(conditions)
        rows = db.query_dicts(
            f"SELECT * FROM time_entries_daily WHERE {where} ORDER BY work_date DESC",
            tuple(params),
        )
        _json_response(self, 200, {"daily": rows})

    def _handle_timesheet_admin_summary(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user: return
        if not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required"})
            return
        from_date = (qs.get("from") or [""])[0]
        to_date = (qs.get("to") or [""])[0]
        conditions = []
        params: list = []
        if from_date:
            conditions.append("ted.work_date >= %s")
            params.append(from_date)
        if to_date:
            conditions.append("ted.work_date <= %s")
            params.append(to_date)
        where = " AND ".join(conditions)
        if where:
            where = "WHERE " + where
        rows = db.query_dicts(
            f"""SELECT ted.*, u.display_name as user_name, u.email as user_email
                FROM time_entries_daily ted
                JOIN users u ON u.id = ted.user_id
                {where}
                ORDER BY u.display_name, ted.work_date DESC""",
            tuple(params),
        )
        _json_response(self, 200, {"summary": rows})

    def _handle_timesheet_admin_open_shifts(self) -> None:
        """GET /api/timesheet/admin/open-shifts — list all currently-open
        (clock_out IS NULL) shifts across all users. Admin only. Used by
        the "Open Shifts (admin)" tab in the activity modal to let admins
        see forgotten clock-outs and close them."""
        user = _require_auth(self)
        if not user: return
        if not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required"})
            return
        rows = db.query_dicts(
            """SELECT te.id, te.user_id, te.clock_in, te.break_minutes,
                      te.break_start, te.opportunity_id, te.notes,
                      te.auto_closed, te.created_at,
                      u.display_name AS user_name, u.email AS user_email,
                      o.title AS opportunity_title
               FROM time_entries te
               JOIN users u ON u.id = te.user_id
               LEFT JOIN opportunities o ON o.id = te.opportunity_id
               WHERE te.clock_out IS NULL
               ORDER BY te.clock_in ASC""",
        )
        _json_response(self, 200, {"shifts": rows})

    def _handle_timesheet_admin_close_entry(self, entry_id: int) -> None:
        """POST /api/timesheet/admin/close/{id} — admin-only close of an
        open shift. Body: { clock_out: ISO timestamp, break_minutes?: int }.
        Sets auto_closed=TRUE and writes a bot_activity_log row."""
        user = _require_auth(self)
        if not user: return
        if not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required"})
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        entries = db.query_dicts(
            "SELECT id, user_id, clock_in, break_minutes, break_start, opportunity_id "
            "FROM time_entries WHERE id = %s",
            (entry_id,),
        )
        if not entries:
            _json_response(self, 404, {"error": "Entry not found"})
            return
        entry = entries[0]
        # Reject if the entry is already closed. The `AND clock_out IS NULL`
        # guard in the UPDATE handles the race when two admins act at the
        # same time, but checking here first gives a friendlier 409.
        if entry.get("clock_in") is None:
            _json_response(self, 400, {"error": "Entry has no clock_in"})
            return
        clock_out_str = payload.get("clock_out")
        if not clock_out_str:
            _json_response(self, 400, {"error": "clock_out is required (ISO timestamp)"})
            return
        # Validate the timestamp parses.
        try:
            clock_out_dt = _parse_iso_datetime(str(clock_out_str))
        except Exception:
            _json_response(self, 400, {"error": "Invalid clock_out timestamp"})
            return
        if clock_out_dt is None:
            _json_response(self, 400, {"error": "Invalid clock_out timestamp"})
            return

        # If the entry has an active break, fold the elapsed break time
        # into break_minutes. Mirrors the original _handle_timesheet_clock_out
        # behavior the user explicitly asked us to preserve (Q3 answer).
        extra_break = 0
        if entry.get("break_start"):
            try:
                bs = _parse_iso_datetime(str(entry["break_start"]))
                if bs:
                    # Use the admin-supplied clock_out (not NOW()) so the
                    # math matches the timestamp the admin confirmed.
                    delta = (clock_out_dt - bs).total_seconds()
                    if delta > 0:
                        extra_break = int(delta / 60)
            except Exception:
                pass

        # Atomic close: only update if still open. 0 rows affected = race lost.
        rows = db.query_dicts(
            """UPDATE time_entries
               SET clock_out = %s,
                   break_minutes = break_minutes + %s,
                   break_start = NULL,
                   auto_closed = TRUE,
                   updated_at = NOW()
               WHERE id = %s AND clock_out IS NULL
               RETURNING id, user_id, clock_in, clock_out, break_minutes,
                         break_start, opportunity_id, notes, auto_closed""",
            (clock_out_dt, extra_break, entry_id),
        )
        if not rows:
            _json_response(self, 409, {"error": "Entry was already closed by another admin"})
            return
        updated = rows[0]

        # Audit trail: log the close to bot_activity_log so it shows up in
        # the existing admin bot-activity viewer.
        try:
            db.execute(
                """INSERT INTO bot_activity_log (chat_id, user_name, action, detail, project_id)
                   VALUES (%s, %s, 'timesheet_auto_closed', %s, %s)""",
                (
                    0,
                    user.get("display_name") or user.get("email") or "admin",
                    f"Closed open shift for user {updated['user_id']} from {updated['clock_in']} to {updated['clock_out']}",
                    updated.get("opportunity_id"),
                ),
            )
        except Exception:
            # Audit failure must not roll back the close.
            pass

        _json_response(self, 200, {"ok": True, "entry": updated})

    def _handle_timesheet_entry_update(self, entry_id: int) -> None:
        user = _require_auth(self)
        if not user: return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        entries = db.query_dicts("SELECT * FROM time_entries WHERE id = %s", (entry_id,))
        if not entries:
            _json_response(self, 404, {"error": "Entry not found"})
            return
        entry = entries[0]
        if entry["user_id"] != user["id"] and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Not authorized"})
            return
        opp_id = payload.get("opportunity_id")
        notes = payload.get("notes")
        break_mins = payload.get("break_minutes")
        updates = ["updated_at = NOW()"]
        params: list = []
        if opp_id is not None:
            updates.append("opportunity_id = %s")
            params.append(opp_id if opp_id else None)
        if notes is not None:
            updates.append("notes = %s")
            params.append(notes)
        if break_mins is not None:
            updates.append("break_minutes = %s")
            params.append(int(break_mins))
        params.append(entry_id)
        db.execute(
            f"UPDATE time_entries SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )
        updated = db.query_dicts("SELECT * FROM time_entries WHERE id = %s", (entry_id,))
        _json_response(self, 200, {"ok": True, "entry": updated[0] if updated else None})

    def _handle_timesheet_entry_delete(self, entry_id: int) -> None:
        user = _require_auth(self)
        if not user: return
        entries = db.query_dicts("SELECT * FROM time_entries WHERE id = %s", (entry_id,))
        if not entries:
            _json_response(self, 404, {"error": "Entry not found"})
            return
        if entries[0]["user_id"] != user["id"] and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Not authorized"})
            return
        db.execute("DELETE FROM time_entries WHERE id = %s", (entry_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_timesheet_export_csv(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user: return
        uid = (qs.get("user_id") or [""])[0]
        from_date = (qs.get("from") or [""])[0]
        to_date = (qs.get("to") or [""])[0]
        target_user = int(uid) if uid.isdigit() else user["id"]
        # Non-admin users may only export their own data. A request that asks
        # for a different user_id must be rejected explicitly so the caller
        # cannot silently coerce the result to their own data.
        if uid.isdigit() and target_user != user["id"] and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required to export other users' timesheets"})
            return
        conditions = ["te.user_id = %s"]
        params: list = [target_user]
        if from_date:
            conditions.append("te.clock_in >= %s")
            params.append(from_date)
        if to_date:
            conditions.append("te.clock_in < (%s::date + interval '1 day')")
            params.append(to_date)
        where = " AND ".join(conditions)
        rows = db.query_dicts(
            f"""SELECT te.*, o.title as opportunity_title, u.display_name as user_name
                FROM time_entries te
                LEFT JOIN opportunities o ON o.id = te.opportunity_id
                LEFT JOIN users u ON u.id = te.user_id
                WHERE {where}
                ORDER BY te.clock_in""",
            tuple(params),
        )
        import csv
        import io
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=[
            "user_name", "clock_in", "clock_out", "break_minutes",
            "opportunity_title", "notes", "auto_closed"
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "user_name": r.get("user_name", ""),
                "clock_in": r.get("clock_in", ""),
                "clock_out": r.get("clock_out", ""),
                "break_minutes": r.get("break_minutes", 0),
                "opportunity_title": r.get("opportunity_title", ""),
                "notes": r.get("notes", ""),
                "auto_closed": r.get("auto_closed", False),
            })
        csv_data = output.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(csv_data)))
        self.send_header("Content-Disposition",
            f'attachment; filename="timesheet-{target_user}-{from_date or "all"}-to-{to_date or "all"}.csv"')
        self.end_headers()
        self.wfile.write(csv_data)

    def _handle_timesheet_export_pdf(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user: return
        try:
            from fpdf import FPDF
        except ImportError:
            _json_response(self, 500, {"error": "fpdf2 not installed. Run: pip install fpdf2"})
            return
        uid = (qs.get("user_id") or [""])[0]
        from_date = (qs.get("from") or [""])[0]
        to_date = (qs.get("to") or [""])[0]
        target_user = int(uid) if uid.isdigit() else user["id"]
        # Non-admin users may only export their own data. A request that asks
        # for a different user_id must be rejected explicitly so the caller
        # cannot silently coerce the result to their own data.
        if uid.isdigit() and target_user != user["id"] and not user.get("is_admin"):
            _json_response(self, 403, {"error": "Admin required to export other users' timesheets"})
            return
        users = db.query_dicts("SELECT display_name, email FROM users WHERE id = %s", (target_user,))
        user_info = users[0] if users else {"display_name": "Unknown", "email": ""}
        conditions = ["te.user_id = %s"]
        params: list = [target_user]
        if from_date:
            conditions.append("te.clock_in >= %s")
            params.append(from_date)
        if to_date:
            conditions.append("te.clock_in < (%s::date + interval '1 day')")
            params.append(to_date)
        where = " AND ".join(conditions)
        rows = db.query_dicts(
            f"""SELECT te.*, o.title as opportunity_title
                FROM time_entries te
                LEFT JOIN opportunities o ON o.id = te.opportunity_id
                WHERE {where}
                ORDER BY te.clock_in""",
            tuple(params),
        )
        daily_conditions = ["user_id = %s"]
        daily_params: list = [target_user]
        if from_date:
            daily_conditions.append("work_date >= %s")
            daily_params.append(from_date)
        if to_date:
            daily_conditions.append("work_date <= %s")
            daily_params.append(to_date)
        daily_where = " AND ".join(daily_conditions)
        daily_rows = db.query_dicts(
            f"SELECT * FROM time_entries_daily WHERE {daily_where} ORDER BY work_date",
            tuple(daily_params),
        )
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Sietch CRM", ln=True, align="C")
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 8, "Timesheet Report", ln=True, align="C")
        pdf.ln(5)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, f"Employee: {user_info['display_name']}", ln=True)
        period = f"{from_date or 'All time'}"
        if to_date:
            period += f" to {to_date}"
        pdf.cell(0, 7, f"Period: {period}", ln=True)
        pdf.ln(5)
        if rows:
            col_widths = [25, 22, 22, 18, 16, 35, 42]
            headers = ["Date", "In", "Out", "Hrs", "Brk", "Project", "Notes"]
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_fill_color(230, 230, 230)
            for w, h in zip(col_widths, headers):
                pdf.cell(w, 6, h, border=1, fill=True, align="C")
            pdf.ln()
            pdf.set_font("Helvetica", "", 7)
            def _pdf_safe(text):
                """Strip non-Latin-1 characters for fpdf2 Helvetica font."""
                if not text:
                    return ""
                return text.encode("latin-1", errors="replace").decode("latin-1")

            for r in rows:
                ci = _parse_iso_datetime(str(r["clock_in"])) if r.get("clock_in") else None
                co = _parse_iso_datetime(str(r["clock_out"])) if r.get("clock_out") else None
                date_str = ci.strftime("%Y-%m-%d") if ci else "-"
                in_str = ci.strftime("%H:%M") if ci else "-"
                out_str = co.strftime("%H:%M") if co else "-"
                dur = 0
                if ci and co:
                    dur = (co - ci).total_seconds() / 3600 - (r.get("break_minutes") or 0) / 60
                dur_str = f"{dur:.1f}" if dur else "-"
                brk_str = f"{r.get('break_minutes', 0)}m"
                proj = _pdf_safe((r.get("opportunity_title") or "-")[:20])
                notes = _pdf_safe((r.get("notes") or "-")[:25])
                pdf.cell(col_widths[0], 5, date_str, border=1)
                pdf.cell(col_widths[1], 5, in_str, border=1, align="C")
                pdf.cell(col_widths[2], 5, out_str, border=1, align="C")
                pdf.cell(col_widths[3], 5, dur_str, border=1, align="C")
                pdf.cell(col_widths[4], 5, brk_str, border=1, align="C")
                pdf.cell(col_widths[5], 5, proj, border=1)
                pdf.cell(col_widths[6], 5, notes, border=1)
                pdf.ln()
        pdf.ln(5)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, "Daily Totals", ln=True)
        if daily_rows:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_fill_color(230, 230, 230)
            pdf.cell(50, 6, "Date", border=1, fill=True, align="C")
            pdf.cell(25, 6, "Hours", border=1, fill=True, align="C")
            pdf.cell(25, 6, "Break", border=1, fill=True, align="C")
            pdf.ln()
            pdf.set_font("Helvetica", "", 9)
            grand_hours = 0
            grand_break = 0
            for d in daily_rows:
                h = float(d.get("total_hours") or 0)
                b = int(d.get("total_break_minutes") or 0)
                grand_hours += h
                grand_break += b
                pdf.cell(50, 6, str(d["work_date"]), border=1, align="C")
                pdf.cell(25, 6, f"{h:.1f}", border=1, align="C")
                pdf.cell(25, 6, f"{b}m", border=1, align="C")
                pdf.ln()
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(50, 6, "TOTAL", border=1, align="C")
            pdf.cell(25, 6, f"{grand_hours:.1f}", border=1, align="C")
            pdf.cell(25, 6, f"{grand_break}m", border=1, align="C")
            pdf.ln()
        pdf_output = pdf.output()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(pdf_output)))
        self.send_header("Content-Disposition",
            f'attachment; filename="timesheet-{target_user}-{from_date or "all"}-to-{to_date or "all"}.pdf"')
        self.end_headers()
        self.wfile.write(pdf_output)

    # ── Calendar v2 API Handlers ──────────────────────────────────────────────

    def _handle_calendar_sources_get(self) -> None:
        """List user's calendar sources."""
        user = _require_auth(self)
        if not user: return
        sources = db.query_dicts(
            "SELECT id, name, source_type, color, visible, last_synced_at, ics_url, oauth_email, caldav_url, "
            "caldav_username, caldav_password_encrypted "
            "FROM calendar_sources WHERE user_id = %s ORDER BY name",
            (user["id"],),
        )
        # Sanitize: never expose tokens
        for s in sources:
            s.pop("oauth_access_token", None)
            s.pop("oauth_refresh_token", None)
            has_pw = bool(s.pop("caldav_password_encrypted", None))
            s["has_password"] = has_pw
        _json_response(self, 200, {
            "sources": sources,
            "oauth": {
                "google": bool(oauth_providers.GOOGLE_CLIENT_ID),
                "microsoft": bool(oauth_providers.MICROSOFT_CLIENT_ID),
            },
        })

    def _handle_calendar_events_get(self, qs: dict) -> None:
        """List events in a date range from the local DB."""
        user = _require_auth(self)
        if not user: return
        start_str = (qs.get("start") or [""])[0]
        end_str = (qs.get("end") or [""])[0]
        source_id = (qs.get("source_id") or [""])[0]
        if not start_str or not end_str:
            _json_response(self, 400, {"error": "start and end query params required"})
            return
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start_dt.tzinfo: start_dt = start_dt.astimezone(timezone.utc).replace(tzinfo=None)
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_dt.tzinfo: end_dt = end_dt.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            _json_response(self, 400, {"error": "Invalid date format"})
            return
        conditions = ["ce.user_id = %s", "ce.status != 'CANCELLED'", "ce.dtstart < %s", "ce.dtend > %s",
                      "(cs.visible IS NULL OR cs.visible != FALSE)"]
        params: list = [user["id"], end_dt, start_dt]
        if source_id and source_id.isdigit():
            conditions.append("ce.source_id = %s")
            params.append(int(source_id))
        where = " AND ".join(conditions)
        events = db.query_dicts(
            f"""SELECT ce.id, ce.uid, ce.summary, ce.description, ce.location,
                       ce.dtstart, ce.dtend, ce.all_day, ce.rrule, ce.color, ce.status,
                       ce.reminder_minutes, ce.source_id, cs.name AS source_name, cs.source_type,
                       ce.recurrence_id, ce.is_occurrence,
                       ce.transparency, ce.url, ce.attendees, ce.timezone
                FROM calendar_events ce
                LEFT JOIN calendar_sources cs ON cs.id = ce.source_id
                WHERE {where}
                ORDER BY ce.dtstart""",
            tuple(params),
        )
        try:
            from ics_calendar import _humanize_rrule
        except ImportError:
            _humanize_rrule = lambda r: r  # noqa: E731
        for ev in events:
            try:
                ev["recurrence_id"] = ev.get("recurrence_id")
                ev["is_occurrence"] = bool(ev.get("is_occurrence"))
            except Exception:
                pass
            try:
                from calendar_sync import parse_attendees_json
                ev["attendees"] = parse_attendees_json(ev.get("attendees"))
            except Exception:
                ev["attendees"] = []
            if ev.get("rrule"):
                try:
                    ev["recurrence"] = _humanize_rrule(ev["rrule"])
                except Exception:
                    ev["recurrence"] = ev["rrule"]
        _json_response(self, 200, {"events": events})

    def _handle_calendar_settings_get(self) -> None:
        """Get user's calendar reminder/notification settings."""
        user = _require_auth(self)
        if not user: return
        rows = db.query_dicts(
            "SELECT notification_type, enabled FROM notification_preferences WHERE user_id = %s",
            (user["id"],),
        )
        prefs = {r["notification_type"]: r["enabled"] for r in rows}
        _json_response(self, 200, {
            "browser_notifications": prefs.get("calendar_browser", True),
            # NOTE: the dispatcher gates Telegram on telegram_<type>, so the
            # calendar toggle must use telegram_calendar_reminder (a legacy
            # calendar_telegram row is honored as fallback).
            "telegram_notifications": prefs.get(
                "telegram_calendar_reminder", prefs.get("calendar_telegram", True)),
            "default_reminder_minutes": 15,
        })

    def _handle_calendar_settings_put(self) -> None:
        """Update user's calendar reminder/notification settings."""
        user = _require_auth(self)
        if not user: return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        for key, db_type in [("browser_notifications", "calendar_browser"), ("telegram_notifications", "telegram_calendar_reminder")]:
            if key in body:
                val = bool(body[key])
                db.execute(
                    """INSERT INTO notification_preferences (user_id, notification_type, enabled)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, notification_type) DO UPDATE SET enabled = %s""",
                    (user["id"], db_type, val, val),
                )
        _json_response(self, 200, {"ok": True})

    def _handle_calendar_source_create(self) -> None:
        """Add a new calendar source (ICS URL, CalDAV, or OAuth)."""
        user = _require_auth(self)
        if not user: return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        source_type = (body.get("source_type") or "").strip()
        name = (body.get("name") or "").strip()
        color = (body.get("color") or "#5b8abf").strip()
        if source_type not in ("ics", "google_oauth", "microsoft_oauth", "caldav"):
            _json_response(self, 400, {"error": "Invalid source_type"})
            return
        if not name:
            name = {"ics": "ICS Feed", "google_oauth": "Google Calendar", "microsoft_oauth": "Outlook Calendar", "caldav": "CalDAV"}.get(source_type, "Calendar")
        ics_url = (body.get("ics_url") or "").strip() or None
        caldav_url = (body.get("caldav_url") or "").strip() or None
        caldav_username = (body.get("caldav_username") or "").strip() or None
        caldav_password = (body.get("caldav_password") or "").strip() or None
        caldav_password_encrypted = caldav_password if caldav_password else None
        source_id = db.insert_returning(
            """INSERT INTO calendar_sources (user_id, name, source_type, color, ics_url, caldav_url, caldav_username, caldav_password_encrypted)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (user["id"], name, source_type, color, ics_url, caldav_url, caldav_username, caldav_password_encrypted),
        )
        # If ICS, trigger immediate sync
        if source_type == "ics" and ics_url:
            try:
                from calendar_sync import _fetch_ics_feed, _store_ics_cache, _sync_ics_events_to_db
                from ics_calendar import parse_ics_calendar
                raw_text, error = _fetch_ics_feed(ics_url)
                if raw_text:
                    parsed = parse_ics_calendar(raw_text)
                    _store_ics_cache(ics_url, raw_text, parsed)
                    _sync_ics_events_to_db(user["id"], source_id, parsed)
                    db.execute("UPDATE calendar_sources SET last_synced_at = NOW() WHERE id = %s", (source_id,))
            except Exception as e:
                print(f"[WARN] Initial ICS sync failed: {e}", file=sys.stderr)
        _json_response(self, 200, {"ok": True, "id": source_id})

    def _handle_calendar_source_update(self, source_id: int) -> None:
        """Update a calendar source (name, color, visibility)."""
        user = _require_auth(self)
        if not user: return
        src = db.query_one("SELECT * FROM calendar_sources WHERE id = %s AND user_id = %s", (source_id, user["id"]))
        if not src:
            _json_response(self, 404, {"error": "Source not found"})
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        if "name" in body:
            db.execute("UPDATE calendar_sources SET name = %s WHERE id = %s", ((body["name"] or "").strip() or "Calendar", source_id))
        if "color" in body:
            db.execute("UPDATE calendar_sources SET color = %s WHERE id = %s", ((body["color"] or "#5b8abf").strip(), source_id))
        if "visible" in body:
            db.execute("UPDATE calendar_sources SET visible = %s WHERE id = %s", (bool(body["visible"]), source_id))
        _json_response(self, 200, {"ok": True})

    def _handle_calendar_source_delete(self, source_id: int) -> None:
        """Delete a calendar source and its events."""
        user = _require_auth(self)
        if not user: return
        src = db.query_one("SELECT * FROM calendar_sources WHERE id = %s AND user_id = %s", (source_id, user["id"]))
        if not src:
            _json_response(self, 404, {"error": "Source not found"})
            return
        db.execute("DELETE FROM calendar_events WHERE source_id = %s", (source_id,))
        db.execute("DELETE FROM calendar_sources WHERE id = %s", (source_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_calendar_source_sync(self, source_id: int) -> None:
        """Trigger sync for a specific ICS source."""
        user = _require_auth(self)
        if not user: return
        src = db.query_one("SELECT * FROM calendar_sources WHERE id = %s AND user_id = %s", (source_id, user["id"]))
        if not src:
            _json_response(self, 404, {"error": "Source not found"})
            return
        if src["source_type"] != "ics" or not src.get("ics_url"):
            _json_response(self, 400, {"error": "Only ICS sources can be synced manually"})
            return
        try:
            from calendar_sync import _fetch_ics_feed, _store_ics_cache, _sync_ics_events_to_db
            from ics_calendar import parse_ics_calendar
            raw_text, error = _fetch_ics_feed(src["ics_url"])
            if error:
                _json_response(self, 502, {"error": f"Feed fetch failed: {error}"})
                return
            parsed = parse_ics_calendar(raw_text)
            _store_ics_cache(src["ics_url"], raw_text, parsed)
            count = _sync_ics_events_to_db(user["id"], source_id, parsed)
            db.execute("UPDATE calendar_sources SET last_synced_at = NOW() WHERE id = %s", (source_id,))
            _json_response(self, 200, {"ok": True, "events_synced": count})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_calendar_sync_all(self) -> None:
        """Trigger sync for all user's sources."""
        user = _require_auth(self)
        if not user: return
        sources = db.query_dicts("SELECT id, source_type, ics_url FROM calendar_sources WHERE user_id = %s", (user["id"],))
        synced = 0
        for src in sources:
            if src["source_type"] == "ics" and src.get("ics_url"):
                try:
                    from calendar_sync import _fetch_ics_feed, _store_ics_cache, _sync_ics_events_to_db
                    from ics_calendar import parse_ics_calendar
                    raw_text, _ = _fetch_ics_feed(src["ics_url"])
                    if raw_text:
                        parsed = parse_ics_calendar(raw_text)
                        _store_ics_cache(src["ics_url"], raw_text, parsed)
                        _sync_ics_events_to_db(user["id"], src["id"], parsed)
                        db.execute("UPDATE calendar_sources SET last_synced_at = NOW() WHERE id = %s", (src["id"],))
                        synced += 1
                except Exception:
                    pass
        _json_response(self, 200, {"ok": True, "synced": synced})

    def _handle_calendar_event_create(self) -> None:
        """Create a local calendar event."""
        user = _require_auth(self)
        if not user: return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        summary = (body.get("summary") or "").strip()
        if not summary:
            _json_response(self, 400, {"error": "summary is required"})
            return
        dtstart_str = (body.get("dtstart") or "").strip()
        if not dtstart_str:
            _json_response(self, 400, {"error": "dtstart is required"})
            return
        all_day = bool(body.get("all_day", False))
        try:
            if all_day and len(dtstart_str) == 10:
                dtstart = datetime.strptime(dtstart_str, "%Y-%m-%d")
            else:
                dtstart = datetime.fromisoformat(dtstart_str.replace("Z", "+00:00"))
                if dtstart.tzinfo: dtstart = dtstart.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            _json_response(self, 400, {"error": "Invalid dtstart format"})
            return
        dtend_str = (body.get("dtend") or "").strip()
        if dtend_str:
            try:
                if all_day and len(dtend_str) == 10:
                    dtend = datetime.strptime(dtend_str, "%Y-%m-%d")
                else:
                    dtend = datetime.fromisoformat(dtend_str.replace("Z", "+00:00"))
                    if dtend.tzinfo: dtend = dtend.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))
        else:
            dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))
        if dtend <= dtstart:
            dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))
        description = (body.get("description") or "").strip()
        location = (body.get("location") or "").strip()
        color = (body.get("color") or "").strip() or None
        rrule = (body.get("rrule") or "").strip() or None
        reminder = body.get("reminder_minutes")
        transparency = (body.get("transparency") or "").strip().upper() or None
        if transparency not in ("TRANSPARENT", "OPAQUE", None):
            transparency = None
        url = (body.get("url") or "").strip() or None
        attendees_list = body.get("attendees")
        event_timezone = (body.get("timezone") or "").strip() or None
        source_id = body.get("source_id")
        if source_id == "" or source_id == "null":
            source_id = None
        elif source_id and str(source_id).isdigit():
            source_id = int(source_id)
        else:
            source_id = None
        uid = f"local-{user['id']}-{int(time.time()*1000)}"
        from calendar_sync import build_local_series_rows
        rows = build_local_series_rows({
            "uid": uid, "summary": summary, "description": description, "location": location,
            "dtstart": dtstart, "dtend": dtend, "all_day": all_day, "rrule": rrule or "",
            "color": color or "", "status": "CONFIRMED", "reminder_minutes": reminder,
            "transparency": transparency or "", "url": url or "",
            "attendees_list": attendees_list, "timezone": event_timezone or "",
        })
        first_id = None
        for row in rows:
            rid = db.insert_returning(
                """INSERT INTO calendar_events (user_id, source_id, uid, summary, description, location,
                   dtstart, dtend, all_day, rrule, color, reminder_minutes,
                   transparency, url, attendees, timezone, recurrence_id, is_occurrence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (user["id"], source_id, row["uid"], row["summary"], row["description"], row["location"],
                 row["dtstart"], row["dtend"], row["all_day"], row["rrule"], row["color"],
                 row["reminder_minutes"], row["transparency"], row["url"], row["attendees"],
                 row["timezone"], row["recurrence_id"], row["is_occurrence"]),
            )
            if first_id is None:
                first_id = rid
        _json_response(self, 200, {"ok": True, "id": first_id, "uid": uid, "occurrences": len(rows)})

    def _handle_calendar_event_update(self, event_id: int) -> None:
        """Update a local calendar event.

        edit_scope="series" rewrites every row of the series (local series
        only); "single" (default) updates just this row and detaches an
        edited occurrence from its series.
        """
        user = _require_auth(self)
        if not user: return
        ev = db.query_one("SELECT * FROM calendar_events WHERE id = %s AND user_id = %s", (event_id, user["id"]))
        if not ev:
            _json_response(self, 404, {"error": "Event not found"})
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        from calendar_sync import build_local_series_rows, parse_attendees_json, serialize_attendees
        scope = "series" if (body.get("edit_scope") or "") == "series" else "single"
        master_uid = ev.get("recurrence_id") or ev.get("uid")
        is_series = bool(ev.get("is_occurrence") or ev.get("recurrence_id") or ev.get("rrule"))

        if scope == "series" and is_series:
            # Rebuild the whole series from merged fields.
            cur_att = parse_attendees_json(ev.get("attendees"))
            merged = {
                "uid": master_uid,
                "summary": body.get("summary", ev.get("summary") or "").strip(),
                "description": body.get("description", ev.get("description") or "").strip(),
                "location": body.get("location", ev.get("location") or "").strip(),
                "dtstart": ev.get("dtstart"),
                "dtend": ev.get("dtend"),
                "all_day": bool(body.get("all_day", ev.get("all_day", False))),
                "rrule": (body.get("rrule", ev.get("rrule") or "") or "").strip(),
                "color": (body.get("color", ev.get("color") or "") or "").strip(),
                "status": (body.get("status", ev.get("status") or "CONFIRMED") or "CONFIRMED"),
                "reminder_minutes": body.get("reminder_minutes", ev.get("reminder_minutes")),
                "transparency": (body.get("transparency", ev.get("transparency") or "") or "").strip(),
                "url": (body.get("url", ev.get("url") or "") or "").strip(),
                "attendees_list": body.get("attendees", cur_att),
                "timezone": (body.get("timezone", ev.get("timezone") or "") or "").strip(),
            }
            for key in ("dtstart", "dtend"):
                if key in body:
                    try:
                        dt = datetime.fromisoformat(body[key].replace("Z", "+00:00"))
                        if dt.tzinfo: dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        merged[key] = dt
                    except (ValueError, AttributeError):
                        pass
            if isinstance(merged["dtstart"], datetime) and isinstance(merged["dtend"], datetime):
                if merged["dtend"] <= merged["dtstart"]:
                    merged["dtend"] = merged["dtstart"] + (
                        timedelta(days=1) if merged["all_day"] else timedelta(hours=1))
            new_source = ev.get("source_id")
            if "source_id" in body:
                sid = body.get("source_id")
                new_source = int(sid) if sid and str(sid).isdigit() else None
            db.execute(
                "DELETE FROM calendar_events WHERE user_id = %s AND (uid = %s OR recurrence_id = %s)",
                (user["id"], master_uid, master_uid),
            )
            rows = build_local_series_rows(merged)
            for row in rows:
                db.execute(
                    """INSERT INTO calendar_events (user_id, source_id, uid, summary, description, location,
                       dtstart, dtend, all_day, rrule, color, reminder_minutes,
                       transparency, url, attendees, timezone, recurrence_id, is_occurrence,
                       status, last_modified)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (user["id"], new_source, row["uid"], row["summary"], row["description"], row["location"],
                     row["dtstart"], row["dtend"], row["all_day"], row["rrule"], row["color"],
                     row["reminder_minutes"], row["transparency"], row["url"], row["attendees"],
                     row["timezone"], row["recurrence_id"], row["is_occurrence"], row["status"]),
                )
            _json_response(self, 200, {"ok": True, "occurrences": len(rows)})
            return

        # Single-row update (detaches an edited occurrence from its series).
        updates = []
        params = []
        for field in ["summary", "description", "location", "color", "status", "transparency", "url", "timezone"]:
            if field in body:
                updates.append(f"{field} = %s")
                val = body[field]
                params.append(val.strip() if isinstance(val, str) else val)
        if "attendees" in body:
            updates.append("attendees = %s")
            params.append(serialize_attendees(body["attendees"]))
        if "rrule" in body:
            updates.append("rrule = %s")
            params.append((body["rrule"] or "").strip() or None)
        if "reminder_minutes" in body:
            updates.append("reminder_minutes = %s")
            updates.append("reminder_sent = FALSE")
            params.append(body["reminder_minutes"])
        if "dtstart" in body:
            try:
                dtstart = datetime.fromisoformat(body["dtstart"].replace("Z", "+00:00"))
                if dtstart.tzinfo: dtstart = dtstart.astimezone(timezone.utc).replace(tzinfo=None)
                updates.append("dtstart = %s")
                params.append(dtstart)
            except ValueError:
                pass
        if "dtend" in body:
            try:
                dtend = datetime.fromisoformat(body["dtend"].replace("Z", "+00:00"))
                if dtend.tzinfo: dtend = dtend.astimezone(timezone.utc).replace(tzinfo=None)
                updates.append("dtend = %s")
                params.append(dtend)
            except ValueError:
                pass
        if "all_day" in body:
            updates.append("all_day = %s")
            params.append(bool(body["all_day"]))
        if "source_id" in body:
            sid = body.get("source_id")
            updates.append("source_id = %s")
            params.append(int(sid) if sid and str(sid).isdigit() else None)
        if ev.get("is_occurrence"):
            # Detach: an individually edited occurrence becomes a standalone event.
            updates.append("recurrence_id = NULL")
            updates.append("is_occurrence = FALSE")
            updates.append("rrule = NULL")
        if updates:
            params.append(event_id)
            db.execute(f"UPDATE calendar_events SET {', '.join(updates)}, last_modified = NOW() WHERE id = %s", tuple(params))
        _json_response(self, 200, {"ok": True})

    def _handle_calendar_import(self) -> None:
        """Import a pasted/uploaded .ics file as local events (recurring expanded)."""
        from ics_calendar import (
            EXPANSION_FUTURE_DAYS,
            EXPANSION_PAST_DAYS,
            _event_start_naive,
            expand_calendar_events,
            parse_ics_calendar,
        )
        from calendar_sync import serialize_attendees
        user = _require_auth(self)
        if not user: return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        ics_text = body.get("ics_text") or ""
        if not isinstance(ics_text, str) or not ics_text.strip():
            _json_response(self, 400, {"error": "ics_text is required"})
            return
        if len(ics_text) > 1024 * 1024:
            _json_response(self, 413, {"error": "ICS file too large (1 MB max)"})
            return
        source_id = body.get("source_id")
        if source_id and str(source_id).isdigit():
            src = db.query_one(
                "SELECT id FROM calendar_sources WHERE id = %s AND user_id = %s",
                (int(source_id), user["id"]),
            )
            source_id = int(source_id) if src else None
        else:
            source_id = None
        try:
            parsed = parse_ics_calendar(ics_text)
        except Exception as exc:
            _json_response(self, 422, {"error": f"Could not parse calendar: {exc}"})
            return
        today = datetime.now(timezone.utc).date()
        expanded = expand_calendar_events(
            [e for e in parsed.get("events", []) if (e.get("status") or "") != "CANCELLED"],
            today - timedelta(days=EXPANSION_PAST_DAYS),
            today + timedelta(days=EXPANSION_FUTURE_DAYS),
        )[:2000]
        stamp = int(time.time() * 1000)
        imported = 0
        for n, ev in enumerate(expanded):
            start_dt = _event_start_naive(ev)
            end_dt = _event_start_naive({"start": ev.get("end")}) or start_dt
            if not start_dt:
                continue
            new_master = f"import-{user['id']}-{stamp}-{n}"
            if ev.get("isOccurrence") and "#" in (ev.get("uid") or ""):
                new_uid = new_master + "#" + ev["uid"].split("#", 1)[1]
            elif ev.get("isOccurrence"):
                new_uid = f"{new_master}#{start_dt.strftime('%Y%m%dT%H%M%S')}"
            else:
                new_uid = new_master
            try:
                db.execute(
                    """INSERT INTO calendar_events (user_id, source_id, uid, summary, description, location,
                       dtstart, dtend, all_day, rrule, color, status, reminder_minutes,
                       transparency, url, attendees, timezone, recurrence_id, is_occurrence, last_modified)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                       ON CONFLICT (user_id, uid) DO NOTHING""",
                    (user["id"], source_id, new_uid, ev.get("summary", ""), ev.get("description", ""),
                     ev.get("location", ""), start_dt, end_dt, bool((ev.get("start") or {}).get("allDay")),
                     ev.get("rrule") or None, ev.get("color") or None,
                     (ev.get("status") or "CONFIRMED").upper(), ev.get("alarmMinutes"),
                     (ev.get("transparency") or "").strip() or None, (ev.get("url") or "").strip() or None,
                     serialize_attendees(ev.get("attendees")),
                     (ev.get("start") or {}).get("tzid") or None,
                     new_master if ev.get("isOccurrence") else None, bool(ev.get("isOccurrence"))),
                )
                imported += 1
            except Exception:
                continue
        _json_response(self, 200, {"ok": True, "imported": imported})

    def _handle_calendar_event_delete(self, event_id: int) -> None:
        """Delete a calendar event (?scope=series deletes the whole local series)."""
        user = _require_auth(self)
        if not user: return
        ev = db.query_one("SELECT * FROM calendar_events WHERE id = %s AND user_id = %s", (event_id, user["id"]))
        if not ev:
            _json_response(self, 404, {"error": "Event not found"})
            return
        qs = parse_qs(urlparse(self.path).query)
        scope = (qs.get("scope") or [""])[0]
        master_uid = ev.get("recurrence_id") or ev.get("uid")
        if scope == "series" and (ev.get("is_occurrence") or ev.get("recurrence_id") or ev.get("rrule")):
            db.execute(
                "DELETE FROM calendar_events WHERE user_id = %s AND (uid = %s OR recurrence_id = %s)",
                (user["id"], master_uid, master_uid),
            )
            _json_response(self, 200, {"ok": True, "scope": "series"})
            return
        db.execute("DELETE FROM calendar_events WHERE id = %s", (event_id,))
        _json_response(self, 200, {"ok": True})

    def _handle_calendar_event_snooze(self, event_id: int) -> None:
        """Snooze the next reminder by `minutes` (default 10) or `until` ISO."""
        user = _require_auth(self)
        if not user: return
        ev = db.query_one("SELECT id FROM calendar_events WHERE id = %s AND user_id = %s", (event_id, user["id"]))
        if not ev:
            _json_response(self, 404, {"error": "Event not found"})
            return
        try:
            body = json.loads(_read_body(self) or b"{}")
        except Exception:
            body = {}
        minutes_raw = body.get("minutes")
        try:
            minutes = int(minutes_raw) if minutes_raw is not None and str(minutes_raw) != "" else 10
        except (TypeError, ValueError):
            minutes = 10
        until_str = (body.get("until") or "").strip()
        if until_str:
            try:
                until = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
                if until.tzinfo: until = until.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                _json_response(self, 400, {"error": "Invalid until"})
                return
        elif minutes and minutes > 0:
            until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)
        else:
            _json_response(self, 400, {"error": "minutes or until is required"})
            return
        # Don't snooze past the event start.
        start = db.query_one("SELECT dtstart FROM calendar_events WHERE id = %s", (event_id,))
        if start and start["dtstart"] and until >= start["dtstart"]:
            until = start["dtstart"] - timedelta(minutes=1)
        db.execute(
            "UPDATE calendar_events SET snoozed_until = %s, last_modified = NOW() WHERE id = %s",
            (until, event_id),
        )
        _json_response(self, 200, {"ok": True, "snoozed_until": until.isoformat()})

    def _handle_calendar_event_dismiss_reminder(self, event_id: int) -> None:
        """Permanently dismiss the next reminder for this event (one-shot)."""
        user = _require_auth(self)
        if not user: return
        ev = db.query_one("SELECT id FROM calendar_events WHERE id = %s AND user_id = %s", (event_id, user["id"]))
        if not ev:
            _json_response(self, 404, {"error": "Event not found"})
            return
        db.execute(
            "UPDATE calendar_events SET reminder_sent = TRUE, last_modified = NOW() WHERE id = %s",
            (event_id,),
        )
        _json_response(self, 200, {"ok": True})

    def _handle_calendar_search(self, qs: dict) -> None:
        """User-wide search across all calendar events (paginated, ?q=)."""
        user = _require_auth(self)
        if not user: return
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            _json_response(self, 400, {"error": "q is required"})
            return
        try:
            limit = max(1, min(int((qs.get("limit") or [50])[0]), 200))
        except ValueError:
            limit = 50
        try:
            offset = max(0, int((qs.get("offset") or [0])[0]))
        except ValueError:
            offset = 0
        like = f"%{q}%"
        conditions = [
            "ce.user_id = %s",
            "ce.status != 'CANCELLED'",
            "(ce.summary ILIKE %s OR ce.description ILIKE %s OR ce.location ILIKE %s)",
        ]
        params: list = [user["id"], like, like, like]
        where = " AND ".join(conditions)
        events = db.query_dicts(
            f"""SELECT ce.id, ce.uid, ce.summary, ce.description, ce.location,
                       ce.dtstart, ce.dtend, ce.all_day, ce.color,
                       ce.source_id, cs.name AS source_name
                FROM calendar_events ce
                LEFT JOIN calendar_sources cs ON cs.id = ce.source_id
                WHERE {where}
                ORDER BY ce.dtstart DESC
                LIMIT %s OFFSET %s""",
            tuple(params) + (limit, offset),
        )
        total = db.query_one(
            f"SELECT COUNT(*) AS c FROM calendar_events ce WHERE {where}",
            tuple(params),
        ) or {"c": 0}
        _json_response(self, 200, {
            "events": events,
            "total": total.get("c", 0),
            "limit": limit,
            "offset": offset,
        })

    def _handle_calendar_export(self, qs: dict) -> None:
        """Download current view as a fresh .ics file."""
        user = _require_auth(self)
        if not user: return
        start_str = (qs.get("start") or [""])[0]
        end_str = (qs.get("end") or [""])[0]
        if not start_str or not end_str:
            _json_response(self, 400, {"error": "start and end required"})
            return
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start_dt.tzinfo: start_dt = start_dt.astimezone(timezone.utc).replace(tzinfo=None)
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_dt.tzinfo: end_dt = end_dt.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            _json_response(self, 400, {"error": "Invalid date format"})
            return
        source_id = (qs.get("source_id") or [""])[0]
        conditions = [
            "ce.user_id = %s", "ce.status != 'CANCELLED'",
            "ce.dtstart < %s", "ce.dtend > %s",
            "(cs.visible IS NULL OR cs.visible != FALSE)",
        ]
        params: list = [user["id"], end_dt, start_dt]
        if source_id and source_id.isdigit():
            conditions.append("ce.source_id = %s")
            params.append(int(source_id))
        where = " AND ".join(conditions)
        events = db.query_dicts(
            f"""SELECT ce.uid, ce.summary, ce.description, ce.location,
                       ce.dtstart, ce.dtend, ce.all_day, ce.rrule
                FROM calendar_events ce
                LEFT JOIN calendar_sources cs ON cs.id = ce.source_id
                WHERE {where}
                ORDER BY ce.dtstart""",
            tuple(params),
        )
        body = _ics_dump(events)
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="calendar-{start_str}-{end_str}.ics"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ── OAuth Handlers ───────────────────────────────────────────────────────

    def _handle_oauth_authorize(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        qs = parse_qs(urlparse(self.path).query)
        provider_name = (qs.get("provider") or [""])[0].strip().lower()
        is_crm_mail = (qs.get("is_crm_mail") or ["0"])[0] in ("1", "true", "True")
        if is_crm_mail and not user.get("is_admin"):
            is_crm_mail = False
        prov = oauth_providers.get_provider(provider_name)
        if not prov:
            _json_response(self, 400, {"error": f"Unknown provider: {provider_name}"})
            return
        if not oauth_providers.MICROSOFT_CLIENT_ID and not oauth_providers.GOOGLE_CLIENT_ID:
            _json_response(self, 400, {"error": "No OAuth providers configured"})
            return
        state = oauth_providers.generate_state()
        _oauth_states[state] = (user["id"], provider_name, time.time() + 600, is_crm_mail)
        auth_url = prov.authorize_url(state)
        _json_response(self, 200, {"authUrl": auth_url})

    def _handle_oauth_callback(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        code = (qs.get("code") or [""])[0]
        state_val = (qs.get("state") or [""])[0]
        error = (qs.get("error") or [""])[0]

        if error:
            logger.warning("OAuth error: %s", error)
            self._oauth_redirect_with_status("error", error)
            return

        if not code or not state_val:
            _json_response(self, 400, {"error": "Missing code or state"})
            return

        state_data = _oauth_states.pop(state_val, None)
        if not state_data:
            # Graph flow shares the single registered redirect_uri (Microsoft
            # only calls back to OAUTH_REDIRECT_URI) but keeps a separate
            # state store — delegate instead of erroring.
            if state_val in _oauth_graph_states:
                return self._handle_oauth_graph_callback()
            _json_response(self, 400, {"error": "Invalid or expired state"})
            return
        if len(state_data) >= 4:
            user_id, provider_name, _, is_crm_mail = state_data
        else:
            user_id, provider_name, _ = state_data
            is_crm_mail = False

        prov = oauth_providers.get_provider(provider_name)
        if not prov:
            _json_response(self, 400, {"error": f"Unknown provider: {provider_name}"})
            return

        try:
            token_data = prov.exchange_code(code)
        except Exception as e:
            logger.exception("OAuth code exchange failed for %s", provider_name)
            self._oauth_redirect_with_status("error", f"Token exchange failed: {e}")
            return

        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 3600)
        email = token_data.get("email", "")
        logger.info("OAuth callback: provider=%s email=%s has_access=%s has_refresh=%s",
                     provider_name, email, bool(access_token), bool(refresh_token))

        if not access_token:
            self._oauth_redirect_with_status("error", "No access token returned")
            return

        expires_ts = datetime.now(timezone.utc).timestamp() + expires_in
        imap = prov.imap_settings(email)
        smtp = prov.smtp_settings(email)

        existing = db.query_one(
            "SELECT id FROM mail_accounts WHERE email = %s AND owner_user_id = %s",
            (email, user_id),
        )

        if existing:
            db.execute(
                """UPDATE mail_accounts SET
                   oauth_provider = %s, oauth_access_token = %s, oauth_refresh_token = %s,
                   oauth_token_expires = to_timestamp(%s), imap_host = COALESCE(NULLIF(imap_host, ''), %s),
                   imap_port = COALESCE(NULLIF(imap_port, 0), %s),
                   smtp_host = COALESCE(NULLIF(smtp_host, ''), %s),
                   smtp_port = COALESCE(NULLIF(smtp_port, 0), %s),
                   sync_enabled = TRUE
                   WHERE id = %s""",
                (provider_name, access_token, refresh_token, expires_ts,
                 imap["host"], imap["port"], smtp["host"], smtp["port"],
                 existing["id"]),
            )
        else:
            try:
                db.execute(
                    """INSERT INTO mail_accounts
                       (email, imap_host, imap_port, smtp_host, smtp_port, owner_user_id,
                        oauth_provider, oauth_access_token, oauth_refresh_token,
                        oauth_token_expires, smtp_user, sync_enabled, monitored_folders,
                        password_encrypted, display_name, is_crm_mail)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), %s, TRUE, 'INBOX', '', %s, %s)""",
                    (email, imap["host"], imap["port"], smtp["host"], smtp["port"],
                     user_id, provider_name, access_token, refresh_token, expires_ts, email,
                     email.split("@")[0].replace(".", " ").title(), bool(is_crm_mail)),
                )
                logger.info("OAuth INSERT succeeded for email=%s user_id=%s", email, user_id)
            except Exception as e:
                logger.exception("OAuth INSERT failed for email=%s", email)
                self._oauth_redirect_with_status("error", f"DB insert failed: {e}")
                return

        self._oauth_redirect_with_status("success", email)

    def _oauth_redirect_with_status(self, status: str, message: str) -> None:
        base = CRM_PUBLIC_URL.rstrip("/") or DOCS_PUBLIC_URL.rstrip("/") or "/"
        url = f"{base}?mailOAuth={status}&msg={urllib.parse.quote(message)}"
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _handle_oauth_refresh(self, account_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            row = db.query_one(
                "SELECT oauth_provider, oauth_refresh_token FROM mail_accounts WHERE id = %s AND owner_user_id = %s",
                (account_id, user["id"]),
            )
            if not row or not row.get("oauth_provider") or not row.get("oauth_refresh_token"):
                _json_response(self, 400, {"error": "Account not found or not OAuth-enabled"})
                return
            prov = oauth_providers.get_provider(row["oauth_provider"])
            if not prov:
                _json_response(self, 400, {"error": "Unknown provider"})
                return
            token_data = prov.refresh_token(row["oauth_refresh_token"])
            expires_ts = datetime.now(timezone.utc).timestamp() + token_data.get("expires_in", 3600)
            new_refresh = token_data.get("refresh_token", row["oauth_refresh_token"])
            db.execute(
                "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, oauth_token_expires = to_timestamp(%s) WHERE id = %s",
                (token_data["access_token"], new_refresh, expires_ts, account_id),
            )
            _json_response(self, 200, {"ok": True, "expiresAt": expires_ts})
        except Exception as e:
            logger.exception("Failed to refresh OAuth token for account %d", account_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_oauth_refresh_manual(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
            account_id = payload.get("account_id")
            if not account_id:
                _json_response(self, 400, {"error": "account_id required"})
                return
            self._handle_oauth_refresh(int(account_id))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    # ── Graph sendMail OAuth (Microsoft-only) ────────────────────────
    #
    # Separate OAuth flow that mints a Graph-audience token. Stored in
    # oauth_graph_* columns and used to bypass SMTP AUTH when Microsoft
    # has disabled it on the mailbox (5.7.139).

    def _handle_oauth_graph_authorize(self, account_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        acct = db.query_one(
            "SELECT id, oauth_provider FROM mail_accounts WHERE id = %s AND owner_user_id = %s",
            (account_id, user["id"]),
        )
        if not acct:
            _json_response(self, 404, {"error": "Account not found"})
            return
        if acct.get("oauth_provider") != "microsoft":
            _json_response(self, 400, {"error": "Graph sendMail only applies to Microsoft OAuth accounts"})
            return
        state = oauth_providers.generate_state()
        _oauth_graph_states[state] = (user["id"], account_id, time.time() + 600)
        _json_response(self, 200, {"authUrl": oauth_providers.MicrosoftProvider.graph_authorize_url(state)})

    def _handle_oauth_graph_callback(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        code = (qs.get("code") or [""])[0]
        state_val = (qs.get("state") or [""])[0]
        error = (qs.get("error") or [""])[0]

        if error:
            self._oauth_redirect_with_status("error", f"Graph OAuth error: {error}")
            return
        if not code or not state_val:
            _json_response(self, 400, {"error": "Missing code or state"})
            return

        state_data = _oauth_graph_states.pop(state_val, None)
        if not state_data:
            self._oauth_redirect_with_status("error", "Graph OAuth state expired — reconnect from mail account settings")
            return
        user_id, account_id, _ = state_data

        # Re-check ownership before writing tokens. The state only encoded
        # user_id/account_id; checking ownership here prevents CSRF where a
        # leaked callback URL is replayed to a different authenticated session.
        acct = db.query_one(
            "SELECT id FROM mail_accounts WHERE id = %s AND owner_user_id = %s",
            (account_id, user_id),
        )
        if not acct:
            self._oauth_redirect_with_status("error", "Account no longer accessible")
            return

        try:
            token_data = oauth_providers.MicrosoftProvider.graph_exchange_code(code)
        except Exception as e:
            logger.exception("Graph OAuth code exchange failed")
            self._oauth_redirect_with_status("error", f"Graph token exchange failed: {e}")
            return

        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        if not access_token:
            self._oauth_redirect_with_status("error", "Graph OAuth returned no access token")
            return

        expires_ts = datetime.now(timezone.utc).timestamp() + token_data.get("expires_in", 3600)
        db.execute(
            """UPDATE mail_accounts SET
               oauth_graph_access_token = %s,
               oauth_graph_refresh_token = %s,
               oauth_graph_token_expires = to_timestamp(%s),
               oauth_graph_scope = %s
               WHERE id = %s""",
            (access_token, refresh_token, expires_ts,
             oauth_providers.MicrosoftProvider._GRAPH_SCOPE, account_id),
        )
        self._oauth_redirect_with_status("graph_success", "")

    def _handle_oauth_graph_disconnect(self, account_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        acct = db.query_one(
            "SELECT id FROM mail_accounts WHERE id = %s AND owner_user_id = %s",
            (account_id, user["id"]),
        )
        if not acct:
            _json_response(self, 404, {"error": "Account not found"})
            return
        db.execute(
            """UPDATE mail_accounts SET
               oauth_graph_access_token = NULL,
               oauth_graph_refresh_token = NULL,
               oauth_graph_token_expires = NULL,
               oauth_graph_scope = NULL
               WHERE id = %s""",
            (account_id,),
        )
        _json_response(self, 200, {"ok": True})

    def _refresh_graph_token_if_needed(self, account_id: int) -> tuple[str | None, str | None]:
        """Returns (graph_access_token, error_message). Refreshes and persists
        the Graph access token if expired. None token means no Graph token
        configured (not an error)."""
        row = db.query_one(
            "SELECT oauth_graph_access_token, oauth_graph_refresh_token, oauth_graph_token_expires FROM mail_accounts WHERE id = %s",
            (account_id,),
        )
        if not row or not row.get("oauth_graph_access_token"):
            return None, None
        expires = row.get("oauth_graph_token_expires")
        token = row.get("oauth_graph_access_token", "")
        if expires and isinstance(expires, datetime) and expires.timestamp() > datetime.now(timezone.utc).timestamp():
            return token, None
        if not row.get("oauth_graph_refresh_token"):
            return None, "Graph token expired and no refresh token stored — reconnect from mail account settings"
        try:
            tok = oauth_providers.MicrosoftProvider.graph_refresh_token(row["oauth_graph_refresh_token"])
            new_token = tok.get("access_token", "")
            new_refresh = tok.get("refresh_token", row["oauth_graph_refresh_token"])
            new_expires_ts = datetime.now(timezone.utc).timestamp() + tok.get("expires_in", 3600)
            if not new_token:
                return None, "Graph token refresh returned no access token"
            db.execute(
                "UPDATE mail_accounts SET oauth_graph_access_token = %s, oauth_graph_refresh_token = %s, oauth_graph_token_expires = to_timestamp(%s) WHERE id = %s",
                (new_token, new_refresh, new_expires_ts, account_id),
            )
            return new_token, None
        except Exception as e:
            logger.exception("Graph token refresh failed for account %d", account_id)
            return None, f"Graph token refresh failed: {e}"

    def _try_send_for_account(
        self,
        acct: dict,
        *,
        to_addr: str,
        cc_addr: str | None,
        bcc_addr: str | None,
        subject: str,
        html_body: str,
        text_body: str | None,
        attachments: list[dict] | None = None,
    ) -> tuple[bool, str | None, str]:
        """Server-side wrapper that injects the DB-aware Graph token refresher
        into the shared `mail_send.try_send_for_account` helper. All send
        routing decisions (Graph-first / SMTP fallback / 5.7.139 retry) live
        in the shared helper."""
        from mail_send import try_send_for_account
        return try_send_for_account(
            acct,
            to_addr=to_addr,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            cc_addr=cc_addr,
            bcc_addr=bcc_addr,
            attachments=attachments,
            refresh_graph_token=self._refresh_graph_token_if_needed,
        )

    # ── Mail Scanner Handlers ──────────────────────────────────────

    def _link_email_to_deal(self, message_id: int, opp_id: int, linked_by: int | None = None) -> bool:
        try:
            existing = db.query_one(
                "SELECT id FROM mail_deal_links WHERE message_id = %s AND opportunity_id = %s",
                (message_id, opp_id),
            )
            if existing:
                return False
            db.execute(
                "INSERT INTO mail_deal_links (message_id, opportunity_id, linked_by_user_id) VALUES (%s, %s, %s)",
                (message_id, opp_id, linked_by),
            )
            return True
        except Exception as e:
            logger.error("Failed to link email %d to deal %d: %s", message_id, opp_id, e)
            return False

    def _add_tag_to_message(self, message_id: int, tag_title: str, assigned_by: int | None = None) -> bool:
        try:
            existing = db.query_one("SELECT id FROM mail_tags WHERE title = %s", (tag_title,))
            if existing:
                tag_id = existing["id"]
            else:
                result = db.query_one(
                    "INSERT INTO mail_tags (title, created_by) VALUES (%s, %s) RETURNING id",
                    (tag_title, assigned_by),
                )
                tag_id = result["id"] if result else None
            if tag_id is None:
                return False
            existing_assignment = db.query_one(
                "SELECT id FROM mail_tag_assignments WHERE message_id = %s AND tag_id = %s",
                (message_id, tag_id),
            )
            if existing_assignment:
                return False
            db.execute(
                "INSERT INTO mail_tag_assignments (message_id, tag_id, assigned_by) VALUES (%s, %s, %s)",
                (message_id, tag_id, assigned_by),
            )
            return True
        except Exception as e:
            logger.error("Failed to tag message %d with %s: %s", message_id, tag_title, e)
            return False

    def _handle_mail_inbox(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        if _is_limited(user):
            _json_response(self, 403, {"error": "CRM Mail not available for limited accounts"})
            return
        qs = parse_qs(urlparse(self.path).query)
        page = int(qs.get("page", ["1"])[0])
        page_size = int(qs.get("page_size", ["50"])[0])
        folder = qs.get("folder", ["INBOX"])[0].strip()
        try:
            rows = db.query_dicts(
                "SELECT m.*, a.email AS account_email FROM mail_messages m "
                "LEFT JOIN mail_accounts a ON m.account_id = a.id "
                "WHERE m.folder = %s ORDER BY m.date_received DESC LIMIT %s OFFSET %s",
                (folder, page_size, (page - 1) * page_size),
            )
            _json_response(self, 200, {"messages": rows, "page": page, "page_size": page_size})
        except Exception as e:
            logger.exception("Failed to fetch inbox")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_messages(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        page = int(qs.get("page", ["1"])[0])
        page_size = int(qs.get("page_size", ["50"])[0])
        search = qs.get("search", [""])[0].strip()
        folder = qs.get("folder", ["INBOX"])[0].strip()
        account_id = qs.get("account_id", [None])[0]
        tag = qs.get("tag", [None])[0]
        user = _require_auth(self)
        if not user:
            return
        if _is_limited(user):
            _json_response(self, 403, {"error": "CRM Mail not available for limited accounts"})
            return
        try:
            where = ["m.folder = %s"]
            params: list[Any] = [folder]
            join = ""
            if account_id == 'crm':
                where.append("m.account_id IN (SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE)")
            elif account_id:
                where.append("m.account_id = %s")
                params.append(int(account_id))
            where.append(f"m.account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})")
            params.extend([user["id"], user["id"]])
            if search:
                where.append("(m.subject ILIKE %s OR m.from_addr ILIKE %s)")
                params.extend([f"%{search}%", f"%{search}%"])
            if tag:
                join = "JOIN mail_tag_assignments ta ON m.id = ta.message_id JOIN mail_tags t ON ta.tag_id = t.id"
                where.append("t.title = %s")
                params.append(tag)
            q = " AND ".join(where)
            rows = db.query_dicts(
                "SELECT m.*, a.email AS account_email FROM mail_messages m "
                "LEFT JOIN mail_accounts a ON m.account_id = a.id "
                + join + " WHERE " + q + " ORDER BY m.date_received DESC LIMIT %s OFFSET %s",
                tuple(params) + (page_size, (page - 1) * page_size),
            )
            # Fetch tags and linked deals for each message in single batch queries
            if rows:
                msg_ids = [r["id"] for r in rows]
                tag_rows = db.query_dicts(
                    "SELECT ta.message_id, t.title, t.color FROM mail_tag_assignments ta "
                    "JOIN mail_tags t ON ta.tag_id = t.id WHERE ta.message_id = ANY(%s)",
                    (msg_ids,),
                )
                tags_by_msg = {}
                for tr in tag_rows:
                    mid = tr["message_id"]
                    if mid not in tags_by_msg:
                        tags_by_msg[mid] = []
                    tags_by_msg[mid].append({"title": tr["title"], "color": tr["color"]})
                # Fetch linked deals
                deal_link_rows = db.query_dicts(
                    "SELECT dl.message_id, dl.opportunity_id, o.title FROM mail_deal_links dl "
                    "JOIN opportunities o ON dl.opportunity_id = o.id WHERE dl.message_id = ANY(%s)",
                    (msg_ids,),
                )
                deals_by_msg = {}
                for dl in deal_link_rows:
                    mid = dl["message_id"]
                    if mid not in deals_by_msg:
                        deals_by_msg[mid] = []
                    deals_by_msg[mid].append({"deal_id": dl["opportunity_id"], "title": dl["title"]})
                # Fetch attachment counts
                att_count_rows = db.query_dicts(
                    "SELECT message_id, COUNT(*) AS cnt FROM mail_attachments WHERE message_id = ANY(%s) GROUP BY message_id",
                    (msg_ids,),
                )
                att_counts = {r["message_id"]: r["cnt"] for r in att_count_rows}
                for r in rows:
                    r["tags"] = tags_by_msg.get(r["id"], [])
                    r["linked_deals"] = deals_by_msg.get(r["id"], [])
                    # Attachment count: prefer mail_attachments table, fallback to attachments_json
                    r["attachment_count"] = att_counts.get(r["id"], 0)
                    if not r["attachment_count"] and r.get("attachments_json"):
                        try:
                            aj = r["attachments_json"]
                            if isinstance(aj, list):
                                r["attachment_count"] = len(aj)
                        except Exception:
                            pass
            total = db.query_one(
                "SELECT COUNT(*) AS cnt FROM mail_messages m " + join + " WHERE " + q,
                tuple(params),
            )
            _json_response(self, 200, {"messages": rows, "page": page, "page_size": page_size, "total": total["cnt"] if total else 0})
        except Exception as e:
            logger.exception("Failed to fetch messages")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message(self, message_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            row = db.query_one(
                "SELECT m.*, a.email AS account_email FROM mail_messages m "
                "LEFT JOIN mail_accounts a ON m.account_id = a.id WHERE m.id = %s",
                (message_id,),
            )
            if not row:
                _json_response(self, 404, {"error": "Message not found"})
                return
            if not _mail_account_accessible(user, row["account_id"]):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            row_dict = dict(row)
            tags = db.query_dicts(
                "SELECT t.id, t.title, t.color FROM mail_tag_assignments ta "
                "JOIN mail_tags t ON ta.tag_id = t.id WHERE ta.message_id = %s",
                (message_id,),
            )
            links = db.query_dicts(
                "SELECT o.id AS opportunity_id, o.title AS opportunity_title FROM mail_deal_links dl "
                "JOIN opportunities o ON dl.opportunity_id = o.id WHERE dl.message_id = %s",
                (message_id,),
            )
            # Attachments — prefer mail_attachments table, fallback to attachments_json
            att_rows = db.query_dicts(
                "SELECT * FROM mail_attachments WHERE message_id = %s ORDER BY filename",
                (message_id,),
            )
            if att_rows:
                row_dict["attachments"] = att_rows
            elif row_dict.get("attachments_json"):
                row_dict["attachments"] = row_dict["attachments_json"]
            else:
                row_dict["attachments"] = []
            row_dict["tags"] = tags
            row_dict["linked_deals"] = links
            _json_response(self, 200, row_dict)
        except Exception as e:
            logger.exception("Failed to fetch message %d", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _create_email_history_event(self, opp_id: int, message_id: int, user_id: int | None) -> bool:
        """Create the project-timeline event for a linked email (idempotent).

        Returns True when an event now exists (created or already present).
        Mail-link callers must use this on BOTH fresh and already-linked
        paths: link rows can predate the event (older builds, send flow),
        and without the event nothing appears in project history.
        """
        try:
            existing = db.query_one(
                "SELECT id FROM history_events WHERE opportunity_id = %s AND content LIKE %s",
                (opp_id, f'%"message_id": {int(message_id)}%'),
            )
            if existing:
                return True
            msg = db.query_one(
                "SELECT subject, from_addr, to_addr, cc_addr, body_html, body_text, date_received FROM mail_messages WHERE id = %s",
                (message_id,),
            )
            if not msg:
                return False
            try:
                att_rows = db.query_dicts(
                    "SELECT filename, size_bytes, mime_type FROM mail_attachments WHERE message_id = %s",
                    (message_id,),
                )
                att_snapshot = [
                    {"filename": a["filename"], "size_bytes": a["size_bytes"] or 0, "mime_type": a["mime_type"] or ""}
                    for a in att_rows
                ]
            except Exception:
                att_snapshot = []
            snapshot = {
                "type": "email_snapshot",
                "message_id": message_id,
                "from": msg["from_addr"] or "",
                "to": msg["to_addr"] or "",
                "cc": msg["cc_addr"] or "",
                "subject": msg["subject"] or "",
                "date_sent": str(msg["date_received"]) if msg["date_received"] else "",
                "body_html": msg["body_html"] or "",
                "body_text": msg["body_text"] or "",
                "attachments": att_snapshot,
            }
            db.execute(
                "INSERT INTO history_events (opportunity_id, category_id, content, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (opp_id, EMAIL_HISTORY_CATEGORY_ID, json.dumps(snapshot), user_id),
            )
            return True
        except Exception:
            logger.exception("Failed to create history event for email %d", message_id)
            return False

    def _handle_mail_link(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            opp_id = payload.get("opportunityId") or payload.get("oppId")
            if not opp_id:
                _json_response(self, 400, {"error": "opportunityId required"})
                return
            try:
                opp_id = int(opp_id)
            except (ValueError, TypeError):
                _json_response(self, 400, {"error": "Invalid opportunityId"})
                return
            # Verify the opportunity exists
            opp = db.query_one("SELECT id FROM opportunities WHERE id = %s", (opp_id,))
            if not opp:
                _json_response(self, 404, {"error": f"Opportunity {opp_id} not found"})
                return
            linked_by = user["id"]
            ok = self._link_email_to_deal(message_id, opp_id, linked_by)
            if not ok:
                # Check if already linked
                existing = db.query_one(
                    "SELECT id FROM mail_deal_links WHERE message_id = %s AND opportunity_id = %s",
                    (message_id, opp_id),
                )
                if not existing:
                    _json_response(self, 500, {"error": "Failed to link email to deal"})
                    return
                # Already linked — but the timeline event may still be
                # missing (link rows predate it). Backfill it now instead
                # of reporting success while history stays empty.
                history_created = self._create_email_history_event(opp_id, message_id, linked_by)
                _json_response(self, 200, {"ok": True, "linked": False, "historyCreated": history_created, "message": "Already linked"})
                return
            # Create history event so the link appears in the deal timeline.
            # Store a full JSON snapshot so the embedded mail view survives account
            # deletion / auto-delete retention purging.
            history_created = self._create_email_history_event(opp_id, message_id, linked_by)
            _json_response(self, 200, {"ok": True, "linked": True, "historyCreated": history_created})
        except Exception as e:
            logger.exception("Failed to link message %d", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_mark_read(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            db.execute("UPDATE mail_messages SET is_read = TRUE WHERE id = %s", (message_id,))
            # Sync to IMAP — set \Seen flag so Outlook/webmail reflect the change
            msg = db.query_one("SELECT account_id, folder, imap_uid FROM mail_messages WHERE id = %s", (message_id,))
            if msg and msg.get("imap_uid"):
                threading.Thread(target=_imap_set_seen, args=(msg["account_id"], msg["folder"], str(msg["imap_uid"]), True), daemon=True).start()
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to mark message %d read", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_mark_unread(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            db.execute("UPDATE mail_messages SET is_read = FALSE WHERE id = %s", (message_id,))
            # Sync to IMAP — remove \Seen flag
            msg = db.query_one("SELECT account_id, folder, imap_uid FROM mail_messages WHERE id = %s", (message_id,))
            if msg and msg.get("imap_uid"):
                threading.Thread(target=_imap_set_seen, args=(msg["account_id"], msg["folder"], str(msg["imap_uid"]), False), daemon=True).start()
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to mark message %d unread", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_delete(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            db.execute("UPDATE mail_messages SET folder = 'Trash' WHERE id = %s", (message_id,))
            # Sync to IMAP — move to Trash folder
            msg = db.query_one("SELECT account_id, folder, imap_uid FROM mail_messages WHERE id = %s", (message_id,))
            if msg and msg.get("imap_uid"):
                threading.Thread(target=_imap_move, args=(msg["account_id"], msg["folder"], str(msg["imap_uid"]), "Trash"), daemon=True).start()
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to trash message %d", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_trash_count(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            row = db.query_one(
                f"SELECT COUNT(*) AS cnt FROM mail_messages WHERE folder = 'Trash' "
                f"AND account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})",
                (user["id"], user["id"]),
            )
            _json_response(self, 200, {"count": row["cnt"] if row else 0})
        except Exception as e:
            logger.exception("Failed to get trash count")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_draft_get(self, draft_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            row = db.query_one(
                f"SELECT * FROM mail_outgoing WHERE id = %s AND status = 'draft' "
                f"AND account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})",
                (draft_id, user["id"], user["id"]),
            )
            if not row:
                _json_response(self, 404, {"error": "Draft not found"})
                return
            _json_response(self, 200, dict(row))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    # ── Persistent minimized modal state ──

    def _handle_minimized_state_get(self) -> None:
        try:
            user = _require_auth(self)
            row = db.query_one("SELECT state_json FROM minimized_modal_state WHERE user_id = %s", (user["id"],))
            if row:
                _json_response(self, 200, row["state_json"] if isinstance(row["state_json"], dict) else json.loads(row["state_json"] or "{}"))
            else:
                _json_response(self, 200, {})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_minimized_state_put(self) -> None:
        try:
            user = _require_auth(self)
            payload = json.loads(_read_body(self) or b"{}")
            db.execute(
                "INSERT INTO minimized_modal_state (user_id, state_json, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (user_id) DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = NOW()",
                (user["id"], json.dumps(payload)),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_drafts_get(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        account_id = qs.get("account_id", [None])[0]
        user = _require_auth(self)
        if not user:
            return
        try:
            where = "status = 'draft'"
            params: tuple = ()
            if account_id == 'crm':
                where += " AND account_id IN (SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE)"
            elif account_id:
                try:
                    where += " AND account_id = %s"
                    params = (int(account_id),)
                except (ValueError, TypeError):
                    pass
            where += f" AND account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})"
            params += (user["id"], user["id"])
            rows = db.query_dicts(
                f"SELECT * FROM mail_outgoing WHERE {where} ORDER BY created_at DESC LIMIT 50",
                params,
            )
            _json_response(self, 200, {"drafts": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_empty_trash(self) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            db.execute(
                f"DELETE FROM mail_messages WHERE folder = 'Trash' "
                f"AND account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})",
                (user["id"], user["id"]),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to empty trash")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_add_tag(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            tag_title = payload.get("title") or payload.get("tag")
            if not tag_title:
                _json_response(self, 400, {"error": "title required"})
                return
            assigned_by = user["id"]
            ok = self._add_tag_to_message(message_id, tag_title, assigned_by)
            _json_response(self, 200, {"ok": ok})
        except Exception as e:
            logger.exception("Failed to add tag to message %d", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_create(self) -> None:
        # Audit #5: previously had no auth, no access check, no size cap, no
        # folder allowlist. The scanner doesn't actually call this endpoint
        # (it inserts via db.execute), so this is a hardening pass for any
        # future caller (or hand-rolled request). All real client calls go
        # through the GET inbox / mark-read / etc endpoints.
        try:
            user = _require_auth(self)
            if not user:
                return
            payload = json.loads(_read_body(self) or b"{}")
            account_id_raw = payload.get("account_id")
            if account_id_raw is None:
                _json_response(self, 400, {"error": "account_id required"})
                return
            try:
                account_id = int(account_id_raw)
            except (ValueError, TypeError):
                _json_response(self, 400, {"error": "account_id must be an integer"})
                return
            if not _mail_account_accessible(user, account_id):
                _json_response(self, 403, {"error": "You don't have access to this account"})
                return

            # Cap payload sizes so a single request can't insert a 1 GB body
            # and OOM the row. 5 MB per text/html field, 10 MB combined.
            body_text = payload.get("body_text") or ""
            body_html = payload.get("body_html") or ""
            MAX_BODY_FIELD = 5 * 1024 * 1024
            if len(body_text) > MAX_BODY_FIELD:
                _json_response(self, 413, {"error": f"body_text exceeds {MAX_BODY_FIELD} bytes"})
                return
            if len(body_html) > MAX_BODY_FIELD:
                _json_response(self, 413, {"error": f"body_html exceeds {MAX_BODY_FIELD} bytes"})
                return
            if len(body_text) + len(body_html) > 10 * 1024 * 1024:
                _json_response(self, 413, {"error": "combined body exceeds 10 MB"})
                return

            # Folder allowlist — reject anything not in this set. Custom user
            # folders are also accepted if the user owns the account (handled
            # below via the mail_folders table lookup).
            folder = (payload.get("folder") or "INBOX").strip()
            builtin_folders = {"INBOX", "Sent", "Drafts", "Spam", "Trash", "Archive", "Junk", "Outbox"}
            if folder not in builtin_folders:
                # Allow custom folders for this account if the row exists.
                custom = db.query_one(
                    "SELECT id FROM mail_folders WHERE imap_account_id = %s AND (name = %s OR imap_path = %s) LIMIT 1",
                    (account_id, folder, folder),
                )
                if not custom:
                    _json_response(self, 400, {"error": f"Invalid folder {folder!r}"})
                    return

            # Coerce boolean fields defensively.
            is_read = bool(payload.get("is_read", False))
            is_flagged = bool(payload.get("is_flagged", False))

            imap_uid = payload.get("imap_uid") or str(int(time.time() * 1000))
            result = db.query_one(
                """INSERT INTO mail_messages
                   (account_id, imap_uid, message_id, from_addr, to_addr, cc_addr,
                    subject, body_text, body_html, date_received, folder, is_read, is_flagged)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    account_id,
                    imap_uid,
                    payload.get("message_id"),
                    payload.get("from_addr"),
                    payload.get("to_addr"),
                    payload.get("cc_addr"),
                    payload.get("subject"),
                    body_text,
                    body_html,
                    payload.get("date_received"),
                    folder,
                    is_read,
                    is_flagged,
                ),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            logger.exception("Failed to create message")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_tags(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        page = int(qs.get("page", ["1"])[0])
        page_size = int(qs.get("page_size", ["50"])[0])
        try:
            rows = db.query_dicts("SELECT * FROM mail_tags ORDER BY title LIMIT %s OFFSET %s", (page_size, (page - 1) * page_size))
            _json_response(self, 200, {"tags": rows, "page": page, "page_size": page_size})
        except Exception as e:
            logger.exception("Failed to fetch tags")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_tag_get(self, tag_id: int) -> None:
        try:
            row = db.query_one("SELECT * FROM mail_tags WHERE id = %s", (tag_id,))
            if not row:
                _json_response(self, 404, {"error": "Tag not found"})
                return
            _json_response(self, 200, dict(row))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_tag_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            title = (payload.get("title") or "").strip()
            color = (payload.get("color") or "#6c757d").strip()
            if not title:
                _json_response(self, 400, {"error": "title required"})
                return
            user = _require_auth(self)
            created_by = user["id"] if user else None
            result = db.query_one(
                "INSERT INTO mail_tags (title, color, created_by) VALUES (%s, %s, %s) RETURNING id",
                (title, color, created_by),
            )
            _json_response(self, 201, {"id": result["id"], "title": title, "color": color})
        except Exception as e:
            if "unique" in str(e).lower():
                _json_response(self, 409, {"error": "Tag already exists"})
            else:
                logger.exception("Failed to create tag")
                _json_response(self, 500, {"error": str(e)})

    def _handle_mail_tag_update(self, tag_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            title = (payload.get("title") or "").strip()
            color = payload.get("color")
            existing = db.query_one("SELECT id FROM mail_tags WHERE id = %s", (tag_id,))
            if not existing:
                _json_response(self, 404, {"error": "Tag not found"})
                return
            if title:
                db.execute("UPDATE mail_tags SET title = %s, color = COALESCE(%s, color) WHERE id = %s", (title, color, tag_id))
            elif color:
                db.execute("UPDATE mail_tags SET color = %s WHERE id = %s", (color, tag_id))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            if "unique" in str(e).lower():
                _json_response(self, 409, {"error": "Tag name already exists"})
            else:
                _json_response(self, 500, {"error": str(e)})

    def _handle_mail_tag_delete(self, tag_id: int) -> None:
        try:
            existing = db.query_one("SELECT id FROM mail_tags WHERE id = %s", (tag_id,))
            if not existing:
                _json_response(self, 404, {"error": "Tag not found"})
                return
            db.execute("DELETE FROM mail_tag_assignments WHERE tag_id = %s", (tag_id,))
            db.execute("DELETE FROM mail_tags WHERE id = %s", (tag_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_remove_tag(self, message_id: int, tag_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            db.execute("DELETE FROM mail_tag_assignments WHERE message_id = %s AND tag_id = %s", (message_id, tag_id))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_templates(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        page = int(qs.get("page", ["1"])[0])
        page_size = int(qs.get("page_size", ["50"])[0])
        try:
            rows = db.query_dicts("SELECT * FROM mail_templates ORDER BY title LIMIT %s OFFSET %s", (page_size, (page - 1) * page_size))
            _json_response(self, 200, {"templates": rows, "page": page, "page_size": page_size})
        except Exception as e:
            logger.exception("Failed to fetch templates")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_template_get(self, template_id: int) -> None:
        try:
            row = db.query_one("SELECT * FROM mail_templates WHERE id = %s", (template_id,))
            if not row:
                _json_response(self, 404, {"error": "Template not found"})
                return
            _json_response(self, 200, dict(row))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_template_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            title = (payload.get("title") or "").strip()
            if not title:
                _json_response(self, 400, {"error": "title required"})
                return
            user = _require_auth(self)
            created_by = user["id"] if user else None
            result = db.query_one(
                "INSERT INTO mail_templates (title, subject, body_html, created_by) VALUES (%s, %s, %s, %s) RETURNING id",
                (title, payload.get("subject", ""), payload.get("body_html", ""), created_by),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            logger.exception("Failed to create template")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_template_update(self, template_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            existing = db.query_one("SELECT id FROM mail_templates WHERE id = %s", (template_id,))
            if not existing:
                _json_response(self, 404, {"error": "Template not found"})
                return
            title = (payload.get("title") or "").strip()
            subject = payload.get("subject")
            body_html = payload.get("body_html")
            sets = []
            params: list[Any] = []
            if title:
                sets.append("title = %s")
                params.append(title)
            if subject is not None:
                sets.append("subject = %s")
                params.append(subject)
            if body_html is not None:
                sets.append("body_html = %s")
                params.append(body_html)
            if sets:
                sets.append("updated_at = NOW()")
                params.append(template_id)
                db.execute("UPDATE mail_templates SET " + ", ".join(sets) + " WHERE id = %s", tuple(params))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_template_delete(self, template_id: int) -> None:
        try:
            existing = db.query_one("SELECT id FROM mail_templates WHERE id = %s", (template_id,))
            if not existing:
                _json_response(self, 404, {"error": "Template not found"})
                return
            db.execute("DELETE FROM mail_templates WHERE id = %s", (template_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_accounts(self) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if _is_limited(user):
                _json_response(self, 403, {"error": "CRM Mail not available for limited accounts"})
                return
            rows = db.query_dicts(
                f"""SELECT id, email, display_name, imap_host, imap_port,
                          smtp_host, smtp_port, smtp_from_name, smtp_user,
                          sync_enabled, monitored_folders, oauth_provider,
                          oauth_token_expires, oauth_scopes, is_crm_mail,
                          auto_bcc_addr, retention_days, tab_icon, tab_color,
                          last_sync, owner_user_id, created_at
                   FROM mail_accounts
                   WHERE {_mail_visible_accounts_sql(user["id"])}
                   ORDER BY email""",
                (user["id"], user["id"]),
            )
            now_ts = datetime.now(timezone.utc).timestamp()
            for r in rows:
                r["password_encrypted"] = ""
                r["smtp_password_encrypted"] = ""
                r["oauth_access_token"] = ""
                r["oauth_refresh_token"] = ""
                r["authType"] = r.get("oauth_provider") or "password"
                r["isOwner"] = r["owner_user_id"] == user["id"] or bool(user.get("is_admin"))
                r["canManage"] = _mail_account_manageable(user, r["id"])
                if r.get("oauth_provider"):
                    expires = r.get("oauth_token_expires")
                    if expires and isinstance(expires, datetime):
                        r["oauthStatus"] = "connected" if expires.timestamp() > now_ts else "expired"
                    else:
                        r["oauthStatus"] = "connected"
                else:
                    r["oauthStatus"] = None
                # Graph sendMail status (Microsoft-only). Used by the
                # connect/disconnect UI and the compose-window banner.
                if r.get("oauth_provider") == "microsoft":
                    g_expires = r.get("oauth_graph_token_expires")
                    if r.get("oauth_graph_access_token"):
                        if g_expires and isinstance(g_expires, datetime):
                            r["graphStatus"] = "connected" if g_expires.timestamp() > now_ts else "expired"
                        else:
                            r["graphStatus"] = "connected"
                    else:
                        r["graphStatus"] = None
                else:
                    r["graphStatus"] = None
            # Attach shared-with users for each visible account
            acct_ids = [r["id"] for r in rows]
            shares: dict[int, list[dict]] = {}
            if acct_ids:
                share_rows = db.query_dicts(
                    "SELECT aa.account_id, u.id AS user_id, u.display_name, u.email "
                    "FROM mail_account_access aa JOIN users u ON u.id = aa.user_id "
                    "WHERE aa.account_id = ANY(%s) ORDER BY u.display_name",
                    (acct_ids,),
                )
                for sr in share_rows:
                    shares.setdefault(sr["account_id"], []).append({
                        "userId": sr["user_id"],
                        "displayName": sr["display_name"],
                        "email": sr["email"],
                    })
            for r in rows:
                r["sharedWith"] = shares.get(r["id"], [])
            _json_response(self, 200, {"accounts": rows})
        except Exception as e:
            logger.exception("Failed to fetch mail accounts")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_status(self) -> None:
        _json_response(self, 200, mail_scanner.get_scanner_status() if mail_scanner else {"enabled": False})

    def _handle_mail_log(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        limit = int(qs.get("limit", ["500"])[0])
        account_id = qs.get("account_id", [None])[0]
        entries = mail_scanner.get_scanner_log(limit=limit, account_id=account_id) if mail_scanner else []
        _json_response(self, 200, {"entries": entries})

    def _handle_mail_log_correct(self) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            payload = json.loads(_read_body(self) or b"{}")
            uids = payload.get("uids", [])
            correct_project_id = payload.get("correct_project_id")
            if not uids or not correct_project_id:
                _json_response(self, 400, {"error": "uids and correct_project_id required"})
                return
            # Verify the project exists
            opp = db.query_one("SELECT id FROM opportunities WHERE id = %s", (correct_project_id,))
            if not opp:
                _json_response(self, 404, {"error": f"Project {correct_project_id} not found"})
                return
            # Find messages by IMAP UID and save corrections to training data
            count = 0
            for uid in uids:
                msg = db.query_one("SELECT id, subject, from_addr FROM mail_messages WHERE imap_uid = %s", (uid,))
                if msg:
                    try:
                        db.execute(
                            "INSERT INTO classifier_training_data "
                            "(message_id, correct_classification, correct_project_id, correct_action_type, reviewer_id, reviewed_at) "
                            "VALUES (%s, %s, %s, %s, %s, NOW())",
                            (msg["id"], "link_deal", correct_project_id, "link", user["id"]),
                        )
                        count += 1
                    except Exception:
                        pass
            _json_response(self, 200, {"ok": True, "corrected": count})
            # Auto-retrain after 10+ corrections accumulate
            if count > 0 and mail_scanner:
                try:
                    corr_count = db.query_one("SELECT COUNT(*) AS cnt FROM classifier_training_data")
                    if corr_count and corr_count["cnt"] >= 10:
                        import threading
                        threading.Thread(target=mail_scanner.retrain_classifier_head, kwargs={"mock_samples": 0}, daemon=True).start()
                except Exception:
                    pass
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_trigger_test(self) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            payload = json.loads(_read_body(self) or b"{}")
            triggers = payload.get("triggers", [])
            account_id = payload.get("account_id")

            # Get recent messages for testing
            if account_id:
                messages = db.query_dicts(
                    "SELECT * FROM mail_messages WHERE account_id = %s ORDER BY date_received DESC LIMIT 100",
                    (int(account_id),),
                )
            else:
                messages = db.query_dicts(
                    "SELECT * FROM mail_messages ORDER BY date_received DESC LIMIT 100"
                )

            matches = []
            for msg in (messages or []):
                # Build a minimal classification dict for trigger evaluation
                classification = {"linked_deal_id": None}
                if mail_scanner and mail_scanner._evaluate_triggers(triggers, msg, classification):
                    matches.append({
                        "id": msg["id"],
                        "subject": msg.get("subject", ""),
                        "from": msg.get("from_addr", ""),
                        "date": str(msg.get("date_received", "")),
                    })

            _json_response(self, 200, {"matches": matches, "total": len(messages or [])})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_reprocess(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            entries = payload.get("entries") or []
            if entries:
                results = mail_scanner.reprocess_conversations(entries)
            else:
                # Legacy fallback for integer IDs (unscoped, may not clear real keys)
                ids = payload.get("conversation_ids") or payload.get("ids") or []
                results = mail_scanner.reprocess_conversations([int(x) for x in ids])
            _json_response(self, 200, {"results": results})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_retrain(self) -> None:
        try:
            result = mail_scanner.retrain_classifier_head() if mail_scanner else {"ok": False, "message": "Scanner not available"}
            _json_response(self, 200, result)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_config(self) -> None:
        _json_response(self, 200, mail_scanner.get_contractors() if mail_scanner else {})

    def _handle_mail_config_put(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            result = mail_scanner.update_contractors(payload) if mail_scanner else payload
            _json_response(self, 200, result)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_unread_count(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        account_id = qs.get("account_id", [None])[0]
        user = _require_auth(self)
        if not user:
            return
        try:
            where = f"m.is_read = FALSE AND m.account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})"
            params: tuple = (user["id"], user["id"])
            if account_id == 'crm':
                where += " AND m.account_id IN (SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE)"
            elif account_id:
                where += " AND m.account_id = %s"
                params = params + (int(account_id),)
            row = db.query_one(f"SELECT COUNT(*) AS cnt FROM mail_messages m WHERE {where}", params)
            _json_response(self, 200, {"count": row["cnt"] if row else 0})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_outgoing(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        page = int(qs.get("page", ["1"])[0])
        page_size = int(qs.get("page_size", ["50"])[0])
        user = _require_auth(self)
        if not user:
            return
        try:
            rows = db.query_dicts(
                f"SELECT o.*, a.email AS account_email FROM mail_outgoing o "
                f"LEFT JOIN mail_accounts a ON o.account_id = a.id "
                f"WHERE o.account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])}) "
                f"ORDER BY o.created_at DESC LIMIT %s OFFSET %s",
                (user["id"], user["id"], page_size, (page - 1) * page_size),
            )
            _json_response(self, 200, {"messages": rows, "page": page, "page_size": page_size})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            user = _require_auth(self)
            folders_raw = payload.get("monitored_folders")
            if isinstance(folders_raw, list):
                folders_str = ",".join(folders_raw)
            else:
                folders_str = str(folders_raw or "INBOX")
            oauth_provider = payload.get("oauth_provider") or None
            is_crm_mail = bool(payload.get("is_crm_mail")) and bool(user.get("is_admin")) if user else False
            result = db.query_one(
                """INSERT INTO mail_accounts
                   (email, display_name, imap_host, imap_port, password_encrypted, owner_user_id,
                    smtp_host, smtp_port, smtp_user, smtp_password_encrypted,
                    smtp_use_tls, smtp_from_name, oauth_provider, monitored_folders,
                    oauth_access_token, oauth_refresh_token, oauth_token_expires, is_crm_mail,
                    auto_bcc_addr, retention_days, tab_icon, tab_color)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    payload.get("email"),
                    payload.get("display_name"),
                    payload.get("imap_host"),
                    int(payload.get("imap_port", 993)),
                    payload.get("password", ""),
                    user["id"] if user else None,
                    payload.get("smtp_host"),
                    int(payload.get("smtp_port", 587)) if payload.get("smtp_host") else None,
                    payload.get("smtp_user"),
                    payload.get("smtp_password"),
                    payload.get("smtp_use_tls", True),
                    payload.get("smtp_from_name"),
                    oauth_provider,
                    folders_str,
                    payload.get("oauth_access_token"),
                    payload.get("oauth_refresh_token"),
                    payload.get("oauth_token_expires"),
                    is_crm_mail,
                    payload.get("auto_bcc_addr"),
                    int(payload.get("retention_days") or 90),
                    payload.get("tab_icon") or "user",
                    payload.get("tab_color") or "var(--accent)",
                ),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            logger.exception("Failed to create mail account")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_test(self) -> None:
        """Test IMAP connection with provided credentials."""
        try:
            user = _require_auth(self)
            if not user:
                return
            payload = json.loads(_read_body(self) or b"{}")
            host = (payload.get("host") or "").strip()
            port = int(payload.get("port") or 993)
            email_addr = (payload.get("email") or "").strip()
            password = payload.get("password") or ""
            if not host or not email_addr:
                _json_response(self, 400, {"ok": False, "error": "Host and email are required"})
                return
            if not password:
                _json_response(self, 400, {"ok": False, "error": "Password is required"})
                return
            try:
                from imap_tools import MailBox
                mb = MailBox(host, port=port)
                mb.login(email_addr, password)
                mb.logout()
                _json_response(self, 200, {"ok": True, "message": "Connected successfully"})
            except Exception as imap_e:
                _json_response(self, 200, {"ok": False, "error": f"IMAP login failed: {imap_e}"})
        except Exception as e:
            logger.exception("Failed to test mail account")
            _json_response(self, 500, {"ok": False, "error": str(e)})

    def _handle_mail_account_update(self, account_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_manageable(user, account_id):
                _json_response(self, 403, {"error": "You don't have permission to manage this account"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            fields = []
            params: list[Any] = []
            for key in ("email", "display_name", "imap_host", "smtp_host", "smtp_user", "smtp_from_name", "oauth_provider",
                        "oauth_access_token", "oauth_refresh_token", "auto_bcc_addr", "tab_icon", "tab_color"):
                if key in payload:
                    fields.append(f"{key} = %s")
                    params.append(payload[key])
            if "retention_days" in payload:
                fields.append("retention_days = %s")
                params.append(int(payload["retention_days"] or 90))
            if "signature_html" in payload:
                fields.append("signature_html = %s")
                params.append(payload["signature_html"])
            if "oauth_token_expires" in payload:
                val = payload["oauth_token_expires"]
                if isinstance(val, (int, float)):
                    fields.append("oauth_token_expires = to_timestamp(%s)")
                    params.append(val)
                else:
                    fields.append("oauth_token_expires = %s")
                    params.append(val)
            for key in ("imap_port", "smtp_port"):
                if key in payload:
                    fields.append(f"{key} = %s")
                    params.append(int(payload[key]))
            for key in ("password", "password_encrypted"):
                if key in payload:
                    fields.append("password_encrypted = %s")
                    params.append(payload[key])
            if "smtp_password" in payload:
                fields.append("smtp_password_encrypted = %s")
                params.append(payload["smtp_password"])
            if "smtp_use_tls" in payload:
                fields.append("smtp_use_tls = %s")
                params.append(bool(payload["smtp_use_tls"]))
            if "sync_enabled" in payload:
                fields.append("sync_enabled = %s")
                params.append(bool(payload["sync_enabled"]))
            if "is_crm_mail" in payload and user.get("is_admin"):
                fields.append("is_crm_mail = %s")
                params.append(bool(payload["is_crm_mail"]))
            if "monitored_folders" in payload:
                raw = payload["monitored_folders"]
                if isinstance(raw, list):
                    raw = ",".join(raw)
                fields.append("monitored_folders = %s")
                params.append(str(raw))
            if not fields:
                _json_response(self, 400, {"error": "No fields to update"})
                return
            params.append(account_id)
            db.execute(f"UPDATE mail_accounts SET {', '.join(fields)} WHERE id = %s", tuple(params))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to update mail account %d", account_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_delete(self, account_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_manageable(user, account_id):
                _json_response(self, 403, {"error": "You don't have permission to manage this account"})
                return
            db.execute("DELETE FROM mail_accounts WHERE id = %s", (account_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_share(self, account_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_manageable(user, account_id):
                _json_response(self, 403, {"error": "You don't have permission to share this account"})
                return
            acct = db.query_one("SELECT is_crm_mail FROM mail_accounts WHERE id = %s", (account_id,))
            if not acct:
                _json_response(self, 404, {"error": "Account not found"})
                return
            if acct.get("is_crm_mail"):
                _json_response(self, 400, {"error": "Company inbox is already shared with all users"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            try:
                share_user_id = int(payload.get("user_id"))
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "user_id is required"})
                return
            target = db.query_one("SELECT id, is_admin, is_active FROM users WHERE id = %s", (share_user_id,))
            if not target or not target.get("is_active"):
                _json_response(self, 404, {"error": "User not found"})
                return
            owner = db.query_one("SELECT owner_user_id FROM mail_accounts WHERE id = %s", (account_id,))
            if owner and owner["owner_user_id"] == share_user_id:
                _json_response(self, 400, {"error": "This user already owns the account"})
                return
            granted_by = user["id"]
            db.execute(
                "INSERT INTO mail_account_access (account_id, user_id, granted_by) VALUES (%s, %s, %s) ON CONFLICT (account_id, user_id) DO NOTHING",
                (account_id, share_user_id, granted_by),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_unshare(self, account_id: int, user_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_manageable(user, account_id):
                _json_response(self, 403, {"error": "You don't have permission to manage this account"})
                return
            db.execute(
                "DELETE FROM mail_account_access WHERE account_id = %s AND user_id = %s",
                (account_id, user_id),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_shares(self, account_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_accessible(user, account_id):
                _json_response(self, 403, {"error": "You don't have access to this account"})
                return
            rows = db.query_dicts(
                "SELECT aa.account_id, u.id AS user_id, u.display_name, u.email, aa.granted_at "
                "FROM mail_account_access aa JOIN users u ON u.id = aa.user_id "
                "WHERE aa.account_id = %s ORDER BY u.display_name",
                (account_id,),
            )
            _json_response(self, 200, {"shares": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_send(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            user = _require_auth(self)
            account_id = int(payload.get("account_id"))
            to_addr = payload.get("to", "")
            cc_addr = payload.get("cc")
            bcc_addr = payload.get("bcc")
            subject = payload.get("subject", "")
            body_html = payload.get("body", "")
            body_text = payload.get("body_text")
            deal_id = int(payload["deal_id"]) if payload.get("deal_id") else None

            decoded_attachments = []
            if payload.get("attachments"):
                import base64
                for att in payload["attachments"]:
                    decoded_attachments.append({
                        "filename": att.get("filename", "file"),
                        "content": base64.b64decode(att.get("content", "")),
                        "mime_type": att.get("mime_type", "application/octet-stream"),
                    })

            acct = db.query_one("SELECT * FROM mail_accounts WHERE id = %s", (account_id,))
            if not acct:
                _json_response(self, 404, {"error": "Account not found"})
                return
            if not _mail_account_accessible(user, account_id):
                _json_response(self, 403, {"error": "You don't have access to this account"})
                return

            # Merge account-level auto-BCC addresses (comma-separated) into the outgoing BCC
            auto_bcc = (acct.get("auto_bcc_addr") or "").strip()
            if auto_bcc:
                merged = []
                seen: set[str] = set()
                for raw in (bcc_addr or "").split(",") + auto_bcc.split(","):
                    addr = raw.strip()
                    if addr and addr.lower() not in seen:
                        seen.add(addr.lower())
                        merged.append(addr)
                if merged:
                    bcc_addr = ", ".join(merged)

            # All send routing — Graph-first / SMTP fallback / 5.7.139 retry — lives in
            # _try_send_for_account. _handle_mail_send only needs to call it
            # and persist the outgoing row + sent-message copy + deal link.
            ok, err, send_method = self._try_send_for_account(
                acct,
                to_addr=to_addr,
                cc_addr=cc_addr,
                bcc_addr=bcc_addr,
                subject=subject,
                html_body=body_html,
                text_body=body_text,
                attachments=decoded_attachments if decoded_attachments else None,
            )
            _ = send_method  # reserved for future analytics (per-account send method)

            attachments_json = json.dumps([{"filename": a.get("filename"), "mime_type": a.get("mime_type"), "size": len(a.get("content", ""))} for a in (payload.get("attachments") or [])]) if payload.get("attachments") else None

            outgoing = db.query_one(
                """INSERT INTO mail_outgoing
                   (account_id, from_addr, to_addr, cc_addr, bcc_addr, subject,
                    body_text, body_html, deal_id, attachments_json, status, sent_at, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    account_id, acct["email"], to_addr, cc_addr, bcc_addr,
                    subject, body_text, body_html, deal_id, attachments_json,
                    "sent" if ok else "failed",
                    datetime.now(timezone.utc) if ok else None,
                    user["id"] if user else None,
                ),
            )

            message_id = None
            if ok and outgoing:
                # Store a sent-message copy so it appears in Sent and can be linked to deals.
                # mail_deal_links.message_id is a FK to mail_messages.id, not mail_outgoing.id.
                message_id_header = make_msgid(domain="sietch.local")
                msg_row = db.query_one(
                    """INSERT INTO mail_messages
                       (account_id, imap_uid, message_id, from_addr, to_addr, cc_addr,
                        subject, body_text, body_html, date_received, folder, is_read,
                        is_flagged, attachments_json)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Sent', TRUE, FALSE, %s)
                       RETURNING id""",
                    (
                        account_id,
                        f"sent:outgoing:{outgoing['id']}",
                        message_id_header,
                        acct["email"],
                        to_addr,
                        cc_addr,
                        subject,
                        body_text,
                        body_html,
                        datetime.now(timezone.utc),
                        attachments_json,
                    ),
                )
                message_id = msg_row["id"] if msg_row else None

            if ok and deal_id and message_id:
                db.execute(
                    "INSERT INTO mail_deal_links (message_id, opportunity_id, linked_by_user_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (message_id, deal_id, user["id"] if user else None),
                )
                # Sent mail links into project history like manual links.
                self._create_email_history_event(int(deal_id), int(message_id), user["id"] if user else None)

            if ok:
                _json_response(self, 200, {"ok": True, "id": outgoing["id"] if outgoing else None})
            else:
                _json_response(self, 500, {"ok": False, "error": err or "Send failed"})
        except Exception as e:
            logger.exception("Failed to send mail")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_send_undo(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            outgoing_id = int(payload.get("id"))
            db.execute("DELETE FROM mail_outgoing WHERE id = %s AND status = 'queued'", (outgoing_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_star(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            row = db.query_one("SELECT starred FROM mail_messages WHERE id = %s", (message_id,))
            _json_response(self, 200, {"starred": row["starred"] if row else False})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_toggle_star(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            db.execute("UPDATE mail_messages SET starred = NOT starred WHERE id = %s", (message_id,))
            row = db.query_one("SELECT starred, account_id, folder, imap_uid FROM mail_messages WHERE id = %s", (message_id,))
            # Sync to IMAP — set/unset \Flagged
            if row and row.get("imap_uid"):
                threading.Thread(target=_imap_set_flagged, args=(row["account_id"], row["folder"], str(row["imap_uid"]), bool(row["starred"])), daemon=True).start()
            _json_response(self, 200, {"starred": row["starred"] if row else False})
        except Exception as e:
            logger.exception("Failed to toggle star for message %d", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_archive(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            db.execute("UPDATE mail_messages SET is_archived = TRUE WHERE id = %s", (message_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_move(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            folder = payload.get("folder", "INBOX")
            if folder.lower() in ("inbox",): folder = "INBOX"
            # Get old folder for IMAP move
            old = db.query_one("SELECT account_id, folder, imap_uid FROM mail_messages WHERE id = %s", (message_id,))
            db.execute("UPDATE mail_messages SET folder = %s WHERE id = %s", (folder, message_id))
            # Sync to IMAP — move to destination folder
            if old and old.get("imap_uid") and old.get("folder") != folder:
                threading.Thread(target=_imap_move, args=(old["account_id"], old["folder"], str(old["imap_uid"]), folder), daemon=True).start()
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            logger.exception("Failed to move message %d", message_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_reply(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            row = db.query_one("SELECT * FROM mail_messages WHERE id = %s", (message_id,))
            if not row:
                _json_response(self, 404, {"error": "Message not found"})
                return
            _json_response(self, 200, {
                "to": row["from_addr"] or "",
                "cc": "",
                "subject": "Re: " + (row["subject"] or ""),
                "body": f'<blockquote>{row["body_html"] or row["body_text"] or ""}</blockquote>',
                "original_from": row["from_addr"],
                "original_subject": row["subject"],
                "original_date": str(row["date_received"]) if row["date_received"] else "",
            })
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_forward(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            row = db.query_one("SELECT * FROM mail_messages WHERE id = %s", (message_id,))
            if not row:
                _json_response(self, 404, {"error": "Message not found"})
                return
            _json_response(self, 200, {
                "to": "",
                "cc": "",
                "subject": "Fwd: " + (row["subject"] or ""),
                "body": f'<p>---------- Forwarded message ----------</p><p>From: {row["from_addr"] or ""}<br>Date: {row["date_received"] or ""}<br>Subject: {row["subject"] or ""}</p><blockquote>{row["body_html"] or row["body_text"] or ""}</blockquote>',
                "original_from": row["from_addr"],
                "original_subject": row["subject"],
                "original_date": str(row["date_received"]) if row["date_received"] else "",
            })
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_draft_save(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            user = _require_auth(self)
            if not user:
                return
            account_id = payload.get("account_id")
            if account_id and not _mail_account_accessible(user, int(account_id)):
                _json_response(self, 403, {"error": "You don't have access to this account"})
                return
            result = db.query_one(
                """INSERT INTO mail_outgoing
                   (account_id, to_addr, cc_addr, bcc_addr, subject, body_html, deal_id, status, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', %s)
                   RETURNING id""",
                (account_id, payload.get("to"), payload.get("cc"),
                 payload.get("bcc"), payload.get("subject"), payload.get("body"),
                 payload.get("deal_id"), user["id"] if user else None),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_draft_update(self, draft_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_draft_accessible(user, draft_id):
                _json_response(self, 403, {"error": "You don't have access to this draft"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            db.execute(
                "UPDATE mail_outgoing SET to_addr=%s, cc_addr=%s, bcc_addr=%s, subject=%s, body_html=%s WHERE id=%s",
                (payload.get("to"), payload.get("cc"), payload.get("bcc"),
                 payload.get("subject"), payload.get("body"), draft_id),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_draft_delete(self, draft_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_draft_accessible(user, draft_id):
                _json_response(self, 403, {"error": "You don't have access to this draft"})
                return
            db.execute("DELETE FROM mail_outgoing WHERE id = %s AND status = 'draft'", (draft_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contacts(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        user = _require_auth(self)
        if not user:
            _json_response(self, 401, {"error": "Unauthorized"})
            return
        try:
            rows = db.query_dicts(
                "SELECT * FROM mail_contacts WHERE user_id = %s ORDER BY name, email",
                (user["id"],),
            )
            _json_response(self, 200, {"contacts": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contacts_search(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        q = qs.get("q", [""])[0].strip()
        user = _require_auth(self)
        if not user:
            _json_response(self, 401, {"error": "Unauthorized"})
            return
        try:
            if q:
                rows = db.query_dicts(
                    "SELECT * FROM mail_contacts WHERE user_id = %s AND (name ILIKE %s OR email ILIKE %s) ORDER BY name LIMIT 10",
                    (user["id"], f"%{q}%", f"%{q}%"),
                )
            else:
                rows = db.query_dicts(
                    "SELECT * FROM mail_contacts WHERE user_id = %s ORDER BY name LIMIT 10",
                    (user["id"],),
                )
            _json_response(self, 200, {"contacts": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contact_get(self, contact_id: int) -> None:
        try:
            row = db.query_one("SELECT * FROM mail_contacts WHERE id = %s", (contact_id,))
            if not row:
                _json_response(self, 404, {"error": "Contact not found"})
                return
            _json_response(self, 200, row)
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contact_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            user = _require_auth(self)
            result = db.query_one(
                """INSERT INTO mail_contacts (user_id, email, name, company, phone, notes)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (user["id"] if user else None, payload.get("email"), payload.get("name", ""),
                 payload.get("company", ""), payload.get("phone", ""), payload.get("notes", "")),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contact_update(self, contact_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            db.execute(
                "UPDATE mail_contacts SET email=%s, name=%s, company=%s, phone=%s, notes=%s, updated_at=NOW() WHERE id=%s",
                (payload.get("email"), payload.get("name"), payload.get("company"),
                 payload.get("phone"), payload.get("notes"), contact_id),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contact_delete(self, contact_id: int) -> None:
        try:
            db.execute("DELETE FROM mail_contacts WHERE id = %s", (contact_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contacts_import(self) -> None:
        try:
            user = _require_auth(self)
            raw = _read_body(self)
            import json
            data = json.loads(raw.decode("utf-8"))
            csv_text = data.get("csv", "")
            if not csv_text:
                _json_response(self, 400, {"error": "No CSV data"})
                return
            import csv
            import io
            import re
            reader = csv.DictReader(io.StringIO(csv_text))
            count = 0
            is_gmail = "E-mail 1 - Value" in (reader.fieldnames or [])
            if is_gmail:
                # Gmail CSV has unquoted commas in Labels field that break column parsing.
                # Extract emails and names from raw text using regex.
                email_re = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')
                name_re = re.compile(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', re.MULTILINE)
                logger.info("Gmail CSV import: %d lines, first 200 chars: %s", len(csv_text.splitlines()), csv_text[:200])
                for line in csv_text.splitlines():
                    emails_found = email_re.findall(line)
                    for email in emails_found:
                        email = email.strip()
                        if not email or email.endswith(('.com', '.net', '.org')) and len(email) < 5:
                            continue
                        # Try to extract name from the line
                        name = ""
                        name_match = name_re.match(line)
                        if name_match:
                            name = name_match.group(1)
                        db.execute(
                            """INSERT INTO mail_contacts (user_id, email, name, company, phone, notes)
                               VALUES (%s, %s, %s, %s, %s, %s)
                               ON CONFLICT (user_id, email) DO UPDATE SET name=EXCLUDED.name, company=EXCLUDED.company""",
                            (user["id"], email, name, "", "", ""),
                        )
                        count += 1
            else:
                for row in reader:
                    email = (row.get("email") or "").strip()
                    if not email:
                        continue
                    name = (row.get("name") or "").strip()
                    company = (row.get("company") or "").strip()
                    phone = (row.get("phone") or "").strip()
                    notes = (row.get("notes") or "").strip()
                    db.execute(
                        """INSERT INTO mail_contacts (user_id, email, name, company, phone, notes)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (user_id, email) DO UPDATE SET name=EXCLUDED.name, company=EXCLUDED.company""",
                        (user["id"], email, name, company, phone, notes),
                    )
                    count += 1
            _json_response(self, 200, {"ok": True, "imported": count})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contacts_export(self) -> None:
        try:
            user = _require_auth(self)
            rows = db.query_dicts(
                "SELECT name, email, company, phone, notes FROM mail_contacts WHERE user_id = %s ORDER BY name",
                (user["id"],),
            )
            import csv
            import io
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["name", "email", "company", "phone", "notes"])
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="contacts.csv"')
            self.end_headers()
            self.wfile.write(output.getvalue().encode("utf-8"))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_signature_get(self, account_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_accessible(user, account_id):
                _json_response(self, 403, {"error": "You don't have access to this account"})
                return
            row = db.query_one("SELECT signature_html, signature_text FROM mail_accounts WHERE id = %s", (account_id,))
            if not row:
                _json_response(self, 404, {"error": "Account not found"})
                return
            _json_response(self, 200, {"signature_html": row["signature_html"] or "", "signature_text": row["signature_text"] or ""})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_account_signature_put(self, account_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_account_manageable(user, account_id):
                _json_response(self, 403, {"error": "You don't have permission to manage this account"})
                return
            payload = json.loads(_read_body(self) or b"{}")
            db.execute(
                "UPDATE mail_accounts SET signature_html=%s, signature_text=%s WHERE id=%s",
                (payload.get("signature_html", ""), payload.get("signature_text", ""), account_id),
            )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_attachments(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            rows = db.query_dicts(
                "SELECT * FROM mail_attachments WHERE message_id = %s ORDER BY filename",
                (message_id,),
            )
            _json_response(self, 200, {"attachments": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_attachment_download(self, message_id: int, attachment_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            att = db.query_one(
                "SELECT * FROM mail_attachments WHERE id = %s AND message_id = %s",
                (attachment_id, message_id),
            )
            if not att:
                _json_response(self, 404, {"error": "Attachment not found"})
                return
            msg = db.query_one(
                "SELECT m.imap_uid, m.folder, m.account_id FROM mail_messages m WHERE m.id = %s",
                (message_id,),
            )
            if not msg:
                _json_response(self, 404, {"error": "Message not found"})
                return
            acct = db.query_one(
                "SELECT * FROM mail_accounts WHERE id = %s", (msg["account_id"],),
            )
            if not acct:
                _json_response(self, 404, {"error": "Account not found"})
                return
            # Lazy-fetch attachment content from IMAP
            try:
                from imap_tools import A as _ImapA
                mailbox = _imap_connect(msg["account_id"])
                if not mailbox:
                    _json_response(self, 502, {"error": "Could not connect to IMAP"})
                    return
                try:
                    mailbox.folder.set(msg["folder"] or "INBOX")
                    uid = str(msg["imap_uid"])
                    msgs = list(mailbox.fetch(criteria=_ImapA(uid=uid), mark_seen=False, bulk=False))
                    if not msgs:
                        _json_response(self, 404, {"error": "Message not found on IMAP server"})
                        return
                    imap_msg = msgs[0]
                    content = None
                    part_id = str(att["imap_part_id"]) if att["imap_part_id"] else ""
                    for att_obj in (imap_msg.attachments or []):
                        if part_id and str(att_obj.part or "") == part_id:
                            content = att_obj.payload
                            break
                        if att_obj.filename == att["filename"]:
                            content = att_obj.payload
                    if content is None:
                        _json_response(self, 404, {"error": "Attachment content not found on IMAP server"})
                        return
                    # Stream binary content
                    self.send_response(200)
                    self.send_header("Content-Type", att["mime_type"] or "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{att["filename"]}"')
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                finally:
                    try:
                        mailbox.logout()
                    except Exception:
                        pass
            except Exception as imap_e:
                logger.exception("IMAP fetch failed for attachment %d", attachment_id)
                _json_response(self, 502, {"error": f"IMAP fetch failed: {imap_e}"})
        except Exception as e:
            logger.exception("Failed to download attachment %d", attachment_id)
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_attachment_editor_config(self, message_id: int, attachment_id: int) -> None:
        """Return OnlyOffice editor config for a mail attachment (view-only, comment + chat enabled)."""
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            att = db.query_one(
                "SELECT * FROM mail_attachments WHERE id = %s AND message_id = %s",
                (attachment_id, message_id),
            )
            if not att:
                _json_response(self, 404, {"error": "Attachment not found"})
                return
            filename = att.get("filename") or "file"
            file_ext = Path(filename).suffix.lstrip(".").lower()
            doc_type = _document_type_from_ext(file_ext)
            supported = {"word", "cell", "slide", "pdf"}
            if doc_type not in supported:
                _json_response(self, 200, {"error": "unsupported", "downloadUrl": f"/api/v2/mail/messages/{message_id}/attachments/{attachment_id}/download"})
                return
            if not DOCS_PUBLIC_URL:
                _json_response(self, 503, {"error": "Document Server public URL not configured"})
                return
            if not DOCS_JWT_SECRET:
                _json_response(self, 503, {"error": "Document Server JWT secret not configured"})
                return
            # Stable key per attachment id so reloads join the same comment/chat session.
            doc_key = f"sietch-mail-att-{attachment_id}"
            token_payload = {"type": "mail_att", "message_id": message_id, "attachment_id": attachment_id, "ts": int(time.time())}
            download_token = _sign_jwt(token_payload, DOCS_JWT_SECRET)
            serve_url = f"{CRM_PUBLIC_URL}/api/v2/mail/attachments/serve?token={download_token}"
            editor_mode = "view"  # mail attachments are always view-only
            customization = _build_onlyoffice_customization()
            customization["review"]["trackChanges"] = False
            config = {
                "document": {
                    "fileType": file_ext,
                    "key": doc_key,
                    "title": filename,
                    "url": serve_url,
                    "permissions": _build_onlyoffice_permissions(can_edit=False),
                },
                "documentType": doc_type,
                "editorConfig": {
                    "mode": editor_mode,
                    "lang": "en",
                    "region": "en-US",
                    "user": {
                        "id": str(user["id"]),
                        "name": user.get("display_name") or user.get("email") or "User",
                    },
                    "customization": customization,
                },
            }
            config["token"] = _sign_jwt(config, DOCS_JWT_SECRET)
            config["docsApiUrl"] = f"{DOCS_PUBLIC_URL}/web-apps/apps/api/documents/api.js"
            _json_response(self, 200, config)
        except Exception as e:
            logger.exception("Failed to build mail attachment editor config")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_attachment_serve(self) -> None:
        """Serve a mail attachment to the OnlyOffice Document Server via JWT auth.

        Source priority (Bug fix 2026-10-19):
          1. local stored_path — already-on-disk mirror, fastest, no network
          2. OnlyOffice via crm_file_id — filehandler.ashx download, works for
             years-old emails regardless of live IMAP state. This is the
             primary path on dev / production when the user's mail aggregator
             archived the email into OnlyOffice's DB.
          3. live IMAP — fallback for brand-new mail that hasn't been
             archived into OnlyOffice yet (mail aggregator lag).

        All three return Content-Type + Content-Disposition so the OnlyOffice
        editor renders the file with its real filename.
        """
        try:
            token = ""
            if "?" in (self.path or ""):
                qs = self.path.split("?", 1)[1]
                for part in qs.split("&"):
                    if part.startswith("token="):
                        token = part[6:]
                        break
            if not token:
                _json_response(self, 401, {"error": "Missing token"})
                return
            payload = _verify_jwt(token, DOCS_JWT_SECRET)
            if not payload or payload.get("type") != "mail_att":
                _json_response(self, 403, {"error": "Invalid or expired token"})
                return
            message_id = payload["message_id"]
            attachment_id = payload["attachment_id"]
            att = db.query_one(
                "SELECT * FROM mail_attachments WHERE id = %s AND message_id = %s",
                (attachment_id, message_id),
            )
            if not att:
                _json_response(self, 404, {"error": "Attachment not found"})
                return

            filename = att["filename"]
            mime_type = att.get("mime_type") or "application/octet-stream"

            def _send_bytes(content: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Disposition", f'inline; filename="{filename}"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            # 1. Local mirror (existing fast path)
            stored_path = att.get("stored_path")
            if stored_path:
                full = ROOT / stored_path
                if full.is_file():
                    try:
                        _send_bytes(full.read_bytes())
                        return
                    except Exception as e:
                        logger.warning("Failed to read local mirror %s: %s", full, e)
                        # fall through to OnlyOffice

            # 2. OnlyOffice via crm_file_id (NEW primary path)
            crm_file_id = att.get("crm_file_id")
            if crm_file_id and OO_PORTAL_URL:
                try:
                    oo_url = f"{OO_PORTAL_URL.rstrip('/')}/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={crm_file_id}"
                    oo_data = _fetch_onlyoffice_file(oo_url, timeout=30)
                    if oo_data:
                        # Opportunistically cache to disk for next time
                        try:
                            safe_name = re.sub(r"[^\w.\- ]+", "_", filename).strip("._") or "file"
                            rel_dir = Path("data") / "mail-attachments" / str(message_id)
                            full = ROOT / rel_dir / f"{int(attachment_id)}_{safe_name}"
                            full.parent.mkdir(parents=True, exist_ok=True)
                            full.write_bytes(oo_data)
                            db.execute(
                                "UPDATE mail_attachments SET stored_path=%s, fetch_source=COALESCE(fetch_source,'onlyoffice') "
                                "WHERE id=%s AND stored_path IS NULL",
                                (str(rel_dir / full.name), attachment_id),
                            )
                        except Exception as cache_exc:
                            logger.warning("Failed to cache OnlyOffice-fetched mail attachment: %s", cache_exc)
                        _send_bytes(oo_data)
                        return
                except Exception as e:
                    logger.warning("OnlyOffice fetch for crm_file_id=%s failed: %s", crm_file_id, e)
                    # fall through to IMAP

            # 3. Live IMAP fallback (existing behavior)
            msg = db.query_one(
                "SELECT m.imap_uid, m.folder, m.account_id FROM mail_messages m WHERE m.id = %s",
                (message_id,),
            )
            if not msg:
                _json_response(self, 410, {"error": "Attachment no longer available: source message gone from local store"})
                return
            mailbox = _imap_connect(msg["account_id"])
            if not mailbox:
                _json_response(self, 502, {"error": "Could not connect to IMAP"})
                return
            try:
                from imap_tools import A as _ImapA2
                mailbox.folder.set(msg["folder"] or "INBOX")
                uid = str(msg["imap_uid"])
                msgs = list(mailbox.fetch(criteria=_ImapA2(uid=uid), mark_seen=False, bulk=False))
                if not msgs:
                    _json_response(self, 410, {"error": "Attachment no longer available: source message purged from mail server"})
                    return
                imap_msg = msgs[0]
                content = None
                part_id = str(att.get("imap_part_id") or "")
                for att_obj in (imap_msg.attachments or []):
                    if part_id and str(att_obj.part or "") == part_id:
                        content = att_obj.payload
                        break
                    if att_obj.filename == att["filename"]:
                        content = att_obj.payload
                if content is None:
                    _json_response(self, 410, {"error": "Attachment no longer available: content missing on mail server"})
                    return
                _send_bytes(content)
            finally:
                try:
                    mailbox.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Failed to serve mail attachment")
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_message_headers(self, message_id: int) -> None:
        try:
            user = _require_auth(self)
            if not user:
                return
            if not _mail_message_accessible(user, message_id):
                _json_response(self, 403, {"error": "You don't have access to this message"})
                return
            row = db.query_one("SELECT raw_headers FROM mail_messages WHERE id = %s", (message_id,))
            if not row:
                _json_response(self, 404, {"error": "Message not found"})
                return
            _json_response(self, 200, {"headers": row["raw_headers"] or ""})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_threads(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        account_id = qs.get("account_id", [None])[0]
        user = _require_auth(self)
        if not user:
            return
        try:
            where = "m.folder != 'Trash'"
            params: tuple = ()
            if account_id == 'crm':
                where += " AND m.account_id IN (SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE)"
            elif account_id:
                try:
                    where += " AND m.account_id = %s"
                    params = (int(account_id),)
                except (ValueError, TypeError):
                    pass
            where += f" AND m.account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})"
            params += (user["id"], user["id"])
            rows = db.query_dicts(
                f"SELECT m.conversation_id, COUNT(*) AS message_count, "
                f"MAX(m.date_received) AS latest_date, "
                f"MAX(m.subject) AS subject, "
                f"MAX(m.from_addr) AS from_addr, "
                f"BOOL_OR(NOT m.is_read) AS has_unread "
                f"FROM mail_messages m WHERE {where} AND m.conversation_id IS NOT NULL "
                f"GROUP BY m.conversation_id ORDER BY latest_date DESC LIMIT 50",
                params,
            )
            _json_response(self, 200, {"threads": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_folders(self) -> None:
        try:
            qs = parse_qs(urlparse(self.path).query)
            account_id = qs.get("account_id", [None])[0]
            if account_id == "crm":
                rows = db.query_dicts(
                    "SELECT * FROM mail_folders "
                    "WHERE imap_account_id IN (SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE) "
                    "ORDER BY sort_order, name",
                )
            elif account_id:
                rows = db.query_dicts(
                    "SELECT * FROM mail_folders WHERE imap_account_id = %s ORDER BY sort_order, name",
                    (int(account_id),),
                )
            else:
                rows = db.query_dicts(
                    "SELECT * FROM mail_folders ORDER BY sort_order, name"
                )
            _json_response(self, 200, {"folders": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_folder_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            name = payload.get("name", "").strip()
            if not name:
                _json_response(self, 400, {"error": "Name required"})
                return
            user = _require_auth(self)
            result = db.query_one(
                "INSERT INTO mail_folders (name, user_id) VALUES (%s, %s) RETURNING id",
                (name, user["id"] if user else None),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_folder_update(self, folder_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            name = payload.get("name", "").strip()
            if not name:
                _json_response(self, 400, {"error": "Name required"})
                return
            folder = db.query_one("SELECT name, is_system FROM mail_folders WHERE id = %s", (folder_id,))
            if not folder:
                _json_response(self, 404, {"error": "Folder not found"})
                return
            if folder["is_system"]:
                _json_response(self, 400, {"error": "Cannot rename system folder"})
                return
            # Update messages in this folder
            db.execute("UPDATE mail_messages SET folder = %s WHERE folder = %s", (name, folder["name"]))
            db.execute("UPDATE mail_folders SET name = %s WHERE id = %s", (name, folder_id))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_folder_delete(self, folder_id: int) -> None:
        try:
            folder = db.query_one("SELECT name, is_system FROM mail_folders WHERE id = %s", (folder_id,))
            if not folder:
                _json_response(self, 404, {"error": "Folder not found"})
                return
            if folder["is_system"]:
                _json_response(self, 400, {"error": "Cannot delete system folder"})
                return
            # Move messages in this folder to INBOX
            db.execute("UPDATE mail_messages SET folder = 'INBOX' WHERE folder = %s", (folder["name"],))
            db.execute("DELETE FROM mail_folders WHERE id = %s", (folder_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    # ── Contractors ───────────────────────────────────────────────────────

    def _handle_mail_contractors(self) -> None:
        try:
            rows = db.query_dicts("SELECT * FROM mail_contractors ORDER BY priority DESC, name")
            _json_response(self, 200, {"contractors": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contractor_get(self, contractor_id: int) -> None:
        try:
            row = db.query_one("SELECT * FROM mail_contractors WHERE id = %s", (contractor_id,))
            if not row:
                _json_response(self, 404, {"error": "Contractor not found"})
                return
            _json_response(self, 200, dict(row))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contractor_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            name = (payload.get("name") or "").strip()
            if not name:
                _json_response(self, 400, {"error": "name required"})
                return
            result = db.query_one(
                "INSERT INTO mail_contractors (name, imap_account_id, folder, action, responsible_user_id, enabled, priority) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (name, payload.get("imap_account_id"), payload.get("folder", "INBOX"),
                 payload.get("action", "link_only"), payload.get("responsible_user_id"),
                 payload.get("enabled", True), payload.get("priority", 0)),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contractor_update(self, contractor_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            existing = db.query_one("SELECT id FROM mail_contractors WHERE id = %s", (contractor_id,))
            if not existing:
                _json_response(self, 404, {"error": "Contractor not found"})
                return
            sets, params_list = [], []
            for field in ("name", "imap_account_id", "folder", "action", "responsible_user_id", "enabled", "priority"):
                if field in payload:
                    sets.append(f"{field} = %s")
                    params_list.append(payload[field])
            if sets:
                sets.append("updated_at = NOW()")
                params_list.append(contractor_id)
                db.execute("UPDATE mail_contractors SET " + ", ".join(sets) + " WHERE id = %s", tuple(params_list))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_contractor_delete(self, contractor_id: int) -> None:
        try:
            existing = db.query_one("SELECT id FROM mail_contractors WHERE id = %s", (contractor_id,))
            if not existing:
                _json_response(self, 404, {"error": "Contractor not found"})
                return
            db.execute("DELETE FROM mail_contractors WHERE id = %s", (contractor_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    # ── Classification Rules ──────────────────────────────────────────────

    def _handle_mail_classification_rules(self) -> None:
        try:
            rows = db.query_dicts("SELECT * FROM mail_classification_rules ORDER BY priority DESC, rule_name")
            _json_response(self, 200, {"rules": rows})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_classification_rule_get(self, rule_id: int) -> None:
        try:
            row = db.query_one("SELECT * FROM mail_classification_rules WHERE id = %s", (rule_id,))
            if not row:
                _json_response(self, 404, {"error": "Rule not found"})
                return
            _json_response(self, 200, dict(row))
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_classification_rule_create(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            rule_name = (payload.get("rule_name") or "").strip()
            rule_type = (payload.get("rule_type") or "").strip()
            pattern = (payload.get("pattern") or "").strip()
            if not rule_name or not rule_type or not pattern:
                _json_response(self, 400, {"error": "rule_name, rule_type, and pattern required"})
                return
            # Validate regex patterns at save time so admins see a clear error
            # instead of the rule silently never matching at scan time.
            if rule_type in ("subject_regex", "body_regex") and not mail_scanner._is_valid_regex(pattern):
                _json_response(self, 400, {"error": f"Invalid regex pattern for rule type {rule_type!r}"})
                return
            result = db.query_one(
                "INSERT INTO mail_classification_rules (rule_name, rule_type, pattern, action, action_target, priority, enabled) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (rule_name, rule_type, pattern, payload.get("action", "tag"),
                 payload.get("action_target"), payload.get("priority", 0), payload.get("enabled", True)),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_classification_rule_update(self, rule_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            existing = db.query_one("SELECT id FROM mail_classification_rules WHERE id = %s", (rule_id,))
            if not existing:
                _json_response(self, 404, {"error": "Rule not found"})
                return
            # If the update touches both rule_type and pattern, validate the
            # resulting combination. If only one of them is being changed, look
            # up the other from the existing row to validate the pair.
            if "pattern" in payload or "rule_type" in payload:
                rule_type = (payload.get("rule_type") or "").strip()
                pattern = (payload.get("pattern") or "").strip()
                if not rule_type or not pattern:
                    # Fetch the missing field from DB to do full validation.
                    full = db.query_one(
                        "SELECT rule_type, pattern FROM mail_classification_rules WHERE id = %s",
                        (rule_id,),
                    )
                    if full:
                        rule_type = rule_type or full.get("rule_type") or ""
                        pattern = pattern or full.get("pattern") or ""
                if rule_type in ("subject_regex", "body_regex") and pattern and not mail_scanner._is_valid_regex(pattern):
                    _json_response(self, 400, {"error": f"Invalid regex pattern for rule type {rule_type!r}"})
                    return
            sets, params_list = [], []
            for field in ("rule_name", "rule_type", "pattern", "action", "action_target", "priority", "enabled"):
                if field in payload:
                    sets.append(f"{field} = %s")
                    params_list.append(payload[field])
            if sets:
                sets.append("updated_at = NOW()")
                params_list.append(rule_id)
                db.execute("UPDATE mail_classification_rules SET " + ", ".join(sets) + " WHERE id = %s", tuple(params_list))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_classification_rule_delete(self, rule_id: int) -> None:
        try:
            existing = db.query_one("SELECT id FROM mail_classification_rules WHERE id = %s", (rule_id,))
            if not existing:
                _json_response(self, 404, {"error": "Rule not found"})
                return
            db.execute("DELETE FROM mail_classification_rules WHERE id = %s", (rule_id,))
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    # ── Feedback ──────────────────────────────────────────────────────────

    def _handle_mail_feedback(self) -> None:
        try:
            entries = mail_scanner.get_feedback_entries(200) if mail_scanner else []
            _json_response(self, 200, {"entries": entries})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

    def _handle_mail_feedback_review(self, feedback_id: int) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
            approved = payload.get("approved", False)
            user = _require_auth(self)
            reviewed_by = user["id"] if user else None
            # Store as training data if approved
            if approved:
                db.execute(
                    "INSERT INTO classifier_training_data (message_subject, sender_email, correct_classification, correct_project_id, correct_action_type) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (payload.get("subject", ""), payload.get("sender", ""),
                     payload.get("classification", ""), payload.get("project_id"),
                     payload.get("action_type", "")),
                )
            _json_response(self, 200, {"ok": True})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})

# ── Main ───────────────────────────────────────────────────────────────

def _lan_urls(port):
    addrs = ["127.0.0.1"]
    try:
        h = socket.gethostname()
        for a in socket.gethostbyname_ex(h)[2]:
            if a and not a.startswith("127."):
                addrs.append(a)
    except:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        a = s.getsockname()[0]
        if a and not a.startswith("127.") and a not in addrs:
            addrs.append(a)
        s.close()
    except:
        pass
    return [f"http://{a}:{port}" for a in sorted(set(addrs))]


def _sse_publish_recent_notifications(user_id: str, since_ts: float) -> None:
    """Publish any notifications inserted since since_ts for this user.
    Catches DB-triggered inserts (e.g. task assignment) that bypass event_bus."""
    try:
        rows = db.query(
            "SELECT id, type, message, payload, opportunity_id, actor_user_id "
            "FROM notifications WHERE created_at > to_timestamp(%s) AND user_id = %s "
            "ORDER BY created_at ASC LIMIT 20",
            (since_ts, int(user_id)),
        )
        for row in rows:
            event_bus.publish("notification:new", {
                "id": row[0], "type": row[1], "message": row[2],
                "payload": row[3], "opportunity_id": row[4],
                "actor_user_id": row[5],
            }, user_ids=[user_id])
    except Exception:
        pass  # non-fatal — will retry on next sweep


def _bind_server_with_retry(port: int, attempts: int = 60, delay: float = 0.5) -> ThreadingHTTPServer:
    """Bind the HTTP server, retrying while the port is still held.

    The admin "Restart Server" button spawns a replacement process and then
    os._exit()s. Whichever process wins the race, the loser finds the port
    still bound — SO_REUSEADDR (HTTPServer.allow_reuse_address = 1) only
    covers TIME_WAIT, not an active listener. Without this retry the
    replacement dies with EADDRINUSE while the original has already exited,
    and since the container's restart policy is `on-failure:5` an exit-0
    container is never restarted — leaving the CRM down with no supervisor.

    Retrying also makes the server survive any transient port conflict at
    boot (e.g. a previous instance still shutting down).

    Non-EADDRINUSE errors (bad address, permission denied) raise immediately.
    """
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            return ThreadingHTTPServer(("0.0.0.0", port), KanbanHandler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_exc = exc
            if attempt == 0:
                print(f"Port {port} still in use (restart race or stale instance); retrying for up to {attempts * delay:.0f}s…", flush=True)
            time.sleep(delay)
    raise last_exc if last_exc else OSError(f"Could not bind port {port}")


def main() -> None:
    server = _bind_server_with_retry(PORT)
    print(f"Sietch CRM v3.0 starting on port {PORT}")
    for u in _lan_urls(PORT):
        print(f"Open: {u}")
    print(f"Version: {APP_VERSION}")
    dispatcher_stop = start_dispatcher()
    scanner_stop = mail_scanner.start_scanner() if mail_scanner else None
    # Service-feed poller (RSS/Atom → notifications). Started after dispatcher
    # so notification rows inserted by the poller get picked up immediately.
    try:
        import service_feed_poller
        service_feed_poller.start_poller(sys.modules[__name__])
    except Exception as e:
        print(f"Service feed poller not started: {e}")
    backup_stop = None
    reminders_stop = None
    calendar_sync_stop = None
    try:
        from backup_service import start_backup_scheduler
        backup_stop = start_backup_scheduler()
    except Exception as e:
        print(f"Backup scheduler not started: {e}")
    try:
        from task_reminders import start_reminder_sweep
        reminders_stop = start_reminder_sweep()
    except Exception as e:
        print(f"Task reminder sweep not started: {e}")
    try:
        calendar_sync_stop = start_calendar_sync()
        print("Calendar sync service started")
    except Exception as e:
        print(f"Calendar sync not started: {e}")
    automation_stop = None
    try:
        from automation_engine import start_automation_engine
        automation_stop = start_automation_engine()
        print("Automation engine started")
    except Exception as e:
        print(f"Automation engine not started: {e}")
    ts_overdue_stop = None
    try:
        from timesheet_overdue_checker import start_checker as start_ts_overdue_checker
        ts_overdue_stop = start_ts_overdue_checker()
        print("Timesheet overdue checker started")
    except Exception as e:
        print(f"Timesheet overdue checker not started: {e}")
    # READY — log a single line that ops can grep for to confirm all
    # background subsystems are up before the server starts accepting
    # requests. Each subsystem's status is included so a partial start
    # (e.g. "scanner disabled") is visible at a glance.
    import time as _time
    _subsystem_status = {
        "dispatcher": "ok" if dispatcher_stop else "disabled",
        "scanner": "ok" if scanner_stop else "disabled",
        "service_feeds": "ok",
        "backup": "ok" if backup_stop else "disabled",
        "task_reminders": "ok" if reminders_stop else "disabled",
        "calendar_sync": "ok" if calendar_sync_stop else "disabled",
        "automation": "ok" if automation_stop else "disabled",
        "ts_overdue": "ok" if ts_overdue_stop else "disabled",
    }
    print(f"[READY {int(_time.time())}] sietch-crm ready on port {PORT} pid {os.getpid()} subsystems=" + ",".join(f"{k}={v}" for k, v in _subsystem_status.items()), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_dispatcher(dispatcher_stop)
        if backup_stop:
            from backup_service import stop_backup_scheduler
            stop_backup_scheduler(backup_stop)
        if reminders_stop:
            from task_reminders import stop_reminder_sweep
            stop_reminder_sweep(reminders_stop)
        if calendar_sync_stop:
            stop_calendar_sync(calendar_sync_stop)
        if automation_stop:
            from automation_engine import stop_automation_engine
            stop_automation_engine(automation_stop)
        if ts_overdue_stop:
            from timesheet_overdue_checker import stop_checker as stop_ts_overdue_checker
            stop_ts_overdue_checker(ts_overdue_stop)
        server.shutdown()


if __name__ == "__main__":
    main()
