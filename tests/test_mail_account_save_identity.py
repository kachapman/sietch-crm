#!/usr/bin/env python3
"""Account-settings Save must not erase verified mail identity (hotfix 2026-11-10).

Live bug: `_handle_mail_account_update` cleared
`oauth_identity` / `oauth_graph_identity` / `auth_error` / `auth_error_at`
whenever the PUT body merely CONTAINED an `email` key, and the settings modal
sent the whole form on every Save. So each display-name / sharing Save wiped
the identity stamps the reconnect callback had just written, and the account
went back to "Mail account needs reconnecting".

Covered here (offline, synthetic data, no network):
  * same-address Save (also different case / surrounding whitespace) keeps
    oauth_identity, oauth_graph_identity and the auth_error state;
  * a REAL address change still clears them;
  * display-name-only Save keeps identity;
  * a sharing Save (the modal Save with no email key) keeps identity, and the
    share/unshare endpoints never touch credential columns;
  * a PUT can never set token / identity / auth_error columns, and the
    server-side allowlist invariant refuses such an assignment;
  * a blank password field no longer overwrites the stored secret;
  * the client payload builder omits unchanged `email` and blank passwords.

Run with: python3 tests/test_mail_account_save_identity.py
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import network_guard  # noqa: E402

network_guard.install()

try:
    import server as srv  # noqa: E402
except Exception as exc:  # pragma: no cover - needs a reachable DB to import
    srv = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

APP_JS = ROOT / "public" / "app.js"
SERVER_PY = ROOT / "server.py"

PROTECTED = (
    "oauth_identity",
    "oauth_graph_identity",
    "oauth_access_token",
    "oauth_refresh_token",
    "oauth_token_expires",
    "oauth_graph_access_token",
    "oauth_graph_refresh_token",
    "oauth_graph_token_expires",
    "auth_error",
    "auth_error_at",
)


def _account_row(**over):
    row = {
        "id": 8,
        "email": "estimates.masterx@outlook.com",
        "oauth_provider": "microsoft",
        "oauth_access_token": "AT-SECRET",
        "oauth_refresh_token": "RT-SECRET",
        "oauth_graph_access_token": "GAT-SECRET",
        "oauth_graph_refresh_token": "GRT-SECRET",
        "oauth_identity": "estimates.masterx@outlook.com",
        "oauth_graph_identity": "estimates.masterx@outlook.com",
        "auth_error": None,
    }
    row.update(over)
    return row


class FakeDB:
    """Minimal DB double: one account row, records every write."""

    def __init__(self, row):
        self.row = row
        self.writes = []

    def query_one(self, sql, params=None):
        if "mail_accounts" in sql:
            return self.row
        return None

    def query_dicts(self, sql, params=None):
        return []

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return 1


def assigned_columns(sql: str) -> set:
    """Column names an UPDATE ... SET statement assigns."""
    if " SET " not in sql:
        return set()
    body = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    return {part.split("=", 1)[0].strip() for part in body.split(",")}


@unittest.skipIf(srv is None, "needs importable server (live DB)")
class AccountUpdateIdentity(unittest.TestCase):
    def _run(self, body, row=None):
        dbx = FakeDB(row or _account_row())
        responses = []
        with patch.object(srv, "_require_auth", return_value={"id": 7, "is_admin": True}), \
             patch.object(srv, "_mail_account_manageable", return_value=True), \
             patch.object(srv, "_read_body", return_value=json.dumps(body).encode()), \
             patch.object(srv, "_json_response",
                           lambda h, status, payload: responses.append((status, payload))), \
             patch.object(srv, "db", dbx), \
             patch.object(srv, "logger", MagicMock()):
            srv.KanbanHandler._handle_mail_account_update(MagicMock(), 8)
        updates = [w for w in dbx.writes if "UPDATE mail_accounts" in w[0]]
        return updates, responses

    # ── the bug ────────────────────────────────────────────────────────────
    def test_same_address_save_keeps_identity(self):
        updates, responses = self._run({
            "email": "estimates.masterx@outlook.com",
            "display_name": "Estimates",
        })
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(len(updates), 1)
        sql = updates[0][0]
        for col in ("oauth_identity", "oauth_graph_identity", "auth_error", "auth_error_at"):
            self.assertNotIn(col, sql, f"{col} must survive an unchanged-address Save")

    def test_same_address_with_case_and_whitespace_keeps_identity(self):
        updates, responses = self._run({
            "email": "  Estimates.MasterX@Outlook.COM  ",
            "display_name": "Estimates",
        })
        self.assertEqual(responses[0][0], 200)
        sql = updates[0][0]
        self.assertNotIn("oauth_identity", sql)
        self.assertNotIn("oauth_graph_identity", sql)
        self.assertNotIn("auth_error", sql)

    def test_display_name_only_save_keeps_identity(self):
        updates, _ = self._run({"display_name": "Estimates"})
        sql = updates[0][0]
        self.assertNotIn("oauth_identity", sql)
        self.assertNotIn("auth_error", sql)

    def test_sharing_save_keeps_identity(self):
        # The settings modal's Save after editing shares: no email key, no
        # credential keys — identity and auth state must be untouched.
        updates, responses = self._run({
            "display_name": "Estimates",
            "monitored_folders": ["INBOX"],
            "auto_bcc_addr": "",
            "retention_days": 90,
            "signature_html": "",
            "tab_icon": "user",
            "tab_color": "var(--accent)",
        })
        self.assertEqual(responses[0][0], 200)
        sql = updates[0][0]
        for col in ("oauth_identity", "oauth_graph_identity", "auth_error"):
            self.assertNotIn(col, sql)

    def test_real_address_change_clears_identity_and_auth_error(self):
        updates, responses = self._run(
            {"email": "new.address@outlook.com"},
            row=_account_row(oauth_access_token=None, oauth_refresh_token=None,
                             oauth_graph_access_token=None, oauth_graph_refresh_token=None,
                             auth_error="needs reconnecting"),
        )
        self.assertEqual(responses[0][0], 200)
        sql = updates[0][0]
        self.assertIn("oauth_identity = NULL", sql)
        self.assertIn("oauth_graph_identity = NULL", sql)
        self.assertIn("auth_error = NULL", sql)
        self.assertIn("auth_error_at = NULL", sql)
        self.assertIn("new.address@outlook.com", updates[0][1])

    def test_real_address_change_with_tokens_is_refused(self):
        updates, responses = self._run({"email": "new.address@outlook.com"})
        self.assertEqual(responses[0][0], 400)
        self.assertEqual(updates, [], "an address change with live tokens must write nothing")

    # ── belt and braces: client input can never write credentials ─────────
    def test_put_cannot_set_token_or_identity_columns(self):
        updates, responses = self._run({
            "display_name": "Estimates",
            "oauth_access_token": "INJECTED",
            "oauth_refresh_token": "INJECTED",
            "oauth_identity": "attacker@example.invalid",
            "oauth_graph_identity": "attacker@example.invalid",
            "oauth_graph_access_token": "INJECTED",
            "auth_error": "cleared",
            "auth_error_at": "2020-01-01",
        })
        self.assertEqual(responses[0][0], 200)
        sql, params = updates[0]
        for col in PROTECTED:
            self.assertNotIn(col, sql)
        self.assertNotIn("INJECTED", str(params))
        self.assertNotIn("attacker@example.invalid", str(params))

    def test_invariant_flags_protected_assignments(self):
        self.assertEqual(srv._mail_account_protected_writes(
            ["display_name = %s", "email = %s", "monitored_folders = %s"]), set())
        self.assertEqual(srv._mail_account_protected_writes(
            ["oauth_identity = NULL"]), {"oauth_identity"})
        self.assertEqual(srv._mail_account_protected_writes(
            ["auth_error = %s", "oauth_graph_token_expires = %s"]),
            {"auth_error", "oauth_graph_token_expires"})
        # Every column the bug wiped is in the protected set.
        for col in ("oauth_identity", "oauth_graph_identity",
                    "auth_error", "auth_error_at"):
            self.assertIn(col, srv.MAIL_ACCOUNT_PROTECTED_COLUMNS)

    def test_update_sql_assigns_only_allowlisted_columns(self):
        """Whatever the payload, a Save may only write non-credential columns."""
        updates, _ = self._run({
            "email": "estimates.masterx@outlook.com",
            "display_name": "Estimates",
            "imap_host": "outlook.office365.com",
            "imap_port": 993,
            "smtp_host": "smtp.office365.com",
            "smtp_port": 587,
            "smtp_user": "estimates.masterx@outlook.com",
            "smtp_from_name": "Estimates",
            "monitored_folders": ["INBOX", "Estimates"],
            "auto_bcc_addr": "",
            "retention_days": 180,
            "signature_html": "<p>sig</p>",
            "tab_icon": "user",
            "tab_color": "var(--accent)",
        })
        cols = assigned_columns(updates[0][0])
        self.assertTrue(cols)
        self.assertEqual(cols & srv.MAIL_ACCOUNT_PROTECTED_COLUMNS, set())

    # ── adjacent wipe: blank password overwrote the stored secret ─────────
    def test_blank_password_does_not_overwrite_stored_secret(self):
        updates, _ = self._run({
            "email": "estimates.masterx@outlook.com",
            "display_name": "Estimates",
            "password": "",
            "smtp_password": "",
        })
        sql, params = updates[0]
        self.assertNotIn("password_encrypted", sql)
        self.assertNotIn("smtp_password_encrypted", sql)
        self.assertNotIn("", params)

    def test_new_password_is_still_stored(self):
        updates, _ = self._run({
            "display_name": "Estimates",
            "password": "new-imap-secret",
            "smtp_password": "new-smtp-secret",
        })
        sql, params = updates[0]
        self.assertIn("password_encrypted = %s", sql)
        self.assertIn("smtp_password_encrypted = %s", sql)
        self.assertIn("new-imap-secret", params)


@unittest.skipIf(srv is None, "needs importable server (live DB)")
class OtherPathsChecked(unittest.TestCase):
    """Every other settings path that could reset credentials (audit 2026-11-10)."""

    def _region(self, start_marker, end_marker):
        src = SERVER_PY.read_text(encoding="utf-8")
        start = src.index(start_marker)
        end = src.index(end_marker, start)
        return src[start:end]

    def test_share_and_unshare_never_update_mail_accounts(self):
        for start, end in (
            ("def _handle_mail_account_share", "def _handle_mail_account_unshare"),
            ("def _handle_mail_account_unshare", "def _handle_mail_account_shares"),
        ):
            region = self._region(start, end)
            self.assertNotIn("UPDATE mail_accounts", region, start)
            self.assertNotIn("oauth_", region, start)

    def test_signature_put_only_writes_the_signature(self):
        region = self._region("def _handle_mail_account_signature_put",
                              "def _handle_mail_message_attachments")
        self.assertNotIn("oauth_identity", region)
        self.assertNotIn("auth_error", region)
        self.assertNotIn("token", region)

    def test_folder_update_only_relabels_folders(self):
        region = self._region("def _handle_mail_folder_update",
                              "def _handle_mail_folder_delete")
        self.assertNotIn("mail_accounts", region)

    def test_sync_now_only_queues_a_poll(self):
        region = self._region("def _handle_mail_account_sync_now",
                              "def _handle_mail_account_delete")
        self.assertNotIn("UPDATE mail_accounts", region)

    def test_disconnect_paths_are_the_only_identity_nullers(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        # oauth_identity = NULL appears in the explicit Exchange/Graph
        # disconnect handlers and in the real-address-change clear only.
        self.assertEqual(src.count("oauth_identity = NULL"), 2,
                         "only disconnect + real address change may clear oauth_identity")
        exchange = self._region("def _handle_oauth_exchange_disconnect",
                                "def _record_graph_error")
        self.assertIn("oauth_identity = NULL", exchange)


class ClientPayload(unittest.TestCase):
    """The settings modal must send only what the user changed."""

    @classmethod
    def setUpClass(cls):
        cls.js = APP_JS.read_text(encoding="utf-8")

    def _save_block(self):
        start = self.js.index("// Bind save")
        return self.js[start:start + 6000]

    def test_records_the_stored_address_when_populating_the_modal(self):
        self.assertIn("modal.dataset.originalEmail = String(acct.email || '').trim();",
                      self.js)
        self.assertIn("delete modal.dataset.originalEmail;", self.js)

    def test_save_omits_unchanged_email(self):
        block = self._save_block()
        self.assertIn("const originalEmail = String(modal.dataset.originalEmail || '').trim();",
                      block)
        self.assertIn("delete payload.email;", block)
        self.assertLess(block.index("originalEmail.toLowerCase()"),
                        block.index("delete payload.email;"))

    def test_save_omits_blank_passwords(self):
        block = self._save_block()
        self.assertIn("if (!payload.password) delete payload.password;", block)
        self.assertIn("if (!payload.smtp_password) delete payload.smtp_password;", block)

    def test_save_still_validates_the_typed_address(self):
        block = self._save_block()
        self.assertIn("if (!submittedEmail || !payload.imap_host)", block)

    def test_oauth_reconcile_payload_never_sends_credentials(self):
        block = self.js[self.js.index("async function reconcileOAuthConnect"):]
        block = block[:block.index("if (modal) {")]
        payload = block[block.index("JSON.stringify({"):]
        for forbidden in ("email:", "password", "oauth_", "smtp_host", "imap_host"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
