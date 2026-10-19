#!/usr/bin/env python3
"""Import portable JSON export (produced by migrate_from_onlyoffice.py --export-only)
into Sietch CRM PostgreSQL.

Imports: users (w/ external_user_id), stages, tags, custom field defs (numeric
type map), contacts (with multi-value emails/phones/addresses + person→company
linkage), opportunities (w/ stage/contact/responsible + tags + actual_close_date),
tasks, history events (w/ category mapping, author mapping, embedded mail-message
JSON + email attachments), history attachments (DOWNLOADED into data/history-attachments/).

Usage:
    python import_json_export.py --source ./CRM_export --db-host 127.0.0.1 ...
    python import_json_export.py --source ./CRM_export --dry-run
    python import_json_export.py --source ./CRM_export --skip-attachment-download
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not found. pip install psycopg2-binary")


# ── Configuration ──────────────────────────────────────────────────────────────

RATE_LIMIT_DELAY = 0.05   # 20 req/sec for downloads
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5
HISTORY_ATTACHMENT_DIR = Path(__file__).resolve().parent / "data" / "history-attachments"
CONTACT_AVATAR_DIR = Path(__file__).resolve().parent / "data" / "contact-avatars"
OO_CLOSURE_SENTINEL = "0001-01-01T00:00:00"

# OnlyOffice fieldType is NUMERIC. Map every value we've seen in the wild.
# 0=text, 1=textarea, 2=number, 3=file/url picker, 4=select, 5=date, 6=checkbox,
# 7=currency, 8=user picker. Unknown numeric values fall back to "text".
_OO_FIELD_TYPE_MAP = {
    "0": "text", "1": "textarea", "2": "number", "3": "url",
    "4": "select", "5": "date", "6": "checkbox", "7": "currency",
    "8": "text",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "response" in data:
        return data["response"]
    return data


def _db_conn(args: argparse.Namespace):
    return psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )


def _normalize_field_type(raw: Any) -> str:
    """Source fieldType is numeric; map to Sietch string type."""
    if raw is None:
        return "text"
    key = str(raw).strip().lower()
    return _OO_FIELD_TYPE_MAP.get(key, "text")


def _is_closure_sentinel(raw: Any) -> bool:
    """OnlyOffice sends the .NET min-value date as the 'not closed' marker."""
    s = str(raw or "").strip()
    return s.startswith(OO_CLOSURE_SENTINEL)


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    seg = re.sub(r"[^\w.-]+", "_", str(value or "").strip())[:120]
    return seg or fallback


def _safe_filename(name: str) -> str:
    out = re.sub(r"[^\w.\-]+", "_", name or "").strip("._")
    return out or "file"


def _ssl_context():
    return None  # OnlyOffice SSL handled by caller


# ── Download helpers ──────────────────────────────────────────────────────────

def _download_bytes(url: str, headers: dict | None = None, timeout: int = 60) -> bytes | None:
    """Download raw bytes with retry."""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_err = exc
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    print(f"    [warn] download failed: {url}: {last_err}")
    return None


# ── Stages / Tags / CF Defs ────────────────────────────────────────────────────

def _insert_stages(conn, stages: list[dict]) -> dict[str, int]:
    if not stages:
        return {}
    cur = conn.cursor()
    crm_to_v3: dict[str, int] = {}
    for s in stages:
        if not isinstance(s, dict):
            continue
        title = s.get("title") or s.get("Title") or "Stage"
        color = s.get("color") or "#4f8cff"
        sort = s.get("sortOrder") or s.get("sort_order") or 0
        stype = s.get("stageType") or s.get("stage_type") or 0
        prob = s.get("probability") or s.get("successProbability") or 0
        cur.execute(
            """INSERT INTO stages (title, color, sort_order, stage_type, probability)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (title) DO UPDATE SET
                   color=EXCLUDED.color,
                   sort_order=EXCLUDED.sort_order,
                   stage_type=EXCLUDED.stage_type,
                   probability=EXCLUDED.probability
               RETURNING id""",
            (title, color, sort, stype, prob),
        )
        vid = cur.fetchone()[0]
        crm_id = str(s.get("id") or s.get("ID") or title)
        crm_to_v3[crm_id] = vid
    conn.commit()
    print(f"  stages: {len(crm_to_v3)}")
    return crm_to_v3


def _insert_tags(conn, tags: list[dict]) -> dict[str, int]:
    if not tags:
        return {}
    cur = conn.cursor()
    crm_to_v3: dict[str, int] = {}
    for t in tags:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or t.get("Title") or t.get("name") or "").strip()
        if not title:
            continue
        color = t.get("color") or "#6c757d"
        cur.execute(
            """INSERT INTO tag_definitions (title, color)
               VALUES (%s, %s)
               ON CONFLICT (title) DO NOTHING
               RETURNING id""",
            (title, color),
        )
        row = cur.fetchone()
        if row:
            crm_id = str(t.get("id") or t.get("ID") or title)
            crm_to_v3[crm_id] = row[0]
        else:
            cur.execute("SELECT id FROM tag_definitions WHERE title = %s", (title,))
            row = cur.fetchone()
            if row:
                crm_to_v3[str(t.get("id") or t.get("ID") or title)] = row[0]
    conn.commit()
    print(f"  tags: {len(crm_to_v3)}")
    return crm_to_v3


def _insert_custom_fields(conn, cfs: list[dict]) -> dict[str, int]:
    """Custom field defs with numeric type_map."""
    if not cfs:
        return {}
    cur = conn.cursor()
    crm_to_v3: dict[str, int] = {}
    count = 0
    for cf in cfs:
        if not isinstance(cf, dict):
            continue
        label = str(cf.get("label") or cf.get("title") or cf.get("name") or cf.get("fieldTitle") or "").strip()
        if not label:
            continue
        crm_id = str(cf.get("id") or cf.get("ID") or "")
        # Convention: field_key = "field_<crm_id>" so both scripts agree.
        field_key = f"field_{crm_id}" if crm_id else f"field_{label.lower().replace(' ', '_')}"
        mapped_type = _normalize_field_type(cf.get("fieldType") or cf.get("type") or cf.get("Type"))
        is_required = bool(cf.get("isRequired") or cf.get("IsRequired"))
        sort_order = int(cf.get("position") or cf.get("sortOrder") or 0)
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
        row = cur.fetchone()
        if row:
            crm_to_v3[crm_id] = row[0]
            count += 1
    conn.commit()
    print(f"  custom fields: {count}")
    return crm_to_v3


# ── Users ──────────────────────────────────────────────────────────────────────

def _insert_users(conn, users: list[dict]) -> dict[str, int]:
    if not users:
        return {}
    cur = conn.cursor()
    crm_to_v3: dict[str, int] = {}
    count = 0
    for u in users:
        if not isinstance(u, dict):
            continue
        email = str(u.get("email") or u.get("Email") or "").strip().lower()
        if not email:
            continue
        display_name = str(u.get("displayName") or u.get("DisplayName") or u.get("userName") or "").strip()
        first_name = str(u.get("firstName") or u.get("FirstName") or "").strip()
        last_name = str(u.get("lastName") or u.get("LastName") or "").strip()
        is_admin = bool(u.get("isAdmin") or u.get("IsAdmin"))
        crm_id = str(u.get("id") or u.get("ID") or "")

        cur.execute(
            """INSERT INTO users (email, external_user_id, display_name, first_name, last_name, is_admin, must_change_password)
               VALUES (%s, %s, %s, %s, %s, %s, TRUE)
               ON CONFLICT (email) DO UPDATE SET
                   external_user_id = EXCLUDED.external_user_id,
                   display_name = EXCLUDED.display_name,
                   first_name = EXCLUDED.first_name,
                   last_name = EXCLUDED.last_name,
                   is_admin = EXCLUDED.is_admin
               RETURNING id""",
            (email, crm_id or None, display_name or None, first_name or None, last_name or None, is_admin),
        )
        vid = cur.fetchone()[0]
        if crm_id:
            crm_to_v3[crm_id] = vid
        count += 1
    conn.commit()
    print(f"  users: {count}")
    return crm_to_v3


# ── Contacts (with multi-value tables + person→company + avatar) ──────────────

def _insert_contacts(
    conn, contacts: list[dict], user_map: dict[str, int],
    portal_url: str | None = None, download_avatars: bool = True,
) -> dict[str, int]:
    """Insert with multi-value commonData and addresses.

    Returns crm_id -> local_id.
    """
    if not contacts:
        return {}
    cur = conn.cursor()
    crm_to_v3: dict[str, int] = {}
    count = 0
    multi_emails = 0
    multi_phones = 0
    multi_addrs = 0

    # Dedupe by crm_id — the export can list a contact multiple times
    # across paginated endpoints.
    seen: set[str] = set()
    unique_contacts: list[dict] = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or c.get("ID") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        unique_contacts.append(c)
    contacts = unique_contacts

    for c in contacts:
        if not isinstance(c, dict):
            continue
        crm_id = str(c.get("id") or c.get("ID") or "")
        if not crm_id:
            continue
        if crm_id in crm_to_v3:
            continue  # already inserted

        first_name = str(c.get("firstName") or c.get("FirstName") or "").strip() or None
        last_name = str(c.get("lastName") or c.get("LastName") or "").strip() or None
        display_name = str(c.get("displayName") or "").strip()
        company_name = str(c.get("companyName") or "").strip() or None
        is_company = bool(c.get("isCompany"))
        about = str(c.get("about") or "").strip() or None
        industry = str(c.get("industry") or "").strip() or None

        contact_status = None
        cs = c.get("contactStatus")
        if isinstance(cs, dict):
            contact_status = str(cs.get("title") or "").strip() or None

        task_count = int(c.get("taskCount") or 0) if c.get("taskCount") is not None else None

        # Single-valued email/phone/address (primary) for backwards compat
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
        # Fallback to first entry if no primary
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

        # Single-valued primary address (for backwards compat)
        addresses = c.get("addresses") or []
        primary_addr = None
        for a in addresses:
            if isinstance(a, dict) and a.get("isPrimary"):
                primary_addr = a
                break
        if not primary_addr and addresses:
            primary_addr = addresses[0] if isinstance(addresses[0], dict) else None

        # Avatar
        avatar_remote = str(c.get("smallFotoUrl") or "").strip() or None
        avatar_local = None
        if download_avatars and avatar_remote and portal_url:
            try:
                # OnlyOffice paths are relative — they start with "/" or "Products/"
                full_url = avatar_remote if avatar_remote.startswith("http") else f"{portal_url.rstrip('/')}{avatar_remote}"
                raw = _download_bytes(full_url, timeout=15)
                if raw:
                    ext = "jpg"
                    if "png" in full_url.lower():
                        ext = "png"
                    elif "jpeg" in full_url.lower() or "jpg" in full_url.lower():
                        ext = "jpg"
                    avatar_local_path = CONTACT_AVATAR_DIR / f"{crm_id}.{ext}"
                    avatar_local_path.parent.mkdir(parents=True, exist_ok=True)
                    avatar_local_path.write_bytes(raw)
                    avatar_local = str(avatar_local_path.relative_to(Path(__file__).resolve().parent))
            except Exception as exc:
                print(f"    [warn] avatar download failed for {crm_id}: {exc}")

        # Creator
        create_by = c.get("createBy") or {}
        creator_id = user_map.get(str(create_by.get("id") or "")) if isinstance(create_by, dict) else None

        cur.execute(
            """INSERT INTO contacts (
                   first_name, last_name, company, email, phone, is_company,
                   contact_type, website, industry, about, contact_status, avatar_url,
                   onlyoffice_id, address_street, address_city, address_state, address_zip,
                   address_country, created_by,
                   onlyoffice_task_count, onlyoffice_status_id,
                   raw_about, raw_industry, avatar_local_path)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING
               RETURNING id""",
            (first_name, last_name, company_name, primary_email, primary_phone, is_company,
             None, website, industry, about, contact_status,
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
             int(c.get("contactStatus", {}).get("id") or 0) if isinstance(c.get("contactStatus"), dict) else None,
             about, industry, avatar_local),
        )
        row = cur.fetchone()
        if not row:
            # Already imported (cross-run dedupe). Look up the existing id.
            cur.execute("SELECT id FROM contacts WHERE onlyoffice_id = %s", (crm_id,))
            erow = cur.fetchone()
            if not erow:
                continue
            vid = erow[0]
        else:
            vid = row[0]
        crm_to_v3[crm_id] = vid
        count += 1

        # Insert multi-value commonData entries (skipped for companies that share with persons)
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
                # email
                cur.execute(
                    """INSERT INTO contact_emails (contact_id, email, category, category_id, is_primary, onlyoffice_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (vid, data, cat_name or None, cat_id, is_primary, oo_id),
                )
                multi_emails += 1
            elif info_type == 0:
                # phone
                cur.execute(
                    """INSERT INTO contact_phones (contact_id, phone, category, category_id, is_primary, onlyoffice_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (vid, data, cat_name or None, cat_id, is_primary, oo_id),
                )
                multi_phones += 1
            elif info_type == 2:
                # URL — store as a contact_phone is wrong; skip; already on contacts.website
                pass

        # Multi-value addresses
        for a in addresses:
            if not isinstance(a, dict):
                continue
            cur.execute(
                """INSERT INTO contact_addresses
                       (contact_id, street, city, state, zip, country, category, category_id, is_primary, onlyoffice_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (vid,
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

    conn.commit()
    print(f"  contacts: {count} (emails: {multi_emails}, phones: {multi_phones}, addresses: {multi_addrs})")
    return crm_to_v3


def _link_contacts_to_companies(conn, contacts: list[dict], crm_to_v3: dict[str, int]) -> int:
    """Second pass: set company_id on persons based on company link."""
    cur = conn.cursor()
    count = 0
    for c in contacts:
        if not isinstance(c, dict):
            continue
        if c.get("isCompany"):
            continue
        crm_id = str(c.get("id") or "")
        local_id = crm_to_v3.get(crm_id)
        if not local_id:
            continue
        company_obj = c.get("company")
        if isinstance(company_obj, dict):
            comp_oo_id = str(company_obj.get("id") or "")
            comp_local_id = crm_to_v3.get(comp_oo_id)
            if comp_local_id:
                cur.execute(
                    "UPDATE contacts SET company_id = %s, company = COALESCE(company, %s) WHERE id = %s",
                    (comp_local_id, str(company_obj.get("displayName") or "").strip() or None, local_id),
                )
                count += 1
    conn.commit()
    print(f"  person→company links: {count}")
    return count


# ── Opportunities ─────────────────────────────────────────────────────────────

def _insert_opportunities(
    conn, opps: list[dict], stage_map: dict[str, int],
    contact_map: dict[str, int], user_map: dict[str, int], tag_map: dict[str, int],
    cf_map: dict[str, int],
) -> dict[str, int]:
    if not opps:
        return {}
    cur = conn.cursor()
    opp_map: dict[str, int] = {}
    count = 0

    for o in opps:
        if not isinstance(o, dict):
            continue
        crm_id = str(o.get("id") or o.get("ID") or "")
        if not crm_id:
            continue

        title = str(o.get("title") or o.get("Title") or "(Untitled)").strip()

        # Stage
        stage_data = o.get("stage") or o.get("Stage") or {}
        stage_crm_id = str(stage_data.get("id") or stage_data.get("ID") or o.get("stageId") or "")
        stage_id = stage_map.get(stage_crm_id)
        stage_type = 0
        try:
            st = stage_data.get("stageType") if isinstance(stage_data, dict) else None
            stage_type = int(st) if st is not None else 0
        except Exception:
            stage_type = 0

        # Contact (person OR company)
        contact_data = o.get("contact") or o.get("Contact") or {}
        contact_crm_id = str(contact_data.get("id") or contact_data.get("ID") or "") if isinstance(contact_data, dict) else ""
        local_contact_id = contact_map.get(contact_crm_id)

        # Responsible
        resp_data = o.get("responsible") or o.get("Responsible") or {}
        resp_crm_id = str(resp_data.get("id") or resp_data.get("ID") or "") if isinstance(resp_data, dict) else ""
        responsible_id = user_map.get(resp_crm_id)

        # Bid value
        bid_value = None
        for k in ("bidValue", "BidValue", "value", "Value"):
            if k in o and o[k] is not None:
                try:
                    bid_value = float(o[k])
                except Exception:
                    pass
                break

        # Dates
        due = None
        for k in ("expectedCloseDate", "ExpectedCloseDate", "dueDate"):
            raw = o.get(k)
            if raw:
                try:
                    due = str(raw)[:10]
                except Exception:
                    due = None
                if due:
                    break

        actual_close_date = None
        actual_close_value = None
        acd = o.get("actualCloseDate")
        if acd and not _is_closure_sentinel(acd):
            try:
                actual_close_date = str(acd)[:10]
            except Exception:
                actual_close_date = None
        # Capture close value if present (some exports carry this)
        for k in ("actualCloseValue", "ActualCloseValue", "closeValue"):
            if k in o and o[k] is not None:
                try:
                    actual_close_value = float(o[k])
                except Exception:
                    actual_close_value = None
                break

        desc = o.get("description") or o.get("Description") or ""
        if isinstance(desc, str):
            desc = desc.strip() or None

        # created
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
                   stage_id = EXCLUDED.stage_id,
                   bid_value = EXCLUDED.bid_value,
                   actual_close_date = EXCLUDED.actual_close_date,
                   contact_id = COALESCE(EXCLUDED.contact_id, opportunities.contact_id)
               RETURNING id""",
            (title, desc, stage_id, stage_type, bid_value,
             due, actual_close_date, actual_close_value,
             local_contact_id, responsible_id, created_at, crm_id),
        )
        vid = cur.fetchone()[0]
        opp_map[crm_id] = vid
        count += 1

        # Tags
        try:
            opp_tags = o.get("tags") or o.get("Tags") or []
            if isinstance(opp_tags, list):
                for tg in opp_tags:
                    tag_title = ""
                    if isinstance(tg, dict):
                        tag_title = str(tg.get("title") or tg.get("Title") or tg.get("name") or "").strip()
                    elif isinstance(tg, str):
                        tag_title = tg.strip()
                    if tag_title:
                        cur.execute("SELECT id FROM tag_definitions WHERE title = %s", (tag_title,))
                        tr = cur.fetchone()
                        if tr:
                            cur.execute(
                                """INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at)
                                   VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING""",
                                (vid, tr[0]),
                            )
        except Exception:
            pass

        # Custom field values (embedded customFields[] from export)
        try:
            cf_list = o.get("customFields") or []
            for cf in cf_list:
                if not isinstance(cf, dict):
                    continue
                cf_crm_id = str(cf.get("id") or cf.get("ID") or "")
                cf_value = cf.get("fieldValue") or cf.get("value") or cf.get("Value") or ""
                if isinstance(cf_value, dict):
                    cf_value = cf_value.get("title") or cf_value.get("text") or cf_value.get("value") or ""
                cf_value = str(cf_value).strip()
                v3_field_id = cf_map.get(cf_crm_id)
                if v3_field_id and cf_value:
                    cur.execute(
                        """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (opportunity_id, field_id) DO UPDATE SET field_value = EXCLUDED.field_value""",
                        (vid, v3_field_id, cf_value),
                    )
        except Exception:
            pass

    conn.commit()
    print(f"  opportunities: {count}")
    return opp_map


# ── Tasks ──────────────────────────────────────────────────────────────────────

def _insert_tasks(conn, tasks: list[dict], opp_map: dict[str, int], user_map: dict[str, int]) -> int:
    if not tasks:
        return 0
    cur = conn.cursor()
    count = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or t.get("Title") or "").strip()
        if not title:
            continue
        description = str(t.get("description") or t.get("Description") or "").strip() or None
        is_closed = bool(t.get("isClosed") or t.get("IsClosed") or t.get("closed") or False)

        # Link via opportunity entity OR top-level opportunityId
        opp_data = t.get("opportunity") or {}
        crm_opp_id = ""
        if isinstance(opp_data, dict):
            crm_opp_id = str(opp_data.get("id") or "")
        if not crm_opp_id:
            ent = t.get("entity") or {}
            if isinstance(ent, dict) and ent.get("entityType") == "opportunity":
                crm_opp_id = str(ent.get("entityId") or "")
        if not crm_opp_id:
            crm_opp_id = str(t.get("entityId") or t.get("opportunityId") or "")
        v3_opp_id = opp_map.get(crm_opp_id)

        # Responsible
        resp_data = t.get("responsible") or {}
        crm_resp_id = str(resp_data.get("id") or "") if isinstance(resp_data, dict) else ""
        v3_resp_id = user_map.get(crm_resp_id)

        due_date = t.get("dueDate") or t.get("DueDate") or None
        priority = int(t.get("priority") or t.get("Priority") or 0)
        closed_at = None
        if is_closed:
            closed_at = t.get("closedDate") or t.get("ClosedDate") or t.get("closedAt") or None

        crm_task_id = str(t.get("id") or t.get("ID") or "")

        cur.execute(
            """INSERT INTO tasks
                   (title, description, opportunity_id, responsible_user_id,
                    due_date, priority, is_closed, closed_at, created_at, onlyoffice_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
               ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING
               RETURNING id""",
            (title, description, v3_opp_id, v3_resp_id, due_date, priority,
             is_closed, closed_at, crm_task_id or None),
        )
        result = cur.fetchone()
        if result:
            count += 1
        else:
            # Conflict on onlyoffice_id — already imported; count as skipped
            pass
    conn.commit()
    print(f"  tasks: {count}")
    return count


def _insert_task_labels(conn, labels: list[dict]) -> int:
    if not labels:
        return 0
    cur = conn.cursor()
    count = 0
    for lb in labels:
        if not isinstance(lb, dict):
            continue
        title = str(lb.get("title") or lb.get("name") or "").strip()
        if not title:
            continue
        color = lb.get("color") or None
        sort_order = int(lb.get("sort_order") or lb.get("sortOrder") or 0)
        cur.execute(
            """INSERT INTO task_labels (title, color, sort_order)
               VALUES (%s, %s, %s)
               ON CONFLICT (title) DO UPDATE SET color = EXCLUDED.color, sort_order = EXCLUDED.sort_order""",
            (title, color, sort_order),
        )
        count += 1
    conn.commit()
    print(f"  task labels: {count}")
    return count


# ── History events + attachments ──────────────────────────────────────────────

def _resolve_category(cur, cat_title: str, cat_data: dict) -> int:
    """Get-or-create category. Returns id."""
    cur.execute("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
    row = cur.fetchone()
    if row:
        return row[0]
    color = cat_data.get("display_color") or cat_data.get("color") if isinstance(cat_data, dict) else None
    icon_key = (cat_data.get("icon_key", "note") if isinstance(cat_data, dict) else "note") or "note"
    icon_style = (cat_data.get("icon_style", "stroke") if isinstance(cat_data, dict) else "stroke") or "stroke"
    cur.execute(
        """INSERT INTO history_categories (title, display_color, icon_key, icon_style, is_system)
           VALUES (%s, %s, %s, %s, TRUE) RETURNING id""",
        (cat_title, color, icon_key, icon_style),
    )
    return cur.fetchone()[0]


def _parse_email_payload(content_str: str) -> dict | None:
    """Try to parse the JSON email payload stored in Mail Message events."""
    if not content_str:
        return None
    try:
        data = json.loads(content_str)
        if isinstance(data, dict) and (data.get("from") or data.get("subject")):
            return data
    except (json.JSONDecodeError, TypeError):
        return None
    return None


def _download_history_attachments(
    history: list[dict], opp_map: dict[str, int],
    portal_url: str | None,
    skip: bool = False,
) -> dict[tuple[int, int], list[dict]]:
    """Pre-pass: build {(opp_v3_id, crm_event_id): [file_item, ...]} map for downloads.

    Returns a dict mapping (opp_v3_id, crm_event_id) -> list of File-Item dicts.
    If skip=True, returns empty dict.
    if portal_url is None, returns empty dict.
    """
    if skip or not portal_url:
        return {}
    print("Pre-fetching attachment metadata for downloads...")
    by_key: dict[tuple[int, int], list[dict]] = {}
    for h in history:
        if not isinstance(h, dict):
            continue
        crm_event_id = int(h.get("id") or 0)
        crm_entity_id = int(h.get("entityId") or 0)
        if not crm_event_id or not crm_entity_id:
            continue
        local_opp_id = opp_map.get(str(crm_entity_id))
        if not local_opp_id:
            continue
        files = h.get("files") or []
        if not isinstance(files, list) or not files:
            continue
        for f in files:
            if isinstance(f, dict) and f.get("id"):
                by_key.setdefault((local_opp_id, crm_event_id), []).append(f)
    print(f"  Found {sum(len(v) for v in by_key.values())} attachment files across {len(by_key)} events")
    return by_key


def _download_one_file(
    portal_url: str, crm_file_id: int, opp_local_id: int, crm_event_id: int,
) -> tuple[bytes | None, str | None]:
    """Download a single OnlyOffice file. Returns (bytes, error_msg)."""
    url = f"{portal_url.rstrip('/')}/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={crm_file_id}"
    raw = _download_bytes(url, timeout=60)
    if raw is None:
        return None, "download failed"
    return raw, None


def _insert_history(
    conn, history: list[dict], opp_map: dict[str, int], user_map: dict[str, int],
    portal_url: str | None = None, download_attachments: bool = True,
) -> tuple[int, int]:
    """Insert history events. Returns (events_count, attachments_downloaded_count).

    Handles Mail Message JSON payloads, author mapping, and downloads file
    attachments into data/history-attachments/<opp_id>/<event_id>/<safe_name>.
    """
    if not history:
        return 0, 0
    cur = conn.cursor()
    count = 0
    attachments_inserted = 0
    skipped_no_opp = 0

    # Pre-pass: collect attachment metadata for downloading
    attachments_by_key = _download_history_attachments(history, opp_map, portal_url, skip=not download_attachments)

    for h in history:
        if not isinstance(h, dict):
            continue
        opp_data = h.get("opportunity") or {}
        opp_crm_id = ""
        if isinstance(opp_data, dict):
            opp_crm_id = str(opp_data.get("id") or "")
        if not opp_crm_id:
            opp_crm_id = str(h.get("entityId") or h.get("opportunityId") or "")
        opp_id = opp_map.get(opp_crm_id)
        if not opp_id:
            skipped_no_opp += 1
            continue

        # Category
        cat_data = h.get("category") or {}
        cat_title = str(cat_data.get("title") or "Note").strip() if isinstance(cat_data, dict) else "Note"
        cat_id = _resolve_category(cur, cat_title, cat_data if isinstance(cat_data, dict) else {})

        # Content
        raw_content = h.get("content") or h.get("Content") or ""
        if isinstance(raw_content, str):
            content = raw_content.strip() or None
        elif isinstance(raw_content, dict):
            content = json.dumps(raw_content, default=str)
        else:
            content = str(raw_content) if raw_content else None

        created_raw = h.get("created") or h.get("createOn") or h.get("Created") or h.get("date") or None

        # Author mapping
        author_data = h.get("createBy") or h.get("CreateBy") or h.get("createdBy") or {}
        crm_author_id = str(author_data.get("id") or "") if isinstance(author_data, dict) else ""
        author_id = user_map.get(crm_author_id)

        crm_event_id = int(h.get("id") or 0) or None
        crm_entity_id = int(h.get("entityId") or 0) or None

        cur.execute(
            """INSERT INTO history_events
                   (opportunity_id, category_id, content, created_by, created_at, backdated_created_at,
                    crm_event_id, crm_entity_id)
               VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (opp_id, cat_id, content, author_id, created_raw, created_raw,
             crm_event_id, crm_entity_id),
        )
        row = cur.fetchone()
        if not row:
            # Already imported (no PK conflict possible but crm_event_id might dup).
            # Skip without raising.
            continue
        event_id = row[0]
        count += 1

        # Download + record attachments
        files_to_download = attachments_by_key.get((opp_id, crm_event_id)) if crm_event_id else None
        if files_to_download:
            for f_idx, f_item in enumerate(files_to_download):
                if not isinstance(f_item, dict):
                    continue
                f_id = int(f_item.get("id") or 0)
                f_title = str(f_item.get("title") or f"file_{f_id}").strip()
                if not f_id:
                    continue
                file_size = int(f_item.get("pureContentLength") or f_item.get("size") or 0)
                file_ext = (Path(f_title).suffix.lstrip(".") or "").lower()
                mime_type = _guess_mime(file_ext)
                view_url = f_item.get("viewUrl") or f_item.get("webUrl") or ""

                # Local storage path — use crm_file_id to guarantee uniqueness
                # (in case multiple attachments in the same event have the same title)
                rel_dir = Path("data") / "history-attachments" / str(opp_id) / str(event_id)
                safe_name = _safe_filename(f_title)
                unique_name = f"{f_id}_{safe_name}"
                local_path = Path(__file__).resolve().parent / rel_dir / unique_name
                local_path.parent.mkdir(parents=True, exist_ok=True)

                if portal_url:
                    raw, err = _download_one_file(portal_url, f_id, opp_id, crm_event_id)
                    if raw:
                        local_path.write_bytes(raw)
                        actual_size = len(raw)
                    else:
                        actual_size = file_size
                else:
                    actual_size = file_size

                try:
                    cur.execute(
                        """INSERT INTO history_attachments
                               (event_id, filename, file_path, file_size, mime_type, uploaded_by,
                                original_url, crm_file_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (event_id, crm_file_id) WHERE crm_file_id IS NOT NULL DO NOTHING""",
                        (event_id, f_title, str(rel_dir / unique_name), actual_size, mime_type,
                         author_id, view_url, f_id),
                    )
                    if cur.rowcount:
                        attachments_inserted += 1
                except Exception as exc:
                    print(f"    [warn] attachment insert failed for {f_id}: {exc}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    cur = conn.cursor()

    conn.commit()
    print(f"  history events: {count} (skipped no-opp: {skipped_no_opp}), attachments downloaded+recorded: {attachments_inserted}")
    return count, attachments_inserted


def _guess_mime(ext: str) -> str:
    m = {
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
    return m.get(ext.lower(), "application/octet-stream")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import JSON export from OnlyOffice into Sietch CRM")
    parser.add_argument("--source", required=True, help="Path to directory containing users.json, opportunities.json, ...")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--portal-url", default=None, help="OnlyOffice base URL (required to download attachments/avatars)")
    parser.add_argument("--dry-run", action="store_true", help="Validate files only, no DB writes")
    parser.add_argument("--skip-attachment-download", action="store_true", help="Don't download file attachments or avatars")
    args = parser.parse_args()

    src = Path(args.source)
    if not src.is_dir():
        sys.exit(f"Source dir not found: {src}")

    print(f"Loading export from {src}...")
    users = _load_json(src / "users.json") or []
    stages = _load_json(src / "stages.json") or []
    tags = _load_json(src / "tags.json") or []
    custom_fields = _load_json(src / "custom_fields.json") or []
    contacts = _load_json(src / "contacts.json") or []
    opps = _load_json(src / "opportunities.json") or []
    tasks = _load_json(src / "tasks.json") or []
    task_labels = _load_json(src / "task_labels.json") or []
    history = _load_json(src / "history.json") or []

    print(f"  users: {len(users)}, stages: {len(stages)}, tags: {len(tags)}, custom_fields: {len(custom_fields)},")
    print(f"  contacts: {len(contacts)}, opps: {len(opps)}, tasks: {len(tasks)}, task_labels: {len(task_labels)},")
    print(f"  history: {len(history)}")

    if args.dry_run:
        print("Dry run — not writing to DB.")
        return

    conn = _db_conn(args)
    conn.autocommit = False

    download_avatars = bool(args.portal_url) and not args.skip_attachment_download
    download_atts = bool(args.portal_url) and not args.skip_attachment_download

    try:
        print("\nImporting users...")
        user_map = _insert_users(conn, users)

        print("\nImporting stages...")
        stage_map = _insert_stages(conn, stages)

        print("\nImporting tags...")
        tag_map = _insert_tags(conn, tags)

        print("\nImporting custom field definitions...")
        cf_map = _insert_custom_fields(conn, custom_fields)

        print("\nImporting task labels...")
        _insert_task_labels(conn, task_labels)

        print("\nImporting contacts...")
        contact_map = _insert_contacts(conn, contacts, user_map,
                                       portal_url=args.portal_url,
                                       download_avatars=download_avatars)
        _link_contacts_to_companies(conn, contacts, contact_map)

        print("\nImporting opportunities...")
        opp_map = _insert_opportunities(conn, opps, stage_map, contact_map, user_map, tag_map, cf_map)

        print("\nImporting tasks...")
        _insert_tasks(conn, tasks, opp_map, user_map)

        print("\nImporting history events + attachments...")
        hist_count, att_count = _insert_history(
            conn, history, opp_map, user_map,
            portal_url=args.portal_url,
            download_attachments=download_atts,
        )

        conn.commit()
        print("\n" + "=" * 60)
        print("Import complete.")
        print(f"  Users:        {len(user_map)}")
        print(f"  Stages:       {len(stage_map)}")
        print(f"  Tags:         {len(tag_map)}")
        print(f"  Custom flds:  {len(cf_map)}")
        print(f"  Contacts:     {len(contact_map)}")
        print(f"  Opportunities:{len(opp_map)}")
        print(f"  History:      {hist_count}")
        print(f"  Attachments:  {att_count}")
        print("=" * 60)
        if not args.portal_url:
            print("\nNote: --portal-url not provided. History attachments and contact avatars")
            print("      were NOT downloaded (metadata only). Re-run with --portal-url to fetch.")
        else:
            print(f"\nAttachments saved to: {HISTORY_ATTACHMENT_DIR.relative_to(Path.cwd()) if HISTORY_ATTACHMENT_DIR.is_absolute() else HISTORY_ATTACHMENT_DIR}")
    except Exception as exc:
        conn.rollback()
        print(f"\n❌ Import failed (rolled back): {exc}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()