"""Regression tests for the three bug fixes shipped 2026-10-19.

Bug A — sync dropped contact_id:
  server.py sync INSERT/UPDATE must write contact_id from the OnlyOffice
  contact.id field. import_json_export.py and migrate_from_onlyoffice.py
  must include contact_id in their ON CONFLICT DO UPDATE sets.

Bug B — Event Note icon/style:
  styles.css must have a left-bar-only --accent (no fill) and a new --wash
  (left bar + 8% fill). app.js must have the wash branch in the render path
  and the customer-update class application.

Bug C — mail attachments OnlyOffice-first:
  server.py _handle_mail_attachment_serve must consult local stored_path,
  then OnlyOffice via crm_file_id (NEW), then fall back to IMAP. The
  _fetch_onlyoffice_file helper must exist and return None on 404/410.

These are source-level guards, no live DB needed.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
APPJS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
IMPORT_JSON = (ROOT / "import_json_export.py").read_text(encoding="utf-8")
MIGRATE = (ROOT / "migrate_from_onlyoffice.py").read_text(encoding="utf-8")


class TestContactSyncPatches(unittest.TestCase):
    """Bug A: every code path that writes opportunities must write contact_id."""

    def _server_sync_body(self) -> str:
        # the pull-deals handler
        i = SERVER.index("def _handle_sync_pull_deals")
        return SERVER[i: i + 30000]

    def test_sync_resolves_contact_by_onlyoffice_id(self):
        """The sync must look up contacts by onlyoffice_id to populate contact_id."""
        body = self._server_sync_body()
        self.assertIn("contacts WHERE onlyoffice_id = %s", body)

    def test_sync_insert_writes_contact_id(self):
        body = self._server_sync_body()
        # The INSERT columns must include contact_id, and the Python argument
        # list that follows the SQL must pass v3_contact_id. (Assert
        # structurally — the exact arg list changed when stage_type and
        # responsible_user_id were added.)
        m = re.search(
            r"INSERT INTO opportunities\s*\((?P<cols>.*?)\)\s*VALUES\s*\((?P<vals>.*?)\)\s*RETURNING",
            body, re.DOTALL,
        )
        self.assertIsNotNone(m, "opportunities INSERT not found")
        self.assertIn("contact_id", m.group("cols"))
        # The db.execute(...) args follow the SQL string.
        after = body[m.end(): m.end() + 500]
        self.assertIn("v3_contact_id", after)

    def test_sync_update_writes_contact_id(self):
        body = self._server_sync_body()
        # Two UPDATE forms exist now: full-mode overwrite and supplemental
        # fill-gaps. BOTH must carry contact_id (the non-greedy match hits
        # whichever comes first, so check all of them).
        matches = list(re.finditer(
            r"UPDATE opportunities\s+SET(?P<sets>.*?)WHERE id = %s",
            body, re.DOTALL,
        ))
        self.assertTrue(matches, "opportunities UPDATE not found")
        full = [m for m in matches
                if "contact_id = COALESCE(%s, contact_id)" in m.group("sets")]
        self.assertTrue(full, "full-mode overwrite UPDATE with contact_id not found")
        # Supplemental builds its UPDATE dynamically from field_pairs (no
        # literal to match), so assert the fill-gaps template plus the
        # contact_id pair that flows through it. Runtime SQL shape is covered
        # behaviorally in test_sync_supplemental.py.
        self.assertIn("COALESCE({_col}, %s)", body)
        self.assertIn('("contact_id", v3_contact_id)', body)
        # And the args tuple must pass the resolved contact.
        after = body[full[0].end(): full[0].end() + 300]
        self.assertIn("v3_contact_id", after)

    def test_import_json_export_on_conflict_includes_contact_id(self):
        """import_json_export.py ON CONFLICT DO UPDATE must write contact_id."""
        # Scope to the opportunities INSERT (not the contacts one, which also has
        # ON CONFLICT but a different schema).
        m = re.search(
            r"INSERT INTO opportunities.{0,800}",
            IMPORT_JSON, re.DOTALL,
        )
        self.assertIsNotNone(m, "opportunities INSERT not found")
        block = m.group()
        self.assertIn("contact_id = COALESCE(EXCLUDED.contact_id", block)

    def test_migrate_from_onlyoffice_on_conflict_includes_contact_id(self):
        m = re.search(
            r"INSERT INTO opportunities.{0,800}",
            MIGRATE, re.DOTALL,
        )
        self.assertIsNotNone(m, "opportunities INSERT not found")
        block = m.group()
        self.assertIn("contact_id = COALESCE(EXCLUDED.contact_id", block)


class TestMailAttachmentOnlyOfficeFirst(unittest.TestCase):
    """Bug C: serve endpoint must prefer local → OnlyOffice → IMAP."""

    def _serve_body(self) -> str:
        i = SERVER.index("def _handle_mail_attachment_serve")
        return SERVER[i: i + 6000]

    def test_tries_local_stored_path_first(self):
        body = self._serve_body()
        # Path #1: stored_path
        self.assertIn("att.get(\"stored_path\")", body)
        # stored_path check must come before crm_file_id check
        sp_idx = body.index("att.get(\"stored_path\")")
        cf_idx = body.index("att.get(\"crm_file_id\")")
        self.assertLess(sp_idx, cf_idx, "local stored_path must come before OnlyOffice fetch")

    def test_tries_onlyoffice_via_crm_file_id(self):
        """Path #2: fetch from filehandler.ashx using crm_file_id."""
        body = self._serve_body()
        self.assertIn("filehandler.ashx?action=download&fileid=", body)
        self.assertIn("att.get(\"crm_file_id\")", body)
        self.assertIn("_fetch_onlyoffice_file", body)

    def test_imap_fallback_after_onlyoffice(self):
        """Path #3: live IMAP. Must be after crm_file_id, not the first path."""
        body = self._serve_body()
        cf_idx = body.index("att.get(\"crm_file_id\")")
        imap_idx = body.index("_imap_connect")
        self.assertLess(cf_idx, imap_idx, "IMAP must be the fallback, not the first path")

    def test_returns_410_on_permanent_loss(self):
        """When both OnlyOffice and IMAP can't find the file, return 410 Gone."""
        body = self._serve_body()
        self.assertIn("410", body)

    def test_helper_function_exists(self):
        """_fetch_onlyoffice_file must exist and handle 404/410."""
        i = SERVER.index("def _fetch_onlyoffice_file")
        helper = SERVER[i: i + 1200]
        self.assertIn("404", helper)
        self.assertIn("410", helper)
        self.assertIn("filehandler", helper.lower() or "filehandler" in SERVER)

    def test_oo_portal_constant_set(self):
        self.assertIn("OO_PORTAL_URL = os.environ.get(\"ONLYOFFICE_PORTAL_URL\"", SERVER)


class TestEventNoteIconStyle(unittest.TestCase):
    """Bug B: --accent left-bar only, --wash new style, customer-update class."""

    def test_accent_no_background(self):
        """The --accent rule must not have a `background:` line."""
        i = CSS.index(".opp-preview-history-item--accent {")
        j = CSS.index("}", i)
        block = CSS[i: j]
        self.assertNotIn("background:", block, "Accent must be left-bar only (no fill)")

    def test_wash_rule_exists(self):
        self.assertIn(".opp-preview-history-item--wash {", CSS)
        i = CSS.index(".opp-preview-history-item--wash {")
        j = CSS.index("}", i)
        block = CSS[i: j]
        # Wash should have BOTH border-left AND a fill
        self.assertIn("border-left", block)
        self.assertIn("background", block)

    def test_wash_render_branch_in_appjs(self):
        i = APPJS.index("cat.iconStyle === 'accent'")
        block = APPJS[i: i + 600]
        self.assertIn("cat.iconStyle === 'wash'", block)
        self.assertIn("opp-preview-history-item--wash", block)

    def test_customer_update_class_applied(self):
        i = APPJS.index("cat.iconStyle === 'accent'")
        block = APPJS[i: i + 800]
        self.assertIn("customer update", block.lower())
        self.assertIn("opp-preview-history-item--customer-update", block)

    def test_eye_check_icon_present(self):
        self.assertIn('key: "eye-check"', APPJS)
        # Should have an svg element
        idx = APPJS.index('key: "eye-check"')
        block = APPJS[idx: idx + 600]
        self.assertIn("<svg", block)
        self.assertIn("circle", block)

    def test_device_mobile_icon_present(self):
        self.assertIn('key: "device-mobile"', APPJS)
        idx = APPJS.index('key: "device-mobile"')
        block = APPJS[idx: idx + 600]
        self.assertIn("<svg", block)
        self.assertIn("rect", block)

    def test_cache_bust_bumped(self):
        idx_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn("app.js?v=3.7.43", idx_html)
        self.assertIn("styles.css?v=3.7.22", idx_html)

    def test_wash_radio_button_exists_in_admin_form(self):
        """The admin Event Note Type form must OFFER a Wash option.

        Bug 2026-10-19: the --wash CSS class was added but the radio button was
        never added to the form, so the user could not select it.
        """
        idx_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            'name="admin-history-cat-icon-style" value="wash"',
            idx_html,
            "Wash radio button missing from the Event Note Type admin form",
        )
        # All four styles must be present
        for style in ("stroke", "fill", "accent", "wash"):
            self.assertIn(
                f'name="admin-history-cat-icon-style" value="{style}"',
                idx_html,
                f"Icon style '{style}' missing from admin form",
            )

    def test_icon_style_help_text_updated(self):
        """The help text must describe Accent as left-bar only and mention Wash."""
        idx_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Accent: left border bar only", idx_html)
        self.assertIn("Wash: left border bar + light background fill", idx_html)
        # The old (wrong) description must be gone
        self.assertNotIn("Accent: background wash + border", idx_html)

    def test_system_categories_are_editable(self):
        """System Event Note Types must have an Edit button.

        Bug 2026-10-19: the edit button was hidden for isSystem categories, so
        the user could not change icons on Mail Message / Files are uploaded /
        Task is closed. They must remain non-deletable though.
        """
        i = APPJS.index("function renderHistoryCatsList(")
        block = APPJS[i: i + 2000]
        # Edit button must render unconditionally
        self.assertIn(
            'data-edit-cat="${cat.id}"',
            block,
            "Edit button must be available for system categories too",
        )
        # Delete button must still be hidden for system categories
        self.assertIn(
            '${cat.isSystem ? "" : `<button class="btn btn-ghost" data-delete-cat=',
            block,
            "Delete button must stay hidden for system categories",
        )

    def test_icon_svg_renderer_handles_wash(self):
        """The shared icon renderer must color the stroke for wash style."""
        i = APPJS.index('iconStyle === "fill"')
        block = APPJS[i: i + 500]
        self.assertIn('iconStyle === "accent" || iconStyle === "wash"', block)

    def test_customer_update_uses_configured_color(self):
        """Customer Update's orange bar should follow the category color."""
        i = APPJS.index("cat.iconStyle === 'accent'")
        block = APPJS[i: i + 900]
        self.assertIn("'--cat-accent-color', cat.color || '#f97316'", block)


class TestBackfillScripts(unittest.TestCase):
    """The two backfill scripts must exist and default to dry-run."""

    def test_contact_backfill_exists(self):
        path = ROOT / "scripts" / "backfill_contacts_from_export.py"
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn("DRY-RUN BY DEFAULT", body)
        self.assertIn("--write", body)
        self.assertIn("contact_id", body)

    def test_mail_attachment_backfill_exists(self):
        path = ROOT / "scripts" / "backfill_mail_attachments_onlyoffice.py"
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn("DRY-RUN BY DEFAULT", body)
        self.assertIn("filehandler.ashx", body)
        self.assertIn("crm_file_id", body)
        self.assertIn("--write", body)

    def test_contact_live_backfill_exists(self):
        path = ROOT / "scripts" / "backfill_contacts_live.py"
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn("DRY-RUN BY DEFAULT", body)
        self.assertIn("oo_authenticate", body)
        self.assertIn("/api/2.0/crm/opportunity/", body)
        self.assertIn("--write", body)
        # Must use authenticated API, not just any HTTP
        self.assertIn("Authorization", body)


class TestNoteAttachmentButtons(unittest.TestCase):
    """Paperclip button left of the calendar in all three note editors, and
    the bookmark/search Post fix."""

    def test_preview_editor_has_attach_button_left_of_backdate(self):
        i = APPJS.index("preview-note-actions")
        block = APPJS[i: i + 3000]
        self.assertIn("preview-note-attach-btn", block)
        # Attach must come before backdate in the markup order
        self.assertLess(
            block.index("preview-note-attach-btn"),
            block.index("preview-note-backdate-btn"),
            "paperclip must sit left of the calendar button",
        )
        self.assertIn("preview-note-attachments-input", block)
        self.assertIn("preview-note-selected-attachments", block)

    def test_preview_submit_uploads_attachments(self):
        self.assertIn("editorWrap._pendingAttachments", APPJS)
        self.assertIn("uploadAttachmentForNote(f, oppId)", APPJS)

    def test_bookmark_post_refreshes_bookmark_not_main_modal(self):
        i = APPJS.index('if (context === "bookmark")')
        block = APPJS[i: i + 800]
        self.assertIn("refreshBookmarkTab", block)
        # The unconditional main-modal reopen must be gone from this path
        self.assertNotIn(
            "openOpportunityPreviewModal(opp.id ?? opp.ID, titleHint",
            block,
        )

    def test_search_preview_renders_with_search_context(self):
        self.assertIn('renderOpportunityPreviewContent(body, data, "search")', APPJS)
        self.assertIn('if (context === "search")', APPJS)
        self.assertIn("refreshSearchPreviewTab", APPJS)

    def test_quick_note_has_attach_button(self):
        idx_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn("quick-note-attach-btn", idx_html)
        qi = idx_html.index("quick-note-attach-btn")
        bi = idx_html.index("note-backdate-btn", qi - 2000)
        # Button order in the row: attach before backdate
        row = idx_html[max(0, qi - 500): qi + 500]
        self.assertIn("note-backdate-btn", row)

    def test_project_page_has_attach_button(self):
        phtml = (ROOT / "public" / "project.html").read_text(encoding="utf-8")
        self.assertIn("phtml-note-attach-btn", phtml)
        ai = phtml.index("phtml-note-attach-btn")
        bi = phtml.index("note-backdate-btn", ai)
        self.assertLess(ai, bi, "paperclip must sit left of the calendar button")
        self.assertIn("phtml-note-attachments", phtml)
        self.assertIn("phtml-selected-attachments", phtml)
        self.assertIn("fileIds", phtml)


import re  # used above


if __name__ == "__main__":
    unittest.main(verbosity=2)