"""MCP tool registry: JSON schemas + mapping onto Sietch's /api/v2 routes.

Every tool builds a normal API request (method, path, JSON body) that is then
executed in-process by `mcp_local_api`. That keeps one implementation of
authorization, validation and serialization for the dashboard, the REST API,
API tokens and MCP.

Write tools carry `mode: "propose" | "apply"` (default propose). There is
deliberately NO send-email tool and no tool path may contain `/mail/send`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

# Any tool path containing this is refused (defence in depth, see tests).
FORBIDDEN_PATH_MARKERS = ("/mail/send", "/mail/send/undo")


@dataclass(frozen=True)
class ToolContext:
    token: str
    user: dict
    call: Callable[..., Any]  # (method, path, body) -> LocalApiResponse


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    # "read"     — GET, no side effects
    # "write"    — propose/apply (human-in-the-loop by default)
    # "queue"    — immediate write that only inserts an internal queue row
    #              (request_ocr); never a CRM record, so no approve step.
    kind: str
    build: Callable[[ToolContext, dict], tuple[str, str, dict | None]] | None = None
    summarize: Callable[[dict], str] = lambda args: ""
    # Optional result shaping: (payload, response headers, args) -> payload.
    # Used to add paging metadata from X-Total-Count, to strip non-public
    # fields, and to keep the tool result small.
    shape: Callable[[Any, dict, dict], Any] | None = None
    # For tools whose underlying route no longer exists: return this instead
    # of calling the API (never a fake success).
    static_result: Callable[[ToolContext, dict], dict] | None = None

    @property
    def is_write(self) -> bool:
        return self.kind == "write"

    @property
    def is_queue(self) -> bool:
        return self.kind == "queue"


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _s(desc: str, **extra) -> dict:
    out = {"type": "string", "description": desc}
    out.update(extra)
    return out


def _i(desc: str, **extra) -> dict:
    out = {"type": "integer", "description": desc}
    out.update(extra)
    return out


def _b(desc: str, **extra) -> dict:
    out = {"type": "boolean", "description": desc}
    out.update(extra)
    return out


def _q(value: Any) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _mode_prop() -> dict:
    return {
        "type": "string",
        "enum": ["propose", "apply"],
        "description": (
            "propose (default) stores the action for a human to approve in Sietch; "
            "apply writes immediately (still subject to the token's permissions)."
        ),
        "default": "propose",
    }


WRITE_SCHEMA_EXTRA = {"mode": _mode_prop()}


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def _search_projects(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    count = max(1, min(int(args.get("count") or 25), 100))
    start = max(0, int(args.get("startIndex") or 0))
    # MCP `query` maps to the REST `search` parameter (do not rename).
    path = f"/api/v2/projects?search={_q(args.get('query') or '')}&count={count}"
    if start:
        path += f"&startIndex={start}"
    if args.get("include_closed"):
        path += "&include_closed=true"
    if args.get("stage_type") is not None:
        path += f"&stage_type={int(args['stage_type'])}"
    if args.get("tag_id") not in (None, ""):
        path += f"&tag_id={_q(args['tag_id'])}"
    return "GET", path, None


def _get_project(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", f"/api/v2/projects/{int(args['project_id'])}", None


def _list_project_tags(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", f"/api/v2/projects/{int(args['project_id'])}/tags", None


def _search_contacts(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    count = max(1, min(int(args.get("count") or 25), 200))
    return "GET", f"/api/v2/contacts?q={_q(args.get('query') or '')}&count={count}", None


def _list_project_history(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    count = max(1, min(int(args.get("count") or 50), 200))
    start = max(0, int(args.get("startIndex") or 0))
    path = f"/api/v2/projects/{int(args['project_id'])}/history?count={count}"
    if start:
        path += f"&startIndex={start}"
    return "GET", path, None


def _list_history_replies(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return ("GET", f"/api/v2/projects/{int(args['project_id'])}/history/"
            f"{int(args['history_id'])}/replies", None)


def _search_mail(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    page_size = max(1, min(int(args.get("count") or 25), 100))
    path = (f"/api/v2/mail/messages?page=1&page_size={page_size}"
            f"&folder={_q(args.get('folder') or 'INBOX')}")
    if args.get("query"):
        path += f"&search={_q(args['query'])}"
    if args.get("account_id") is not None:
        path += f"&account_id={int(args['account_id'])}"
    return "GET", path, None


def _get_mail_message(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", f"/api/v2/mail/messages/{int(args['message_id'])}", None


def _list_mail_attachments(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", f"/api/v2/mail/messages/{int(args['message_id'])}/attachments", None


def _list_mail_accounts(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", "/api/v2/mail/accounts", None


def _list_users(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", "/api/v2/users", None


def _list_stages(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", "/api/v2/stages", None


def _list_company_documents(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    path = "/api/v2/documents/company"
    params = {}
    if args.get("folder_id") is not None:
        params["folder_id"] = int(args["folder_id"])
    for key in ("limit", "offset"):
        if args.get(key) is not None:
            params[key] = int(args[key])
    if params:
        from urllib.parse import urlencode
        path += "?" + urlencode(params)
    return "GET", path, None


def _list_mail_threads(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    path = "/api/v2/mail/threads"
    if args.get("folder"):
        path += f"?folder={_q(args['folder'])}"
    return "GET", path, None


def _list_tasks(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", "/api/v2/tasks", None


def _list_project_documents(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", f"/api/v2/projects/{int(args['project_id'])}/documents", None


def _paging_query(args: dict) -> str:
    from urllib.parse import urlencode

    params = {}
    for key in ("page_start", "page_end", "max_chars"):
        value = args.get(key)
        if value not in (None, ""):
            params[key] = int(value)
    return ("?" + urlencode(params)) if params else ""


def _get_project_document(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    path = (f"/api/v2/projects/{int(args['project_id'])}/documents/"
            f"{int(args['document_id'])}/text{_paging_query(args)}")
    return "GET", path, None


def _get_mail_attachment_text(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    path = (f"/api/v2/mail/messages/{int(args['message_id'])}/attachments/"
            f"{int(args['attachment_id'])}/text{_paging_query(args)}")
    return "GET", path, None


_OCR_TARGET_KEYS = ("project_id", "document_id", "message_id", "attachment_id")

# The ids each source requires: a bare {"source": …} is invalid.
_OCR_REQUIRED_IDS = {
    "project_document": ("project_id", "document_id"),
    "mail_attachment": ("message_id", "attachment_id"),
}


def _ocr_target_args(args: dict) -> dict:
    out: dict[str, Any] = {"source": str(args.get("source") or "").strip()}
    for key in _OCR_TARGET_KEYS:
        if args.get(key) not in (None, ""):
            out[key] = int(args[key])
    source = out["source"]
    if source not in _OCR_REQUIRED_IDS:
        raise ValueError("source must be 'project_document' or 'mail_attachment'")
    missing = [k for k in _OCR_REQUIRED_IDS[source] if k not in out]
    if missing:
        raise ValueError(f"source={source} requires {', '.join(missing)}")
    return out


def _ocr_schema() -> dict:
    """oneOf by source: the matching ids are required, not just `source`."""
    def branch(source: str, ids: tuple[tuple[str, str], ...]) -> dict:
        props: dict[str, Any] = {"source": {"const": source,
                                           "description": "Which kind of file the ids refer to."}}
        for key, desc in ids:
            props[key] = _i(desc)
        return {
            "type": "object",
            "properties": props,
            "required": ["source", *[k for k, _ in ids]],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "description": (
            "source=project_document requires project_id + document_id; "
            "source=mail_attachment requires message_id + attachment_id."
        ),
        "oneOf": [
            branch("project_document", (("project_id", "Project id."),
                                        ("document_id", "Document id."))),
            branch("mail_attachment", (("message_id", "mail_messages.id."),
                                       ("attachment_id", "mail_attachments.id "
                                                         "belonging to that message."))),
        ],
    }


def _ocr_status(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    from urllib.parse import urlencode

    params = {k: v for k, v in _ocr_target_args(args).items() if v != ""}
    return "GET", "/api/v2/ocr/status?" + urlencode(params), None


def _request_ocr(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "POST", "/api/v2/ocr/request", _ocr_target_args(args)


def _list_notes(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    path = "/api/v2/notes"
    if args.get("archived"):
        path += "?archived=true"
    return "GET", path, None


def _list_history_categories(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "GET", "/api/v2/history-categories", None


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_category(ctx: ToolContext, title: str) -> int | None:
    """Find a history category id by (case-insensitive) title."""
    try:
        resp = ctx.call("GET", "/api/v2/history-categories", None)
    except Exception:
        return None
    if not resp.ok:
        return None
    data = resp.payload
    if isinstance(data, dict):
        data = data.get("categories") or data.get("items") or []
    if not isinstance(data, list):
        return None
    needle = (title or "").strip().lower()
    for row in data:
        if not isinstance(row, dict):
            continue
        if str(row.get("title") or "").strip().lower() == needle:
            try:
                return int(row.get("id"))
            except (TypeError, ValueError):
                return None
    return None


def _category_ids(ctx: ToolContext) -> list[int]:
    try:
        resp = ctx.call("GET", "/api/v2/history-categories", None)
    except Exception:
        return []
    if not resp.ok:
        return []
    data = resp.payload
    if isinstance(data, dict):
        data = data.get("categories") or data.get("items") or []
    out = []
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict) and row.get("id") is not None:
            try:
                out.append(int(row["id"]))
            except (TypeError, ValueError):
                pass
    return out


def _resolve_note_category(ctx: ToolContext, explicit: Any) -> int:
    """Pick a category that actually exists in this database.

    The API defaults to id 1, but a fresh install may have different ids; the
    tool must not fail with an FK violation just because 1 is missing.
    """
    if explicit is not None and str(explicit).strip() != "":
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    ids = _category_ids(ctx)
    if ids:
        return ids[0]
    return 1


def _note_body(ctx: ToolContext, args: dict) -> dict:
    body: dict[str, Any] = {
        "content": str(args.get("content") or "").strip(),
        "categoryId": _resolve_note_category(ctx, args.get("category_id")),
    }
    if args.get("mentions"):
        body["notifyUserList"] = [int(x) for x in args["mentions"]]
    if args.get("backdated"):
        body["created"] = str(args["backdated"])
    return body


def _add_note(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return "POST", f"/api/v2/projects/{int(args['project_id'])}/history", _note_body(ctx, args)


def _add_intake_note(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body = _note_body(ctx, args)
    if not args.get("category_id"):
        cat = _resolve_category(ctx, "Intake") or _resolve_category(ctx, "Intake Note")
        if cat:
            body["categoryId"] = cat
    return "POST", f"/api/v2/projects/{int(args['project_id'])}/history", body


def _mention_users(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body = {
        "content": str(args.get("content") or "").strip(),
        "categoryId": int(args.get("category_id") or 1),
        "notifyUserList": [int(x) for x in (args.get("user_ids") or [])],
    }
    return "POST", f"/api/v2/projects/{int(args['project_id'])}/history", body


def _link_email(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    return ("POST", f"/api/v2/mail/messages/{int(args['message_id'])}/link",
            {"opportunityId": int(args["project_id"])})


def _create_project(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body = {"title": str(args.get("title") or "").strip()}
    for key in ("description", "stageId", "bidValue", "contactId", "responsibleUserId",
                "expectedCloseDate", "isPrivate"):
        if args.get(key) is not None:
            body[key] = args[key]
    return "POST", "/api/v2/projects", body


def _create_task(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body = {"title": str(args.get("title") or "").strip()}
    for key in ("description", "opportunityId", "responsibleUserId", "dueDate",
                "priority", "categoryId"):
        if args.get(key) is not None:
            body[key] = args[key]
    return "POST", "/api/v2/tasks", body


def _tag_project(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body: dict[str, Any] = {}
    if args.get("tag_id") is not None:
        body["tagId"] = int(args["tag_id"])
    elif args.get("tag"):
        body["title"] = str(args["tag"]).strip()
    return "POST", f"/api/v2/projects/{int(args['project_id'])}/tags", body


def _untag_project(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    """Unlink one tag from a project. Only the project link; the tag stays."""
    return ("DELETE", f"/api/v2/projects/{int(args['project_id'])}/tags/"
            f"{int(args['tag_id'])}", None)


def _create_contact(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body: dict[str, Any] = {}
    for key in ("firstName", "lastName", "company", "email", "phone",
                "mobilePhone", "jobTitle", "isCompany"):
        if args.get(key) is not None:
            body[key] = args[key]
    return "POST", "/api/v2/contacts", body


def _add_history_reply(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body: dict[str, Any] = {"content": str(args.get("content") or "").strip()}
    if args.get("parent_reply_id") is not None:
        body["parentReplyId"] = int(args["parent_reply_id"])
    if args.get("mentions"):
        body["notifyUserList"] = [int(x) for x in args["mentions"]]
    return ("POST", f"/api/v2/projects/{int(args['project_id'])}/history/"
            f"{int(args['history_id'])}/replies", body)


def _update_project_fields(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    fields = args.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty object")
    return "PUT", f"/api/v2/projects/{int(args['project_id'])}", dict(fields)


def _create_email_draft(ctx: ToolContext, args: dict) -> tuple[str, str, dict | None]:
    body = {
        "account_id": args.get("account_id"),
        "to": str(args.get("to") or "").strip(),
        "cc": args.get("cc"),
        "bcc": args.get("bcc"),
        "subject": str(args.get("subject") or "").strip(),
        "body": str(args.get("body") or ""),
        "deal_id": args.get("deal_id"),
    }
    return "POST", "/api/v2/mail/drafts", body


# ─────────────────────────────────────────────────────────────────────────────
# Result shapes (keep MCP payloads small and secret-free)
# ─────────────────────────────────────────────────────────────────────────────

def _header_int(headers: dict | None, name: str) -> int | None:
    raw = (headers or {}).get((name or "").lower())
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _shape_project_page(payload: Any, headers: dict, args: dict) -> dict:
    """Array + X-Total-Count -> a paged envelope the caller can walk."""
    rows = payload if isinstance(payload, list) else []
    start = max(0, int(args.get("startIndex") or 0))
    total = _header_int(headers, "x-total-count")
    if total is None:
        total = start + len(rows)
    return {
        "projects": rows,
        "count": len(rows),
        "startIndex": start,
        "total": total,
        "hasMore": (start + len(rows)) < total,
        "nextStartIndex": (start + len(rows)) if (start + len(rows)) < total else None,
    }


def _shape_mail_attachments(payload: Any, headers: dict, args: dict) -> dict:
    rows: Any = payload
    if isinstance(payload, dict):
        rows = payload.get("attachments")
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append({
            "id": row.get("id"),
            "filename": row.get("filename"),
            "mime_type": row.get("mime_type"),
            "size_bytes": row.get("size_bytes"),
            # Bytes on disk -> the text/OCR tools can read it (never IMAP).
            "stored": bool(str(row.get("stored_path") or "").strip()),
        })
    return {"attachments": out}


# Only these leave the server in a tool result. Passwords, OAuth tokens and
# encrypted blobs are never selected, and are also dropped defensively below.
_MAIL_ACCOUNT_PUBLIC_KEYS = (
    "id", "email", "display_name",
    "imap_host", "imap_port", "smtp_host", "smtp_port", "smtp_user",
    "smtp_from_name", "smtp_auth_disabled",
    "sync_enabled", "monitored_folders", "is_crm_mail", "owner_user_id",
    "isOwner", "canManage", "retention_days", "last_sync",
    "oauth_provider", "oauthStatus", "authType",
    "authError", "identityStatus", "identityError",
)

_SECRET_KEY_RE = re.compile(r"password|token|secret|encrypted|refresh|credential", re.I)


def _shape_mail_accounts(payload: Any, headers: dict, args: dict) -> list:
    rows: Any = payload
    if isinstance(payload, dict):
        rows = payload.get("accounts")
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append({
            k: row.get(k) for k in _MAIL_ACCOUNT_PUBLIC_KEYS
            if k in row and not _SECRET_KEY_RE.search(k)
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

TOOLS: tuple[ToolSpec, ...] = (
    # ── reads ──────────────────────────────────────────────────────────────
    ToolSpec(
        "search_projects",
        "Search Sietch projects/jobs by title, description or contact. Returns id, title, "
        "stage, value, responsible, contact. `query` is the REST `search` parameter. "
        "Paged: pass startIndex to walk past the first page; the result carries `total` "
        "and `nextStartIndex` (the REST array itself is unchanged, the total comes from "
        "its X-Total-Count header). `tag_id` filters to projects with that tag "
        "(comma-separate ids to OR-match).",
        _schema({
            "query": _s("Free-text search. Omit to list recent projects."),
            "count": _i("Page size (1-100, default 25)."),
            "startIndex": _i("Offset (default 0) — page past the first result set."),
            "include_closed": _b("Include won/lost projects."),
            "stage_type": _i("0=open, 1=won, 2=lost."),
            "tag_id": _s("Only projects with this tag id. Comma-separate ids "
                         "(e.g. \"3,9\") to OR-match tags."),
        }),
        "read", _search_projects, shape=_shape_project_page),
    ToolSpec(
        "get_project",
        "Get one project/job by id (title, description, stage, value, contact, "
        "responsible, dates, and its tags).",
        _schema({"project_id": _i("Project id (opportunities.id).")}, ["project_id"]),
        "read", _get_project),
    ToolSpec(
        "list_project_tags",
        "List the tags assigned to one project (id, title, color, isSystem). "
        "Pass an id to untag_project to remove it from this project only.",
        _schema({"project_id": _i("Project id.")}, ["project_id"]),
        "read", _list_project_tags),
    ToolSpec(
        "search_contacts",
        "Search contacts by name, company, email or phone.",
        _schema({
            "query": _s("Free-text search."),
            "count": _i("Max results (1-200, default 25)."),
        }),
        "read", _search_contacts),
    ToolSpec(
        "list_users",
        "List Sietch users (id, email, display name, role, admin flag) for "
        "@-mentions (add_note.mentions / mention_users.user_ids) and task "
        "assignment.",
        _schema({}),
        "read", _list_users),
    ToolSpec(
        "list_stages",
        "List pipeline stages (id, title, stageType, probability) so "
        "create_project can pass a valid stageId.",
        _schema({}),
        "read", _list_stages),
    ToolSpec(
        "list_project_history",
        "List a project's notes/history events (newest first), including "
        "embedded emails. Paged: `count` is the page size and `startIndex` the "
        "offset; the result carries `total` so a long timeline can be walked "
        "page by page instead of being truncated.",
        _schema({
            "project_id": _i("Project id."),
            "count": _i("Page size (1-200, default 50)."),
            "startIndex": _i("Offset (default 0)."),
        }, ["project_id"]),
        "read", _list_project_history),
    ToolSpec(
        "list_history_replies",
        "List the replies on one project timeline event (oldest first).",
        _schema({
            "project_id": _i("Project id."),
            "history_id": _i("history_events.id (a timeline event id)."),
        }, ["project_id", "history_id"]),
        "read", _list_history_replies),
    ToolSpec(
        "search_mail",
        "Search the synced mailbox (subject/from) and list messages in a folder.",
        _schema({
            "query": _s("Search subject or sender."),
            "folder": _s("Mail folder (default INBOX)."),
            "account_id": _i("Restrict to one mail account id."),
            "count": _i("Max messages (1-100, default 25)."),
        }),
        "read", _search_mail),
    ToolSpec(
        "get_mail_message",
        "Get one email message (headers, body_text/body_html, attachments "
        "metadata, tags, linked deals). Attachment bytes are not returned here; "
        "use list_mail_attachments + get_mail_attachment_text.",
        _schema({"message_id": _i("mail_messages.id.")}, ["message_id"]),
        "read", _get_mail_message),
    ToolSpec(
        "list_mail_attachments",
        "List one email's attachments metadata: id, filename, MIME type, size "
        "and whether the bytes are stored on the server (only stored files can "
        "be read as text). No bytes, no IMAP credentials.",
        _schema({"message_id": _i("mail_messages.id.")}, ["message_id"]),
        "read", _list_mail_attachments, shape=_shape_mail_attachments),
    ToolSpec(
        "list_mail_accounts",
        "List mail accounts: ids, addresses, display names, host settings and "
        "connection status. Passwords and OAuth tokens are never returned.",
        _schema({}),
        "read", _list_mail_accounts, shape=_shape_mail_accounts),
    ToolSpec(
        "list_mail_threads",
        "List mail conversation threads.",
        _schema({"folder": _s("Optional folder filter.")}),
        "read", _list_mail_threads),
    ToolSpec(
        "list_tasks",
        "List tasks.",
        _schema({}),
        "read", _list_tasks),
    ToolSpec(
        "list_project_documents",
        "List a project's documents/attachments metadata (name, size, type, folder).",
        _schema({"project_id": _i("Project id.")}, ["project_id"]),
        "read", _list_project_documents),
    ToolSpec(
        "list_company_documents",
        "List company-scoped documents in a folder (or the root): documents, "
        "subfolders and total. Document ids are project_documents ids — the "
        "same id space as GET /api/v2/documents/{id}; a file with an owning "
        "project can also be read as text with get_project_document.",
        _schema({
            "folder_id": _i("Folder id (omit for the root)."),
            "limit": _i("Page size (default 200, max 500)."),
            "offset": _i("Offset (default 0)."),
        }),
        "read", _list_company_documents),
    ToolSpec(
        "get_project_document",
        "Read one project document as text. PDFs are extracted per page with "
        "pdftotext; scanned pages are marked ocr_needed and come back as "
        "ocr_pending until an OCR worker has cached their OCR text. Use "
        "page_start/page_end (1-based, inclusive) to narrow a long file and "
        "max_chars to bound the response.",
        _schema({
            "project_id": _i("Project id."),
            "document_id": _i("Document id (project_documents.id or a "
                              "history_attachments.id shown in the Documents tab)."),
            "page_start": _i("First page (1-based, inclusive)."),
            "page_end": _i("Last page (inclusive)."),
            "max_chars": _i("Maximum characters returned (default 100000)."),
        }, ["project_id", "document_id"]),
        "read", _get_project_document),
    ToolSpec(
        "get_mail_attachment_text",
        "Read one mail attachment as text from the bytes already stored on the "
        "server (no IMAP, no network). Same PDF/page/OCR-cache rules as a "
        "project document; an attachment that was never stored returns "
        "not_stored.",
        _schema({
            "message_id": _i("mail_messages.id."),
            "attachment_id": _i("mail_attachments.id belonging to that message."),
            "page_start": _i("First page (1-based, inclusive)."),
            "page_end": _i("Last page (inclusive)."),
            "max_chars": _i("Maximum characters returned (default 100000)."),
        }, ["message_id", "attachment_id"]),
        "read", _get_mail_attachment_text),
    ToolSpec(
        "ocr_status",
        "Report OCR coverage for one document or mail attachment: per-page "
        "cache coverage, the active/queued job, file sha256 and the engine "
        "version. Never returns OCR text.",
        _ocr_schema(),
        "read", _ocr_status),
    ToolSpec(
        "request_ocr",
        "Queue a document or attachment for OCR (queue row only). Immediate "
        "(not a proposal): the only write is one ocr_queue row, audited with "
        "ids and sha256. OCR runs on a host-agnostic worker, never on this "
        "server. Deduped by file hash; already-cached files are not re-queued.",
        _ocr_schema(),
        "queue", _request_ocr),
    ToolSpec(
        "list_notes",
        "List personal notes (tiles).",
        _schema({"archived": _b("Include archived notes.")}),
        "read", _list_notes),
    ToolSpec(
        "list_history_categories",
        "List the available note/history categories (id + title).",
        _schema({}),
        "read", _list_history_categories),
    # ── writes (propose/apply) ─────────────────────────────────────────────
    ToolSpec(
        "add_note",
        "Add a note to a project's timeline. In propose mode it waits for approval in Sietch.",
        _schema({
            "project_id": _i("Project id."),
            "content": _s("Note text (plain text or simple HTML)."),
            "category_id": _i("History category id (default 1 = Note)."),
            "mentions": {"type": "array", "items": {"type": "integer"},
                         "description": "User ids to @-mention (they get notified)."},
            "backdated": _s("Optional ISO timestamp to backdate the note."),
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id", "content"]),
        "write", _add_note),
    ToolSpec(
        "add_intake_note",
        "Add an intake note to a project (uses the Intake category when one exists).",
        _schema({
            "project_id": _i("Project id."),
            "content": _s("Note text."),
            "category_id": _i("Override the category id."),
            "mentions": {"type": "array", "items": {"type": "integer"}},
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id", "content"]),
        "write", _add_intake_note),
    ToolSpec(
        "mention_users",
        "Post a note/comment that @-mentions Sietch users (they get a notification).",
        _schema({
            "project_id": _i("Project id."),
            "content": _s("Message text."),
            "user_ids": {"type": "array", "items": {"type": "integer"},
                         "description": "User ids to notify."},
            "category_id": _i("History category id (default 1)."),
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id", "content", "user_ids"]),
        "write", _mention_users),
    ToolSpec(
        "link_email_to_project",
        "Link an email message to a project/job (creates the timeline entry + copies attachments).",
        _schema({
            "message_id": _i("mail_messages.id."),
            "project_id": _i("Project id."),
            **WRITE_SCHEMA_EXTRA,
        }, ["message_id", "project_id"]),
        "write", _link_email),
    ToolSpec(
        "create_project",
        "Create a new project/job.",
        _schema({
            "title": _s("Project title."),
            "description": _s("Description."),
            "stageId": _i("Stage id."),
            "bidValue": {"type": "number", "description": "Contract value."},
            "contactId": _i("Contact id."),
            "responsibleUserId": _i("Responsible user id (defaults to the token's user)."),
            "expectedCloseDate": _s("ISO date."),
            "isPrivate": _b("Private project."),
            **WRITE_SCHEMA_EXTRA,
        }, ["title"]),
        "write", _create_project),
    ToolSpec(
        "create_task",
        "Create a task, optionally attached to a project.",
        _schema({
            "title": _s("Task title."),
            "description": _s("Details."),
            "opportunityId": _i("Project id to attach the task to."),
            "responsibleUserId": _i("Assignee user id (defaults to the token's user)."),
            "dueDate": _s("ISO date/time."),
            "priority": _i("Priority (0 = normal)."),
            "categoryId": _i("Task category id."),
            **WRITE_SCHEMA_EXTRA,
        }, ["title"]),
        "write", _create_task),
    ToolSpec(
        "tag_project",
        "Add a tag to a project (creates the tag if the title does not exist).",
        _schema({
            "project_id": _i("Project id."),
            "tag": _s("Tag title."),
            "tag_id": _i("Existing tag id (instead of title)."),
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id"]),
        "write", _tag_project),
    ToolSpec(
        "untag_project",
        "Remove a tag from a project. Removes only this project's link — the "
        "tag definition and its use on other projects are untouched. Requires "
        "the tag id (list_project_tags), never a title alone.",
        _schema({
            "project_id": _i("Project id."),
            "tag_id": _i("Tag id to remove from this project (tag_definitions.id)."),
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id", "tag_id"]),
        "write", _untag_project),
    ToolSpec(
        "create_contact",
        "Create a contact (or a company when isCompany is true).",
        _schema({
            "firstName": _s("First name."),
            "lastName": _s("Last name."),
            "company": _s("Company name."),
            "email": _s("Email address."),
            "phone": _s("Phone."),
            "mobilePhone": _s("Mobile phone."),
            "jobTitle": _s("Job title."),
            "isCompany": _b("Create a company record instead of a person."),
            **WRITE_SCHEMA_EXTRA,
        }, []),
        "write", _create_contact),
    ToolSpec(
        "add_history_reply",
        "Reply to an existing project timeline event (a comment under the "
        "note, not a new timeline event).",
        _schema({
            "project_id": _i("Project id."),
            "history_id": _i("history_events.id of the note being replied to."),
            "content": _s("Reply text (plain text or simple HTML)."),
            "parent_reply_id": _i("Optional parent reply id (nested reply)."),
            "mentions": {"type": "array", "items": {"type": "integer"},
                         "description": "User ids to notify (they get a reply_tagged notification)."},
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id", "history_id", "content"]),
        "write", _add_history_reply),
    ToolSpec(
        "update_project_fields",
        "Update project fields (title, description, stageId, bidValue, contactId, "
        "responsibleUserId, expectedCloseDate, probability, isPrivate).",
        _schema({
            "project_id": _i("Project id."),
            "fields": {"type": "object", "description": "Fields to change (API field names).",
                       "additionalProperties": True},
            **WRITE_SCHEMA_EXTRA,
        }, ["project_id", "fields"]),
        "write", _update_project_fields),
    ToolSpec(
        "create_email_draft",
        "Create an email DRAFT in Sietch (never sends). A human reviews and sends it in the UI.",
        _schema({
            "account_id": _i("Mail account id."),
            "to": _s("Recipient address."),
            "cc": _s("CC."),
            "bcc": _s("BCC."),
            "subject": _s("Subject."),
            "body": _s("HTML body."),
            "deal_id": _i("Optional project id to link the draft to."),
            **WRITE_SCHEMA_EXTRA,
        }, ["to", "subject", "body"]),
        "write", _create_email_draft),
)

def _spec_description(spec: ToolSpec) -> str:
    """Prefix the tool description with the OpenAPI operation summary.

    The REST spec is the single source of truth: a tool may not describe an
    endpoint differently from the documentation.
    """
    op = _api_spec.MCP_TOOL_OPERATIONS.get(spec.name)
    if not op:
        return spec.description
    summary = _api_spec.operation_summary(*op)
    if not summary:
        return spec.description
    if summary.lower() in spec.description.lower():
        return spec.description
    return f"{summary}. {spec.description}"


import api_spec as _api_spec  # noqa: E402  (spec is stdlib-only; no cycle)

TOOLS = tuple(
    ToolSpec(
        name=t.name,
        description=_spec_description(t),
        input_schema=t.input_schema,
        kind=t.kind,
        build=t.build,
        summarize=t.summarize,
        shape=t.shape,
        static_result=t.static_result,
    )
    for t in TOOLS
)

TOOLS_BY_NAME = {t.name: t for t in TOOLS}
TOOL_NAMES = tuple(t.name for t in TOOLS)


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS_BY_NAME.get(name)


def check_path_allowed(path: str) -> None:
    """Refuse any tool path that could send email (defence in depth)."""
    lowered = (path or "").lower()
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in lowered:
            raise ValueError(f"MCP tools may not call {marker}")


def mode_of(args: dict) -> str:
    mode = str((args or {}).get("mode") or "propose").strip().lower()
    return "apply" if mode == "apply" else "propose"


def strip_mode(args: dict) -> dict:
    out = dict(args or {})
    out.pop("mode", None)
    return out


def summarize_args(args: dict, limit: int = 300) -> str:
    """Short, secret-free description of the arguments for the audit log."""
    if not isinstance(args, dict):
        return ""
    parts = []
    for key in sorted(args):
        if re.search(r"token|password|secret|authorization|cookie", key, re.I):
            parts.append(f"{key}=<redacted>")
            continue
        value = args[key]
        text = str(value)
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
        if sum(len(p) for p in parts) > limit:
            break
    return "; ".join(parts)[:limit]


__all__ = [
    "FORBIDDEN_PATH_MARKERS",
    "TOOLS",
    "TOOLS_BY_NAME",
    "TOOL_NAMES",
    "ToolContext",
    "ToolSpec",
    "check_path_allowed",
    "get_tool",
    "mode_of",
    "strip_mode",
    "summarize_args",
]
