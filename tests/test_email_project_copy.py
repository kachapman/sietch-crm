#!/usr/bin/env python3
"""Unit tests: a linked email is COPIED into the project, files included.

OnlyOffice parity / live requirement 2026-10-29
------------------------------------------------
Linking an email to a project must MATERIALIZE the email in the project:

  * the email body inline in history_events (so every project user can read
    it without the mail account), and
  * the ATTACHMENTS in the project's own storage, so removing the mail
    account later does not remove access to the files.

Before this, the timeline only REFERENCED the mail attachments: they were
resolved live from `mail_attachments` and served through the mail download
route (gated by mail-account access). Deleting the mail account cascaded
`mail_attachments` away and the project's files vanished — which is what
happened to the Aplus/MDM mail on 2026-10-29.

Run with: python3 tests/test_email_project_copy.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ROOT = REPO_ROOT


class TestProjectCopyHelpers(unittest.TestCase):
    def setUp(self):
        import scanner.mail_scanner as ms
        self.ms = ms

    def test_copy_helper_exists(self):
        self.assertTrue(callable(self.ms._copy_mail_attachments_to_project))

    def test_snapshot_creates_event_then_copies_files(self):
        """The copy must happen AFTER the event exists (it needs event_id).

        `create_email_history_snapshot()` has two paths: a repair path for an
        existing event (copy against the known id, no INSERT) and a create
        path (INSERT, then copy against `RETURNING id`). Only the copy that
        follows the INSERT belongs to the create path — searching for the
        first mention matched the repair path's call and made this assert
        nonsense.
        """
        import inspect
        src = inspect.getsource(self.ms.create_email_history_snapshot)
        insert_at = src.index("INSERT INTO history_events")
        copy_at = src.index("_copy_mail_attachments_to_project", insert_at)
        self.assertLess(insert_at, copy_at,
                        "the event must be created before its files are copied")
        self.assertIn("RETURNING id", src,
                      "the new event id is needed for the file rows")

    def _copy_region(self) -> str:
        """Body of the detailed copy helper (the one that writes the rows).

        `_copy_mail_attachments_to_project` is a thin wrapper that returns the
        detailed report's `copied` count; the INSERTs live in
        `copy_mail_attachments_to_project_detailed`. The region ends at the
        next top-level def so growing the function cannot silently move an
        assertion out of scope (the old fixed 4000-character window did).
        """
        src = Path(ROOT / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")
        at = src.index("def copy_mail_attachments_to_project_detailed")
        rest = src[at + 1:]
        nxt = rest.find("\ndef ")
        return src[at:at + 1 + (len(rest) if nxt == -1 else nxt)]

    def test_copy_writes_history_attachments_row(self):
        region = self._copy_region()
        self.assertIn("INSERT INTO history_attachments", region)
        self.assertIn("history-attachments", region,
                      "files must land in the project's own storage tree")
        self.assertIn("INSERT INTO project_documents", region,
                      "the copy must also be a project document (editor + Documents tab)")

    def test_copy_tolerates_a_failing_file(self):
        """One bad file must not abort the whole copy or the link."""
        region = self._copy_region()
        self.assertIn("except Exception", region)
        self.assertIn('report["skipped"].append', region)

    def test_wrapper_still_returns_the_copied_count(self):
        """Callers use the int return value; it must track the detailed report."""
        src = Path(ROOT / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")
        at = src.index("def _copy_mail_attachments_to_project(")
        region = src[at:src.index("def _find_existing_project_copy", at)]
        self.assertIn("copy_mail_attachments_to_project_detailed(", region)
        self.assertIn('return report["copied"]', region)


class TestCopyBehaviour(unittest.TestCase):
    """Exercise the copy helper against a temp storage root."""

    def test_copies_bytes_into_project_storage(self):
        import scanner.mail_scanner as ms
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "data" / "mail-attachments" / "999"
            src_dir.mkdir(parents=True)
            (src_dir / "1_Invoice.pdf").write_bytes(b"%PDF-1.4 test payload")

            mock_db = MagicMock()
            mock_db.query_dicts.return_value = [{
                "id": 42, "filename": "Invoice.pdf", "mime_type": "application/pdf",
                "size_bytes": 22, "stored_path": "data/mail-attachments/999/1_Invoice.pdf",
            }]
            # Dedupe asks query_one "is this attachment already copied?" — a
            # bare MagicMock answers "yes" to everything, which would skip the
            # copy this test is about. Answer None (nothing copied yet) and
            # report the dedupe indexes as present, so the ON CONFLICT path is
            # the one exercised.
            mock_db.query_one.return_value = None
            # The helper resolves its root from the module's location; point
            # it at the temp dir by faking the module __file__.
            with patch.object(ms, "db", mock_db), \
                 patch.object(ms, "_unique_index_exists", return_value=True), \
                 patch.object(ms, "__file__", str(root / "scanner" / "mail_scanner.py")):
                copied = ms._copy_mail_attachments_to_project(7, 1234, 999)

            self.assertEqual(copied, 1)
            dest = root / "data" / "history-attachments" / "7" / "1234"
            self.assertTrue(dest.is_dir(), "the project copy must live under the project's storage")
            files = list(dest.glob("*"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"%PDF-1.4 test payload")

            inserts = [c.args[0] for c in mock_db.execute.call_args_list
                       if c.args and isinstance(c.args[0], str)]
            self.assertTrue(any("history_attachments" in s for s in inserts))
            self.assertTrue(any("project_documents" in s for s in inserts))


class TestTimelinePrefersProjectCopy(unittest.TestCase):
    def test_read_path_falls_back_only_when_no_project_copy(self):
        """The live-mail fallback must not add duplicate entries when the
        project already holds a copy of the file."""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        at = src.index("def _handle_project_history_get")
        region = src[at:at + 12000]
        self.assertIn("if _ev[\"attachments\"]:", region,
                      "the fallback must be skipped when a project copy exists")
        self.assertIn("mail/messages/{_mid}/attachments", region,
                      "legacy snapshots still resolve live mail attachments")


class TestSourceGuards(unittest.TestCase):
    def test_history_events_have_no_fk_to_mail_messages(self):
        """The email body must survive the mail account being deleted —
        that works because history_events.content is a self-contained
        snapshot with no FK to mail_messages."""
        init = (ROOT / "init.sql").read_text(encoding="utf-8")
        at = init.index("CREATE TABLE history_events")
        region = init[at:at + 1200]
        self.assertNotIn("mail_messages", region,
                         "history_events must not depend on mail_messages")


if __name__ == "__main__":
    unittest.main(verbosity=1)
