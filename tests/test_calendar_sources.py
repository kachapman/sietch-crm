"""Unit tests for calendar Phase 5 (sources/sync/settings wiring).

No DB required: user_profile_store imports cleanly; server.py surface is
pinned via source-level guards (needs a live socket end-to-end).

Run with: python3 tests/test_calendar_sources.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from user_profile_store import _clean_calendar_display, _clean_calendar_tiles  # noqa: E402


class TestCalendarDisplayReminder(unittest.TestCase):
    def test_legacy_default_is_15(self):
        self.assertEqual(_clean_calendar_display({})["defaultReminder"], 15)

    def test_explicit_none_survives(self):
        self.assertIsNone(_clean_calendar_display({"defaultReminder": None})["defaultReminder"])
        self.assertIsNone(_clean_calendar_display({"defaultReminder": ""})["defaultReminder"])

    def test_valid_value_kept(self):
        self.assertEqual(_clean_calendar_display({"defaultReminder": 30})["defaultReminder"], 30)
        self.assertEqual(_clean_calendar_display({"defaultReminder": "60"})["defaultReminder"], 60)

    def test_out_of_range_becomes_none(self):
        self.assertIsNone(_clean_calendar_display({"defaultReminder": 99999})["defaultReminder"])


class TestOnlySourceId(unittest.TestCase):
    def test_preserved_when_numeric(self):
        tiles = _clean_calendar_tiles([{"id": "c1", "onlySourceId": 7}])
        self.assertEqual(tiles[0]["onlySourceId"], 7)

    def test_string_digits_coerced(self):
        tiles = _clean_calendar_tiles([{"id": "c1", "onlySourceId": "7"}])
        self.assertEqual(tiles[0]["onlySourceId"], 7)

    def test_absent_when_missing_or_invalid(self):
        self.assertNotIn("onlySourceId", _clean_calendar_tiles([{"id": "c1"}])[0])
        self.assertNotIn("onlySourceId", _clean_calendar_tiles([{"id": "c1", "onlySourceId": "x"}])[0])


class TestServerWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        cls.app = (HERE.parent / "public" / "app.js").read_text(encoding="utf-8")

    def test_has_password_actually_selected(self):
        # Regression: has_password was always False (column never selected).
        self.assertGreaterEqual(self.src.count("caldav_password_encrypted"), 2)

    def test_oauth_flags_exposed(self):
        self.assertIn('"oauth"', self.src)
        self.assertIn("GOOGLE_CLIENT_ID", self.src)

    def test_hidden_sources_filtered_from_range_query(self):
        self.assertIn("cs.visible", self.src)

    def test_import_endpoint_wired(self):
        self.assertIn("/api/v2/calendar/import", self.src)
        self.assertIn("_handle_calendar_import", self.src)

    def test_tile_filter_param_used(self):
        self.assertIn("calendarTileSourceParam", self.app)
        self.assertIn("source_id", self.app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
