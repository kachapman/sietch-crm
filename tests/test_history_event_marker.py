#!/usr/bin/env python3
"""Tests for the server-side auto-log marker on project history rows.

Live bug (Ken, 2026-11-10 8:27 PM ET): "Follow-up Due Date changed from
2026-11-11 to 2026-11-12" rendered as a full card with a Reply button under a
"Quick Context" chip. On production the row (event 50504) had
`category_id = NULL`: the field-change writer looks up the synthetic "Action"
category, whose seed row never landed, so the client classifier saw no category
and fell through to "human note".

The fix is `history_events.is_system_event`, set by every auto-log writer and
returned by the history API. `history_event_marker.py` holds the shared,
DB-free predicate the import writers use for rows restored from another CRM.

Run with: python3 tests/test_history_event_marker.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import network_guard  # noqa: E402

network_guard.install()

from history_event_marker import (  # noqa: E402
    AUTO_LOG_EVENT_TITLES,
    FIELD_CHANGE_LABELS,
    is_auto_history_event,
    looks_like_field_change,
)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


SERVER = _read("server.py")
INIT = _read("init.sql")
MIGRATION = _read("migrations/053_history_events_system_marker.sql")
SCANNER = _read("scanner/mail_scanner.py")
SYNC_OPP = _read("sync_opp_details.py")
CSV_IMPORT = _read("import_csv_export.py")
JSON_IMPORT = _read("import_json_export.py")
MIGRATE = _read("migrate_from_onlyoffice.py")


def _block(src: str, start_marker: str, end_marker: str) -> str:
    at = src.index(start_marker)
    end = src.index(end_marker, at)
    return src[at:end]


class TestFieldChangePredicate(unittest.TestCase):
    def test_every_tracked_field_shape_matches(self):
        self.assertEqual(
            set(FIELD_CHANGE_LABELS),
            {"Stage", "Value", "Contact", "Responsible",
             "Follow-up Due Date", "Private"},
        )
        for label in FIELD_CHANGE_LABELS:
            with self.subTest(label=label):
                self.assertTrue(looks_like_field_change(
                    f"{label} changed from old to new"))
                self.assertTrue(looks_like_field_change(f"{label} set to new"))
                self.assertTrue(looks_like_field_change(f"{label} cleared"))

    def test_ken_row_matches(self):
        self.assertTrue(looks_like_field_change(
            "Follow-up Due Date changed from 2026-11-11 to 2026-11-12"))

    def test_human_prose_does_not_match(self):
        for text in (
            "This was set to closing tasks when its supposed to be in closed paid",
            "the date wat set to 10/5 in sietch, the old crm doesn't have either",
            "<p>Stage changed from Estimate to Won</p>",
            "Follow-up Due Date changed from 2026-11-11 to 2026-11-12\nand more",
            "Note cleared",
            "",
            None,
        ):
            with self.subTest(text=text):
                self.assertFalse(looks_like_field_change(text))

    def test_content_with_html_or_newline_is_not_the_logger(self):
        self.assertFalse(looks_like_field_change(
            "Stage changed from A to B<br/>extra"))


class TestAutoHistoryEventClassifier(unittest.TestCase):
    def test_action_category_is_auto(self):
        self.assertTrue(is_auto_history_event(category_title="Action"))

    def test_system_category_is_auto(self):
        self.assertTrue(is_auto_history_event(
            category_title="Files are uploaded", category_is_system=True))
        self.assertTrue(is_auto_history_event(
            category_title="Task is closed", category_is_system=True))

    def test_mail_message_is_never_auto(self):
        self.assertFalse(is_auto_history_event(
            category_title="Mail Message", category_is_system=True,
            content='{"subject": "hi"}'))

    def test_human_category_is_not_auto(self):
        for title in ("Note", "Quick Context", "Comment", "Customer Update"):
            with self.subTest(title=title):
                self.assertFalse(is_auto_history_event(
                    category_title=title, category_is_system=False,
                    content="called the adjuster"))

    def test_generated_document_titles_are_auto(self):
        for title in AUTO_LOG_EVENT_TITLES:
            with self.subTest(title=title):
                self.assertTrue(is_auto_history_event(
                    category_title="Quick Context", title=title))

    def test_field_change_content_is_auto_even_under_human_category(self):
        self.assertTrue(is_auto_history_event(
            category_title="Note", category_is_system=False,
            content="Stage changed from Estimate to Won"))

    def test_empty(self):
        self.assertFalse(is_auto_history_event())


class TestSchemaAndMigration(unittest.TestCase):
    def test_init_sql_has_the_marker(self):
        block = _block(INIT, "CREATE TABLE history_events (",
                       "CREATE TABLE history_replies")
        self.assertIn("is_system_event BOOLEAN NOT NULL DEFAULT FALSE", block)

    def test_init_sql_seeds_the_action_category(self):
        self.assertIn(
            "VALUES ('Action', '#6c757d', TRUE, 15, 'pencil', 'accent')", INIT)

    def test_init_sql_stamps_bundled_migrations(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS _applied_migrations", INIT)
        files = sorted(p.name for p in (ROOT / "migrations").glob("0*.sql"))
        self.assertTrue(files, "expected bundled migrations")
        for name in files:
            with self.subTest(name=name):
                self.assertIn(f"('{name}')", INIT)

    def test_migration_is_additive_and_idempotent(self):
        self.assertIn("ADD COLUMN IF NOT EXISTS is_system_event BOOLEAN NOT NULL DEFAULT FALSE",
                      MIGRATION)
        self.assertNotIn("DELETE FROM", MIGRATION)
        self.assertNotIn("DROP COLUMN", MIGRATION)

    def test_migration_backfills_only_durable_signals(self):
        self.assertIn("category_id IS NULL", MIGRATION)
        self.assertIn("lower(btrim(title)) = 'action'", MIGRATION)
        for label in FIELD_CHANGE_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, MIGRATION)
        for title in AUTO_LOG_EVENT_TITLES:
            with self.subTest(title=title):
                self.assertIn(title, MIGRATION)
        # The predicate must be anchored: a bare '% changed from %' would
        # compact human notes that merely mention a change.
        self.assertNotIn("LIKE '% changed from", MIGRATION)
        self.assertIn("^(", MIGRATION)
        self.assertIn(")$", MIGRATION)

    def test_migration_records_the_prod_counts(self):
        self.assertIn("405", MIGRATION)


class TestAutoLogWritersSetTheMarker(unittest.TestCase):
    def test_field_change_logger_marks_rows(self):
        block = _block(SERVER, "def _handle_project_update(self, opp_id: int)",
                       "def _handle_project_delete(self, opp_id: int)")
        self.assertIn("is_system_event", block)
        self.assertIn("NOW(), TRUE) RETURNING id", block)
        # still files under Action when it exists, but never depends on it
        self.assertIn("SELECT id FROM history_categories WHERE title = 'Action'", block)

    def test_generated_document_writers_mark_rows(self):
        for title in AUTO_LOG_EVENT_TITLES:
            with self.subTest(title=title):
                at = SERVER.index(f'"{title}"')
                block = SERVER[max(0, at - 400):at + 200]
                self.assertIn("is_system_event", block, title)
                self.assertIn("TRUE", block, title)
        self.assertEqual(
            SERVER.count("VALUES (%s, %s, %s, %s, %s, TRUE)"), 3,
            "invoice, COC and photo-report inserts must all pass the marker")

    def test_action_category_helper_returns_none_not_minus_one(self):
        block = _block(SERVER, "def _resolve_action_category_id()",
                       "def _doc_pdf_safe(")
        self.assertIn("return None", block)
        self.assertNotIn("return -1", block)

    def test_human_note_writers_do_not_mark(self):
        project_note = _block(SERVER, "def _handle_project_history_create(",
                              "def _handle_history_event_delete(")
        self.assertNotIn("is_system_event", project_note)
        contact_note = _block(SERVER, "def _handle_contact_history_create(",
                              "def _handle_contact_photo_get(")
        self.assertNotIn("is_system_event", contact_note)
        bot_note = _block(SERVER, 'if api_path == "/api/bot/note" and method == "POST"',
                          "# Process notifyUserList (same as web endpoint)")
        self.assertNotIn("is_system_event", bot_note)

    def test_email_writers_do_not_mark(self):
        self.assertNotIn("is_system_event", SCANNER)
        email_import = _block(SERVER, "INSERT INTO history_events (opportunity_id, category_id, content, created_by, created_at, backdated_created_at)",
                              "RETURNING id")
        self.assertNotIn("is_system_event", email_import)

    def test_importers_use_the_shared_predicate(self):
        for name, src in (
            ("server.py", SERVER),
            ("sync_opp_details.py", SYNC_OPP),
            ("import_csv_export.py", CSV_IMPORT),
            ("import_json_export.py", JSON_IMPORT),
            ("migrate_from_onlyoffice.py", MIGRATE),
        ):
            with self.subTest(file=name):
                self.assertIn("is_auto_history_event(", src, name)
                self.assertIn("is_system_event", src, name)


class TestHistoryApiReturnsTheMarker(unittest.TestCase):
    def test_project_history_get_selects_and_returns_marker(self):
        block = _block(SERVER, "def _handle_project_history_get(",
                       "def _handle_project_history_create(")
        self.assertIn("h.is_system_event", block)
        self.assertIn('"isSystemEvent": bool(r[9])', block)

    def test_contact_history_get_selects_and_returns_marker(self):
        block = _block(SERVER, "def _handle_contact_history_get(",
                       "def _handle_contact_history_create(")
        self.assertIn("he.is_system_event", block)
        self.assertIn('"isSystemEvent": bool(r[10])', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
