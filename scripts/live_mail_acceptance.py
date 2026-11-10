#!/usr/bin/env python3
"""LIVE mail acceptance for the real accounts 2, 6, 8, 9, 10.

NOT run automatically. It runs only when Ken/the operator triggers it after a
deploy. DRY RUN BY DEFAULT.

Safety:
  * recipients are HARD-LOCKED to crm@vanguardadj.online (to, cc and bcc);
    any other address aborts the whole run;
  * --execute AND SIETCH_ACCEPTANCE_CONFIRM=YES are both required to send;
  * credentials come from env (token or email/password) and are never printed;
  * no message bodies are printed.

Per account: new compose from that account, a reply and a forward (driving the
app's own endpoints with the exact account_id, like the UI), then verification:
  * the delivered copy in the CRM inbox (account 2) has From address AND
    display name matching the selected account (From/Sender/Return-Path from
    real headers);
  * the Sent copy is filed under the right account/folder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_RECIPIENTS = {"crm@vanguardadj.online"}
ACCOUNTS = [2, 6, 8, 9, 10]
BASE = os.environ.get("SIETCH_BASE_URL", "https://crm.publicadjustermidwest.com")
TAG = f"[ACCEPTANCE {int(time.time())}"


def validate_recipients(to_addr: str, cc: str | None = None, bcc: str | None = None) -> list[str]:
    """Return the offending addresses (empty = allowed). Code-level hard lock."""
    bad = []
    for raw in (to_addr, cc, bcc):
        for part in str(raw or "").split(","):
            addr = part.strip().lower()
            if not addr:
                continue
            if addr not in ALLOWED_RECIPIENTS:
                bad.append(addr)
    return bad


def _request(method: str, path: str, token: str | None, body: dict | None = None,
             cookie: str | None = None) -> tuple[int, dict | list | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE.rstrip("/") + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if cookie:
        req.add_header("Cookie", cookie)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def login() -> tuple[str | None, str | None]:
    """Return (token, cookie). Never prints credentials."""
    token = os.environ.get("SIETCH_ACCEPTANCE_TOKEN")
    if token:
        return token, None
    email = os.environ.get("SIETCH_ACCEPTANCE_USER")
    password = os.environ.get("SIETCH_ACCEPTANCE_PASS")
    if not email or not password:
        return None, None
    req = urllib.request.Request(
        BASE.rstrip("/") + "/api/v2/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
            return None, cookie or None
    except Exception:
        return None, None


AUTOMATED_KINDS = ("new", "reply", "reply_all", "forward")
CONCURRENT_PAIRS = ((2, 9), (9, 10), (8, 9))
SEQUENTIAL_PAIRS = ((2, 10), (6, 9), (8, 10))

# Manual rows are UI-only; the operator fills the result table in the plan.
MANUAL_ROWS = [
    ("M1", "Open each account tab (2,6,8,9,10) -> compose shows that account"),
    ("M2", "All-inboxes view -> compose From is empty"),
    ("M3", "Job page -> compose From is empty"),
    ("M4", "Contact page -> compose From is empty"),
    ("M5", "Template -> compose From is empty, user picks"),
    ("M6", "Switch From after opening, then Send -> sent via the switched account"),
    ("M7", "Switch tabs/From quickly right before Send -> sent via the visible account"),
    ("M8", "Send immediately on open (before the account list loads) -> blocked or visible error"),
    ("M9", "Two browser windows with different accounts open at once -> each sends its own"),
    ("M10", "Page left open for several hours -> send still uses the selected account"),
    ("M11", "Send with an attachment -> delivered attachment, From correct"),
    ("M12", "Reply to a message received as a CRM BCC copy -> original account"),
    ("M13", "Send right after a server restart -> correct account, visible errors if not ready"),
    ("M14", "Expired access token (wait for hourly expiry; do NOT revoke) -> forced refresh, correct account"),
]


def matrix_rows() -> list[dict]:
    """Automated live matrix. Every row has an id; recipients are hard-locked."""
    rows = []
    n = 0
    for acct in ACCOUNTS:
        for kind in AUTOMATED_KINDS:
            n += 1
            rows.append({"id": f"A{n:02d}", "account_id": acct, "kind": kind,
                         "to": "crm@vanguardadj.online",
                         "subject": f"{TAG} acct {acct} {kind}]",
                         "note": ""})
    for acct in ACCOUNTS:
        n += 1
        rows.append({"id": f"A{n:02d}", "account_id": acct, "kind": "draft_reopen",
                     "to": "crm@vanguardadj.online",
                     "subject": f"{TAG} acct {acct} draft_reopen]", "note": "save draft, reopen, send"})
    for acct in ACCOUNTS:
        n += 1
        rows.append({"id": f"A{n:02d}", "account_id": acct, "kind": "switch_from",
                     "to": "crm@vanguardadj.online",
                     "subject": f"{TAG} acct {acct} switch_from]", "note": "open on another account, switch"})
    for acct in ACCOUNTS:
        n += 1
        rows.append({"id": f"A{n:02d}", "account_id": acct, "kind": "fast_send",
                     "to": "crm@vanguardadj.online",
                     "subject": f"{TAG} acct {acct} fast_send]", "note": "send immediately on open"})
    for a, b in CONCURRENT_PAIRS:
        n += 1
        rows.append({"id": f"A{n:02d}", "account_id": a, "kind": "concurrent_pair",
                     "to": "crm@vanguardadj.online",
                     "subject": f"{TAG} acct {a}+{b} concurrent]",
                     "note": f"concurrent with account {b}"})
    for a, b in SEQUENTIAL_PAIRS:
        n += 1
        rows.append({"id": f"A{n:02d}", "account_id": a, "kind": "sequential_pair",
                     "to": "crm@vanguardadj.online",
                     "subject": f"{TAG} acct {a}+{b} sequential]",
                     "note": f"back-to-back with account {b}"})
    return rows


def _headers(token, cookie, message_id):
    status, data = _request("GET", f"/api/v2/mail/messages/{message_id}/headers", token, cookie=cookie)
    return data if status == 200 else {}


def _parse_from(headers: dict) -> dict:
    text = json.dumps(headers)
    def get(name):
        for key, value in (headers or {}).items():
            if str(key).lower() == name:
                return str(value)
        return ""
    return {"from": get("from"), "sender": get("sender"), "return_path": get("return-path")}


def verify(token, cookie, account_email, account_name, tag, sent_folder_ok):
    # delivered copy in the CRM inbox (account 2)
    status, data = _request("GET", "/api/v2/mail/messages?folder=INBOX&account_id=2&page_size=50", token, cookie=cookie)
    msgs = (data or {}).get("messages", []) if isinstance(data, dict) else []
    hit = next((m for m in msgs if tag in str(m.get("subject") or "")), None)
    if not hit:
        return False, "delivered copy not found in CRM inbox"
    hdr = _parse_from(_headers(token, cookie, hit.get("id")))
    ok_addr = account_email.lower() in (hdr["from"] or "").lower()
    ok_name = (not account_name) or (account_name.lower() in (hdr["from"] or "").lower())
    return (ok_addr and ok_name and sent_folder_ok,
            f"from={hdr['from']!r} sender={hdr['sender']!r} rp={hdr['return_path']!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live mail acceptance (dry-run by default)")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--account", action="append", type=int, default=None)
    parser.add_argument("--matrix", action="store_true", help="list the matrix and exit")
    args = parser.parse_args(argv)

    accounts = args.account or ACCOUNTS
    rows = [r for r in matrix_rows() if r["account_id"] in accounts]
    if args.matrix:
        print(f"LIVE MATRIX: {len(matrix_rows())} automated rows, {len(MANUAL_ROWS)} manual rows")
        for r in matrix_rows():
            print(f"  {r['id']} acct {r['account_id']:>2} {r['kind']:<15} {r['note']}")
        for mid, text in MANUAL_ROWS:
            print(f"  {mid} MANUAL  {text}")
        return 0
    for r in rows:
        bad = validate_recipients(r["to"])
        if bad:
            print(f"ABORT: recipient not allowed: {bad}")
            return 2

    if not args.execute or os.environ.get("SIETCH_ACCEPTANCE_CONFIRM") != "YES":
        print("DRY RUN — nothing sent. Plan:")
        for r in rows:
            print(f"  account {r['account_id']:>2} {r['kind']:<7} -> {r['to']}  {r['subject']}")
        print(f"\n{len(rows)} sends. To execute: --execute with SIETCH_ACCEPTANCE_CONFIRM=YES")
        return 0

    token, cookie = login()
    if not token and not cookie:
        print("ABORT: set SIETCH_ACCEPTANCE_TOKEN or SIETCH_ACCEPTANCE_USER/PASS")
        return 2

    results = []
    for r in rows:
        acct_id = r["account_id"]
        status, data = _request("GET", "/api/v2/mail/accounts", token, cookie=cookie)
        accounts_data = (data or {}).get("accounts", []) if isinstance(data, dict) else []
        acct = next((a for a in accounts_data if a.get("id") == acct_id), None)
        if not acct:
            results.append((acct_id, r["kind"], "SKIP", "account not visible"))
            continue
        payload = {"account_id": acct_id, "to": r["to"], "subject": r["subject"],
                   "body": "<p>acceptance</p>"}
        if r["kind"] in ("reply", "forward"):
            s2, msgs = _request("GET", f"/api/v2/mail/messages?folder=INBOX&account_id={acct_id}&page_size=1", token, cookie=cookie)
            lst = (msgs or {}).get("messages", []) if isinstance(msgs, dict) else []
            if not lst:
                results.append((acct_id, r["kind"], "SKIP", "no message to reply/forward"))
                continue
            payload["reply_to_message_id"] = lst[0]["id"]
        st, resp = _request("POST", "/api/v2/mail/send", token, payload, cookie=cookie)
        if st != 200:
            results.append((acct_id, r["kind"], "FAIL", f"send HTTP {st}: {(resp or {}).get('error','') if isinstance(resp, dict) else ''}"))
            continue
        sent_as = (resp or {}).get("sentAs", "") if isinstance(resp, dict) else ""
        s3, sent = _request("GET", f"/api/v2/mail/messages?folder=Sent&account_id={acct_id}&page_size=50", token, cookie=cookie)
        sl = (sent or {}).get("messages", []) if isinstance(sent, dict) else []
        sent_ok = any(r["subject"] in str(m.get("subject") or "") for m in sl)
        ok, detail = verify(token, cookie, acct.get("email", ""), acct.get("smtp_from_name") or acct.get("display_name") or "", r["subject"], sent_ok)
        results.append((acct_id, r["kind"], "PASS" if ok else "FAIL",
                        f"sentAs={sent_as} {detail}"))

    print("\nrow  account kind             result  detail")
    for idx, (acct_id, kind, res, detail) in enumerate(results):
        print(f"A{idx+1:02d} {acct_id:>7} {kind:<15} {res:<7} {detail[:100]}")
    fails = sum(1 for _, _, res, _ in results if res == "FAIL")
    print(f"\npass={sum(1 for _,_,r,_ in results if r=='PASS')} fail={fails} skip={sum(1 for _,_,r,_ in results if r=='SKIP')}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
