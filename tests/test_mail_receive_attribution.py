#!/usr/bin/env python3
"""Receive-side attribution tests (2026-11-10, item 3 of the mail fix).

Every path that can file a message under the wrong mail_account or folder:

  1. `_store_message` — the row's account comes from the mailbox config's
     `account_id`, and BOTH the UID lookup and the Message-ID/Sent-copy
     reconciliation are scoped to it (UIDs are only unique per folder, so
     two accounts can legitimately hold UID 4114).
  2. The CRM's own sent copy (`imap_uid = sent:outgoing:*`, always folder
     'Sent') is reconciled against the IMAP copy in ANY sent-folder spelling
     ("Sent Items" on Outlook IMAP), still scoped to one account.
  3. A reconciled (not inserted) message is marked processed, so it is not
     re-classified / re-note-posted on the next poll of the same window.
  4. `_list_imap_folders` logs in through the gated `_login_mailbox`: a
     mailbox holding another account's credential is refused before a
     socket is opened.
  5. Folder rename / delete in the UI relabels only the folder's OWN
     account's messages (it used to relabel every account with that name).
  6. `import_email.py` never copies a source `account_id` / `message_id`
     verbatim: accounts are remapped by email, links by the target message's
     real (account, uid, folder) key, and anything unmapped is skipped.

Offline (no IMAP, stubs only). The two route-handler tests need `server`,
which opens a DB pool at import; they are skipped, with a clear line, when
no database is reachable — the source guard below still runs.

Run with: python3 tests/test_mail_receive_attribution.py
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scanner.mail_scanner as ms  # noqa: E402
import import_email  # noqa: E402

try:
    import server as _server  # noqa: E402
    _SERVER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent (needs a live DB)
    _server = None
    _SERVER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

SCANNER_SRC = (REPO_ROOT / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")
SERVER_SRC = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
IMPORT_SRC = (REPO_ROOT / "import_email.py").read_text(encoding="utf-8")


def _msg(uid="4114", folder="INBOX", message_id="", date="Mon, 26 Oct 2026 21:45:00 +0000"):
    return {
        "uid": uid,
        "message_id": message_id,
        "from": "sender@example.com",
        "to": "customer@example.com",
        "cc": "",
        "subject": "Estimate 600-1336411",
        "body_text": "hello",
        "body_html": "",
        "date": date,
        "folder": folder,
        "attachments": [],
        "raw_headers": "",
        "flags": [],
    }


class TestStoreMessageAccountScope(unittest.TestCase):
    """`_store_message` must key everything on the config's account id."""

    def test_uid_lookup_includes_account_id(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [None, {"id": 77}]
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", MagicMock()):
            got = ms._store_message(_msg(), {"account_id": 9})
        self.assertEqual(got, 77)
        lookup_sql, lookup_params = mock_db.query_one.call_args_list[0].args
        self.assertIn("account_id = %s", lookup_sql)
        self.assertEqual(lookup_params, ("4114", "INBOX", 9))
        _insert_sql, insert_params = mock_db.query_one.call_args_list[1].args
        self.assertEqual(insert_params[0], 9, "row inserted under another account")

    def test_same_uid_two_accounts_do_not_collide(self):
        """Account 10 holding UID 4114 must not be found for account 9."""
        mock_db = MagicMock()
        # No row for THIS account; the other account's row is invisible to the
        # scoped query, so the message is inserted as its own row.
        mock_db.query_one.side_effect = [None, {"id": 900}]
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", MagicMock()):
            got = ms._store_message(_msg(uid="4114"), {"account_id": 9})
        self.assertEqual(got, 900)
        self.assertEqual(mock_db.query_one.call_count, 2)

    def test_message_id_reconciliation_is_account_scoped(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [None, {"id": 55}]
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", MagicMock()):
            got = ms._store_message(_msg(message_id="<abc@example.com>"),
                                   {"account_id": 9})
        self.assertEqual(got, 55)
        sql, params = mock_db.query_one.call_args_list[0].args
        self.assertIn("account_id = %s AND message_id = %s", sql)
        self.assertEqual(params, (9, "<abc@example.com>"))

    def test_env_mailbox_stays_null_account(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [None, {"id": 12}]
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", MagicMock()):
            got = ms._store_message(_msg(), {})
        self.assertEqual(got, 12)
        sql, _params = mock_db.query_one.call_args_list[0].args
        self.assertIn("account_id IS NULL", sql)
        _insert_sql, insert_params = mock_db.query_one.call_args_list[1].args
        self.assertIsNone(insert_params[0])


class TestSentCopyReconciliation(unittest.TestCase):
    """CRM sent copy vs IMAP sent copy — any provider spelling, one account."""

    def test_outlook_sent_items_reconciles_with_crm_copy(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [{"id": 501}]
        mark = MagicMock()
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", mark):
            got = ms._store_message(_msg(folder="Sent Items"), {"account_id": 9})
        self.assertEqual(got, 501, "IMAP Sent Items copy was stored as a second row")
        self.assertEqual(mock_db.query_one.call_count, 1, "an INSERT was attempted")
        sql, params = mock_db.query_one.call_args_list[0].args
        self.assertIn("lower(coalesce(folder, '')) = ANY(%s)", sql)
        self.assertIn("sent:outgoing:", sql)
        self.assertEqual(params[0], 9, "reconciliation is not account-scoped")
        self.assertEqual(set(params[1]),
                         {"sent", "sent items", "sent messages", "sent mail"})

    def test_plain_sent_still_matches(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [{"id": 502}]
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", MagicMock()):
            self.assertEqual(ms._store_message(_msg(folder="Sent"), {"account_id": 6}), 502)

    def test_inbox_never_runs_sent_reconciliation(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [None, {"id": 7}]
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", MagicMock()):
            ms._store_message(_msg(folder="INBOX"), {"account_id": 9})
        sql, _params = mock_db.query_one.call_args_list[0].args
        self.assertNotIn("sent:outgoing:", sql)

    def test_sent_folder_family(self):
        for name in ("Sent", "sent", "Sent Items", "Sent Messages", "Sent Mail", " SENT "):
            self.assertTrue(ms._is_sent_folder(name), name)
        for name in ("INBOX", "Drafts", "Trash", "Archive", "Statements", ""):
            self.assertFalse(ms._is_sent_folder(name), name)
        self.assertFalse(ms._is_sent_folder(None))


class TestReconciledMessagesAreMarkedProcessed(unittest.TestCase):
    """A dedupe hit must not be re-classified on the next poll."""

    def test_sent_copy_hit_marks_processed(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [{"id": 501}]
        mark = MagicMock()
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", mark):
            ms._store_message(_msg(folder="Sent Items"), {"account_id": 9})
        mark.assert_called_once_with(9, "4114", "Sent Items")

    def test_existing_uid_hit_marks_processed(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [{"id": 88}]
        mark = MagicMock()
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", mark):
            ms._store_message(_msg(), {"account_id": 9})
        mark.assert_called_once_with(9, "4114", "INBOX")

    def test_message_id_hit_marks_processed(self):
        mock_db = MagicMock()
        mock_db.query_one.side_effect = [{"id": 55}]
        mark = MagicMock()
        with patch.object(ms, "db", mock_db), \
                patch.object(ms, "_mark_processed", mark):
            ms._store_message(_msg(uid="5000", message_id="<abc@example.com>"),
                              {"account_id": 9})
        mark.assert_called_once_with(9, "5000", "INBOX")


class TestFolderListingGate(unittest.TestCase):
    """`_list_imap_folders` must use the gated login."""

    def test_source_uses_login_mailbox(self):
        start = SCANNER_SRC.index("def _list_imap_folders")
        end = SCANNER_SRC.index("_SYSTEM_FOLDER_NORMALIZE", start)
        region = SCANNER_SRC[start:end]
        self.assertIn("_login_mailbox(mailbox_cfg)", region)
        self.assertNotIn("MailBox(", region)
        self.assertNotIn("xoauth2(", region)
        self.assertNotIn(".login(", region)

    def test_refuses_foreign_login_user(self):
        cfg = {"host": "imap.example.com", "port": 993, "user": "other@example.com",
               "email": "a@example.com", "auth_type": "password", "password": "x"}
        fake_mailbox_cls = MagicMock()
        with patch.object(ms, "MailBox", fake_mailbox_cls):
            with self.assertRaises(ms.MailboxIdentityError):
                ms._list_imap_folders(cfg)
        fake_mailbox_cls.assert_not_called()

    def test_refuses_unverified_xoauth2(self):
        cfg = {"host": "imap.example.com", "port": 993, "user": "a@example.com",
               "email": "a@example.com", "auth_type": "xoauth2",
               "oauth_token": "tok", "identity_state": "unverified"}
        fake_mailbox_cls = MagicMock()
        with patch.object(ms, "MailBox", fake_mailbox_cls):
            with self.assertRaises(ms.MailboxIdentityError):
                ms._list_imap_folders(cfg)
        fake_mailbox_cls.assert_not_called()


class TestImportEmailRemapsIds(unittest.TestCase):
    """The legacy import must never copy an account/message id verbatim."""

    class FakeCursor:
        def __init__(self, conn):
            self.conn = conn
            self.sql = ""
            self.params = ()

        def execute(self, sql, params=()):
            self.sql = " ".join(sql.split())
            self.params = params

        def fetchall(self):
            return list(self.conn.rows_for(self.sql))

    class FakeConn:
        def __init__(self, tables):
            self.tables = tables
            self.inserts = []

        def cursor(self):
            return TestImportEmailRemapsIds.FakeCursor(self)

        def commit(self):
            pass

        def rows_for(self, sql):
            if "FROM mail_accounts" in sql:
                key = "accounts"
            elif "FROM mail_messages" in sql:
                key = "messages"
            elif "FROM mail_deal_links" in sql:
                key = "links"
            else:
                return []
            return self.tables.get(key, [])

    def _inserts_for(self, conn, table):
        return [i for i in conn.inserts if i[0].startswith("INSERT INTO " + table)]

    def _patch_cursor_inserts(self, conn):
        """Record INSERTs instead of executing them."""
        original = TestImportEmailRemapsIds.FakeCursor.execute

        def execute(self, sql, params=()):
            original(self, sql, params)
            flat = " ".join(sql.split())
            if flat.startswith("INSERT"):
                conn.inserts.append((flat, params))

        return patch.object(TestImportEmailRemapsIds.FakeCursor, "execute", execute)

    def test_accounts_are_remapped_by_email(self):
        # Source id 3 == target id 11 (same address); source id 4 has no target.
        src = self.FakeConn({"accounts": [(3, "a@example.com"), (4, "gone@example.com")]})
        tgt = self.FakeConn({"accounts": [(11, "a@example.com")]})
        msg = (3, "101", "<m1@x>", None, "s@x.com", "c@y.com", "",
               "subj", "body", "", None, False, False, False, "INBOX", None)
        unmapped = (4, "202", "<m2@x>", None, "s@x.com", "c@y.com", "",
                    "subj2", "body", "", None, False, False, False, "INBOX", None)
        src.tables["messages"] = [msg, unmapped]
        with self._patch_cursor_inserts(tgt):
            imported = import_email.import_mail_messages(src, tgt)
        self.assertEqual(imported, 1, "an unmapped message was imported anyway")
        inserts = self._inserts_for(tgt, "mail_messages")
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], 11, "source account id copied verbatim")

    def test_deal_links_point_at_the_same_email(self):
        src = self.FakeConn({
            "accounts": [(3, "a@example.com")],
            "messages": [(70, 3, "101", "INBOX"), (71, 3, "202", "INBOX")],
            "links": [(70, 555, 1, None), (999, 556, 1, None)],
        })
        tgt = self.FakeConn({
            "accounts": [(11, "a@example.com")],
            "messages": [(9001, 11, "101", "INBOX")],
            "links": [],
        })
        with self._patch_cursor_inserts(tgt):
            imported = import_email.import_mail_deal_links(src, tgt)
        self.assertEqual(imported, 1, "a link with no target message was imported")
        inserts = self._inserts_for(tgt, "mail_deal_links")
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], 9001, "link points at the wrong message row")
        self.assertEqual(inserts[0][1][1], 555)

    def test_source_has_no_verbatim_account_id_copy(self):
        self.assertIn("load_account_id_map", IMPORT_SRC)
        self.assertIn("load_message_id_map", IMPORT_SRC)
        start = IMPORT_SRC.index("def import_mail_messages")
        end = IMPORT_SRC.index("def load_message_id_map")
        region = IMPORT_SRC[start:end]
        self.assertNotIn("row[0], row[1]", region)
        self.assertIn("target_account_id", region)


class TestFolderRenameIsAccountScoped(unittest.TestCase):
    """Renaming/deleting a folder must not touch other accounts' same-name folder."""

    @classmethod
    def setUpClass(cls):
        if _server is None:
            raise unittest.SkipTest(f"server import skipped: {_SERVER_IMPORT_ERROR}")

    class Shim:
        path = "/api/v2/mail/folders/3"

        def __init__(self, body=b"{}"):
            self.headers = {"Content-Length": str(len(body))}
            self.rfile = MagicMock()
            self.rfile.read.return_value = body

        @staticmethod
        def send_response(code):
            pass

        @staticmethod
        def send_header(k, v):
            pass

        @staticmethod
        def end_headers():
            pass

        wfile = MagicMock()

    def _run(self, handler, folder_row, body=b"{}"):
        responses = []
        mock_db = MagicMock()
        mock_db.query_one.return_value = folder_row
        mock_db.execute.return_value = 1
        shim = self.Shim(body)
        with patch.object(_server, "_require_auth", lambda h: {"id": 1, "is_admin": True}), \
                patch.object(_server, "_json_response",
                             lambda h, s, b: responses.append((s, b))), \
                patch.object(_server, "db", mock_db), \
                patch.object(_server, "logger"):
            handler(shim, 3)
        self.assertTrue(responses, "no response")
        return responses[0], mock_db

    def test_rename_scoped_to_folder_account(self):
        status, mock_db = self._run(
            _server.KanbanHandler._handle_mail_folder_update,
            {"id": 3, "name": "Statements", "is_system": False, "imap_account_id": 10},
            body=b'{"name": "Invoices"}')
        self.assertEqual(status[0], 200)
        first = mock_db.execute.call_args_list[0].args
        self.assertIn("WHERE folder = %s AND account_id = %s", first[0])
        self.assertEqual(first[1], ("Invoices", "Statements", 10))

    def test_delete_scoped_to_folder_account(self):
        status, mock_db = self._run(
            _server.KanbanHandler._handle_mail_folder_delete,
            {"id": 3, "name": "Statements", "is_system": False, "imap_account_id": 10})
        self.assertEqual(status[0], 200)
        first = mock_db.execute.call_args_list[0].args
        self.assertIn("WHERE folder = %s AND account_id = %s", first[0])
        self.assertEqual(first[1], ("Statements", 10))

    def test_unbound_folder_relabels_nothing(self):
        status, mock_db = self._run(
            _server.KanbanHandler._handle_mail_folder_delete,
            {"id": 3, "name": "Notes", "is_system": False, "imap_account_id": None})
        self.assertEqual(status[0], 200)
        for call in mock_db.execute.call_args_list:
            self.assertNotIn("UPDATE mail_messages", call.args[0])

    def test_source_guards(self):
        self.assertNotIn(
            '"UPDATE mail_messages SET folder = %s WHERE folder = %s" (name, folder["name"]))',
            SERVER_SRC)
        self.assertNotIn(
            '"UPDATE mail_messages SET folder = \'INBOX\' WHERE folder = %s"',
            SERVER_SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
