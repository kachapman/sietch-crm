#!/usr/bin/env python3
"""Unit tests for the opt-in `include_closed` project-list filter (2026-11-03).

Background
----------
The email inbox toolbar's "Link to deal" popup could only ever find OPEN
projects: the frontend searched `/api/v2/projects` with `stageType=0`, so a
closed project (Won=1 / Lost=2) produced "No matches" and could not be
linked at all.

The fix adds an opt-in `include_closed` query parameter to the backend
project search. It defaults to FALSE so the existing behaviour of every
other caller is untouched; when it is true the stage_type filter is widened
so closed projects come back too.

Covered here:
  - default (no include_closed)  -> `o.stage_type = %s` (unchanged)
  - include_closed=false / 0     -> unchanged
  - include_closed=true / 1      -> `o.stage_type = ANY(%s)` with [0, 1, 2]
  - the flag is opt-in (absent => False)
  - the flag composes with the other filters (search / ids / tag)
  - source guards: the frontend sends the flag and renders a "Closed" badge

server imports cleanly (it only binds the socket inside main(), which the
tests never call), so the handler is exercised for real via
KanbanHandler._build_projects_where.

Run with: python3 tests/test_link_show_closed.py
"""

import re
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode

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


def _qval(qs_, *keys):
    """Mirror of KanbanHandler._projects_query_val (the handler only uses
    self._projects_query_val, so a stub can stand in for the instance)."""
    for k in keys:
        if k in qs_ and qs_[k]:
            v = qs_[k]
            return v[0] if isinstance(v, (list, tuple)) else v
    return None


class _Stub:
    pass


def _build_where(qs: dict, user: dict | None = None):
    """Call the real _build_projects_where with the given query string."""
    parsed = parse_qs(urlencode(qs, doseq=True), keep_blank_values=True)
    stub = _Stub()
    stub._projects_query_val = _qval
    method = server.KanbanHandler._build_projects_where
    return method(stub, parsed, user)


# ── Default behaviour must be unchanged ──────────────────────────────────────

class TestIncludeClosedDefaultsToFalse(unittest.TestCase):
    """Without the flag the endpoint must behave exactly as before:
    `o.stage_type = %s` bound to the requested stage type."""

    def test_absent_flag_keeps_equality_filter(self):
        where, params = _build_where({"stageType": "0"})
        joined = " AND ".join(where)
        self.assertIn("o.stage_type = %s", joined)
        self.assertNotIn("ANY(%s)", joined)
        self.assertEqual(params, [0])

    def test_absent_flag_leaves_open_deals_only(self):
        # The email Link popup asks for stageType=0 -> open projects only.
        where, params = _build_where({"stageType": "0"})
        self.assertEqual(params, [0])

    def test_explicit_false_keeps_equality_filter(self):
        for value in ("false", "False", "FALSE", "0", "no", "off", ""):
            with self.subTest(value=value):
                where, params = _build_where({"stageType": "0", "include_closed": value})
                joined = " AND ".join(where)
                self.assertIn("o.stage_type = %s", joined)
                self.assertNotIn("ANY(%s)", joined)
                self.assertEqual(params, [0])

    def test_garbage_value_is_treated_as_false(self):
        where, params = _build_where({"stageType": "0", "include_closed": "banana"})
        self.assertNotIn("ANY(%s)", " AND ".join(where))
        self.assertEqual(params, [0])

    def test_no_stage_type_filter_is_unaffected(self):
        # Without a stageType there is nothing to widen, closed or not.
        where, params = _build_where({})
        self.assertEqual(params, [])
        self.assertNotIn("stage_type", " AND ".join(where))


# ── include_closed=true widens the filter ───────────────────────────────────

class TestIncludeClosedTrue(unittest.TestCase):
    """With the flag on, the stage_type filter becomes an IN-list that also
    contains the closed stage types (1 = Won, 2 = Lost)."""

    def test_true_widens_to_open_plus_closed(self):
        where, params = _build_where({"stageType": "0", "include_closed": "true"})
        joined = " AND ".join(where)
        self.assertIn("o.stage_type = ANY(%s)", joined)
        self.assertNotIn("o.stage_type = %s", joined)
        self.assertEqual(params, [[0, 1, 2]])

    def test_numeric_one_widens_too(self):
        where, params = _build_where({"stageType": "0", "include_closed": "1"})
        self.assertIn("o.stage_type = ANY(%s)", " AND ".join(where))
        self.assertEqual(params, [[0, 1, 2]])

    def test_yes_and_on_are_accepted(self):
        for value in ("yes", "YES", "on", "On", " true "):
            with self.subTest(value=value):
                where, params = _build_where({"stageType": "0", "include_closed": value})
                self.assertIn("o.stage_type = ANY(%s)", " AND ".join(where))
                self.assertEqual(params, [[0, 1, 2]])

    def test_camel_case_alias_is_supported(self):
        # The frontend sends include_closed; accept includeClosed too so the
        # param name matches the rest of the v2 API's camelCase style.
        where, params = _build_where({"stageType": "0", "includeClosed": "true"})
        self.assertIn("o.stage_type = ANY(%s)", " AND ".join(where))
        self.assertEqual(params, [[0, 1, 2]])

    def test_requested_stage_type_is_still_honoured(self):
        # stageType=1 (Won) + include_closed -> 1 and 2 (not 0).
        where, params = _build_where({"stageType": "1", "include_closed": "true"})
        self.assertEqual(params, [[1, 2]])

    def test_closed_types_are_deduplicated(self):
        # stageType=2 + include_closed -> [1, 2] (2 not repeated).
        where, params = _build_where({"stageType": "2", "include_closed": "true"})
        self.assertEqual(params, [[1, 2]])

    def test_flag_composes_with_the_search_filter(self):
        where, params = _build_where({
            "stageType": "0",
            "include_closed": "true",
            "filterValue": "Ponderosa",
        })
        joined = " AND ".join(where)
        self.assertIn("o.title ILIKE %s", joined)
        self.assertIn("o.stage_type = ANY(%s)", joined)
        # 6 search placeholders + the stage-type array.
        self.assertEqual(len(params), 7)
        self.assertEqual(params[-1], [0, 1, 2])

    def test_flag_composes_with_the_ids_filter(self):
        where, params = _build_where({
            "stageType": "0",
            "include_closed": "true",
            "ids": "1,2,3",
        })
        joined = " AND ".join(where)
        self.assertIn("o.id = ANY(%s)", joined)
        self.assertIn("o.stage_type = ANY(%s)", joined)
        # The stage-type filter is applied before the ids filter, so its
        # array is bound first.
        self.assertEqual(params[0], [0, 1, 2])
        self.assertEqual(params[1], [1, 2, 3])


# ── The flag parser itself ──────────────────────────────────────────────────

class TestQueryBoolHelper(unittest.TestCase):
    def _call(self, qs):
        return server._query_bool(qs, "include_closed", "includeClosed")

    def test_missing_key_is_false(self):
        self.assertFalse(self._call({}))

    def test_true_spellings(self):
        for v in ("1", "true", "True", "TRUE", " yes ", "on", "ON"):
            with self.subTest(v=v):
                self.assertTrue(self._call({"include_closed": v}))

    def test_false_spellings(self):
        for v in ("0", "false", "False", "no", "off", "", "maybe"):
            with self.subTest(v=v):
                self.assertFalse(self._call({"include_closed": v}))

    def test_list_values_from_parse_qs_are_unwrapped(self):
        self.assertTrue(self._call({"include_closed": ["true"]}))
        self.assertFalse(self._call({"include_closed": ["false"]}))

    def test_first_matching_alias_wins(self):
        # _query_bool returns on the FIRST key that is present, so the
        # snake_case alias takes precedence over the camelCase one. This
        # keeps the behaviour deterministic when a caller sends both.
        self.assertFalse(self._call({"include_closed": "false", "includeClosed": "true"}))
        self.assertTrue(self._call({"include_closed": "true", "includeClosed": "false"}))


# ── Frontend guards (source-level) ──────────────────────────────────────────

class TestFrontendLinkPopupGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        cls.html_src = (HERE.parent / "public" / "index.html").read_text(encoding="utf-8")
        cls.css_src = (HERE.parent / "public" / "styles.css").read_text(encoding="utf-8")

    def _fn(self, name):
        m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
                      self.app_src)
        self.assertIsNotNone(m, f"{name} not found in app.js")
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
        return self.app_src[start:i]

    def test_popup_has_the_show_closed_checkbox(self):
        self.assertIn('id="mail-link-deal-include-closed"', self.html_src)
        self.assertIn("Show closed", self.html_src)

    def test_checkbox_is_inside_the_link_dropdown(self):
        block = self.html_src[
            self.html_src.index('id="mail-link-deal-dropdown"'):][:1200]
        self.assertIn('id="mail-link-deal-include-closed"', block)
        # It must be a checkbox and must not be pre-checked.
        self.assertIn('type="checkbox"', block)
        self.assertNotIn('id="mail-link-deal-include-closed" checked', block)

    def test_search_passes_include_closed_when_checked(self):
        body = self._fn("searchDealsForMailLink")
        self.assertIn('#mail-link-deal-include-closed', body)
        self.assertIn("includeClosed", body)

    def test_search_helper_sends_include_closed_to_the_api(self):
        body = self._fn("searchOpportunitiesByTitle")
        self.assertIn("include_closed", body)
        self.assertIn('params.set("include_closed", "true")', body)
        # The default must stay closed-excluded - the option is opt-in, so
        # every other caller of searchOpportunitiesByTitle is unaffected.
        self.assertIn("includeClosed = false", self.app_src,
                      "searchOpportunitiesByTitle must default includeClosed to false")

    def test_toggling_the_checkbox_re_runs_the_search(self):
        # The checkbox is bound once, next to the search-input listener.
        self.assertIn('const dealIncludeClosed = $("#mail-link-deal-include-closed");',
                      self.app_src)
        at = self.app_src.index('const dealIncludeClosed = $("#mail-link-deal-include-closed");')
        region = self.app_src[at:at + 1500]
        self.assertIn("dealIncludeClosed.addEventListener", region)
        self.assertIn("searchDealsForMailLink(q)", region)

    def test_closed_results_render_a_closed_badge(self):
        body = self._fn("searchDealsForMailLink")
        self.assertIn("mail-link-deal-closed-badge", body)
        self.assertIn('badge.textContent = "Closed"', body)

    def test_open_results_are_unchanged(self):
        # The else-branch still assigns the title as plain text.
        body = self._fn("searchDealsForMailLink")
        self.assertIn("b.textContent = opp.title;", body)

    def test_closed_badge_is_styled(self):
        self.assertIn(".mail-link-deal-closed-badge", self.css_src)
        self.assertIn(".mail-link-deal-closed-toggle", self.css_src)

    def test_link_endpoint_is_unchanged_for_closed_projects(self):
        # Linking uses the same endpoint regardless of closed/open - the
        # selected id is what matters, not the stage type.
        at = self.app_src.index('const linkSubmitBtn = $("#mail-link-deal-submit")')
        region = self.app_src[at:at + 2500]
        self.assertIn("/api/v2/mail/messages/${mid}/link", region)
        self.assertIn("opportunityId: Number(dealId)", region)
        # ... and there is no closed-specific branch in the link path.
        self.assertNotIn("isClosed", region)

    def test_results_are_reset_when_the_popup_resets(self):
        body = self._fn("resetLinkDealDropdown")
        self.assertIn("#mail-link-deal-include-closed", body)
        self.assertIn("ic.checked = false", body)


# ── Frontend guards for the bookmark preview project ID (Fix 2) ─────────────

class TestBookmarkPreviewProjectIdGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")

    def _fn(self, name):
        m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
                      self.app_src)
        self.assertIsNotNone(m, f"{name} not found in app.js")
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
        return self.app_src[start:i]

    def test_shared_badge_renderer_exists(self):
        self.assertIn("function renderProjectNumberBadge(", self.app_src)

    def test_badge_uses_the_same_format_as_the_project_page(self):
        body = self._fn("renderProjectNumberBadge")
        # `#<project_number>` - the same format the project preview modal
        # used before the helper was extracted.
        self.assertIn("`#${projNum}`", body)
        self.assertIn("opp-project-number-badge", body)

    def test_badge_falls_back_to_the_opportunity_id(self):
        body = self._fn("renderProjectNumberBadge")
        self.assertIn("projectNumber || oppId", body)

    def test_badge_inserts_before_the_header_actions(self):
        body = self._fn("renderProjectNumberBadge")
        self.assertIn("bookmark-preview-actions", body)
        self.assertIn("insertBefore(badge, actionsEl)", body)

    def test_project_preview_modal_uses_the_shared_renderer(self):
        body = self._fn("openOpportunityPreviewModal")
        self.assertIn("renderProjectNumberBadge(titleEl, data.opp.project_number, id)", body)

    def test_bookmark_preview_shows_the_project_number(self):
        # Cached path, freshly-fetched path and the refresh path must all
        # render the badge.
        body = self._fn("activateBookmarkTab")
        self.assertIn("renderProjectNumberBadge(titleEl, deal._cachedData.opp?.project_number, id)", body)
        self.assertIn("renderProjectNumberBadge(titleEl, data.opp?.project_number, id)", body)

        refresh = self._fn("refreshBookmarkTab")
        self.assertIn("renderProjectNumberBadge(titleEl, data.opp?.project_number, id)", refresh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
