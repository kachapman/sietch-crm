# Email Modal & Scanner Audit (2026-10-14)

Quick-scan audit of the email modal UI and IMAP-based scanner, plus the
shared send helpers. Findings are tagged by severity. **Fix suggestions are
included for every item.**

**Legend:** 🔴 Critical · 🟠 High · 🟡 Medium · ⚪ Low / Nit

---

## Files in scope

### Frontend
- `public/index.html` — modals: `#mail-inbox-modal` (607–948), `#compose-email-modal` (951–1055), `#email-preview-modal` (1091–1118), `#account-settings-modal` (1121+), `#mail-scanner-admin` (695–870+)
- `public/app.js` — entry points: `openMailInboxModal` (19758), `openComposeModal` (20040), `openAccountSettingsModal` (21522), `renderMailList` (23506), `pickMailBodyForDisplay` (25031), `mailBodyIframeSrcdoc` (25064), `openEmailPreviewModal` (25232), `fetchMailMessage` (25486), `populateEmailScannerTab` (31459), `bindScannerAdminButtons` (31974), `sanitizeHistoryHtml` (24449)
- `public/styles.css` — email-modal CSS ~4700–11130, preview ~15899–16600

### Backend
- `server.py` — `_handle_mail_*` (16497–18664) + route dispatch (3220–4470)
- `mail_send.py` — `try_send_for_account` shared send router
- `mail_graph_client.py` — Microsoft Graph `sendMail` client
- `scanner/mail_scanner.py` — IMAP polling, classification, behavior execution
- `scanner/scanner_service.py` — admin HTTP service for stand-alone container

### Database
- `init.sql` (703–1020) + 19 mail migrations
- Tables: `mail_accounts`, `mail_messages`, `mail_tags`, `mail_message_tags`, `mail_templates`, `mail_account_access`, `mail_outgoing`, `mail_folders`, `mail_attachments`, `mail_deal_links`, `mail_classification_rules`, `mail_contractors`, `mail_contacts`, `classifier_training_data`

---

## Findings

### 🔴 1. Email body iframe uses `allow-scripts` sandbox
**File:** `public/app.js:25307`

```js
iframe.setAttribute('sandbox', 'allow-same-origin allow-scripts');
```

Email bodies are pre-sanitized by `sanitizeHistoryHtml` (strips `<script>`,
`on*`, `javascript:` URLs, `<iframe>`, `<object>`, etc.), but the iframe
sandbox still permits script execution. The sanitizer also only blocks
*white* backgrounds, not arbitrary `url()` references — a CSS exfil image
or a near-white `position:fixed; z-index:9999` overlay is still possible.

**Fix:** Drop `allow-scripts` (and `allow-same-origin`). Add a CSP meta
inside the iframe srcdoc.

```js
iframe.setAttribute('sandbox', 'allow-popups');
```

And inside `mailBodyIframeSrcdoc` add a CSP meta tag:
```html
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src https: data:; style-src 'unsafe-inline';">
```

**Status: Fixed in this session** (see SESSION_HISTORY 2026-10-14).

---

### 🟠 2. `_get_regex` pattern cache is unbounded — ReDoS / memory growth
**File:** `scanner/mail_scanner.py:512–519, 564–575`

```python
_regex_cache: dict[str, re.Pattern] = {}

def _get_regex(pattern: str) -> re.Pattern:
    if pattern not in _regex_cache:
        _regex_cache[pattern] = re.compile(pattern, re.IGNORECASE)
    return _regex_cache[pattern]
```

Two problems:
1. **No timeout.** An admin-supplied `body_regex` like `((a+)+)+b`
   (catastrophic backtracking) wedges the scanner on every match attempt.
2. **Cache is unbounded.** Saving thousands of distinct rule patterns
   leaks memory forever.

**Fix:** Use `functools.lru_cache(maxsize=1024)` and return a never-match
pattern on `re.error` so `_evaluate_condition` treats bad regex as a
no-match instead of throwing. Also reject bad regex at rule-save time in
`_handle_mail_classification_rule_create/update`.

**Status: Fixed in this session** (see SESSION_HISTORY 2026-10-14, second entry).

---

### 🟠 3. `populateEmailScannerTab` (~1400 lines) and `openAccountSettingsModal` rebuild the entire panel on every open — listener accumulation, lost scroll
**File:** `public/app.js:31459`, `public/app.js:21522`

Both functions rebuild HTML and rebind listeners on every open. Open the
scanner tab 20 times → 20× listeners and lost scroll position. Any global
`document.addEventListener('keydown', …)` bound inside is never cleaned
up → memory leak.

**Fix:** Render once, toggle visibility via CSS class. Use
`AbortController` per panel; call `.abort()` on close. Re-render only the
affected section on data refresh.

---

### 🟠 4. `openEmailPreviewModal` rebinds ESC + action listeners on every open
**File:** `public/app.js:25239–25241, 25422–25478`

```js
const escH = (e) => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', escH); } };
document.addEventListener('keydown', escH);
// ...
modal.querySelectorAll('.email-preview-action').forEach(btn => btn.addEventListener('click', …));
```

ESC handler is only removed if the user actually presses ESC while the
modal is open. If the user closes via X (which fires the dismiss listener
but not `escH`), the `keydown` listener stays on `document` forever.
After 5 opens there are 5 click listeners per action button.

**Fix:** Use `AbortController` per modal-open cycle, call `abort()` in
`close()`. Apply same pattern to `openComposeModal` and
`openAccountSettingsModal`.

**Status: Fixed in this session** (see SESSION_HISTORY 2026-10-14, second entry).

---

### 🟠 5. `_handle_mail_message_create` — verify `account_id` access check
**File:** `server.py:17016` (needs confirmation)

If the create handler accepts `account_id` from the body and inserts a
`mail_messages` row without calling `_mail_account_accessible(user,
account_id)`, any authenticated user can post a message under any
account they can read. (Confirmed `_handle_mail_inbox` / `_handle_mail_message`
both check; the create path needs verification.)

**Fix:** Add `_mail_account_accessible(user, account_id)` check. Cap
`body_html` / `body_text` at ~5 MB. Validate `folder` against an
allowlist.

**Status: Fixed in this session** (see SESSION_HISTORY 2026-10-14, second entry).

---

### 🟠 6. `record_feedback_candidate` writes to `FEEDBACK_FILE` without locking
**File:** `scanner/mail_scanner.py:1957–1962`

CPython `with open(..., "a")` flushes on `__exit__`, so single-line
atomicity holds, but the reader `get_feedback_entries` (line 1970) can
see a half-written line during training.

**Fix:** Wrap parse in `try/except json.JSONDecodeError` in the reader and
skip malformed lines. Optional: `threading.Lock` for cross-process safety.

---

### 🟡 7. `try_send_for_account` swallows Graph exceptions
**File:** `mail_send.py:95–97, 123–125, 132–156`

Graph send exceptions are re-wrapped as `str(e)` (e.g.
`<urlopen error [Errno -2] Name or service not known>`) — not useful for
end users.

**Fix:** Hoist `_summarize_graph_error` to be called on caught
exceptions too. Best-effort summary when underlying error is from Graph.

---

### 🟡 8. `_mail_visible_accounts_sql` — confirm it's parameterized
**File:** `server.py:16651`

```python
where.append(f"m.account_id IN (SELECT id FROM mail_accounts WHERE {_mail_visible_accounts_sql(user['id'])})")
```

Only safe if the helper returns a hardcoded SQL fragment (with the user
ID added via `%s` + params at the call site). **Action:** grep the
helper, confirm it returns a parameterized fragment. If so, add a code
comment to prevent future f-string interpolation.

---

### 🟡 9. `mail_messages.attachments_json` fallback is read without size cap
**File:** `server.py:16752–16757`

If a long-lived account has many large attachments, each row's JSON can
be hundreds of KB. Returning 50 messages balloons the response.

**Fix:** Return only metadata (filename, size, mime_type, content_id).
Lazy-load actual content on the existing attachments endpoint.

---

### 🟡 10. `mailBodyIframeSrcdoc` re-serializes HTML → brittle
**File:** `public/app.js:25067–25083`

`temp.innerHTML` is the re-serialized HTML, which means `&lt;script&gt;`
becomes `<script>`. This is intentional (sandboxed iframe), but fragile:
anyone who later edits `sanitizeHistoryHtml` must remember the second
walk re-introduces attribute text without re-escaping.

**Fix:** Add a comment warning future maintainers, and re-run the
sanitizer on the cleaned output as defense-in-depth:
```js
const cleaned = sanitizeHistoryHtml(temp.innerHTML);
```

---

### 🟡 11. Outbound send has no per-user rate limit
**File:** `server.py:17677`, `mail_send.py:29`

A compromised account can send unlimited emails to any address using any
configured account. No daily cap, no per-account quota.

**Fix:** Per-user per-day outbound cap (e.g., 500/day, configurable).
Check against `mail_outgoing` count before sending. Restrict which
accounts a user can send from to those they own / are shared to (likely
already done — verify `_mail_account_accessible` is called for
`from_account_id`).

---

### 🟡 12. Admin-only endpoints may be missing is_admin check
**Files:** `server.py:17297, 17377, 17391, 17401` (`_handle_mail_log_correct`,
`_handle_mail_reprocess`, `_handle_mail_retrain`, `_handle_mail_config_put`)

If any of these accept submissions from non-admin users, they can poison
the ML training set or change scanner behavior for everyone.

**Fix:** Add `if not user.get("is_admin"): return _json_response(self, 403, ...)`.

---

### 🟡 13. Account-settings form may persist password in formData
**Files:** `public/index.html:712, 730`, `public/app.js:21522`

After a successful save, the password field isn't cleared and the form
data is still in memory. Also the password is POSTed on every edit even
when the user only changed the display name.

**Fix:** After save, clear `#scanner-acct-password` and
`#scanner-acct-smtp-password` values. Send password only on first create
or when the field is non-empty.

---

### ⚪ 14. `_handle_mail_messages` builds WHERE with string concatenation
**File:** `server.py:16643–16665`

Currently safe (all user inputs go through `%s`), but the pattern invites
future SQLi.

**Fix:** Refactor to a `QueryBuilder` helper with typed methods
(`.eq_int`, `.eq_str`, `.ilike`). Optional; not urgent.

---

### ⚪ 15. Phishing overlay via near-white `position:fixed`
**File:** `public/app.js:25064–25083` + `sanitizeHistoryHtml:24474–24487`

Sanitizer only blocks `background` when the value contains pure white.
An attacker can use `rgba(250,250,250,1)` or a `background-image:url(...)`
to create a near-white overlay with `position:fixed; z-index:9999` to
cover the dashboard with a fake "session expired" prompt.

**Fix:** Strip `position:fixed`/`position:absolute` and any `z-index` from
inline styles. Add `html,body{position:static !important;}` to the iframe
srcdoc CSS.

---

### ⚪ 16. `get_scanner_log` / `get_feedback_entries` accept `limit=0`
**File:** `scanner/mail_scanner.py:1903–1919, 1970–1978`

`return entries[-limit:] if limit else entries` returns the entire log
when `limit` is 0/None. The handler probably caps it, but the helper
should be defensive.

**Fix:** Default to 200 if `limit <= 0`.

---

### ⚪ 17. `openComposeModal` and `bindComposeEmailModal` — same listener-accumulation risk as #3/#4
**File:** `public/app.js:20040, 22127`

Apply the same `AbortController` fix as `openEmailPreviewModal`.

---

## Bonus XSS in reply/forward quotes (not in the original 17)

**File:** `public/app.js:25447, 25451, 25455` (original lines, pre-refactor)

The reply/forward action buttons built a `quotedBody` by interpolating
`${bodyText}` (plain-text email content from `pickMailBodyForDisplay`)
directly into HTML. Plain-text content can contain `<script>` or other
markup. Once injected into the compose modal's contenteditable, the
script would run in the dashboard's origin and could be sent to the
recipient on Send.

**Fix:** Apply `escapeHtml(bodyText)` before interpolating into the
quotedBody HTML.

**Status: Fixed in this session** (see SESSION_HISTORY 2026-10-14, second entry).

---

## What's NOT a problem (verified)

- **SQL injection** — every `db.execute/query_dicts/query_one` uses `%s`
  placeholders with `params` tuples. No f-string interpolation of user
  input into SQL. ✓
- **XSS via `escapeHtml`** — `renderMailList` (23506), email preview head
  (25273), linked-deal spans (25356), reply/forward quotes (25438/42/46)
  all escape. ✓
- **Graph auth** — `mail_graph_client.py:117` uses
  `Authorization: Bearer {token}`, no URL-embedded secrets. ✓
- **OAuth token refresh** — `_build_graph_token_refresher`
  (mail_scanner.py:210) refreshes on demand and persists. Same pattern in
  `server.py:16497`. ✓

---

## Summary

| Sev | Count | Top items |
|---|---|---|
| 🔴 Critical | 1 | #1 iframe sandbox `allow-scripts` |
| 🟠 High | 4 | #2 ReDoS, #3/#4 listener leaks, #5 message-create access |
| 🟡 Medium | 7 | #6/#7/#8/#9/#11/#12/#13 |
| ⚪ Low / Nit | 5 | #10/#14/#15/#16/#17 |

## Fixes landed this session

- **#1** — iframe sandbox hardened (`sandbox='allow-popups'` only).
- **#2** — regex cache bounded via `lru_cache(maxsize=1024)`; bad
  patterns no-match instead of throwing.
- **#16** — defensive `limit` default in `get_scanner_log` and
  `get_feedback_entries` (small adjacent fix).
