#!/usr/bin/env python3
"""Unit tests for users + profiles + password-reset deliverability.

Covers the users audit fixes:
  - kebab Reset Password uses the temp-password endpoint (was a bare
    POST with no body -> always 400)
  - users-list click listener guarded (was stacking one handler per refresh)
  - dup-email 409 on create (was a raw 500)
  - avatar serve containment under AVATAR_STORAGE_PATH (was ROOT / value)
  - plaintext reset email: single text/plain part, bare URL + copy-paste
    instruction, no HTML / remote image
  - /reset landing page: rewrite, token handling, existing /api/v2/auth/reset
  - forgot-email button greyed out; no-Telegram fallback directs to admin
    instead of sending a doomed email

Run with: python3 tests/test_users.py
"""

import json
import os
import re
import sys
import unittest
from email import message_from_string
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import server  # noqa: E402
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    sys.exit(0)

import smtp_client  # noqa: E402


def _capture_setup(test):
    orig = server._json_response
    captured = {}
    server._json_response = lambda h, s, b: captured.update(status=s, body=b)
    test.addCleanup(setattr, server, "_json_response", orig)
    return captured


def _admin(test):
    p = patch("server._require_admin", return_value={"id": 1, "email": "a@b.c", "is_admin": True})
    p.start()
    test.addCleanup(p.stop)


def _stub_body(test, body):
    body_bytes = b"" if body is None else json.dumps(body).encode()
    p = patch("server._read_body", return_value=body_bytes)
    p.start()
    test.addCleanup(p.stop)


def _handler():
    class _H:
        pass
    h = _H()
    h.command = "POST"
    h.path = "/api/v2/test"
    return h


class TestCreateDupEmail(unittest.TestCase):
    def test_duplicate_returns_409_not_500(self):
        _admin(self)
        _stub_body(self, {"email": "dup@x.com", "firstName": "D"})
        captured = _capture_setup(self)
        with patch("server.db") as db:
            db.query_one.return_value = {"id": 99}  # existing row
            server.KanbanHandler._handle_user_create(_handler())
        self.assertEqual(captured["status"], 409)
        self.assertIn("already in use", captured["body"].get("error", ""))
        db.insert_returning.assert_not_called()

    def test_missing_email_still_400(self):
        _admin(self)
        _stub_body(self, {"firstName": "No"})
        captured = _capture_setup(self)
        with patch("server.db") as db:
            db.query_one.side_effect = AssertionError("no DB on empty email")
            server.KanbanHandler._handle_user_create(_handler())
        self.assertEqual(captured["status"], 400)


class TestAvatarContainment(unittest.TestCase):
    def _body(self, name):
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.index(f"def {name}")
        nxt = src.find("\n    def ", start + 1)
        nxt2 = src.find("\ndef ", start + 1)
        ends = [e for e in (nxt, nxt2) if e != -1]
        return src[start:min(ends) if ends else len(src)]

    def test_my_avatar_resolves_under_storage(self):
        body = self._body("_handle_avatar_get")
        self.assertIn("(ROOT / avatar_url).resolve()", body)
        self.assertIn("AVATAR_STORAGE_PATH.resolve()", body)
        self.assertIn("avatar_path.parents", body)

    def test_user_avatar_resolves_under_storage(self):
        body = self._body("_handle_user_avatar_get")
        self.assertIn("(ROOT / avatar_url).resolve()", body)
        self.assertIn("avatar_path.parents", body)

    def test_no_bare_root_join_for_avatar_serve(self):
        for name in ("_handle_avatar_get", "_handle_user_avatar_get"):
            body = self._body(name)
            # The old pattern assigned ROOT / avatar_url directly with no
            # resolve. It must be gone from both serve handlers. (The
            # delete-personal-data unlink path has its own guarded
            # version and is out of scope here.)
            self.assertNotIn("avatar_path = ROOT / avatar_url", body)


class TestPlaintextResetEmail(unittest.TestCase):
    def _send(self, **kw):
        cfg = {
            "email": "crm@example.com", "smtp_host": "h", "smtp_port": 587,
            "smtp_user": "u", "smtp_password_encrypted": "p",
            "oauth_provider": None, "oauth_access_token": None,
        }
        cfg.update(kw.pop("cfg", {}))
        with patch("smtp_client.send_email_from_account",
                   return_value=(True, None)) as m:
            ok = smtp_client.send_password_reset_email(
                "user@example.com", "https://x/reset?token=abc", smtp_config=cfg)
        self.assertTrue(ok)
        return m.call_args

    def test_single_text_plain_part_only(self):
        call = self._send()
        html_body = call.kwargs.get("html_body", "")
        self.assertEqual(html_body, "")
        self.assertIn("reset?token=abc", call.kwargs.get("text_body", ""))

    def test_bare_url_plus_copy_paste_instruction(self):
        call = self._send()
        text = call.kwargs.get("text_body", "")
        self.assertIn("https://x/reset?token=abc", text)
        self.assertIn("copy", text.lower())
        self.assertIn("paste", text.lower())
        self.assertNotIn("<html", text)
        self.assertNotIn("<img", text)
        self.assertNotIn("<a href", text)

    def test_no_remote_image_anywhere(self):
        src = Path(smtp_client.__file__).read_text(encoding="utf-8")
        fn = src[src.index("def send_password_reset_email"):]
        fn = fn[fn.index('"""', fn.index("\n")) + 3:]  # strip docstring
        fn = fn[fn.index('"""') + 3:]
        self.assertNotIn("<img", fn)
        self.assertNotIn("MIMEText(html_body", fn)

    def test_fallback_path_is_text_only(self):
        with patch("smtp_client.send_email") as m:
            m.return_value = True
            ok = smtp_client.send_password_reset_email(
                "u@e.c", "https://x/reset?token=t", smtp_config=None)
        self.assertTrue(ok)
        args, kwargs = m.call_args
        html_body = kwargs.get("html_body", args[2] if len(args) > 2 else "KEEP")
        text_body = kwargs.get("text_body", args[3] if len(args) > 3 else "")
        self.assertEqual(html_body or "", "")
        self.assertIn("reset?token=t", text_body)


class TestResetLandingPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reset_html = (HERE.parent / "public" / "reset.html").read_text(encoding="utf-8")
        cls.server_src = Path(server.__file__).read_text(encoding="utf-8")

    def test_reset_html_exists_and_posts_to_reset_endpoint(self):
        self.assertIn("/api/v2/auth/reset", self.reset_html)
        self.assertIn("token", self.reset_html)

    def test_reset_html_reads_token_from_query(self):
        self.assertIn("URLSearchParams", self.reset_html)
        self.assertIn('get("token")', self.reset_html)

    def test_reset_html_handles_missing_token(self):
        self.assertIn("missing", self.reset_html.lower())

    def test_server_rewrites_reset_to_reset_html(self):
        self.assertIn('urlparse(self.path).path == "/reset"', self.server_src)
        self.assertIn('"/reset.html"', self.server_src)

    def test_reset_endpoint_still_verifies_single_use(self):
        src = self.server_src
        start = src.index("def _handle_password_reset(self)")
        nxt = src.find("\n    def ", start + 1)
        body = src[start:nxt if nxt != -1 else len(src)]
        self.assertIn("verify_reset_token", body)
        self.assertIn("consume_reset_token", body)


class TestEmailGreyOut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        cls.html_src = (HERE.parent / "public" / "index.html").read_text(encoding="utf-8")

    def test_email_button_disabled_in_markup(self):
        m = re.search(r'<button[^>]*id="forgot-email-btn"[^>]*>', self.html_src)
        self.assertIsNotNone(m)
        self.assertIn("disabled", m.group(0))

    def test_no_telegram_fallback_directs_to_admin(self):
        self.assertIn("ask your admin for a temporary password", self.app_src)

    def test_fallback_does_not_send_email(self):
        # The old no-Telegram fallback called /reset-request (exact).
        # Scope to the forgot-submit handler's fallback branch: from the
        # "No Telegram linked" comment to the end of that else-block.
        # (The separate forgot-email-btn handler intentionally keeps its
        # send for when email is re-enabled; the disabled-button guard
        # blocks it until then.)
        start = self.app_src.index("// No Telegram linked. Email resets are temporarily disabled")
        block = self.app_src[start:start + 900]
        self.assertIn("ask your admin", block)
        self.assertNotIn('/api/v2/auth/reset-request"', block)

    def test_email_button_click_guarded(self):
        self.assertIn("btn.disabled", self.app_src)


class TestKebabResetUsesTempFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")

    def test_kebab_calls_temp_endpoint(self):
        start = self.app_src.index("// Reset Password — same temp-password flow")
        block = self.app_src[start:start + 1200]
        self.assertIn('/reset-password`, { method: "POST", body: JSON.stringify({ method: "temp" }) }', block)
        self.assertIn("showTempPasswordDialog", block)

    def test_no_bare_send_call_in_kebab(self):
        start = self.app_src.index("// Reset Password — same temp-password flow")
        block = self.app_src[start:start + 2500]
        # Strip // comments: the explanatory comment above the handler
        # names the old endpoint, which must not trip this assertion.
        code = "\n".join(
            line.split("//")[0] for line in block.splitlines()
        )
        self.assertNotIn("reset-password/send", code)


class TestUsersListListenerGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")

    def test_container_click_guarded(self):
        start = self.app_src.index("Click handler for the user list")
        block = self.app_src[start:start + 1200]
        # The exact guard statement, not just a mention in a comment.
        self.assertIn("if (!container.dataset.bound) {", block)
        self.assertIn('container.dataset.bound = "1";', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
