#!/usr/bin/env python3
"""One-time script to backfill contact data from OnlyOffice CRM export.

Reads CRM_export/contacts.json and enriches the local contacts table with:
- email, phone, website (from commonData array)
- address fields (from addresses array)
- about, industry, currency, contact_status
- avatar_url
- onlyoffice_id
- company_id linkage for persons

Run AFTER the init.sql migration (ALTER TABLE contacts ADD COLUMN ...).
Idempotent: skips contacts that already have onlyoffice_id set.

Usage:
    cd /home/chapman-server/Projects/new-crm
    DB_HOST=127.0.0.1 ./.venv/bin/python3 scripts/enrich_contacts.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def connect_db():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sietch_crm"),
        user=os.getenv("DB_USER", "sietch"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def extract_primary(common_data: list, info_type: int) -> str | None:
    """Extract primary value from commonData array by infoType."""
    for item in common_data:
        if item.get("infoType") == info_type and item.get("isPrimary"):
            return str(item.get("data") or "").strip() or None
    # Fallback: any item of this type
    for item in common_data:
        if item.get("infoType") == info_type:
            return str(item.get("data") or "").strip() or None
    return None


def extract_primary_address(addresses: list) -> dict:
    """Extract primary address from addresses array."""
    for addr in addresses:
        if addr.get("isPrimary"):
            return addr
    # Fallback: first address
    return addresses[0] if addresses else {}


def main():
    export_path = Path(__file__).resolve().parent.parent / "CRM_export" / "contacts.json"
    if not export_path.exists():
        print(f"Error: {export_path} not found")
        sys.exit(1)

    with open(export_path, "r", encoding="utf-8") as f:
        oo_contacts = json.load(f)

    print(f"Loaded {len(oo_contacts)} OnlyOffice contacts from {export_path}")

    conn = connect_db()
    cur = conn.cursor()

    # Build name → id map for existing contacts
    cur.execute("SELECT id, first_name, last_name, company, onlyoffice_id FROM contacts")
    existing = {}
    for row in cur.fetchall():
        cid, first, last, company, oo_id = row
        if oo_id:
            existing[f"oo:{oo_id}"] = cid
        name_key = f"{(first or '').strip().lower()} {(last or '').strip().lower()}".strip()
        if name_key:
            existing[name_key] = cid
        if company:
            existing[f"company:{company.strip().lower()}"] = cid

    enriched = 0
    skipped = 0
    errors = 0

    for oo in oo_contacts:
        if not isinstance(oo, dict):
            continue

        oo_id = str(oo.get("id") or "")
        is_company = bool(oo.get("isCompany", False))

        # Find existing contact
        local_id = None
        if oo_id:
            local_id = existing.get(f"oo:{oo_id}")

        if local_id is None:
            # Try name match
            if is_company:
                company_name = str(oo.get("companyName") or "").strip().lower()
                local_id = existing.get(f"company:{company_name}")
            else:
                first = str(oo.get("firstName") or "").strip().lower()
                last = str(oo.get("lastName") or "").strip().lower()
                local_id = existing.get(f"{first} {last}".strip())

        if local_id is None:
            print(f"  [skip] No matching contact for OO id={oo_id} ({oo.get('displayName', '?')})")
            skipped += 1
            continue

        # Skip if already enriched
        cur.execute("SELECT onlyoffice_id FROM contacts WHERE id = %s", (local_id,))
        row = cur.fetchone()
        if row and row[0]:
            skipped += 1
            continue

        # Extract data
        common_data = oo.get("commonData") or []
        addresses = oo.get("addresses") or []

        email = extract_primary(common_data, 1)  # infoType 1 = Email
        phone = extract_primary(common_data, 0)  # infoType 0 = Phone
        website = extract_primary(common_data, 2)  # infoType 2 = Website

        addr = extract_primary_address(addresses)
        addr_street = str(addr.get("street") or "").strip() or None
        addr_city = str(addr.get("city") or "").strip() or None
        addr_state = str(addr.get("state") or "").strip() or None
        addr_zip = str(addr.get("zip") or "").strip() or None
        addr_country = str(addr.get("country") or "").strip() or None

        about = str(oo.get("about") or "").strip() or None
        industry = str(oo.get("industry") or "").strip() or None
        currency = str(oo.get("currency") or "").strip() or None

        contact_status = None
        cs = oo.get("contactStatus")
        if isinstance(cs, dict):
            contact_status = str(cs.get("title") or "").strip() or None

        avatar_url = str(oo.get("smallFotoUrl") or "").strip() or None

        # Build update
        sets = []
        params = []

        if oo_id:
            sets.append("onlyoffice_id = %s")
            params.append(oo_id)

        if email:
            sets.append("email = COALESCE(email, %s)")
            params.append(email)
        if phone:
            sets.append("phone = COALESCE(phone, %s)")
            params.append(phone)
        if website:
            sets.append("website = %s")
            params.append(website)
        if about:
            sets.append("about = COALESCE(about, %s)")
            params.append(about)
        if industry:
            sets.append("industry = %s")
            params.append(industry)
        if currency:
            sets.append("currency = %s")
            params.append(currency)
        if contact_status:
            sets.append("contact_status = %s")
            params.append(contact_status)
        if avatar_url:
            sets.append("avatar_url = %s")
            params.append(avatar_url)
        if addr_street:
            sets.append("address_street = COALESCE(address_street, %s)")
            params.append(addr_street)
        if addr_city:
            sets.append("address_city = COALESCE(address_city, %s)")
            params.append(addr_city)
        if addr_state:
            sets.append("address_state = COALESCE(address_state, %s)")
            params.append(addr_state)
        if addr_zip:
            sets.append("address_zip = COALESCE(address_zip, %s)")
            params.append(addr_zip)
        if addr_country:
            sets.append("address_country = %s")
            params.append(addr_country)

        if sets:
            params.append(local_id)
            cur.execute(
                f"UPDATE contacts SET {', '.join(sets)} WHERE id = %s",
                (*params,),
            )
            enriched += 1
            print(f"  [enriched] id={local_id} OO={oo_id} ({oo.get('displayName', '?')})")

    # Second pass: resolve company_id for persons
    print("\nResolving company linkages...")
    cur.execute("SELECT id, first_name, last_name, company, onlyoffice_id FROM contacts WHERE is_company = FALSE AND company_id IS NULL")
    persons = cur.fetchall()

    # Rebuild oo_id → local map
    cur.execute("SELECT id, onlyoffice_id, company FROM contacts WHERE onlyoffice_id IS NOT NULL")
    oo_map = {}
    company_name_map = {}
    for row in cur.fetchall():
        cid, oo_id, company_name = row
        if oo_id:
            oo_map[str(oo_id)] = cid
        if company_name:
            company_name_map[company_name.strip().lower()] = cid

    linked = 0
    for person_row in persons:
        pid, first, last, company_name, person_oo_id = person_row

        # Find the OO data for this person
        oo_person = None
        for oo in oo_contacts:
            if isinstance(oo, dict) and str(oo.get("id")) == str(person_oo_id):
                oo_person = oo
                break

        if not oo_person:
            continue

        # Person has a company sub-object with id
        company_obj = oo_person.get("company")
        if isinstance(company_obj, dict):
            comp_oo_id = str(company_obj.get("id") or "")
            comp_local_id = oo_map.get(comp_oo_id)
            if comp_local_id:
                cur.execute("UPDATE contacts SET company_id = %s WHERE id = %s", (comp_local_id, pid))
                linked += 1
                print(f"  [linked] person id={pid} → company_id={comp_local_id}")

    conn.commit()
    cur.close()
    conn.close()

    print(f"\nDone: {enriched} enriched, {linked} companies linked, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
