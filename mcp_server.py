"""Remote MCP server (streamable HTTP) for Sietch CRM.

Transport, sessions, authentication, per-token tool allowlist, audit log and
proposal (human-in-the-loop) mode. Tools live in `mcp_tools.py` and are
executed in-process against the real `/api/v2` handlers by `mcp_local_api.py`.

Design rules (see plans/sietch/sietch-mcp-server.md):
  * The caller's normal Sietch API token is the only credential.
  * Token restrictions (read-only / methods / resources / expiry) are enforced
    by the same code the HTTP API uses.
  * No email send tool, ever. Drafts only.
  * Tokens are never logged or returned; audit rows carry id + name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from typing import Any

import db
import mcp_local_api
import mcp_tools

logger = logging.getLogger("sietch.mcp")

MCP_PATH = "/mcp"
SERVER_NAME = "sietch-crm"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SETTINGS_KEY = "mcp_server"
SESSION_TTL_SECONDS = 60 * 60
MAX_REQUEST_BYTES = 1024 * 1024

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def _version() -> str:
    try:
        from pathlib import Path
        return (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def get_settings() -> dict:
    row = db.query_one("SELECT value FROM system_settings WHERE key = %s", (SETTINGS_KEY,))
    data: dict = {}
    if row and row.get("value"):
        try:
            parsed = json.loads(row["value"])
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, json.JSONDecodeError):
            data = {}
    return {"enabled": bool(data.get("enabled", False))}


def set_enabled(enabled: bool) -> dict:
    payload = json.dumps({"enabled": bool(enabled)})
    db.execute(
        """INSERT INTO system_settings (key, value, description) VALUES (%s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (SETTINGS_KEY, payload, "MCP server (remote tool access)"),
    )
    return get_settings()


def is_enabled() -> bool:
    try:
        return get_settings()["enabled"]
    except Exception:
        logger.exception("Could not read MCP settings")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Auth + allowlist
# ─────────────────────────────────────────────────────────────────────────────

def authenticate(raw_token: str | None) -> dict | None:
    """Verify a Bearer token with the same rules as the HTTP API."""
    if not raw_token:
        return None
    import api_tokens
    return api_tokens.verify_api_token(raw_token)


def allowed_tool_names(token_id: int) -> set[str] | None:
    """None = all tools; otherwise the token's explicit allowlist."""
    rows = db.query_dicts(
        "SELECT tool_name FROM mcp_token_tools WHERE token_id = %s", (int(token_id),))
    if not rows:
        return None
    return {str(r["tool_name"]) for r in rows}


def set_tool_allowlist(token_id: int, tool_names: list[str] | None) -> None:
    """Replace a token's allowlist. Empty/None = all tools (rows deleted)."""
    tid = int(token_id)
    db.execute("DELETE FROM mcp_token_tools WHERE token_id = %s", (tid,))
    for name in (tool_names or []):
        name = str(name).strip()
        if name and name in mcp_tools.TOOLS_BY_NAME:
            db.execute(
                "INSERT INTO mcp_token_tools (token_id, tool_name) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (tid, name),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Audit
# ─────────────────────────────────────────────────────────────────────────────

def audit(token: dict, tool: str, mode: str, args_summary: str,
          result: str, status: str) -> None:
    try:
        db.execute(
            """INSERT INTO mcp_audit_log
                 (token_id, token_name, tool, mode, args_summary, result, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (token.get("api_token_id"), token.get("api_token_name") or "",
             str(tool)[:120], str(mode)[:20], str(args_summary)[:2000],
             str(result)[:2000], str(status)[:40]),
        )
    except Exception:
        logger.exception("MCP audit insert failed")


def list_audit(limit: int = 100) -> list[dict]:
    rows = db.query_dicts(
        """SELECT id, token_id, token_name, tool, mode, args_summary, result, status, created_at
             FROM mcp_audit_log ORDER BY id DESC LIMIT %s""",
        (max(1, min(int(limit or 100), 500)),),
    )
    return [{
        "id": r["id"], "tokenId": r.get("token_id"), "tokenName": r.get("token_name"),
        "tool": r.get("tool"), "mode": r.get("mode"),
        "argsSummary": r.get("args_summary"), "result": r.get("result"),
        "status": r.get("status"),
        "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
    } for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Proposals (human-in-the-loop writes)
# ─────────────────────────────────────────────────────────────────────────────

def create_proposal(token: dict, tool: mcp_tools.ToolSpec, args: dict,
                    method: str, path: str, body: dict | None) -> int:
    row = db.query_one(
        """INSERT INTO mcp_pending_actions
             (token_id, token_name, user_id, tool, args, method, path, request_body,
              restrictions, status)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, 'pending')
           RETURNING id""",
        (token.get("api_token_id"), token.get("api_token_name") or "",
         token.get("id"), tool.name, json.dumps(args, default=str), method, path,
         json.dumps(body, default=str) if body is not None else None,
         json.dumps(token.get("api_token_restrictions") or {})),
    )
    return int(row["id"])


def list_proposals(status: str = "pending", limit: int = 100) -> list[dict]:
    sql = """SELECT id, token_id, token_name, user_id, tool, args, method, path,
                    status, created_at, decided_at, decided_by, decision_note, result
               FROM mcp_pending_actions"""
    params: tuple = ()
    if status and status != "all":
        sql += " WHERE status = %s"
        params = (status,)
    sql += " ORDER BY id DESC LIMIT %s"
    params = params + (max(1, min(int(limit or 100), 500)),)
    rows = db.query_dicts(sql, params)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "tokenId": r.get("token_id"),
            "tokenName": r.get("token_name"),
            "userId": r.get("user_id"),
            "tool": r.get("tool"),
            "args": r.get("args") if isinstance(r.get("args"), (dict, list))
            else (json.loads(r["args"]) if r.get("args") else {}),
            "method": r.get("method"),
            "path": r.get("path"),
            "status": r.get("status"),
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
            "decidedAt": r["decided_at"].isoformat() if r.get("decided_at") else None,
            "decidedBy": r.get("decided_by"),
            "decisionNote": r.get("decision_note"),
            "result": r.get("result") if isinstance(r.get("result"), (dict, list))
            else (json.loads(r["result"]) if r.get("result") else None),
        })
    return out


def _decided_rows() -> list[dict]:
    return db.query_dicts(
        """SELECT id, tool, args, status, decided_at, decided_by, decision_note, result
             FROM mcp_pending_actions
            WHERE status IN ('approved', 'rejected', 'failed')
            ORDER BY id""",
    )


def training_jsonl() -> str:
    """Export approved/rejected/failed proposals as JSONL training data."""
    lines = []
    for r in _decided_rows():
        args = r.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        result = r.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = {"raw": result}
        lines.append(json.dumps({
            "ts": r["decided_at"].isoformat() if r.get("decided_at") else None,
            "proposal_id": r["id"],
            "tool": r.get("tool"),
            "args": args or {},
            "decision": r.get("status"),
            "decided_by": r.get("decided_by"),
            "note": r.get("decision_note") or "",
            "result": result,
        }, default=str))
    return "\n".join(lines) + ("\n" if lines else "")


def decide_proposal(proposal_id: int, decision: str, admin_user: dict,
                    note: str = "") -> dict:
    """Approve (execute) or reject a pending action. Returns the updated row."""
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    row = db.query_one(
        """SELECT id, token_id, token_name, user_id, tool, args, method, path,
                  request_body, restrictions, status
             FROM mcp_pending_actions WHERE id = %s""",
        (int(proposal_id),),
    )
    if not row:
        raise LookupError("proposal not found")
    if row.get("status") != "pending":
        raise ValueError(f"proposal already {row.get('status')}")

    if decision == "reject":
        db.execute(
            """UPDATE mcp_pending_actions
                  SET status = 'rejected', decided_at = NOW(), decided_by = %s, decision_note = %s
                WHERE id = %s AND status = 'pending'""",
            (admin_user.get("id"), (note or "")[:500], int(proposal_id)),
        )
        audit({"api_token_id": row.get("token_id"), "api_token_name": row.get("token_name")},
              row.get("tool") or "", "reject", "", "rejected by admin", "rejected")
        return _proposal_by_id(proposal_id)

    # ── approve: re-check the token, its restrictions, and the allowlist ──
    token_row = db.query_one(
        """SELECT t.id AS token_id, t.name AS token_name, t.scope, t.expires_at,
                  t.restrictions, t.revoked_at, u.id AS user_id, u.is_admin, u.email
             FROM api_tokens t JOIN users u ON u.id = t.created_by
            WHERE t.id = %s""",
        (row.get("token_id"),),
    )
    if not token_row or token_row.get("revoked_at") is not None:
        raise ValueError("the proposing API token is revoked")
    expires = token_row.get("expires_at")
    if expires is not None:
        from datetime import datetime, timezone
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise ValueError("the proposing API token has expired")

    restrictions = token_row.get("restrictions") or {}
    if isinstance(restrictions, str):
        try:
            restrictions = json.loads(restrictions)
        except json.JSONDecodeError:
            restrictions = {}
    import api_tokens
    token_user = {
        "id": token_row["user_id"],
        "email": token_row.get("email"),
        "is_admin": token_row.get("is_admin"),
        "api_token_id": token_row["token_id"],
        "api_token_name": token_row.get("token_name") or "",
        "api_token_scope": token_row.get("scope") or api_tokens.SCOPE_FULL,
        "api_token_restrictions": restrictions,
    }
    method = str(row.get("method") or "POST")
    path = str(row.get("path") or "")
    ok, err = api_tokens.request_allowed(token_user, method, path)
    if not ok:
        raise ValueError(f"token no longer permits this action: {err}")
    allow = allowed_tool_names(token_row["token_id"])
    if allow is not None and str(row.get("tool")) not in allow:
        raise ValueError("tool is no longer on this token's allowlist")
    mcp_tools.check_path_allowed(path)

    body = row.get("request_body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = None

    # Execute with a REAL short-lived session for the token's user. The
    # approval is the authorization; the token restrictions were just checked.
    import auth as auth_mod
    session_token = auth_mod.create_session(int(token_row["user_id"]), ip_address="mcp-approval")
    try:
        resp = mcp_local_api.call_session(session_token, method, path, body)
    finally:
        try:
            auth_mod.destroy_session(session_token)
        except Exception:
            logger.exception("Could not destroy the MCP approval session")

    status = "approved" if resp.ok else "failed"
    result = {"status": resp.status, "payload": resp.payload}
    db.execute(
        """UPDATE mcp_pending_actions
              SET status = %s, decided_at = NOW(), decided_by = %s,
                  decision_note = %s, result = %s::jsonb
            WHERE id = %s AND status = 'pending'""",
        (status, admin_user.get("id"), (note or "")[:500],
         json.dumps(result, default=str), int(proposal_id)),
    )
    audit({"api_token_id": row.get("token_id"), "api_token_name": row.get("token_name")},
          row.get("tool") or "", "apply", "", f"{status}: HTTP {resp.status}", status)
    return _proposal_by_id(proposal_id)


def _proposal_by_id(proposal_id: int) -> dict:
    rows = list_proposals(status="all", limit=500)
    for r in rows:
        if r["id"] == int(proposal_id):
            return r
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────

def _token_fingerprint(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:16]


def _new_session(user: dict, raw_token: str) -> str:
    sid = secrets.token_urlsafe(24)
    with _sessions_lock:
        now = time.time()
        for key in [k for k, v in _sessions.items() if now - v["created"] > SESSION_TTL_SECONDS]:
            _sessions.pop(key, None)
        _sessions[sid] = {
            "token_fp": _token_fingerprint(raw_token),
            "user_id": user.get("id"),
            "created": now,
            "initialized": False,
        }
    return sid


def _session_ok(sid: str | None, raw_token: str) -> bool:
    if not sid:
        return False
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return False
        if time.time() - sess["created"] > SESSION_TTL_SECONDS:
            _sessions.pop(sid, None)
            return False
        return sess["token_fp"] == _token_fingerprint(raw_token)


def _mark_initialized(sid: str | None) -> None:
    if not sid:
        return
    with _sessions_lock:
        if sid in _sessions:
            _sessions[sid]["initialized"] = True


def _drop_session(sid: str | None) -> None:
    if not sid:
        return
    with _sessions_lock:
        _sessions.pop(sid, None)


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC
# ─────────────────────────────────────────────────────────────────────────────

def _rpc_error(msg_id, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _rpc_result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _tool_list(user: dict) -> list[dict]:
    allow = allowed_tool_names(user["api_token_id"])
    tools = []
    for spec in mcp_tools.TOOLS:
        if allow is not None and spec.name not in allow:
            continue
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
        })
    return tools


def _text_result(payload: Any, is_error: bool = False, summary: str | None = None) -> dict:
    if summary is None:
        summary = _bounded_payload(payload)
    if len(summary) > 60000:
        summary = summary[:60000] + "\n…(truncated)"
    out: dict = {
        "content": [{"type": "text", "text": summary}],
        "isError": bool(is_error),
    }
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


TEXT_PREVIEW_CHARS = 2000


def _trim_long_strings(value: Any, depth: int = 0) -> Any:
    """Replace very long strings in a result summary (never in the payload)."""
    if depth > 4:
        return value
    if isinstance(value, str):
        if len(value) > TEXT_PREVIEW_CHARS:
            return value[:TEXT_PREVIEW_CHARS] + "…(truncated)"
        return value
    if isinstance(value, list):
        return [_trim_long_strings(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _trim_long_strings(v, depth + 1) for k, v in value.items()}
    return value


def _bounded_payload(payload: Any) -> str:
    """Human-readable JSON summary that stays valid for large text results.

    Extracted document text can legitimately exceed the text-block budget; a
    raw slice of the JSON would produce invalid JSON for the client. Long
    string values are previewed instead, while `structuredContent` keeps the
    full payload.
    """
    try:
        full = json.dumps(payload, indent=2, default=str)
    except Exception:
        return str(payload)
    if len(full) <= 60000:
        return full
    try:
        return json.dumps(_trim_long_strings(payload), indent=2, default=str)
    except Exception:
        return full


def _call_tool(user: dict, raw_token: str, name: str, args: dict) -> dict:
    spec = mcp_tools.get_tool(name)
    if spec is None:
        return _text_result({"error": f"unknown tool: {name}"}, is_error=True)

    allow = allowed_tool_names(user["api_token_id"])
    if allow is not None and name not in allow:
        audit(user, name, "n/a", mcp_tools.summarize_args(args),
              "tool not permitted for this token", "denied")
        return _text_result(
            {"error": f"tool {name!r} is not enabled for this API token"}, is_error=True)

    mode = mcp_tools.mode_of(args) if spec.is_write else "read"
    clean = mcp_tools.strip_mode(args) if spec.is_write else dict(args or {})
    summary = mcp_tools.summarize_args(args)

    # Tools whose route no longer exists answer honestly, without an API call.
    if spec.static_result is not None:
        payload = spec.static_result(None, clean)  # type: ignore[arg-type]
        audit(user, name, mode, summary, "static result", "ok")
        return _text_result(payload)

    ctx = mcp_tools.ToolContext(
        token=raw_token, user=user,
        call=lambda method, path, body=None: mcp_local_api.call(raw_token, method, path, body),
    )
    try:
        method, path, body = spec.build(ctx, clean)  # type: ignore[misc]
    except (KeyError, TypeError, ValueError) as exc:
        audit(user, name, mode, summary, f"invalid arguments: {exc}", "error")
        return _text_result({"error": f"invalid arguments: {exc}"}, is_error=True)

    mcp_tools.check_path_allowed(path)
    import api_tokens
    ok, err = api_tokens.request_allowed(user, method, path)
    if not ok:
        audit(user, name, mode, summary, f"token restriction: {err}", "denied")
        return _text_result({"error": err or "API token not permitted"}, is_error=True)

    if spec.is_write and mode == "propose":
        try:
            proposal_id = create_proposal(user, spec, clean, method, path, body)
        except Exception as exc:
            logger.exception("MCP proposal insert failed")
            audit(user, name, mode, summary, f"proposal failed: {exc}", "error")
            return _text_result({"error": f"could not store proposal: {exc}"}, is_error=True)
        audit(user, name, mode, summary, f"proposal {proposal_id} pending", "proposed")
        return _text_result({
            "proposal_id": proposal_id,
            "status": "pending",
            "message": ("Proposed. A human must approve it in Sietch → Admin → MCP "
                        "before anything is written."),
            "tool": name,
        })

    try:
        resp = mcp_local_api.call(raw_token, method, path, body)
    except mcp_local_api.LocalApiError as exc:
        audit(user, name, mode, summary, f"call failed: {exc}", "error")
        return _text_result({"error": str(exc)}, is_error=True)

    if not resp.ok:
        msg = resp.error_message() or f"HTTP {resp.status}"
        payload: dict = {"error": msg, "status": resp.status}
        # Keep structured detail from the API error envelope (size_bytes,
        # max_bytes, job status, …). Error envelopes never carry document text.
        if isinstance(resp.payload, dict):
            for key, value in resp.payload.items():
                payload.setdefault(key, value)
        audit(user, name, mode, summary, f"HTTP {resp.status}: {msg}", "error")
        return _text_result(payload, is_error=True)

    audit(user, name, mode, summary, f"HTTP {resp.status}", "ok")
    payload = resp.payload
    if spec.shape is not None:
        # Paging metadata (X-Total-Count), secret-free projections, etc.
        try:
            payload = spec.shape(payload, resp.headers, clean)
        except Exception:
            logger.exception("MCP tool %s result shaping failed", name)
            payload = resp.payload
    return _text_result(payload)


def process_message(user: dict, raw_token: str, msg: Any,
                    session_id: str | None) -> tuple[dict | None, str | None, int]:
    """Handle one JSON-RPC message.

    Returns (payload, new_session_id, http_status). payload None = 202 Accepted
    (notifications).
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request"), None, 200
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        params = msg.get("params") or {}
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        sid = _new_session(user, raw_token)
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        }
        _mark_initialized(sid)
        return _rpc_result(msg_id, result), sid, 200

    if is_notification:
        # notifications/initialized and friends: no response body.
        return None, None, 202

    if method == "ping":
        return _rpc_result(msg_id, {}), None, 200

    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": _tool_list(user)}), None, 200

    if method == "tools/call":
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, -32602, "arguments must be an object"), None, 200
        try:
            result = _call_tool(user, raw_token, name, args)
        except Exception as exc:
            logger.exception("MCP tool %s failed", name)
            return _rpc_error(msg_id, -32603, f"tool failed: {exc}"), None, 200
        return _rpc_result(msg_id, result), None, 200

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return _rpc_result(msg_id, {key: []}), None, 200

    return _rpc_error(msg_id, -32601, f"Method not found: {method}"), None, 200


# ─────────────────────────────────────────────────────────────────────────────
# HTTP entry point (called from server.py)
# ─────────────────────────────────────────────────────────────────────────────

def _send(handler, status: int, payload: dict | None, extra_headers: dict | None = None) -> None:
    body = b"" if payload is None else json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for key, value in (extra_headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _bearer(handler) -> str | None:
    authz = handler.headers.get("Authorization", "") or ""
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        return token or None
    return None


def handle_http(handler) -> None:
    """Serve /mcp: POST (JSON-RPC), GET (405, no SSE stream), DELETE (end session)."""
    method = (handler.command or "GET").upper()

    if not is_enabled():
        _send(handler, 503, {"error": "MCP server is disabled (Admin → MCP)"})
        return

    raw_token = _bearer(handler)
    user = authenticate(raw_token)
    if user is None:
        _send(handler, 401, {"error": "Invalid, expired, or revoked API token"},
              {"WWW-Authenticate": "Bearer"})
        return

    if method == "GET":
        # We do not offer a server-initiated SSE stream; the spec allows 405.
        _send(handler, 405, {"error": "Use POST for MCP requests (no SSE stream)"},
              {"Allow": "POST, DELETE"})
        return

    if method == "DELETE":
        _drop_session(handler.headers.get("Mcp-Session-Id"))
        _send(handler, 204, None)
        return

    if method != "POST":
        _send(handler, 405, {"error": "Method not allowed"}, {"Allow": "POST, DELETE"})
        return

    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0 or length > MAX_REQUEST_BYTES:
        _send(handler, 400, {"error": "Missing or oversized request body"})
        return
    try:
        body = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send(handler, 400, {"error": "Invalid JSON"})
        return

    session_id = handler.headers.get("Mcp-Session-Id")
    if session_id and not _session_ok(session_id, raw_token):
        _send(handler, 404, {"error": "Unknown or expired MCP session"})
        return
    if not session_id and isinstance(body, dict) and body.get("method") != "initialize":
        # Spec: a client must initialize first. Be lenient only for initialize.
        _send(handler, 400, {"error": "Missing Mcp-Session-Id (send initialize first)"})
        return

    if isinstance(body, list):
        # Batching was removed in 2025-06-18; accept it for older clients.
        results = []
        for item in body:
            payload, new_sid, _ = process_message(user, raw_token, item, session_id)
            if payload is not None:
                results.append(payload)
            if new_sid:
                session_id = new_sid
        _send(handler, 200, results, {"Mcp-Session-Id": session_id} if session_id else None)
        return

    payload, new_sid, status = process_message(user, raw_token, body, session_id)
    headers = {}
    if new_sid:
        headers["Mcp-Session-Id"] = new_sid
    _send(handler, status, payload, headers or None)


__all__ = [
    "MCP_PATH",
    "PROTOCOL_VERSION",
    "allowed_tool_names",
    "authenticate",
    "decide_proposal",
    "get_settings",
    "handle_http",
    "is_enabled",
    "list_audit",
    "list_proposals",
    "process_message",
    "set_enabled",
    "set_tool_allowlist",
    "training_jsonl",
]
