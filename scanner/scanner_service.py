"""Minimal HTTP service for the Sietch mail scanner.

Exposes admin endpoints for status, config, log,
reprocess, and action toggles.

Intended to run alongside the scanner module;
the dashboard proxies /api/v2/mail/* requests
through server.py to this service when running
in a separate container.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import mail_scanner

PORT = int(os.environ.get("SCANNER_PORT", "8787"))
ADMIN_TOKEN = os.environ.get("SCANNER_ADMIN_TOKEN", "")


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except Exception:
        length = 0
    return handler.rfile.read(length) if length else b""


def _require_admin(handler: BaseHTTPRequestHandler, cached_body: bytes | None = None) -> tuple[bool, bytes]:
    if not ADMIN_TOKEN:
        return True, cached_body or b""
    supplied = handler.headers.get("X-Scanner-Admin-Token", "") or ""
    if supplied == ADMIN_TOKEN:
        return True, cached_body or b""
    body = cached_body if cached_body is not None else _read_body(handler)
    try:
        parsed = json.loads(body or b"{}")
        if parsed.get("admin_token") == ADMIN_TOKEN:
            return True, body
    except Exception:
        pass
    _json_response(handler, 403, {"error": "Scanner admin token required", "admin_token_required": True})
    return False, body


class ScannerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/status":
            ok, _ = _require_admin(self)
            if not ok:
                return
            _json_response(self, 200, mail_scanner.get_scanner_status())
            return

        if path == "/log":
            ok, _ = _require_admin(self)
            if not ok:
                return
            limit = int(qs.get("limit", ["200"])[0])
            _json_response(self, 200, {"entries": mail_scanner.get_scanner_log(limit)})
            return

        if path == "/config":
            ok, _ = _require_admin(self)
            if not ok:
                return
            cfg = mail_scanner.get_contractors() or {}
            sb = cfg.get("scanner_behavior") or {}
            custom = cfg.get("custom_behaviors") or []
            _json_response(self, 200, {
                "scanner_behavior": sb,
                "custom_behaviors": custom,
            })
            return

        if path == "/feedback":
            ok, _ = _require_admin(self)
            if not ok:
                return
            entries = mail_scanner.get_feedback_entries(limit=200)
            _json_response(self, 200, {"entries": entries})
            return

        _json_response(self, 404, {"error": "Not found"})
        return

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/config":
            ok, body = _require_admin(self)
            if not ok:
                return
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON"})
                return
            cfg = mail_scanner.get_contractors()
            # Update scanner_behavior from payload
            if "scanner_behavior" in payload:
                sb = payload["scanner_behavior"]
                # Merge with existing config
                existing_sb = cfg.get("scanner_behavior", {})
                for key in ("auto_link_project_id", "auto_link_by_content", "post_notes"):
                    if key in sb:
                        existing_sb[key] = sb[key]
                cfg["scanner_behavior"] = existing_sb
            # Update custom_behaviors from payload
            if "custom_behaviors" in payload:
                cfg["custom_behaviors"] = payload["custom_behaviors"]
            mail_scanner.update_contractors(cfg)
            _json_response(self, 200, {"ok": True, "config": cfg})
            return

        _json_response(self, 404, {"error": "Not found"})
        return

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/reprocess":
            ok, body = _require_admin(self)
            if not ok:
                return
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON"})
                return
            ids = payload.get("conversation_ids") or payload.get("ids") or []
            if not ids:
                _json_response(self, 400, {"error": "conversation_ids required"})
                return
            try:
                int_ids = [int(x) for x in ids]
            except (ValueError, TypeError):
                _json_response(self, 400, {"error": "ids must be integers"})
                return
            results = mail_scanner.reprocess_conversations(int_ids)
            _json_response(self, 200, {"results": results})
            return

        if path == "/feedback":
            ok, body = _require_admin(self)
            if not ok:
                return
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON"})
                return
            result = mail_scanner.store_user_feedback(payload)
            _json_response(self, 200, result)
            return

        if path == "/retrain":
            ok, body = _require_admin(self)
            if not ok:
                return
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "Invalid JSON"})
                return
            result = mail_scanner.retrain_classifier_head(
                mock_samples=int(payload.get("samples", 300)),
                use_feedback=bool(payload.get("use_feedback", True)),
            )
            _json_response(self, 200 if result.get("ok") else 500, result)
            return

        _json_response(self, 404, {"error": "Not found"})
        return


def main() -> None:
    mail_scanner.start_scanner()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ScannerHandler)
    print(f"Sietch scanner service listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()