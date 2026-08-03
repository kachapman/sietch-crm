#!/usr/bin/env python3
"""Sietch CRM v3.0 — Standalone server with PostgreSQL backend.

Replaces OnlyOffice CRM proxy with direct database queries.
All data owned locally. Zero external API calls.
"""

from __future__ import annotations

import collections
import base64
import gzip
import sys
import threading
import traceback
import hashlib
import hmac
import io
import json
import os
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

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow is optional in dev, present in Docker
    Image = None

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sietch_server")

# ── Infrastructure ring buffer (rolling 200 events) ───────────────────────────
_infra_log: collections.deque = collections.deque(maxlen=200)
_infra_start_time = time.time()

def log_infra_event(level: str, msg: str) -> None:
    """Append an event to the infrastructure ring buffer."""
    _infra_log.append({"ts": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg})

log_infra_event("info", "Server started")

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
    remove_mapping_by_chat, set_nickname, set_verify_chat_id,
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
DOCS_JWT_SECRET = os.environ.get("DOCS_JWT_SECRET", "")
DOCS_INTERNAL_URL = os.environ.get("DOCS_INTERNAL_URL", "").rstrip("/")
DOCS_PUBLIC_URL = os.environ.get("DOCS_PUBLIC_URL", "").rstrip("/")
CRM_PUBLIC_URL = os.environ.get("CRM_PUBLIC_URL", "").rstrip("/") or DOCS_PUBLIC_URL
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

# ── DB init ────────────────────────────────────────────────────────────────────
import db
import auth as auth_mod

db.init_db()

# ── Schema migrations ──────────────────────────────────────────────────────────
try:
    db.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS monitored_folders TEXT DEFAULT 'INBOX'")
except Exception:
    pass  # table may not exist yet

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

# History category id for email link events ("Email" in history_categories)
EMAIL_HISTORY_CATEGORY_ID = 16

# ── Helpers ────────────────────────────────────────────────────────────────────


def _json_response(handler: SimpleHTTPRequestHandler, status: int, payload: dict | list) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
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
    """Return admin user dict or None after sending error response."""
    user = _require_auth(handler)
    if not user:
        return None
    if not user.get("is_admin"):
        _json_response(handler, 403, {"error": "Forbidden"})
        return None
    return user


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
    from imap_tools import MailBox
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


def _is_running_in_docker() -> bool:
    """Best-effort detection of whether we are inside a Docker container."""
    try:
        with open("/proc/1/cgroup", "r") as f:
            return "docker" in f.read() or "kubepods" in f.read()
    except Exception:
        return False


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
        log_infra_event("error", f"Document Server unreachable: {ds_path}")
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

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api_get()
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
        if self.path.startswith("/api/"):
            self._handle_api_post_put("POST")
            return
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api_post_put("PUT")
            return
        self.send_error(405)

    def do_DELETE(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api_post_put("DELETE")
            return
        self.send_error(405)

    def do_PATCH(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api_post_put("PATCH")
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
            _json_response(self, 401, {"error": "Invalid email or password"})
            return

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
            },
        }).encode("utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

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
            reset_url = f"{self.headers.get('Origin', 'http://localhost:' + str(PORT))}/reset?token={token}"
            send_password_reset_email(email, reset_url)
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

        # ── Config / Info ──
        if api_path == "/api/config":
            _json_response(self, 200, {"version": APP_VERSION})
            return
        if api_path == "/api/branding":
            try:
                row = db.query_one(
                    """SELECT company_name, logo_path, watermark_path, login_title,
                              header_eyebrow, header_title, primary_color, favicon_path
                       FROM branding ORDER BY id LIMIT 1"""
                )
                if row:
                    _json_response(self, 200, {
                        "companyName": row["company_name"] or "Sietch CRM",
                        "logoPath": row["logo_path"] or "/assets/sietch-logo-2-nobg2.png",
                        "watermarkPath": row["watermark_path"],
                        "loginTitle": row["login_title"] or "Sietch CRM",
                        "headerEyebrow": row["header_eyebrow"] or "Sietch CRM",
                        "headerTitle": row["header_title"] or "Workspace <em>dashboard</em>",
                        "primaryColor": row["primary_color"] or "#3b82f6",
                        "faviconPath": row["favicon_path"] or "/favicon.ico",
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
            _json_response(self, 200, {"events": list(_infra_log), "uptime": time.time() - _infra_start_time})
            return
        if api_path == "/api/v2/admin/docker-health":
            user = _require_admin(self)
            if not user:
                return
            self._handle_docker_health()
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

        # ── Admin check ──
        if api_path == "/api/check-admin":
            self._handle_check_admin()
            return

        # ── Bot customers (admin, unchanged) ──
        if api_path == "/api/bot-customers":
            self._handle_bot_customers_list()
            return
        if api_path.startswith("/api/bot/"):
            self._handle_bot_api_get()
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
            self._handle_project_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/tags$", api_path)
        if m:
            self._handle_project_tags_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/custom-fields$", api_path)
        if m:
            self._handle_project_custom_fields_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history$", api_path)
        if m:
            self._handle_project_history_get(int(m.group(1)), qs)
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photos$", api_path)
        if m:
            self._handle_project_photos_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photo-folders$", api_path)
        if m:
            self._handle_project_photo_folders_get(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history/(\d+)/replies$", api_path)
        if m:
            self._handle_history_replies_get(int(m.group(1)), int(m.group(2)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/documents$", api_path)
        if m:
            self._handle_project_documents_get(int(m.group(1)))
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
        if api_path == "/api/v2/contacts":
            self._handle_contacts_get(qs)
            return
        if api_path == "/api/v2/tasks":
            self._handle_tasks_get(qs)
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
        if api_path == "/api/v2/me/notification-prefs":
            self._handle_notification_prefs_get()
            return
        if api_path == "/api/v2/notifications":
            self._handle_notifications_get(qs)
            return
        if api_path == "/api/v2/notifications/unread-count":
            self._handle_notifications_unread_count()
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

        self.send_error(404)

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
        if api_path == "/api/user-profile" and method == "PUT":
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
        if api_path == "/api/v2/me/notification-prefs" and method == "PUT":
            self._handle_notification_prefs_put()
            return

        # ── Branding (admin) ──
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
                if existing:
                    db.execute(
                        """UPDATE branding SET
                            company_name = COALESCE(%s, company_name),
                            logo_path = COALESCE(%s, logo_path),
                            watermark_path = %s,
                            login_title = COALESCE(%s, login_title),
                            header_eyebrow = COALESCE(%s, header_eyebrow),
                            header_title = COALESCE(%s, header_title),
                            primary_color = COALESCE(%s, primary_color),
                            favicon_path = COALESCE(%s, favicon_path),
                            updated_at = NOW()
                        WHERE id = %s""",
                        (payload.get("companyName"), payload.get("logoPath"),
                         payload.get("watermarkPath"), payload.get("loginTitle"),
                         payload.get("headerEyebrow"), payload.get("headerTitle"),
                         payload.get("primaryColor"), payload.get("faviconPath"),
                         existing["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO branding (company_name, logo_path, watermark_path,
                            login_title, header_eyebrow, header_title, primary_color, favicon_path)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (payload.get("companyName", "Sietch CRM"),
                         payload.get("logoPath", "/assets/sietch-logo-2-nobg2.png"),
                         payload.get("watermarkPath"),
                         payload.get("loginTitle", "Sietch CRM"),
                         payload.get("headerEyebrow", "Sietch CRM"),
                         payload.get("headerTitle", "Workspace <em>dashboard</em>"),
                         payload.get("primaryColor", "#3b82f6"),
                         payload.get("faviconPath", "/favicon.ico")),
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
            log_infra_event("info", f"Server restart requested by {user.get('email', 'unknown')}")
            _json_response(self, 200, {"ok": True, "message": "Server restarting…"})
            import subprocess
            import threading
            def _respawn():
                try:
                    # Spawn a new server process detached from this one, then exit.
                    subprocess.Popen([sys.executable] + sys.argv, start_new_session=True)
                except Exception:
                    logger.exception("Failed to spawn replacement server process")
                finally:
                    os._exit(0)
            threading.Timer(1.0, _respawn).start()
            return
        if api_path == "/api/v2/admin/restart-container" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            log_infra_event("info", f"Docker container restart requested by {user.get('email', 'unknown')}")
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
                    log_infra_event("error", f"Docker restart failed: {err}")
                    _json_response(self, 500, {"error": f"Docker restart failed: {err}"})
            except Exception as exc:
                logger.exception("Docker restart failed")
                log_infra_event("error", f"Docker restart exception: {exc}")
                _json_response(self, 500, {"error": str(exc)})
            return
        if api_path == "/api/v2/admin/restart-docserver" and method == "POST":
            user = _require_admin(self)
            if not user:
                return
            log_infra_event("info", f"Document Server restart requested by {user.get('email', 'unknown')}")
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
                    log_infra_event("error", f"Document Server restart failed: {err}")
                    _json_response(self, 500, {"error": f"Document Server restart failed: {err}"})
            except Exception as exc:
                logger.exception("Document Server restart failed")
                log_infra_event("error", f"Document Server restart exception: {exc}")
                _json_response(self, 500, {"error": str(exc)})
            return

        # ── Bot customers (admin) ──
        if api_path.startswith("/api/bot-customers"):
            self._handle_bot_customers_post_put(method)
            return
        if api_path.startswith("/api/bot/"):
            self._handle_bot_api_post(method)
            return

        # ── v2 API: Projects ──
        if api_path == "/api/v2/projects" and method == "POST":
            self._handle_project_create()
            return
        m = re.match(r"^/api/v2/projects/(\d+)$", api_path)
        if m and method in ("PUT", "PATCH"):
            self._handle_project_update(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_project_delete(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/tags$", api_path)
        if m and method == "POST":
            self._handle_project_tag_add(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/tags/(\d+)$", api_path)
        if m and method == "DELETE":
            self._handle_project_tag_remove(int(m.group(1)), int(m.group(2)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/custom-fields$", api_path)
        if m and method == "PUT":
            self._handle_project_custom_fields_update(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history$", api_path)
        if m and method == "POST":
            self._handle_project_history_create(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photos$", api_path)
        if m and method == "POST":
            self._handle_project_photo_upload(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/photo-folders$", api_path)
        if m and method == "POST":
            self._handle_project_photo_folder_add(int(m.group(1)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/history/(\d+)/replies$", api_path)
        if m and method == "POST":
            self._handle_history_reply_create(int(m.group(1)), int(m.group(2)))
            return
        m = re.match(r"^/api/v2/projects/(\d+)/documents$", api_path)
        if m and method == "POST":
            self._handle_project_document_upload(int(m.group(1)))
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
        m = re.match(r"^/api/v2/stages/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_stage_update(int(m.group(1)))
            return
        if api_path == "/api/v2/tags" and method == "POST":
            self._handle_tag_create()
            return
        m = re.match(r"^/api/v2/tags/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_tag_update(int(m.group(1)))
            return
        if api_path == "/api/v2/custom-fields" and method == "POST":
            self._handle_custom_field_create()
            return
        m = re.match(r"^/api/v2/custom-fields/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_custom_field_update(int(m.group(1)))
            return
        if api_path == "/api/v2/contacts" and method == "POST":
            self._handle_contact_create()
            return
        m = re.match(r"^/api/v2/contacts/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_contact_update(int(m.group(1)))
            return
        if api_path == "/api/v2/tasks" and method == "POST":
            self._handle_task_create()
            return
        m = re.match(r"^/api/v2/tasks/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_task_update(int(m.group(1)))
            return
        if api_path == "/api/v2/users" and method == "POST":
            self._handle_user_create()
            return
        m = re.match(r"^/api/v2/users/(\d+)$", api_path)
        if m and method == "PUT":
            self._handle_user_update(int(m.group(1)))
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
        # Account CRUD
        if api_path == "/api/v2/mail/accounts" and method == "POST":
            self._handle_mail_account_create()
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

    def _build_projects_where(self, qs: dict) -> tuple[list[str], list]:
        """Build WHERE clauses and params for project list/count queries."""
        qval = self._projects_query_val
        where = ["1=1"]
        params: list = []

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
        where, params = self._build_projects_where(qs)
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
        where, params = self._build_projects_where(qs)

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
                      o.project_number
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
        _json_response(self, 200, {
            "id": row[0], "title": row[1], "description": row[2],
            "stageId": row[3], "stageType": row[4], "bidValue": float(row[5]) if row[5] else None,
            "expectedCloseDate": str(row[6]) if row[6] else None, "probability": row[7],
            "contactId": row[8], "responsibleUserId": row[9], "isPrivate": row[10],
            "created": row[11].isoformat() if row[11] else None,
            "createdBy": row[12], "updatedAt": row[13].isoformat() if row[13] else None,
            "stage": {"title": row[14], "color": row[15]} if row[14] else None,
            "responsible": {"displayName": row[17]} if row[17] else None,
            "contact": {"displayName": f"{row[18] or ''} {row[19] or ''}".strip(), "company": row[20], "email": row[21], "phone": row[22]} if row[18] or row[19] else None,
            "project_number": row[23],
        })

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
                   expected_close_date, contact_id, responsible_user_id, created_by, is_private)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id""",
            (title, description, stage_id, stage_type, bid_value, expected_close, contact_id, responsible_user_id, user["id"], is_private),
        )
        _json_response(self, 201, {"id": opp_id, "ok": True})

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
            params.append(opp_id)
            db.execute(f"UPDATE opportunities SET {', '.join(sets)} WHERE id = %s", (*params,))

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
            """SELECT t.id, t.title, t.color FROM tag_definitions t
               JOIN opportunity_tags ot ON t.id = ot.tag_id
               WHERE ot.opportunity_id = %s""",
            (opp_id,),
        )
        _json_response(self, 200, [{"id": r[0], "title": r[1], "color": r[2]} for r in rows])

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
            "INSERT INTO opportunity_tags (opportunity_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (opp_id, tag_id),
        )
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
                      sort_order, show_on_create, show_on_edit
               FROM custom_field_definitions WHERE is_active = TRUE ORDER BY sort_order"""
        )
        fields = []
        for r in rows:
            opts = db.query(
                "SELECT option_value, option_label FROM custom_field_options WHERE field_id = %s ORDER BY sort_order",
                (r[0],),
            )
            fields.append({
                "id": r[0], "key": r[1], "label": r[2], "type": r[3],
                "isRequired": r[4], "defaultValue": r[5], "sortOrder": r[6],
                "showOnCreate": r[7], "showOnEdit": r[8],
                "options": [{"value": o[0], "label": o[1]} for o in opts],
            })
        _json_response(self, 200, fields)

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
                """SELECT id, event_id, filename, file_path, file_size, mime_type, uploaded_by
                   FROM history_attachments WHERE event_id = ANY(%s)""",
                (event_ids,),
            )
            for ar in att_rows:
                attachments_by_event.setdefault(ar[1], []).append({
                    "id": ar[0], "filename": ar[2], "filePath": ar[3],
                    "fileSize": ar[4], "mimeType": ar[5], "uploadedBy": ar[6],
                })
        events = []
        for r in rows:
            events.append({
                "id": r[0], "categoryId": r[1], "category": {"title": r[2]},
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
        category_id = payload.get("categoryId") or 1
        backdated = str(payload.get("created") or "").strip() or None
        event_id = db.insert_returning(
            """INSERT INTO history_events (opportunity_id, category_id, content, created_by, backdated_created_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (opp_id, category_id, content, user["id"], backdated),
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
                except Exception as e:
                    print(f"[WARN] Failed to create notification for user {uid}: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
        # Link uploaded document IDs as history attachments
        file_ids = payload.get("fileIds") or []
        for fid in file_ids:
            try:
                fid_int = int(fid)
            except (TypeError, ValueError):
                continue
            doc = db.query(
                "SELECT id, title, file_path, file_size, mime_type, uploaded_by FROM project_documents WHERE id = %s AND is_deleted = FALSE",
                (fid_int,), fetch="one",
            )
            if doc:
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
        replies = []
        for r in rows:
            replies.append({
                "id": r[0], "parentReplyId": r[1], "content": "" if r[5] else r[2],
                "createdBy": r[3], "created": r[4].isoformat() if r[4] else None,
                "isDeleted": r[5], "author": r[6] or "",
            })
        _json_response(self, 200, replies)

    def _handle_history_reply_create(self, opp_id: int, event_id: int) -> None:
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
            _json_response(self, 400, {"error": "Content is required"})
            return
        parent_reply_id = payload.get("parentReplyId")
        reply_id = db.insert_returning(
            """INSERT INTO history_replies (event_id, parent_reply_id, content, created_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (event_id, parent_reply_id, content, user["id"]),
        )
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
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            "SELECT id, title, color, sort_order, stage_type, probability, is_active FROM stages ORDER BY sort_order"
        )
        _json_response(self, 200, [
            {"id": r[0], "title": r[1], "color": r[2], "sortOrder": r[3],
             "stageType": r[4], "probability": r[5], "isActive": r[6]}
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
            """INSERT INTO stages (title, color, sort_order, stage_type, probability)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (title, payload.get("color"), payload.get("sortOrder", 0), payload.get("stageType", 0), payload.get("probability", 0)),
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

    # ── Tags ────────────────────────────────────────────────────────────────

    def _handle_tags_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query("SELECT id, title, color FROM tag_definitions ORDER BY title")
        _json_response(self, 200, [{"id": r[0], "title": r[1], "color": r[2]} for r in rows])

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

    # ── Contacts ────────────────────────────────────────────────────────────

    def _handle_contacts_get(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        where, params = ["1=1"], []
        if "q" in qs:
            where.append("(first_name ILIKE %s OR last_name ILIKE %s OR company ILIKE %s OR email ILIKE %s)")
            q = f"%{qs['q'][0]}%"
            params.extend([q, q, q, q])
        rows = db.query(
            f"SELECT id, first_name, last_name, email, phone, company FROM contacts WHERE {' AND '.join(where)} ORDER BY last_name, first_name LIMIT 200",
            (*params,),
        )
        _json_response(self, 200, [
            {"id": r[0], "firstName": r[1], "lastName": r[2], "email": r[3], "phone": r[4], "company": r[5],
             "displayName": f"{r[1] or ''} {r[2] or ''}".strip()}
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
        cid = db.insert_returning(
            """INSERT INTO contacts (first_name, last_name, email, phone, company, job_title, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (payload.get("firstName"), payload.get("lastName"), payload.get("email"),
             payload.get("phone"), payload.get("company"), payload.get("jobTitle"), user["id"]),
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
                           ("phone", "phone"), ("company", "company"), ("jobTitle", "job_title")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        if sets:
            params.append(contact_id)
            db.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

    # ── Tasks ───────────────────────────────────────────────────────────────

    def _handle_tasks_get(self, qs: dict) -> None:
        user = _require_auth(self)
        if not user:
            return
        closed = qs.get("closed", ["false"])[0].lower() == "true"
        where = ["t.is_closed = %s"]
        params: list = [closed]
        if "responsible_user_id" in qs:
            where.append("t.responsible_user_id = %s")
            params.append(int(qs["responsible_user_id"][0]))
        rows = db.query(
            f"""SELECT t.id, t.title, t.description, t.opportunity_id, t.responsible_user_id,
                       t.due_date, t.priority, t.is_closed, t.closed_at, t.created_at,
                       u.display_name, o.title
                FROM tasks t
                LEFT JOIN users u ON t.responsible_user_id = u.id
                LEFT JOIN opportunities o ON t.opportunity_id = o.id
                WHERE {' AND '.join(where)}
                ORDER BY t.due_date ASC NULLS LAST""",
            (*params,),
        )
        _json_response(self, 200, [
            {"id": r[0], "title": r[1], "description": r[2], "opportunityId": r[3],
             "responsibleUserId": r[4], "dueDate": r[5].isoformat() if r[5] else None,
             "priority": r[6], "isClosed": r[7], "closedAt": r[8].isoformat() if r[8] else None,
             "created": r[9].isoformat() if r[9] else None,
             "responsible": {"id": r[4], "displayName": r[10]} if r[10] else None,
             "opportunity": {"title": r[11]} if r[11] else None}
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
            """INSERT INTO tasks (title, description, opportunity_id, responsible_user_id, due_date, priority, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, payload.get("description"), payload.get("opportunityId"),
             payload.get("responsibleUserId", user["id"]),
             payload.get("dueDate"), payload.get("priority", 0), user["id"]),
        )
        _json_response(self, 201, {"id": tid, "ok": True})

    def _handle_task_update(self, task_id: int) -> None:
        user = _require_auth(self)
        if not user:
            return
        try:
            payload = json.loads(_read_body(self) or b"{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
            return
        if "isClosed" in payload:
            if payload["isClosed"]:
                db.execute(
                    "UPDATE tasks SET is_closed = TRUE, closed_at = NOW(), closed_by = %s WHERE id = %s",
                    (user["id"], task_id),
                )
            else:
                db.execute("UPDATE tasks SET is_closed = FALSE, closed_at = NULL WHERE id = %s", (task_id,))
        for field, col in [("title", "title"), ("description", "description"), ("dueDate", "due_date"), ("priority", "priority")]:
            if field in payload:
                db.execute(f"UPDATE tasks SET {col} = %s WHERE id = %s", (payload[field], task_id))
        _json_response(self, 200, {"ok": True})

    # ── Users ───────────────────────────────────────────────────────────────

    def _handle_users_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        rows = db.query(
            "SELECT id, email, display_name, first_name, last_name, is_admin, is_active FROM users ORDER BY display_name"
        )
        _json_response(self, 200, [
            {"id": r[0], "email": r[1], "displayName": r[2], "firstName": r[3],
             "lastName": r[4], "isAdmin": r[5], "isActive": r[6]}
            for r in rows
        ])

    def _handle_me_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        # Fetch avatar_url from DB (not in session cache)
        avatar_row = db.query_one("SELECT avatar_url FROM users WHERE id = %s", (user["id"],))
        avatar_url = avatar_row.get("avatar_url") if avatar_row else None
        _json_response(self, 200, {
            "id": user.get("id"),
            "email": user.get("email"),
            "displayName": user.get("display_name"),
            "firstName": user.get("first_name"),
            "lastName": user.get("last_name"),
            "isAdmin": user.get("is_admin", False),
            "mustChangePassword": user.get("must_change_password", False),
            "avatarUrl": f"/api/v2/my/avatar" if avatar_url else None,
            "thumbnailUrl": f"/api/v2/my/avatar?thumbnail=1" if avatar_url else None,
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
        avatar_path = ROOT / avatar_url
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
        _json_response(self, 200, {
            "inDashboard": prefs.get("in_dashboard", True),
            "telegram": prefs.get("telegram", True),
            "emailDigest": prefs.get("email_digest", "disabled"),
        })

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
            db.execute(
                """INSERT INTO notification_preferences (user_id, notification_type, enabled)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, notification_type) DO UPDATE SET enabled = %s""",
                (user["id"], "email_digest", val, val),
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
        temp_password = str(payload.get("password") or "changeme")
        pw_hash, salt = auth_mod.hash_password(temp_password)
        uid = db.insert_returning(
            """INSERT INTO users (email, password_hash, password_salt, display_name, first_name, last_name, is_admin, must_change_password)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id""",
            (email, pw_hash, salt, payload.get("displayName"), payload.get("firstName"),
             payload.get("lastName"), payload.get("isAdmin", False)),
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
        sets, params = [], []
        for field, col in [("displayName", "display_name"), ("firstName", "first_name"),
                           ("lastName", "last_name"), ("isAdmin", "is_admin"), ("isActive", "is_active")]:
            if field in payload:
                sets.append(f"{col} = %s")
                params.append(payload[field])
        if "password" in payload and payload["password"]:
            pw_hash, salt = auth_mod.hash_password(payload["password"])
            sets.extend(["password_hash = %s", "password_salt = %s", "must_change_password = FALSE"])
            params.extend([pw_hash, salt])
        if sets:
            params.append(user_id)
            db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", (*params,))
        _json_response(self, 200, {"ok": True})

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
        rows = db.query("SELECT id, title, display_color FROM history_categories ORDER BY sort_order")
        _json_response(self, 200, [{"id": r[0], "title": r[1], "color": r[2]} for r in rows])

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
                """INSERT INTO project_photos (opportunity_id, filename, file_path, file_size, mime_type, exif_data, thumbnail_path, uploaded_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (opp_id, filename, str(file_path.relative_to(ROOT)), file_size, mime_type,
                 json.dumps(exif_data) if exif_data else None,
                 str(thumb_path.relative_to(ROOT)) if thumb_path else None,
                 user["id"]),
            )
        except Exception as exc:
            # DB trigger may reject quota; clean up saved files
            try:
                file_path.unlink(missing_ok=True)
                if thumb_path:
                    thumb_path.unlink(missing_ok=True)
            except Exception:
                pass
            log_infra_event("error", f"Photo upload DB insert failed: {exc}")
            _json_response(self, 500, {"error": f"Upload failed: {exc}"})
            return
        _json_response(self, 201, {
            "id": photo_id, "filename": filename, "fileSize": file_size,
            "mimeType": mime_type, "exifData": exif_data,
            "url": f"/api/v2/photos/{photo_id}",
            "thumbnailUrl": f"/api/v2/photos/{photo_id}?thumbnail=1",
            "uploadedBy": user["id"], "uploadedAt": datetime.now(timezone.utc).isoformat(),
        })

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
                log_infra_event("error", f"Photo copy failed for {photo_id}: {exc}")
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
            for r in rows:
                docs.append({
                    "id": r[0], "title": r[1], "filePath": r[2],
                    "fileSize": r[3], "mimeType": r[4], "uploadedBy": r[5],
                    "uploadedAt": r[6].isoformat() if r[6] else None,
                    "companyScope": r[7],
                    "editUrl": f"/doc-editor.html?id={r[0]}",
                })
            _json_response(self, 200, {"documents": docs})
        except Exception as exc:
            logger.exception("projects/{id}/documents failed")
            log_infra_event("error", f"projects/{opp_id}/documents failed: {exc}")
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
        try:
            with open("/proc/1/cgroup", "r") as f:
                content = f.read()
                if "docker" in content or "kubepods" in content:
                    info["inDocker"] = True
        except Exception:
            pass
        if not info["inDocker"]:
            _json_response(self, 200, info)
            return
        try:
            hostname = socket.gethostname()
            info["containerName"] = hostname
            import subprocess
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}} {{.RestartCount}}", hostname],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                info["status"] = parts[0] if parts else "unknown"
                info["restartCount"] = int(parts[1]) if len(parts) > 1 else 0
            else:
                info["status"] = "inspect-failed"
        except Exception as exc:
            info["status"] = f"error: {exc}"
        _json_response(self, 200, info)

    def _handle_document_download(self, doc_id: int) -> None:
        # Authenticate either via session or a valid document-server JWT token
        qs = parse_qs(urlparse(self.path).query)
        token = (qs.get("token") or [""])[0]
        user = _require_auth(self) if not token else None
        if not user and not token:
            return
        if not user:
            if not DOCS_JWT_SECRET:
                self.send_error(401)
                return
            payload = _verify_jwt(token, DOCS_JWT_SECRET)
            if not payload or payload.get("id") != doc_id:
                self.send_error(401)
                return
        row = db.query(
            "SELECT opportunity_id, title, file_path, file_size, mime_type FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            self.send_error(404)
            return
        opp_id, title, file_path, file_size, mime_type = row
        full_path = ROOT / file_path
        if not full_path.exists():
            self.send_error(404)
            return
        data = full_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{title}"')
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
        # document.key must be a plain unique string (OnlyOffice 7.1+ requirement).
        # Use a separate JWT for the CRM download endpoint so auth stays stateless.
        ts = int(time.time())
        doc_key = f"sietch-doc-{doc_id}-{ts}"
        download_token = _sign_jwt({"id": doc_id, "path": file_path, "ts": ts}, DOCS_JWT_SECRET)
        # Public URL that Document Server will use to download the file
        download_url = f"{CRM_PUBLIC_URL}/api/v2/documents/{doc_id}?token={download_token}"
        editor_mode = "view" if doc_type == "pdf" else "edit"
        config = {
            "document": {
                "fileType": file_ext,
                "key": doc_key,
                "title": title,
                "url": download_url,
                "permissions": {"download": True, "edit": editor_mode == "edit"},
            },
            "documentType": doc_type,
            "editorConfig": {
                "callbackUrl": f"{CRM_PUBLIC_URL}/api/v2/documents/{doc_id}/callback",
                "mode": editor_mode,
                "user": {"id": str(user["id"]), "name": user.get("display_name") or user.get("email") or "User"},
            },
        }
        config["token"] = _sign_jwt(config, DOCS_JWT_SECRET)
        config["docsApiUrl"] = f"{DOCS_PUBLIC_URL}/web-apps/apps/api/documents/api.js"
        _json_response(self, 200, config)

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
            # List documents at this level
            if folder_id:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE uploaded_by = %s AND is_deleted = FALSE AND opportunity_id IS NULL AND company_scope = FALSE AND folder_id = %s
                       ORDER BY uploaded_at DESC""",
                    (user["id"], folder_id),
                )
            else:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE uploaded_by = %s AND is_deleted = FALSE AND opportunity_id IS NULL AND company_scope = FALSE AND folder_id IS NULL
                       ORDER BY uploaded_at DESC""",
                    (user["id"],),
                )
            docs = [self._doc_row(r) for r in rows]
            _json_response(self, 200, {"documents": docs, "folders": folders})
        except Exception as exc:
            logger.exception("documents/personal failed")
            log_infra_event("error", f"documents/personal failed: {exc}")
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
            # List documents at this level
            if folder_id:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE company_scope = TRUE AND is_deleted = FALSE AND folder_id = %s
                       ORDER BY uploaded_at DESC""",
                    (folder_id,),
                )
            else:
                rows = db.query(
                    """SELECT id, title, file_path, file_size, mime_type, uploaded_at, opportunity_id, company_scope, folder_id
                       FROM project_documents
                       WHERE company_scope = TRUE AND is_deleted = FALSE AND folder_id IS NULL
                       ORDER BY uploaded_at DESC""",
                )
            docs = [self._doc_row(r) for r in rows]
            _json_response(self, 200, {"documents": docs, "folders": folders})
        except Exception as exc:
            logger.exception("documents/company failed")
            log_infra_event("error", f"documents/company failed: {exc}")
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
            if q:
                if project_id:
                    rows = db.query(
                        """SELECT d.id, d.title, d.file_path, d.file_size, d.mime_type, d.uploaded_at,
                                  d.opportunity_id, d.company_scope, o.title AS opp_title
                           FROM project_documents d
                           JOIN opportunities o ON o.id = d.opportunity_id
                           WHERE d.opportunity_id = %s AND d.is_deleted = FALSE
                             AND LOWER(d.title) LIKE %s
                           ORDER BY d.uploaded_at DESC""",
                        (project_id, f"%{q}%"),
                    )
                else:
                    rows = db.query(
                        """SELECT d.id, d.title, d.file_path, d.file_size, d.mime_type, d.uploaded_at,
                                  d.opportunity_id, d.company_scope, o.title AS opp_title
                           FROM project_documents d
                           JOIN opportunities o ON o.id = d.opportunity_id
                           WHERE d.opportunity_id IS NOT NULL AND d.is_deleted = FALSE
                             AND LOWER(d.title) LIKE %s
                           ORDER BY o.title, d.uploaded_at DESC""",
                        (f"%{q}%",),
                    )
            else:
                if project_id:
                    rows = db.query(
                        """SELECT d.id, d.title, d.file_path, d.file_size, d.mime_type, d.uploaded_at,
                                  d.opportunity_id, d.company_scope, o.title AS opp_title
                           FROM project_documents d
                           JOIN opportunities o ON o.id = d.opportunity_id
                           WHERE d.opportunity_id = %s AND d.is_deleted = FALSE
                           ORDER BY d.uploaded_at DESC""",
                        (project_id,),
                    )
                else:
                    rows = db.query(
                        """SELECT d.id, d.title, d.file_path, d.file_size, d.mime_type, d.uploaded_at,
                                  d.opportunity_id, d.company_scope, o.title AS opp_title
                           FROM project_documents d
                           JOIN opportunities o ON o.id = d.opportunity_id
                           WHERE d.opportunity_id IS NOT NULL AND d.is_deleted = FALSE
                           ORDER BY o.title, d.uploaded_at DESC""",
                    )
            # Group by project
            grouped = {}
            for r in rows:
                opp_title = r[-1]
                if opp_title not in grouped:
                    grouped[opp_title] = {"project": opp_title, "projectId": r[7], "documents": []}
                grouped[opp_title]["documents"].append(self._doc_row(r[:-1]))
            _json_response(self, 200, {"results": list(grouped.values()), "total": len(rows)})
        except Exception as exc:
            logger.exception("documents/search failed")
            log_infra_event("error", f"documents/search failed: {exc}")
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
            log_infra_event("error", f"documents/folders failed: {exc}")
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
            log_infra_event("error", f"documents/folders/tree failed: {exc}")
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
            log_infra_event("error", f"documents/folders/{folder_id} delete failed: {exc}")
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
            "SELECT file_path FROM project_documents WHERE id = %s AND is_deleted = FALSE",
            (doc_id,), fetch="one",
        )
        if not row:
            _json_response(self, 404, {"error": "Document not found"})
            return
        file_path = row[0]
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
                        log_infra_event("info", f"Callback saved doc {doc_id}: {len(data)} bytes (status={status})")
                except Exception as exc:
                    log_infra_event("error", f"Callback download failed for doc {doc_id}: {exc}")
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
                """SELECT t.id, t.title, t.color FROM tag_definitions t
                   JOIN opportunity_tags ot ON t.id = ot.tag_id WHERE ot.opportunity_id = %s""",
                (opp_id,),
            )
            result[str(opp_id)] = [{"id": r[0], "title": r[1], "color": r[2]} for r in rows]
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
            if not employee and not contact_id:
                _json_response(self, 400, {"error": "contactId is required"})
                return
            result = generate_code("sietch", int(contact_id) if contact_id else None,
                                   contact_name, int(notes_category_id) if notes_category_id else None,
                                   nickname, employee=bool(employee))
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
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
        self.send_error(404)

    def _handle_bot_api_get(self) -> None:
        api_path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
            raw_chat_deals = (qs.get("chatId") or [""])[0]
            if raw_chat_deals:
                track_request("sietch", int(raw_chat_deals), "deals")
            is_employee = (qs.get("employee") or [""])[0].lower() == "true"
            raw_contact = (qs.get("contactId") or [""])[0]
            if not is_employee and not raw_contact:
                _json_response(self, 400, {"error": "contactId is required"})
                return
            contact_id = int(raw_contact) if raw_contact else None

            # Query PostgreSQL directly
            where = ["o.stage_type = 0"]
            params: list = []
            if contact_id:
                where.append("o.contact_id = %s")
                params.append(contact_id)
            search = (qs.get("search") or [""])[0].strip().lower()
            if search:
                where.append("o.title ILIKE %s")
                params.append(f"%{search}%")

            rows = db.query(
                f"""SELECT o.id, o.title, o.bid_value, o.description, o.created_at, o.expected_close_date,
                           s.title, c.first_name, c.last_name, u.display_name
                    FROM opportunities o
                    LEFT JOIN stages s ON o.stage_id = s.id
                    LEFT JOIN contacts c ON o.contact_id = c.id
                    LEFT JOIN users u ON o.responsible_user_id = u.id
                    WHERE {' AND '.join(where)} ORDER BY o.created_at DESC LIMIT 100""",
                (*params,),
            )
            deals = []
            for r in rows:
                deals.append({
                    "id": r[0], "title": r[1], "amount": float(r[2]) if r[2] else 0,
                    "stage": r[6] or "", "contact": f"{r[7] or ''} {r[8] or ''}".strip(),
                    "responsible": r[9] or "",
                })
            _json_response(self, 200, deals)
            return

        if api_path == "/api/bot/categories":
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

        self.send_error(404)

    def _handle_bot_api_post(self, method: str) -> None:
        api_path = urlparse(self.path).path
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
            opp_id = payload.get("opportunityId")
            content = str(payload.get("content") or "").strip()
            category_id = int(payload.get("categoryId") or 1)
            created_by = payload.get("createdBy")
            if not opp_id or not content:
                _json_response(self, 400, {"error": "opportunityId and content are required"})
                return
            event_id = db.insert_returning(
                """INSERT INTO history_events (opportunity_id, category_id, content, created_by)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (int(opp_id), category_id, content, int(created_by) if created_by else None),
            )
            _json_response(self, 200, {"ok": True, "eventId": event_id})
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
                bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                if not bot_token:
                    _json_response(self, 500, {"error": "TELEGRAM_BOT_TOKEN not configured"})
                    return
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                body: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
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

        if api_path == "/api/bot/notification-by-message" and method == "GET":
            if not bot_token or auth_header != f"Bearer {bot_token}":
                _json_response(self, 403, {"error": "Forbidden"})
                return
            chat_id = _qp.get("chatId", [""])[0]
            message_id = _qp.get("messageId", [""])[0]
            if not chat_id or not message_id:
                _json_response(self, 400, {"error": "chatId and messageId are required"})
                return
            try:
                row = db.query_one(
                    """SELECT n.id AS notification_id, n.opportunity_id, o.title AS project_title
                       FROM telegram_notification_log tnl
                       JOIN notifications n ON n.id = tnl.notification_id
                       LEFT JOIN opportunities o ON o.id = n.opportunity_id
                       WHERE tnl.chat_id = %s AND tnl.message_id = %s""",
                    (int(chat_id), int(message_id)),
                )
                if row:
                    _json_response(self, 200, {
                        "notificationId": row["notification_id"],
                        "opportunityId": row["opportunity_id"],
                        "projectTitle": row.get("project_title") or "Unknown Project",
                    })
                else:
                    _json_response(self, 404, {"error": "Notification not found"})
            except Exception:
                logger.exception("Failed to look up notification by message")
                _json_response(self, 500, {"error": "Internal error"})
            return

    # ── Presence (local store, ported from v2 — unchanged) ──────────────────

    def _handle_presence_users(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        # Return users from DB instead of CRM API
        rows = db.query(
            "SELECT id, email, display_name, first_name, last_name, is_admin FROM users WHERE is_active = TRUE"
        )
        people = [{"id": r[0], "email": r[1], "displayName": r[2] or f"{r[3] or ''} {r[4] or ''}".strip()} for r in rows]
        _json_response(self, 200, people)

    def _handle_presence_get(self) -> None:
        user = _require_auth(self)
        if not user:
            return
        portal = "sietch"
        overlays = get_portal_presence_snapshot(portal)
        clean_stale_presence_records(portal)
        now = datetime.now(timezone.utc)

        rows = db.query(
            "SELECT id, email, display_name, first_name, last_name FROM users WHERE is_active = TRUE"
        )
        out_users = []
        for r in rows:
            uid = str(r[0])
            ov = overlays.get(uid) or {}
            hb = _parse_iso_datetime(ov.get("lastHeartbeat") or "")
            online = bool(hb and (now - hb).total_seconds() < 600)
            afd = bool(hb and not online and (now - hb).total_seconds() < 10800)
            auto_status = ov.get("autoStatus") or ""
            if auto_status and not online:
                auto_status = ""

            out_users.append({
                "id": uid,
                "displayName": r[2] or f"{r[3] or ''} {r[4] or ''}".strip(),
                "email": r[1] or "",
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
        try:
            body = _read_body(self)
            if body:
                payload = json.loads(body)
                offline = bool(payload.get("offline"))
                visible = bool(payload.get("visible"))
        except (json.JSONDecodeError, ValueError):
            pass
        touch_heartbeat("sietch", str(user["id"]), offline=offline, visible=visible)
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
        set_last_read_dm("sietch", str(user["id"]), other, at or None)
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
        mark_messages_read("sietch", str(user["id"]), with_id)
        msgs, has_more = get_conversation("sietch", str(user["id"]), with_id)
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
            # Fetch tags for each message in a single batch query
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
                for r in rows:
                    r["tags"] = tags_by_msg.get(r["id"], [])
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
                if existing:
                    _json_response(self, 200, {"ok": True, "linked": False, "message": "Already linked"})
                    return
                _json_response(self, 500, {"error": "Failed to link email to deal"})
                return
            # Create history event so the link appears in the deal timeline.
            # Store a full JSON snapshot so the embedded mail view survives account
            # deletion / auto-delete retention purging.
            msg = db.query_one(
                "SELECT subject, from_addr, to_addr, cc_addr, body_html, body_text, date_received FROM mail_messages WHERE id = %s",
                (message_id,),
            )
            if msg:
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
                    (opp_id, EMAIL_HISTORY_CATEGORY_ID, json.dumps(snapshot), linked_by),
                )
            _json_response(self, 200, {"ok": True, "linked": True})
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
        try:
            payload = json.loads(_read_body(self) or b"{}")
            imap_uid = payload.get("imap_uid") or str(int(time.time() * 1000))
            result = db.query_one(
                """INSERT INTO mail_messages
                   (account_id, imap_uid, message_id, from_addr, to_addr, cc_addr,
                    subject, body_text, body_html, date_received, folder, is_read, is_flagged)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    payload.get("account_id"),
                    imap_uid,
                    payload.get("message_id"),
                    payload.get("from_addr"),
                    payload.get("to_addr"),
                    payload.get("cc_addr"),
                    payload.get("subject"),
                    payload.get("body_text"),
                    payload.get("body_html"),
                    payload.get("date_received"),
                    payload.get("folder", "INBOX"),
                    payload.get("is_read", False),
                    payload.get("is_flagged", False),
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
            rows = db.query_dicts(
                f"""SELECT id, email, display_name, imap_host, imap_port,
                          smtp_host, smtp_port, smtp_from_name, smtp_user,
                          sync_enabled, monitored_folders, oauth_provider,
                          oauth_token_expires, oauth_scopes, is_crm_mail,
                          auto_bcc_addr, auto_delete_days, tab_icon, tab_color,
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
        limit = int(qs.get("limit", ["200"])[0])
        _json_response(self, 200, {"entries": mail_scanner.get_scanner_log(limit) if mail_scanner else []})

    def _handle_mail_reprocess(self) -> None:
        try:
            payload = json.loads(_read_body(self) or b"{}")
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
                    auto_bcc_addr, auto_delete_days, tab_icon, tab_color)
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
                    int(payload.get("auto_delete_days") or 0),
                    payload.get("tab_icon") or "user",
                    payload.get("tab_color") or "var(--accent)",
                ),
            )
            _json_response(self, 201, {"id": result["id"]})
        except Exception as e:
            logger.exception("Failed to create mail account")
            _json_response(self, 500, {"error": str(e)})

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
            if "auto_delete_days" in payload:
                fields.append("auto_delete_days = %s")
                params.append(int(payload["auto_delete_days"] or 0))
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

            # Determine SMTP auth method: app password takes precedence over OAuth
            # (personal Outlook.com accounts don't support OAuth SMTP)
            oauth_provider = acct.get("oauth_provider")
            oauth_access_token = acct.get("oauth_access_token")
            smtp_password = acct.get("smtp_password_encrypted") or ""
            use_oauth_smtp = bool(oauth_provider and oauth_access_token and not smtp_password)
            if use_oauth_smtp:
                expires = acct.get("oauth_token_expires")
                if expires and isinstance(expires, datetime) and expires.timestamp() < datetime.now(timezone.utc).timestamp():
                    try:
                        import oauth_providers as op_mod
                        prov = op_mod.get_provider(oauth_provider)
                        if prov and acct.get("oauth_refresh_token"):
                            tok = prov.refresh_token(acct["oauth_refresh_token"])
                            oauth_access_token = tok.get("access_token", oauth_access_token)
                            new_expires = time.time() + tok.get("expires_in", 3600)
                            new_refresh = tok.get("refresh_token", acct["oauth_refresh_token"])
                            db.execute(
                                "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, oauth_token_expires = to_timestamp(%s) WHERE id = %s",
                                (oauth_access_token, new_refresh, new_expires, account_id),
                            )
                    except Exception:
                        logger.exception("Failed to refresh OAuth token for send")

            from smtp_client import send_email_from_account
            ok, err = send_email_from_account(
                smtp_host=acct["smtp_host"] or "",
                smtp_port=int(acct["smtp_port"] or 587),
                smtp_user=acct["smtp_user"] or acct["email"],
                smtp_password=smtp_password,
                from_name=acct["smtp_from_name"],
                from_addr=acct["email"],
                to_addr=to_addr,
                subject=subject,
                html_body=body_html,
                text_body=body_text,
                cc_addr=cc_addr,
                bcc_addr=bcc_addr,
                use_tls=bool(acct["smtp_use_tls"]),
                attachments=decoded_attachments if decoded_attachments else None,
                oauth_provider=oauth_provider if use_oauth_smtp else None,
                oauth_access_token=oauth_access_token if use_oauth_smtp else None,
            )

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
                from imap_tools import MailBox
                mailbox = MailBox(acct["imap_host"], port=acct["imap_port"])
                mailbox.login(acct["email"], acct["password_encrypted"])
                mailbox.folder.set(msg["folder"] or "INBOX")
                uid = str(msg["imap_uid"])
                msgs = list(mailbox.fetch(uid=uid, mark_seen=False, bulk=False))
                if not msgs:
                    _json_response(self, 404, {"error": "Message not found on IMAP server"})
                    mailbox.logout()
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
                mailbox.logout()
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
            except Exception as imap_e:
                logger.exception("IMAP fetch failed for attachment %d", attachment_id)
                _json_response(self, 502, {"error": f"IMAP fetch failed: {imap_e}"})
        except Exception as e:
            logger.exception("Failed to download attachment %d", attachment_id)
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

def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), KanbanHandler)
    print(f"Sietch CRM v3.0 starting on port {PORT}")
    for u in _lan_urls(PORT):
        print(f"Open: {u}")
    print(f"Version: {APP_VERSION}")
    dispatcher_stop = start_dispatcher()
    scanner_stop = mail_scanner.start_scanner() if mail_scanner else None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_dispatcher(dispatcher_stop)
        server.shutdown()


if __name__ == "__main__":
    main()
