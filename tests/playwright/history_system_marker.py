#!/usr/bin/env python3
"""Playwright regression: a field-change history row is a compact one-liner
even when it has NO category (the prod "Quick Context" / Reply-button bug).

Live bug (Ken, 2026-11-10 8:27 PM ET): "Follow-up Due Date changed from
2026-11-11 to 2026-11-12" rendered as a full card with a Reply button. The row
(prod event 50504) had `category_id = NULL` because the field-change writer
looks up the synthetic "Action" category, whose seed row never landed on prod.

This test reproduces that exact condition on a local scratch DB:
  1. unique scratch database (never reuses/drops another worktree's DB), loads
     `init.sql`; the server applies `migrations/` on boot;
  2. deletes the `Action` category after boot so the tracked-field writer
     inserts a NULL category — the production shape;
  3. seeds a user + one project with a Follow-up Due Date;
  4. starts the app on 127.0.0.1 with that scratch DB;
  5. logs in, opens the project preview, changes the Follow-up Due Date in the
     preview panel, and asserts the new history entry is the compact one-line
     treatment (no Reply, no Delete);
  6. adds a typed "Quick Context" human note and asserts it is still a full
     card WITH the Reply button;
  7. asserts the history API reports `isSystemEvent` true/false accordingly.

Any request to a non-localhost host is aborted at the browser route layer.

Usage:
    python3 tests/playwright/history_system_marker.py

Env overrides:
    PW_DB_HOST / PW_DB_PORT / PW_DB_USER / PW_DB_PASSWORD (default 127.0.0.1:5432 sietch)
    PW_PORT (default 8923), PW_KEEP_DB=1 to keep the scratch DB.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DB_HOST = os.getenv("PW_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("PW_DB_PORT", "5432"))
DB_USER = os.getenv("PW_DB_USER", "sietch")
DB_PASSWORD = os.getenv("PW_DB_PASSWORD", "local_dev_password")
PORT = int(os.getenv("PW_PORT", "8923"))
BASE = f"http://127.0.0.1:{PORT}"
DB_NAME = os.getenv("PW_DB_NAME", f"sietch_pw_histmarker_{os.getpid()}")
KEEP_DB = os.getenv("PW_KEEP_DB", "") == "1"

ALLOWED_BLOCKED_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.jsdelivr.net",
)

USER_EMAIL = "histmarker@example.invalid"
USER_PASSWORD = "histmarker-test-pw"
PROJECT_TITLE = "History Marker Fixture Project"
HUMAN_NOTE = "Playwright quick context note (human typed, no marker)"
INITIAL_DUE = "2026-12-01"
NEW_DUE = "2026-12-15"


def _connect(dbname):
    import psycopg2

    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=dbname,
                            user=DB_USER, password=DB_PASSWORD, connect_timeout=10)
    conn.autocommit = True
    return conn


def _exec_script(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)


def bootstrap_db():
    if not DB_NAME.startswith("sietch_pw_histmarker_"):
        raise SystemExit(
            f"refusing DB name {DB_NAME!r}: must start with 'sietch_pw_histmarker_' "
            "(never point this at a real/other-worktree DB)")
    admin = _connect("postgres")
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if cur.fetchone():
            cur.execute(f'DROP DATABASE "{DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{DB_NAME}" OWNER "{DB_USER}"')
    admin.close()

    scratch = _connect(DB_NAME)
    _exec_script(scratch, (ROOT / "init.sql").read_text(encoding="utf-8"))

    from auth import hash_password

    pw_hash, pw_salt = hash_password(USER_PASSWORD)
    with scratch.cursor() as cur:
        cur.execute(
            """INSERT INTO users (email, display_name, first_name, last_name,
                                  password_hash, password_salt, is_admin, is_active,
                                  must_change_password)
               VALUES (%s,%s,%s,%s,%s,%s,FALSE,TRUE,FALSE) RETURNING id""",
            (USER_EMAIL, "Hist Marker", "Hist", "Marker", pw_hash, pw_salt))
        uid = cur.fetchone()[0]
        cur.execute("INSERT INTO contacts (first_name,last_name,company) "
                    "VALUES ('Bonnie','Gosz','Gosz Roofing') RETURNING id")
        cid = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO opportunities
                   (title, contact_id, stage_id, stage_type, expected_close_date,
                    created_by, responsible_user_id)
               VALUES (%s,%s,(SELECT id FROM stages ORDER BY sort_order LIMIT 1),0,%s,%s,%s)
               RETURNING id""",
            (PROJECT_TITLE, cid, INITIAL_DUE, uid, uid))
        opp_id = cur.fetchone()[0]
    scratch.close()
    return opp_id


def drop_action_category():
    """Reproduce prod: migration 017 is recorded as applied but its seed row is
    absent, so `_handle_project_update` inserts category_id = NULL."""
    scratch = _connect(DB_NAME)
    with scratch.cursor() as cur:
        cur.execute("DELETE FROM history_categories WHERE lower(title) = 'action'")
        cur.execute("SELECT COUNT(*) FROM history_categories WHERE lower(title) = 'action'")
        remaining = cur.fetchone()[0]
    scratch.close()
    assert remaining == 0, "failed to remove the Action category for the repro"


def _assert_port_free():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise SystemExit(f"port {PORT} is already in use by another process ({exc}); "
                             "set PW_PORT to a free port")
    finally:
        sock.close()


def start_server():
    _assert_port_free()
    env = os.environ.copy()
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_PUBLIC_URL"):
        env.pop(key, None)
    env.update({
        "DB_HOST": DB_HOST, "DB_PORT": str(DB_PORT), "DB_NAME": DB_NAME,
        "DB_USER": DB_USER, "DB_PASSWORD": DB_PASSWORD,
        "PORT": str(PORT), "CRM_PUBLIC_URL": BASE, "DASHBOARD_URL": BASE,
        "PYTHONPATH": str(ROOT),
    })
    log_path = Path("/tmp/opencode/pw_histmarker_server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"server exited early ({proc.returncode}); see {log_path}")
        try:
            with urllib.request.urlopen(f"{BASE}/api/config", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"server did not become ready; see {log_path}")


def run_browser(opp_id):
    from playwright.sync_api import sync_playwright

    blocked = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()

        def route_handler(route):
            url = route.request.url
            if url.startswith(BASE) or url.startswith("data:") or url.startswith("blob:"):
                route.continue_()
                return
            blocked.append(url)
            route.abort()

        page.route("**/*", route_handler)
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.fill("#username", USER_EMAIL)
        page.fill("#password", USER_PASSWORD)
        page.click("#login-submit-btn")
        page.wait_for_selector("#app:not(.hidden)", timeout=20000)

        # "What's New" opens shortly after login and intercepts clicks.
        for _ in range(20):
            if page.query_selector("#changelog-modal:not(.hidden)"):
                page.click("#changelog-close")
                page.wait_for_selector("#changelog-modal", state="hidden", timeout=5000)
                break
            page.wait_for_timeout(250)

        # Open the project preview from the default "Open pipeline" group.
        card_link = page.locator(".card-title-link", has_text=PROJECT_TITLE).first
        card_link.wait_for(timeout=20000)
        card_link.click()
        page.wait_for_selector("#opp-preview-modal:not(.hidden)", timeout=20000)
        page.wait_for_selector(".opp-preview-due-date-display", timeout=20000)

        # Change the tracked field exactly like a user: click the display, pick
        # a date, blur → PATCH /api/v2/projects/{id} → preview refresh.
        page.click(".opp-preview-due-date-display")
        page.fill(".opp-preview-due-date-input", NEW_DUE)
        page.press(".opp-preview-due-date-input", "Tab")

        expected_change = f"Follow-up Due Date changed from {INITIAL_DUE} to {NEW_DUE}"
        sys_row = page.locator(".opp-preview-history-item--system",
                               has_text="Follow-up Due Date changed from")
        sys_row.first.wait_for(timeout=20000)

        text = sys_row.first.text_content() or ""
        assert expected_change in text, f"compact row text = {text!r}"
        assert sys_row.first.locator(".opp-preview-history-system-row").count() == 1, \
            "expected the one-line system row"
        assert sys_row.first.locator(".opp-preview-history-system-time").count() == 1, \
            "expected the timestamp inside the compact row"
        assert sys_row.first.locator("button.opp-preview-history-reply-btn").count() == 0, \
            "a system event must NOT render a Reply button"
        assert sys_row.first.locator("button.opp-preview-history-delete").count() == 0, \
            "a system event must NOT render a delete button"

        # The same row must be reported by the API as a system event.
        resp = page.request.get(BASE + f"/api/v2/projects/{opp_id}/history")
        assert resp.ok, f"GET history -> {resp.status}"
        events = resp.json()
        changed = [e for e in events
                   if str(e.get("content") or "").startswith("Follow-up Due Date changed from")]
        assert changed, f"field-change event missing from API: {[e.get('content') for e in events]}"
        assert changed[0].get("isSystemEvent") is True, \
            f"field-change row must carry isSystemEvent=true: {changed[0]}"
        assert changed[0].get("categoryId") is None, \
            "repro precondition: the field change must have NO category (prod shape)"

        # A typed human note on the SAME project must stay a full card.
        page.click(".opp-preview-section-add-note")
        page.select_option(".preview-note-type-select", label="Quick Context")
        page.fill(".preview-note-editor", HUMAN_NOTE)
        page.click(".preview-note-submit")
        page.wait_for_selector(".opp-preview-history-item:not(.opp-preview-history-item--system)",
                               timeout=20000)

        human = page.locator(".opp-preview-history-item", has_text=HUMAN_NOTE).first
        human.wait_for(timeout=20000)
        assert "opp-preview-history-item--system" not in (human.get_attribute("class") or ""), \
            "a human Quick Context note must NOT be compact"
        assert human.locator("button.opp-preview-history-reply-btn").count() == 1, \
            "a human Quick Context note must keep its Reply button"

        resp = page.request.get(BASE + f"/api/v2/projects/{opp_id}/history")
        assert resp.ok, f"GET history -> {resp.status}"
        events = resp.json()
        human_ev = [e for e in events if HUMAN_NOTE in str(e.get("content") or "")]
        assert human_ev, "human note missing from API"
        assert human_ev[0].get("isSystemEvent") is False, \
            f"human note must not be marked: {human_ev[0]}"
        assert (human_ev[0].get("category") or {}).get("title") == "Quick Context", \
            f"human note category = {human_ev[0].get('category')!r}"

        browser.close()

    unexpected = [u for u in blocked
                  if not any(host in u for host in ALLOWED_BLOCKED_HOSTS)]
    assert not unexpected, f"non-localhost requests were attempted: {unexpected}"
    print(f"OK: field change is compact (no Reply); human Quick Context note keeps "
          f"Reply; API isSystemEvent true/false. Blocked external asset requests: {len(blocked)}")


def main():
    try:
        import psycopg2  # noqa: F401
    except Exception as exc:
        raise SystemExit(f"psycopg2 required: {exc}")
    try:
        import playwright  # noqa: F401
    except Exception as exc:
        raise SystemExit(
            "playwright (Python) required: pip install playwright && playwright install chromium\n"
            f"({exc})")

    opp_id = bootstrap_db()
    proc = start_server()
    try:
        drop_action_category()
        run_browser(opp_id)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not KEEP_DB:
            admin = _connect("postgres")
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (DB_NAME,))
                cur.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
            admin.close()


if __name__ == "__main__":
    main()
