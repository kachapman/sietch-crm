"""Regression tests for the 2026-10-19 import-sync fixes.

Three bugs, all in the same pull-deals sync path, all in the same batch of
13 opportunities created 2026-10-19 (ids 1235-1247):

1. STAGE LOOKUP NEVER MATCHED. `stage_map` was keyed by `title.lower()` but
   looked up with `crm_stage_id.lower()`. The OnlyOffice stage id and the
   Sietch stage id are DIFFERENT namespaces (OO goes to 21+, Sietch to 19), so
   the only valid join key is the stage TITLE. Every newly-synced deal got
   stage_id = NULL.

2. responsible_user_id WAS NEVER WRITTEN. The sync built `user_id_map`
   (OnlyOffice UUID -> Sietch users.id) but only used it for history-event
   authors, never for the opportunity's responsible user.

3. THE INSERT DROPPED COLUMNS. The INSERT listed 7 columns; expected_close_date
   was never written either.

Also covered: the email-body backfill (6,854 of 7,521 bodies restored from
OnlyOffice's mail_mail Elasticsearch index) and the frontend id-collision guard
(608 events whose OnlyOffice message_id coincides with a Sietch
mail_messages.id — fetching by id would show a different email).

Source-level guards, no live DB needed.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
APPJS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")


def _pull_deals_body() -> str:
    i = SERVER.index("def _handle_sync_pull_deals")
    return SERVER[i: i + 30000]


class TestStageLookup(unittest.TestCase):
    """Bug 1: stage lookup must join on title, and both maps must agree."""

    def test_stage_map_keyed_by_title(self):
        body = _pull_deals_body()
        m = re.search(r"stage_map\s*=\s*\{.*?\n\s*\}\s*\n", body, re.DOTALL)
        self.assertIsNotNone(m, "stage_map definition not found")
        block = m.group()
        self.assertIn('title', block, "stage_map must be keyed by title")
        # The old bug: keyed by title but looked up by id.
        self.assertNotIn(
            'stage_map[str(row["id"])]',
            block,
            "stage_map must NOT be keyed by id — OO and Sietch stage ids are "
            "different namespaces",
        )

    def test_stage_lookup_uses_title(self):
        body = _pull_deals_body()
        self.assertIn("stage_map.get(stage_title)", body)
        # The old bug: lookup by the CRM stage id.
        self.assertNotIn(
            "stage_map.get(crm_stage_id.lower()",
            body,
            "stage lookup by crm stage id never matched (id namespaces differ)",
        )

    def test_stage_title_is_extracted(self):
        body = _pull_deals_body()
        self.assertRegex(
            body,
            r'stage_title\s*=\s*str\(\s*stage_data\.get\("title"\)',
            "stage title must be read from the CRM stage payload",
        )


class TestResponsibleUser(unittest.TestCase):
    """Bug 2: responsible_user_id must be resolved and written."""

    def test_resolves_responsible_via_user_id_map(self):
        body = _pull_deals_body()
        self.assertIn("v3_responsible_id = user_id_map.get(resp_crm_id)", body)

    def test_insert_writes_responsible_user_id(self):
        body = _pull_deals_body()
        m = re.search(r"INSERT INTO opportunities\s*\((.*?)\)\s*VALUES", body, re.DOTALL)
        self.assertIsNotNone(m, "opportunities INSERT not found")
        cols = m.group(1)
        for col in ("stage_id", "stage_type", "bid_value", "expected_close_date",
                    "actual_close_date", "actual_close_value", "contact_id",
                    "responsible_user_id"):
            self.assertIn(col, cols, f"INSERT must write {col}")

    def test_update_writes_responsible_user_id(self):
        body = _pull_deals_body()
        m = re.search(r"UPDATE opportunities\s+SET(.*?)WHERE id = %s", body, re.DOTALL)
        self.assertIsNotNone(m, "opportunities UPDATE not found")
        sets = m.group(1)
        for col in ("stage_id", "stage_type", "bid_value", "expected_close_date",
                    "actual_close_date", "actual_close_value", "contact_id",
                    "responsible_user_id"):
            self.assertIn(col, sets, f"UPDATE must write {col}")


class TestContactSyncStillWired(unittest.TestCase):
    """The contact fix (shipped earlier) must survive the rewrite."""

    def test_contact_resolved_by_onlyoffice_id(self):
        body = _pull_deals_body()
        self.assertIn("contacts WHERE onlyoffice_id = %s", body)

    def test_contact_in_insert_and_update(self):
        body = _pull_deals_body()
        m = re.search(r"INSERT INTO opportunities\s*\((.*?)\)\s*VALUES", body, re.DOTALL)
        self.assertIn("contact_id", m.group(1))


class TestEmailBodyBackfill(unittest.TestCase):
    """The body backfill must be present and dry-run by default."""

    def test_backfill_script_exists(self):
        p = ROOT / "scripts" / "backfill_email_bodies.py"
        self.assertTrue(p.exists())
        body = p.read_text(encoding="utf-8")
        self.assertIn("DRY-RUN BY DEFAULT", body)
        self.assertIn("--write", body)
        # Must resolve via the OnlyOffice mail archive over SSH.
        self.assertIn("mail_mail", body)

    def test_remote_fetcher_exists(self):
        p = ROOT / "scripts" / "oo_fetch_bodies.py"
        self.assertTrue(p.exists())
        body = p.read_text(encoding="utf-8")
        self.assertIn("mail_mail", body)
        self.assertIn("base64", body)

    def test_server_has_mail_body_fetcher(self):
        self.assertIn("_fetch_onlyoffice_mail_body", SERVER)
        self.assertIn("OO_MAIL_ES_SSH_KEY", SERVER)

    def test_deal_fields_backfill_exists(self):
        p = ROOT / "scripts" / "backfill_deal_fields.py"
        self.assertTrue(p.exists())
        body = p.read_text(encoding="utf-8")
        self.assertIn("DRY-RUN BY DEFAULT", body)
        self.assertIn("--write", body)
        self.assertIn("stage_id", body)
        self.assertIn("responsible_user_id", body)


class TestFrontendCollisionGuard(unittest.TestCase):
    """content.message_id is an ONLYOFFICE id that can collide with a Sietch
    mail_messages.id. The UI must prefer the event's own body."""

    def test_renders_event_body_before_fetching(self):
        i = APPJS.index("if (mailPayload && (mailPayload.body_html || mailPayload.body_text)) {")
        block = APPJS[i: i + 2000]
        self.assertIn("renderMailEmbedPanel", block)
        self.assertIn("body_html", block)

    def test_subject_guard_on_fetched_message(self):
        self.assertIn("Linked email subject mismatch", APPJS)
        self.assertIn("wantSubj", APPJS)
        self.assertIn("gotSubj", APPJS)

    def test_error_message_mentions_pre2024(self):
        """The old message said 'deleted or access changed in CRM', which was
        wrong — the emails are archived, they just predate the mail archive."""
        self.assertNotIn(
            "Linked email no longer available (deleted or access changed in CRM)",
            APPJS,
            "misleading error message must be gone",
        )
        self.assertIn("predates the CRM mail archive", APPJS)


class TestFollowUpDueAlwaysShown(unittest.TestCase):
    """Follow-up Due must render even when expected_close_date is NULL, so the
    date is settable from the panel. The due-date and stage saves must refresh
    in place for bookmark/search instead of popping the main modal."""

    def test_due_section_not_gated_on_row(self):
        self.assertNotIn(
            "if (dueDateRow) {",
            APPJS,
            "due section must not be gated on dueDateRow existing",
        )
        self.assertIn("dueRow = dueDateRow ||", APPJS)

    def test_due_save_branches_on_context(self):
        i = APPJS.index("opp-preview-due-date-input");
        block = APPJS[i: i + 4000]
        self.assertIn('if (context === "bookmark")', block)
        self.assertIn('} else if (context === "search") {', block)
        self.assertIn("refreshBookmarkTab", block)
        self.assertIn("refreshSearchPreviewTab", block)

    def test_stage_save_branches_on_search(self):
        i = APPJS.index("opp-preview-stage-select");
        block = APPJS[i: i + 3000]
        self.assertIn('} else if (context === "search") {', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
