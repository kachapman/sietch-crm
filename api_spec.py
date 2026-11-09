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
    "contact": _obj({"displayName": _STR, "company": _STR}),
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

MAIL_MESSAGE = _obj({
    "id": _INT, "accountId": _INT, "folder": _STR, "subject": _STR,
    "fromAddr": _STR, "toAddr": _STR,
    "dateReceived": {"type": "string", "format": "date-time"},
    "isRead": _BOOL, "isFlagged": _BOOL,
    "attachmentCount": _INT,
    "tags": _arr(_obj({"title": _STR, "color": _STR})),
    "linkedDeals": _arr(_obj({"dealId": _INT, "title": _STR})),
}, ["id"])

DOCUMENT = _obj({
    "id": _INT, "filename": _STR, "filePath": _STR,
    "mimeType": _STR, "sizeBytes": _INT,
    "folderId": _INT,
    "createdAt": {"type": "string", "format": "date-time"},
}, ["id", "filename"])

USER = _obj({
    "id": _INT, "email": _STR, "displayName": _STR,
    "firstName": _STR, "lastName": _STR, "isAdmin": _BOOL,
}, ["id", "email"])

PAGINATION = _obj({
    "page": _INT, "pageSize": _INT, "total": _INT,
}, desc="Limit/offset pagination metadata.")

COMPONENTS = {
    "schemas": {
        "Error": ERROR_SCHEMA,
        "Project": PROJECT,
        "Contact": CONTACT,
        "Task": TASK,
        "Note": NOTE,
        "HistoryEvent": HISTORY_EVENT,
        "MailMessage": MAIL_MESSAGE,
        "Document": DOCUMENT,
        "User": USER,
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
        "description": "Limit/offset pagination. `search` matches title, description and contact.",
        "tags": ["projects"],
        "parameters": [
            _q("search", "Free-text search (title/description/contact)."),
            _q("count", "Max results (default 500).", "integer"),
            _q("startIndex", "Offset (default 0).", "integer"),
            _q("stage_type", "0=open, 1=won, 2=lost.", "integer"),
            _q("include_closed", "Include won/lost projects.", "boolean"),
            _q("sort_by", "date_created | title | bid_value | stage."),
            _q("sort_order", "ascending | descending."),
        ],
        "responses": {"200": _json_response("Projects", _arr(_ref("Project")))},
        "x-pagination": {"style": "limit-offset", "params": ["count", "startIndex"]},
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
        "tags": ["history"],
        "parameters": [_path_param("id", "Project id."),
                       _q("count", "Max events (default 50).", "integer")],
        "responses": {"200": _json_response("History events", _arr(_ref("HistoryEvent")))},
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
        "tags": ["mail"],
        "parameters": [_path_param("id", "mail_messages.id.")],
        "responses": {"200": _json_response("Message", _ref("MailMessage"))},
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
        "tags": ["documents"],
        "parameters": [_path_param("id", "Project id.")],
        "responses": {"200": _json_response("Documents", _arr(_ref("Document")))},
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
    "search_contacts": ("GET", "/api/v2/contacts"),
    "list_project_history": ("GET", "/api/v2/projects/{id}/history"),
    "search_mail": ("GET", "/api/v2/mail/messages"),
    "get_mail_message": ("GET", "/api/v2/mail/messages/{id}"),
    "list_mail_threads": ("GET", "/api/v2/mail/threads"),
    "list_tasks": ("GET", "/api/v2/tasks"),
    "list_project_documents": ("GET", "/api/v2/projects/{id}/documents"),
    "list_notes": ("GET", "/api/v2/notes"),
    "list_history_categories": ("GET", "/api/v2/history-categories"),
    "add_note": ("POST", "/api/v2/projects/{id}/history"),
    "add_intake_note": ("POST", "/api/v2/projects/{id}/history"),
    "mention_users": ("POST", "/api/v2/projects/{id}/history"),
    "link_email_to_project": ("POST", "/api/v2/mail/messages/{id}/link"),
    "create_project": ("POST", "/api/v2/projects"),
    "create_task": ("POST", "/api/v2/tasks"),
    "tag_project": ("POST", "/api/v2/projects/{id}/tags"),
    "update_project_fields": ("PUT", "/api/v2/projects/{id}"),
    "create_email_draft": ("POST", "/api/v2/mail/drafts"),
    "get_carrier_estimate_lines": ("GET", "/api/v2/projects/{id}/carrier-estimate-lines"),
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
