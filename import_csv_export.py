#!/usr/bin/env python3
"""Import OnlyOffice CRM CSV export into Sietch CRM PostgreSQL database.

The CSV export (from OnlyOffice CRM → Export data) contains:
  - Contacts_*.csv
  - Opportunities_*.csv
  - Tasks_*.csv
  - History_*.csv

This script imports them into the new PostgreSQL schema. It creates placeholder users
for authors/responsibles found in the CSV (since the CSV export does not include the
Users table) and maps them by display name.

Usage:
    python3 import_csv_export.py --source CRM_export/CRMExport_639200290145865960

For user mapping, create a JSON file:
    { "Ken Chapman": {"email": "kenc@example.com", "isAdmin": true}, ... }
    python3 import_csv_export.py --source ... --user-map user_map.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not found. Install with: pip install psycopg2-binary")


# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_USER_DOMAIN = "imported.local"


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
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


# ── DB helpers ───────────────────────────────────────────────────────────────────

def _db_conn(args: argparse.Namespace) -> Any:
    return psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )


# ── Parsing helpers ──────────────────────────────────────────────────────────────

def _parse_datetime(raw: str) -> datetime | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name).strip()


def _name_to_email(name: str, domain: str = DEFAULT_USER_DOMAIN) -> str:
    clean = re.sub(r"[^\w]", "_", name.strip()).lower()
    return f"{clean}@{domain}"


def _extract_opportunity_title(associated: str) -> str | None:
    if not associated:
        return None
    m = re.search(r"Opportunity:\s*(.+)", associated)
    if m:
        return m.group(1).strip()
    return None


# ── User management ──────────────────────────────────────────────────────────────

def _load_user_map(conn: Any, user_map_file: Path | None) -> dict[str, dict[str, Any]]:
    """Return display_name -> {id, email, is_admin} from DB + optional JSON file."""
    cur = conn.cursor()
    cur.execute("SELECT id, email, display_name, is_admin FROM users")
    by_name: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        user_id, email, display_name, is_admin = row
        if display_name:
            by_name[display_name.strip()] = {"id": user_id, "email": email, "is_admin": is_admin}
    cur.close()

    if user_map_file and user_map_file.is_file():
        data = json.loads(user_map_file.read_text(encoding="utf-8"))
        # Don't override existing DB users by name
        by_name.update({k: v for k, v in data.items() if k not in by_name})
    return by_name


def _get_or_create_user(conn: Any, display_name: str, by_name: dict[str, dict[str, Any]]) -> int:
    if not display_name:
        display_name = "Unknown"
    name = display_name.strip()
    if name in by_name and "id" in by_name[name]:
        return by_name[name]["id"]

    email = by_name.get(name, {}).get("email") or _name_to_email(name)
    is_admin = bool(by_name.get(name, {}).get("isAdmin", False))

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO users (email, display_name, is_admin, role, is_active, must_change_password)
           VALUES (%s, %s, %s, CASE WHEN %s THEN 'admin' ELSE 'member' END, TRUE, TRUE)
           ON CONFLICT (email) DO UPDATE SET
               display_name = EXCLUDED.display_name,
               role = CASE WHEN EXCLUDED.is_admin THEN 'admin' ELSE 'member' END
           RETURNING id""",
        (email, name, is_admin, is_admin),
    )
    user_id = cur.fetchone()[0]
    cur.close()
    by_name[name] = {"id": user_id, "email": email, "is_admin": is_admin}
    return user_id


# ── Importers ────────────────────────────────────────────────────────────────────

def _import_contacts(conn: Any, source: Path) -> int:
    files = sorted(source.glob("Contacts_*.csv"))
    if not files:
        print("  [skip] No Contacts CSV found")
        return 0
    count = 0
    cur = conn.cursor()
    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                first = str(row.get("First Name") or "").strip()
                last = str(row.get("Last Name") or "").strip()
                company = str(row.get("Company Name") or "").strip()
                email = str(row.get("Email (work") or "").strip()
                if not email:
                    email = str(row.get("Email (home") or "").strip()
                if not email:
                    email = str(row.get("Email (other") or "").strip()
                phone = str(row.get("Phone (work") or "").strip()
                if not phone:
                    phone = str(row.get("Phone (mobile") or "").strip()
                cur.execute(
                    """INSERT INTO contacts (first_name, last_name, company, email, phone)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id""",
                    (first or None, last or None, company or None, email or None, phone or None),
                )
                cur.fetchone()
                count += 1
    conn.commit()
    cur.close()
    print(f"  → {count} contacts imported")
    return count


def _load_contacts_map(conn: Any) -> dict[str, int]:
    """Return name -> contact_id for matching opportunities."""
    cur = conn.cursor()
    cur.execute("SELECT id, first_name, last_name, company FROM contacts")

    mapping: dict[str, int] = {}
    for row in cur.fetchall():
        cid, first, last, company = row
        names = []
        if first and last:
            names.append(f"{first} {last}")
        if company:
            names.append(company)
        for name in names:
            mapping[name.strip()] = cid
    cur.close()
    return mapping


def _get_or_create_stage(conn: Any, title: str, stage_type: int = 0) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM stages WHERE title = %s", (title,))
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]
    cur.execute(
        """INSERT INTO stages (title, color, sort_order, stage_type, probability, is_active)
           VALUES (%s, %s, 0, %s, 0, TRUE)
           RETURNING id""",
        (title, "#999999", stage_type),
    )
    stage_id = cur.fetchone()[0]
    cur.close()
    return stage_id


def _import_opportunities(conn: Any, source: Path, by_name: dict[str, dict[str, Any]]) -> tuple[int, dict[str, int]]:
    files = sorted(source.glob("Opportunities_*.csv"))
    if not files:
        print("  [skip] No Opportunities CSV found")
        return 0, {}
    contacts_map = _load_contacts_map(conn)
    cur = conn.cursor()
    title_to_id: dict[str, int] = {}
    seen_titles: set[str] = set()
    count = 0
    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = str(row.get("Opportunity title") or "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                contact_name = str(row.get("Opportunity contact") or "").strip()
                contact_id = contacts_map.get(contact_name)
                stage_title = str(row.get("Opportunity at stage") or "").strip()
                stage_id = _get_or_create_stage(conn, stage_title or "Open") if stage_title else None
                responsible_name = str(row.get("Responsible for the opportunity") or "").strip()
                responsible_id = _get_or_create_user(conn, responsible_name, by_name) if responsible_name else None
                due = _parse_datetime(row.get("Estimated deal due date"))
                bid_value = None
                try:
                    bid_value = float(row.get("Sum") or 0)
                except (ValueError, TypeError):
                    pass
                cur.execute(
                    """INSERT INTO opportunities (title, description, stage_id, stage_type, bid_value,
                           expected_close_date, contact_id, responsible_user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (title, str(row.get("Description") or "").strip() or None,
                     stage_id, 0, bid_value, due, contact_id, responsible_id),
                )
                row_result = cur.fetchone()
                if row_result:
                    title_to_id[title] = row_result[0]
                    count += 1
    conn.commit()
    cur.close()
    print(f"  → {count} opportunities imported")
    return count, title_to_id


def _import_tasks(conn: Any, source: Path, opp_title_to_id: dict[str, int], by_name: dict[str, dict[str, Any]]) -> int:
    files = sorted(source.glob("Tasks_*.csv"))
    if not files:
        print("  [skip] No Tasks CSV found")
        return 0
    count = 0
    cur = conn.cursor()
    seen_tasks: set[tuple[str, str | None]] = set()
    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = str(row.get("Task title") or "").strip()
                if not title:
                    continue
                opp_title = _extract_opportunity_title(str(row.get("Associated identity") or ""))
                opp_id = opp_title_to_id.get(opp_title) if opp_title else None
                key = (title, opp_id)
                if key in seen_tasks:
                    continue
                seen_tasks.add(key)
                responsible_name = str(row.get("Responsible") or "").strip()
                responsible_id = _get_or_create_user(conn, responsible_name, by_name) if responsible_name else None
                due = _parse_datetime(row.get("Due date"))
                is_closed = str(row.get("Task status") or "").strip().lower() == "closed"
                cur.execute(
                    """INSERT INTO tasks (title, description, opportunity_id, responsible_user_id, due_date, is_closed)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (title, str(row.get("Description") or "").strip() or None,
                     opp_id, responsible_id, due, is_closed),
                )
                cur.fetchone()
                count += 1
    conn.commit()
    cur.close()
    print(f"  → {count} tasks imported")
    return count


def _get_or_create_category(conn: Any, title: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM history_categories WHERE title = %s", (title,))
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]
    cur.execute(
        "INSERT INTO history_categories (title, is_system) VALUES (%s, TRUE) RETURNING id",
        (title,),
    )
    cat_id = cur.fetchone()[0]
    cur.close()
    return cat_id


def _import_history(conn: Any, source: Path, opp_title_to_id: dict[str, int], by_name: dict[str, dict[str, Any]]) -> int:
    files = sorted(source.glob("History_*.csv"))
    if not files:
        print("  [skip] No History CSV found")
        return 0
    count = 0
    cur = conn.cursor()
    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                content = str(row.get("Content") or "").strip()
                if not content:
                    continue
                category_title = str(row.get("Category") or "Note").strip()
                cat_id = _get_or_create_category(conn, category_title)
                author_name = str(row.get("Author") or "").strip()
                author_id = _get_or_create_user(conn, author_name, by_name) if author_name else None
                opp_title = _extract_opportunity_title(str(row.get("Associated identity") or ""))
                opp_id = opp_title_to_id.get(opp_title) if opp_title else None
                created = _parse_datetime(row.get("Creation date"))
                if not opp_id:
                    continue
                cur.execute(
                    """INSERT INTO history_events (opportunity_id, category_id, content, created_by, created_at)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id""",
                    (opp_id, cat_id, content, author_id, created or datetime.now()),
                )
                cur.fetchone()
                count += 1
    conn.commit()
    cur.close()
    print(f"  → {count} history events imported")
    return count


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import OnlyOffice CRM CSV export into Sietch CRM")
    parser.add_argument("--source", required=True, help="Path to CRM export folder")
    parser.add_argument("--user-map", help="Optional JSON file mapping display names to user info")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    args = parser.parse_args()

    source_dir = Path(args.source).resolve()
    if not source_dir.is_dir():
        sys.exit(f"Source directory not found: {source_dir}")

    user_map_file = Path(args.user_map) if args.user_map else None

    print(f"Connecting to PostgreSQL at {args.db_host}:{args.db_port}/{args.db_name}...")
    conn = _db_conn(args)
    conn.autocommit = False

    by_name = _load_user_map(conn, user_map_file)
    print(f"Loaded {len(by_name)} user mappings.")

    print("\n[Importing contacts]")
    _import_contacts(conn, source_dir)

    print("\n[Importing opportunities]")
    opp_count, opp_title_to_id = _import_opportunities(conn, source_dir, by_name)

    print("\n[Importing tasks]")
    _import_tasks(conn, source_dir, opp_title_to_id, by_name)

    print("\n[Importing history]")
    _import_history(conn, source_dir, opp_title_to_id, by_name)

    conn.close()
    print("\nCSV import complete.")


if __name__ == "__main__":
    main()
