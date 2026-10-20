"""Regression tests for the branding header fix (2026-10-20).

Bug: changing Company Name did nothing visible on the dashboard. Root
causes, all verified against the live droplet:

1. `applyBranding()` never applied `headerTitle` anywhere — the
   `.hero-title` ("Workspace dashboard") stayed hardcoded even though the
   branding tab has a Header Title field for it.
2. A stale stored `header_eyebrow = "Sietch CRM"` shadowed Company Name
   ("Vanguard Adjusting") via `headerEyebrow || companyName`, so the eyebrow
   above the hero title never moved.
3. The admin form could never clear the stale value: empty sent null and the
   server's `COALESCE(%s, header_eyebrow)` kept the old row.
4. The server GET defaulted an unset eyebrow to "Sietch CRM", which shadowed
   Company Name a second time even with a clean row.

Contract after the fix:
- `.eyebrow` <- headerEyebrow (explicit) else companyName else hardcoded default
- `.hero-title` <- headerTitle (sanitized em/strong/span) else hardcoded default
- `.sietch-name` version line + footer stay static, never touched
- Empty form fields clear back to default ("" -> NULL on the server)

Source-level guards, no live DB needed.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
APPJS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "public" / "index.html").read_text(encoding="utf-8")


class TestBrandingApplies(unittest.TestCase):
    def _apply_body(self) -> str:
        i = APPJS.index("function applyBranding(branding)")
        return APPJS[i: i + 2500]

    def test_eyebrow_falls_back_to_company_name(self):
        body = self._apply_body()
        self.assertIn("branding.headerEyebrow || branding.companyName", body)

    def test_hero_title_applied_from_its_own_field(self):
        body = self._apply_body()
        self.assertIn(".hero-title", body)
        self.assertIn("branding.headerTitle", body)
        # Company Name must never drive the hero title (it has its own field).
        # Check the hero-title assignment statement itself, not the whole
        # function body (noscript legitimately uses companyName after it).
        m = re.search(r"heroTitle\.innerHTML\s*=\s*([^\n;]+)", body)
        self.assertIsNotNone(m, "hero-title assignment not found")
        self.assertNotIn("branding.companyName", m.group(1))

    def test_sanitizer_exists_and_is_used(self):
        self.assertIn("function sanitizeBrandingHtml", APPJS)
        body = self._apply_body()
        self.assertIn("sanitizeBrandingHtml(branding.headerTitle)", body)

    def test_version_line_and_footer_untouched(self):
        body = self._apply_body()
        self.assertNotIn("sietch-name", body)
        self.assertNotIn("sietch-footer", body)
        self.assertNotIn("app-version", body)


class TestBrandingServerContract(unittest.TestCase):
    def test_get_returns_nullable_eyebrow(self):
        # The old default ("Sietch CRM") shadowed Company Name a second time
        # even with a clean row. The client fallback must be able to run.
        self.assertIn('"headerEyebrow": row["header_eyebrow"] or None', SERVER)

    def test_post_supports_clearing(self):
        self.assertIn("__keep__", SERVER)
        self.assertIn("_clearable", SERVER)


class TestBrandingForm(unittest.TestCase):
    def test_eyebrow_hint_documents_precedence(self):
        self.assertIn("Defaults to Company Name", HTML)

    def test_save_sends_empty_string_for_clearables(self):
        i = APPJS.index("async function saveBranding()")
        block = APPJS[i: i + 1500]
        # Empty must send "" (clear), not null (which the old server kept).
        self.assertIn('headerEyebrow: $("#branding-header-eyebrow").value.trim()', block)
        self.assertNotIn('headerEyebrow: $("#branding-header-eyebrow").value.trim() || null', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
