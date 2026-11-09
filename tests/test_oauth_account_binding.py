#!/usr/bin/env python3
"""Regression tests: OAuth tokens must be bound to ONE mail account.

THE BUG (live, 2026-11-09)
--------------------------
Ken's report: the settings page for estimates.masterx@outlook.com (account 8)
showed "Connected as mdm.estimates@outlook.com via Microsoft Graph", trying to
connect Graph while signed in as estimates.masterx failed with
"OAuth failed: You signed in as estimates.masterx@outlook.com, but this
connects mdm.estimates@outlook.com. Disconnect first…", and the other Outlook
accounts lost OAuth access the same day (accounts 8/9/10 `auth_failed`).

Root causes pinned here:

  1. `_handle_oauth_authorize` (IMAP/SMTP flow) minted an opaque state that
     carried no account. The callback then looked the row up by the email in
     the token response, so "Connect" inside account 8 refreshed whichever
     mailbox Microsoft had signed in — or inserted a NEW row with a blank
     email when the identity was unreadable.
  2. The callback wrote `oauth_refresh_token = %s` with "" when the provider
     omitted a refresh token, destroying the account's only renewal path.
  3. `mail_oauth.refresh_access_token` never checked WHICH mailbox the
     refreshed grant belongs to.
  4. The Graph UI bound its Connect/Disconnect handlers once
     (`dataset.bound`) with the first account id, so the buttons acted on the
     wrong mailbox for every later account — which is why "Disconnect first"
     never cleared the token the error complained about.
  5. The account update endpoint accepted `oauth_*` tokens from the client
     (the list endpoint hands the client "" for those fields).

Run with: python3 tests/test_oauth_account_binding.py
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import oauth_state
import mail_oauth_binding as binding


# ─────────────────────────────────────────────────────────────────────────────
# 1. Signed, account-bound state
# ─────────────────────────────────────────────────────────────────────────────

class TestSignedState(unittest.TestCase):
    def setUp(self):
        oauth_state.reset_used_nonces()
        oauth_state.reset_secret_cache()
        self._env = os.environ.get("OAUTH_STATE_SECRET")
        os.environ["OAUTH_STATE_SECRET"] = "unit-test-state-secret"
        oauth_state.reset_secret_cache()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("OAUTH_STATE_SECRET", None)
        else:
            os.environ["OAUTH_STATE_SECRET"] = self._env
        oauth_state.reset_secret_cache()

    def test_round_trip_keeps_the_account_binding(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        payload = oauth_state.verify_state(state, expected_kind=oauth_state.KIND_EXCHANGE)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["a"], 8)
        self.assertEqual(payload["u"], 7)
        self.assertEqual(payload["p"], "microsoft")

    def test_state_survives_a_process_restart(self):
        """The old state lived in a dict, so a redeploy mid-flow lost it."""
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        oauth_state.reset_secret_cache()  # simulate a fresh process (same env key)
        self.assertIsNotNone(oauth_state.verify_state(state))

    def test_tampered_account_id_is_rejected(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        body, sig = state.split(".")
        import base64
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        raw["a"] = 10  # point the flow at MDM's account
        forged = base64.urlsafe_b64encode(json.dumps(raw).encode()).rstrip(b"=").decode()
        self.assertIsNone(oauth_state.verify_state(f"{forged}.{sig}"))

    def test_expired_state_is_rejected(self):
        past = time.time() - oauth_state.STATE_TTL_SECONDS - 5
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft",
                                       account_id=8, now=past)
        self.assertIsNone(oauth_state.verify_state(state))

    def test_graph_state_cannot_be_replayed_into_the_exchange_callback(self):
        state = oauth_state.sign_state(oauth_state.KIND_GRAPH, 7, "microsoft", account_id=8)
        self.assertIsNone(oauth_state.verify_state(state, expected_kind=oauth_state.KIND_EXCHANGE))
        self.assertIsNotNone(oauth_state.verify_state(state, expected_kind=oauth_state.KIND_GRAPH))

    def test_graph_state_must_name_an_account(self):
        with self.assertRaises(ValueError):
            oauth_state.sign_state(oauth_state.KIND_GRAPH, 7, "microsoft")

    def test_state_is_single_use(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        self.assertIsNotNone(oauth_state.consume_state(state))
        self.assertIsNone(oauth_state.consume_state(state))

    def test_extra_flags_round_trip(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft",
                                       account_id=None, extra={"crm": True})
        payload = oauth_state.verify_state(state)
        self.assertTrue(payload["x"]["crm"])
        self.assertIsNone(payload["a"])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Identity rules
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentityRules(unittest.TestCase):
    def test_match(self):
        self.assertEqual(binding.identity_state("A@Outlook.com ", "a@outlook.com")[0],
                         binding.IDENTITY_OK)

    def test_mismatch_names_the_other_mailbox(self):
        state, detail = binding.identity_state("mdm.estimates@outlook.com",
                                               "estimates.masterx@outlook.com")
        self.assertEqual(state, binding.IDENTITY_MISMATCH)
        self.assertEqual(detail, "mdm.estimates@outlook.com")

    def test_msa_prefixed_preferred_username_is_normalized(self):
        self.assertEqual(binding.identity_state("live.com#mdm@outlook.com", "mdm@outlook.com")[0],
                         binding.IDENTITY_OK)

    def test_guest_identity_is_not_treated_as_the_personal_mailbox(self):
        state, _ = binding.identity_state(
            "mdm.com#EXT#@tenant.onmicrosoft.com", "mdm@outlook.com")
        self.assertEqual(state, binding.IDENTITY_MISMATCH)

    def test_unreadable_identity_is_unverifiable_not_ok(self):
        self.assertEqual(binding.identity_state("", "a@b.com")[0],
                         binding.IDENTITY_UNVERIFIABLE)
        self.assertEqual(binding.identity_state(None, "a@b.com")[0],
                         binding.IDENTITY_UNVERIFIABLE)

    def test_account_without_email_is_unverifiable(self):
        self.assertEqual(binding.identity_state("a@b.com", "")[0],
                         binding.IDENTITY_UNVERIFIABLE)

    def test_mismatch_message_tells_the_user_what_to_do(self):
        msg = binding.mismatch_message("mdm@x.com", "masterx@x.com", kind="graph")
        self.assertIn("mdm@x.com", msg)
        self.assertIn("masterx@x.com", msg)
        self.assertIn("Disconnect Graph", msg)


class TestRefreshTokenIsNeverBlanked(unittest.TestCase):
    def test_new_token_wins(self):
        self.assertEqual(binding.refresh_token_to_store("NEW", "OLD"), "NEW")

    def test_missing_new_token_keeps_the_old_one(self):
        """A response without refresh_token used to wipe the account's grant."""
        self.assertEqual(binding.refresh_token_to_store("", "OLD"), "OLD")
        self.assertEqual(binding.refresh_token_to_store(None, "OLD"), "OLD")

    def test_no_tokens_stays_empty(self):
        self.assertEqual(binding.refresh_token_to_store("", None), "")


class TestGrantExclusivity(unittest.TestCase):
    def test_shared_grant_is_found(self):
        db = MagicMock()
        db.query_one.return_value = {"id": 10, "email": "mdm.estimates@outlook.com"}
        other = binding.grant_owner(db, "TOKEN", "oauth_refresh_token", exclude_account_id=8)
        self.assertEqual(other["id"], 10)
        sql, params = db.query_one.call_args[0]
        self.assertIn("id <> %s", sql)
        self.assertEqual(params, ("TOKEN", 8))

    def test_no_conflict(self):
        db = MagicMock()
        db.query_one.return_value = None
        self.assertIsNone(binding.grant_owner(db, "TOKEN", "oauth_graph_refresh_token"))

    def test_unknown_column_is_refused(self):
        with self.assertRaises(ValueError):
            binding.grant_owner(MagicMock(), "TOKEN", "password_encrypted")

    def test_empty_token_never_matches(self):
        db = MagicMock()
        self.assertIsNone(binding.grant_owner(db, "", "oauth_refresh_token"))
        db.query_one.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Refresh path (shared by scanner, server, IMAP flag sync)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDB:
    def __init__(self, email, oauth_identity=None):
        self.email = email
        self.oauth_identity = oauth_identity
        self.writes = []

    def query_one(self, sql, params=None):
        if "SELECT email, oauth_identity FROM mail_accounts" in sql:
            return {"email": self.email, "oauth_identity": self.oauth_identity}
        return None

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return 1


class _StubProvider:
    def __init__(self, email="", reports=True):
        self._email = email
        self.reports_identity_on_refresh = reports

    def refresh_token(self, refresh_token):
        return {"access_token": "NEWAT", "refresh_token": "NEWRT",
                "expires_in": 3600, "email": self._email}


class TestRefreshBindsToTheAccount(unittest.TestCase):
    def _refresh(self, account_email, token_email, reports=True, provider_name="microsoft"):
        import mail_oauth
        fake_db = _FakeDB(account_email)
        prov = _StubProvider(token_email, reports=reports)
        with patch.object(mail_oauth, "db", fake_db), \
             patch("oauth_providers.get_provider", return_value=prov):
            result = mail_oauth.refresh_access_token(
                8, provider_name, "OLD", None, force=True, expected_access="OLDA")
        return result, fake_db

    def test_matching_identity_is_stored_with_the_identity(self):
        result, db = self._refresh("estimates.masterx@outlook.com",
                                   "estimates.masterx@outlook.com")
        self.assertEqual(result, ("NEWAT", "NEWRT"))
        update = [w for w in db.writes if "oauth_access_token = %s" in w[0]]
        self.assertTrue(update)
        self.assertIn("oauth_identity = COALESCE(%s, oauth_identity)", update[0][0])
        self.assertIn("estimates.masterx@outlook.com", update[0][1])

    def test_mismatched_identity_is_refused_and_nothing_is_written(self):
        result, db = self._refresh("estimates.masterx@outlook.com",
                                   "mdm.estimates@outlook.com")
        self.assertIsNone(result)
        self.assertEqual([w for w in db.writes if "oauth_access_token = %s" in w[0]], [])
        errors = [w for w in db.writes if "oauth_last_error = %s" in w[0]]
        self.assertTrue(errors, "the reason must be persisted for the UI")

    def test_unverifiable_identity_is_refused_for_microsoft(self):
        result, db = self._refresh("estimates.masterx@outlook.com", "")
        self.assertIsNone(result)
        self.assertEqual([w for w in db.writes if "oauth_access_token = %s" in w[0]], [])

    def test_previously_verified_account_keeps_refreshing(self):
        """A token response that omits the id_token must not break an account
        that was verified at connect time (oauth_identity == email)."""
        import mail_oauth
        fake_db = _FakeDB("estimates.masterx@outlook.com",
                          oauth_identity="estimates.masterx@outlook.com")
        prov = _StubProvider("", reports=True)
        with patch.object(mail_oauth, "db", fake_db), \
             patch("oauth_providers.get_provider", return_value=prov):
            result = mail_oauth.refresh_access_token(
                8, "microsoft", "OLD", None, force=True, expected_access="OLDA")
        self.assertEqual(result, ("NEWAT", "NEWRT"))

    def test_unverified_account_is_refused_when_the_identity_is_missing(self):
        import mail_oauth
        fake_db = _FakeDB("estimates.masterx@outlook.com", oauth_identity=None)
        prov = _StubProvider("", reports=True)
        with patch.object(mail_oauth, "db", fake_db), \
             patch("oauth_providers.get_provider", return_value=prov):
            result = mail_oauth.refresh_access_token(
                8, "microsoft", "OLD", None, force=True, expected_access="OLDA")
        self.assertIsNone(result)
        self.assertTrue([w for w in fake_db.writes if "oauth_last_error = %s" in w[0]])

    def test_google_refresh_is_not_blocked_by_a_missing_identity(self):
        """Google's refresh response carries no id_token — do not pretend to
        verify an identity the provider never sends."""
        result, db = self._refresh("someone@gmail.com", "", reports=False, provider_name="google")
        self.assertEqual(result, ("NEWAT", "NEWRT"))

    def test_compare_and_swap_is_kept(self):
        result, db = self._refresh("a@b.com", "a@b.com")
        update = [w for w in db.writes if "oauth_access_token = %s" in w[0]][0]
        self.assertIn("AND oauth_access_token = %s", update[0])
        self.assertEqual(update[1][-1], "OLDA")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Callbacks (server.py) — the write itself
# ─────────────────────────────────────────────────────────────────────────────

class _Handler:
    """Minimal stand-in for KanbanHandler used by the callback tests."""

    def __init__(self, path):
        self.path = path
        self.redirects = []

    def send_response(self, *a, **k):
        pass

    def end_headers(self):
        pass

    def _oauth_redirect_with_status(self, status, message):
        self.redirects.append((status, message))


class _CallbackDB:
    def __init__(self, accounts=None, users=None, grant_conflict=None, by_email=None):
        self.accounts = accounts or {}
        self.users = users or {}
        self.grant_conflict = grant_conflict
        self.by_email = by_email
        self.writes = []

    def query_one(self, sql, params=None):
        params = params or ()
        if "FROM users" in sql:
            return self.users.get(params[0])
        if "owner_user_id FROM mail_accounts" in sql:
            acct = self.accounts.get(params[0])
            return {"owner_user_id": acct.get("owner_user_id")} if acct else None
        if "WHERE email = %s" in sql:
            return self.by_email
        if "AND id <> %s" in sql:
            return self.grant_conflict
        if "FROM mail_accounts" in sql:
            return self.accounts.get(params[0])
        return None

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return 1

    def token_writes(self):
        return [w for w in self.writes if "oauth_access_token = %s" in w[0]
                or "INSERT INTO mail_accounts" in w[0]]


def _run_exchange_callback(state, token_email, *, refresh_token="RT", accounts=None,
                           by_email=None, grant_conflict=None, users=None):
    import server as _server
    db = _CallbackDB(accounts=accounts, by_email=by_email,
                     grant_conflict=grant_conflict, users=users)
    handler = _Handler(f"/api/v2/mail/oauth/callback?code=C&state={state}")
    exchange = {"access_token": "AT", "refresh_token": refresh_token,
                "expires_in": 3600, "email": token_email}
    with patch.object(_server, "db", db), \
         patch.object(_server, "logger"), \
         patch.object(_server.oauth_providers.MicrosoftProvider, "exchange_code",
                      classmethod(lambda cls, code: exchange)):
        _server.KanbanHandler._handle_oauth_callback(handler)
    return handler.redirects, db


class TestExchangeCallbackBindsToTheAccount(unittest.TestCase):
    def setUp(self):
        oauth_state.reset_used_nonces()
        oauth_state.reset_secret_cache()
        self.account = {"id": 8, "email": "estimates.masterx@outlook.com",
                        "owner_user_id": 7, "oauth_refresh_token": "OLD"}
        self.accounts = {8: self.account}
        self.users = {7: {"id": 7, "is_admin": False}}

    def test_bound_state_writes_only_that_account(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        redirects, db = _run_exchange_callback(
            state, "estimates.masterx@outlook.com",
            accounts=self.accounts, users=self.users)
        self.assertEqual(redirects[0][0], "success")
        writes = db.token_writes()
        self.assertEqual(len(writes), 1)
        self.assertIn("WHERE id = %s", writes[0][0])
        self.assertEqual(writes[0][1][-1], 8)

    def test_mismatched_identity_writes_nothing(self):
        """Connecting account 8 while signed in as MDM must not store MDM's
        token anywhere (this is Ken's exact error)."""
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        redirects, db = _run_exchange_callback(
            state, "mdm.estimates@outlook.com", accounts=self.accounts, users=self.users)
        self.assertEqual(redirects[0][0], "error")
        self.assertIn("mdm.estimates@outlook.com", redirects[0][1])
        self.assertEqual(db.token_writes(), [])

    def test_unreadable_identity_writes_nothing(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        redirects, db = _run_exchange_callback(
            state, "", accounts=self.accounts, users=self.users)
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual(db.token_writes(), [])

    def test_account_deleted_mid_flow_writes_nothing(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        redirects, db = _run_exchange_callback(
            state, "estimates.masterx@outlook.com", accounts={}, users=self.users)
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual(db.token_writes(), [])

    def test_user_who_lost_access_cannot_write(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        accounts = {8: dict(self.account, owner_user_id=99)}
        redirects, db = _run_exchange_callback(
            state, "estimates.masterx@outlook.com",
            accounts=accounts, users={7: {"id": 7, "is_admin": False}})
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual(db.token_writes(), [])

    def test_missing_refresh_token_does_not_wipe_the_stored_one(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        redirects, db = _run_exchange_callback(
            state, "estimates.masterx@outlook.com", refresh_token="",
            accounts=self.accounts, users=self.users)
        self.assertEqual(redirects[0][0], "success")
        write = db.token_writes()[0]
        self.assertEqual(write[1][2], "OLD", "the stored refresh token must be preserved")

    def test_grant_attached_to_another_account_is_refused(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft", account_id=8)
        redirects, db = _run_exchange_callback(
            state, "estimates.masterx@outlook.com", accounts=self.accounts,
            users=self.users,
            grant_conflict={"id": 10, "email": "mdm.estimates@outlook.com"})
        self.assertEqual(redirects[0][0], "error")
        self.assertIn("mdm.estimates@outlook.com", redirects[0][1])
        self.assertEqual(db.token_writes(), [])

    def test_new_account_flow_refuses_a_blank_identity(self):
        """Used to INSERT a row with email = ''."""
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft")
        redirects, db = _run_exchange_callback(state, "", users=self.users)
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual(db.token_writes(), [])

    def test_new_account_flow_requires_a_refresh_token(self):
        state = oauth_state.sign_state(oauth_state.KIND_EXCHANGE, 7, "microsoft")
        redirects, db = _run_exchange_callback(
            state, "new@outlook.com", refresh_token="", users=self.users)
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual(db.token_writes(), [])

    def test_legacy_in_memory_state_still_completes(self):
        """A flow started by the previous build must not dead-end."""
        import server as _server
        legacy = "legacy-state-token"
        _server._oauth_states[legacy] = (7, "microsoft", time.time() + 600, False)
        try:
            redirects, db = _run_exchange_callback(
                legacy, "estimates.masterx@outlook.com",
                by_email=self.account, users=self.users)
        finally:
            _server._oauth_states.pop(legacy, None)
        self.assertEqual(redirects[0][0], "success")
        self.assertTrue(db.token_writes())


class TestGraphCallbackBindsToTheAccount(unittest.TestCase):
    def setUp(self):
        oauth_state.reset_used_nonces()
        oauth_state.reset_secret_cache()

    def _run(self, token_email, account_email="estimates.masterx@outlook.com",
             refresh_token="GRT", state_kind="signed"):
        import server as _server
        accounts = {8: {"id": 8, "email": account_email,
                        "owner_user_id": 7, "oauth_graph_refresh_token": "GOLD"}}
        db = _CallbackDB(accounts=accounts, users={7: {"id": 7, "is_admin": False}})
        if state_kind == "signed":
            state = oauth_state.sign_state(oauth_state.KIND_GRAPH, 7, "microsoft", account_id=8)
        else:
            state = "legacy-graph-state"
            _server._oauth_graph_states[state] = (7, 8, time.time() + 600)
        handler = _Handler(f"/x?code=C&state={state}")
        tok = {"access_token": "GAT", "refresh_token": refresh_token,
               "expires_in": 3600, "email": token_email}
        with patch.object(_server, "db", db), \
             patch.object(_server, "logger"), \
             patch.object(_server.oauth_providers.MicrosoftProvider, "graph_exchange_code",
                          classmethod(lambda cls, code: tok)):
            _server.KanbanHandler._handle_oauth_graph_callback(handler)
        if state_kind != "signed":
            _server._oauth_graph_states.pop(state, None)
        return handler.redirects, db

    def test_matching_identity_stores_with_the_identity(self):
        redirects, db = self._run("estimates.masterx@outlook.com")
        self.assertEqual(redirects[0][0], "graph_success")
        writes = [w for w in db.writes if "oauth_graph_access_token = %s" in w[0]]
        self.assertTrue(writes)
        self.assertIn("estimates.masterx@outlook.com", writes[0][1])

    def test_mismatch_is_refused(self):
        redirects, db = self._run("mdm.estimates@outlook.com")
        self.assertEqual(redirects[0][0], "error")
        self.assertIn("Disconnect Graph", redirects[0][1])
        self.assertEqual([w for w in db.writes if "oauth_graph_access_token = %s" in w[0]], [])

    def test_unverifiable_identity_is_refused(self):
        redirects, db = self._run("")
        self.assertEqual(redirects[0][0], "error")
        self.assertEqual([w for w in db.writes if "oauth_graph_access_token = %s" in w[0]], [])

    def test_missing_refresh_token_does_not_wipe_the_stored_one(self):
        redirects, db = self._run("estimates.masterx@outlook.com", refresh_token="")
        self.assertEqual(redirects[0][0], "graph_success")
        write = [w for w in db.writes if "oauth_graph_access_token = %s" in w[0]][0]
        self.assertEqual(write[1][1], "GOLD")

    def test_legacy_in_memory_state_still_completes(self):
        redirects, db = self._run("estimates.masterx@outlook.com", state_kind="legacy")
        self.assertEqual(redirects[0][0], "graph_success")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Source guards (paths that need a live session / browser)
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceGuards(unittest.TestCase):
    def setUp(self):
        self.server_src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.app_src = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.dispatcher_src = (ROOT / "notification_dispatcher.py").read_text(encoding="utf-8")
        self.index_src = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    def test_update_endpoint_does_not_accept_tokens_from_the_client(self):
        at = self.server_src.index("def _handle_mail_account_update")
        region = self.server_src[at:at + 2500]
        self.assertIn("payload.pop(forbidden", region)
        self.assertNotIn('"oauth_access_token", "oauth_refresh_token", "auto_bcc_addr"', region)

    def test_create_endpoint_does_not_accept_tokens_from_the_client(self):
        at = self.server_src.index("def _handle_mail_account_create")
        region = self.server_src[at:at + 2600]
        self.assertNotIn('payload.get("oauth_access_token")', region)
        self.assertNotIn('payload.get("oauth_refresh_token")', region)

    def test_exchange_authorize_accepts_an_account_id_and_sets_login_hint(self):
        at = self.server_src.index("def _handle_oauth_authorize")
        region = self.server_src[at:at + 3600]
        self.assertIn("account_id", region)
        self.assertIn("login_hint=login_hint", region)

    def test_accounts_endpoint_exposes_the_real_identity(self):
        self.assertIn('r["oauthIdentity"] = r.get("oauth_identity")', self.server_src)
        self.assertIn('r["oauthIdentityMismatch"]', self.server_src)

    def test_graph_buttons_are_not_bound_once(self):
        at = self.app_src.index("function updateGraphStatusBlock")
        region = self.app_src[at:at + 6000]
        self.assertNotIn("connectBtn.dataset.bound", region)
        self.assertNotIn("disconnectBtn.dataset.bound", region)
        self.assertIn("connectBtn.onclick", region)
        self.assertIn("accountSettingsAccountId()", region)

    def test_graph_buttons_read_the_account_at_click_time(self):
        at = self.app_src.index("function accountSettingsAccountId")
        region = self.app_src[at:at + 700]
        self.assertIn("#account-settings-id", region)

    def test_imap_connect_sends_the_account_id(self):
        at = self.app_src.index("/api/v2/mail/oauth/authorize?provider=")
        region = self.app_src[at - 700:at + 300]
        self.assertIn("account_id=", region)

    def test_ui_shows_the_connected_identity_not_the_account_email(self):
        at = self.app_src.index("if (authType !== 'password' && oauthStatusEl)")
        region = self.app_src[at:at + 1600]
        self.assertIn("acct.oauthIdentity", region)
        self.assertNotIn("`Connected as ${acct.email}`", region)

    def test_ui_has_a_disconnect_for_the_imap_oauth_tokens(self):
        self.assertIn('id="account-settings-oauth-disconnect"', self.index_src)
        self.assertIn("async function disconnectExchangeOAuth", self.app_src)

    def test_notification_refresh_updates_by_id_not_email(self):
        at = self.dispatcher_src.index("Refresh OAuth token if expired")
        region = self.dispatcher_src[at:at + 1800]
        self.assertNotIn("WHERE email = %s", region)
        self.assertIn("WHERE id = %s", region)

    def test_migration_047_is_additive(self):
        sql = (ROOT / "migrations" / "047_mail_oauth_identity.sql").read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS oauth_identity", sql)
        self.assertNotIn("DROP ", sql.upper())
        self.assertNotIn("DELETE FROM", sql.upper())

    def test_boot_ddl_mirrors_the_migration(self):
        self.assertIn('ADD COLUMN IF NOT EXISTS oauth_identity TEXT', self.server_src)
        self.assertIn('ADD COLUMN IF NOT EXISTS oauth_last_error TEXT', self.server_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
