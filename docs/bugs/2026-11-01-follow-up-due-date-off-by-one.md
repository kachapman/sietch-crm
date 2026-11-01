# Bug: follow-up due date off by one — project page shows Oct 25, dashboard shows Oct 26

- Date filed: 2026-11-01
- Page / record: `project.html?id=30` — Wayne Muehlbauer, job 425
- Live site: `crm.publicadjustermidwest.com`
- Status: **confirmed by code inspection; fix NOT applied** (planning-only session)
- Priority: **High** (customer-visible wrong date on the full project page; stored data is correct)

## 1. Symptoms

- Full project page (`project.html?id=30`) shows follow-up due date as **Oct 25, 2026**.
- Dashboard project popup and dashboard card both show **Oct 26, 2026**.
- The history log's last change (Oct 23, by Claudiu Chereji) set the follow-up due date
  from `2026-10-23` to **`2026-10-26`** — so the dashboard matches the stored value
  and the project page is **one day early**.
- Suspected cause from the session (timezone/date-parsing) is confirmed below:
  the project page parses the date-only API string with `new Date()`, the
  dashboard does not.

## 2. Data path with exact code locations

### 2.1 Stored (PostgreSQL)

- `init.sql:207` — `expected_close_date DATE` on `opportunities`
  (plain calendar date, no time component, no timezone).
- `server.py:5773` — create path reads `payload.get("expectedCloseDate")`
  into `expected_close` for the INSERT at `server.py:5776-5782`.
- `server.py:5825-5839` — update path maps `expectedCloseDate →
  expected_close_date` and runs the UPDATE.
- `server.py:5804-5810` — `TRACKED_FIELDS` labels this column
  `"Follow-up Due Date"`; `server.py:5854-5880` writes the history change
  event. The history entry recorded the raw values (`2026-10-23` →
  `2026-10-26`) and displays them correctly — the history pipeline is
  **not** part of this bug.

### 2.2 Served by the API

Both project endpoints serialize the DATE column with plain `str()`,
producing a date-only string `"2026-10-26"` (correct, no shift here):

- `server.py:5559` — project list:
  `"expectedCloseDate": str(r[6]) if r[6] else None`
- `server.py:5601` — single-project GET (`GET /api/v2/projects/<id>`,
  the endpoint `project.html` uses at `project.html:1224`):
  `"expectedCloseDate": str(row[6]) if row[6] else None`

The dashboard group tiles read the same field through
`fetchOpportunitiesForGroup` / `buildFilterQuery` (same list shape).

### 2.3 Rendered — dashboard card (CORRECT, Oct 26)

- `public/app.js:12318` — `renderCard` computes
  `const dueLabel = formatOppDueLabel(opp)` and renders it at
  `public/app.js:12328-12331` (`Due ${dueLabel}`).
- `public/app.js:11391-11396` — `formatOppDueLabel` parses via
  `parseCrmDateOnly` and formats with `toLocaleDateString`, i.e. local
  calendar day, no UTC shift.
- `public/app.js:11369-11374` — `parseCrmDateOnly` (the safe parser):
  date-only strings are constructed as **local** midnight:

```js
function parseCrmDateOnly(raw) {
  if (raw == null || raw === "") return null;
  const m = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return new Date(raw);
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}
```

### 2.4 Rendered — dashboard popup (CORRECT, Oct 26)

- `public/app.js:28581-28586` — `buildOpportunityPreviewStandardFields`
  renders `formatPreviewDueDate(dueDateRaw)` with label "Follow-up Due Date".
- `public/app.js:28507-28511` — `formatPreviewDueDate` uses the same safe
  `parseCrmDateOnly`, then `toLocaleDateString()`:

```js
function formatPreviewDueDate(raw) {
  const d = parseCrmDateOnly(raw);
  if (!d || Number.isNaN(d.getTime())) return String(raw ?? "");
  return d.toLocaleDateString();
}
```

Related popup paths also use the safe parser and are not affected:
`public/app.js:31483-31487` (bookmark-tab due label),
`public/app.js:30631` (search popup via `formatOppDueLabel`),
`public/app.js:39699`, `public/app.js:39966` (inline edit date),
`public/app.js:11398-11405` `dueDateToInputValue`.

### 2.5 Rendered — full project page (WRONG, Oct 25) ← the bug

- `project.html:1224-1230` — page fetches `GET /api/v2/projects/<id>` and
  calls `renderFields(opp)`.
- `project.html:532` — extracts
  `data.dueDate || data.DueDate || data.expectedCloseDate || data.ExpectedCloseDate`.
- `project.html:543` — renders `"Due: " + formatDate(dueDate)`.
- `project.html:515-523` — `formatDate` parses with **bare `new Date(s)`**:

```js
function formatDate(s) {
  if (!s) return "";
  try {
    var d = new Date(s);   // <-- date-only string parsed as UTC midnight
    ...
    return months[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
  } catch(e) { return String(s); }
}
```

## 3. Root-cause analysis

Per the ECMAScript spec, `new Date("2026-10-26")` (ISO date-only form) is
parsed as **UTC midnight**, `2026-10-26T00:00:00Z`. The getters used for
display (`getMonth` / `getDate` / `getFullYear`) return **local** time.
In any US timezone (UTC-4/UTC-5/UTC-6 in late October), UTC midnight is
still the previous local calendar day:

- `2026-10-26T00:00:00Z` → Oct 25, 20:00 EDT / 19:00 CDT.

Hence the project page shows **Oct 25** while the stored value and the
dashboard (which constructs local midnight via `parseCrmDateOnly`,
`public/app.js:11369-11374`) show **Oct 26**. The dashboard is right;
`project.html`'s `formatDate` is the single outlier.

Note the write path is safe for US timezones and needs no change for this
bug: `toApiExpectedCloseDate` (`public/app.js:11407-11412`) and
`serializeCrmTimestamp` (`public/app.js:11421-11426`) serialize noon local
time (`date + "T12:00:00"` → `toISOString()`), and Postgres casts the ISO
timestamp to DATE, preserving the calendar day for UTC-12…UTC+11.
(Residual edge: UTC+13/+14 could shift the day on write; see §6.)

## 4. Other places with the same date-only parsing pattern

| # | Location | What it does | Verdict |
|---|----------|--------------|---------|
| 1 | `project.html:515-523` `formatDate` | Formats due date AND event/created datetimes on the project page (`:543`, `:548`, `:659`) | **THE BUG.** Mixed-use function: correct for datetimes with offsets, wrong for date-only strings |
| 2 | `public/app.js:10850` `new Date(card.due).toLocaleDateString()` | Local-kanban card due display | Same off-by-one risk for date-only input; **Low** (local kanban only, not CRM deals) |
| 3 | `public/app.js:10937-10950` `getCardDueInfo` — `new Date(due)` day-diff math | Local-kanban "Due in N days / Overdue" labels | Hour shift can flip the label near midnight; **Low** (local kanban only) |
| 4 | `public/app.js:42248-42265` `toLocalDate`/`toLocalDateTime`, used at `:42319` to prefill task-edit `<input type="date">` | Task due-date editing | Same pattern; fix only after confirming the `tasks.due_date` column type (DATE vs TIMESTAMPTZ); **Medium-low** |
| 5 | `public/app.js:41934`, `public/app.js:42173` task due display via `new Date(...)` | Task due labels | Same caveat as #4; **Medium-low** |
| 6 | `server.py:17203-17242` overdue / due-this-week queries compare `expected_close_date` to `NOW()` | Server-side "red" and due-week lists | DATE-vs-timestamptz comparison follows the droplet's PG timezone (UTC per the `server.py:1169` `_ts` docstring), so "today" can differ from the US calendar day late in the day. Display is unaffected; behavior note only; **Low** |

Correctly-handled datetime sites that must NOT be "fixed" by a blanket
change (they carry real time components and `new Date` is right):
`public/app.js:17753`, `:17767`, `:26900`, `:27567`, `:28500`,
`:46001`/`:46009`, and the `:12322` fallback (datetimes only —
`parseCrmDateOnly` result is preferred when present). Safe date-only
sites already using `parseCrmDateOnly`: `:11744` (`isRedOpportunity`),
`:31484`, `:39699`, `:39966`, `:11391-11405`.

## 5. Proposed fix

1. **Project page (the fix):** teach `project.html`'s `formatDate`
   (`project.html:515-523`) to branch on shape — if the input matches
   `/^\d{4}-\d{2}-\d{2}/` (or is exactly 10 chars), construct local
   midnight exactly like `parseCrmDateOnly` (`public/app.js:11369-11374`)
   instead of `new Date(s)`; keep `new Date` for genuine datetimes
   (event dates at `project.html:659`, created at `:548`). One shared
   date-only formatter (copy the 6-line parser, or extract a shared
   snippet) fixes the bug without touching datetime rendering.
2. **Same-pattern sweep:** apply the same date-only helper to #2/#3 above;
   for #4/#5 check the `tasks.due_date` column type first (if TIMESTAMPTZ,
   `new Date` is correct and only DATE inputs need the helper).
3. **No API/DB change needed:** `str(DATE)` already emits date-only, and
   the noon-local write path preserves the calendar day in US zones.
   Optionally document the UTC+13/+14 write edge (§3) as a known limitation.

## 6. Test plan

- **Unit (no server needed):** assert the fixed formatter returns
  "Oct 26, 2026" for `"2026-10-26"` under `TZ=America/Chicago` and
  `TZ=America/New_York`, plus a DST-boundary sweep (Oct 25–Nov 2) and a
  datetime regression case (`"2026-10-26T14:00:00Z"` still renders local time).
- **Manual:** set a project's `expected_close_date` to `2026-10-26`, load
  `project.html?id=<id>` with the OS timezone set to US Central → Due shows
  Oct 26; dashboard card and popup unchanged (Oct 26); history still shows
  `2026-10-23 → 2026-10-26`.
- **Regression:** project-page event timestamps still show correct local
  time (they go through the datetime branch); run the JS suite
  (`tests/test_pure_helpers.mjs`) and the Python suite per AGENTS.md.
- **No migration:** storage format unchanged (still DATE).

## 7. Minor observations from the same session (assessed)

1. **Tiles briefly show "Refreshing…" with 0 deals — NOT A BUG.**
   `public/app.js:43488` deliberately injects a `Refreshing deals…`
   placeholder after 200 ms during `refreshGroup`, and
   `public/app.js:43545` sets the global `Refreshing…` status during
   `refreshAll`; tile DOM renders asynchronously after fetch
   (`public/app.js:43506-43513`). A brief 0-deal flash on slow loads is
   the expected loading state. Priority: none (optional polish: skeleton
   counts — not requested).
2. **4–8 dashboard console errors — likely benign, worth one cleanup pass.
   Priority: Low.** Candidate sources found in code (not reproduced):
   SSE reconnect/parse logging (`public/app.js:233-245`), notes load
   failures (`:9255`, `:43573`), sync-poll errors (`:32783-33069`),
   mail-inbox link errors (`:24289`, `:24334`, `:25569`), branding load
   (`:44995`), and known leftover debug logs from the doc-preview work
   (`:40910`, `:41213` — CHANGELOG notes these were kept temporarily).
   Suggested action: reproduce with DevTools open, match messages to this
   list, remove the preview debug logs.
3. **Popup "Open Project" icon click unresponsive — NOT CONFIRMED as a bug;
   Priority: Low.** The icon IS the anchor: `createCrmOpenLink`
   (`public/app.js:25895-25906`) builds an `<a target="_blank">` whose
   innerHTML is just the SVG (`ICON_EXTERNAL_LINK`, `:25864`), injected
   into `span#opp-preview-open-crm-wrap` (`index.html:1801`,
   `public/app.js:28917-28923`). The handler only `stopPropagation`s
   (no `preventDefault`), and the CSS box is a 2 rem button
   (`styles.css:6613-6632`). Clicking the icon itself should open a new
   tab, consistent with "the link target worked". Likely explanations for
   a dead click: the click landed on the surrounding `<span>` padding
   rather than the anchor, a popup-blocker swallowed the `_blank`
   navigation, or `setOpportunityPreviewCrmLink` re-rendered the wrap
   mid-click. Suggested check: in DevTools, confirm the click target is
   `a.opp-preview-open-crm` vs the span, with popup-blocker disabled.

## 8. Explicit non-bugs (confirmed)

- Dashboard card click not opening the popup is **intended** behavior.
- The project page is **meant** to be lightweight with no tag editor.

## 9. Priority summary

| Item | Priority |
|------|----------|
| Project-page due-date off-by-one (§5.1) | **High** |
| Task date-input/display same-pattern check (§4 #4–#5) | Medium-low |
| Local-kanban same-pattern (§4 #2–#3) | Low |
| Server `NOW()` vs DATE note (§4 #6) | Low (note only) |
| Console-error cleanup (§7.2) | Low |
| "Open Project" click repro (§7.3) | Low |
| "Refreshing…" flash (§7.1) | None — intended |
