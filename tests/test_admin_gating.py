"""Unit tests for basic admin gating (is_admin is the single source of truth).

Context
-------
Admin gating used to be split across two different checks:

  * `_require_admin()`        -> checks the `is_admin` boolean; guarded 109 endpoints.
  * `_require_strict_admin()` -> checked `role == 'admin'`; guarded exactly the two
    password-reset handlers.

An account with is_admin=true but role='member' therefore rendered the entire admin
console (is_admin gate passes) yet got "Forbidden: requires one of roles: admin" on
password reset. There was no intentional extra strictness, so gating is now uniformly
`_require_admin` and `role` is a descriptive/feature field only.

The second half of the fix closes the drift hole in `_handle_user_update`: the old
generic field loop wrote `is_admin` on its own whenever a payload carried `isAdmin`
but no `role`, leaving the two columns disagreeing (e.g. is_admin=false with
role='admin'). `role` is now authoritative and `is_admin` is always derived from it.

These are source-level guards: they pin the invariant without needing a live DB, so
they run in CI.

Run with: python3 tests/test_admin_gating.py
"""

import re
import unittest
from pathlib import Path

SERVER_PY = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")


def _handler(name: str, src: str = SERVER_PY, span: int = 3000) -> str:
    """Return the body of a method definition, by name."""
    start = src.index(f"def {name}(")
    return src[start:start + span]


class TestSingleAdminGate(unittest.TestCase):
    """There must be exactly one admin gate."""

    def test_role_based_admin_helpers_are_gone(self):
        """A second, stricter admin gate must not creep back in."""
        for gone in ("def _require_strict_admin(", "def _require_role("):
            self.assertNotIn(gone, SERVER_PY, f"{gone} reintroduced a divergent admin gate")

    def test_no_caller_uses_removed_helpers(self):
        # Strip comments so the explanatory NOTE about the removal isn't a false hit.
        code = "\n".join(
            line for line in SERVER_PY.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("_require_strict_admin", code)
        self.assertNotIn("_require_role", code)

    def test_reset_password_uses_require_admin(self):
        body = _handler("_handle_user_reset_password")
        self.assertIn("_require_admin(self)", body)

    def test_reset_password_send_uses_require_admin(self):
        body = _handler("_handle_user_reset_password_send")
        self.assertIn("_require_admin(self)", body)

    def test_every_admin_endpoint_uses_the_same_gate(self):
        """Sweep: no /api/v2/admin/ handler may gate on anything but _require_admin."""
        for match in re.finditer(r'if api_path == "/api/v2/admin/[a-z0-9/_-]+"', SERVER_PY):
            body = SERVER_PY[match.start():match.start() + 600]
            gated = re.search(r"user = (_require\w+)\(", body)
            if gated:
                self.assertEqual(
                    gated.group(1), "_require_admin",
                    f"{match.group(0)} gates on {gated.group(1)}, not _require_admin")


class TestRoleIsAuthoritative(unittest.TestCase):
    """is_admin must always be derived from role, never written independently."""

    def _update_body(self) -> str:
        return _handler("_handle_user_update", span=4000)

    def test_generic_field_loop_no_longer_writes_is_admin(self):
        """The old loop mapped isAdmin -> is_admin, allowing an independent write."""
        body = self._update_body()
        generic = body[body.index("sets, params = [], []"):body.index("# Email uniqueness check")]
        self.assertNotIn('("isAdmin", "is_admin")', generic)

    def test_update_derives_is_admin_from_role(self):
        body = self._update_body()
        self.assertIn('role_write in ("admin", "manager")', body)
        self.assertIn('sets.append("is_admin = %s")', body)

    def test_legacy_isadmin_alone_still_moves_role(self):
        """A caller sending only isAdmin must not be able to strand role."""
        body = self._update_body()
        self.assertIn('elif "isAdmin" in payload:', body)

    def test_invalid_role_is_rejected(self):
        body = self._update_body()
        self.assertIn("Invalid role", body)

    def test_create_still_derives_is_admin_from_role(self):
        create = _handler("_handle_user_create")
        self.assertIn('is_admin = role in ("admin", "manager")', create)


if __name__ == "__main__":
    unittest.main(verbosity=2)