"""Unit tests: attachment mirroring + bookmark title resolution.

Covers two production regressions:

1. The pull-deals OnlyOffice sync inserted into `history_attachments` but never
   mirrored into `project_documents`. The Documents tab
   (GET /api/v2/projects/{id}/documents) reads ONLY `project_documents`, so
   synced files were invisible ("0 documents") even though they were on disk
   and in the DB. 130 rows were backfilled and the sync now mirrors.

2. The preview modal's bookmark button was clickable while the project was
   still loading. `oppPreviewContext.opp` is null in that window, so the
   bookmark was stored with the "Project #<id>" placeholder and never
   corrected. Titles are now resolved before saving, and the panel resolves a
   live title at render time.

These are source-level guards (no live DB needed), so they run in CI.

Run with: python3 tests/test_attachment_mirror_and_bookmark_titles.py
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = (ROOT / "server.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")


def _server_slice(start_marker: str, end_marker: str, src: str = SERVER_PY) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class TestSyncMirrorsToProjectDocuments(unittest.TestCase):
    """The sync must write BOTH attachment tables."""

    def _sync_body(self) -> str:
        return _server_slice(
            "def _handle_sync_pull_deals",
            "def _handle_sync_pull_email_bodies",
        )

    def test_sync_inserts_into_history_attachments(self):
        self.assertIn("INSERT INTO history_attachments", self._sync_body())

    def test_sync_also_inserts_into_project_documents(self):
        """REGRESSION: the sync only wrote history_attachments, so the
        Documents tab showed 0 documents for every synced deal."""
        self.assertIn("INSERT INTO project_documents", self._sync_body())

    def test_sync_attachment_query_is_not_limited_to_events_with_no_attachments(self):
        """REGRESSION: the query only selected events that had NO attachments,
        so an event that already had some files was skipped entirely and its
        remaining files were downloaded to disk but never recorded."""
        body = self._sync_body()
        self.assertNotIn("AND NOT EXISTS (", body.split("INSERT INTO history_attachments")[0][-2000:])

    def test_documents_endpoint_falls_back_to_history_attachments(self):
        """Defensive: a missing mirror must never render as '0 documents'."""
        handler = _server_slice(
            "def _handle_project_documents_get",
            "def _handle_project_document_upload",
        )
        self.assertIn("history_attachments", handler)


class TestBookmarkTitleResolution(unittest.TestCase):
    """A bookmark must never persist a 'Project #<id>' placeholder."""

    def test_helper_exists(self):
        self.assertIn("function resolveOpportunityTitleById(", APP_JS)

    def test_add_bookmark_resolves_title_before_fallback(self):
        body = _server_slice(
            "function addBookmarkDeal(",
            "function removeBookmarkDeal(",
            APP_JS,
        )
        self.assertIn("resolveOpportunityTitleById(id)", body)

    def test_add_bookmark_does_not_store_placeholder_when_title_available(self):
        body = _server_slice(
            "function addBookmarkDeal(",
            "function removeBookmarkDeal(",
            APP_JS,
        )
        # The placeholder must be the LAST resort, after resolution.
        self.assertLess(
            body.index("resolveOpportunityTitleById(id)"),
            body.index("`Project #${id}`"),
        )

    def test_preview_modal_bookmark_uses_resolved_title(self):
        """REGRESSION: the preview context is null while loading, so
        ctx.opp.title was empty and the placeholder was stored."""
        body = _server_slice(
            "const ctx = oppPreviewContext;",
            "updatePreviewModalBookmarkButton(id);",
            APP_JS,
        )
        self.assertIn("resolveOpportunityTitleById(id)", body)

    def test_bookmark_panel_resolves_live_title_at_render(self):
        """The panel must self-heal a bad stored snapshot."""
        body = _server_slice(
            "function renderBookmarkTabs()",
            "function renderBookmarkDivider(",
            APP_JS,
        )
        self.assertIn("resolveOpportunityTitleById(deal.oppId)", body)
        self.assertIn("shownTitle", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)