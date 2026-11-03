# Invoice + Certificate of Completion PDF upgrade (Sherwood "modern" builders)

**Date:** 2026-11-03
**Branch:** `invoice-coc-upgrade`
**Base:** `main` @ `c637526`
**Status:** implemented + tested locally. **NOT deployed.** Needs a Docker
image rebuild (new `reportlab` dependency) and applies **migration 045**.
Deploy late at night per Ken.

> **Update (2026-11-03, later session):** Ken reviewed the open decisions and
> they are now **implemented**. See §1a for the decisions and what changed.

---

## 1a. Ken's decisions (implemented 2026-11-03)

| # | Decision | Status |
|---|---|---|
| 1 | The modern (ReportLab) layout is the **default** for both invoice and COC. Classic fpdf stays available as a fallback/option in the "PDF Layout" field. | **Done.** `DOC_LAYOUT_DEFAULT = "modern"`. |
| 2 | **Hide $0.00 lines** on the invoice (any line item whose amount is $0.00 is not printed). | **Done.** Both layouts. |
| 3 | **NO homeowner signature line** on the COC. Remove it from the modern COC (and the classic one if it has it). | **Done.** The upstream branch was deleted from `build_coc`; the `homeowner_line` option no longer exists. The classic COC never had one. |
| 4 | Add **website, license number and signer-title** fields to the document templates, with a new migration, exposed in the template settings UI, shown on BOTH the invoice PDF and the COC PDF (both layouts). Empty values must be omitted, not print blank labels. | **Done.** Migration **045**. |
| 5 | Keep the current Sietch COC wording as the default text (do not swap in the Sherwood wording). | **Done.** `COC_STATEMENT = "Work completed per agreement."` unchanged. |
| 6 | Keep the builders **copied** into Sietch (the `doc_pdf/` package). Do not turn it into a shared package or dependency on Sherwood. | **Done.** No change — `doc_pdf/` stays a local copy. |
| 7 | **No "Base charge" UI field.** Do not add one. | **Done.** No field added. `base_amount` remains a server-side API field that the modal does not send. |
| 8 | Make the **CLASSIC COC** use the entered `completion_date` (same behaviour and fallback as the modern COC). | **Done.** `_create_coc_pdf()` takes a `completion_date` argument. |

### Migration number: **045**

`migrations/045_document_templates_branding_extras.sql`.

Numbering note: the highest migration on `invoice-coc-upgrade` was **042**.
The separate `fix-mail-attachments` branch uses **043** and **044**, so this
branch deliberately starts at **045** to avoid a filename collision when the
branches merge. 043 and 044 are absent from this branch.

```sql
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS company_website  TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS company_license  TEXT NOT NULL DEFAULT '';
ALTER TABLE document_templates
    ADD COLUMN IF NOT EXISTS signature_title   TEXT NOT NULL DEFAULT '';
```

Additive, idempotent (`IF NOT EXISTS`), empty-string defaults so existing
template rows keep working and print no extra labels. `server.py` also
carries the same inline `ALTER`s (the existing pattern for
`show_branding_footer`) so a database that skipped the migration files
still gets the columns.

### Where the new fields appear

| Field | Column | Invoice (modern) | Invoice (classic) | COC (modern) | COC (classic) |
|---|---|---|---|---|---|
| Website | `company_website` | company block | company block | company block | company block |
| License | `company_license` | company block | company block | company block | company block |
| Signer title | `signature_title` | signature block | signature block | signature block | signature block |

* The license is prefixed with `"License "` unless it already starts with
  `"lic"` or `"#"` (the upstream Sherwood rule, kept).
* The signer title prints under the name on the invoice, and as
  `"<name>, <title>"` on the COC.
* **Empty values are omitted entirely** — no blank line, no blank label.
  This is asserted in the tests for all four document types.

### Admin UI

Three new fields in the document-template editor
(`public/index.html` + `public/app.js`):

| Label | Element id | Saved as |
|---|---|---|
| Website | `doc-tpl-admin-website` | `company_website` |
| License Number | `doc-tpl-admin-license` | `company_license` |
| Signature Title | `doc-tpl-admin-sig-title` | `signature_title` |

The admin API (`POST /api/v2/admin/document-templates` and
`PUT /api/v2/admin/document-templates/<id>`) persists all three.

### $0.00 lines

`doc_pdf/mapping.py: build_items()` skips any row whose amount is exactly
`0.00`, and `server.py: _create_invoice_pdf()` (the classic renderer) does
the same. A zero-amount row contributes nothing to the grand total either
way, so **the total is unchanged** — only the row disappears.

---

## 1. What changed

Sietch CRM rendered its Invoice and Certificate of Completion PDFs with
hand-rolled **fpdf2** code inside `server.py`
(`_create_invoice_pdf` / `_create_coc_pdf`). Those renderers produced a
plain, serviceable document — but they also had real defects:

* The footer printed a literal placeholder: `Page 1 of (1)` (fpdf2's
  `{{nb}}` alias is never substituted without an `AliasNb` call).
* No accent-coloured header bar, no zebra striping, no branded footer.
* The COC ignored the `completion_date` the modal has always posted and
  stamped today's date instead.
* The COC had no property-owner or contractor signature block unless a
  signature image was configured, and no page numbering.

This change ports the **Sherwood Toolbox "v26.5" document builders**
(kachapman/sherwood-toolbox @ `3d5bf83`, "v28.1") into Sietch as a new
`doc_pdf/` package and wires the four generation endpoints to them by
default. The classic fpdf2 renderers are kept behind a switch so Ken can
roll back without a code redeploy.

### New files

| File | Purpose |
|---|---|
| `doc_pdf/__init__.py` | Package docstring / export list. |
| `doc_pdf/pdf_common.py` | Port of `toolbox/tools/documents/pdf_common.py` — shared styles, accent helpers (`accent` / `on_accent` / `text_accent` / `tint`), `company_lines`, `money`, `numbered_canvas` (accent bar + "Page x of y" footer). |
| `doc_pdf/invoice_pdf.py` | Port of `toolbox/tools/documents/invoice_pdf.py` — `build_invoice(out_path, brand, job, items) -> total`. |
| `doc_pdf/coc_pdf.py` | Port of `toolbox/tools/documents/coc_pdf.py` — `build_coc(out_path, brand, job, opts)`. |
| `doc_pdf/mapping.py` | **Sietch-specific** pure mapping layer: `document_templates` row + request payload → `brand` / `job` / `items` / `opts`. No DB, no HTTP, no writes. |
| `tests/test_invoice_coc_modern.py` | 60 tests: mapping, builders, layout switch, the four HTTP handlers with a mocked DB, and the migration 045 guard. |
| `tests/test_reportlab_dependency.py` | 6 tests guarding the new `reportlab` dependency (Dockerfile pip line, requirements pin, `doc_pdf/` COPY). |
| `migrations/045_document_templates_branding_extras.sql` | Adds `company_website`, `company_license`, `signature_title` to `document_templates`. |
| `scripts/make_invoice_coc_review.py` | Builds the sample review pack (modern + classic + multi-page + with-extras + zero-line) from the local DB. |

### Modified files

| File | Change |
|---|---|
| `server.py` | New `_resolve_doc_layout()` / `_modern_pdf_ready()` helpers; new `_create_invoice_pdf_modern()` / `_create_coc_pdf_modern()` renderers; the four generation handlers now dispatch on the selected layout; the COC handlers honour `completion_date`; the classic renderers gained the website / license / signer title and the `$0.00` hiding; the admin template API persists the three new columns; inline `ALTER`s for migration 045. |
| `public/app.js` | "PDF Layout" field on the Invoice and COC modals; sends `layout` in the payload; the admin template editor loads and saves Website / License Number / Signature Title. |
| `public/index.html` | The three new admin template fields. |
| `requirements.txt` | `reportlab>=4.0,<5`. |
| `Dockerfile` | `reportlab>=4.0,<5` on the `RUN pip install` line; `COPY doc_pdf ./doc_pdf`. |
| `tests/test_pdf_dependency.py` | Paths made relative to the file (it hardcoded the main checkout). |
| `tests/test_reports.py` | Tolerates a missing `.env` (the file is gitignored; worktrees do not have one). |
| `tests/test_onlyoffice_import.py` | Paths made relative to the file. |
| `.gitignore` | `tmp/` (the review pack is never committed). |

---

## 2. Field mapping

### 2.1 `brand` (drives both layouts)

| Builder key | Sietch source | Notes |
|---|---|---|
| `name` | `document_templates.company_name` | |
| `address` | `document_templates.company_address` | |
| `city_state_zip` | `document_templates.company_city_state_zip` | |
| `phone` | `document_templates.company_phone` | |
| `email` | `document_templates.company_email` | |
| `accent` | `document_templates.primary_color` | Invalid values fall back to `#2c5130` upstream. |
| `logo_path` | `document_templates.logo_path` | Resolved against `ROOT`; **a missing file resolves to `None`, never crashes**. |
| `invoice_title` | `document_templates.invoice_title` | Default `INVOICE`. |
| `coc_title` | `document_templates.coc_title` | Default `CERTIFICATE OF COMPLETION`. |
| `footer_text` | `document_templates.show_branding_footer` | `"Generated by Sietch CRM"` when true, `""` when false. |
| `website` | `document_templates.company_website` | **Added in migration 045.** Empty → omitted from the document. |
| `license` | `document_templates.company_license` | **Added in migration 045.** Empty → omitted. Prefixed with `"License "` unless it already starts with `"lic"` / `"#"`. |

### 2.2 `job`

| Builder key | Sietch source | Notes |
|---|---|---|
| `customer_name` | `payload.customer_name` | Falls back to `opportunities.title`, then `"Customer"`. |
| `street` | `payload.street` | |
| `city_state_zip` | `payload.city_state_zip` | |
| `job_number` | `payload.project_number` | |
| `insurance_claim` | `payload.claim_number` | |
| `sales_rep` | `payload.sales_rep` → `template.default_sales_rep` | |
| `invoice_number` | `payload.invoice_number` → `opportunities.project_number` | **No running counter is introduced** — Sietch's existing behaviour is preserved exactly. Sherwood's `next_invoice_number` logic is deliberately NOT ported. |
| `invoice_date` | `payload.invoice_date` → today (`MM/DD/YYYY`) | |
| `terms` | `payload.terms` → `template.default_terms` → `"Due on receipt"` | |
| `invoice_notes` | `payload.notes` | |
| `completion_date` | `payload.completion_date` → today | **Bug fix** — the classic renderer ignored the posted value. |
| `note` | `payload.note` | COC free-text note. |

### 2.3 `items`

| Builder key | Sietch source | Notes |
|---|---|---|
| `(description, amount)` | `payload.base_amount` + `payload.line_items` | `base_amount` is folded in as the **first** row, labelled with `terms` (fallback `"Services"`) — exactly the label the classic fpdf2 renderer used. |

**Zero-amount rows:** upstream Sherwood drops rows whose amount is `0`
(`if float(a or 0) != 0`). The classic Sietch renderer *also* skipped a row
only when the description was blank **and** the amount was `0` — a row with
a description and a `0` amount was rendered. To keep the grand total
identical to what Sietch shows today, the modern builder **keeps upstream's
drop rule**: a `0`-amount row contributes nothing to the total either way,
so the printed total is unchanged; only a descriptive `$0.00` row is no
longer listed. Documented here so the difference is not a surprise.

**Totals:** `total = base_amount + sum(line_items)` in both renderers, so the
grand total is unchanged for any given input. Asserted by
`test_items_total_matches_classic_grand_total`.

### 2.4 `opts` (COC)

| Builder key | Sietch source | Notes |
|---|---|---|
| `statement` | — (Sietch wording) | `"Work completed per agreement."` — the closest equivalent to the classic renderer's note fallback. |
| `signature_path` | `document_templates.signature_path` | Only when `include_signature` is true. Resolved against `ROOT`; missing file → `None`. |
| `signer_name` | `payload.signature_name` → `template.signature_name` → `sales_rep` | |
| `signer_title` | `document_templates.signature_title` | **Added in migration 045.** Empty → omitted. |

> The `homeowner_line` option was **removed** (Ken's decision 3): the Sietch
> COC never draws a property-owner signature line, so the builder branch was
> deleted rather than left behind a flag.

### 2.5 Invoice signature (Sietch-only addition)

Upstream `build_invoice` has **no** signature block. The classic Sietch
invoice drew one (signature image + "By <name>" + "Date: <date>"). To avoid
losing that feature, `build_invoice` gained an optional `signature` dict:

```python
build_invoice(out_path, brand, job, items,
              signature={"path": ..., "name": ..., "date": ...},
              root=...)
```

The block is only drawn when `include_signature` is true. The image is
optional — when the file is missing the name/date lines are still printed,
so the signer still appears on the document.

---

## 3. Deviations from upstream `3d5bf83`

Everything below is intentional and documented in the header comment of the
relevant ported file.

| # | Deviation | Why |
|---|---|---|
| 1 | `from doc_pdf.pdf_common import ...` instead of `from .pdf_common import ...` | Sietch keeps the package at the repo root next to `server.py`. |
| 2 | `logo_flowable()` / `build_invoice()` / `build_coc()` take an optional `root` argument | Sietch stores logos/signatures as repo-relative paths under `data/`. With `root=None` the behaviour is byte-identical to upstream. |
| 3 | `build_invoice()` accepts an optional `signature` dict and renders a signature block | Upstream has none; the classic Sietch invoice did. Sietch-only, additive. |
| 4 | `build_coc()` honours `opts["signature_path"]` only when the caller asks for it | Sietch gates signatures on `document_templates.show_signature`; upstream always draws the block. |
| 5 | COC title casing: `"CERTIFICATE OF COMPLETION"` → **"Certificate of Completion"** | Upstream's `str.title()` yields "Certificate Of Completion". Sietch keeps the short function word lowercase so the heading matches the classic wording. |
| 6 | The **property-owner signature line was removed** (upstream draws a second "Property owner" block gated on `opts["homeowner_line"]`, default `True`) | Ken's decision 3 (2026-11-03): the Sietch COC never has a homeowner line. The branch was deleted from `build_coc`; the `homeowner_line` option no longer exists. |
| 7 | `opts["statement"]` uses the Sietch wording, not Sherwood's | Ken's decision 5: keep the Sietch wording. |
| 8 | `brand["website"]` / `brand["license"]` come from the new `company_website` / `company_license` columns | Added in migration 045 (Ken's decision 4). Empty → omitted. |
| 9 | `opts["signer_title"]` comes from the new `signature_title` column | Added in migration 045 (Ken's decision 4). Empty → omitted. |
| 10 | No running invoice-number counter | Sherwood's `branding.py` has `invoice_prefix` + `next_invoice_number`. Sietch's invoice number is the payload value or the project's `project_number`; that behaviour is preserved exactly. |
| 11 | `_money()` helper duplicated in `doc_pdf/mapping.py` | `server.py` cannot be imported by the pure mapping module without pulling in the DB pool. The helper is a 6-line mirror of `server._money`. |

---

## 4. Layout switch

The four endpoints are unchanged in URL, request shape and response shape:

```
POST /api/v2/projects/<id>/invoice
POST /api/v2/projects/<id>/coc
POST /api/v2/documents/invoice
POST /api/v2/documents/coc
```

Response shape is unchanged: `201 {id, title, fileSize, mimeType[, historyNoteCreated]}`.
File locations, the `project_documents` insert (including
`opportunity_id = NULL` for standalone) and the `history_events` action note
are unchanged.

### Choosing the layout

| Precedence | Source | Scope |
|---|---|---|
| 1 | `payload["layout"]` = `"classic"` \| `"modern"` | One request. Set from the new "PDF Layout" field in the Invoice / COC modals. |
| 2 | `SIETCH_DOC_LAYOUT` env var = `"classic"` \| `"modern"` | Whole server. Changing it needs a container restart, **not** a code redeploy. |
| 3 | `"modern"` | Default. |

Unknown values fall back to the default. `classic` keeps using fpdf2, so
`tests/test_pdf_dependency.py` (fpdf2 must stay in the image for the photo
reports) still applies.

### Wiring detail

Each handler now:

1. reads the body first (so the layout can be chosen before the dependency
   check),
2. resolves the layout,
3. checks the *matching* dependency (`reportlab` for modern, `fpdf2` for
   classic),
4. resolves the template / line items / defaults exactly as before,
5. **writes the resolved defaults back onto the payload** so the modern
   renderer sees exactly the same values the classic one did
   (`customer_name`, `invoice_number`, `invoice_date`, `terms`, `sales_rep`,
   `claim_number`, `project_number`, `street`, `city_state_zip`, `notes`,
   `base_amount`, `include_signature`, `signature_name`, and for the COC
   `note` + `completion_date`),
6. calls the modern or classic renderer.

---

## 5. Gaps

The `website` / `license` / `signer_title` gaps were **closed** by
migration 045 (see §1a). The remaining ones:

| Gap | Impact | Status |
|---|---|---|
| `base_amount` has no UI field | The Invoice modal only sends `line_items`; `base_amount` is a server-side API field that the frontend never sets (so it is always `0` in practice). | **Open by decision** — Ken's decision 7: no "Base charge" UI field. |
| No `website` / `license` / `signer_title` on the **photo report** templates | `photo_report_templates` is a separate table and is unchanged. | Open, out of scope. |
| The classic invoice's footer still prints `Page 1 of (1)` | fpdf2's `{{nb}}` alias is never substituted in the classic renderer. The modern one prints `Page 1 of 1` correctly. | Open — the classic renderer is a fallback; fixing it would touch a path Ken may not use. |

---

## 6. Decisions needing Ken

**All resolved on 2026-11-03** (see §1a). Kept here for the record:

1. **Default layout.** → **modern** (decision 1). Classic stays one env
   var away (`SIETCH_DOC_LAYOUT=classic`) or one field in the modal.
2. **Property-owner signature line on the COC.** → **removed** (decision 3).
3. **Zero-amount rows.** → **hidden** (decision 2).
4. **COC statement wording.** → **keep the Sietch wording** (decision 5).
5. **Website / licence / signer-title columns.** → **added via migration
   045** (decision 4).
6. **Copy vs shared module.** → **keep the copy** (decision 6).
7. **Base charge UI field.** → **not added** (decision 7).
8. **Deploy timing.** → still open: the image needs a rebuild for
   `reportlab`, and migration 045 must run. Deploy late at night per Ken.

---

## 7. Deploy notes

* **New dependency:** `reportlab>=4.0,<5` — added to `requirements.txt` **and**
  to the hardcoded `RUN pip install` line in the `Dockerfile` (the image does
  not use `requirements.txt`; this is the exact failure mode
  `tests/test_pdf_dependency.py` was written to catch).
* **New package:** `doc_pdf/` — added to the `Dockerfile` as
  `COPY doc_pdf ./doc_pdf` (the `COPY *.py ./` glob does not pick up a
  package directory).
* **New migration:** `045_document_templates_branding_extras.sql` — applied
  automatically by the migration runner on the next container boot (and by
  the inline `ALTER`s in `server.py`). Additive + idempotent.
* **Deploy steps:** the standard `git pull` → `docker compose build` →
  `docker compose up -d` (see `docs/UPDATE_AND_DEPLOY.txt`).
* **Rollback without a redeploy:** `SIETCH_DOC_LAYOUT=classic` in the
  container environment, then restart. The classic fpdf2 renderers are still
  in `server.py`.
* **Rollback to the old code:** `git revert` the upgrade commits and rebuild
  the image. The migration is additive, so leaving it applied is harmless.

---

## 8. Test results

All runs throttled with `nice -n 10 ionice -c3`.

| Suite | Result |
|---|---|
| `tests/test_invoice_coc_modern.py` | **60 passed**, 0 failed |
| `tests/test_reportlab_dependency.py` | **6 passed**, 0 failed |
| `tests/test_pdf_dependency.py` | **2 passed**, 0 failed |
| `tests/test_reports.py` | **33 passed**, 0 failed |
| `tests/test_doc_download_auth.py` | **19 passed**, 0 failed |
| `tests/test_documents_batch_download.py` | **6 passed**, 0 failed |
| `tests/test_documents_pagination.mjs` | **16 passed**, 0 failed |
| Full Python suite (`tests/test_*.py`, 63 files) | **61 passed, 2 failed** |
| Full Node suite (`tests/test_*.mjs`, 35 files) | **35 passed**, 0 failed |

The 2 Python failures — `tests/test_scanner_toggle_gating.py` and
`tests/test_sync_pull_deals_fields.py` — are **pre-existing** and unrelated:
both fail identically on `main` @ `c637526` in the main checkout (they are
source-level guards for the mail scanner / deal sync, which this change does
not touch). Re-verified after the Ken-decisions work: both still report
`FAILED (failures=1)` on `main`.

### Tests added for the Ken decisions

| Decision | Test |
|---|---|
| 1 — modern is the default | `test_default_layout_constant_is_modern`, `test_resolve_doc_layout_defaults_to_modern` |
| 2 — $0.00 lines hidden | `test_items_hide_zero_amount_rows`, `test_items_total_unchanged_by_hidden_zero_rows`, `test_zero_amount_row_is_not_printed` |
| 3 — no homeowner line | `test_coc_opts_statement_and_no_homeowner_line`, `test_coc_has_no_property_owner_line` |
| 4 — the new fields | `test_brand_maps_website_and_license`, `test_company_lines_include_website_and_license`, `test_company_lines_omit_empty_website_and_license`, `test_coc_opts_signer_title_from_template`, `test_invoice_signature_carries_the_signer_title`, `test_invoice_shows_website_and_license_when_set`, `test_invoice_omits_website_and_license_when_empty`, `test_invoice_signature_shows_the_signer_title`, `test_coc_shows_website_and_license_when_set`, `test_coc_omits_website_and_license_when_empty`, `test_coc_shows_the_signer_title`, `test_branding_extras_render_on_both_layouts`, `test_branding_extras_omitted_when_empty` |
| 4 — the migration | `TestBrandingExtrasMigration` (8 tests): the file exists, the number does not collide with 043/044, it adds the three columns, it is idempotent, it applies cleanly twice against a scratch table, `server.py` inlines the same ALTERs, the admin API accepts the fields, the admin UI exposes them |
| 5 — Sietch COC wording kept | `test_coc_opts_statement_and_no_homeowner_line` (asserts `COC_STATEMENT`) |
| 8 — classic COC completion date | `test_project_coc_classic_uses_completion_date`, `test_project_coc_classic_defaults_completion_date_to_today` |

### What the new suite covers

**Mapping (pure):** brand column mapping; `show_branding_footer` on/off;
website/licence blank gaps; missing logo → `None`; job field mapping;
`completion_date` default-to-today; `base_amount` as the first row labelled
by `terms`; base amount of `0` omitted; explicit line items overriding the
payload; total equal to the classic grand total; COC opts (statement,
`homeowner_line=False`, signer name, empty signer title); signature gating
by `include_signature`.

**Builders:** invoice text contains company name, title, invoice #, date,
customer, street, claim #, job #, every line description + amount, the
total, the notes and "Page 1 of 1"; the branding footer present/absent per
`show_branding_footer`; the invoice signature block; a **60-line invoice**
producing multiple pages with correct "Page x of y" and the table header
repeated on every page; the COC text contains the title, customer, address,
claim #, job #, completion date and the contractor signature label; the COC
has **no** "Property owner" line; missing logo + missing signature files do
not crash; a near-white accent still produces readable (ink) text; a dark
accent produces white text on the fill; zero-amount rows are dropped;
`company_lines` / `money` helpers.

**Server:** `_resolve_doc_layout` defaults to `modern`, honours the payload
field and the `SIETCH_DOC_LAYOUT` env var (payload wins), and ignores
unknown values; both classic and modern renderers still exist in
`server.py`.

**Handlers (mocked DB):** the project invoice returns `201` with the
expected response keys, the `project_documents` insert carries the
`opportunity_id`, and the PDF lands under `data/documents/<opp_id>/` with the
expected text; the project COC returns `201`, inserts with the
`opportunity_id`, and **honours the posted `completion_date`** (and falls
back to today when the payload omits it); the classic layout still renders
(and still uses the classic footer, not the branding footer); the standalone
invoice + COC return `201` and insert with `opportunity_id = NULL`.

**Deliberate-failure check:** the suite was verified to actually execute —
flipping an expected brand name and an expected `opportunity_id` each
produced a real `FAILED (failures=1)` / `FAILED (failures=2)` run, so a
green result is meaningful.

---

## 9. Sample files for visual review

Generated by `scripts/make_invoice_coc_review.py` into
`tmp/invoice-coc-review/` (**gitignored** — not committed). The script reads
the local `sietch-postgres` DB read-only (a read-only psycopg2 connection;
no migrations, no writes) and falls back to a seeded sample when the DB has
no `document_templates` / `opportunities` rows.

On the review machine the local `sietch_crm` DB had **zero**
`document_templates` rows and **zero** `opportunities`, so a throwaway
database (`sietch_inv_coc_review`, created and dropped for this exercise)
was seeded with one realistic `document_templates` row and three realistic
`opportunities` rows, and the review pack was generated from those real DB
rows.

### Files

| File | What it shows |
|---|---|
| `tmp/invoice-coc-review/project-101-1042-invoice-modern.pdf` | Modern invoice, Jordan Ellis / job 1042. |
| `tmp/invoice-coc-review/project-101-1042-invoice-modern.page1.png` | Page 1 as an image. |
| `tmp/invoice-coc-review/project-101-1042-invoice-classic.pdf` | The same invoice in the old fpdf2 layout, for side-by-side. |
| `tmp/invoice-coc-review/project-101-1042-invoice-classic.page1.png` | |
| `tmp/invoice-coc-review/project-101-1042-coc-modern.pdf` | Modern COC, Jordan Ellis. |
| `tmp/invoice-coc-review/project-101-1042-coc-modern.page1.png` | |
| `tmp/invoice-coc-review/project-101-1042-coc-classic.pdf` | The same COC in the old fpdf2 layout. |
| `tmp/invoice-coc-review/project-101-1042-coc-classic.page1.png` | |
| `tmp/invoice-coc-review/project-102-1057-invoice-modern.pdf` (+ `.page1.png`) | Second project (Hollis Family Trust, no base amount). |
| `tmp/invoice-coc-review/project-102-1057-coc-modern.pdf` (+ `.page1.png`) | |
| `tmp/invoice-coc-review/project-102-1057-invoice-classic.pdf` (+ `.page1.png`) | |
| `tmp/invoice-coc-review/project-102-1057-coc-classic.pdf` (+ `.page1.png`) | |
| `tmp/invoice-coc-review/project-103-1063-invoice-modern.pdf` (+ `.page1.png`) | Third project (Miguel Santos). |
| `tmp/invoice-coc-review/project-103-1063-coc-modern.pdf` (+ `.page1.png`) | |
| `tmp/invoice-coc-review/project-103-1063-invoice-classic.pdf` (+ `.page1.png`) | |
| `tmp/invoice-coc-review/project-103-1063-coc-classic.pdf` (+ `.page1.png`) | |
| `tmp/invoice-coc-review/with-extras-project-103-1063-invoice-modern.pdf` (+ `.page1.png`) | **Website + license + signer title filled in** — modern invoice. Shows `www.publicadjustermidwest.com`, `License OH LIC 104.8872` in the company block and "Licensed Public Adjuster" under the signature. |
| `tmp/invoice-coc-review/with-extras-project-103-1063-coc-modern.pdf` (+ `.page1.png`) | Same, modern COC. Shows "Ken Chapman, Licensed Public Adjuster" and a single signature block (no homeowner line). |
| `tmp/invoice-coc-review/with-extras-project-103-1063-invoice-classic.pdf` (+ `.page1.png`) | Same, classic invoice — website + license in the company block. |
| `tmp/invoice-coc-review/with-extras-project-103-1063-coc-classic.pdf` (+ `.page1.png`) | Same, classic COC — website + license in the company block. |
| `tmp/invoice-coc-review/zero-line-hidden-invoice-modern.pdf` (+ `.page1.png`) | **Two `$0.00` line items are not printed.** The total is unchanged ($6,670.00) and only the real rows appear. |
| `tmp/invoice-coc-review/zero-line-hidden-invoice-classic.pdf` (+ `.page1.png`) | The same for the classic renderer. |
| `tmp/invoice-coc-review/long-multipage-invoice-modern.pdf` (+ `.page1.png`) | 60 line items → **3 pages**, header row repeats, "Page 1 of 3" … "Page 3 of 3". |

Regenerate with:

```bash
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=<db> DB_USER=<user> DB_PASSWORD=<pw> \
  python3 scripts/make_invoice_coc_review.py
```

### Visual notes

* The modern invoice renders a full-bleed accent bar at the top, a
  two-column "BILL TO" / "PROJECT" card pair with tinted backgrounds, a
  numbered + zebra-striped items table with an accent header row, a
  right-aligned "Subtotal" / "Total due" block on the accent fill, a
  "NOTES" section and a signature block.
* The classic invoice's footer literally prints `Page 1 of (1)` — the
  modern one prints `Page 1 of 1`.
* The classic COC has **no** signature block and no accent bar; the modern
  COC has both plus the details table and the page footer.
* The website and the license appear under the company contact line in
  both layouts, and the license is prefixed with "License ".
* The `$0.00` rows are absent from both layouts, and the grand total is
  unchanged.

---

## 10. Deferred / not done

* **No running invoice-number counter** — deliberate (Sietch's behaviour is
  preserved; Sherwood's `branding.py` logic is not ported).
* **No `base_amount` UI field** — by decision (7); the server-side field
  exists and is honoured, but the modal does not send it (§5).
* **The website / licence / signer title are not on the photo-report
  templates** — `photo_report_templates` is a separate table.
* **The classic invoice footer still prints `Page 1 of (1)`** — fpdf2's
  `{{nb}}` alias is never substituted in the classic renderer.
* **Sherwood's admin UI** (`branding.py` branding editor, per-customer
  profiles, image re-encoding) — not ported. Sietch keeps its existing
  `document_templates` admin CRUD (extended with the three new fields).
* **Sherwood's Flask routes** — not ported (Sietch uses `http.server`).
* **The `layout` field is not yet exposed in the admin UI** — it is a
  per-request field in the Invoice / COC modals plus the `SIETCH_DOC_LAYOUT`
  env var.
* **Not deployed.** No merge, no push, no deploy, no container restart.
* **Sherwood's `fitz`/PyMuPDF dependency** was not ported — Sietch's tests
  use `pdftotext` (poppler) with a `pypdf` fallback.
