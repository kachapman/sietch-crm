"""In-process calls into Sietch's own /api/v2 handlers (no loopback HTTP).

WHY
---
The MCP server must expose the *same* service layer the dashboard uses: the
same auth, the same token restrictions, the same validation and JSON shapes.
Shelling out to `curl http://127.0.0.1:8766/...` would add a socket hop, a
second auth parse, a timeout/DoS surface and a chance for the two paths to
diverge. Instead we instantiate the real `KanbanHandler` without going through
`BaseHTTPRequestHandler.__init__` (which would call `handle()` and block on a
socket) and let its normal routing run against a synthetic request.

What the shim provides:
  * `path`, `command`, `headers` (Authorization: Bearer … or a session cookie)
  * `rfile` (request body) and `wfile` (response body)
  * `send_response` / `send_header` / `end_headers` that capture instead of
    writing to a socket
  * `log_message` / `log_error` silenced

Everything else — `_require_auth`, `api_tokens.request_allowed`,
`api_token_audit_log`, `_mail_account_accessible`, note normalization, the
event bus — runs exactly as it does for a browser or a `curl` client.
"""

from __future__ import annotations

import email.message
import io
import json
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("sietch.mcp.local_api")

MAX_BODY_BYTES = 4 * 1024 * 1024
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
# Only /api/ paths: a non-API path would fall through to static-file serving.
PATH_PREFIX = "/api/"

_SHIM_CLASS = None


class LocalApiError(RuntimeError):
    """Raised when the in-process call itself cannot run (never for 4xx)."""


class LocalApiResponse:
    __slots__ = ("status", "payload", "text", "headers")

    def __init__(self, status: int, payload: Any, text: str,
                 headers: dict[str, str] | None = None):
        self.status = status
        self.payload = payload
        self.text = text
        # Lower-cased response headers (e.g. x-total-count for list paging).
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_message(self) -> str:
        if isinstance(self.payload, dict):
            return str(self.payload.get("error") or self.payload.get("message") or "")
        return ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LocalApiResponse(status={self.status})"


def _shim_class():
    """Build (once) the capture handler subclass of the real KanbanHandler."""
    global _SHIM_CLASS
    if _SHIM_CLASS is not None:
        return _SHIM_CLASS

    import server as app

    class _CaptureHandler(app.KanbanHandler):
        """A KanbanHandler that never touches a socket."""

        def __init__(self) -> None:  # deliberately does NOT call super()
            self.path = "/"
            self.command = "GET"
            self.headers = email.message.Message()
            self.rfile = io.BytesIO(b"")
            self.wfile = io.BytesIO()
            self.client_address = ("127.0.0.1", 0)
            self.request = None
            self.request_version = "HTTP/1.1"
            self.close_connection = True
            self._status: int | None = None
            self._captured_headers: dict[str, str] = {}

        # ── capture instead of write ────────────────────────────────────
        def send_response(self, code, message=None):  # noqa: D102
            self._status = int(code)

        def send_response_only(self, code, message=None):  # noqa: D102
            self._status = int(code)

        def send_header(self, key, value):  # noqa: D102
            self._captured_headers[str(key).lower()] = str(value)

        def end_headers(self):  # noqa: D102
            return

        def log_message(self, *args, **kwargs):  # noqa: D102
            return

        def log_error(self, *args, **kwargs):  # noqa: D102
            return

        def log_request(self, *args, **kwargs):  # noqa: D102
            return

    _SHIM_CLASS = _CaptureHandler
    return _SHIM_CLASS


def _build_handler(method: str, path: str, body: dict | None,
                   bearer: str | None, session: str | None):
    handler = _shim_class()()
    handler.command = method.upper()
    handler.path = path
    headers = email.message.Message()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if session:
        headers["Cookie"] = f"sietch_session={session}"
    payload = b""
    if body is not None:
        payload = json.dumps(body, default=str).encode("utf-8")
        if len(payload) > MAX_BODY_BYTES:
            raise LocalApiError("request body too large")
        headers["Content-Length"] = str(len(payload))
        headers["Content-Type"] = "application/json"
    handler.headers = headers
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    return handler


def _run(method: str, path: str, body: dict | None,
         bearer: str | None, session: str | None) -> LocalApiResponse:
    method = (method or "").upper()
    if method not in ALLOWED_METHODS:
        raise LocalApiError(f"unsupported method {method!r}")
    if not str(path).startswith(PATH_PREFIX):
        raise LocalApiError("only /api/ paths may be called in-process")
    parsed = urlparse(path)
    if not parsed.path.startswith(PATH_PREFIX):
        raise LocalApiError("only /api/ paths may be called in-process")

    handler = _build_handler(method, path, body, bearer, session)
    dispatch = getattr(handler, f"do_{method}")
    try:
        dispatch()
    except LocalApiError:
        raise
    except Exception as exc:  # the handler already logged; surface a clean 500
        logger.exception("in-process %s %s failed", method, parsed.path)
        raise LocalApiError(f"{type(exc).__name__}: {exc}") from exc

    status = handler._status if handler._status is not None else 200
    raw = handler.wfile.getvalue()
    text = raw.decode("utf-8", errors="replace") if raw else ""
    payload: Any = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
    return LocalApiResponse(status, payload, text,
                            dict(getattr(handler, "_captured_headers", {}) or {}))


def call(token: str, method: str, path: str, body: dict | None = None) -> LocalApiResponse:
    """Call the API as the owner of `token` (Bearer), honouring its restrictions."""
    if not token:
        raise LocalApiError("no API token")
    return _run(method, path, body, bearer=token, session=None)


def call_session(session_token: str, method: str, path: str,
                 body: dict | None = None) -> LocalApiResponse:
    """Call the API with a real, short-lived session cookie.

    Used only by approved MCP proposals: the approval creates a session for the
    proposing token's user, runs the write, then destroys it. Token restrictions
    are re-checked by the caller before this is invoked.
    """
    if not session_token:
        raise LocalApiError("no session token")
    return _run(method, path, body, bearer=None, session=session_token)


__all__ = ["LocalApiError", "LocalApiResponse", "call", "call_session"]
