#!/usr/bin/env python3
"""One-time migration from OnlyOffice CRM to PostgreSQL (Sietch CRM v3.0).

Run after init.sql has been executed and the PostgreSQL container is up.

For --export-only (recommended for round-trip tests): no psycopg2 required.
For full migration (writes to DB): requires psycopg2 (pip install psycopg2-binary)

Usage:
    python migrate_from_onlyoffice.py --portal-url https://office.publicadjustermidwest.com --email admin@example.com --password secret
    python migrate_from_onlyoffice.py --portal-url ... --email ... --password ... --export-only --export-dir ./crm-export
    python migrate_from_onlyoffice.py --help
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
# We set it to None here so --export-only works even if the package is not installed.
psycopg2 = None


# ── Configuration ──────────────────────────────────────────────────────────────

RATE_LIMIT_DELAY = 0.1       # 10 requests/sec
SUB_REQUEST_DELAY = 1.0      # 1s between opportunity sub-requests
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
ATTACHMENT_DIR = Path(__file__).resolve().parent / "data" / "attachments"
MIGRATION_REPORT = Path(__file__).resolve().parent / "migration_report.txt"


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
}


def _guess_mime_type(ext: str) -> str:
    return MIME_TYPES.get(ext.lower(), "application/octet-stream")

# Map CRM user IDs to new v3 user IDs
_crm_id_map: dict[str, dict[str, int]] = {
    "users": {},        # crm_user_id -> v3 user.id
    "contacts": {},     # crm_contact_id -> v3 contact.id
    "opportunities": {},  # crm_opp_id -> v3 opportunity.id
    "stages": {},       # crm_stage_id -> v3 stage.id
    "tags": {},         # crm_tag_id -> v3 tag.id
    "custom_fields": {},  # crm_field_id -> v3 custom_field_definition.id
    "history_categories": {},  # crm_category_title -> v3 history_category.id
}


# ── SSL Context ────────────────────────────────────────────────────────────────

def _ssl_context():
    verify = os.environ.get("ONLYOFFICE_SSL_VERIFY", "true").lower() not in ("0", "false", "no")
    if verify:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── HTTP Helpers ───────────────────────────────────────────────────────────────

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


def _unwrap(data: Any) -> Any:
    """Unwrap OnlyOffice API response envelope."""
    if isinstance(data, dict):
        if "response" in data:
            return data["response"]
        if "result" in data:
            return data["result"]
    return data


def _paginate(portal: str, token: str, base_path: str, param_name: str = "count", page_size: int = 500) -> list:
    """Paginate through a CRM list endpoint."""
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


# ── Database Helpers ───────────────────────────────────────────────────────────

def _db_insert_returning(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _db_insert(cur, sql: str, params: tuple = ()) -> None:
    cur.execute(sql, params)


# ── Migration Steps ────────────────────────────────────────────────────────────

def _migrate_users(conn, portal: str, token: str) -> int:
    """Step 1: Pull users from CRM and insert into PostgreSQL."""
    print("\n[Step 1] Migrating users...")
    data = _unwrap(_crm_get(portal, token, "/api/2.0/people/"))
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
    """Step 2: Pull stages from CRM."""
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
            probability = int(s.get("probability") or s.get("Probability") or 0)

            cur.execute(
                """INSERT INTO stages (title, color, sort_order, stage_type, probability)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (title) DO UPDATE SET
                       color = EXCLUDED.color, sort_order = EXCLUDED.sort_order
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
    """Step 3: Pull tag definitions from CRM."""
    print("\n[Step 3] Migrating tags...")
    data = _unwrap(_crm_get(portal, token, "/api/2.0/crm/tag"))
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
            color = str(t.get("color") or t.get("Color") or "").strip() or None

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
    """Step 4: Pull custom field definitions."""
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
            field_key = str(f.get("key") or f.get("Key") or f.get("fieldKey") or "").strip() or f"field_{crm_id}"
            field_type = str(f.get("type") or f.get("Type") or f.get("fieldType") or "text").strip().lower()
            is_required = bool(f.get("isRequired") or f.get("IsRequired"))
            sort_order = int(f.get("sortOrder") or 0)

            # Map OnlyOffice field types to our types
            type_map = {
                "text": "text", "textarea": "textarea", "htmleditor": "textarea",
                "select": "select", "combobox": "select", "checkbox": "checkbox",
                "date": "date", "datetime": "date", "number": "number",
                "money": "currency", "currency": "currency",
            }
            mapped_type = type_map.get(field_type, "text")

            cur.execute(
                """INSERT INTO custom_field_definitions (field_key, label, field_type, is_required, sort_order)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (field_key) DO UPDATE SET
                       label = EXCLUDED.label, field_type = EXCLUDED.field_type
                   RETURNING id""",
                (field_key, label, mapped_type, is_required, sort_order),
            )
            v3_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["custom_fields"][crm_id] = v3_id

            # Pull options for select-type fields
            if mapped_type in ("select",):
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
            count += 1
        conn.commit()
    print(f"  → {count} custom field definitions imported")
    return count


def _migrate_contacts(conn, portal: str, token: str) -> int:
    """Step 5: Pull contacts."""
    print("\n[Step 5] Migrating contacts...")
    crm_contacts = _paginate(portal, token, "/api/2.0/crm/contact")
    count = 0
    with conn.cursor() as cur:
        for c in crm_contacts:
            if not isinstance(c, dict):
                continue
            crm_id = str(c.get("id") or c.get("ID") or "")
            first_name = str(c.get("firstName") or c.get("FirstName") or "").strip()
            last_name = str(c.get("lastName") or c.get("LastName") or "").strip()
            email = str(c.get("email") or c.get("Email") or "").strip() or None
            phone = str(c.get("phone") or c.get("Phone") or "").strip() or None
            company = str(c.get("companyName") or c.get("CompanyName") or "").strip() or None

            cur.execute(
                """INSERT INTO contacts (first_name, last_name, email, phone, company)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""",
                (first_name or None, last_name or None, email, phone, company),
            )
            v3_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["contacts"][crm_id] = v3_id
            count += 1
        conn.commit()
    print(f"  → {count} contacts imported")
    return count


def _migrate_opportunities(conn, portal: str, token: str) -> tuple[int, int, int]:
    """Step 6: Pull all opportunities (all stages) with tags, custom fields, history, files."""
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
            title = str(o.get("title") or o.get("Title") or "Untitled").strip()

            # Stage mapping
            stage_data = o.get("stage") or o.get("Stage") or {}
            crm_stage_id = str(stage_data.get("id") or stage_data.get("ID") or "")
            v3_stage_id = _crm_id_map["stages"].get(crm_stage_id)
            stage_type = int(stage_data.get("stageType") or 0) if isinstance(stage_data, dict) else 0

            # Contact mapping
            contact_data = o.get("contact") or o.get("Contact") or {}
            crm_contact_id = str(contact_data.get("id") or contact_data.get("ID") or "") if isinstance(contact_data, dict) else ""
            v3_contact_id = _crm_id_map["contacts"].get(crm_contact_id)

            # Responsible user mapping
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
                    due_date = str(raw)[:10]  # YYYY-MM-DD
                    break

            description = str(o.get("description") or o.get("Description") or "").strip() or None

            cur.execute(
                """INSERT INTO opportunities (title, description, stage_id, stage_type, bid_value,
                       expected_close_date, contact_id, responsible_user_id, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   RETURNING id""",
                (title, description, v3_stage_id, stage_type, bid_value, due_date, v3_contact_id, v3_responsible_id),
            )
            v3_opp_id = cur.fetchone()[0]
            if crm_id:
                _crm_id_map["opportunities"][crm_id] = v3_opp_id

            # ── Tags for this opportunity ──
            time.sleep(SUB_REQUEST_DELAY)
            try:
                tags_data = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/opportunity/tag/{crm_id}"))
                tag_list = tags_data if isinstance(tags_data, list) else []
                for t in tag_list:
                    tag_title = ""
                    tag_crm_id = ""
                    if isinstance(t, dict):
                        tag_title = str(t.get("title") or t.get("name") or t.get("Title") or t.get("Name") or "").strip()
                        tag_crm_id = str(t.get("id") or t.get("ID") or "")
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
            except Exception as exc:
                print(f"  [warn] Tags for opp {crm_id}: {exc}")

            # ── Custom field values ──
            time.sleep(SUB_REQUEST_DELAY)
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
            except Exception as exc:
                print(f"  [warn] Custom fields for opp {crm_id}: {exc}")

            # ── History events ──
            time.sleep(SUB_REQUEST_DELAY)
            try:
                hist_data = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={crm_id}&count=500&startIndex=0"))
                hist_list = hist_data if isinstance(hist_data, list) else []
                for h in hist_list:
                    if not isinstance(h, dict):
                        continue
                    # Category
                    cat_data = h.get("category") or h.get("Category") or {}
                    cat_title = str(cat_data.get("title") or cat_data.get("Title") or "Note").strip() if isinstance(cat_data, dict) else "Note"
                    cur.execute("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
                    cat_row = cur.fetchone()
                    if not cat_row:
                        cur.execute(
                            "INSERT INTO history_categories (title, is_system) VALUES (%s, TRUE) RETURNING id",
                            (cat_title,),
                        )
                        cat_id = cur.fetchone()[0]
                    else:
                        cat_id = cat_row[0]

                    event_content = str(h.get("content") or h.get("Content") or "").strip() or None
                    event_created = h.get("created") or h.get("Created") or h.get("date") or None

                    # Author mapping
                    create_by = h.get("createBy") or h.get("CreateBy") or h.get("createdBy") or {}
                    crm_author_id = str(create_by.get("id") or create_by.get("ID") or "") if isinstance(create_by, dict) else ""
                    v3_author_id = _crm_id_map["users"].get(crm_author_id)

                    cur.execute(
                        """INSERT INTO history_events (opportunity_id, category_id, content, created_by, created_at)
                           VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()))
                           RETURNING id""",
                        (v3_opp_id, cat_id, event_content, v3_author_id, event_created),
                    )
                    history_count += 1

                    # Notify users from this event
                    notify_list = h.get("notifyUserList") or h.get("NotifyUserList") or []
                    event_id = cur.fetchone()[0]
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
            except Exception as exc:
                print(f"  [warn] History for opp {crm_id}: {exc}")

            # ── Attachments ──
            time.sleep(SUB_REQUEST_DELAY)
            try:
                files_data = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/opportunity/{crm_id}/files"))
                files_list = files_data if isinstance(files_data, list) else []
                for f_item in files_data if isinstance(files_data, dict) else files_list:
                    if not isinstance(f_item, dict):
                        continue
                    file_id = str(f_item.get("id") or f_item.get("ID") or "")
                    file_name = str(f_item.get("name") or f_item.get("Name") or f"file_{file_id}").strip()
                    file_size = int(f_item.get("size") or f_item.get("Size") or 0)
                    file_ext = Path(file_name).suffix.lstrip(".").lower()
                    mime_type = _guess_mime_type(file_ext)

                    # Map file owner to v3 user
                    owner = f_item.get("createdBy") or f_item.get("CreatedBy") or f_item.get("owner") or f_item.get("Owner") or {}
                    owner_crm_id = str(owner.get("id") or owner.get("ID") or "") if isinstance(owner, dict) else ""
                    v3_uploader_id = _crm_id_map["users"].get(owner_crm_id) or v3_author_id

                    # Download file
                    opp_dir = ATTACHMENT_DIR / str(v3_opp_id)
                    opp_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r'[^\w.\-]', '_', file_name)
                    if not safe_name:
                        safe_name = f"file_{file_id}"
                    unique_name = f"{int(time.time())}_{safe_name}"
                    file_path = opp_dir / unique_name

                    raw = _crm_get_raw(portal, token, f"/api/2.0/files/{file_id}")
                    if raw:
                        file_path.write_bytes(raw)
                        cur.execute(
                            """INSERT INTO history_attachments (event_id, filename, file_path, file_size, mime_type, uploaded_by)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (event_id, file_name, str(file_path.relative_to(Path(__file__).resolve().parent)), file_size, mime_type, v3_uploader_id),
                        )
                        attachment_count += 1
            except Exception as exc:
                print(f"  [warn] Files for opp {crm_id}: {exc}")

            opp_count += 1
            if opp_count % 50 == 0:
                conn.commit()
                print(f"  ... {opp_count}/{len(crm_opps)} opportunities processed")

        conn.commit()
    print(f"  → {opp_count} opportunities, {history_count} history events, {attachment_count} attachments")
    return opp_count, history_count, attachment_count


def _migrate_tasks(conn, portal: str, token: str) -> int:
    """Step 7: Pull tasks (open + closed)."""
    print("\n[Step 7] Migrating tasks...")
    open_tasks = _paginate(portal, token, "/api/2.0/projects/task/")
    closed_tasks = _paginate(portal, token, "/api/2.0/projects/task/?closed=true")
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

            # Link to opportunity
            opp_data = t.get("opportunity") or t.get("Opportunity") or {}
            crm_opp_id = str(opp_data.get("id") or opp_data.get("ID") or "") if isinstance(opp_data, dict) else ""
            v3_opp_id = _crm_id_map["opportunities"].get(crm_opp_id)

            # Responsible user
            resp_data = t.get("responsible") or t.get("Responsible") or {}
            crm_resp_id = str(resp_data.get("id") or resp_data.get("ID") or "") if isinstance(resp_data, dict) else ""
            v3_resp_id = _crm_id_map["users"].get(crm_resp_id)

            due_date = t.get("dueDate") or t.get("DueDate") or None
            priority = int(t.get("priority") or t.get("Priority") or 0)

            cur.execute(
                """INSERT INTO tasks (title, description, opportunity_id, responsible_user_id,
                       due_date, priority, is_closed, closed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (title, description, v3_opp_id, v3_resp_id, due_date, priority, is_closed,
                 t.get("closedDate") or t.get("ClosedDate") if is_closed else None),
            )
            count += 1
        conn.commit()
    print(f"  → {count} tasks imported")
    return count


def _migrate_user_profiles(conn) -> int:
    """Step 9: Migrate existing JSON user profiles."""
    print("\n[Step 9] Migrating user profiles from JSON...")
    profiles_dir = Path(__file__).resolve().parent / "data" / "user-profiles"
    if not profiles_dir.is_dir():
        print("  → No user-profiles directory found, skipping")
        return 0

    count = 0
    with conn.cursor() as cur:
        for portal_dir in profiles_dir.iterdir():
            if not portal_dir.is_dir():
                continue
            for profile_file in portal_dir.glob("*.json"):
                try:
                    data = json.loads(profile_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        continue

                    # Profiles are remapped properly by migrate_dashboard_data.py using external_user_id
                    # Skip insert here to avoid polluting with user_id=1.
                    # This step is kept for compatibility but does not write.
                    print(f"  [note] Skipping profile insert for {profile_file.name} (use migrate_dashboard_data.py for remap)")
                    count += 1  # counted for report, no DB write
                except (json.JSONDecodeError, OSError) as exc:
                    print(f"  [warn] Profile {profile_file}: {exc}")
        conn.commit()
    print(f"  → {count} user profiles migrated (remap after user migration)")
    return count


# ── Export-only (Phase 2) ──────────────────────────────────────────────────────

def _write_json(out_dir: Path, name: str, data: Any) -> None:
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {name}.json ({len(data) if isinstance(data, (list, dict)) else 'data'})")


def _do_export_only(portal: str, token: str, out_dir: Path) -> None:
    """Fetch data from OnlyOffice and write portable JSON files. No DB writes."""

    # Users (for reference / later mapping)
    print("Exporting users...")
    try:
        users = _paginate(portal, token, "/api/2.0/people/filter?count=500")
        _write_json(out_dir, "users", users)
    except Exception as e:
        print(f"  [warn] users: {e}")

    # Stages
    print("Exporting stages...")
    try:
        stages = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/stage"))
        _write_json(out_dir, "stages", stages)
    except Exception as e:
        print(f"  [warn] stages: {e}")

    # Tags
    print("Exporting tags...")
    try:
        tags = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/tag"))
        _write_json(out_dir, "tags", tags)
    except Exception as e:
        print(f"  [warn] tags: {e}")

    # Custom field definitions
    print("Exporting custom field defs...")
    try:
        cfs = _unwrap(_crm_get(portal, token, "/api/2.0/crm/opportunity/customfield/definitions"))
        _write_json(out_dir, "custom_fields", cfs)
    except Exception as e:
        print(f"  [warn] custom_fields: {e}")

    # Contacts (working /filter query)
    print("Exporting contacts...")
    try:
        contacts = _paginate(portal, token, "/api/2.0/crm/contact/filter?count=500")
        _write_json(out_dir, "contacts", contacts)
    except Exception as e:
        print(f"  [warn] contacts: {e}")

    # Opportunities (working query)
    print("Exporting opportunities...")
    try:
        opps = _paginate(portal, token, "/api/2.0/crm/opportunity/filter?count=500")
        _write_json(out_dir, "opportunities", opps)
    except Exception as e:
        print(f"  [warn] opportunities: {e}")

    # History — per-opportunity (the only pattern that works)
    print("Exporting history events...")
    history = []
    try:
        opps = json.load(open(out_dir / "opportunities.json")) or []
        total = len(opps)
        for idx, opp in enumerate(opps):
            if not isinstance(opp, dict): continue
            oid = opp.get("id") or opp.get("ID")
            if not oid: continue
            try:
                hlist = _unwrap(_crm_get(portal, token, f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={oid}&count=500"))
                if isinstance(hlist, list):
                    for h in hlist:
                        if isinstance(h, dict):
                            h["entityId"] = oid
                    history.extend(hlist)
            except Exception:
                pass
            if (idx + 1) % 100 == 0:
                print(f"  ... {idx+1}/{total} opportunities")
        _write_json(out_dir, "history", history)
        print(f"  → {len(history)} history events")
    except Exception as e:
        print(f"  [warn] history: {e}")
        _write_json(out_dir, "history", history)

    # Tasks
    print("Exporting tasks...")
    try:
        open_tasks = _paginate(portal, token, "/api/2.0/projects/task/")
        closed_tasks = _paginate(portal, token, "/api/2.0/projects/task/?closed=true")
        all_tasks = (open_tasks or []) + (closed_tasks or [])
        _write_json(out_dir, "tasks", all_tasks)
    except Exception as e:
        print(f"  [warn] tasks: {e}")

    # Optional: basic files metadata note (attachments are pulled during history in full migration)
    print("Exporting notes (files metadata not bulk-exported here; use full migration for attachments)")
    _write_json(out_dir, "export_manifest", {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "note": "Run with full migration or use document endpoints for files. This export is for core CRM entities."
    })


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migrate data from OnlyOffice CRM to PostgreSQL")
    parser.add_argument("--portal-url", required=True, help="OnlyOffice CRM portal URL")
    parser.add_argument("--email", required=True, help="Admin email for CRM login")
    parser.add_argument("--password", required=True, help="Admin password for CRM login")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "vanguard"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "vanguard"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--export-only", action="store_true", help="Export OnlyOffice data to JSON files only (portable, no DB connection or writes)")
    parser.add_argument("--export-dir", default="crm_export_json", help="Output directory for JSON export when using --export-only")
    args = parser.parse_args()

    # ── Connect to OnlyOffice CRM ──
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
        print("\nExport complete. JSON files written.")
        return

    # ── Connect to PostgreSQL ──
    try:
        import psycopg2
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
        # Run migration steps in dependency order
        u = _migrate_users(conn, args.portal_url, token)
        report_lines.append(f"  Users imported:       {u}")

        s = _migrate_stages(conn, args.portal_url, token)
        report_lines.append(f"  Stages imported:      {s}")

        t = _migrate_tags(conn, args.portal_url, token)
        report_lines.append(f"  Tags imported:        {t}")

        cf = _migrate_custom_fields(conn, args.portal_url, token)
        report_lines.append(f"  Custom field defs:    {cf}")

        c = _migrate_contacts(conn, args.portal_url, token)
        report_lines.append(f"  Contacts imported:    {c}")

        opps, hist, att = _migrate_opportunities(conn, args.portal_url, token)
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
