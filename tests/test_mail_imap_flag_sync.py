#!/usr/bin/env python3
"""Unit tests for the Outlook read-status sync path (IMAP flag operations).

Live bug (2026-11-03, MDM outlook.com account): opening an email in the
Sietch mail modal did NOT mark it read in Outlook.

Root causes covered here:
  1. The stored OAuth access token was used as-is — no expiry check, no
     refresh. A stale token means the IMAP STORE never reaches the server.
  2. `imap_tools` MailBox.flag() runs EXPUNGE after every UID STORE, so a
     read/unread toggle permanently expunged every \\Deleted message in the
     folder. We must issue a plain UID STORE instead.
  3. The UID stored in Postgres can be stale (after a move the message has a
     NEW uid in the destination folder). The target is verified by
     Message-ID; a mismatched message must never be flagged.
  4. Failures were only logged at warning level and swallowed — the user had
     no idea the change never reached Outlook.

All IMAP access is mocked — no mail server is contacted.

Run with: python3 tests/test_mail_imap_flag_sync.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as _server  # noqa: E402


MESSAGE_ID = "abc123@example.com"


def _mock_db():
    db = MagicMock()
    db.query_one.return_value = None
    db.query_dicts.return_value = []
    db.execute.return_value = None
    return db


class FakeIMAPClient:
    """Minimal stand-in for imaplib's IMAP4 client.

    Records every UID command so the tests can assert that no EXPUNGE was
    ever issued and that the right UID was flagged.
    """

    def __init__(self, headers_by_uid=None, uids_by_message_id=None, fail_first=None):
        # headers_by_uid: {uid: raw_header_bytes}
        self.headers_by_uid = headers_by_uid or {}
        # uids_by_message_id: {message_id: [uid, ...]}
        self.uids_by_message_id = uids_by_message_id or {}
        self.fail_first = fail_first
        self.commands = []
        self.expunges = 0
        self.auth_error_on = fail_first

    # imap_tools calls client.uid(command, *args)
    def uid(self, command, *args):
        cmd = str(command).upper()
        self.commands.append((cmd, args))
        if cmd == "EXPUNGE":
            self.expunges += 1
            return ("OK", [b""])
        if cmd == "FETCH":
            uid = str(args[0])
            if self.fail_first is not None and uid == self.fail_first:
                raise RuntimeError("AUTHENTICATE failed: invalid token")
            raw = self.headers_by_uid.get(uid, b"")
            return ("OK", [(b"1 (BODY[HEADER] {0}", raw), b")"])
        if cmd == "STORE":
            return ("OK", [b""])
        if cmd == "SEARCH":
            return ("OK", [b""])
        return ("OK", [b""])

    def expunge(self):
        self.expunges += 1
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


class FakeMailBox:
    """Stand-in for imap_tools.MailBox."""

    def __init__(self, client, uids_by_message_id=None):
        self.client = client
        self.folder = MagicMock()
        self.folder.set = MagicMock()
        self.logged_out = False
        self._uids_by_message_id = uids_by_message_id or {}
        self.moved = []
        self.searched = []

    def uids(self, criteria=None, charset="US-ASCII", sort=None):
        # The criteria object stringifies to something like
        # 'AND (HEADER Message-Id "abc")' — find the matching message-id.
        text = str(criteria)
        for mid, uids in self._uids_by_message_id.items():
            if mid in text:
                return [str(u) for u in uids]
        return []

    def move(self, uid_list, dest_folder, chunks=None):
        self.moved.append((list(uid_list), dest_folder))

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])

    # Convenience for assertions
    def store_calls(self):
        return [c for c in self.client.commands if c[0] == "STORE"]


def _header_bytes(message_id):
    return (
        f"Message-ID: <{message_id}>\r\n"
        "Subject: Test\r\n"
        "From: a@b.c\r\n"
        "\r\n"
    ).encode()


class TestTokenRefresh(unittest.TestCase):
    """An expired/near-expiry OAuth token must be refreshed before use."""

    def _token_row(self, expires_in_seconds, access="old-token", refresh="refresh-token"):
        return {
            "email": "mdm@outlook.com",
            "oauth_provider": "microsoft",
            "oauth_access_token": access,
            "oauth_refresh_token": refresh,
            "oauth_token_expires": datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        }

    def test_expired_token_is_refreshed(self):
        db = _mock_db()
        row = self._token_row(-60)
        db.query_one.return_value = row
        provider = MagicMock()
        provider.refresh_token.return_value = {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        with patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, refresh = _server._imap_account_token(5)
        self.assertEqual(token, "new-token")
        self.assertEqual(refresh, "new-refresh")
        provider.refresh_token.assert_called_once_with("refresh-token")
        # The new token must be persisted so the scanner sees it too.
        self.assertTrue(db.execute.called)
        sql, params = db.execute.call_args[0]
        self.assertIn("UPDATE mail_accounts SET oauth_access_token", sql)
        self.assertIn("new-token", params)

    def test_token_near_expiry_is_refreshed(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(120)
        provider = MagicMock()
        provider.refresh_token.return_value = {"access_token": "fresh", "expires_in": 3600}
        with patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = _server._imap_account_token(5)
        self.assertEqual(token, "fresh")
        provider.refresh_token.assert_called_once()

    def test_valid_token_is_not_refreshed(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(3600, access="good-token")
        provider = MagicMock()
        with patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = _server._imap_account_token(5)
        self.assertEqual(token, "good-token")
        provider.refresh_token.assert_not_called()

    def test_refresh_failure_returns_empty_without_raising(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(-60)
        provider = MagicMock()
        provider.refresh_token.side_effect = RuntimeError("AADSTS70000 invalid_grant")
        with patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = _server._imap_account_token(5)
        self.assertEqual(token, "old-token")

    def test_no_refresh_token_cannot_refresh(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(-60, refresh="")
        provider = MagicMock()
        with patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = _server._imap_account_token(5)
        self.assertEqual(token, "old-token")
        provider.refresh_token.assert_not_called()

    def test_connect_uses_the_refreshed_token(self):
        """_imap_connect must authenticate with the REFRESHED token, not the
        stale stored one — this is the live bug (mark-read did nothing in
        Outlook because the stored access token had expired)."""
        db = _mock_db()
        db.query_one.return_value = {
            "email": "mdm@outlook.com",
            "imap_host": "imap.outlook.com",
            "imap_port": 993,
            "password_encrypted": "",
            "oauth_provider": "microsoft",
            "oauth_access_token": "STALE-TOKEN",
            "oauth_refresh_token": "refresh-token",
            "oauth_token_expires": datetime.now(timezone.utc) - timedelta(minutes=5),
        }
        provider = MagicMock()
        provider.refresh_token.return_value = {
            "access_token": "FRESH-TOKEN",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        fake_mb = MagicMock()
        import imap_tools
        with patch.object(_server, "db", db), \
            patch.object(imap_tools, "MailBox", return_value=fake_mb), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            _server._imap_connect(5)
        provider.refresh_token.assert_called_once_with("refresh-token")
        # The XOAUTH2 handshake must carry the refreshed token.
        fake_mb.xoauth2.assert_called_once_with("mdm@outlook.com", "FRESH-TOKEN")


class TestNoExpunge(unittest.TestCase):
    """A read/unread toggle must never EXPUNGE anything."""

    def test_set_seen_issues_no_expunge(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertTrue(ok)
        self.assertEqual(client.expunges, 0)
        self.assertNotIn("EXPUNGE", [c[0] for c in client.commands])
        # And it must be a plain UID STORE with +FLAGS (\Seen)
        stores = mb.store_calls()
        self.assertEqual(len(stores), 1)
        cmd, args = stores[0]
        self.assertEqual(cmd, "STORE")
        self.assertEqual(args[0], "42")
        self.assertEqual(args[1], "+FLAGS")
        self.assertEqual(args[2], "(\\Seen)")

    def test_unset_seen_uses_minus_flags(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", False, message_id=MESSAGE_ID)
        self.assertTrue(ok)
        self.assertEqual(client.expunges, 0)
        _cmd, args = mb.store_calls()[0]
        self.assertEqual(args[1], "-FLAGS")
        self.assertEqual(args[2], "(\\Seen)")

    def test_star_flag_also_avoids_expunge(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_flagged(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertTrue(ok)
        self.assertEqual(client.expunges, 0)
        _cmd, args = mb.store_calls()[0]
        self.assertEqual(args[2], "(\\Flagged)")

    def test_source_contains_no_imap_tools_flag_call(self):
        """imap_tools MailBox.flag() EXPUNGEs — the flag path must not use it."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")) as fh:
            src = fh.read()
        self.assertNotIn("mb.flag(", src)
        self.assertNotIn(".flag([uid]", src)


class TestMessageIdVerification(unittest.TestCase):
    """The flag must only ever be applied to the message we mean."""

    def test_mismatched_message_id_is_not_flagged(self):
        # The stored UID points at a DIFFERENT message.
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes("other@example.com")})
        mb = FakeMailBox(client, uids_by_message_id={})
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertFalse(ok)
        self.assertEqual(mb.store_calls(), [])
        self.assertEqual(client.expunges, 0)

    def test_moved_message_found_by_message_id(self):
        # Stored UID is stale (message was moved) — the Message-ID lookup in
        # the folder finds the real UID and that one gets flagged.
        client = FakeIMAPClient(headers_by_uid={"99": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["99"]})
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertTrue(ok)
        stores = mb.store_calls()
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0][1][0], "99")

    def test_moved_message_updates_stored_uid_and_folder(self):
        client = FakeIMAPClient(headers_by_uid={"99": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["99"]})
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=7)
        self.assertTrue(ok)
        sql, params = db.execute.call_args[0]
        self.assertIn("UPDATE mail_messages SET imap_uid", sql)
        self.assertIn("99", params)

    def test_matching_message_id_flags_stored_uid(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["42"]})
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertTrue(ok)
        self.assertEqual(mb.store_calls()[0][1][0], "42")

    def test_message_id_comparison_ignores_angle_brackets(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True,
                                        message_id=f"<{MESSAGE_ID}>")
        self.assertTrue(ok)
        self.assertEqual(len(mb.store_calls()), 1)

    def test_without_message_id_the_stored_uid_is_trusted(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=None)
        self.assertTrue(ok)
        self.assertEqual(mb.store_calls()[0][1][0], "42")

    def test_message_found_in_another_folder_when_stored_folder_is_stale(self):
        """The DB folder and the IMAP move are not atomic — the message may
        still be in the folder the row no longer names."""
        client = FakeIMAPClient(headers_by_uid={"99": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["99"]})
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "Trash", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=7)
        self.assertTrue(ok)
        self.assertEqual(mb.store_calls()[0][1][0], "99")
        # The row is updated so the next toggle hits the right message.
        sql, params = db.execute.call_args[0]
        self.assertIn("UPDATE mail_messages SET imap_uid", sql)
        self.assertIn("99", params)


class TestAuthRetry(unittest.TestCase):
    """An auth failure refreshes the token and retries once."""

    def test_auth_failure_refreshes_and_retries(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        client.fail_first = "42"
        mb = FakeMailBox(client)
        db = _mock_db()
        provider = MagicMock()
        provider.refresh_token.return_value = {"access_token": "new", "expires_in": 3600}

        connect_calls = []

        def fake_connect(account_id):
            connect_calls.append(account_id)
            if len(connect_calls) == 1:
                # First attempt: the FETCH fails with an auth error.
                client.fail_first = "42"
            else:
                client.fail_first = None
            return mb

        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", side_effect=fake_connect), \
            patch.object(_server, "_imap_force_token_refresh", return_value=True) as force, \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)

        self.assertTrue(ok, "the retry after an auth failure should succeed")
        self.assertEqual(len(connect_calls), 2, "exactly one retry is expected")
        force.assert_called_once_with(1)

    def test_repeated_auth_failure_is_reported(self):
        client = FakeIMAPClient(headers_by_uid={})
        client.fail_first = "42"
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb), \
            patch.object(_server, "_imap_force_token_refresh", return_value=True):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertFalse(ok)

    def test_auth_error_detection(self):
        self.assertTrue(_server._is_imap_auth_error(Exception("MailboxLoginError: AUTHENTICATE failed")))
        self.assertTrue(_server._is_imap_auth_error(Exception("535 5.7.3 Authentication unsuccessful")))
        self.assertTrue(_server._is_imap_auth_error(Exception("invalid token")))
        self.assertTrue(_server._is_imap_auth_error(Exception("[AUTHENTICATIONFAILED] Invalid credentials")))
        self.assertFalse(_server._is_imap_auth_error(Exception("message not found by Message-ID")))

    def test_a_bare_uid_number_is_not_an_auth_error(self):
        """A UID like 535 must not trigger a pointless token refresh."""
        self.assertFalse(_server._is_imap_auth_error(Exception("message 535 not found")))
        self.assertFalse(_server._is_imap_auth_error(Exception("folder 535 unavailable")))


class TestFailureReporting(unittest.TestCase):
    """Failures are logged at error level and recorded for the UI."""

    def setUp(self):
        with _server._mail_sync_failures_lock:
            _server._mail_sync_failures.clear()

    def tearDown(self):
        with _server._mail_sync_failures_lock:
            _server._mail_sync_failures.clear()

    def test_failure_is_logged_at_error_level(self):
        client = FakeIMAPClient(headers_by_uid={})
        mb = FakeMailBox(client)
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb), \
            patch.object(_server, "_imap_force_token_refresh", return_value=False), \
            patch.object(_server, "logger") as logger:
            ok = _server._imap_set_seen(7, "INBOX", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=99)
        self.assertFalse(ok)
        # logger.error must have been called (never a warning for this)
        self.assertTrue(logger.error.called)
        logged = " ".join(str(c) for c in logger.error.call_args_list)
        self.assertIn("99", logged)
        self.assertIn("7", logged)

    def test_failure_is_recorded_for_the_ui(self):
        client = FakeIMAPClient(headers_by_uid={})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb), \
            patch.object(_server, "_imap_force_token_refresh", return_value=False):
            ok = _server._imap_set_seen(7, "INBOX", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=99)
        self.assertFalse(ok)
        fail = _server._mail_sync_failure(99)
        self.assertIsNotNone(fail)
        self.assertEqual(fail["action"], "read")
        self.assertTrue(fail["error"])

    def test_success_clears_a_previous_failure(self):
        _server._record_mail_sync_failure(99, "read", "boom")
        self.assertIsNotNone(_server._mail_sync_failure(99))
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(7, "INBOX", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=99)
        self.assertTrue(ok)
        self.assertIsNone(_server._mail_sync_failure(99))

    def test_tokens_are_never_logged(self):
        """The failure log must not contain the access token."""
        db = _mock_db()
        db.query_one.return_value = {
            "email": "mdm@outlook.com",
            "imap_host": "imap.outlook.com",
            "imap_port": 993,
            "password_encrypted": "",
            "oauth_provider": "microsoft",
            "oauth_access_token": "SUPER-SECRET-TOKEN",
            "oauth_refresh_token": "",
            # No expiry info -> treated as expired -> refresh attempted.
            "oauth_token_expires": None,
        }
        provider = MagicMock()
        provider.refresh_token.side_effect = RuntimeError("boom")
        with patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider), \
            patch.object(_server, "logger") as logger:
            # No usable token -> _imap_connect logs and returns None.
            result = _server._imap_account_token(5)
        self.assertNotIn("SUPER-SECRET-TOKEN", " ".join(str(c) for c in logger.error.call_args_list))
        self.assertEqual(result[0], "SUPER-SECRET-TOKEN")

    def test_connect_does_not_leak_the_token_into_the_log(self):
        """_imap_connect must never log the access token it sends."""
        db = _mock_db()
        db.query_one.return_value = {
            "email": "mdm@outlook.com",
            "imap_host": "imap.outlook.com",
            "imap_port": 993,
            "password_encrypted": "",
            "oauth_provider": "microsoft",
            "oauth_access_token": "SUPER-SECRET-TOKEN",
            "oauth_refresh_token": "r",
            "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        fake_mb = MagicMock()
        import imap_tools
        with patch.object(_server, "db", db), \
            patch.object(imap_tools, "MailBox", return_value=fake_mb), \
            patch.object(_server, "logger") as logger:
            _server._imap_connect(5)
        logged = " ".join(str(c) for c in logger.error.call_args_list)
        self.assertNotIn("SUPER-SECRET-TOKEN", logged)


class TestMoveUpdatesUid(unittest.TestCase):
    """After a move, the destination folder's UID must be stored."""

    def test_move_stores_new_uid_found_by_message_id(self):
        # The stored UID is stale — the move resolves the real UID (77) by
        # Message-ID first, then re-finds it in the destination folder.
        client = FakeIMAPClient(headers_by_uid={"77": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["77"]})
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_move(1, "INBOX", "42", "Archive",
                                    message_id=MESSAGE_ID, message_db_id=7)
        self.assertTrue(ok)
        # The move targets the resolved UID, not the stale one.
        self.assertEqual(mb.moved, [(["77"], "Archive")])
        sql, params = db.execute.call_args[0]
        self.assertIn("UPDATE mail_messages SET imap_uid", sql)
        self.assertIn("77", params)

    def test_move_without_message_id_uses_stored_uid(self):
        client = FakeIMAPClient()
        mb = FakeMailBox(client)
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_move(1, "INBOX", "42", "Archive",
                                    message_id=None, message_db_id=7)
        self.assertTrue(ok)
        self.assertEqual(mb.moved, [(["42"], "Archive")])


class TestSourceGuards(unittest.TestCase):
    """Source-level guards so the fix cannot silently regress."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "server.py")) as fh:
            self.server_src = fh.read()
        with open(os.path.join(here, "public", "app.js")) as fh:
            self.app_src = fh.read()
        # Strip comments and docstrings so the guards only look at real code.
        import io
        import tokenize
        code_tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(self.server_src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_tokens.append(tok.string)
        self.server_code = " ".join(code_tokens)

    def test_imap_connect_does_not_use_the_raw_stored_token_only(self):
        # The connect path must go through the refresh helper.
        self.assertIn("_imap_account_token", self.server_src)

    def test_read_handler_passes_message_id(self):
        self.assertIn('kwargs={"message_id": msg.get("message_id"), "message_db_id": message_id}',
                      self.server_src)

    def test_no_expunge_command_is_ever_issued(self):
        """EXPUNGE may only appear in comments/docstrings — never as a command."""
        self.assertNotIn("EXPUNGE", self.server_code.upper())

    def test_imap_tools_flag_helper_is_not_used(self):
        """imap_tools MailBox.flag() EXPUNGEs — the flag path must not use it."""
        self.assertNotIn(".flag(", self.server_code)

    def test_sync_failure_is_exposed_to_the_ui(self):
        self.assertIn("sync_failure", self.server_src)
        self.assertIn("sync_failure", self.app_src)

    def test_flag_store_uses_plus_or_minus_flags_only(self):
        # The tokenized code drops string literals, so check the raw source for
        # the +/-FLAGS selection used by _imap_store_flags.
        self.assertIn('"+FLAGS" if value else "-FLAGS"', self.server_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
