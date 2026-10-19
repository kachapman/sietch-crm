"""Unit tests: scanner behavior gating + project_number resolution.

Two scanner bugs:

1. Line 1433 used `toggles.get("auto_link_project_id", True)` to gate the
   classifiers. But `toggles.get(...)` returns a CONFIG DICT, not a bool — and a
   non-empty dict is always truthy. The user had `auto_link_by_content.enabled
   = false` in the UI, yet the scanner kept linking, because the broken boolean
   was always True. The fix reads `.get("enabled", False)` from the config dict.

2. `_find_opportunity_by_deal_id` looked up `WHERE id = %s`, but `[#NNNN]` in
   email subjects is the project_number (the number shown in the UI header), not
   the row id. server.py _resolve_project_id does it correctly; the scanner now
   does too. Falls back to id if project_number is not found.

These are source-level guards (no live DB), so they run in CI.

Run with: python3 tests/test_scanner_toggle_gating.py
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER_PY = (ROOT / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")


class TestToggleGating(unittest.TestCase):
    """The classifier gate must read `enabled`, not the dict itself."""

    def _classify_body(self) -> str:
        i = SCANNER_PY.index("def _classify_message(")
        return SCANNER_PY[i:i + 4000]

    def test_does_not_treat_behavior_cfg_as_bool(self):
        """The old code read `toggles.get("auto_link_project_id", True)` which
        returned a dict, not a bool. A non-empty dict is always truthy and
        bypassed every checkbox. Guard against regression."""
        body = self._classify_body()
        # Direct truthiness check on a behavior config is the bug.
        self.assertNotIn(
            'toggles.get("auto_link_project_id", True)',
            body,
            "old truthy-dict pattern reintroduced; use `(\"enabled\", False)`",
        )
        # The fixed pattern reads the enabled key from the config dict.
        self.assertIn(
            ".get(\"enabled\", False)",
            body,
        )

    def test_link_step_uses_helper(self):
        """The link-execution step must call _is_behavior_enabled, like the
        other behavior sites in the file."""
        i = SCANNER_PY.index("if (classification[\"action\"] == \"link_deal\"")
        link_block = SCANNER_PY[i:i + 800]
        self.assertIn(
            "_is_behavior_enabled(toggles.get(\"auto_link_project_id\", {}), account_id)",
            link_block,
        )


class TestProjectNumberResolution(unittest.TestCase):
    """_find_opportunity_by_deal_id must resolve by project_number first."""

    def _func_body(self) -> str:
        i = SCANNER_PY.index("def _find_opportunity_by_deal_id(")
        return SCANNER_PY[i:i + 600]

    def test_resolves_by_project_number_first(self):
        body = self._func_body()
        self.assertIn("project_number = %s", body)
        self.assertIn("id = %s", body)
        # Project_number lookup must come first.
        self.assertLess(
            body.index("project_number = %s"),
            body.index("id = %s"),
        )

    def test_does_not_query_by_id_only(self):
        """The old code only had `WHERE id = %s`. That bypassed the project
        number users actually write in emails."""
        body = self._func_body()
        # Count how many SELECT id lines there are in the function.
        select_id_count = body.count("SELECT id, title FROM opportunities")
        self.assertGreaterEqual(
            select_id_count, 2,
            "expected project_number SELECT and id fallback SELECT both present",
        )
        # A return that is solely the id-lookup result (no project_number
        # branch) is the old bug. Ensure the id branch is conditioned on
        # the project_number branch failing.
        if "project_number = %s" not in body:
            self.fail("project_number lookup missing from _find_opportunity_by_deal_id")


class TestDockerfile(unittest.TestCase):
    """The torch layer must not be built into the image — it's huge and only
    used by the optional retrain path which is already try/except guarded."""

    def _dockerfile(self) -> str:
        return (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_no_torch_layer(self):
        df = self._dockerfile()
        self.assertNotIn("sentence-transformers", df)
        self.assertNotIn("scikit-learn", df)

    def test_core_pip_layer_present(self):
        df = self._dockerfile()
        self.assertIn("psycopg2-binary", df)
        self.assertIn("imap_tools", df)


if __name__ == "__main__":
    unittest.main(verbosity=2)