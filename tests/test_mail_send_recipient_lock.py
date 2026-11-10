#!/usr/bin/env python3
"""Offline tests for the per-user recipient lock (Ken 1:41 PM)."""
import sys, unittest, uuid
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import network_guard; network_guard.install()
import mail_recipient_lock as L

class TestPure(unittest.TestCase):
    def test_exact_allowed(self):
        self.assertEqual(L.check_lock("crm@vanguardadj.online",
            ["crm@vanguardadj.online"]), (True, ""))
    def test_other_refused(self):
        ok, why = L.check_lock("crm@vanguardadj.online", ["x@example.invalid"])
        self.assertFalse(ok); self.assertIn("not the locked", why)
    def test_extra_refused(self):
        ok, _ = L.check_lock("crm@vanguardadj.online",
            ["crm@vanguardadj.online", "x@example.invalid"])
        self.assertFalse(ok)
    def test_cc_bcc_refused(self):
        self.assertFalse(L.check_lock("crm@vanguardadj.online",
            L.parse_recipients("crm@vanguardadj.online", cc_addr="x@example.invalid"))[0])
        self.assertFalse(L.check_lock("crm@vanguardadj.online",
            L.parse_recipients("crm@vanguardadj.online", bcc_addr="x@example.invalid"))[0])
    def test_case_variant_allowed(self):
        self.assertTrue(L.check_lock("crm@vanguardadj.online",
            ["CRM@VanguardAdj.Online"])[0])
    def test_display_name_wrapped_allowed(self):
        self.assertTrue(L.check_lock("crm@vanguardadj.online",
            L.parse_recipients("Vanguard CRM <crm@vanguardadj.online>"))[0])
    def test_multiple_refused(self):
        self.assertFalse(L.check_lock("crm@vanguardadj.online",
            ["crm@vanguardadj.online", "crm@vanguardadj.online"])[0])
    def test_no_lock_allows(self):
        self.assertTrue(L.check_lock("", ["anyone@example.invalid"])[0])

@unittest.skipUnless(__import__("os").environ.get("DB_HOST"), "no database")
class TestHandler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import db, auth
        db.init_db()
        cls.db = db; cls.auth = auth
        if db.query_one("SELECT 1 AS ok FROM information_schema.columns WHERE table_name='users' AND column_name='mail_send_locked_recipient'") is None:
            raise unittest.SkipTest("migration 051 not applied")
        cls.s = uuid.uuid4().hex[:8]
        cls.user = db.query_one("""INSERT INTO users (email, display_name, password_hash, password_salt, is_admin, is_active, role, mail_send_locked_recipient)
            VALUES (%s,'Locked','x','x',TRUE,TRUE,'admin','crm@vanguardadj.online') RETURNING id""", (f"lock-{cls.s}@example.invalid",))
        cls.acct = db.query_one("""INSERT INTO mail_accounts (email, display_name, imap_host, password_encrypted, owner_user_id)
            VALUES (%s,'Locked Acct','imap.example.invalid','x',%s) RETURNING id""", (f"lockacct-{cls.s}@example.invalid", cls.user["id"]))
        cls.session = auth.create_session(cls.user["id"], ip_address="test")
    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.execute("DELETE FROM mail_outgoing WHERE account_id=%s", (cls.acct["id"],))
            cls.db.execute("DELETE FROM mail_messages WHERE account_id=%s", (cls.acct["id"],))
            cls.db.execute("DELETE FROM sessions WHERE user_id=%s", (cls.user["id"],))
            cls.db.execute("DELETE FROM mail_accounts WHERE id=%s", (cls.acct["id"],))
            cls.db.execute("DELETE FROM users WHERE id=%s", (cls.user["id"],))
        except Exception: pass
    def _send(self, **extra):
        import mcp_local_api
        body = {"account_id": self.acct["id"], "to": "crm@vanguardadj.online",
                "subject": "s", "body": "<p>b</p>"}
        body.update(extra)
        return mcp_local_api.call_session(self.session, "POST", "/api/v2/mail/send", body)
    def test_other_recipient_refused(self):
        with patch.object(__import__("server").KanbanHandler, "_try_send_for_account",
                          return_value=(True, None, "smtp_password")):
            r = self._send(to="x@example.invalid")
        self.assertEqual(r.status, 403, r.text); self.assertIn("Recipient lock", r.text)
    def test_cc_bcc_refused(self):
        with patch.object(__import__("server").KanbanHandler, "_try_send_for_account",
                          return_value=(True, None, "smtp_password")):
            r = self._send(cc="x@example.invalid")
        self.assertEqual(r.status, 403)
        r2 = self._send(bcc="x@example.invalid")
        self.assertEqual(r2.status, 403)
    def test_exact_allowed(self):
        with patch.object(__import__("server").KanbanHandler, "_try_send_for_account",
                          return_value=(True, None, "smtp_password")), \
             patch.object(__import__("server").KanbanHandler, "_create_email_history_event", return_value=True):
            r = self._send()
        self.assertEqual(r.status, 200, r.text)

if __name__ == "__main__":
    unittest.main(verbosity=2)
