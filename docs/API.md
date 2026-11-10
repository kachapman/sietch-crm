# Sietch CRM API — quick start

Machine-readable contract: **`GET /api/v2/openapi.json`** (OpenAPI 3.1).
It is generated from `server.py`'s real dispatch code and pinned by
`tests/test_openapi.py`, so it cannot silently drift. Readable version below.

- Base URL: `https://crm.publicadjustermidwest.com`
- All request/response bodies are JSON (`Content-Type: application/json`).
- Timestamps are ISO-8601 **UTC** with an explicit offset (`…+00:00`).

---

## 1. Authentication

Create a token in **Admin → API & MCP**. It is shown once.

```bash
export SIETCH_TOKEN='sietch_…'          # paste the value shown once
curl -s https://crm.publicadjustermidwest.com/api/v2/projects?count=1 \
  -H "Authorization: Bearer $SIETCH_TOKEN"
```

- Tokens are **full access by default** (same surface as the admin who created
  them).
- Optional restrictions are enforced on every request: `scope=read` (GET only),
  method allowlist, resource allowlist, expiry.
- A browser session cookie (`sietch_session`) also works; scripts should use
  the Bearer token.
- Never commit a token. The API never returns a token value.

### Errors

```json
{"error": "API token does not allow POST"}
```

| status | meaning |
|---|---|
| 400 | invalid input |
| 401 | missing/invalid/expired/revoked credential |
| 403 | role or token restriction |
| 404 | not found |
| 500 | server error |

### Pagination

Most list endpoints take `count` + `startIndex` (limit/offset). Mail uses
`page` + `page_size` and returns `{messages, page, page_size, total}`.
The OpenAPI document marks each list endpoint with `x-pagination`.

---

## 2. Common workflows

All examples use synthetic data.

### Find a project

```bash
curl -s "…/api/v2/projects?search=roof&count=10" -H "Authorization: Bearer $SIETCH_TOKEN"
```

```json
[{"id": 101, "title": "ROOF — 123 Main St", "stage": {"title": "Estimate"},
  "bidValue": 18450.0, "contact": {"displayName": "Jane Doe", "company": "Doe Holdings"}}]
```

Then fetch one:

```bash
curl -s "…/api/v2/projects/101" -H "Authorization: Bearer $SIETCH_TOKEN"
```

### Add a note to a project

```bash
curl -s -X POST "…/api/v2/projects/101/history" \
  -H "Authorization: Bearer $SIETCH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"content": "Called the adjuster; scope approved.", "categoryId": 1}'
```

Add `"notifyUserList": [7]` to @-mention user 7 (they get a notification).
Get valid category ids from `GET /api/v2/history-categories`.

### Link an email to a job

```bash
curl -s -X POST "…/api/v2/mail/messages/8842/link" \
  -H "Authorization: Bearer $SIETCH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"opportunityId": 101}'
```

Returns `{"ok": true, "history_created": true, "copied": 2, "total": 2}` —
the timeline entry plus attachment copies into the project.

### Create a task

```bash
curl -s -X POST "…/api/v2/tasks" \
  -H "Authorization: Bearer $SIETCH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title": "Order shingles", "opportunityId": 101, "dueDate": "2026-11-12"}'
```

### Draft an email (never sends)

```bash
curl -s -X POST "…/api/v2/mail/drafts" \
  -H "Authorization: Bearer $SIETCH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"account_id": 6, "to": "jane@example.com", "subject": "Scope update",
       "body": "<p>Hi Jane — scope approved.</p>", "deal_id": 101}'
```

A human reviews and sends it in the Sietch UI. **There is no send endpoint in
the MCP surface**, and drafts are the only mail-write the tools expose.

### Read a document as text (scans included)

```bash
curl -s "…/api/v2/projects/101/documents/5531/text?page_start=1&page_end=5&max_chars=20000" \
  -H "Authorization: Bearer $SIETCH_TOKEN"
```

PDFs are extracted per page with `pdftotext -layout`. A page with fewer than
20 printable characters is marked `ocr_needed`; cached OCR text (from the
worker) is returned as `source: "ocr_cache"`. If the page still needs OCR the
document `status` is `ocr_pending` — read it again after the worker runs, or
ask for its status:

```bash
curl -s "…/api/v2/ocr/status?source=project_document&project_id=101&document_id=5531" \
  -H "Authorization: Bearer $SIETCH_TOKEN"
```

```
{"status": "pending", "pages_needed": [1, 2], "pages_cached": [],
 "file_sha256": "…", "queue": null}
```

Queue the file for a laptop worker (the only write; deduped by file hash):

```bash
curl -s -X POST "…/api/v2/ocr/request" \
  -H "Authorization: Bearer $SIETCH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"source": "project_document", "project_id": 101, "document_id": 5531}'
```

Mail attachments use the bytes already stored on the server
(`stored_path` only — never IMAP, never the network):

```bash
curl -s "…/api/v2/mail/messages/8842/attachments/17/text" \
  -H "Authorization: Bearer $SIETCH_TOKEN"
```

Images and other binary types return `{"status": "unsupported_type", …}` with
the filename and MIME type but no body. An attachment that was never mirrored
returns 409 `{"error": "not_stored"}`.

Download raw project document bytes as base64 (JSON only; 10 MB cap, refused
with `size_bytes` above it):

```bash
curl -s "…/api/v2/projects/101/documents/5531/content" \
  -H "Authorization: Bearer $SIETCH_TOKEN"
```

A raw `GET /api/v2/documents/{id}` also exists for the dashboard; the JSON
content route is what the MCP tool returns because MCP has no binary transport.

### Run the OCR worker

The server never runs OCR. A worker with a token restricted to the `ocr`
resource polls `GET /api/v2/ocr/queue?status=pending`, claims a job, downloads
it through `GET /api/v2/ocr/queue/{id}/file` (job-scoped — not a general
download), uploads page text to `POST /api/v2/ocr/queue/{id}/pages`, then
completes the job. Cache rows are written only by that pages endpoint. See
[`OCR_WORKER.md`](OCR_WORKER.md).

---

## 3. Endpoints used in these examples

| workflow | method + path |
|---|---|
| find a project | `GET /api/v2/projects?search=` |
| get one project | `GET /api/v2/projects/{id}` |
| add a note | `POST /api/v2/projects/{id}/history` |
| link an email | `POST /api/v2/mail/messages/{id}/link` |
| create a task | `POST /api/v2/tasks` |
| draft an email | `POST /api/v2/mail/drafts` |
| list documents | `GET /api/v2/projects/{id}/documents` |
| read document text | `GET /api/v2/projects/{id}/documents/{document_id}/text` |
| download document (base64) | `GET /api/v2/projects/{id}/documents/{document_id}/content` |
| read attachment text | `GET /api/v2/mail/messages/{id}/attachments/{attachment_id}/text` |
| OCR status | `GET /api/v2/ocr/status` |
| queue OCR | `POST /api/v2/ocr/request` |
| API contract | `GET /api/v2/openapi.json` |

List endpoints advertise their pagination in the spec under `x-pagination`
(e.g. `count` + `startIndex`, or mail's `page` + `page_size`).

## 4. What is documented

Every route the server dispatches under `/api/v2` is in the spec: 326
operations across 253 paths (projects, contacts, history, tasks, notes, mail,
documents, OCR, calendar, users, admin, meta). The endpoints used by the MCP
tools carry full query/body/response schemas; the rest are listed with method,
path, auth, path parameters and the standard error set.

Regenerate after adding a route:

```bash
python3 scripts/gen_api_spec.py --write   # rewrites api_spec_routes.py
python3 tests/test_openapi.py             # fails if the dispatcher and spec disagree
```

## 5. MCP

The MCP server (`/mcp`) wraps the same endpoints; tool names and descriptions
are derived from this spec (`api_spec.MCP_TOOL_OPERATIONS`). See
`plans/sietch/sietch-mcp-server.md`.

Document-read tools: `get_project_document`, `download_project_document`,
`get_mail_attachment_text`, `ocr_status`, `request_ocr`. They are read-only
except `request_ocr`, which is immediate (not a proposal) and only inserts one
OCR queue row; a fully cached file is never re-queued. The worker token scope
is `ocr` only, so it cannot read documents, mail or admin routes.

Admin → **API & MCP** is one tab: the MCP on/off switch, connection URL and
copyable client config at the top, then the token list (each row shows the
"posts as" user, its restrictions and its optional MCP tool allowlist), then
the pending proposals and the MCP audit log.
