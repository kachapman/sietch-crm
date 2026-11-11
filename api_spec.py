"""OpenAPI 3.1 description of the Sietch CRM `/api/v2` surface.

Single source of truth for the REST API documentation AND the MCP tool
descriptions (see `MCP_TOOL_OPERATIONS`). The route list is generated from
`server.py`'s dispatch code (`scripts/gen_api_spec.py`) and pinned by
`tests/test_openapi.py`, which fails when a route exists in the dispatcher but
not in the spec.

Served at `GET /api/v2/openapi.json`.
"""

from __future__ import annotations

import re
from typing import Any

from api_spec_routes import ROUTES

OPENAPI_VERSION = "3.1.0"
API_TITLE = "Sietch CRM API"
API_DESCRIPTION = """\
Sietch CRM `/api/v2` REST API.

**Auth** — send `Authorization: Bearer <token>` with a token from
Admin → API Tokens, or use the browser session cookie. Tokens are full-access
by default; optional per-token restrictions (read-only, methods, resources,
expiry) are enforced on every request. `GET /api/v2/openapi.json` requires a
credential too.

**Errors** — non-2xx responses are `{"error": "<message>"}` with 400 (bad
input), 401 (missing/invalid credential), 403 (permission or token
restriction), 404 (not found), 500 (server error).

**Pagination** — list endpoints accept `count` and `startIndex` (limit/offset)
unless documented otherwise; some return `{page, page_size, total}`.

A readable quick start lives in `docs/API.md`.
"""

SERVER_URL = "https://crm.publicadjustermidwest.com"

TAGS = [
    {"name": "projects", "description": "Projects/jobs (opportunities), stages, tags, custom fields."},
    {"name": "contacts", "description": "Contacts and contact history."},
    {"name": "history", "description": "Project timeline events (notes, embedded emails, comments)."},
    {"name": "tasks", "description": "Tasks and task labels."},
    {"name": "notes", "description": "Personal notes."},
    {"name": "mail", "description": "Synced mail, threads, drafts, accounts, tags."},
    {"name": "documents", "description": "Documents and attachments metadata."},
    {"name": "ocr", "description": "Document text extraction, OCR cache and the host-agnostic OCR worker queue."},
    {"name": "calendar", "description": "Calendar sources and events."},
    {"name": "users", "description": "Users, avatars, sessions."},
    {"name": "me", "description": "The authenticated user's own settings."},
    {"name": "admin", "description": "Admin console endpoints (admin session required)."},
    {"name": "meta", "description": "API metadata (this document)."},
]

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

_S = lambda t, **kw: {"type": t, **kw}  # noqa: E731
_STR = _S("string")
_INT = _S("integer")
_NUM = _S("number")
_BOOL = _S("boolean")


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def _obj(props: dict, required: list[str] | None = None, desc: str | None = None) -> dict:
    out: dict = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    if desc:
        out["description"] = desc
    return out


ERROR_SCHEMA = _obj({"error": _STR}, ["error"], "Standard error envelope.")

TAG = _obj({
    "id": _INT, "title": _STR, "color": _STR, "isSystem": _BOOL,
}, ["id", "title"], "A tag definition as assigned to a project.")

PROJECT = _obj({
    "id": _INT,
    "title": _STR,
    "description": _STR,
    "stageId": _INT,
    "stageType": _INT,
    "bidValue": {"type": ["number", "null"]},
    "expectedCloseDate": {"type": ["string", "null"], "format": "date"},
    "probability": _INT,
    "contactId": _INT,
    "responsibleUserId": _INT,
    "isPrivate": _BOOL,
    "created": {"type": "string", "format": "date-time"},
    "stage": _obj({"title": _STR, "color": _STR}),
    "contact": _obj({
        "displayName": _STR, "company": _STR, "email": _STR, "phone": _STR,
        "addressStreet": _STR, "addressCity": _STR, "addressState": _STR,
        "addressZip": _STR,
    }),
    # GET /api/v2/projects/{id} returns more than the list route.
    "createdBy": _INT,
    "updatedAt": {"type": "string", "format": "date-time"},
    "responsible": _obj({"displayName": _STR}),
    "project_number": _STR,
    # Present on GET /api/v2/projects/{id} only (one extra query per project).
    "tags": _arr(_ref("Tag")),
}, ["id", "title"])

CONTACT = _obj({
    "id": _INT, "firstName": _STR, "lastName": _STR, "company": _STR,
    "displayName": _STR, "email": _STR, "phone": _STR, "mobilePhone": _STR,
    "jobTitle": _STR, "contactType": _STR, "isCompany": _BOOL,
}, ["id"])

TASK = _obj({
    "id": _INT, "title": _STR, "description": _STR, "opportunityId": _INT,
    "responsibleUserId": _INT, "dueDate": {"type": ["string", "null"], "format": "date-time"},
    "priority": _INT, "categoryId": _INT, "completed": _BOOL,
}, ["id", "title"])

NOTE = _obj({
    "id": {"type": "string"}, "title": _STR, "body": _STR,
    "color": _STR, "archived": _BOOL,
    "createdAt": {"type": "string", "format": "date-time"},
    "updatedAt": {"type": "string", "format": "date-time"},
})

HISTORY_EVENT = _obj({
    "id": _INT, "opportunityId": _INT, "categoryId": _INT,
    "category": _obj({"id": _INT, "title": _STR}),
    "content": _STR, "author": _STR,
    "created": {"type": "string", "format": "date-time"},
    "attachments": _arr(_ref("Document")),
}, ["id"])

MAIL_ATTACHMENT = _obj({
    "id": _INT,
    "message_id": _INT,
    "filename": _STR,
    "mime_type": _STR,
    "size_bytes": _INT,
    # True when the bytes are on disk (mail_attachments.stored_path); the
    # text/OCR tools can only read stored bytes, never IMAP.
    "stored": _BOOL,
}, ["id", "filename"])

# GET /api/v2/mail/messages/{id} returns the mail_messages row itself
# (snake_case column names, including body_text/body_html/raw_headers) plus
# the derived tags / linked_deals / attachments / sync_failure keys. The
# list route returns the same row shape with linked_deals using
# deal_id/title and attachment_count instead of attachments.
MAIL_MESSAGE = _obj({
    "id": _INT,
    "account_id": _INT,
    "imap_uid": _STR,
    "message_id": _STR,
    "in_reply_to": _STR,
    "from_addr": _STR,
    "to_addr": _STR,
    "cc_addr": _STR,
    "subject": _STR,
    "body_text": _STR,
    "body_html": _STR,
    "raw_headers": _STR,
    "date_received": {"type": ["string", "null"], "format": "date-time"},
    "is_read": _BOOL,
    "is_flagged": _BOOL,
    "is_archived": _BOOL,
    "starred": _BOOL,
    "folder": _STR,
    "conversation_id": _STR,
    "created_at": {"type": "string", "format": "date-time"},
    "account_email": _STR,
    "attachments_json": {"type": ["array", "object", "null"]},
    "tags": _arr(_obj({"id": _INT, "title": _STR, "color": _STR})),
    "linked_deals": _arr(_obj({
        "opportunity_id": _INT, "opportunity_title": _STR,
        "deal_id": _INT, "title": _STR,
    })),
    "attachments": _arr(_ref("MailAttachment")),
    "attachment_count": _INT,
    "sync_failure": {"type": ["string", "null"]},
}, ["id"],
    "The mail_messages row (snake_case) plus tags, linked_deals and "
    "attachments metadata. Attachment bytes and mail credentials are never "
    "returned here.")

DOCUMENT = _obj({
    "id": _INT,
    "title": _STR,
    "filePath": _STR,
    "fileSize": _INT,
    "mimeType": _STR,
    "uploadedBy": _INT,
    "uploadedAt": {"type": "string", "format": "date-time"},
    "opportunityId": _INT,
    "companyScope": _BOOL,
    "folderId": _INT,
    "editUrl": _STR,
    # History attachments surfaced by GET /projects/{id}/documents.
    "filename": _STR,
    "crmFileId": _INT,
    "projectDocumentsId": _INT,
    "url": _STR,
    # True for a history_attachments row listed by GET /projects/{id}/documents.
    "fromHistoryAttachment": _BOOL,
}, ["id"], "A document row (project, personal, company, or history attachment).")

DOCUMENT_TEXT_PAGE = _obj({
    "page": _INT,
    "text": _STR,
    "source": _STR,
    "char_count": _INT,
    "ocr_needed": _BOOL,
    "ocr_status": {"type": ["string", "null"]},
}, ["page", "text", "source", "char_count", "ocr_needed"])

DOCUMENT_TEXT = _obj({
    "status": {"type": "string", "enum": ["ok", "ocr_pending", "unsupported_type"]},
    "error": _STR,
    "filename": _STR,
    "mime_type": _STR,
    "size_bytes": _INT,
    "file_sha256": _STR,
    "page_count": _INT,
    "page_start": {"type": ["integer", "null"]},
    "page_end": {"type": ["integer", "null"]},
    "truncated": _BOOL,
    "text": _STR,
    "pages": _arr(_ref("DocumentTextPage")),
}, ["status", "filename", "page_count", "text", "pages"])

OCR_JOB = _obj({
    "id": _INT,
    "source": _STR,
    "source_id": _INT,
    "project_id": {"type": ["integer", "null"]},
    "message_id": {"type": ["integer", "null"]},
    "file_sha256": _STR,
    "status": _STR,
    "error": {"type": ["string", "null"]},
    "requested_at": {"type": ["string", "null"], "format": "date-time"},
    "started_at": {"type": ["string", "null"], "format": "date-time"},
    "finished_at": {"type": ["string", "null"], "format": "date-time"},
}, ["id", "source", "source_id", "file_sha256", "status"])

OCR_STATUS = _obj({
    "source": _STR,
    "source_id": _INT,
    "project_id": {"type": ["integer", "null"]},
    "message_id": {"type": ["integer", "null"]},
    "filename": _STR,
    "mime_type": _STR,
    "file_sha256": _STR,
    "page_count": _INT,
    "pages_needed": _arr(_INT),
    "pages_cached": _arr(_INT),
    "pages_missing": _arr(_INT),
    "status": _STR,
    "queue": {"anyOf": [_ref("OcrJob"), {"type": "null"}]},
    "engine": {"type": ["string", "null"]},
    "engine_version": {"type": ["string", "null"]},
}, ["source", "status", "file_sha256"])

OCR_TARGET_PROJECT = _obj({
    "source": {"type": "string", "enum": ["project_document"]},
    "project_id": _INT,
    "document_id": _INT,
}, ["source", "project_id", "document_id"],
    "A project document, identified by project_id + document_id.")

OCR_TARGET_MAIL = _obj({
    "source": {"type": "string", "enum": ["mail_attachment"]},
    "message_id": _INT,
    "attachment_id": _INT,
}, ["source", "message_id", "attachment_id"],
    "A mail attachment, identified by message_id + attachment_id.")

# The ids are required per source: a body/query with only `source` is invalid.
OCR_TARGET = {"oneOf": [_ref("OcrTargetProject"), _ref("OcrTargetMail")],
              "description": ("source=project_document requires project_id + "
                              "document_id; source=mail_attachment requires "
                              "message_id + attachment_id.")}

USER = _obj({
    "id": _INT, "email": _STR, "displayName": _STR,
    "firstName": _STR, "lastName": _STR, "isAdmin": _BOOL,
    "isActive": _BOOL, "role": _STR,
    "avatarUrl": {"type": ["string", "null"]},
    "thumbnailUrl": {"type": ["string", "null"]},
    "avatarIcon": {"type": ["string", "null"]},
    "twofaMethod": {"type": ["string", "null"]},
    "createdAt": {"type": ["string", "null"], "format": "date-time"},
    "lastLogin": {"type": ["string", "null"], "format": "date-time"},
    "mustChangePassword": _BOOL,
}, ["id", "email"])

STAGE = _obj({
    "id": _INT, "title": _STR, "color": _STR, "sortOrder": _INT,
    "stageType": _INT, "probability": _INT, "isActive": _BOOL,
    "projectCount": _INT,
}, ["id", "title"])

HISTORY_REPLY = _obj({
    "id": _INT, "parentReplyId": _INT, "content": _STR,
    "createdBy": _INT, "created": {"type": "string", "format": "date-time"},
    "isDeleted": _BOOL, "author": _STR,
    "notifyUserIds": _arr(_INT),
}, ["id"])

# Only non-secret columns. Passwords / OAuth tokens / encrypted blobs are
# blanked by the route and stripped again by the MCP tool.
MAIL_ACCOUNT = _obj({
    "id": _INT, "email": _STR, "display_name": _STR,
    "imap_host": _STR, "imap_port": _INT,
    "smtp_host": _STR, "smtp_port": _INT, "smtp_user": _STR,
    "smtp_from_name": _STR, "smtp_auth_disabled": _BOOL,
    "sync_enabled": _BOOL, "monitored_folders": {"type": ["array", "string", "null"]},
    "oauth_provider": {"type": ["string", "null"]},
    "oauthStatus": {"type": ["string", "null"]},
    "authType": _STR, "authError": _STR,
    "identityStatus": _STR, "identityError": _STR,
    "is_crm_mail": _BOOL, "owner_user_id": _INT,
    "isOwner": _BOOL, "canManage": _BOOL,
    "retention_days": _INT, "last_sync": {"type": ["string", "null"]},
}, ["id", "email"],
    "A mail account without credentials (never a password or OAuth token).")

PAGINATION = _obj({
    "page": _INT, "pageSize": _INT, "total": _INT,
}, desc="Limit/offset pagination metadata.")

COMPONENTS = {
    "schemas": {
        "Error": ERROR_SCHEMA,
        "Tag": TAG,
        "Project": PROJECT,
        "Contact": CONTACT,
        "Task": TASK,
        "Note": NOTE,
        "HistoryEvent": HISTORY_EVENT,
        "HistoryReply": HISTORY_REPLY,
        "MailMessage": MAIL_MESSAGE,
        "MailAttachment": MAIL_ATTACHMENT,
        "MailAccount": MAIL_ACCOUNT,
        "Document": DOCUMENT,
        "DocumentText": DOCUMENT_TEXT,
        "DocumentTextPage": DOCUMENT_TEXT_PAGE,
        "OcrJob": OCR_JOB,
        "OcrStatus": OCR_STATUS,
        "OcrTargetProject": OCR_TARGET_PROJECT,
        "OcrTargetMail": OCR_TARGET_MAIL,
        "User": USER,
        "Stage": STAGE,
        "Pagination": PAGINATION,
    },
    "securitySchemes": {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "Sietch API token from Admin → API Tokens (`sietch_…`). "
                "Optional restrictions (read-only / methods / resources / expiry) "
                "are enforced per request."
            ),
        },
        "sessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "sietch_session",
            "description": "Browser session cookie (the dashboard's own login).",
        },
    },
    "responses": {
        "BadRequest": {"description": "Invalid input.", "content": {"application/json": {"schema": _ref("Error")}}},
        "Unauthorized": {"description": "Missing or invalid credential.", "content": {"application/json": {"schema": _ref("Error")}}},
        "Forbidden": {"description": "Not permitted (role or token restriction).", "content": {"application/json": {"schema": _ref("Error")}}},
        "NotFound": {"description": "Resource not found.", "content": {"application/json": {"schema": _ref("Error")}}},
        "ServerError": {"description": "Unexpected server error.", "content": {"application/json": {"schema": _ref("Error")}}},
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment for the endpoints that matter most (and that MCP wraps)
# ─────────────────────────────────────────────────────────────────────────────

def _q(name: str, desc: str, type_: str = "string", required: bool = False) -> dict:
    return {"name": name, "in": "query", "required": required,
            "description": desc, "schema": {"type": type_}}


def _path_param(name: str, desc: str, type_: str = "integer") -> dict:
    return {"name": name, "in": "path", "required": True,
            "description": desc, "schema": {"type": type_}}


def _json_body(schema: dict, desc: str, required: bool = True) -> dict:
    return {
        "required": required,
        "description": desc,
        "content": {"application/json": {"schema": schema}},
    }


def _json_response(desc: str, schema: dict | None = None) -> dict:
    return {
        "description": desc,
        "content": {"application/json": {"schema": schema or {"type": "object"}}},
    }


_ENRICH: dict[tuple[str, str], dict] = {
    ("GET", "/api/v2/openapi.json"): {
        "summary": "OpenAPI 3.1 description of this API",
        "description": "The machine-readable contract for every /api/v2 route. Requires a credential.",
        "tags": ["meta"],
        "responses": {"200": _json_response("OpenAPI document", {"type": "object"})},
    },
    # ── projects ───────────────────────────────────────────────────────────
    ("GET", "/api/v2/projects"): {
        "summary": "Search/list projects (jobs)",
        "description": (
            "Limit/offset pagination: `count` (alias `limit`) is the page size "
            "(default 500, no hard maximum) and `startIndex` the offset, so "
            "`startIndex=500` pages past the default page. The 200 response is "
            "a bare JSON array; the total number of matching projects is in "
            "the `X-Total-Count` response header. `search` matches title, "
            "description and contact. `tag_id` filters to projects carrying a "
            "tag; comma-separate ids (`tag_id=3,9`) to OR-match tags."
        ),
        "tags": ["projects"],
        "parameters": [
            _q("search", "Free-text search (title/description/contact)."),
            _q("count", "Page size (default 500; no hard maximum).", "integer"),
            _q("limit", "Alias for count.", "integer"),
            _q("startIndex", "Offset (default 0).", "integer"),
            _q("stage_type", "0=open, 1=won, 2=lost.", "integer"),
            _q("include_closed", "Include won/lost projects.", "boolean"),
            _q("tag_id", "Only projects with this tag id (comma-separated ids OR-match).", "string"),
            _q("sort_by", "date_created | title | bid_value | stage."),
            _q("sort_order", "ascending | descending."),
        ],
        "responses": {
            "200": {
                "description": "Matching projects (total in X-Total-Count).",
                "headers": {
                    "X-Total-Count": {
                        "description": "Number of projects matching the filters across all pages.",
                        "schema": {"type": "integer"},
                    },
                },
                "content": {"application/json": {
                    "schema": _arr(_ref("Project"))}},
            },
        },
        "x-pagination": {"style": "limit-offset", "params": ["count", "limit", "startIndex"]},
    },
    ("POST", "/api/v2/projects"): {
        "summary": "Create a project (job)",
        "tags": ["projects"],
        "requestBody": _json_body(_obj({
            "title": _STR, "description": _STR, "stageId": _INT,
            "stageType": _INT, "bidValue": _NUM, "contactId": _INT,
            "responsibleUserId": _INT, "expectedCloseDate": _STR,
            "isPrivate": _BOOL,
        }, ["title"]), "Project fields. `title` is required."),
        "responses": {"201": _json_response("Created", _obj({"id": _INT, "ok": _BOOL}))},
    },
    ("GET", "/api/v2/projects/{id}"): {
        "summary": "Get one project",
        "description": ("A single project plus its assigned `tags` (id, title, "
                        "color, isSystem) and the contact/responsible detail."),
        "tags": ["projects"],
        "parameters": [_path_param("id", "Project id (opportunities.id).")],
        "responses": {"200": _json_response("Project", _ref("Project"))},
    },
    ("PUT", "/api/v2/projects/{id}"): {
        "summary": "Update project fields",
        "tags": ["projects"],
        "parameters": [_path_param("id", "Project id.")],
        "requestBody": _json_body(_obj({
            "title": _STR, "description": _STR, "stageId": _INT,
            "bidValue": _NUM, "contactId": _INT, "responsibleUserId": _INT,
            "isPrivate": _BOOL, "expectedCloseDate": _STR,
            "probability": _INT, "stageType": _INT,
        }), "Any subset of updatable fields."),
        "responses": {"200": _json_response("Updated")},
    },
    ("GET", "/api/v2/projects/{id}/history"): {
        "summary": "List a project's timeline (notes + embedded emails)",
        "description": (
            "Newest first. `count` is the page size (default 10) and "
            "`startIndex` the offset; the response carries `total` so a client "
            "can page through a long timeline."
        ),
        "tags": ["history"],
        "parameters": [_path_param("id", "Project id."),
                       _q("count", "Page size (default 10).", "integer"),
                       _q("startIndex", "Offset (default 0).", "integer")],
        "responses": {"200": _json_response("Timeline page", _obj({
            "events": _arr(_ref("HistoryEvent")),
            "total": _INT,
            "count": _INT,
            "startIndex": _INT,
        }, ["events", "total"]))},
        "x-pagination": {"style": "limit-offset", "params": ["count", "startIndex"]},
    },
    ("POST", "/api/v2/projects/{id}/history"): {
        "summary": "Add a note/comment to a project",
        "description": "`notifyUserList` @-mentions users (they get a notification).",
        "tags": ["history"],
        "parameters": [_path_param("id", "Project id.")],
        "requestBody": _json_body(_obj({
            "content": _STR,
            "categoryId": _INT,
            "notifyUserList": _arr(_INT),
            "created": _STR,
        }, ["content"]), "Note content (required) and optional category/mentions."),
        "responses": {"201": _json_response("Created", _obj({"id": _INT}))},
    },
    ("POST", "/api/v2/projects/{id}/tags"): {
        "summary": "Add a tag to a project",
        "tags": ["projects"],
        "parameters": [_path_param("id", "Project id.")],
        "requestBody": _json_body(_obj({"tagId": _INT, "title": _STR}),
                                  "Either `tagId` or `title` (created if missing)."),
        "responses": {"200": _json_response("Tagged")},
    },
    ("GET", "/api/v2/projects/{id}/tags"): {
        "summary": "List the tags assigned to a project",
        "description": ("The live array of tag definitions attached to this "
                        "project (id, title, color, isSystem)."),
        "tags": ["projects"],
        "parameters": [_path_param("id", "Project id.")],
        "responses": {"200": _json_response("Tags", _arr(_ref("Tag")))},
    },
    ("DELETE", "/api/v2/projects/{id}/tags/{tag_id}"): {
        "summary": "Remove a tag from a project",
        "description": (
            "Deletes only the project↔tag link (`opportunity_tags` row). The "
            "tag definition and its use on other projects are untouched, so "
            "there is no delete-by-title form: pass the tag id."
        ),
        "tags": ["projects"],
        "parameters": [_path_param("id", "Project id."),
                       _path_param("tag_id", "tag_definitions.id attached to the project.")],
        "responses": {"200": _json_response("Untagged", _obj({"ok": _BOOL}))},
    },
    # ── contacts ───────────────────────────────────────────────────────────
    ("GET", "/api/v2/contacts"): {
        "summary": "Search/list contacts",
        "tags": ["contacts"],
        "parameters": [_q("q", "Free-text search."),
                       _q("count", "Max results (default 500).", "integer"),
                       _q("startIndex", "Offset.", "integer")],
        "responses": {"200": _json_response("Contacts", _arr(_ref("Contact")))},
        "x-pagination": {"style": "limit-offset", "params": ["count", "startIndex"]},
    },
    ("POST", "/api/v2/contacts"): {
        "summary": "Create a contact",
        "tags": ["contacts"],
        "requestBody": _json_body(_obj({
            "firstName": _STR, "lastName": _STR, "company": _STR,
            "email": _STR, "phone": _STR, "mobilePhone": _STR,
            "jobTitle": _STR, "isCompany": _BOOL,
        }), "Contact fields."),
        "responses": {"201": _json_response("Created", _obj({"id": _INT}))},
    },
    ("GET", "/api/v2/contacts/{id}"): {
        "summary": "Get one contact",
        "tags": ["contacts"],
        "parameters": [_path_param("id", "Contact id.")],
        "responses": {"200": _json_response("Contact", _ref("Contact"))},
    },
    # ── tasks ──────────────────────────────────────────────────────────────
    ("GET", "/api/v2/tasks"): {
        "summary": "List tasks",
        "tags": ["tasks"],
        "responses": {"200": _json_response("Tasks", _arr(_ref("Task")))},
    },
    ("POST", "/api/v2/tasks"): {
        "summary": "Create a task",
        "tags": ["tasks"],
        "requestBody": _json_body(_obj({
            "title": _STR, "description": _STR, "opportunityId": _INT,
            "responsibleUserId": _INT, "dueDate": _STR, "priority": _INT,
            "categoryId": _INT, "labels": _STR, "repeat": _STR,
        }, ["title"]), "Task fields. `title` is required."),
        "responses": {"201": _json_response("Created", _obj({"id": _INT, "ok": _BOOL}))},
    },
    # ── notes ──────────────────────────────────────────────────────────────
    ("GET", "/api/v2/notes"): {
        "summary": "List personal notes",
        "tags": ["notes"],
        "parameters": [_q("archived", "Include archived notes.", "boolean")],
        "responses": {"200": _json_response("Notes", _arr(_ref("Note")))},
    },
    ("POST", "/api/v2/notes"): {
        "summary": "Create a personal note",
        "tags": ["notes"],
        "requestBody": _json_body(_obj({"title": _STR, "body": _STR, "color": _STR}),
                                  "Note fields."),
        "responses": {"201": _json_response("Created", _obj({"id": _STR}))},
    },
    # ── mail ───────────────────────────────────────────────────────────────
    ("GET", "/api/v2/mail/messages"): {
        "summary": "Search/list synced mail",
        "description": "Limit/offset pagination; returns `{messages, page, page_size, total}`.",
        "tags": ["mail"],
        "parameters": [
            _q("search", "Match subject or sender."),
            _q("folder", "Mail folder (default INBOX)."),
            _q("account_id", "Restrict to one mail account id.", "integer"),
            _q("page", "1-based page.", "integer"),
            _q("page_size", "Page size (default 50).", "integer"),
            _q("tag", "Filter by mail tag title."),
        ],
        "responses": {"200": _json_response("Messages", _obj({
            "messages": _arr(_ref("MailMessage")), "page": _INT,
            "page_size": _INT, "total": _INT,
        }))},
        "x-pagination": {"style": "page", "params": ["page", "page_size"]},
    },
    ("GET", "/api/v2/mail/messages/{id}"): {
        "summary": "Get one email (body, attachments metadata, linked deals)",
        "description": (
            "The full `mail_messages` row (snake_case columns) including "
            "`body_text` / `body_html` / `raw_headers`, plus `tags`, "
            "`linked_deals`, `attachments` metadata (id, filename, mime type, "
            "size, `stored`) and `sync_failure`. Attachment bytes are served "
            "by the download route; credentials are never returned."
        ),
        "tags": ["mail"],
        "parameters": [_path_param("id", "mail_messages.id.")],
        "responses": {"200": _json_response("Message", _ref("MailMessage"))},
    },
    ("GET", "/api/v2/mail/messages/{id}/attachments"): {
        "summary": "List a message's attachments metadata",
        "description": ("Attachment ids, filenames, MIME types, sizes and "
                        "whether the bytes are stored on the server. No bytes, "
                        "no IMAP credentials."),
        "tags": ["mail"],
        "parameters": [_path_param("id", "mail_messages.id.")],
        "responses": {"200": _json_response("Attachments", _obj({
            "attachments": _arr(_ref("MailAttachment")),
        }, ["attachments"]))},
    },
    ("POST", "/api/v2/mail/messages/{id}/link"): {
        "summary": "Link an email to a project (creates the timeline entry)",
        "tags": ["mail", "history"],
        "parameters": [_path_param("id", "mail_messages.id.")],
        "requestBody": _json_body(_obj({"opportunityId": _INT}, ["opportunityId"]),
                                  "Target project id."),
        "responses": {"200": _json_response("Linked", _obj({
            "ok": _BOOL, "history_created": _BOOL, "copied": _INT, "total": _INT,
        }))},
    },
    ("GET", "/api/v2/mail/threads"): {
        "summary": "List mail conversation threads",
        "tags": ["mail"],
        "parameters": [_q("folder", "Optional folder filter.")],
        "responses": {"200": _json_response("Threads", _arr({"type": "object"}))},
    },
    ("POST", "/api/v2/mail/send/preflight"): {
        "summary": "Check whether a mailbox may send for a job/thread (no side effects)",
        "description": "Returns 409 when the job's Contact is mapped to another mailbox, or when a reply would leave from a different account than the original message.",
        "tags": ["mail"],
        "requestBody": _json_body(_obj({
            "account_id": _INT, "deal_id": _INT, "reply_to_message_id": _INT,
        }, ["account_id"]), "Account to test, plus the job/thread context."),
        "responses": {"200": _json_response("Allowed", _obj({
            "ok": _BOOL, "accountEmail": _STR, "contactName": _STR,
            "mappedEmails": _arr(_STR),
        }))},
    },
    ("POST", "/api/v2/mail/drafts"): {
        "summary": "Create an email DRAFT (never sends)",
        "description": "Drafts are reviewed and sent by a human in the Sietch UI. There is no send endpoint in the MCP surface.",
        "tags": ["mail"],
        "requestBody": _json_body(_obj({
            "account_id": _INT, "to": _STR, "cc": _STR, "bcc": _STR,
            "subject": _STR, "body": _STR, "deal_id": _INT,
        }, ["to", "subject", "body"]), "Draft fields."),
        "responses": {"201": _json_response("Draft created", _obj({"id": _INT}))},
    },
    # ── documents ──────────────────────────────────────────────────────────
    ("GET", "/api/v2/projects/{id}/documents"): {
        "summary": "List a project's documents/attachments metadata",
        "description": ("project_documents rows plus history_attachments rows "
                        "that have no project mirror (those carry "
                        "`fromHistoryAttachment: true` and their ids also work "
                        "with the document text/OCR route)."),
        "tags": ["documents"],
        "parameters": [_path_param("id", "Project id.")],
        "responses": {"200": _json_response("Documents", _obj({
            "documents": _arr(_ref("Document")),
        }, ["documents"]))},
    },
    ("GET", "/api/v2/documents/{id}"): {
        "summary": "Download a document's raw bytes",
        "description": (
            "Raw file bytes (not JSON). Auth: session cookie, a read-only API "
            "token, or the per-document OnlyOffice JWT. The response "
            "Content-Type is the file's own MIME type "
            "(`application/octet-stream` when unknown). For text extraction use "
            "GET /api/v2/projects/{id}/documents/{document_id}/text instead."
        ),
        "tags": ["documents"],
        "parameters": [_path_param("id", "project_documents.id.")],
        "responses": {"200": {
            "description": ("The file's raw bytes; Content-Type is the stored "
                            "MIME type."),
            "content": {"application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}}},
        }},
    },
    ("PATCH", "/api/v2/documents/{id}"): {
        "summary": "Update a document (rename, move, change scope)",
        "description": (
            "The document owner or an admin. `opportunity_id` moves the file "
            "into a project (and clears company_scope/folder); `scope` "
            "`personal`|`company` moves it out of a project; `folder_id` "
            "applies only when neither is sent. PUT is accepted as an alias."
        ),
        "tags": ["documents"],
        "parameters": [_path_param("id", "project_documents.id.")],
        "requestBody": _json_body(_obj({
            "title": _STR,
            "opportunity_id": _INT,
            "folder_id": _INT,
            "scope": {"type": "string", "enum": ["personal", "company"]},
        }), "Any subset; at least one field is required.", required=True),
        "responses": {"200": _json_response("Updated", _obj({"ok": _BOOL}))},
    },
    ("GET", "/api/v2/history-attachments/{id}/download"): {
        "summary": "Download a history-attachment file's raw bytes",
        "description": (
            "Raw file bytes (not JSON) from the project's own copy under "
            "data/history-attachments/. Content-Type is the stored MIME type."
        ),
        "tags": ["documents", "history"],
        "parameters": [_path_param("id", "history_attachments.id.")],
        "responses": {"200": {
            "description": "The file's raw bytes",
            "content": {"application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}}},
        }},
    },
    ("GET", "/api/v2/documents/company"): {
        "summary": "List company-scoped documents in a folder",
        "description": ("Folders plus `{documents, folders, total, limit, "
                        "offset}`. Document ids are project_documents ids — the "
                        "same id space as GET /api/v2/documents/{id} and the "
                        "project document text/OCR route (which additionally "
                        "needs the owning project id when there is one)."),
        "tags": ["documents"],
        "parameters": [
            _q("folder_id", "Folder id (omit for the root).", "integer"),
            _q("limit", "Page size (default 200, max 500).", "integer"),
            _q("offset", "Offset (default 0).", "integer"),
        ],
        "responses": {"200": _json_response("Company documents", _obj({
            "documents": _arr(_ref("Document")),
            "folders": _arr(_obj({"id": _INT, "name": _STR})),
            "total": _INT, "limit": _INT, "offset": _INT,
        }, ["documents", "folders", "total"]))},
        "x-pagination": {"style": "limit-offset", "params": ["limit", "offset"]},
    },
    # ── document text + OCR ────────────────────────────────────────────────
    ("GET", "/api/v2/projects/{id}/documents/{document_id}/text"): {
        "summary": "Extract text from a project document (pdftotext + cached OCR)",
        "description": (
            "Read-only. PDF pages are extracted with `pdftotext -layout`. A page "
            "with fewer than 20 printable characters is marked `ocr_needed`: "
            "cached OCR text is returned when present, otherwise the document "
            "status is `ocr_pending` until an OCR worker fills the cache. "
            "Images and other binary types return `status: unsupported_type` "
            "with filename/mime and no body. Never runs OCR on the server."
        ),
        "tags": ["documents", "ocr"],
        "parameters": [
            _path_param("id", "Project id."),
            _path_param("document_id", "project_documents.id or a history_attachments.id shown in the Documents tab."),
            _q("page_start", "First page (1-based, inclusive).", "integer"),
            _q("page_end", "Last page (inclusive).", "integer"),
            _q("max_chars", "Maximum characters returned (default 100000).", "integer"),
        ],
        "responses": {
            "200": _json_response("Extracted text", _ref("DocumentText")),
            "409": {
                "description": ("`not_stored` — the file's bytes are not on the "
                                "server. A storage/extraction state (scanned "
                                "pages, unsupported type) is a 200 with "
                                "`status` = ocr_pending | unsupported_type, not "
                                "an error."),
                "content": {"application/json": {"schema": _ref("Error")}},
            },
        },
    },
    ("GET", "/api/v2/mail/messages/{id}/attachments/{attachment_id}/text"): {
        "summary": "Extract text from a mail attachment (stored bytes only)",
        "description": (
            "Same extraction and OCR-cache rules as a project document. Reads "
            "`mail_attachments.stored_path` only — never IMAP, never the "
            "network. Returns 409 `not_stored` when the bytes are not on disk."
        ),
        "tags": ["mail", "ocr"],
        "parameters": [
            _path_param("id", "mail_messages.id."),
            _path_param("attachment_id", "mail_attachments.id belonging to that message."),
            _q("page_start", "First page (1-based, inclusive).", "integer"),
            _q("page_end", "Last page (inclusive).", "integer"),
            _q("max_chars", "Maximum characters returned (default 100000).", "integer"),
        ],
        "responses": {
            "200": _json_response("Extracted text", _ref("DocumentText")),
            "409": {
                "description": ("`not_stored` — the attachment's bytes are not on "
                                "the server (never fetched from IMAP here). A "
                                "storage/extraction state (scanned pages, "
                                "unsupported type) is a 200 with "
                                "`status` = ocr_pending | unsupported_type, not "
                                "an error."),
                "content": {"application/json": {"schema": _ref("Error")}},
            },
        },
    },
    ("GET", "/api/v2/ocr/status"): {
        "summary": "OCR cache and queue status for a document or attachment",
        "description": (
            "Per-page coverage (`pages_needed`, `pages_cached`, `pages_missing`), "
            "the active/last job, file sha256 and the engine that produced the "
            "cache. Never returns OCR text. The target ids are required per "
            "`source`: project_document needs project_id + document_id, "
            "mail_attachment needs message_id + attachment_id; a request with "
            "only `source` is a 400."
        ),
        "tags": ["ocr"],
        "parameters": [
            _q("source", "project_document | mail_attachment.", required=True),
            _q("project_id", "Required when source=project_document.", "integer"),
            _q("document_id", "Required when source=project_document.", "integer"),
            _q("message_id", "Required when source=mail_attachment.", "integer"),
            _q("attachment_id", "Required when source=mail_attachment.", "integer"),
        ],
        "x-ocr-target": OCR_TARGET,
        "responses": {"200": _json_response("OCR status", _ref("OcrStatus"))},
    },
    ("POST", "/api/v2/ocr/request"): {
        "summary": "Queue a document or attachment for OCR (queue row only)",
        "description": (
            "Deduped by file sha256. The only write is one `ocr_queue` row "
            "(audited with ids + hash). Fully cached files are not re-queued; "
            "an active job is returned instead of creating a duplicate. The "
            "ids are required per `source` (oneOf); `{\"source\": …}` alone is "
            "a 400."
        ),
        "tags": ["ocr"],
        "requestBody": _json_body(
            {"oneOf": [_ref("OcrTargetProject"), _ref("OcrTargetMail")]},
            "OCR target: source plus its matching ids."),
        "responses": {"200": _json_response("Queue result", _obj({
            "queued": _BOOL, "cache_reusable": _BOOL, "status": _STR,
            "job": {"type": ["object", "null"]},
        }))},
    },
    ("GET", "/api/v2/ocr/queue"): {
        "summary": "List OCR worker jobs",
        "description": "Worker-only surface (token resource `ocr`). No text, ids and hashes only.",
        "tags": ["ocr"],
        "parameters": [
            _q("status", "pending | in_progress | done | failed."),
            _q("limit", "Max rows (default 50).", "integer"),
        ],
        "responses": {"200": _json_response("OCR jobs", _obj({
            "jobs": _arr(_ref("OcrJob")), "count": _INT,
        }))},
    },
    ("POST", "/api/v2/ocr/queue/{id}/claim"): {
        "summary": "Claim an OCR job (pending -> in_progress)",
        "description": (
            "A stale in_progress job (default 2h) may be reclaimed so a crashed "
            "worker does not block the hash forever."
        ),
        "tags": ["ocr"],
        "parameters": [_path_param("id", "ocr_queue.id.")],
        "responses": {"200": _json_response("Claimed", _obj({
            "ok": _BOOL, "job": _ref("OcrJob"),
        }))},
    },
    ("GET", "/api/v2/ocr/queue/{id}/file"): {
        "summary": "Download the file for a claimed OCR job (worker only)",
        "description": (
            "Raw bytes, scoped to the job and only while the job is pending or "
            "in_progress — this is NOT a general document download. Mail "
            "attachments require stored bytes (409 `not_stored`, never IMAP). "
            "409 `source_changed` means the stored file no longer matches the "
            "job's sha256, so no OCR text is cached for the wrong bytes; "
            "409 `job_not_active` when the job is already done/failed."
        ),
        "tags": ["ocr"],
        "parameters": [_path_param("id", "ocr_queue.id.")],
        "responses": {
            "200": {
                "description": ("The file's raw bytes; Content-Type is the "
                                "stored MIME type."),
                "content": {"application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}}},
            },
            "409": {
                "description": "`not_stored` | `source_changed` | `job_not_active`.",
                "content": {"application/json": {"schema": _ref("Error")}},
            },
        },
    },
    ("POST", "/api/v2/ocr/queue/{id}/pages"): {
        "summary": "Upload OCR text for job pages (writes the page cache)",
        "description": (
            "Cache rows are written ONLY here. Body pages carry `page` and "
            "`text`; the response never echoes text. Engine/version default to "
            "pp-structurev3 / paddleocr-3.7.0."
        ),
        "tags": ["ocr"],
        "parameters": [_path_param("id", "ocr_queue.id.")],
        "requestBody": _json_body(_obj({
            "engine": _STR, "engine_version": _STR,
            "pages": _arr(_obj({"page": _INT, "text": _STR}, ["page", "text"])),
        }, ["pages"]), "Per-page OCR text."),
        "responses": {"200": _json_response("Cached", _obj({
            "ok": _BOOL, "pages_written": _INT,
        }))},
    },
    ("POST", "/api/v2/ocr/queue/{id}/complete"): {
        "summary": "Complete an OCR job (done or failed)",
        "description": (
            "`done` after pages are uploaded; `failed` with a short machine "
            "error (never document text). A failed hash may be re-queued."
        ),
        "tags": ["ocr"],
        "parameters": [_path_param("id", "ocr_queue.id.")],
        "requestBody": _json_body(_obj({
            "status": _STR, "error": _STR,
        }, ["status"]), "done | failed, plus an optional short error code."),
        "responses": {"200": _json_response("Completed", _obj({
            "ok": _BOOL, "job": _ref("OcrJob"),
        }))},
    },
    ("GET", "/api/v2/documents/search"): {
        "summary": "Search documents",
        "tags": ["documents"],
        "parameters": [_q("q", "Filename search."), _q("count", "Max results.", "integer")],
        "responses": {"200": _json_response("Documents", _arr(_ref("Document")))},
        "x-pagination": {"style": "limit-offset", "params": ["count", "startIndex"]},
    },
    # ── reference ──────────────────────────────────────────────────────────
    ("GET", "/api/v2/history-categories"): {
        "summary": "List note/history categories",
        "tags": ["history"],
        "responses": {"200": _json_response("Categories", _arr(_obj({
            "id": _INT, "title": _STR, "isSystem": _BOOL,
        })))},
    },
    # ── mail attachment download / accounts ────────────────────────────────
    ("GET", "/api/v2/mail/messages/{id}/attachments/{attachment_id}/download"): {
        "summary": "Download a mail attachment's raw bytes",
        "description": (
            "Raw file bytes (not JSON). Served from the stored copy when it "
            "exists, otherwise lazily fetched from IMAP. Content-Type is the "
            "attachment's MIME type. For text use the attachment text route."
        ),
        "tags": ["mail"],
        "parameters": [
            _path_param("id", "mail_messages.id."),
            _path_param("attachment_id", "mail_attachments.id belonging to that message."),
        ],
        "responses": {"200": {
            "description": "The attachment's raw bytes",
            "content": {"application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}}},
        }},
    },
    ("GET", "/api/v2/mail/accounts"): {
        "summary": "List mail accounts (no credentials)",
        "description": (
            "Ids, addresses, display names, hosts and connection status. "
            "Passwords and OAuth tokens are never returned (the credential "
            "columns are blanked by the route)."
        ),
        "tags": ["mail"],
        "responses": {"200": _json_response("Mail accounts", _obj({
            "accounts": _arr(_ref("MailAccount")),
        }, ["accounts"]))},
    },
    # ── users / stages ─────────────────────────────────────────────────────
    ("GET", "/api/v2/users"): {
        "summary": "List users (for @-mentions and assignees)",
        "description": "Active and inactive Sietch users; use the id in notifyUserList / responsibleUserId.",
        "tags": ["users"],
        "responses": {"200": _json_response("Users", _arr(_ref("User")))},
    },
    ("GET", "/api/v2/stages"): {
        "summary": "List pipeline stages",
        "description": "Use a stage id with create_project / update_project_fields.",
        "tags": ["projects"],
        "responses": {"200": _json_response("Stages", _arr(_ref("Stage")))},
    },
    # ── history replies ────────────────────────────────────────────────────
    ("GET", "/api/v2/projects/{id}/history/{history_id}/replies"): {
        "summary": "List replies on one timeline event",
        "description": "Oldest first. `isDeleted` replies come back with empty content.",
        "tags": ["history"],
        "parameters": [
            _path_param("id", "Project id."),
            _path_param("history_id", "history_events.id."),
        ],
        "responses": {"200": _json_response("Replies", _arr(_ref("HistoryReply")))},
    },
    ("POST", "/api/v2/projects/{id}/history/{history_id}/replies"): {
        "summary": "Reply to a timeline event",
        "description": (
            "Adds a reply under an existing note (not a new timeline event). "
            "`notifyUserList` @-mentions users; the parent note must still "
            "exist (404 when deleted)."
        ),
        "tags": ["history"],
        "parameters": [
            _path_param("id", "Project id."),
            _path_param("history_id", "history_events.id."),
        ],
        "requestBody": _json_body(_obj({
            "content": _STR,
            "parentReplyId": _INT,
            "notifyUserList": _arr(_INT),
        }, ["content"]), "Reply text (required), optional parent reply and mentions."),
        "responses": {"201": _json_response("Created", _obj({
            "id": _INT, "ok": _BOOL,
        }))},
    },
    # ── MCP admin ──────────────────────────────────────────────────────────
    ("GET", "/api/v2/admin/mcp/settings"): {
        "summary": "MCP server settings, tools and token allowlists",
        "description": "Admin SESSION only — API tokens are refused.",
        "tags": ["admin", "meta"],
        "responses": {"200": _json_response("Settings")},
    },
    ("PUT", "/api/v2/admin/mcp/settings"): {
        "summary": "Enable or disable the MCP server",
        "description": "Admin SESSION only.",
        "tags": ["admin", "meta"],
        "requestBody": _json_body(_obj({"enabled": _BOOL}, ["enabled"]), "New state."),
        "responses": {"200": _json_response("Updated")},
    },
    ("GET", "/api/v2/admin/mcp/proposals"): {
        "summary": "List MCP write proposals awaiting a decision",
        "description": "Admin SESSION only.",
        "tags": ["admin", "meta"],
        "parameters": [_q("status", "pending | approved | rejected | failed | all.")],
        "responses": {"200": _json_response("Proposals")},
    },
    ("POST", "/api/v2/admin/mcp/proposals/{id}/approve"): {
        "summary": "Approve and execute an MCP proposal",
        "description": "Admin SESSION only; the proposing token's restrictions are re-checked.",
        "tags": ["admin", "meta"],
        "parameters": [_path_param("id", "Proposal id.")],
        "requestBody": _json_body(_obj({"note": _STR}), "Optional decision note.", required=False),
        "responses": {"200": _json_response("Approved")},
    },
    ("POST", "/api/v2/admin/mcp/proposals/{id}/reject"): {
        "summary": "Reject an MCP proposal",
        "description": "Admin SESSION only.",
        "tags": ["admin", "meta"],
        "parameters": [_path_param("id", "Proposal id.")],
        "requestBody": _json_body(_obj({"note": _STR}), "Optional decision note.", required=False),
        "responses": {"200": _json_response("Rejected")},
    },
    ("GET", "/api/v2/admin/mcp/audit"): {
        "summary": "Recent MCP tool calls (no token values)",
        "description": "Admin SESSION only.",
        "tags": ["admin", "meta"],
        "parameters": [_q("limit", "Max rows (default 100).", "integer")],
        "responses": {"200": _json_response("Audit entries")},
    },
    ("GET", "/api/v2/admin/mcp/tokens/{id}/tools"): {
        "summary": "Read a token's MCP tool allowlist (empty = all tools)",
        "description": "Admin SESSION only.",
        "tags": ["admin", "meta"],
        "parameters": [_path_param("id", "API token id.")],
        "responses": {"200": _json_response("Allowlist")},
    },
    ("PUT", "/api/v2/admin/mcp/tokens/{id}/tools"): {
        "summary": "Replace a token's MCP tool allowlist",
        "description": "Admin SESSION only. `tools: null` means all tools.",
        "tags": ["admin", "meta"],
        "parameters": [_path_param("id", "API token id.")],
        "requestBody": _json_body(_obj({
            "tools": {"type": ["array", "null"], "items": _STR},
        }, ["tools"]), "Tool names, or null for all tools."),
        "responses": {"200": _json_response("Updated")},
    },
    ("GET", "/api/v2/admin/mcp/training-export"): {
        "summary": "Export MCP approve/reject decisions as JSONL training data",
        "description": "Admin SESSION only.",
        "tags": ["admin", "meta"],
        "responses": {"200": {
            "description": "JSONL, one decision per line",
            "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
        }},
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def _operation_id(method: str, path: str) -> str:
    slug = path.replace("/api/v2/", "").replace("/", "_").replace("{", "").replace("}", "")
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", slug).strip("_")
    return f"{method.lower()}_{slug or 'root'}"


def _path_params(path: str) -> list[dict]:
    return [_path_param(name, f"{name.replace('_', ' ')} path parameter.")
            for name in re.findall(r"\{([^}]+)\}", path)]


def _default_responses(method: str, path: str, success_code: str | None = None) -> dict:
    ok_code = success_code or (
        "201" if method == "POST" and not path.rstrip("/").endswith(("/link", "/approve", "/reject"))
        else "200")
    responses: dict[str, Any] = {
        ok_code: _json_response("Success"),
        "400": {"$ref": "#/components/responses/BadRequest"},
        "401": {"$ref": "#/components/responses/Unauthorized"},
        "403": {"$ref": "#/components/responses/Forbidden"},
        "500": {"$ref": "#/components/responses/ServerError"},
    }
    if re.search(r"\{[^}]+\}", path):
        responses["404"] = {"$ref": "#/components/responses/NotFound"}
    return responses


def build_openapi() -> dict:
    paths: dict[str, dict] = {}
    for route in ROUTES:
        method = route["method"].lower()
        path = route["path"]
        enriched = _ENRICH.get((route["method"], path), {})
        enriched_responses = enriched.get("responses") or {}
        success_code = next((c for c in enriched_responses if c.startswith("2")), None)
        responses = _default_responses(route["method"], path, success_code)
        responses.update(enriched_responses)
        op: dict[str, Any] = {
            "operationId": _operation_id(route["method"], path),
            "summary": enriched.get("summary") or route["summary"],
            "tags": enriched.get("tags") or [route["tag"]],
            "security": [{"bearerAuth": []}, {"sessionCookie": []}],
            "parameters": enriched.get("parameters", _path_params(path)),
            "responses": responses,
        }
        if enriched.get("description"):
            op["description"] = enriched["description"]
        if enriched.get("requestBody"):
            op["requestBody"] = enriched["requestBody"]
        if enriched.get("x-pagination"):
            op["x-pagination"] = enriched["x-pagination"]
        if enriched.get("x-ocr-target"):
            op["x-ocr-target"] = enriched["x-ocr-target"]
        paths.setdefault(path, {})[method] = op

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": API_TITLE,
            "version": _api_version(),
            "description": API_DESCRIPTION,
            "contact": {"name": "Sietch CRM", "url": SERVER_URL},
        },
        "servers": [{"url": SERVER_URL, "description": "Production"}],
        "tags": TAGS,
        "security": [{"bearerAuth": []}, {"sessionCookie": []}],
        "paths": paths,
        "components": COMPONENTS,
    }


def _api_version() -> str:
    try:
        from pathlib import Path
        return (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# MCP single source of truth
# ─────────────────────────────────────────────────────────────────────────────

# tool name → (HTTP method, path template in the spec)
MCP_TOOL_OPERATIONS: dict[str, tuple[str, str]] = {
    "search_projects": ("GET", "/api/v2/projects"),
    "get_project": ("GET", "/api/v2/projects/{id}"),
    "list_project_tags": ("GET", "/api/v2/projects/{id}/tags"),
    "search_contacts": ("GET", "/api/v2/contacts"),
    "create_contact": ("POST", "/api/v2/contacts"),
    "list_users": ("GET", "/api/v2/users"),
    "list_stages": ("GET", "/api/v2/stages"),
    "list_project_history": ("GET", "/api/v2/projects/{id}/history"),
    "list_history_replies": ("GET", "/api/v2/projects/{id}/history/{history_id}/replies"),
    "add_history_reply": ("POST", "/api/v2/projects/{id}/history/{history_id}/replies"),
    "search_mail": ("GET", "/api/v2/mail/messages"),
    "get_mail_message": ("GET", "/api/v2/mail/messages/{id}"),
    "list_mail_attachments": ("GET", "/api/v2/mail/messages/{id}/attachments"),
    "list_mail_accounts": ("GET", "/api/v2/mail/accounts"),
    "list_mail_threads": ("GET", "/api/v2/mail/threads"),
    "list_tasks": ("GET", "/api/v2/tasks"),
    "list_project_documents": ("GET", "/api/v2/projects/{id}/documents"),
    "list_company_documents": ("GET", "/api/v2/documents/company"),
    "get_project_document": ("GET", "/api/v2/projects/{id}/documents/{document_id}/text"),
    "get_mail_attachment_text": ("GET", "/api/v2/mail/messages/{id}/attachments/{attachment_id}/text"),
    "ocr_status": ("GET", "/api/v2/ocr/status"),
    "request_ocr": ("POST", "/api/v2/ocr/request"),
    "list_notes": ("GET", "/api/v2/notes"),
    "list_history_categories": ("GET", "/api/v2/history-categories"),
    "add_note": ("POST", "/api/v2/projects/{id}/history"),
    "add_intake_note": ("POST", "/api/v2/projects/{id}/history"),
    "mention_users": ("POST", "/api/v2/projects/{id}/history"),
    "link_email_to_project": ("POST", "/api/v2/mail/messages/{id}/link"),
    "create_project": ("POST", "/api/v2/projects"),
    "create_task": ("POST", "/api/v2/tasks"),
    "tag_project": ("POST", "/api/v2/projects/{id}/tags"),
    "untag_project": ("DELETE", "/api/v2/projects/{id}/tags/{tag_id}"),
    "update_project_fields": ("PUT", "/api/v2/projects/{id}"),
    "create_email_draft": ("POST", "/api/v2/mail/drafts"),
}


def operation_summary(method: str, path: str) -> str:
    """The spec's summary for an operation ('' when not documented)."""
    doc = _DOC_CACHE.get("doc")
    if doc is None:
        doc = build_openapi()
        _DOC_CACHE["doc"] = doc
    entry = doc.get("paths", {}).get(path, {}).get(method.lower())
    return (entry or {}).get("summary", "")


_DOC_CACHE: dict[str, dict] = {}


def operation_doc(method: str, path: str) -> dict:
    doc = _DOC_CACHE.get("doc")
    if doc is None:
        doc = build_openapi()
        _DOC_CACHE["doc"] = doc
    return doc.get("paths", {}).get(path, {}).get(method.lower(), {})


def reset_cache() -> None:
    _DOC_CACHE.clear()


__all__ = [
    "MCP_TOOL_OPERATIONS",
    "build_openapi",
    "operation_doc",
    "operation_summary",
    "reset_cache",
]
