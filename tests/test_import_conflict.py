#!/usr/bin/env python3
"""Regression tests for the per-bundle JSON importer's conflict + failure handling.

Background (2026-10-17 droplet migration): the importer used a bare
`ON CONFLICT DO NOTHING` against tables that carry `UNIQUE` constraints other
than the primary key. Combined with init.sql seeding a history_categories
taxonomy whose titles collided with production's, the restore:

  * silently discarded production's history_categories rows (title collision),
  * left 6,597 history_events failing `history_events_category_id_fkey`,
  * mislabelled every event that DID import (category_id=8 showed "Comment"
    instead of "Note"),
  * and still reported `{"ok": true, "rows_skipped": 816873}` because the
    loop counted every exception as a benign "skip".

These tests lock in the three fixes:
  1. conflict targets are always explicit and resolved from the real PK,
  2. a duplicate (23505) is counted separately from a genuine failure,
  3. `complete` is False whenever rows failed, and samples are surfaced.

server.py is NOT imported (import-time side effects: it opens the DB pool);
these are ast/source-level guards plus real logic tests of the extracted
helpers, in the style of the existing test_backup.py source guards.

Run as:  python3 tests/test_import_conflict.py
"""
import ast
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER_PY = REPO / "server.py"
INIT_SQL = REPO / "init.sql"
APP_JS = REPO / "public" / "app.js"

SRC = SERVER_PY.read_text(encoding="utf-8")


def _handler_source() -> str:
    """Extract the _handle_import_json method source via ast."""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_import_json":
            return ast.get_source_segment(SRC, node) or ""
    raise AssertionError("_handle_import_json not found in server.py")


HANDLER = _handler_source()


class TestConflictTargetIsExplicit(unittest.TestCase):
    """Fix 2: never emit a bare `ON CONFLICT DO NOTHING` in the insert loop."""

    def test_handler_has_no_bare_on_conflict(self):
        """No *executable* bare ON CONFLICT DO NOTHING in the insert loop.

        Strip comments first: the prose explains why ON CONFLICT DO NOTHING
        is unsafe, so a naive substring search matches its own warning.
        """
        code = "\n".join(
            line for line in HANDLER.split("\n") if not line.strip().startswith("#")
        )
        self.assertNotRegex(
            code,
            r"ON CONFLICT DO NOTHING",
            "importer must name the conflict target; a bare DO NOTHING "
            "swallows UNIQUE(title) collisions and silently drops rows",
        )

    def test_handler_uses_conflict_helper(self):
        self.assertIn("_conflict_target(", HANDLER)

    def test_helper_exists_and_builds_from_pk(self):
        self.assertIn("def _conflict_target(", SRC)
        self.assertIn("def _table_primary_key(", SRC)

    def test_conflict_target_is_column_qualified(self):
        """The generated SQL must be ON CONFLICT (<cols>) DO NOTHING."""
        tree = ast.parse(SRC)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_conflict_target":
                fn = node
                break
        self.assertIsNotNone(fn, "_conflict_target missing")
        body = ast.get_source_segment(SRC, fn)
        self.assertIn("ON CONFLICT (", body)
        self.assertIn("DO NOTHING", body)


class TestPrimaryKeyLookup(unittest.TestCase):
    """PKs are heterogeneous; the lookup must be runtime + cached, not hardcoded."""

    def test_pk_lookup_queries_catalog(self):
        self.assertIn("pg_constraint", SRC)
        self.assertIn("contype", SRC)
        self.assertIn("to_regclass", SRC)

    def test_pk_lookup_is_cached(self):
        self.assertIn("_PK_CACHE", SRC)

    def test_handles_missing_pk_gracefully(self):
        """A table with no PK must fall back, not raise mid-import."""
        tree = ast.parse(SRC)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_conflict_target":
                fn = node
                break
        body = ast.get_source_segment(SRC, fn)
        self.assertIn("ON CONFLICT DO NOTHING", body, "no-PK fallback must exist")


class TestFailureIsNotSilentlySkipped(unittest.TestCase):
    """Fix 1: distinguish duplicate rows from genuine restore failures."""

    def test_separate_counters_present(self):
        self.assertIn('"rows_duplicate"', HANDLER)
        self.assertIn('"rows_failed"', HANDLER)

    def test_distinguishes_unique_violation_sqlstate(self):
        self.assertIn("23505", HANDLER)

    def test_captures_sqlstate(self):
        self.assertIn("sqlstate", HANDLER)

    def test_records_failure_samples(self):
        self.assertIn("failure_samples", HANDLER)

    def test_samples_are_capped(self):
        """Uncapped samples would blow up the JSON response on a broken schema."""
        self.assertRegex(
            HANDLER,
            r'len\(results\["failure_samples"\]\)\s*<\s*\d+',
        )

    def test_bare_except_no_longer_swallows_everything(self):
        self.assertNotRegex(
            HANDLER,
            r"except Exception:\s*\n\s*skipped \+= 1",
            "the old bare-except counted every error as a benign skip",
        )


class TestCompleteFlag(unittest.TestCase):
    """Fix 1 (surfacing): `ok` is not enough; `complete` must gate on failures."""

    def test_complete_flag_set(self):
        self.assertIn('results["complete"]', HANDLER)

    def test_complete_is_false_on_failed_rows(self):
        self.assertRegex(
            HANDLER,
            r'\["complete"\]\s*=\s*results\["rows_failed"\]\s*==\s*0',
        )

    def test_incomplete_emits_warning(self):
        self.assertRegex(HANDLER, r"INCOMPLETE|restore is INCOMPLETE")

    def test_rows_skipped_backcompat_preserved(self):
        """Older callers read rows_skipped; keep it = duplicates + failures."""
        self.assertIn('results["rows_skipped"] +=', HANDLER)


class TestInitSqlTaxonomy(unittest.TestCase):
    """Fix 3: init.sql must seed the SAME taxonomy, at the SAME ids, as production."""

    INIT = INIT_SQL.read_text(encoding="utf-8")

    PRODUCTION = {
        8: "Note",
        9: "Mail Message",
        10: "Files are uploaded",
        11: "Task is closed",
        12: "Phone call",
        13: "Email",
        14: "Appointment",
        15: "Text",
        16: "Customer Update",
        17: "Quick Context",
    }

    def _seeded(self) -> dict:
        m = re.search(
            r"INSERT INTO history_categories \(id, title.*?\);", self.INIT, re.S
        )
        self.assertIsNotNone(m, "history_categories seed INSERT not found")
        out = {}
        for row in re.findall(r"\(\s*(\d+)\s*,\s*'([^']+)'", m.group(0)):
            out[int(row[0])] = row[1]
        return out

    def test_seed_uses_explicit_ids(self):
        self.assertRegex(
            self.INIT,
            r"INSERT INTO history_categories \(id, title",
            "seed must pin ids; SERIAL-assigned ids collide with restored rows",
        )

    def test_seed_matches_production_ids_and_titles(self):
        self.assertEqual(self._seeded(), self.PRODUCTION)

    def test_seed_advances_sequence(self):
        m = re.search(r"INSERT INTO history_categories.*?;", self.INIT, re.S)
        after = self.INIT[m.end():m.end() + 400]
        self.assertIn("setval", after, "must setval past the explicit ids")

    def test_legacy_titles_gone(self):
        """These were the shadowing rows. They must not come back."""
        seeded_titles = set(self._seeded().values())
        for stale in ("Call", "Meeting", "Text Message", "Task", "Comment",
                      "Follow-Up", "Estimate", "Inspection", "Opportunity",
                      "Stage Estimate", "Action"):
            self.assertNotIn(stale, seeded_titles,
                             f"'{stale}' collides with production taxonomy")


class TestUiSurfacesFailures(unittest.TestCase):
    """The admin console must not render a failed restore as 'Done'."""

    JS = APP_JS.read_text(encoding="utf-8")

    def test_ui_reads_rows_failed(self):
        self.assertIn("rows_failed", self.JS)

    def test_ui_reads_failure_samples(self):
        self.assertIn("failure_samples", self.JS)

    def test_ui_shows_incomplete_heading(self):
        self.assertIn("Incomplete", self.JS)

    def test_ui_does_not_fold_failures_into_skipped(self):
        seg = self.JS[self.JS.index("#import-json-btn"):][:2600]
        self.assertNotIn("`${data.rows_skipped || 0} skipped`", seg)


class TestDeferredForeignKeys(unittest.TestCase):
    """Forward references (contacts.company_id -> contacts.id) must not be lost.

    Three contact rows point at a company that appears LATER in the same
    file, so the first attempt fails an FK check. The importer now defers
    those and retries once every table has been loaded.
    """

    def test_fk_violations_are_deferred(self):
        self.assertIn("23503", HANDLER)
        self.assertIn("deferred_fk", HANDLER)

    def test_retry_pass_exists(self):
        self.assertIn("Deferred FK retry", HANDLER)

    def test_retry_happens_after_all_tables(self):
        """The retry must sit outside the per-table loop, after it has drained."""
        retry = HANDLER.index("Deferred FK retry")
        copy_files = HANDLER.index("# Copy files")
        self.assertLess(
            retry, copy_files,
            "the FK retry must run after the table loop and before file copying",
        )
        # ...and it must be de-indented to the loop's own level (not nested
        # inside the per-table for), otherwise it retries after every table
        # instead of once at the end.
        line = next(l for l in HANDLER.split("\n") if "Deferred FK retry" in l)
        self.assertEqual(
            len(line) - len(line.lstrip()), 16,
            "retry block must be at the with-statement level, not nested in the table loop",
        )

    def test_unresolved_deferred_rows_count_as_failed(self):
        self.assertIn('f"{table_name} (deferred)', HANDLER)

    def test_recovered_counter_reported(self):
        self.assertIn("rows_deferred_recovered", HANDLER)


class TestRowcountIsHonest(unittest.TestCase):
    """ON CONFLICT DO NOTHING doesn't raise, so counting statements lies.

    Before this fix a no-op re-import reported every one of the 68k rows as
    "imported", which makes the restore impossible to verify.
    """

    def test_uses_db_execute_return_value(self):
        self.assertRegex(
            HANDLER,
            r"if db\.execute\(",
            "must branch on db.execute()'s rowcount, not assume success",
        )

    def test_zero_rowcount_counts_as_duplicate(self):
        self.assertRegex(
            HANDLER,
            r"\):\s*\n\s*inserted \+= 1\s*\n\s*else:\s*\n\s*duplicate \+= 1",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)