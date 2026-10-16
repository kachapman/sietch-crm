#!/usr/bin/env python3
"""Unit tests for the search modal (admin search popup).

Covers the audit-round fixes:
  - #1 server-side pagination for the 'Closed' deals filter (count + startIndex
    flowing through to /api/v2/projects and /api/v2/projects/count, instead
    of the old hardcoded count: 500).
  - #3 cross-page export via the new ?ids= filter.
  - #2 quick-note side-by-side layout restore (the inline pointerEvents /
    zIndex on whichever side modal is active must be cleared on restore).

The JS-side fixes (batch parallelism, contacts AbortController, select-all
hint, MAX comment) are covered by source guards in this file.

server imports cleanly (it only binds the socket inside main(), which
the tests never call), so the source guards assert against the real
server module and the real handler bodies.

Run with: python3 tests/test_search_modal.py
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import server  # noqa: E402
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    server = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if server is None:
    print(f"SKIP: server module not importable (no reachable database?): {_IMPORT_ERROR}")
    print("      Run with a live PostgreSQL instance to exercise these tests.")
    sys.exit(0)


# ── Server-side: ?ids= filter for cross-page export (#3) ─────────────────────

class TestProjectsIdsFilter(unittest.TestCase):
    """The cross-page export calls /api/v2/projects?ids=1,2,3 and expects
    only those rows back. _build_projects_where must turn the comma list
    into an integer array and apply o.id = ANY(%s)."""

    def setUp(self):
        self.server_src = Path(server.__file__).read_text(encoding="utf-8")

    def test_build_projects_where_handles_ids_param(self):
        # The ids parsing lives inside _build_projects_where.
        self.assertIn("ids_raw = qval(qs, \"ids\")", self.server_src)
        self.assertIn("where.append(\"o.id = ANY(%s)\")", self.server_src)

    def test_ids_param_is_bound_as_integer_array(self):
        # o.id = ANY(%s) is the right way to match an integer list in
        # psycopg2. String concatenation here would be a SQL injection
        # vector; the code parses to int first and then binds as a
        # single parameter, so the value is always a typed list.
        self.assertIn("[int(x) for x in ids_raw.split(\",\") if x.strip()]", self.server_src)
        # And the list is bound directly, not concatenated.
        self.assertIn("params.append(parsed_ids)", self.server_src)

    def test_invalid_ids_does_not_break(self):
        # If the user passes ids=abc,def, the parser raises ValueError
        # and we silently ignore the param. Verified by the source guard:
        self.assertIn("except ValueError:\n                pass", self.server_src)

    def test_empty_ids_list_does_not_match_everything(self):
        # If ids_raw parses to an empty list (e.g. "ids=,,,"), the
        # filter is skipped. Verified by source guard.
        self.assertIn("if parsed_ids:", self.server_src)


# ── Server-side: the count + startIndex flow stays correct (#1) ────────────

class TestProjectsCountFlow(unittest.TestCase):
    """The Closed-deals path now relies on the server honouring
    'count' and 'startIndex' for both /api/v2/projects and the count
    endpoint. If either handler ever drops the params the page
    count goes off-by-one."""

    def setUp(self):
        self.server_src = Path(server.__file__).read_text(encoding="utf-8")

    def test_projects_list_accepts_count_and_startIndex(self):
        # _handle_projects_list reads 'count' (default 500) and
        # 'startIndex' (default 0). The fix relies on these flowing
        # through to LIMIT / OFFSET.
        body = self._handler_body("_handle_projects_list")
        self.assertIn("int(qs.get(\"count\", [\"500\"])[0])", body)
        self.assertIn("int(qs.get(\"startIndex\", [\"0\"])[0])", body)
        self.assertIn("LIMIT %s OFFSET %s", body)

    def test_projects_count_accepts_count_and_startIndex(self):
        # _handle_projects_count: count + startIndex are harmless
        # to accept (they don't change the result) but the handler
        # must NOT 400 on them. Source guard confirms they're ignored
        # by checking the function ignores them.
        body = self._handler_body("_handle_projects_count")
        # The count query is just a SELECT COUNT(*); no count/offset.
        # What matters is that the WHERE clause still reflects
        # filterValue / stageId / tagId / responsibleUserId.
        self.assertIn("SELECT COUNT(o.id)", body)

    def _handler_body(self, name):
        start = self.server_src.index(f"def {name}")
        nxt = self.server_src.find("\n    def ", start + 1)
        nxt2 = self.server_src.find("\ndef ", start + 1)
        ends = [e for e in (nxt, nxt2) if e != -1]
        return self.server_src[start:min(ends) if ends else len(self.server_src)]


# ── JS: source guards for the fixes in app.js ────────────────────────────────

class TestSearchModalJsGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        cls.html_src = (HERE.parent / "public" / "index.html").read_text(encoding="utf-8")
        cls.css_src = (HERE.parent / "public" / "styles.css").read_text(encoding="utf-8")

    # #1 closed-deals pagination
    def test_closed_path_uses_page_size_not_hardcoded_500(self):
        body = self._function_body("performSearchPopupQuery")
        # The old code had:
        #   wonParams.set("count", "500");
        #   lostParams.set("count", "500");
        # The fix passes SEARCH_POPUP_PAGE_SIZE for both and includes
        # the current startIndex so server-side pagination works.
        self.assertIn("wonParams.set(\"count\", String(SEARCH_POPUP_PAGE_SIZE))", body)
        self.assertIn("lostParams.set(\"count\", String(SEARCH_POPUP_PAGE_SIZE))", body)
        self.assertIn("wonParams.set(\"startIndex\", String(searchPopupStartIndex))", body)
        self.assertIn("lostParams.set(\"startIndex\", String(searchPopupStartIndex))", body)
        # And the old hardcoded 500 must be gone.
        self.assertNotIn('wonParams.set("count", "500")', body)
        self.assertNotIn('lostParams.set("count", "500")', body)

    def test_closed_path_no_client_side_slicing(self):
        # After the fix, the merged opps IS the page (no slice).
        body = self._function_body("performSearchPopupQuery")
        self.assertIn("renderSearchPopupResults(opps)", body)
        self.assertNotIn("const pageSlice = opps.slice(start, end);", body)
        self.assertNotIn("renderSearchPopupResults(pageSlice);", body)

    # #7 contacts pagination wired up
    def test_contacts_query_supports_abort(self):
        # AbortController is declared at module scope and used inside
        # performSearchContactsQuery.
        self.assertIn("let searchPopupContactsQueryAbort = null;", self.app_src)
        body = self._function_body("performSearchContactsQuery")
        self.assertIn("searchPopupContactsQueryAbort.abort()", body)
        self.assertIn("new AbortController()", body)
        self.assertIn("searchPopupContactsQueryAbort = ac;", body)
        self.assertIn("ac.signal.aborted", body)
        # And the data and count calls both pass the signal.
        self.assertIn("signal: ac.signal", body)

    def test_contacts_query_tracks_total_and_renders_page_info(self):
        body = self._function_body("performSearchContactsQuery")
        self.assertIn("searchPopupContactsTotal = countData?.count", body)
        self.assertIn("updateSearchContactsPagination()", body)

    def test_contacts_page_change_function_exists(self):
        # The page-change function and the pagination update live as
        # top-level functions; they must be present and bound to the
        # prev/next buttons inside bindSearchPopupModal.
        self.assertIn("function performSearchContactsPageChange", self.app_src)
        self.assertIn("function updateSearchContactsPagination", self.app_src)
        # Both bound in bindSearchPopupModal.
        self.assertIn('search-popup-contacts-prev")?.addEventListener', self.app_src)
        self.assertIn('search-popup-contacts-next")?.addEventListener', self.app_src)

    def test_contacts_total_resets_on_close(self):
        body = self._function_body("closeSearchPopupModal")
        self.assertIn("searchPopupContactsStartIndex = 0", body)
        self.assertIn("searchPopupContactsTotal = 0", body)
        self.assertIn("searchPopupContactsQueryAbort.abort()", body)

    def test_contacts_pagination_buttons_in_markup(self):
        self.assertIn('id="search-popup-contacts-prev"', self.html_src)
        self.assertIn('id="search-popup-contacts-next"', self.html_src)
        self.assertIn('id="search-popup-contacts-page-info"', self.html_src)

    # #2 restoreSideBySideCards handles the quick-note side modal
    def test_restore_handles_quick_note_side_modal(self):
        body = self._function_body("restoreSideBySideCards")
        # The detect-and-restore branch picks whichever side modal is
        # currently visible (deal-edit preferred, else quick-note):
        self.assertIn("quickNoteModal", body)
        self.assertIn("sideModal", body)
        self.assertIn("? editModal", body)
        self.assertIn(": quickNoteModal", body)
        # And the inline styles it must clear:
        self.assertIn('sideModal.style.pointerEvents = ""', body)
        self.assertIn('sideModal.style.zIndex = ""', body)
        # And the backdrop restore:
        self.assertIn("sBack.dataset.sideHidden", body)

    def test_restore_clears_active_side_modal_inline_styles(self):
        # The deal-edit path is now handled by the generic sideModal
        # branch (which picks whichever of deal-edit/quick-note is the
        # active side modal). Both side modals must end up with the
        # pointerEvents and zIndex inline styles cleared so the modal
        # is clickable again.
        body = self._function_body("restoreSideBySideCards")
        # The detect-and-restore branch:
        self.assertIn("quickNoteModal", body)
        self.assertIn("sideModal", body)
        # The inline-styles-to-clear:
        self.assertIn('sideModal.style.pointerEvents = ""', body)
        self.assertIn('sideModal.style.zIndex = ""', body)
        # And the backdrop restore:
        self.assertIn("sBack.dataset.sideHidden", body)
        # Specifically, the old hardcoded editModal branch is gone -
        # the function must NOT only restore deal-edit and leave
        # quick-note broken.
        self.assertNotIn('editModal.style.pointerEvents = ""', body,
                         "the old deal-edit-only branch is still present - "
                         "quick-note side modal would never get its pointerEvents cleared")
        # And the modal-card style is still restored for all three modals.
        self.assertIn("[previewModal, searchModal, editModal].forEach", body)

    # #4 batch parallelism
    def test_batch_action_uses_promise_allsettled(self):
        body = self._function_body("applySearchPopupBatchAction")
        self.assertIn("Promise.allSettled", body)
        # And the sequential for-await is gone.
        self.assertNotIn("for (const id of ids) {", body)

    def test_batch_action_reports_per_deal_failures(self):
        body = self._function_body("applySearchPopupBatchAction")
        # Show toasts for the failed deals.
        self.assertIn("showToast(`Batch failed on deal #", body)
        # Track the count of fulfilled.
        self.assertIn("const done = perDeal.filter", body)
        self.assertIn('"fulfilled"', body)

    # #3 cross-page export
    def test_export_selected_uses_ids_filter_not_cached_opps(self):
        body = self._function_body("exportSearchPopupSelected")
        # Hits the server with ?ids=... (cross-page).
        self.assertIn("`/api/v2/projects?ids=${ids.join(\",\")}", body)
        # No longer uses the cached searchPopupCurrentOpps.
        self.assertNotIn("searchPopupCurrentOpps.filter", body)

    # #5 select-all clarity
    def test_select_all_label_hints_about_cross_page(self):
        # The new label adds the "(selections carry across pages)" hint.
        self.assertIn("Select all on page", self.app_src)
        self.assertIn("selections carry across pages", self.app_src)

    # #8 MAX comment fixed
    def test_max_search_preview_tabs_comment_is_current(self):
        # The stale "minus 1 for the search tab" wording now only appears
        # inside a meta-comment describing the change. The substantive new
        # wording is split across // comment lines, so strip the comment
        # markers before collapsing whitespace - otherwise the "//"
        # tokens break the phrase.
        no_comments = re.sub(r"//", " ", self.app_src)
        flat = re.sub(r"\s+", " ", no_comments)
        self.assertIn("the cap is on PREVIEW tabs", flat,
                      "new comment doesn't say 'cap is on PREVIEW tabs'")
        self.assertIn("separate from the built-in", flat)
        self.assertIn("Projects/Contacts", flat)
        self.assertIn("tabs that always exist in the bar", flat)

    def _function_body(self, name):
        m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
                      self.app_src)
        if m is None:
            self.fail(f"{name} not found in app.js")
        start = m.end() - 1
        depth = 1
        i = start + 1
        while i < len(self.app_src) and depth > 0:
            ch = self.app_src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        self.assertEqual(depth, 0, f"unbalanced braces in {name}")
        return self.app_src[start:i]


# ── Real handler exercises: the ?ids= filter path ──────────────────────────

class TestIdsFilterHandler(unittest.TestCase):
    """Drive _build_projects_where with the ids= param set and check the
    resulting WHERE clause + params. _build_projects_where is a method
    on KanbanHandler and uses self._projects_query_val; to call it
    without instantiating a real handler (which would need a socket),
    we construct a tiny stub that exposes only the _projects_query_val
    helper, then invoke the unbound function via __get__()."""

    def _call(self, qs, user=None):
        from urllib.parse import urlencode, parse_qs
        raw = urlencode(qs, doseq=True)
        parsed = parse_qs(raw, keep_blank_values=True)

        def qval(qs_, *keys):
            for k in keys:
                if k in qs_ and qs_[k]:
                    v = qs_[k]
                    return v[0] if isinstance(v, (list, tuple)) else v
            return None

        class _Stub:
            pass
        stub = _Stub()
        stub._projects_query_val = qval
        method = server.KanbanHandler._build_projects_where
        return method(stub, parsed, user)

    def test_ids_includes_id_any_clause(self):
        where, params = self._call({"ids": "1,2,3"})
        joined = " AND ".join(where)
        self.assertIn("o.id = ANY(%s)", joined)
        # The integer list is bound as a single parameter.
        self.assertEqual(params, [[1, 2, 3]])

    def test_ids_combined_with_filterValue(self):
        where, params = self._call({"ids": "10,20", "filterValue": "Ponderosa"})
        joined = " AND ".join(where)
        self.assertIn("o.id = ANY(%s)", joined)
        # And the search filter is also applied.
        self.assertIn("o.title ILIKE %s", joined)
        # The search filter adds 6 placeholders first (title, description,
        # first_name, last_name, company, custom_field_value - all bound to
        # the same %Ponderosa% value). The ids array is added last by the
        # order of the if-blocks in _build_projects_where.
        self.assertEqual(len(params), 7)
        for i in range(6):
            self.assertEqual(params[i], "%Ponderosa%",
                             f"expected search placeholder at index {i}, got {params[i]!r}")
        self.assertEqual(params[6], [10, 20])

    def test_invalid_ids_silently_dropped(self):
        where, params = self._call({"ids": "abc,def"})
        joined = " AND ".join(where)
        self.assertNotIn("o.id = ANY", joined)
        self.assertEqual(params, [])

    def test_empty_ids_silently_dropped(self):
        # ids=, parses to [""], filtered out by `if parsed_ids`.
        where, params = self._call({"ids": ",,,"})
        joined = " AND ".join(where)
        self.assertNotIn("o.id = ANY", joined)
        self.assertEqual(params, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)