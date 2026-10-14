# Email Template & Deal-Linking Audit (2026-10-14)

Audit of the email template pipeline, signature flow, and `[#PROJECTID]`
subject auto-link mechanism. The goal is to determine what's needed to
make the deal-id subject-tagging convention **standardized, safe, and
discoverable** for new operators.

**Scope:**
- `mail_templates` table + CRUD + frontend insertion
- `mail_accounts.signature_html` / `signature_text` flow
- `mail_outgoing.template_id` orphan FK
- The scanner's `[#PROJECTID]` / `[#DEAL-NNN]` regex auto-link (incoming)
- Whether the same `[#NNNN]` mechanism exists for **outgoing** mail
- Placeholder engine: scanner's `_render_template` / `_build_template_context`

**Legend:** 🔴 Critical · 🟠 High · 🟡 Medium · ⚪ Low / Nit

---

## The headline problem

**There is no structured `[#PROJECTID]` (or `{project_id}`) placeholder
that works for outgoing email.** Two separate template systems exist:

1. **`mail_templates`** (`/api/v2/mail/templates`) — a CRUD-only store of
   `title` + `subject` + `body_html`. Plain HTML. Inserted into the
   compose modal's `contenteditable` as raw `innerHTML` with no
   sanitization, no placeholder expansion, and no auto-`[#NNN]` subject
   injection. The deal-id linkage for *incoming* mail lives in the
   scanner's regex on the *subject line* and is never re-applied on
   outgoing mail.

2. **Scanner `_render_template`** (`scanner/mail_scanner.py:194-214`) — a
   `{placeholder}` engine that supports `{project_id}` etc. but is only
   used by the scanner's *Custom Behaviors → reply_to_email / create_task
   / create_deal* actions. Never applied to `mail_templates`, never
   applied to manually composed mail.

The result: **an operator who wants to "always include `[#123]` in
outgoing email subjects" must hand-type it into every email.** There is
no template variable for it, no per-account "auto-tag all outgoing
subjects" setting, and no convention for the compose modal to insert
it when a deal is linked via the `#compose-deal-search` widget.

---

## Files in scope

| File | Lines | Role |
|---|---|---|
| `init.sql` | 731–732 | `mail_accounts.signature_html` / `signature_text` |
| `init.sql` | 812–823 | `mail_templates` schema |
| `init.sql` | 842–858 | `mail_outgoing` (with dead `template_id` FK) |
| `migrations/001_add_mail_tables.sql` | 25–33, 46–62 | Mirror of the above |
| `server.py` | 3267–3271, 4397–4406 | Template route dispatch |
| `server.py` | 17190–17267 | `_handle_mail_templates*` (CRUD) |
| `server.py` | 17732–17849 | `_handle_mail_send` (no template processing) |
| `server.py` | 18190–18221 | `_handle_mail_account_signature_get/put` |
| `public/index.html` | 738–755 | Scanner admin "Email Templates" section |
| `public/index.html` | 951–1055 | `#compose-email-modal` |
| `public/index.html` | 1045 | `#compose-template-btn` (footer) |
| `public/app.js` | 20040–20094 | `openComposeModal` (signature load, raw injection) |
| `public/app.js` | 20329–20401 | Compose send button handler |
| `public/app.js` | 20430–20490 | Compose template picker (raw injection at 20460) |
| `public/app.js` | 22160–22187 | Reload signature on From change (raw injection) |
| `public/app.js` | 23061–23107 | `renderMailTemplates` (sidebar list → compose) |
| `public/app.js` | 32018–32039 | Scanner admin template list (escaped, OK) |
| `public/app.js` | 32737–32796 | Scanner admin template CRUD handlers |
| `scanner/mail_scanner.py` | 95–104 | `DEAL_LINK_RE` (`[#\d+]`), `DEAL_ID_RE` (`[#DEAL-\d+]`) |
| `scanner/mail_scanner.py` | 187–214 | `_render_template` / `_build_template_context` |
| `scanner/mail_scanner.py` | 252–507 | `_execute_custom_behavior` (uses `{project_id}` etc.) |
| `scanner/mail_scanner.py` | 1425–1465 | `_classify_message` — `[#PROJECTID]` / `[#DEAL-NNN]` auto-link (incoming) |
| `docs/EMAIL_AUDIT.md` | 1–347 | Existing email audit (does not cover templates) |

---

## Findings

### 🔴 1. `mail_templates.body_html` is injected raw into the compose `contenteditable`
**File:** `public/app.js:20460`, `public/app.js:23078-23084`, `public/app.js:20091`

Three injection sinks drop `tpl.body_html` or `sig.signature_html` into
the compose modal's `#compose-body` via `innerHTML` with **no
sanitization**:

- `app.js:20460` — template picker "Insert template" sets
  `$('#compose-body').innerHTML = tpl.body_html`.
- `app.js:23078-23084` — sidebar template list click calls
  `openComposeModal({ body: tpl.body_html })` which does the same in
  `app.js:20083`.
- `app.js:20091` — on compose open, signature is loaded and
  `bodyDiv.innerHTML = …${sig.signature_html}…` (also `app.js:22173`,
  `22176` when the user switches the From account).

**Impact:** Anyone with access to the "save template" button (every
authenticated user — see #3) or with the right to set
`signature_html` on a mail account (account owner / admin) can inject
`<script>`, `<iframe>`, event handlers, or `<img onerror=…>` into every
email sent from that account. The script runs in the **dashboard
origin** (the compose modal lives in the same page as the rest of the
app), not a sandboxed iframe like the preview modal. On Send, the
injected HTML is also mailed to the recipient — the email client
rendering it is a separate attack surface.

**This is a bigger XSS surface than the email-preview iframe fix
(EMAIL_AUDIT #1).** That fix used a sandboxed iframe + CSP. The compose
modal has no such protection because contenteditable needs full DOM
access to function.

**Fix:**
- Run `tpl.body_html` and `sig.signature_html` through `sanitizeHistoryHtml`
  (already exists for preview bodies — `app.js:24449`) before
  assignment. That sanitizer strips `<script>`, `on*`, `javascript:`
  URLs, iframes, etc.
- Better: store templates in a structured form (Markdown) and render
  to HTML on insertion, so the operator can never write raw HTML in the
  first place.
- Add CSP `default-src 'self'; script-src 'self'` on the dashboard
  origin as defense-in-depth (won't help with `<img onerror>` but
  blocks external script loads).

### 🔴 2. `mail_templates` CRUD has no admin / ownership check
**File:** `server.py:17190-17267`

| Handler | Line | Auth | Admin? | Ownership check? |
|---|---|---|---|---|
| `_handle_mail_templates` (list) | 17190 | None (anyone) | No | No |
| `_handle_mail_template_get` | 17201 | None | No | No |
| `_handle_mail_template_create` | 17211 | `_require_auth` (line 17218) | **No** | Captures `created_by` (line 17219) but never enforces it |
| `_handle_mail_template_update` | 17229 | None | **No** | **No** |
| `_handle_mail_template_delete` | 17258 | None | **No** | **No** |

- `_require_auth` is only called in `_handle_mail_template_create`.
  The other four endpoints have no auth at all.
- No `is_admin` check on any endpoint.
- No `created_by == user['id']` check on update / delete. Any
  authenticated user can overwrite or delete any other user's
  template.
- No `_is_limited` check, even though the mail accounts list
  (`server.py:17274`) and inbox (`server.py:16609`, `16639`) gate on
  it. A `limited` user (per AGENTS.md) is supposed to be a read-only
  role; right now they can create/edit/delete templates.

**Fix:**
- Add `_require_auth` to all five endpoints.
- Add `is_admin` OR `created_by == user["id"]` check on update/delete.
- Add `_is_limited` check on create (read-only should still list).
- Add a UNIQUE constraint on `(created_by, title)` so two users can
  have templates with the same name without collision.

### 🟠 3. `mail_templates` has no size cap, no HTML validation
**File:** `server.py:17211-17227` (create), `server.py:17229-17256` (update)

The create and update handlers accept any `subject` and any
`body_html` with no length cap. A malicious or buggy operator can
insert a 100 MB `body_html` and break the dashboard for everyone
(the compose modal loads it on every open, the picker loads it on
every click, the sidebar list returns the full body).

- No cap on `subject` (a 1 MB subject line is invalid email).
- No cap on `body_html` (similar to fix #5 for `_handle_mail_message_create`).
- No HTML validation — `<script>`, `<style>`, etc. are all stored.

**Fix:** Cap `subject` at 998 chars (RFC 5322 line length) and
`body_html` at 5 MB (same as the message body cap from EMAIL_AUDIT
#5). Reject empty `body_html` (allow plain-subject templates but
block empty everything).

### 🟠 4. The `mail_outgoing.template_id` FK is dead
**File:** `init.sql:853`, `server.py:17793-17806`

Schema:
```sql
CREATE TABLE mail_outgoing (
    …
    template_id INTEGER REFERENCES mail_templates(id),
    …
);
```

But `_handle_mail_send` (line 17793-17806) inserts a new outgoing
row without populating `template_id`. The column is always NULL.

This is a real problem: there is no audit trail for "which template
was used to send this email." When an operator gets a complaint about
"the email I received from you looks weird," there's no way to trace
it back to the template that produced it.

**Fix:** Either (a) populate `template_id` from the compose payload
(the compose modal would need to remember which template was last
inserted) or (b) drop the column and the FK constraint. (a) is
strictly more useful and is a small change.

### 🟠 5. No `[#PROJECTID]` standardization for outgoing mail
**File:** `scanner/mail_scanner.py:1425-1465` (incoming only)

The scanner's auto-link works on **incoming** mail subject lines:

```python
DEAL_LINK_RE = re.compile(r"\[#(\d+)\]", re.IGNORECASE)
DEAL_ID_RE = re.compile(r"\[#DEAL-(\d+)\]", re.IGNORECASE)
```

If an operator manually composes an email and types `Subject: Update
[#123]`, the `[#123]` is sent verbatim. The recipient's CRM (if they
have one) might pick it up, but the **sender's own** CRM has no record
that the outgoing email was associated with deal 123. The operator
must also use the `#compose-deal-search` widget to set `deal_id` in
the payload, which causes `_handle_mail_send` to insert a
`mail_deal_links` row from `sender` to `receiver's incoming email`
when the recipient replies. But that's a one-way link; the outbound
subject tag is not propagated as structured data.

**Worse, two formats are documented** (`[#123]` and `[#DEAL-123]`) but
the scanner's `_classify_message` only applies one as the primary
(`[#123]` first, `[#DEAL-123]` second). Operators who copy-paste
templates may end up with inconsistent formats.

**Fix:** Standardize on a single subject-tag format with a clear spec:

1. Pick **one** format and deprecate the other. Recommendation:
   `[#NNN]` (shorter, more common, matches existing examples in docs).
2. On outgoing send, if the operator has linked a deal via
   `#compose-deal-search` but the subject doesn't already include the
   tag, **auto-insert it** (e.g., `Subject: foo` becomes
   `Subject: foo [#123]`). This must be opt-in via a per-account
   setting (`auto_tag_outgoing_subject BOOLEAN` on `mail_accounts`,
   default false) so it doesn't surprise existing operators.
3. Add a structured placeholder in `mail_templates`:
   - `{project_id}` (already used by the scanner) — expand to the
     numeric deal ID of the linked deal.
   - `{project_id_tag}` — expand to `[#123]` (the full tag including
     brackets), so an operator can put `{project_id_tag}` in a
     subject template and have it auto-substitute.
4. Apply the same substitution at **send time** (server-side, in
   `_handle_mail_send`), not just at template-insert time, so the
   tag is always current even if the deal changes between template
   insert and send.

### 🟠 6. `[#PROJECTID]` formats are documented inconsistently across the codebase
**File:** `public/index.html:799`, `public/app.js:33003, 33018, 33059`,
`scanner/mail_scanner.py:102-104, 1435, 1449`, `docs/MAIL_SCANNER_PLAN.md:18,38,64`,
`FUTURE_FEATURES.md:442, 501`, `CHANGELOG.md:3839`

The codebase uses at least three different words for the same concept:

| File | Says | Format |
|---|---|---|
| `public/index.html:799` | "Auto-link emails by **[#PROJECTID]** in subject" | placeholder-style name |
| `public/app.js:33003` | "Any email with **[#1234]** in the subject gets linked" | concrete example |
| `public/app.js:33018` | "**[#PROJECTID]** linking" | placeholder-style name |
| `scanner/mail_scanner.py:102` | `DEAL_LINK_RE = re.compile(r"\[#(\d+)\]", re.IGNORECASE)` | `DEAL_LINK` |
| `scanner/mail_scanner.py:104` | `DEAL_ID_RE = re.compile(r"\[#DEAL-(\d+)\]", re.IGNORECASE)` | `DEAL_ID` |
| `scanner/mail_scanner.py:1435` | "Try **[#PROJECTID]** format first" | placeholder-style name |
| `scanner/mail_scanner.py:1449` | "Try **[#DEAL-NNN]** format" | concrete example |
| `CHANGELOG.md:3839` | "Both **[#PROJECTID]** and **[#DEAL-NNN]** formats work" | both |
| `docs/MAIL_SCANNER_PLAN.md` | "**[#DEAL-ID]** subject auto-link" | different word! |
| `FUTURE_FEATURES.md:442` | "**[#1042]** subject forwarding" | just the number |

The user-facing docs say "[#PROJECTID]" and "[#DEAL-NNN]" but the
scanner code calls it `DEAL_LINK` and `DEAL_ID`, the scanner's help
text says "PROJECTID", and the scanner plan says "DEAL-ID". This is
confusing for new operators trying to figure out which tag format to
use.

**Fix:**
- Pick **one** canonical name (`project_id` everywhere) and **one**
  canonical format (`[#NNN]`).
- Rename `DEAL_ID_RE` → `_PROJECT_ID_LEGACY_RE` (and remove it after
  one release cycle — see #5).
- Update all user-facing strings to the canonical name.
- Add a single test case `assert DEAL_LINK_RE.fullmatch("[#123]") is not None`
  so any future change to the regex is caught.

### 🟡 7. `_render_template` is duplicated/scattered knowledge
**File:** `scanner/mail_scanner.py:194-214`

The scanner's placeholder engine has its own context builder, regex,
and semantics. It supports `{subject}`, `{from}`, `{from_name}`,
`{project}`, `{project_id}`, `{claim_number}`, `{date}`,
`{body_preview}`. The user-facing placeholder hints in
`CUSTOM_BEH_CONFIGS` (`app.js:32248-32304`) only mention
`{subject}`, `{from}`, `{from_name}`, `{project}`, `{project_id}`.
The undocumented ones (`{claim_number}`, `{date}`, `{body_preview}`)
work but the user has no way to discover them.

**Fix:**
- Move the placeholder list to a single constant in the scanner:
  ```python
  SUPPORTED_PLACEHOLDERS = ("subject", "from", "from_name", "project",
                            "project_id", "claim_number", "date", "body_preview")
  ```
- Expose it via a new admin endpoint `/api/v2/mail/placeholders` so
  the frontend can dynamically populate the placeholder hint list.
- If/when `mail_templates` grows a placeholder engine, reuse this
  list and engine.

### 🟡 8. No `is_admin` check on signature endpoints
**File:** `server.py:18190-18221`

`_handle_mail_account_signature_get` checks `_mail_account_accessible`
(just means the user can read the account). `_handle_mail_account_signature_put`
checks `_mail_account_manageable` (means the user owns the account).
This is correct for owner-managed signatures, but there's no separate
"can the org-level admin edit any signature?" path. If the design
intent is "owner-only", that's fine; if the intent is "owner OR
admin", the check is missing.

**Fix:** Either clarify the docstring (current behavior is owner-only
by design) or add `or user.get("is_admin")` to the manageable check.

### 🟡 9. Signature doesn't flow into scanner auto-reply
**File:** `scanner/mail_scanner.py:421-460`

The `reply_to_email` custom behavior builds a `reply_body` from
`_render_template(config.get("reply_template", ""), ctx)` and sends
it via `mail_send.try_send_for_account`. It does **not** append the
account's `signature_html` / `signature_text`. Manual compose does
append the signature (via `openComposeModal`, `app.js:20091`).

**Fix:** When the scanner sends a reply, look up the account's
`signature_html` and concatenate. Either:
- Server-side: in `try_send_for_account`, accept a `signature_html`
  parameter and append.
- Or scanner-side: in the `reply_to_email` action, fetch the
  signature via `db.query_one(...)` and prepend/append to the
  rendered body before passing to `try_send_for_account`.

Server-side is cleaner — keeps the scanner focused on classification
and behavior selection.

### 🟡 10. Quoted reply body doesn't include the deal tag
**File:** `public/app.js:25578-25586` (in the refactored `bindEmailPreviewActionButtons`)

When the user clicks Reply on an email that's linked to deal 123
(visible in the preview's "Linked deals" section), the quoted body
doesn't include `[#123]`. The reply subject is auto-prefixed with
`Re:` but the tag is not propagated to the subject line, so the
scanner won't re-link the outgoing reply. The deal-link is
effectively one-way (incoming → deal), and replies lose the
connection.

**Fix:** When the operator clicks Reply on a message with
`linked_deals`, set the compose modal's subject to
`Re: <original_subject> [#<deal_id>]` automatically. This mirrors
the scanner's incoming behavior in reverse and is a one-line change
in `bindEmailPreviewActionButtons`.

### ⚪ 11. `mail_templates` is global, not per-account
**File:** `init.sql:812-823`

There's no `account_id` column. All templates are visible to all users
in the compose modal's picker. This is probably intentional
(team-shared templates are a feature) but it means:
- An admin at one company using the same dashboard can't have
  company-A vs. company-B template sets.
- A user can't have a personal "drafts" set of templates.

**Fix:** If multi-tenancy is ever needed, add an optional
`account_id INTEGER REFERENCES mail_accounts(id)` column. If not, this
is fine — just remove the audit finding.

### ⚪ 12. No `is_admin` gate on `_handle_mail_template_*` discovery
**File:** `server.py:17190-17267`

Adjacent to #2. The list endpoint (`_handle_mail_templates`) returns
**all** templates to any caller with no auth. If a `limited` user
shouldn't see admin-curated templates, gate the list behind
`_is_limited` or `is_admin`.

### ⚪ 13. No "preview before insert" for templates
**File:** `public/app.js:20454-20464`

Clicking a template immediately replaces the entire compose body
with no confirmation. If the operator had an in-progress draft, it's
gone. A simple `confirm("Replace current body?")` (or a preview
modal) would be a UX improvement.

### ⚪ 14. `_handle_mail_template_update` accepts `body_html: null` as "no change"
**File:** `server.py:17247-17249`

```python
if body_html is not None:
    sets.append("body_html = %s")
    params.append(body_html)
```

This is correct (allows partial updates), but a frontend bug that
sends `body_html: ""` will silently clear the body. Frontend
should always include all fields on PUT, or the handler should treat
empty string differently from null. This is a minor foot-gun, not a
bug per se.

### ⚪ 15. Scanner regex doesn't strip the tag from outgoing subject
**File:** `scanner/mail_scanner.py:1425-1465`

The scanner parses `[#123]` out of the subject to set the
classification, but does **not** rewrite the subject (e.g., to remove
the tag before storage, or to add it if missing). This is correct
behavior — the subject is preserved verbatim — but it means there's
no canonical "where the tag goes" position. Some operators put
`[#123]` at the start, some at the end. Standardizing the
placement (`[#123] <subject>` or `<subject> [#123]`) would help with
search and grouping.

**Fix:** Pick a convention (`[#NNN] <subject>` is what AGENTS.md and
FUTURE_FEATURES.md examples show) and add it to the docs.

---

## What's NOT a problem (verified)

- **Schema** is reasonable: `mail_templates` is a simple CRUD table,
  `mail_accounts.signature_*` is per-account, `mail_outgoing` has the
  audit columns. The `template_id` FK is the only dead thing.
- **Signature injection in `openComposeModal`** is `await api(...)` →
  then injected into a `contenteditable`. The signature content is
  controlled by the account owner / admin (see #8), so the trust
  boundary is the same as for the compose modal itself.
- **Scanner `[#PROJECTID]` regex** is compiled with `re.IGNORECASE`
  and matches `[#123]`, `[#12345]`, etc. No false positives on
  `[#abc]`, `[#]`, `[# 1]` (the latter would match `(\d+)` on the
  space-separated `1`). The body fallback (`body[:5000]`) at
  `mail_scanner.py:1439` is the same regex on the first 5K chars of
  the plain-text body. ✓
- **Scanner auto-reply subject** uses `f"Re: {ctx['subject']}"`
  (`mail_scanner.py:430, 442`). This is the standard convention and
  is correct.

---

## Summary

| Sev | Count | Top items |
|---|---|---|
| 🔴 Critical | 2 | #1 raw innerHTML injection, #2 missing auth/ownership on template CRUD |
| 🟠 High | 3 | #3 no size cap, #4 dead `template_id` FK, #5 no `[#PROJECTID]` standardization for outgoing |
| 🟡 Medium | 4 | #6 inconsistent naming, #7 placeholder engine scattered, #8 no admin on signatures, #9 signature doesn't flow into scanner reply, #10 reply doesn't propagate tag |
| ⚪ Low / Nit | 4 | #11/#12/#13/#14/#15 |

## Recommended fixes, prioritized

If you want to **standardize `[#PROJECTID]` for outgoing email** with
minimal disruption, the path is:

1. **Today (1–2 hrs):** Apply the XSS fix (#1) — run all template body
   and signature HTML through `sanitizeHistoryHtml` before
   `innerHTML` assignment. This is the same sanitizer already used
   for the email preview, and the change is local to 3 lines in
   `app.js` (20460, 20091, 22173/22176).
2. **Today (15 min):** Add `_require_auth` + `is_admin` to all five
   template handlers (#2). Add `_is_limited` check on the create
   endpoint. Easy wins.
3. **This week (1–2 hrs):** Standardize on a single format (#5, #6).
   - Pick `[#NNN]` as canonical; deprecate `[#DEAL-NNN]`.
   - Add a per-account `auto_tag_outgoing_subject` toggle (default
     off). When on and a deal is linked, auto-insert `[#NNN]` into the
     subject on send.
   - Add a structured `{project_id}` placeholder to
     `mail_templates` and apply it server-side in
     `_handle_mail_send` (so the tag is always current, even if the
     deal changes between template insert and send).
   - Add a single regex test that locks the format in.
4. **This week (30 min):** Populate `mail_outgoing.template_id` on
   send (#4) so we have an audit trail of which template produced
   which email.
5. **Next sprint:** Auto-append the account signature to scanner
   auto-replies (#9), auto-propagate `[#NNN]` to reply subjects
   when the operator clicks Reply on a linked email (#10),
   standardize placeholder documentation (#7).

## What this audit deliberately does NOT cover

- **Photo report templates** (`init.sql:620` `photo_report_templates`)
  and **document templates** (`init.sql:646` `document_templates`).
  These are separate features (the report builder, document
  generation) and are out of scope. They have the same general
  concerns (raw HTML, no admin gate) but are not the email pipeline.
- **Mail drafts** (`mail_outgoing` with folder='Drafts') — related to
  outgoing mail but not to template rendering. Handled by separate
  draft handlers.
- **The scanner's full classifier** — only the `[#PROJECTID]`
  auto-link path was inspected in detail. Custom behaviors
  (`create_deal`, `create_task`, `reply_to_email`) were skimmed for
  placeholder usage but not deep-audited.
- **Outlook / Gmail OAuth UI flows** — these affect the scanner's
  authentication, not the template pipeline.
