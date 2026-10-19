#!/usr/bin/env python3
"""One-time migration from OnlyOffice CRM to PostgreSQL (Sietch CRM).

Run after init.sql + migrations 024/025 have been applied.

For --export-only (recommended for round-trip tests): no psycopg2 required.
For full migration (writes to DB): requires psycopg2 (pip install psycopg2-binary)

Usage:
    python migrate_from_onlyoffice.py --portal-url https://office.publicadjustermidwest.com --email admin@example.com --password secret
    python migrate_from_onlyoffice.py --portal-url ... --email ... --password ... --export-only --export-dir ./crm-export
    python migrate_from_onlyoffice.py --portal-url ... --email ... --password ... --skip-attachment-download
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# psycopg2 is only required for non-export runs.
psycopg2 = None


# ── Configuration ──────────────────────────────────────────────────────────────

RATE_LIMIT_DELAY = 0.05       # 20 req/sec
SUB_REQUEST_DELAY = 0.05      # sub-requests between opp history/tags/CF
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
HISTORY_ATTACHMENT_DIR = Path(__file__).resolve().parent / "data" / "history-attachments"
CONTACT_AVATAR_DIR = Path(__file__).resolve().parent / "data" / "contact-avatars"
MIGRATION_REPORT = Path(__file__).resolve().parent / "migration_report.txt"
OO_CLOSURE_SENTINEL = "0001-01-01T00:00:00"


MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "csv": "text/csv",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "webp": "image/webp",
    "esx": "application/octet-stream",
}


def _guess_mime_type(ext: str) -> str:
    return MIME_TYPES.get(ext.lower(), "application/octet-stream")


# OnlyOffice fieldType is NUMERIC. Map every value we've seen.
_OO_FIELD_TYPE_MAP = {
    "0": "text", "1": "textarea", "2": "number", "3": "url",
    "4": "select", "5": "date", "6": "checkbox", "7": "currency",
    "8": "text",
}


def _normalize_field_type(raw: Any) -> str:
    if raw is None:
        return "text"
    return _OO_FIELD_TYPE_MAP.get(str(raw).strip().lower(), "text")


def _is_closure_sentinel(raw: Any) -> bool:
    s = str(raw or "").strip()
    return s.startswith(OO_CLOSURE_SENTINEL)


def _safe_filename(name: str) -> str:
    out = re.sub(r"[^\w.\-]+", "_", name or "").strip("._")
    return out or "file"


# Map CRM user IDs to new v3 user IDs
_crm_id_map: dict[str, dict[str, str]] = {
    "users": {},            # crm_user_id -> v3 user.id (as str to keep UUIDs)
    "contacts": {},         # crm_contact_id (str) -> v3 contact.id
    "opportunities": {},    # crm_opp_id (str) -> v3 opportunity.id
    "stages": {},
    "tags": {},
    "custom_fields": {},
    "history_categories": {},
}


# ── SSL / HTTP ─────────────────────────────────────────────────────────────────

def _ssl_context():
    verify = os.environ.get("ONLYOFFICE_SSL_VERIFY", "true").lower() not in ("0", "false", "no")
    if verify:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _crm_get(portal: str, token: str, path: str, retries: int = MAX_RETRIES) -> Any:
    """GET from the CRM API with rate limiting and retry."""
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "Authorization": token},
                method="GET",
            )
            with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                time.sleep(RATE_LIMIT_DELAY)
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"  [retry {attempt+1}] HTTP {exc.code} on {path}, waiting {delay}s...")
                time.sleep(delay)
                continue
            raise
    return None


def _crm_get_raw(portal: str, token: str, path: str, retries: int = MAX_RETRIES) -> bytes | None:
    """GET raw bytes (for file downloads) with retry."""
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json, */*", "Authorization": token},
                method="GET",
            )
            with urllib.request.urlopen(req, context=_ssl_context(), timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"  [retry {attempt+1}] HTTP {exc.code} on {path}, waiting {delay}s...")
                time.sleep(delay)
                continue
            print(f"  [warn] Failed to download {path}: HTTP {exc.code}")
            return None
        except Exception as exc:
            if attempt < retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"  [retry {attempt+1}] download error on {path}: {exc}, waiting {delay}s...")
                time.sleep(delay)
                continue
            print(f"  [warn] Failed to download {path}: {exc}")
            return None
    return None


def _crm_get_json_body(portal: str, token: str, path: str) -> Any:
    """GET a body that may be JSON or HTML (filehandler.ashx)."""
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json, text/html, */*",
                "Authorization": token,
            })
            with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", errors="replace")
            if "application/json" in ctype or raw.lstrip().startswith(("{", "[")):
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return None
            return raw
        except Exception:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    return None


def _unwrap(data: Any) -> Any:
    if isinstance(data, dict):
        if "response" in data:
            return data["response"]
        if "result" in data:
            return data["result"]
    return data


def _paginate(portal: str, token: str, base_path: str, param_name: str = "count", page_size: int = 500) -> list:
    items = []
    start = 0
    while True:
        sep = "&" if "?" in base_path else "?"
        path = f"{base_path}{sep}{param_name}={page_size}&startIndex={start}"
        data = _unwrap(_crm_get(portal, token, path))
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if len(data) < page_size:
            break
        start += page_size
    return items


# ── Migration Steps ────────────────────────────────────────────────────────────

def _migrate_users(conn, portal: str, token: str) -> int:
    print("\n[Step 1] Migrating users...")
    data = _unwrap(_crm_get(portal, token, "/api/2.0/people"))
    users = data if isinstance(data, list) else []
    count = 0
    with conn.cursor() as cur:
        for u in users:
            if not isinstance(u, dict):
                continue
            email = str(u.get("email") or u.get("Email") or "").strip().lower()
            if not email:
                continue
            display_name = str(u.get("displayName") or u.get("DisplayName") or "").strip()
            first_name = str(u.get("firstName") or u.get("FirstName") or "").strip()
            last_name = str(u.get("lastName") or u.get("LastName") or "").strip()
            is_admin = bool(u.get("isAdmin") or u.get("IsAdmin"))
            crm_id = str(u.get("id") or u.get("ID") or "")

            cur.execute(
                """INSERT INTO users (email, external_user_id, display_name, first_name, last_name, is_admin, role, must_change_password)
                   VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN 'admin' ELSE 'member' END, TRUE)
                   ON CONFLICT (email) DO UPDATE SET
                       external_user_id = EXCLUDED.external_user_id,
                       display_name = EXCLUDED.display_name,
                       first_name = EXCLUDED.first_name,
                       last_name = EXCLUDED.last_name,
                       is_admin = EXCLUDED.is_admin,
                       role = CASE WHEN EXCLUDED.is_admin THEN 'admin' ELSE 'member' END
                   RETURNING id""",
                (email, crm_id, display_name, first_name, last_name, is_admin, is_admin),
            )
            v3_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["users"][crm_id] = v3_id
            count += 1
        conn.commit()
    print(f"  → {count} users imported")
    return count


def _migrate_stages(conn, portal: str, token: str) -> int:
    print("\n[Step 2] Migrating stages...")
    data = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/stage"))
    stages = data if isinstance(data, list) else []
    count = 0
    with conn.cursor() as cur:
        for s in stages:
            if not isinstance(s, dict):
                continue
            crm_id = str(s.get("id") or s.get("ID") or "")
            title = str(s.get("title") or s.get("Title") or "").strip()
            if not title:
                continue
            color = str(s.get("color") or s.get("Color") or "#999999").strip()
            sort_order = int(s.get("sortOrder") or s.get("SortOrder") or 0)
            stage_type = int(s.get("stageType") or s.get("StageType") or 0)
            probability = int(s.get("probability") or s.get("Probability") or
                              s.get("successProbability") or 0)

            cur.execute(
                """INSERT INTO stages (title, color, sort_order, stage_type, probability)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (title) DO UPDATE SET
                       color = EXCLUDED.color,
                       sort_order = EXCLUDED.sort_order,
                       stage_type = EXCLUDED.stage_type,
                       probability = EXCLUDED.probability
                   RETURNING id""",
                (title, color, sort_order, stage_type, probability),
            )
            v3_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["stages"][crm_id] = v3_id
            count += 1
        conn.commit()
    print(f"  → {count} stages imported")
    return count


def _migrate_tags(conn, portal: str, token: str) -> int:
    print("\n[Step 3] Migrating tags...")
    data = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/tag"))
    tags = data if isinstance(data, list) else []
    count = 0
    with conn.cursor() as cur:
        for t in tags:
            if not isinstance(t, dict):
                continue
            crm_id = str(t.get("id") or t.get("ID") or "")
            title = str(t.get("title") or t.get("name") or t.get("Title") or t.get("Name") or "").strip()
            if not title:
                continue
            color = str(t.get("color") or t.get("Color") or "#6c757d").strip() or None

            cur.execute(
                """INSERT INTO tag_definitions (title, color)
                   VALUES (%s, %s)
                   ON CONFLICT (title) DO UPDATE SET color = EXCLUDED.color
                   RETURNING id""",
                (title, color),
            )
            v3_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["tags"][crm_id] = v3_id
            count += 1
        conn.commit()
    print(f"  → {count} tags imported")
    return count


def _migrate_custom_fields(conn, portal: str, token: str) -> int:
    print("\n[Step 4] Migrating custom field definitions...")
    data = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/customfield/definitions"))
    fields = data if isinstance(data, list) else []
    count = 0
    with conn.cursor() as cur:
        for f in fields:
            if not isinstance(f, dict):
                continue
            crm_id = str(f.get("id") or f.get("ID") or "")
            label = str(f.get("label") or f.get("Label") or f.get("name") or f.get("Name") or "").strip()
            if not label:
                continue
            # Convention: field_key = "field_<crm_id>" (so live handlers can map)
            field_key = f"field_{crm_id}" if crm_id else f"field_{label.lower().replace(' ', '_')}"
            mapped_type = _normalize_field_type(f.get("type") or f.get("Type") or f.get("fieldType"))
            is_required = bool(f.get("isRequired") or f.get("IsRequired"))
            sort_order = int(f.get("sortOrder") or f.get("position") or 0)

            cur.execute(
                """INSERT INTO custom_field_definitions (field_key, label, field_type, is_required, sort_order)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (field_key) DO UPDATE SET
                       label = EXCLUDED.label,
                       field_type = EXCLUDED.field_type,
                       is_required = EXCLUDED.is_required,
                       sort_order = EXCLUDED.sort_order
                   RETURNING id""",
                (field_key, label, mapped_type, is_required, sort_order),
            )
            v3_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["custom_fields"][crm_id] = v3_id

            # Pull options for select-type fields
            if mapped_type == "select":
                try:
                    opts_data = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/opportunity/customfield/{crm_id}/options"))
                    opts = opts_data if isinstance(opts_data, list) else []
                    for idx, opt in enumerate(opts):
                        if isinstance(opt, dict):
                            ov = str(opt.get("value") or opt.get("Value") or opt.get("displayText") or "").strip()
                            ol = str(opt.get("displayText") or opt.get("DisplayText") or ov).strip()
                            if ov:
                                cur.execute(
                                    """INSERT INTO custom_field_options (field_id, option_value, option_label, sort_order)
                                       VALUES (%s, %s, %s, %s)""",
                                    (v3_id, ov, ol, idx),
                                )
                except Exception as exc:
                    print(f"  [warn] CF options for {crm_id}: {exc}")
            count += 1
        conn.commit()
    print(f"  → {count} custom field definitions imported")
    return count


def _migrate_contacts(conn, portal: str, token: str, download_avatars: bool = True) -> int:
    print("\n[Step 5] Migrating contacts...")
    crm_contacts = _paginate(portal, token, "/api/2.0/crm/contact/filter?count=500")
    count = 0
    with conn.cursor() as cur:
        for c in crm_contacts:
            if not isinstance(c, dict):
                continue
            crm_id = str(c.get("id") or c.get("ID") or "")
            if not crm_id:
                continue

            # Skip if already imported
            cur.execute("SELECT id FROM contacts WHERE onlyoffice_id = %s", (crm_id,))
            if cur.fetchone():
                continue

            first_name = str(c.get("firstName") or c.get("FirstName") or "").strip() or None
            last_name = str(c.get("lastName") or c.get("LastName") or "").strip() or None
            company_name = str(c.get("companyName") or "").strip() or None
            is_company = bool(c.get("isCompany", False))
            about = str(c.get("about") or "").strip() or None
            industry = str(c.get("industry") or "").strip() or None

            contact_status = None
            cs = c.get("contactStatus")
            if isinstance(cs, dict):
                contact_status = str(cs.get("title") or "").strip() or None
            status_id = cs.get("id") if isinstance(cs, dict) else None

            task_count = int(c.get("taskCount") or 0) if c.get("taskCount") is not None else None

            # commonData
            common_data = c.get("commonData") or []
            primary_email = None
            primary_phone = None
            website = None
            for item in common_data:
                if not isinstance(item, dict):
                    continue
                info_type = item.get("infoType")
                data = str(item.get("data") or "").strip()
                is_primary = bool(item.get("isPrimary"))
                if info_type == 1 and is_primary and not primary_email:
                    primary_email = data or None
                if info_type == 0 and is_primary and not primary_phone:
                    primary_phone = data or None
                if info_type == 2 and not website:
                    website = data or None
            if not primary_email:
                for item in common_data:
                    if isinstance(item, dict) and item.get("infoType") == 1:
                        primary_email = str(item.get("data") or "").strip() or None
                        break
            if not primary_phone:
                for item in common_data:
                    if isinstance(item, dict) and item.get("infoType") == 0:
                        primary_phone = str(item.get("data") or "").strip() or None
                        break

            # addresses
            addresses = c.get("addresses") or []
            primary_addr = None
            for a in addresses:
                if isinstance(a, dict) and a.get("isPrimary"):
                    primary_addr = a
                    break
            if not primary_addr and addresses and isinstance(addresses[0], dict):
                primary_addr = addresses[0]

            # Avatar download
            avatar_remote = str(c.get("smallFotoUrl") or "").strip() or None
            avatar_local = None
            if download_avatars and avatar_remote:
                # Build full URL — only OO likes relative paths
                if not avatar_remote.startswith("http"):
                    full_url = f"{portal.rstrip('/')}{avatar_remote}"
                else:
                    full_url = avatar_remote
                raw = _crm_get_raw(portal, token, avatar_remote)
                if raw:
                    ext = "jpg"
                    if "png" in full_url.lower():
                        ext = "png"
                    avatar_local_path = CONTACT_AVATAR_DIR / f"{crm_id}.{ext}"
                    avatar_local_path.parent.mkdir(parents=True, exist_ok=True)
                    avatar_local_path.write_bytes(raw)
                    avatar_local = str(avatar_local_path.relative_to(Path(__file__).resolve().parent))

            # Creator
            create_by = c.get("createBy") or {}
            creator_id = _crm_id_map["users"].get(str(create_by.get("id") or "")) if isinstance(create_by, dict) else None

            cur.execute(
                """INSERT INTO contacts (
                       first_name, last_name, company, email, phone, is_company,
                       website, industry, about, contact_status, avatar_url,
                       onlyoffice_id, address_street, address_city, address_state, address_zip,
                       address_country, created_by,
                       onlyoffice_task_count, onlyoffice_status_id,
                       raw_about, raw_industry, avatar_local_path)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING
                   RETURNING id""",
                (first_name, last_name, company_name, primary_email, primary_phone, is_company,
                 website, industry, about, contact_status,
                 # If avatar_local_path is set, prefer it (clear avatar_url so UI never
                 # accidentally renders the broken remote URL).
                 None if avatar_local else avatar_remote,
                 crm_id,
                 str(primary_addr.get("street") or "").strip() or None if primary_addr else None,
                 str(primary_addr.get("city") or "").strip() or None if primary_addr else None,
                 str(primary_addr.get("state") or "").strip() or None if primary_addr else None,
                 str(primary_addr.get("zip") or "").strip() or None if primary_addr else None,
                 str(primary_addr.get("country") or "").strip() or None if primary_addr else None,
                 creator_id,
                 task_count,
                 status_id,
                 about, industry, avatar_local),
            )
            row = cur.fetchone()
            if not row:
                # Already imported (cross-run dedupe). Look up existing id.
                cur.execute("SELECT id FROM contacts WHERE onlyoffice_id = %s", (crm_id,))
                erow = cur.fetchone()
                if not erow:
                    continue
                local_id = erow[0]
            else:
                local_id = row[0]
            _crm_id_map["contacts"][crm_id] = local_id
            count += 1

            # Multi-value commonData entries
            for it in common_data:
                if not isinstance(it, dict):
                    continue
                data = str(it.get("data") or "").strip()
                if not data:
                    continue
                info_type = it.get("infoType")
                is_primary = bool(it.get("isPrimary"))
                cat_id = it.get("category")
                cat_name = str(it.get("categoryName") or "")
                oo_id = int(it.get("id") or 0) or None
                if info_type == 1:
                    cur.execute(
                        """INSERT INTO contact_emails (contact_id, email, category, category_id, is_primary, onlyoffice_id)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT DO NOTHING""",
                        (local_id, data, cat_name or None, cat_id, is_primary, oo_id),
                    )
                elif info_type == 0:
                    cur.execute(
                        """INSERT INTO contact_phones (contact_id, phone, category, category_id, is_primary, onlyoffice_id)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT DO NOTHING""",
                        (local_id, data, cat_name or None, cat_id, is_primary, oo_id),
                    )

            # Multi-value addresses
            for a in addresses:
                if not isinstance(a, dict):
                    continue
                cur.execute(
                    """INSERT INTO contact_addresses
                           (contact_id, street, city, state, zip, country, category, category_id, is_primary, onlyoffice_id)
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
        conn.commit()

        # Second pass: link persons to companies
        linked = 0
        for c in crm_contacts:
            if not isinstance(c, dict) or c.get("isCompany"):
                continue
            crm_id = str(c.get("id") or "")
            local_id = _crm_id_map["contacts"].get(crm_id)
            if not local_id:
                continue
            company_obj = c.get("company")
            if isinstance(company_obj, dict):
                comp_oo_id = str(company_obj.get("id") or "")
                comp_local_id = _crm_id_map["contacts"].get(comp_oo_id)
                if comp_local_id:
                    cur.execute(
                        "UPDATE contacts SET company_id = %s, company = COALESCE(company, %s) WHERE id = %s",
                        (comp_local_id, str(company_obj.get("displayName") or "").strip() or None, local_id),
                    )
                    linked += 1
        conn.commit()

    print(f"  → {count} contacts imported ({linked} person→company links)")
    return count


def _migrate_opportunities(
    conn, portal: str, token: str, download_attachments: bool = True,
) -> tuple[int, int, int]:
    """Returns (opps, history, attachments)."""
    print("\n[Step 6] Migrating opportunities (all stages)...")
    crm_opps = _paginate(portal, token, "/api/2.0/crm/opportunity/filter?count=500")

    opp_count = 0
    history_count = 0
    attachment_count = 0

    with conn.cursor() as cur:
        for idx, o in enumerate(crm_opps):
            if not isinstance(o, dict):
                continue
            crm_id = str(o.get("id") or o.get("ID") or "")
            if not crm_id:
                continue

            # Skip if already imported by onlyoffice_id
            cur.execute("SELECT id FROM opportunities WHERE onlyoffice_id = %s", (crm_id,))
            existing = cur.fetchone()
            if existing:
                v3_opp_id = existing[0]
                opp_count += 1
            else:
                title = str(o.get("title") or o.get("Title") or "Untitled").strip()

                # Stage
                stage_data = o.get("stage") or o.get("Stage") or {}
                crm_stage_id = str(stage_data.get("id") or stage_data.get("ID") or "")
                v3_stage_id = _crm_id_map["stages"].get(crm_stage_id)
                stage_type = int(stage_data.get("stageType") or 0) if isinstance(stage_data, dict) else 0

                # Contact
                contact_data = o.get("contact") or o.get("Contact") or {}
                crm_contact_id = str(contact_data.get("id") or contact_data.get("ID") or "") if isinstance(contact_data, dict) else ""
                v3_contact_id = _crm_id_map["contacts"].get(crm_contact_id)

                # Responsible
                responsible_data = o.get("responsible") or o.get("Responsible") or {}
                crm_responsible_id = str(responsible_data.get("id") or responsible_data.get("ID") or "") if isinstance(responsible_data, dict) else ""
                v3_responsible_id = _crm_id_map["users"].get(crm_responsible_id)

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

                # Due date
                due_date = None
                for dd_key in ("expectedCloseDate", "ExpectedCloseDate"):
                    raw = o.get(dd_key)
                    if raw:
                        try:
                            due_date = str(raw)[:10]
                        except Exception:
                            due_date = None
                        if due_date:
                            break

                # Actual close date
                actual_close_date = None
                actual_close_value = None
                acd = o.get("actualCloseDate")
                if acd and not _is_closure_sentinel(acd):
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

                description = str(o.get("description") or o.get("Description") or "").strip() or None

                created_raw = o.get("created")
                created_at = None
                if created_raw:
                    try:
                        created_at = str(created_raw)[:19]
                    except Exception:
                        created_at = None

                cur.execute(
                    """INSERT INTO opportunities
                           (title, description, stage_id, stage_type, bid_value,
                            expected_close_date, actual_close_date, actual_close_value,
                            contact_id, responsible_user_id, created_at, onlyoffice_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),%s)
                       ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO UPDATE SET
                           title = EXCLUDED.title,
                           contact_id = COALESCE(EXCLUDED.contact_id, opportunities.contact_id)
                       RETURNING id""",
                    (title, description, v3_stage_id, stage_type, bid_value,
                     due_date, actual_close_date, actual_close_value,
                     v3_contact_id, v3_responsible_id, created_at, crm_id),
                )
                v3_opp_id = cur.fetchone()[0]
                opp_count += 1

            _crm_id_map["opportunities"][crm_id] = v3_opp_id

            # ── Tags ──
            time.sleep(SUB_REQUEST_DELAY)
            cur.execute("SAVEPOINT sp_opp_tags")
            try:
                tags_data = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/opportunity/tag/{crm_id}"))
                tag_list = tags_data if isinstance(tags_data, list) else []
                for t in tag_list:
                    tag_title = ""
                    if isinstance(t, dict):
                        tag_title = str(t.get("title") or t.get("name") or t.get("Title") or t.get("Name") or "").strip()
                    elif isinstance(t, str):
                        tag_title = t.strip()
                    if tag_title:
                        cur.execute("SELECT id FROM tag_definitions WHERE title = %s", (tag_title,))
                        tag_row = cur.fetchone()
                        if tag_row:
                            cur.execute(
                                "INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                                (v3_opp_id, tag_row[0]),
                            )
                cur.execute("RELEASE SAVEPOINT sp_opp_tags")
            except Exception as exc:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_opp_tags")
                    cur.execute("RELEASE SAVEPOINT sp_opp_tags")
                except Exception:
                    pass
                print(f"  [warn] Tags for opp {crm_id}: {exc}")

            # ── Custom field values (try /customfield endpoint, fall back to embedded) ──
            time.sleep(SUB_REQUEST_DELAY)
            cur.execute("SAVEPOINT sp_opp_cf")
            try:
                cf_data = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/opportunity/{crm_id}/customfield"))
                cf_list = cf_data if isinstance(cf_data, list) else []
                for cf in cf_list:
                    if not isinstance(cf, dict):
                        continue
                    cf_crm_id = str(cf.get("id") or cf.get("ID") or "")
                    cf_value = cf.get("value") or cf.get("Value") or cf.get("fieldValue") or cf.get("FieldValue") or ""
                    if isinstance(cf_value, dict):
                        cf_value = cf_value.get("title") or cf_value.get("text") or cf_value.get("value") or ""
                    cf_value = str(cf_value).strip()
                    v3_field_id = _crm_id_map["custom_fields"].get(cf_crm_id)
                    if v3_field_id and cf_value:
                        cur.execute(
                            """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                               VALUES (%s, %s, %s)
                               ON CONFLICT (opportunity_id, field_id) DO UPDATE SET field_value = EXCLUDED.field_value""",
                            (v3_opp_id, v3_field_id, cf_value),
                        )
                cur.execute("RELEASE SAVEPOINT sp_opp_cf")
            except Exception as exc:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_opp_cf")
                    cur.execute("RELEASE SAVEPOINT sp_opp_cf")
                except Exception:
                    pass
                print(f"  [warn] Custom fields for opp {crm_id}: {exc}")

            # ── History events + attachments ──
            time.sleep(SUB_REQUEST_DELAY)
            cur.execute("SAVEPOINT sp_opp_hist")
            try:
                hist_data = _unwrap(_crm_get(portal, token,
                    f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={crm_id}&count=500"))
                hist_list = hist_data if isinstance(hist_data, list) else []
                for h in hist_list:
                    if not isinstance(h, dict):
                        continue
                    # Category
                    cat_data = h.get("category") or h.get("Category") or {}
                    cat_title = str(cat_data.get("title") or cat_data.get("Title") or "Note").strip() if isinstance(cat_data, dict) else "Note"
                    cur.execute("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
                    cat_row = cur.fetchone()
                    if cat_row:
                        cat_id = cat_row[0]
                    else:
                        cur.execute(
                            "INSERT INTO history_categories (title, is_system) VALUES (%s, TRUE) RETURNING id",
                            (cat_title,),
                        )
                        cat_id = cur.fetchone()[0]

                    raw_content = h.get("content") or h.get("Content") or ""
                    event_content = str(raw_content).strip() or None if isinstance(raw_content, str) else None

                    event_created = h.get("created") or h.get("Created") or h.get("date") or None

                    # Author
                    create_by = h.get("createBy") or h.get("CreateBy") or h.get("createdBy") or {}
                    crm_author_id = str(create_by.get("id") or create_by.get("ID") or "") if isinstance(create_by, dict) else ""
                    v3_author_id = _crm_id_map["users"].get(crm_author_id)

                    crm_event_id = int(h.get("id") or 0) or None

                    cur.execute(
                        """INSERT INTO history_events
                               (opportunity_id, category_id, content, created_by, created_at, backdated_created_at,
                                crm_event_id, crm_entity_id)
                           VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s)
                           ON CONFLICT DO NOTHING
                           RETURNING id""",
                        (v3_opp_id, cat_id, event_content, v3_author_id, event_created, event_created,
                         crm_event_id, int(crm_id) if crm_id.isdigit() else None),
                    )
                    row = cur.fetchone()
                    if not row:
                        continue
                    event_id = row[0]
                    history_count += 1

                    # Notify users from this event
                    notify_list = h.get("notifyUserList") or h.get("NotifyUserList") or []
                    if isinstance(notify_list, list):
                        for nu in notify_list:
                            if isinstance(nu, dict):
                                nu_crm_id = str(nu.get("id") or nu.get("ID") or "")
                            elif isinstance(nu, str):
                                nu_crm_id = nu
                            else:
                                continue
                            v3_nu_id = _crm_id_map["users"].get(nu_crm_id)
                            if v3_nu_id:
                                cur.execute(
                                    "INSERT INTO history_notify_users (event_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                    (event_id, v3_nu_id),
                                )

                    # Attachments — DOWNLOAD files
                    files = h.get("files") or []
                    if not isinstance(files, list):
                        files = []
                    for f_item in files:
                        if not isinstance(f_item, dict):
                            continue
                        file_id = int(f_item.get("id") or 0)
                        if not file_id:
                            continue
                        file_name = str(f_item.get("title") or f"file_{file_id}").strip()
                        file_size = int(f_item.get("pureContentLength") or f_item.get("size") or 0)
                        file_ext = Path(file_name).suffix.lstrip(".").lower()
                        mime_type = _guess_mime_type(file_ext)
                        view_url = f_item.get("viewUrl") or f_item.get("webUrl") or ""

                        # Owner
                        owner = f_item.get("createdBy") or f_item.get("CreatedBy") or f_item.get("owner") or f_item.get("Owner") or {}
                        owner_crm_id = str(owner.get("id") or owner.get("ID") or "") if isinstance(owner, dict) else ""
                        v3_uploader_id = _crm_id_map["users"].get(owner_crm_id) or v3_author_id

                        # Download
                        rel_dir = Path("data") / "history-attachments" / str(v3_opp_id) / str(event_id)
                        safe_name = _safe_filename(file_name)
                        # Use crm_file_id to guarantee unique filenames within an event
                        unique_name = f"{file_id}_{safe_name}"
                        file_path = Path(__file__).resolve().parent / rel_dir / unique_name
                        file_path.parent.mkdir(parents=True, exist_ok=True)

                        actual_size = file_size
                        if download_attachments:
                            raw = _crm_get_raw(portal, token, f"/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={file_id}")
                            if raw:
                                file_path.write_bytes(raw)
                                actual_size = len(raw)

                        cur.execute(
                            """INSERT INTO history_attachments
                                   (event_id, filename, file_path, file_size, mime_type, uploaded_by,
                                    original_url, crm_file_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (event_id, crm_file_id) WHERE crm_file_id IS NOT NULL DO NOTHING""",
                            (event_id, file_name, str(rel_dir / unique_name), actual_size, mime_type,
                             v3_uploader_id, view_url, file_id),
                        )
                        # rowcount is 0 on conflict (same file already recorded for this event);
                        # same file on a DIFFERENT event is a separate row (allowed by design).
                        if cur.rowcount:
                            attachment_count += 1
                cur.execute("RELEASE SAVEPOINT sp_opp_hist")
            except Exception as exc:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_opp_hist")
                    cur.execute("RELEASE SAVEPOINT sp_opp_hist")
                except Exception:
                    pass
                print(f"  [warn] History for opp {crm_id}: {exc}")

            if (opp_count) % 50 == 0:
                conn.commit()
                print(f"  ... {opp_count}/{len(crm_opps)} opportunities processed")

        conn.commit()
    print(f"  → {opp_count} opportunities, {history_count} history events, {attachment_count} attachments")
    return opp_count, history_count, attachment_count


def _migrate_tasks(conn, portal: str, token: str) -> int:
    print("\n[Step 7] Migrating tasks...")
    open_tasks = _paginate(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=false")
    closed_tasks = _paginate(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=true")
    all_tasks = open_tasks + closed_tasks

    count = 0
    with conn.cursor() as cur:
        for t in all_tasks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or t.get("Title") or "").strip()
            if not title:
                continue
            description = str(t.get("description") or t.get("Description") or "").strip() or None
            is_closed = bool(t.get("isClosed") or t.get("IsClosed") or t.get("closed") or False)

            crm_task_id = str(t.get("id") or t.get("ID") or "")
            if crm_task_id:
                cur.execute("SELECT id FROM tasks WHERE onlyoffice_id = %s", (crm_task_id,))
                if cur.fetchone():
                    continue

            # Link via opportunity entity OR top-level opportunityId
            opp_data = t.get("opportunity") or {}
            crm_opp_id = ""
            if isinstance(opp_data, dict):
                crm_opp_id = str(opp_data.get("id") or "")
            if not crm_opp_id:
                ent = t.get("entity") or {}
                if isinstance(ent, dict) and ent.get("entityType") == "opportunity":
                    crm_opp_id = str(ent.get("entityId") or "")
            v3_opp_id = _crm_id_map["opportunities"].get(crm_opp_id)

            # Responsible
            resp_data = t.get("responsible") or t.get("Responsible") or {}
            crm_resp_id = str(resp_data.get("id") or resp_data.get("ID") or "") if isinstance(resp_data, dict) else ""
            v3_resp_id = _crm_id_map["users"].get(crm_resp_id)

            due_date = t.get("dueDate") or t.get("DueDate") or None
            priority = int(t.get("priority") or t.get("Priority") or 0)

            closed_at = None
            if is_closed:
                closed_at = t.get("closedDate") or t.get("ClosedDate") or None

            cur.execute(
                """INSERT INTO tasks
                       (title, description, opportunity_id, responsible_user_id,
                        due_date, priority, is_closed, closed_at, created_at, onlyoffice_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                   ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING""",
                (title, description, v3_opp_id, v3_resp_id, due_date, priority, is_closed, closed_at,
                 crm_task_id or None),
            )
            count += 1
        conn.commit()
    print(f"  → {count} tasks imported")
    return count


def _migrate_user_profiles(_conn) -> int:
    """Placeholder — JSON profile remap is done by migrate_dashboard_data.py."""
    print("\n[Step 9] User profile remap (skipped — use migrate_dashboard_data.py)")
    return 0


# ── Export-only (Phase 2) ──────────────────────────────────────────────────────

def _write_json(out_dir: Path, name: str, data: Any) -> None:
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    n = len(data) if isinstance(data, (list, dict)) else 0
    print(f"  wrote {name}.json ({n} items)")


def _do_export_only(portal: str, token: str, out_dir: Path) -> None:
    print("Exporting users...")
    try:
        users = _paginate(portal, token, "/api/2.0/people/filter?count=500")
        _write_json(out_dir, "users", users)
    except Exception as e:
        print(f"  [warn] users: {e}")

    print("Exporting stages...")
    try:
        stages = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/stage"))
        _write_json(out_dir, "stages", stages)
    except Exception as e:
        print(f"  [warn] stages: {e}")

    print("Exporting tags...")
    try:
        tags = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/tag"))
        _write_json(out_dir, "tags", tags)
    except Exception as e:
        print(f"  [warn] tags: {e}")

    print("Exporting custom field defs...")
    try:
        cfs = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/customfield/definitions"))
        _write_json(out_dir, "custom_fields", cfs)
    except Exception as e:
        print(f"  [warn] custom_fields: {e}")

    print("Exporting contacts...")
    try:
        contacts = _paginate(portal, token, "/api/2.0/crm/contact/filter?count=500")
        _write_json(out_dir, "contacts", contacts)
    except Exception as e:
        print(f"  [warn] contacts: {e}")

    print("Exporting opportunities...")
    try:
        opps = _paginate(portal, token, "/api/2.0/crm/opportunity/filter?count=500")
        _write_json(out_dir, "opportunities", opps)
    except Exception as e:
        print(f"  [warn] opportunities: {e}")

    print("Exporting history events (this takes the longest)...")
    history = []
    try:
        opps_data = json.load(open(out_dir / "opportunities.json")) or []
        total = len(opps_data)
        for idx, opp in enumerate(opps_data):
            if not isinstance(opp, dict):
                continue
            oid = opp.get("id") or opp.get("ID")
            if not oid:
                continue
            try:
                hlist = _unwrap(_crm_get(portal, token,
                    f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={oid}&count=500"))
                if isinstance(hlist, list):
                    for h in hlist:
                        if isinstance(h, dict):
                            h["entityId"] = oid
                    history.extend(hlist)
            except Exception:
                pass
            if (idx + 1) % 100 == 0:
                print(f"  ... {idx+1}/{total} opportunities ({len(history)} events)")
        _write_json(out_dir, "history", history)
    except Exception as e:
        print(f"  [warn] history: {e}")
        _write_json(out_dir, "history", history)

    print("Exporting tasks...")
    try:
        open_tasks = _paginate(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=false")
        closed_tasks = _paginate(portal, token, "/api/2.0/crm/task/filter?count=500&isClosed=true")
        all_tasks = (open_tasks or []) + (closed_tasks or [])
        _write_json(out_dir, "tasks", all_tasks)
    except Exception as e:
        print(f"  [warn] tasks: {e}")

    print("Exporting task labels...")
    try:
        labels = _unwrap(_crm_get(portal, token, "/api/2.0/crm/task/label"))
        if not isinstance(labels, list):
            labels = []
        _write_json(out_dir, "task_labels", labels)
    except Exception as e:
        print(f"  [warn] task_labels: {e}")
        _write_json(out_dir, "task_labels", [])

    print("Export done.")
    _write_json(out_dir, "export_manifest", {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "note": "Portable export with per-opportunity history + tasks + task_labels. Re-import with import_json_export.py --portal-url <portal> to download attachments + avatars.",
    })


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migrate data from OnlyOffice CRM to PostgreSQL")
    parser.add_argument("--portal-url", required=True, help="OnlyOffice CRM portal URL")
    parser.add_argument("--email", required=True, help="Admin email for CRM login")
    parser.add_argument("--password", required=True, help="Admin password for CRM login")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--export-only", action="store_true", help="Export OnlyOffice data to JSON files only")
    parser.add_argument("--export-dir", default="crm_export_json", help="Output directory for --export-only")
    parser.add_argument("--skip-attachment-download", action="store_true", help="Don't download file attachments/avatars (metadata only)")
    args = parser.parse_args()

    print(f"Authenticating to {args.portal_url}...")
    auth_body = json.dumps({"userName": args.email, "password": args.password}).encode("utf-8")
    req = urllib.request.Request(
        f"{args.portal_url.rstrip('/')}/api/2.0/authentication.json",
        data=auth_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=20) as resp:
            auth_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"Authentication failed: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        sys.exit(f"Authentication failed: {exc.reason}")

    token = auth_data.get("response", {}).get("token")
    if not token:
        sys.exit(f"No token in authentication response: {auth_data}")
    print("Authenticated successfully.")

    if args.export_only:
        export_dir = Path(args.export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[EXPORT-ONLY] Writing to {export_dir.resolve()}")
        _do_export_only(args.portal_url, token, export_dir)
        print("\nExport complete.")
        return

    try:
        import psycopg2  # noqa: F811
    except ImportError:
        sys.exit("psycopg2 not found. Install with: pip install psycopg2-binary")

    print(f"\nConnecting to PostgreSQL at {args.db_host}:{args.db_port}/{args.db_name}...")
    conn = psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )
    conn.autocommit = False

    start_time = time.time()
    report_lines = []

    try:
        u = _migrate_users(conn, args.portal_url, token)
        report_lines.append(f"  Users imported:       {u}")

        s = _migrate_stages(conn, args.portal_url, token)
        report_lines.append(f"  Stages imported:      {s}")

        t = _migrate_tags(conn, args.portal_url, token)
        report_lines.append(f"  Tags imported:        {t}")

        cf = _migrate_custom_fields(conn, args.portal_url, token)
        report_lines.append(f"  Custom field defs:    {cf}")

        c = _migrate_contacts(conn, args.portal_url, token,
                              download_avatars=not args.skip_attachment_download)
        report_lines.append(f"  Contacts imported:    {c}")

        opps, hist, att = _migrate_opportunities(
            conn, args.portal_url, token,
            download_attachments=not args.skip_attachment_download,
        )
        report_lines.append(f"  Opportunities:        {opps}")
        report_lines.append(f"  History events:       {hist}")
        report_lines.append(f"  Attachments:          {att}")

        tasks = _migrate_tasks(conn, args.portal_url, token)
        report_lines.append(f"  Tasks:                {tasks}")

        profiles = _migrate_user_profiles(conn)
        report_lines.append(f"  User profiles:        {profiles}")

        elapsed = time.time() - start_time
        report_lines.append(f"\n  Migration completed in {elapsed:.1f}s")
        report_lines.append(f"\n  CRM user ID map: {json.dumps(_crm_id_map['users'], indent=4)}")
    except Exception as exc:
        conn.rollback()
        print(f"\n❌ Migration failed: {exc}")
        report_lines.append(f"\n  MIGRATION FAILED: {exc}")
        raise
    finally:
        report_lines_text = "\n".join(report_lines)
        MIGRATION_REPORT.write_text(report_lines_text, encoding="utf-8")
        print(f"\n{'='*60}")
        print("Migration Report:")
        print(report_lines_text)
        print(f"{'='*60}")
        print(f"\nReport saved to: {MIGRATION_REPORT}")
        conn.close()


if __name__ == "__main__":
    main()