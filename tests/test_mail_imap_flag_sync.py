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

The IMAP logic lives in mail_imap.py and the OAuth refresh in mail_oauth.py;
server.py holds thin wrappers. Both are tested here.

All IMAP access is mocked — no mail server is contacted.

Run with: python3 tests/test_mail_imap_flag_sync.py
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mail_imap  # noqa: E402
import mail_oauth  # noqa: E402
import server as _server  # noqa: E402


MESSAGE_ID = "abc123@example.com"
OTHER_MESSAGE_ID = "other@example.com"


def _mock_db():
    db = MagicMock()
    db.query_one.return_value = None
    db.query_dicts.return_value = []
    db.execute.return_value = 1
    return db


def _find_execute(db, needle):
    """Return (sql, params) of the db.execute() call whose SQL contains needle.

    The sync path issues several statements (the mail_messages update and the
    mail_flag_queue bookkeeping), so the specific one has to be picked out.
    """
    for call in db.execute.call_args_list:
        sql, params = call[0][0], call[0][1]
        if needle in sql:
            return sql, params
    raise AssertionError(f"no db.execute() call contained {needle!r}; "
                         f"calls: {[c[0][0] for c in db.execute.call_args_list]}")


class FakeIMAPClient:
    """Minimal stand-in for imaplib's IMAP4 client.

    Records every UID command so the tests can assert that no EXPUNGE was
    ever issued and that the right UID was flagged.
    """

    def __init__(self, headers_by_uid=None, fail_on=None, fail_first_uid=None):
        # headers_by_uid: {uid: raw_header_bytes}
        self.headers_by_uid = headers_by_uid or {}
        self.fail_on = fail_on
        self.fail_first_uid = fail_first_uid
        self.commands = []
        self.expunges = 0

    # imap_tools calls client.uid(command, *args)
    def uid(self, command, *args):
        cmd = str(command).upper()
        self.commands.append((cmd, args))
        if cmd == "EXPUNGE":
            self.expunges += 1
            return ("OK", [b""])
        if cmd == "FETCH":
            uid = str(args[0])
            if self.fail_first_uid is not None and uid == self.fail_first_uid:
                self.fail_first_uid = None
                raise RuntimeError("MailboxLoginError: AUTHENTICATE failed")
            if self.fail_on is not None and uid == self.fail_on:
                raise RuntimeError("MailboxLoginError: AUTHENTICATE failed")
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


class FakeFolderInfo:
    """Stand-in for imap_tools' FolderInfo (name + special-use flags)."""

    def __init__(self, name, flags=()):
        self.name = name
        self.delim = "/"
        self.flags = tuple(flags)


class FakeFolderManager:
    """Stand-in for imap_tools' folder manager."""

    def __init__(self, folders=None, list_error=False):
        self.selected = []
        self._folders = folders or []
        self._list_error = list_error
        self.listed = 0

    def set(self, folder):
        self.selected.append(folder)

    def list(self):
        self.listed += 1
        if self._list_error:
            raise RuntimeError("LIST failed")
        out = []
        for item in self._folders:
            if isinstance(item, FakeFolderInfo):
                out.append(item)
            elif isinstance(item, tuple):
                out.append(FakeFolderInfo(item[0], item[1]))
            else:
                out.append(FakeFolderInfo(item))
        return out


class FakeMailBox:
    """Stand-in for imap_tools.MailBox."""

    def __init__(self, client, uids_by_message_id=None, folders=None, list_error=False):
        self.client = client
        self.folder = FakeFolderManager(folders, list_error=list_error)
        self.logged_out = False
        self._uids_by_message_id = uids_by_message_id or {}
        self.moved = []

    def uids(self, criteria=None, charset="US-ASCII", sort=None):
        # The criteria object stringifies to something like
        # '(HEADER "Message-Id" "abc123@example.com")' — find the matching id.
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

    def search_calls(self):
        return [c for c in self.client.commands if c[0] == "SEARCH"]


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
            "oauth_identity": "mdm@outlook.com",
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
            "email": "mdm@outlook.com",
        }
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server, "db", db), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider), \
            patch.object(mail_oauth, "_save_refreshed_token", return_value=True):
            token, refresh = mail_oauth.get_valid_access_token(5)
        self.assertEqual(token, "new-token")
        self.assertEqual(refresh, "new-refresh")
        provider.refresh_token.assert_called_once_with("refresh-token")

    def test_refresh_is_persisted_with_compare_and_swap(self):
        """The save must be guarded on the token that was read, so a
        concurrent refresh (scanner vs server) cannot be overwritten."""
        db = _mock_db()
        db.query_one.return_value = self._token_row(-60, access="old-token")
        provider = MagicMock()
        provider.refresh_token.return_value = {"access_token": "new-token", "expires_in": 3600, "email": "mdm@outlook.com"}
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            mail_oauth.refresh_access_token(
                5, "microsoft", "refresh-token", None,
                expected_access="old-token",
            )
        sql, params = db.execute.call_args[0]
        self.assertIn("oauth_access_token = %s", sql)
        self.assertIn("AND oauth_access_token = %s", sql)
        self.assertIn("old-token", params)

    def test_concurrent_refresh_does_not_overwrite_the_newer_token(self):
        """Two refreshers read the same old token; only the first save wins.

        The second one's compare-and-swap matches no row (the row now holds
        the first one's token), so the newer token survives.
        """
        db = _mock_db()
        # The row still holds the OLD token when both refreshers read it.
        db.query_one.return_value = self._token_row(-60, access="old-token")
        provider = MagicMock()
        provider.refresh_token.side_effect = [
            {"access_token": "token-A", "expires_in": 3600, "email": "mdm@outlook.com"},
            {"access_token": "token-B", "expires_in": 3600, "email": "mdm@outlook.com"},
        ]
        # The CAS succeeds only while the row holds "old-token".
        stored = {"token": "old-token"}

        def fake_execute(sql, params=None):
            if "AND oauth_access_token = %s" in sql:
                if stored["token"] != params[-1]:
                    return 0  # row moved on — swap fails
                stored["token"] = params[0]
                return 1
            stored["token"] = params[0]
            return 1

        db.execute.side_effect = fake_execute

        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            first = mail_oauth.refresh_access_token(
                5, "microsoft", "refresh-token", None, expected_access="old-token")
            second = mail_oauth.refresh_access_token(
                5, "microsoft", "refresh-token", None, expected_access="old-token")

        self.assertIsNotNone(first)
        self.assertEqual(first[0], "token-A")
        # The second refresh must NOT clobber the token the first one stored.
        self.assertIsNone(second)
        self.assertEqual(stored["token"], "token-A")

    def test_concurrent_refresh_from_two_threads_keeps_one_token(self):
        """Two threads refreshing the same account concurrently must not
        produce two different stored tokens."""
        db = _mock_db()
        db.query_one.return_value = self._token_row(-60, access="old-token")
        provider = MagicMock()
        counter = {"n": 0}

        def fake_refresh(_refresh_token):
            counter["n"] += 1
            return {"access_token": f"token-{counter['n']}", "expires_in": 3600, "email": "mdm@outlook.com"}

        provider.refresh_token.side_effect = fake_refresh
        stored = {"token": "old-token"}

        def fake_execute(sql, params=None):
            if "AND oauth_access_token = %s" in sql:
                if stored["token"] != params[-1]:
                    return 0
                stored["token"] = params[0]
                return 1
            stored["token"] = params[0]
            return 1

        db.execute.side_effect = fake_execute
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(mail_oauth.refresh_access_token(
                5, "microsoft", "refresh-token", None, expected_access="old-token"))

        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        winners = [r for r in results if r]
        self.assertEqual(len(winners), 1, "exactly one refresh may win the compare-and-swap")
        self.assertEqual(stored["token"], winners[0][0])

    def test_the_forced_refresh_also_uses_the_compare_and_swap(self):
        """The post-auth-failure refresh must not overwrite a token another
        refresher stored in the meantime."""
        db = _mock_db()
        db.query_one.return_value = {
            "email": "mdm@outlook.com",
            "oauth_provider": "microsoft",
            "oauth_access_token": "old-token",
            "oauth_refresh_token": "refresh-token",
            "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        provider = MagicMock()
        provider.refresh_token.return_value = {"access_token": "forced-token", "expires_in": 3600, "email": "mdm@outlook.com"}
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            mail_oauth.force_refresh_access_token(5)
        sql, params = _find_execute(db, "AND oauth_access_token = %s")
        self.assertIn("old-token", params)

    def test_the_forced_refresh_keeps_the_newer_token_when_it_loses(self):
        """If the compare-and-swap fails, the token another refresher stored
        is used — not the stale one."""
        db = _mock_db()
        db.query_one.side_effect = [
            {  # the initial read
                "email": "mdm@outlook.com",
                "oauth_identity": "mdm@outlook.com",
                "oauth_provider": "microsoft",
                "oauth_access_token": "old-token",
                "oauth_refresh_token": "refresh-token",
                "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            {  # the identity check
                "email": "mdm@outlook.com",
                "oauth_identity": "mdm@outlook.com",
            },
            {  # the re-read after losing the race
                "oauth_access_token": "newer-token",
                "oauth_refresh_token": "refresh-token",
            },
        ]
        provider = MagicMock()
        provider.refresh_token.return_value = {"access_token": "forced-token", "expires_in": 3600, "email": "mdm@outlook.com"}
        db.execute.return_value = 0  # the compare-and-swap matches no row
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            out = mail_oauth.force_refresh_access_token(5)
        self.assertEqual(out[0], "newer-token")

    def test_the_scanner_re_reads_the_token_when_it_loses_the_race(self):
        """The scanner must not keep using the stale token it read before
        the refresh attempt."""
        import scanner.mail_scanner as ms
        db = _mock_db()
        db.query_dicts.return_value = [{
            "id": 5,
            "email": "mdm@outlook.com",
            "imap_host": "imap.outlook.com",
            "imap_port": 993,
            "password_encrypted": "x",
            "oauth_provider": "microsoft",
            "oauth_access_token": "stale-token",
            "oauth_refresh_token": "refresh-token",
            "oauth_token_expires": datetime.now(timezone.utc) - timedelta(minutes=5),
            "sync_enabled": True,
            "monitored_folders": "",
            "retention_days": 0,
        }]
        # The row now holds a newer token (another refresher won).
        db.query_one.return_value = {"oauth_access_token": "newer-token"}
        with patch.object(ms, "db", db), \
            patch.object(ms.mail_oauth, "refresh_access_token", return_value=None):
            mailboxes = ms._get_mailboxes()
        self.assertEqual(len(mailboxes), 1)
        self.assertEqual(mailboxes[0]["oauth_token"], "newer-token")

    def test_token_near_expiry_is_refreshed(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(120)
        provider = MagicMock()
        provider.refresh_token.return_value = {"access_token": "fresh", "expires_in": 3600, "email": "mdm@outlook.com"}
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider), \
            patch.object(mail_oauth, "_save_refreshed_token", return_value=True):
            token, _refresh = mail_oauth.get_valid_access_token(5)
        self.assertEqual(token, "fresh")
        provider.refresh_token.assert_called_once()

    def test_valid_token_is_not_refreshed(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(3600, access="good-token")
        provider = MagicMock()
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = mail_oauth.get_valid_access_token(5)
        self.assertEqual(token, "good-token")
        provider.refresh_token.assert_not_called()

    def test_refresh_failure_returns_empty_without_raising(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(-60)
        provider = MagicMock()
        provider.refresh_token.side_effect = RuntimeError("AADSTS70000 invalid_grant")
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = mail_oauth.get_valid_access_token(5)
        self.assertEqual(token, "old-token")

    def test_no_refresh_token_cannot_refresh(self):
        db = _mock_db()
        db.query_one.return_value = self._token_row(-60, refresh="")
        provider = MagicMock()
        with patch.object(mail_oauth, "db", db, create=True), \
            patch.object(_server.oauth_providers, "get_provider", return_value=provider):
            token, _refresh = mail_oauth.get_valid_access_token(5)
        self.assertEqual(token, "old-token")
        provider.refresh_token.assert_not_called()

    def test_scanner_uses_the_same_shared_helper(self):
        """The scanner must not own a second copy of the refresh logic."""
        import scanner.mail_scanner as ms
        with patch.object(ms.mail_oauth, "refresh_access_token", return_value=("t", "r")) as shared:
            out = ms._refresh_oauth_token_if_needed(5, "microsoft", "refresh-token", None)
        self.assertEqual(out, ("t", "r"))
        shared.assert_called_once()


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
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fname in ("server.py", "mail_imap.py"):
            with open(os.path.join(here, fname)) as fh:
                src = fh.read()
            self.assertNotIn("mb.flag(", src, fname)
            self.assertNotIn(".flag([uid]", src, fname)


class TestMessageIdVerification(unittest.TestCase):
    """The flag must only ever be applied to the message we mean."""

    def test_mismatched_message_id_is_not_flagged(self):
        # The stored UID points at a DIFFERENT message.
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(OTHER_MESSAGE_ID)})
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
        sql, params = _find_execute(db, "UPDATE mail_messages SET imap_uid")
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

    def test_no_stored_message_id_is_not_flagged(self):
        """Without a Message-ID the target cannot be verified — skip it and
        record a sync failure rather than flagging blind."""
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb), \
            patch.object(mail_imap, "logger") as logger:
            ok = _server._imap_set_seen(1, "INBOX", "42", True,
                                        message_id=None, message_db_id=7)
        self.assertFalse(ok)
        self.assertEqual(mb.store_calls(), [])
        logged = " ".join(str(c) for c in logger.error.call_args_list)
        self.assertIn("Message-ID", logged)

    def test_no_stored_message_id_records_a_sync_failure(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        try:
            with patch.object(_server, "db", _mock_db()), \
                patch.object(_server, "_imap_connect", return_value=mb):
                ok = _server._imap_set_seen(1, "INBOX", "42", True,
                                            message_id=None, message_db_id=77)
            self.assertFalse(ok)
            fail = _server._mail_sync_failure(77)
            self.assertIsNotNone(fail)
            self.assertEqual(fail["action"], "read")
        finally:
            _server._clear_mail_sync_failure(77)

    def test_search_substring_match_is_rejected(self):
        """IMAP `SEARCH HEADER Message-Id` is a SUBSTRING match: a hit whose
        Message-ID is only a superstring must not be used."""
        # The search returns uid 42, but its real Message-ID is longer.
        client = FakeIMAPClient(
            headers_by_uid={"42": _header_bytes(f"{MESSAGE_ID}.extra")},
        )
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["42"]})
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertFalse(ok)
        self.assertEqual(mb.store_calls(), [])

    def test_search_exact_match_is_accepted(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={MESSAGE_ID: ["42"]})
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)
        self.assertTrue(ok)
        self.assertEqual(len(mb.store_calls()), 1)


class TestFolderDiscovery(unittest.TestCase):
    """Only the stored folder, INBOX and the special-use folders are searched.

    A big mailbox can have hundreds of ordinary user folders; searching each
    of them costs a SELECT + SEARCH round trip and slows the UI down when
    the message is genuinely gone.
    """

    def setUp(self):
        mail_imap.clear_folder_cache()

    def tearDown(self):
        mail_imap.clear_folder_cache()

    def test_only_special_use_folders_are_candidates(self):
        client = FakeIMAPClient()
        mb = FakeMailBox(client, folders=[
            FakeFolderInfo("INBOX", ("\\HasNoChildren",)),
            FakeFolderInfo("Sent Items", ("\\HasNoChildren", "\\Sent")),
            FakeFolderInfo("Deleted Items", ("\\HasNoChildren", "\\Trash")),
            FakeFolderInfo("Junk Email", ("\\HasNoChildren", "\\Junk")),
            FakeFolderInfo("Projects", ("\\HasNoChildren",)),
            FakeFolderInfo("Clients", ("\\HasNoChildren",)),
        ])
        folders = mail_imap.account_folders(1, mb)
        self.assertIn("Sent Items", folders)
        self.assertIn("Deleted Items", folders)
        self.assertIn("Junk Email", folders)
        # Ordinary user folders must NOT be searched.
        self.assertNotIn("Projects", folders)
        self.assertNotIn("Clients", folders)
        self.assertNotIn("INBOX", folders)

    def test_a_big_mailbox_does_not_get_all_its_folders_searched(self):
        """200 ordinary folders + 2 special-use ones -> only 2 are candidates."""
        folders = [FakeFolderInfo(f"Folder {i}", ("\\HasNoChildren",)) for i in range(200)]
        folders.append(FakeFolderInfo("Sent Items", ("\\Sent",)))
        folders.append(FakeFolderInfo("Deleted Items", ("\\Trash",)))
        client = FakeIMAPClient()
        mb = FakeMailBox(client, folders=folders)
        candidates = mail_imap.account_folders(1, mb)
        self.assertEqual(len(candidates), 2)
        self.assertIn("Sent Items", candidates)
        self.assertIn("Deleted Items", candidates)

    def test_the_search_actually_visits_only_a_few_folders(self):
        """End-to-end: a stale UID must not cause every folder to be searched."""
        headers = {"99": _header_bytes(MESSAGE_ID)}
        folders = [FakeFolderInfo(f"Folder {i}", ("\\HasNoChildren",)) for i in range(50)]
        folders.append(FakeFolderInfo("Sent Items", ("\\Sent",)))
        client = FakeIMAPClient(headers_by_uid=headers)
        mb = FakeMailBox(
            client,
            uids_by_message_id={MESSAGE_ID: ["99"]},
            folders=folders,
        )
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "Trash", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=7)
        self.assertTrue(ok)
        # Only the stored folder, INBOX and the special-use folders were
        # selected — never the 50 ordinary ones.
        searched = set(mb.folder.selected)
        self.assertNotIn("Folder 0", searched)
        self.assertNotIn("Folder 25", searched)
        self.assertLessEqual(len(searched), 4)

    def test_the_static_fallback_is_used_when_list_fails(self):
        client = FakeIMAPClient()
        mb = FakeMailBox(client, list_error=True)
        folders = mail_imap.account_folders(1, mb)
        for name in ("Sent Items", "Deleted Items", "Junk Email", "INBOX"):
            self.assertIn(name, folders)

    def test_the_static_fallback_is_used_when_no_folder_has_a_special_use_flag(self):
        client = FakeIMAPClient()
        mb = FakeMailBox(client, folders=[
            FakeFolderInfo("INBOX", ("\\HasNoChildren",)),
            FakeFolderInfo("Projects", ("\\HasNoChildren",)),
        ])
        folders = mail_imap.account_folders(1, mb)
        # No special-use flags -> the well-known names are used instead.
        self.assertIn("Sent Items", folders)
        self.assertIn("Deleted Items", folders)
        self.assertIn("Junk Email", folders)

    def test_static_fallback_includes_outlook_names(self):
        folders = mail_imap.list_folders(FakeMailBox(FakeIMAPClient()))
        for name in ("Sent Items", "Deleted Items", "Junk Email", "INBOX"):
            self.assertIn(name, folders)

    def test_folder_list_is_cached_per_account(self):
        client = FakeIMAPClient()
        mb = FakeMailBox(client, folders=[FakeFolderInfo("Sent Items", ("\\Sent",))])
        mail_imap.account_folders(1, mb)
        # A second call must not re-LIST.
        mb2 = FakeMailBox(client, folders=[])
        folders = mail_imap.account_folders(1, mb2)
        self.assertIn("Sent Items", folders)
        self.assertEqual(mb2.folder.listed, 0)

    def test_outlook_folders_are_searched_when_the_stored_folder_is_stale(self):
        """A message moved to "Sent Items" must still be found."""
        client = FakeIMAPClient(headers_by_uid={"99": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(
            client,
            uids_by_message_id={MESSAGE_ID: ["99"]},
            folders=[
                FakeFolderInfo("INBOX", ("\\HasNoChildren",)),
                FakeFolderInfo("Sent Items", ("\\Sent",)),
                FakeFolderInfo("Deleted Items", ("\\Trash",)),
                FakeFolderInfo("Junk Email", ("\\Junk",)),
            ],
        )
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_set_seen(1, "Trash", "42", True,
                                        message_id=MESSAGE_ID, message_db_id=7)
        self.assertTrue(ok)
        self.assertEqual(mb.store_calls()[0][1][0], "99")


class TestPermanentDelete(unittest.TestCase):
    """Permanent delete must verify the Message-ID like the move path does."""

    def setUp(self):
        mail_imap.clear_sync_failure(7)

    def tearDown(self):
        mail_imap.clear_sync_failure(7)

    def test_delete_verifies_the_message_id(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        deleted = []
        mb.delete = lambda uids, chunks=None: deleted.append(list(uids))
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_delete(1, "INBOX", "42", message_id=MESSAGE_ID)
        self.assertTrue(ok)
        self.assertEqual(deleted, [["42"]])

    def test_delete_aborts_when_the_message_id_does_not_match(self):
        """A stale UID may belong to a different message — never delete it."""
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(OTHER_MESSAGE_ID)})
        mb = FakeMailBox(client)
        deleted = []
        mb.delete = lambda uids, chunks=None: deleted.append(list(uids))
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_delete(1, "INBOX", "42", message_id=MESSAGE_ID)
        self.assertFalse(ok)
        self.assertEqual(deleted, [], "no message may be deleted when the target is unresolved")

    def test_delete_aborts_without_a_message_id(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        deleted = []
        mb.delete = lambda uids, chunks=None: deleted.append(list(uids))
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_delete(1, "INBOX", "42", message_id=None)
        self.assertFalse(ok)
        self.assertEqual(deleted, [])

    def test_delete_logs_a_warning_when_it_cannot_resolve(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(OTHER_MESSAGE_ID)})
        mb = FakeMailBox(client)
        mb.delete = lambda uids, chunks=None: None
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb), \
            patch.object(mail_imap, "logger") as logger:
            _server._imap_delete(1, "INBOX", "42", message_id=MESSAGE_ID)
        logged = " ".join(str(c) for c in logger.error.call_args_list)
        self.assertIn("delete aborted", logged)


class TestAuthRetry(unittest.TestCase):
    """An auth failure refreshes the token and retries once."""

    def test_auth_failure_refreshes_and_retries(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        client.fail_first_uid = "42"
        mb = FakeMailBox(client)
        db = _mock_db()
        connect_calls = []

        def fake_connect(account_id):
            connect_calls.append(account_id)
            return mb

        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", side_effect=fake_connect), \
            patch.object(_server, "_imap_force_token_refresh", return_value=True) as force:
            ok = _server._imap_set_seen(1, "INBOX", "42", True, message_id=MESSAGE_ID)

        self.assertTrue(ok, "the retry after an auth failure should succeed")
        self.assertEqual(len(connect_calls), 2, "exactly one retry is expected")
        force.assert_called_once_with(1)

    def test_repeated_auth_failure_is_reported(self):
        client = FakeIMAPClient(headers_by_uid={})
        client.fail_on = "42"
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

    def test_the_bare_word_authenticate_is_not_an_auth_error(self):
        """"authenticate" alone is too broad — e.g. a server that does not
        support the mechanism is not a credential problem."""
        self.assertFalse(_server._is_imap_auth_error(
            Exception("server does not support AUTHENTICATE")))
        self.assertFalse(_server._is_imap_auth_error(
            Exception("AUTHENTICATE capability not available")))


class TestFailureReporting(unittest.TestCase):
    """Failures are logged at error level and recorded for the UI."""

    def setUp(self):
        mail_imap.clear_sync_failure(99)

    def tearDown(self):
        mail_imap.clear_sync_failure(99)

    def test_failure_is_logged_at_error_level(self):
        client = FakeIMAPClient(headers_by_uid={})
        mb = FakeMailBox(client)
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb), \
            patch.object(_server, "_imap_force_token_refresh", return_value=False), \
            patch.object(mail_imap, "logger") as logger:
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
            "oauth_refresh_token": "r",
            "oauth_token_expires": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        fake_mb = MagicMock()
        import imap_tools
        with patch.object(_server, "db", db), \
            patch.object(mail_oauth, "db", db, create=True), \
            patch.object(imap_tools, "MailBox", return_value=fake_mb), \
            patch.object(mail_imap, "logger") as logger:
            _server._imap_connect(5)
        logged = " ".join(str(c) for c in logger.error.call_args_list)
        self.assertNotIn("SUPER-SECRET-TOKEN", logged)


class TestMoveUpdatesUid(unittest.TestCase):
    """After a move, the destination folder's UID must be stored."""

    def setUp(self):
        mail_imap.clear_sync_failure(7)

    def tearDown(self):
        mail_imap.clear_sync_failure(7)

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
        sql, params = _find_execute(db, "UPDATE mail_messages SET imap_uid")
        self.assertIn("77", params)

    def test_move_without_message_id_does_not_move_a_stale_uid(self):
        """Without a Message-ID the target cannot be verified — abort."""
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_move(1, "INBOX", "42", "Archive",
                                    message_id=None, message_db_id=7)
        self.assertFalse(ok)
        self.assertEqual(mb.moved, [])

    def test_move_without_message_id_records_a_sync_failure(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(MESSAGE_ID)})
        mb = FakeMailBox(client)
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            _server._imap_move(1, "INBOX", "42", "Archive",
                               message_id=None, message_db_id=7)
        fail = _server._mail_sync_failure(7)
        self.assertIsNotNone(fail)
        self.assertEqual(fail["action"], "move")

    def test_unresolved_move_is_aborted(self):
        """When the Message-ID cannot be resolved the stale UID must NOT be
        moved — it may already belong to a different message."""
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(OTHER_MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={})
        db = _mock_db()
        with patch.object(_server, "db", db), \
            patch.object(_server, "_imap_connect", return_value=mb):
            ok = _server._imap_move(1, "INBOX", "42", "Archive",
                                    message_id=MESSAGE_ID, message_db_id=7)
        self.assertFalse(ok)
        self.assertEqual(mb.moved, [], "no message may be moved when the target is unresolved")

    def test_unresolved_move_records_a_sync_failure(self):
        client = FakeIMAPClient(headers_by_uid={"42": _header_bytes(OTHER_MESSAGE_ID)})
        mb = FakeMailBox(client, uids_by_message_id={})
        with patch.object(_server, "db", _mock_db()), \
            patch.object(_server, "_imap_connect", return_value=mb):
            _server._imap_move(1, "INBOX", "42", "Archive",
                               message_id=MESSAGE_ID, message_db_id=7)
        fail = _server._mail_sync_failure(7)
        self.assertIsNotNone(fail)
        self.assertEqual(fail["action"], "move")


class TestFailurePersistence(unittest.TestCase):
    """The sync-failure marker is persisted in the existing mail_flag_queue
    table (pushed = FALSE) so it survives a restart. No migration needed."""

    def test_failure_is_persisted_in_mail_flag_queue(self):
        db = _mock_db()
        mail_imap.record_sync_failure(55, "read", "boom", db)
        inserts = [c for c in db.execute.call_args_list if "INSERT INTO mail_flag_queue" in c[0][0]]
        self.assertEqual(len(inserts), 1)
        sql, params = inserts[0][0]
        self.assertIn("pushed", sql)
        self.assertIn("FALSE", sql)
        self.assertIn(55, params)
        self.assertIn("\\Seen", params)

    def test_success_clears_the_persisted_row(self):
        db = _mock_db()
        mail_imap.clear_sync_failure(56, "read", db)
        deletes = [c for c in db.execute.call_args_list if "DELETE FROM mail_flag_queue" in c[0][0]]
        self.assertEqual(len(deletes), 1)
        self.assertIn("pushed = FALSE", deletes[0][0][0])

    def test_persisted_failure_is_read_back(self):
        db = _mock_db()
        db.query_one.return_value = {
            "flag_name": "\\Seen",
            "flag_value": True,
            "queued_at": datetime(2026, 11, 3, 10, 0, 0),
        }
        marker = mail_imap.persisted_failure(57, db)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["action"], "read")
        self.assertTrue(marker["persisted"])

    def test_persisted_failure_unread_maps_to_unread(self):
        db = _mock_db()
        db.query_one.return_value = {
            "flag_name": "\\Seen",
            "flag_value": False,
            "queued_at": datetime(2026, 11, 3, 10, 0, 0),
        }
        marker = mail_imap.persisted_failure(58, db)
        self.assertEqual(marker["action"], "unread")

    def test_no_persisted_row_means_no_marker(self):
        db = _mock_db()
        db.query_one.return_value = None
        self.assertIsNone(mail_imap.persisted_failure(59, db))

    def test_the_server_falls_back_to_the_persisted_marker(self):
        """After a restart the in-memory map is empty but the DB row is not."""
        db = _mock_db()
        db.query_one.return_value = {
            "flag_name": "\\Seen",
            "flag_value": True,
            "queued_at": datetime(2026, 11, 3, 10, 0, 0),
        }
        with patch.object(_server, "db", db):
            marker = _server._mail_sync_failure(60)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["action"], "read")

    def test_the_server_batch_lookup_reads_the_persisted_markers(self):
        db = _mock_db()
        db.query_dicts.return_value = [{
            "message_id": 61,
            "flag_name": "\\Seen",
            "flag_value": True,
            "queued_at": datetime(2026, 11, 3, 10, 0, 0),
        }]
        with patch.object(_server, "db", db):
            markers = _server._mail_sync_failures([61, 62])
        self.assertIn(61, markers)
        self.assertNotIn(62, markers)

    def test_the_queue_table_needs_no_migration(self):
        """mail_flag_queue already exists in the schema — the persistence
        must not require a new migration."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "init.sql")) as fh:
            init_src = fh.read()
        self.assertIn("CREATE TABLE mail_flag_queue", init_src)


class TestSourceGuards(unittest.TestCase):
    """Source-level guards so the fix cannot silently regress."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "server.py")) as fh:
            self.server_src = fh.read()
        with open(os.path.join(here, "mail_imap.py")) as fh:
            self.imap_src = fh.read()
        with open(os.path.join(here, "mail_oauth.py")) as fh:
            self.oauth_src = fh.read()
        with open(os.path.join(here, "public", "app.js")) as fh:
            self.app_src = fh.read()
        # Strip comments and docstrings so the guards only look at real code.
        import io
        import tokenize
        code_tokens = []
        for src in (self.server_src, self.imap_src):
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                code_tokens.append(tok.string)
        self.code = " ".join(code_tokens)

    def test_imap_connect_does_not_use_the_raw_stored_token_only(self):
        # The connect path must go through the refresh helper.
        self.assertIn("_imap_account_token", self.server_src)
        self.assertIn("mail_oauth.get_valid_access_token", self.server_src)
        self.assertIn("def get_valid_access_token", self.oauth_src)

    def test_read_handler_passes_message_id(self):
        self.assertIn('kwargs={"message_id": msg.get("message_id"), "message_db_id": message_id}',
                      self.server_src)

    def test_no_expunge_command_is_ever_issued(self):
        """EXPUNGE may only appear in comments/docstrings — never as a command."""
        self.assertNotIn("EXPUNGE", self.code.upper())

    def test_imap_tools_flag_helper_is_not_used(self):
        """imap_tools MailBox.flag() EXPUNGEs — the flag path must not use it."""
        self.assertNotIn(".flag(", self.code)

    def test_sync_failure_is_exposed_to_the_ui(self):
        self.assertIn("sync_failure", self.server_src)
        self.assertIn("sync_failure", self.app_src)

    def test_token_save_is_guarded_at_the_db_level(self):
        """The refresh save must be a compare-and-swap on the old token."""
        self.assertIn("AND oauth_access_token = %s", self.oauth_src)

    def test_only_the_shared_helper_refreshes_tokens(self):
        """mail_oauth.py is the only owner of the token-refresh logic.

        The Graph token family (oauth_graph_*) is a DIFFERENT token and keeps
        its own refresh — it is not the IMAP access token.
        """
        for src, name in ((self.server_src, "server.py"), (self.imap_src, "mail_imap.py")):
            self.assertNotIn("prov.refresh_token", src, name)
        # The shared helper owns it.
        self.assertIn("prov.refresh_token", self.oauth_src)

    def test_the_scanner_uses_the_shared_helper(self):
        import scanner.mail_scanner as ms
        self.assertTrue(hasattr(ms, "mail_oauth"))
        scanner_src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scanner", "mail_scanner.py")).read()
        self.assertIn("mail_oauth.refresh_access_token", scanner_src)
        # The scanner must not own a second copy of the refresh logic.
        self.assertNotIn("prov.refresh_token", scanner_src)

    def test_the_scanner_dockerfile_copies_the_shared_module(self):
        """The standalone scanner image copies files by name — the shared
        token module must be on that list or the scanner fails to import."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dockerfile = open(os.path.join(here, "scanner", "Dockerfile")).read()
        self.assertIn("mail_oauth.py", dockerfile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
