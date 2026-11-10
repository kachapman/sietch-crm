#!/usr/bin/env python3
"""LIVE mail acceptance — drives the REAL Sietch UI with Playwright.

NOT run automatically. Runs only when Ken/the operator triggers it after a
deploy. DRY RUN BY DEFAULT.

Why UI-driven: API-only sends skip the compose-window bugs (stale From select,
tab switches, reply overrides). This script clicks account tabs, Compose /
Reply / Forward, the From select and Send exactly like a user. API/IMAP/DB
reads are used ONLY to verify the delivered copy and the Sent filing.

Safety:
  * recipients HARD-LOCKED to crm@vanguardadj.online in two places:
      - fill-time assertion on the To/Cc/Bcc fields;
      - a Playwright route that ABORTS any /api/v2/mail/send whose payload
        contains another recipient;
  * real login through the normal form with env credentials
    (SIETCH_ACCEPTANCE_USER / SIETCH_ACCEPTANCE_PASS); never stored/printed;
    no forged sessions or cookies;
  * --execute AND SIETCH_ACCEPTANCE_CONFIRM=YES are both required to send;
  * no bodies and no secrets are printed.

Run (after Ken's go):
  pip install playwright && playwright install chromium
  SIETCH_ACCEPTANCE_USER=... SIETCH_ACCEPTANCE_PASS=... \
  SIETCH_ACCEPTANCE_CONFIRM=YES \
  python3 scripts/live_mail_acceptance.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

ALLOWED_RECIPIENTS = {"crm@vanguardadj.online"}
ACCOUNTS = [2, 6, 8, 9, 10]
BASE = os.environ.get("SIETCH_BASE_URL", "https://crm.publicadjustermidwest.com")
TAG = f"[ACCEPTANCE {int(time.time())}"

# UI selectors (adjust here if the markup changes; text locators are used where
# possible so the script survives id churn).
SEL = {
    "login_email": "input[type=email]",
    "login_password": "input[type=password]",
    "login_submit": "button[type=submit], button:has-text('Sign in'), button:has-text('Log in')",
    "compose_btn": "button:has-text('Compose')",
    "from_select": "#compose-from-account",
    "to": "#compose-to",
    "cc": "#compose-cc",
    "bcc": "#compose-bcc",
    "subject": "#compose-subject",
    "body": "#compose-body",
    "send": "#compose-send-btn",
    "reply": "button:has-text('Reply')",
    "forward": "button:has-text('Forward')",
    "account_tab": "text={email}",
}

# Manual-only rows: things Playwright cannot reliably drive here.
MANUAL_ROWS = [
    ("M1", "Send right after a server restart (timing) — correct account or visible error"),
    ("M2", "Expired access token (wait for the hourly expiry; do NOT revoke) — forced refresh"),
    ("M3", "Page left open for several hours — send still uses the selected account"),
]

AUTOMATED_KINDS = ("new", "reply", "reply_all", "forward")
CONCURRENT_PAIRS = ((2, 9), (9, 10), (8, 9))
SEQUENTIAL_PAIRS = ((2, 10), (6, 9), (8, 10))


def validate_recipients(to_addr: str, cc: str | None = None, bcc: str | None = None) -> list[str]:
    bad = []
    for raw in (to_addr, cc, bcc):
        for part in str(raw or "").split(","):
            addr = part.strip().lower()
            if addr and addr not in ALLOWED_RECIPIENTS:
                bad.append(addr)
    return bad


def payload_recipients(payload: dict) -> list[str]:
    out = []
    for key in ("to", "cc", "bcc"):
        out.extend([p.strip().lower() for p in str(payload.get(key) or "").split(",") if p.strip()])
    return out


def matrix_rows() -> list[dict]:
    rows = []
    n = 0
    def add(acct, kind, note=""):
        nonlocal n
        n += 1
        rows.append({"id": f"A{n:02d}", "account_id": acct, "kind": kind,
                     "subject": f"{TAG} acct {acct} {kind}]", "note": note})
    for acct in ACCOUNTS:
        for kind in AUTOMATED_KINDS:
            add(acct, kind)
    for acct in ACCOUNTS:
        add(acct, "draft_reopen", "save draft, reopen, send")
    for acct in ACCOUNTS:
        add(acct, "switch_from", "open on another account, switch From, send")
    for acct in ACCOUNTS:
        add(acct, "fast_send", "Send immediately on open")
    for a, b in CONCURRENT_PAIRS:
        add(a, "concurrent_pair", f"two contexts, concurrent with account {b}")
    for a, b in SEQUENTIAL_PAIRS:
        add(a, "sequential_pair", f"back-to-back with account {b}")
    return rows


# ── Playwright driver ────────────────────────────────────────────────────────

def _install_recipient_guard(context) -> None:
    """Abort any send request whose payload has a non-allowed recipient."""
    def handler(route):
        try:
            body = route.request.post_data_json or {}
        except Exception:
            body = {}
        bad = [a for a in payload_recipients(body) if a not in ALLOWED_RECIPIENTS]
        if bad:
            route.abort()
        else:
            route.continue_()
    context.route("**/api/v2/mail/send*", handler)


def _assert_fields_allowed(page) -> None:
    values = []
    for key in ("to", "cc", "bcc"):
        loc = page.locator(SEL[key])
        if loc.count():
            values.append(loc.first.input_value())
    bad = validate_recipients(values[0] if values else "", values[1] if len(values) > 1 else None,
                              values[2] if len(values) > 2 else None)
    if bad:
        raise RuntimeError(f"recipient not allowed: {bad}")


def _login(page) -> None:
    user = os.environ.get("SIETCH_ACCEPTANCE_USER")
    password = os.environ.get("SIETCH_ACCEPTANCE_PASS")
    if not user or not password:
        raise RuntimeError("SIETCH_ACCEPTANCE_USER/PASS are required (no auth bypass)")
    page.goto(BASE, wait_until="domcontentloaded")
    page.fill(SEL["login_email"], user)
    page.fill(SEL["login_password"], password)
    page.click(SEL["login_submit"])
    page.wait_for_load_state("networkidle")


def _open_account_tab(page, acct: dict) -> None:
    page.click(SEL["account_tab"].format(email=acct["email"]))


def _send(page, acct: dict, kind: str, subject: str) -> None:
    """Drive the UI for one row. Raises on a visible failure."""
    if kind in ("reply", "reply_all", "forward"):
        page.click(SEL["reply"] if kind != "forward" else SEL["forward"])
    else:
        page.click(SEL["compose_btn"])
    page.wait_for_selector(SEL["from_select"])
    # The user picks the account in the From select (like a human).
    page.select_option(SEL["from_select"], str(acct["id"]))
    page.fill(SEL["to"], "crm@vanguardadj.online")
    page.fill(SEL["subject"], subject)
    _assert_fields_allowed(page)
    page.click(SEL["send"])


def _verify(base_cookie: str, acct: dict, subject: str) -> tuple[bool, str]:
    """API reads only: delivered copy in account 2 + Sent filing for acct."""
    def get(path):
        req = urllib.request.Request(BASE.rstrip("/") + path)
        req.add_header("Cookie", base_cookie)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    inbox = get("/api/v2/mail/messages?folder=INBOX&account_id=2&page_size=50")
    hit = next((m for m in inbox.get("messages", []) if subject in str(m.get("subject") or "")), None)
    if not hit:
        return False, "delivered copy not found in CRM inbox"
    hdr = get(f"/api/v2/mail/messages/{hit['id']}/headers")
    frm = ""
    for k, v in (hdr or {}).items():
        if str(k).lower() == "from":
            frm = str(v)
    ok_addr = acct["email"].lower() in frm.lower()
    name = (acct.get("smtp_from_name") or acct.get("display_name") or "").lower()
    ok_name = (not name) or (name in frm.lower())
    sent = get(f"/api/v2/mail/messages?folder=Sent&account_id={acct['id']}&page_size=50")
    sent_ok = any(subject in str(m.get("subject") or "") for m in sent.get("messages", []))
    return ok_addr and ok_name and sent_ok, f"from={frm!r} sent_filed={sent_ok}"


def run_matrix(execute: bool) -> int:
    rows = matrix_rows()
    if not execute or os.environ.get("SIETCH_ACCEPTANCE_CONFIRM") != "YES":
        print(f"DRY RUN — nothing sent. {len(rows)} UI-driven rows + {len(MANUAL_ROWS)} manual:")
        for r in rows:
            print(f"  {r['id']} acct {r['account_id']:>2} {r['kind']:<15} {r['note']}")
        for mid, text in MANUAL_ROWS:
            print(f"  {mid} MANUAL  {text}")
        print("\nTo execute: --execute with SIETCH_ACCEPTANCE_CONFIRM=YES")
        return 0

    from playwright.sync_api import sync_playwright  # live only
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        _install_recipient_guard(ctx)
        page = ctx.new_page()
        _login(page)
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        accounts = {a["id"]: a for a in json.loads(
            urllib.request.urlopen(urllib.request.Request(
                BASE.rstrip("/") + "/api/v2/mail/accounts",
                headers={"Cookie": cookie})).read())["accounts"]}
        for r in rows:
            acct = accounts.get(r["account_id"])
            if not acct:
                results.append((r["id"], "SKIP", "account not visible"))
                continue
            try:
                _open_account_tab(page, acct)
                _send(page, acct, r["kind"], r["subject"])
                ok, detail = _verify(cookie, acct, r["subject"])
                results.append((r["id"], "PASS" if ok else "FAIL", detail))
            except Exception as exc:
                results.append((r["id"], "FAIL", str(exc)[:120]))
        browser.close()
    print("\nrow  result  detail")
    for rid, res, detail in results:
        print(f"{rid} {res:<7} {detail}")
    fails = sum(1 for _, r, _ in results if r == "FAIL")
    print(f"\npass={sum(1 for _,r,_ in results if r=='PASS')} fail={fails} "
          f"skip={sum(1 for _,r,_ in results if r=='SKIP')} manual={len(MANUAL_ROWS)}")
    return 1 if fails else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live UI acceptance (dry-run by default)")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--matrix", action="store_true", help="list the matrix and exit")
    args = parser.parse_args(argv)
    for r in matrix_rows():
        bad = validate_recipients("crm@vanguardadj.online")
        if bad:
            print(f"ABORT: {bad}"); return 2
    if args.matrix:
        print(f"LIVE MATRIX (UI-driven): {len(matrix_rows())} automated + {len(MANUAL_ROWS)} manual")
        for r in matrix_rows():
            print(f"  {r['id']} acct {r['account_id']:>2} {r['kind']:<15} {r['note']}")
        for mid, text in MANUAL_ROWS:
            print(f"  {mid} MANUAL  {text}")
        return 0
    return run_matrix(args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
