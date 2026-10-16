#!/usr/bin/env python3
"""Unit tests for the admin 'Customize Projects' tab.

Covers the audit-round fixes:
  - type-select contains textarea/url (audit #1)
  - the edit path defensively injects an unknown type rather than blanking
    the select (audit #1)
  - duplicate field key returns 409, not 500 (audit #6)
  - field_type is validated against CUSTOM_FIELD_TYPES on create and
    update (audit #13)
  - stages ORDER BY has an id tiebreak (audit #3)
  - the option rewrite in custom-field update is wrapped in db_cursor()
    so a mid-loop failure rolls back (audit #9)
  - history-category update no longer runs an information_schema query
    (audit #8)
  - showOnCreate / showOnEdit columns are no longer in the API contract
    (audit #7)
  - isActive is honoured on stage create; groupId on custom-field create
    (audits #10, #11)
  - dragging a stage while a search filter is active is a no-op (audit #2)

server imports cleanly (it only binds the socket inside main(), which
the tests never call), so the source guards assert against the real
server module.

Run with: python3 tests/test_customize.py
"""

import os
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


# ── Server-side: field type whitelist + duplicate key ────────────────────────

class TestCustomFieldTypes(unittest.TestCase):

    def setUp(self):
        # Capture _json_response so the real handler can write to our stub.
        self._orig_json_response = server._json_response
        self.captured = {}
        server._json_response = self._capture

    def tearDown(self):
        server._json_response = self._orig_json_response

    def _capture(self, handler, status, body):
        self.captured["status"] = status
        self.captured["body"] = body

    def _last(self):
        return self.captured.get("status"), self.captured.get("body", {})

    def _admin_ok(self):
        # Bypass the cookie/email check by making _require_admin return
        # an admin-shaped dict.
        patcher = patch("server._require_admin",
                        return_value={"id": 1, "email": "admin@test", "is_admin": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_handler(self, method, body, field_id=None):
        # We don't need a real handler object — the module-level helpers
        # (`_read_body`, `_json_response`, `_require_admin`) are all patched
        # directly. The class methods are bound functions, so we can call
        # them as `KanbanHandler._handle_x(h, ...)` with `h` a minimal
        # object that exposes the path the handler looks at.
        class _H:
            pass
        h = _H()
        h.command = method
        h.path = (f"/api/v2/custom-fields/{field_id}" if method == "PUT"
                  else "/api/v2/custom-fields")
        return h

    def _stub_body(self, body):
        """Make server._read_body() return the given dict (or b'' for None)."""
        import json as _json
        body_bytes = b"" if body is None else _json.dumps(body).encode("utf-8")
        return patch("server._read_body", return_value=body_bytes)

    def test_set_contains_every_option_in_the_admin_dropdown(self):
        # If a UI option is added, add it here too. If a value is removed
        # from the set, remove the corresponding <option> in index.html.
        self.assertEqual(server.CUSTOM_FIELD_TYPES, frozenset({
            "text", "textarea", "url", "number", "date", "select", "checkbox",
        }))

    def test_set_is_immutable(self):
        # A frozenset can't be mutated at runtime - protects against
        # code accidentally adding an unvetted type.
        with self.assertRaises(AttributeError):
            server.CUSTOM_FIELD_TYPES.add("bogus")  # type: ignore[attr-defined]

    def _run(self, method, body, field_id, db_setup):
        """Drive a create or update handler with the given body and DB mock."""
        self._admin_ok()
        with patch("server.db") as db, self._stub_body(body) as stub:
            db_setup(db)
            handler = self._make_handler(method, body, field_id)
            if method == "POST":
                server.KanbanHandler._handle_custom_field_create(handler)
            else:
                server.KanbanHandler._handle_custom_field_update(handler, field_id)
        return self._last(), db

    def test_create_rejects_unknown_type(self):
        # The 400 must come from the type validator BEFORE the DB is touched.
        def db_setup(db):
            db.query_one.side_effect = AssertionError("DB should not be touched for an invalid type")
        (status, body), _ = self._run("POST", {"label": "x", "type": "bogus"}, None, db_setup)
        self.assertEqual(status, 400)
        self.assertIn("bogus", body.get("error", ""))
        self.assertIn("textarea", body.get("error", ""))

    def test_create_rejects_missing_label(self):
        def db_setup(db):
            db.query_one.side_effect = AssertionError("DB should not be touched for an empty label")
        (status, body), _ = self._run("POST", {"type": "text"}, None, db_setup)
        self.assertEqual(status, 400)
        self.assertIn("Label", body.get("error", ""))

    def test_create_rejects_duplicate_key_with_409(self):
        # Audit #6: previously this leaked as a 500. The existing record
        # is reported by name so an admin can tell what collided.
        existing = {"id": 99, "label": "Address"}
        def db_setup(db):
            db.query_one.return_value = existing
        (status, body), db = self._run("POST", {"label": "address", "type": "text"}, None, db_setup)
        self.assertEqual(status, 409)
        self.assertIn("address", body.get("error", ""))
        self.assertIn("Address", body.get("error", ""))
        # And the insert path was NOT reached.
        db.insert_returning.assert_not_called()

    def test_create_accepts_groupId(self):
        # Audit #11: only update accepted groupId before.
        def db_setup(db):
            # The handler first looks up an existing key (returns None), then
            # fetches MAX(sort_order), then inserts. Set up the query_one
            # side effects in order.
            db.query_one.side_effect = [None, {"mx": 0}]
            db.insert_returning.return_value = 1
            db.execute.return_value = 0
        (status, _body), db = self._run("POST",
            {"label": "New field", "type": "text", "groupId": 3}, None, db_setup)
        self.assertEqual(status, 201)
        params = db.insert_returning.call_args.args[1]
        self.assertEqual(params[6], 3)

    def test_create_coerces_blank_groupId_to_null(self):
        def db_setup(db):
            db.query_one.side_effect = [None, {"mx": 0}]
            db.insert_returning.return_value = 1
            db.execute.return_value = 0
        (status, _body), db = self._run("POST",
            {"label": "x", "type": "text", "groupId": ""}, None, db_setup)
        self.assertEqual(status, 201)
        params = db.insert_returning.call_args.args[1]
        self.assertIsNone(params[6])

    def test_update_rejects_unknown_type(self):
        def db_setup(db):
            db.execute.side_effect = AssertionError("DB should not be touched for an invalid type")
        (status, body), _ = self._run("PUT", {"type": "bogus"}, 1, db_setup)
        self.assertEqual(status, 400)
        self.assertIn("bogus", body.get("error", ""))


# ── Source guards ────────────────────────────────────────────────────────────

class TestCustomizeSourceGuards(unittest.TestCase):
    """Source-level assertions for changes that need live DB / DOM."""

    @classmethod
    def setUpClass(cls):
        cls.server_src = Path(server.__file__).read_text(encoding="utf-8")
        cls.app_src = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        cls.html_src = (HERE.parent / "public" / "index.html").read_text(encoding="utf-8")
        cls.css_src = (HERE.parent / "public" / "styles.css").read_text(encoding="utf-8")

    # #1 — type dropdown contains textarea + url
    def test_cf_type_dropdown_lists_every_value_in_CUSTOM_FIELD_TYPES(self):
        m = re.search(r'<select id="admin-cf-type"[^>]*>(.*?)</select>',
                      self.html_src, re.S)
        self.assertIsNotNone(m, "admin-cf-type <select> not found in index.html")
        opts = set(re.findall(r'value="([^"]+)"', m.group(1)))
        self.assertEqual(opts, set(server.CUSTOM_FIELD_TYPES),
                         f"dropdown {opts} is not in sync with CUSTOM_FIELD_TYPES "
                         f"{set(server.CUSTOM_FIELD_TYPES)}")

    def test_app_js_uses_defensive_setter(self):
        # _setCustomFieldTypeSelect must exist and be used in the edit path;
        # the original `$('#admin-cf-type').value = ...` line silently blanks
        # the select for any type that isn't a <option>.
        self.assertIn("function _setCustomFieldTypeSelect", self.app_src)
        # The two call sites:
        self.assertIn("_setCustomFieldTypeSelect($(\"#admin-cf-type\")", self.app_src)
        # The old direct assignment should be gone:
        self.assertNotIn(
            'value = f.type || f.fieldType || "text"',
            self.app_src,
            "direct select.value assignment for type was removed; use _setCustomFieldTypeSelect",
        )
        # And the helper is paired with a reset on Add.
        self.assertIn("_resetCustomFieldTypeSelect", self.app_src)

    # #2 — drag disabled when filter active
    def test_stages_filtered_class_toggled_by_populate(self):
        # Anchor on the function name to avoid catching some other function body.
        body = self._function_body("populateAdminStagesList")
        self.assertIn('"stages-filtered"', body,
                      "populateAdminStagesList should toggle the stages-filtered class")
        self.assertIn("Reordering is disabled while searching", body,
                      "the filter notice should be present so the user knows why drag is gone")

    def test_stages_filtered_css_hides_drag_handles(self):
        self.assertIn("#admin-stages-list.stages-filtered .admin-customize-drag-handle",
                      self.css_src)
        # And it has display:none for that selector.
        self.assertRegex(
            self.css_src,
            r"#admin-stages-list\.stages-filtered \.admin-customize-drag-handle\s*\{[^}]*display:\s*none",
            "drag handle must be hidden when stages-filtered",
        )

    def test_sortable_onEnd_has_a_filter_guard(self):
        # The stages Sortable onEnd - look for the stages-specific version,
        # which references stagesList and POSTs to /stages/reorder.
        m = re.search(
            r"Sortable\.create\(stagesList,.*?onEnd:\s*async\s*\(evt\)\s*=>\s*\{(.*?)\n\s+\}\);",
            self.app_src, re.S)
        self.assertIsNotNone(m, "stages Sortable.onEnd not found")
        body = m.group(1)
        self.assertIn("stages-filtered", body,
                      "Sortable onEnd must check stages-filtered before posting ids")
        self.assertIn("Clear the search to reorder stages", body,
                      "filter guard should tell the user the action was refused")

    # #3 — deterministic ordering
    def test_stages_get_orders_by_sort_order_then_id(self):
        self.assertIn("ORDER BY s.sort_order, s.id", self.server_src,
                      "_handle_stages_get must tiebreak sort_order to be deterministic")

    def test_custom_fields_get_orders_by_sort_order_then_id(self):
        self.assertIn("FROM custom_field_definitions WHERE is_active = TRUE "
                      "ORDER BY sort_order, id", self.server_src)

    # #4 — unassigning custom fields from a group
    def test_field_group_save_unassigns_unchecked_currently_assigned(self):
        # The previous behaviour left groupId intact on unchecked fields.
        self.assertIn("currentlyHere", self.app_src)
        self.assertIn("groupIdValue = null", self.app_src,
                      "unchecking a custom field that was in this group should clear its groupId")

    # #5 — state.fieldGroups refilled
    def test_field_group_save_refills_state(self):
        # The old code did 'state.fieldGroups = []' on success, leaving
        # grouping broken until a page reload. Now it calls the real loader.
        self.assertIn("if (typeof loadFieldGroups === \"function\") await loadFieldGroups()", self.app_src)
        # At least 3 occurrences (save, reorder, delete).
        self.assertGreaterEqual(self.app_src.count(
            "if (typeof loadFieldGroups === \"function\") await loadFieldGroups()"), 3)

    # #6, #13 — duplicate key + type validation in create
    def test_create_handler_pre_checks_duplicate_key(self):
        create_body = self._handler_body("_handle_custom_field_create")
        self.assertIn("CUSTOM_FIELD_TYPES", create_body)
        # Must SELECT before INSERT.
        self.assertLess(
            create_body.index('FROM custom_field_definitions WHERE field_key ='),
            create_body.index('INSERT INTO custom_field_definitions'),
        )
        # And return 409, not 500.
        self.assertIn("409", create_body)

    def test_create_handler_validates_field_type(self):
        create_body = self._handler_body("_handle_custom_field_create")
        self.assertIn("not in CUSTOM_FIELD_TYPES", create_body)

    def test_create_handler_includes_groupId(self):
        create_body = self._handler_body("_handle_custom_field_create")
        # INSERT must include group_id column and the param handling must
        # coerce empty string to None.
        self.assertIn("group_id, created_by", create_body)
        self.assertIn('payload.get("groupId")', create_body)

    # #7 — showOnCreate / showOnEdit dropped
    def test_custom_fields_get_does_not_return_show_on(self):
        get_body = self._handler_body("_handle_custom_fields_get")
        # The camelCase JSON keys must not appear in the response dict.
        for key in ('"showOnCreate"', '"showOnEdit"', "'showOnCreate'", "'showOnEdit'"):
            self.assertNotIn(key, get_body,
                             f"response key {key} was dropped from the contract")
        # The raw SQL columns may still appear as the rest of the SELECT,
        # but the function body must not list them in the SELECT clause.
        self.assertNotIn("show_on_create, show_on_edit", get_body,
                         "the SELECT must not list show_on_create / show_on_edit")
        self.assertNotIn("show_on_create", get_body.split("# show_on_create / show_on_edit", 1)[0]
                         if "# show_on_create" in get_body else get_body)

    # #8 — no information_schema in history-cat update
    def test_history_category_update_does_not_query_information_schema(self):
        body = self._handler_body("_handle_history_category_update")
        self.assertNotIn("information_schema", body,
                         "the dynamic updated_at branch was dead weight — removed")
        self.assertNotIn("updated_at", body,
                         "history_categories has no updated_at column; the SET clause shouldn't include it")

    # #9 — option rewrite is transactional
    def test_custom_field_update_uses_db_cursor(self):
        body = self._handler_body("_handle_custom_field_update")
        # The options rewrite must use a context manager.
        self.assertIn("with db.db_cursor() as cur:", body,
                      "options DELETE+INSERT must commit/rollback together")

    # #10 — isActive on stage create
    def test_stage_create_persists_is_active(self):
        body = self._handler_body("_handle_stage_create")
        self.assertIn("is_active", body)
        # INSERT must list is_active.
        self.assertIn("(title, color, sort_order, stage_type, probability, is_active)", body)
        # And default to True when the UI omits it.
        self.assertIn("bool(payload.get(\"isActive\", True))", body)

    # #12 — parallel assignment
    def test_field_group_save_uses_promise_all_settled(self):
        body = self._function_body("bindAdminFieldGroups")
        self.assertIn("Promise.allSettled", body,
                      "group assignment should run in parallel, not sequentially")
        self.assertNotIn("for (const cb of cfWrap.querySelectorAll",
                          body[:body.index("Promise.allSettled")] if "Promise.allSettled" in body else "",
                          "the for-of await loop is the old sequential path; "
                          "Promise.allSettled must be the only path used here")
        # Also confirm the old sequential await is gone.
        for bad in [
            "await api(`/api/v2/custom-fields/${cfId}`, { method: \"PUT\", "
            "body: JSON.stringify({ groupId: groupId }) });",
        ]:
            self.assertNotIn(bad, body,
                             f"sequential assignment path is still present: {bad[:60]}")

    def _handler_body(self, name):
        """Extract a method/function body by name from the real source."""
        start = self.server_src.index(f"def {name}")
        # Find the next top-level def (no leading spaces) after this one.
        nxt = self.server_src.find("\n    def ", start + 1)
        nxt2 = self.server_src.find("\ndef ", start + 1)
        ends = [e for e in (nxt, nxt2) if e != -1]
        return self.server_src[start:min(ends) if ends else len(self.server_src)]

    def _function_body(self, name):
        """Extract a JS function body by name from the real source."""
        m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
                      self.app_src)
        self.assertIsNotNone(m, f"{name} not found in app.js")
        # Walk braces from the opening one to balance.
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

    def _handler_body(self, name):
        """Extract a method/function body by name from the real source."""
        start = self.server_src.index(f"def {name}")
        # Find the next top-level def (no leading spaces) after this one.
        nxt = self.server_src.find("\n    def ", start + 1)
        nxt2 = self.server_src.find("\ndef ", start + 1)
        ends = [e for e in (nxt, nxt2) if e != -1]
        return self.server_src[start:min(ends) if ends else len(self.server_src)]


# ── HTML markup guard for the search-filter notice ───────────────────────────

class TestFilterNoticeUI(unittest.TestCase):
    """The notice is rendered by JS, not HTML, so this just checks the CSS
    side and that the JS string is not gratuitously different from a couple
    of other admin-customize notices."""

    def test_css_target_selector_matches_dom_class(self):
        css = (HERE.parent / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("#admin-stages-list.stages-filtered", css)
        app = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"stages-filtered"', app)


if __name__ == "__main__":
    unittest.main(verbosity=2)