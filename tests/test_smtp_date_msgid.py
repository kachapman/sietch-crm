#!/usr/bin/env python3
"""Regression: every SMTP message leaves with a Date and a Message-ID.

THE BUG (live, 2026-11-11)
--------------------------
SMTP sends from Sietch built only From/To/Subject/Cc. Bluehost stored the
messages exactly as sent — no `Date`, no `Message-ID`. The IMAP scanner then
saw `date=1900-01-01` (imap_tools' "no Date header" sentinel), the 90-day
retention guard classified brand-new mail as ancient, and the messages were
never imported. The six stranded UIDs (INBOX 12397/12401/12405, Junk 112-114)
were all sent this way.

THE RULES PINNED HERE
---------------------
1. `send_email_from_account` sets `Date` (formatdate) and `Message-ID`
   (make_msgid with the SENDING account's domain) unless they exist.
2. `send_email` (the SMTP_USER relay path, used by password reset fallback)
   does the same.
3. An attachment switch to multipart/mixed copies Date and Message-ID onto the
   wrapper — they were previously lost at exactly that point.
4. Existing Date/Message-ID headers are never overwritten.
5. The Message-ID is reported back to the caller (`result_meta`) so the sent
   `mail_outgoing` row can persist it (migration 056).
6. No default/fallback ACCOUNT: the domain comes from the address that sends.

NO NETWORK: `smtplib.SMTP` is replaced in every test; network_guard also fails
any attempt to open an outbound socket.

Run with: python3 tests/test_smtp_date_msgid.py
"""

from __future__ import annotations

import sys
import unittest
from email import message_from_string
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import network_guard  # noqa: E402

network_guard.install()

import smtp_client  # noqa: E402

SENT_FROM = "crm@vanguardadj.online"


class _FakeSMTP:
    """Minimal smtplib.SMTP stand-in. Records the raw message on the wire."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.raw = None
        self.sent_message = None
        self.envelope_from = None
        self.recipients = None
        self.logged_in_as = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def ehlo(self):
        pass

    def login(self, user, password):
        self.logged_in_as = user

    def auth(self, mechanism, callback):
        self.logged_in_as = "XOAUTH2"

    def sendmail(self, from_addr, recipients, raw):
        self.envelope_from = from_addr
        self.recipients = recipients
        self.raw = raw

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent_message = msg
        self.envelope_from = from_addr
        self.recipients = to_addrs
        self.raw = msg.as_string()

    # Helpers -------------------------------------------------------------
    def parsed(self):
        return message_from_string(self.raw or "")

    def last():
        return _FakeSMTP.instances[-1]


def _send(**kw):
    """Real send_email_from_account, no socket."""
    _FakeSMTP.instances = []
    params = dict(
        smtp_host="smtp.vanguardadj.online",
        smtp_port=587,
        smtp_user=SENT_FROM,
        smtp_password="not-a-real-password",
        from_name="Vanguard Adjusting",
        from_addr=SENT_FROM,
        to_addr="adjuster@example.invalid",
        subject="S01 acceptance test",
        html_body="<p>hello</p>",
        text_body="hello",
    )
    params.update(kw)
    with patch.object(smtp_client.smtplib, "SMTP", _FakeSMTP):
        result = smtp_client.send_email_from_account(**params)
    return result, _FakeSMTP.last()


class TestSendEmailFromAccount(unittest.TestCase):
    def test_date_and_message_id_are_on_the_wire(self):
        (ok, err), server = _send()
        self.assertTrue(ok, err)
        payload = server.parsed()
        date_header = payload.get("Date")
        message_id = payload.get("Message-ID")
        self.assertTrue(date_header, "SMTP messages must carry a Date header")
        self.assertTrue(message_id, "SMTP messages must carry a Message-ID header")
        # The Date must be a real, parseable timestamp (not 1900).
        parsed = parsedate_to_datetime(date_header)
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed.year, 2000)
        self.assertIn("2026", date_header)

    def test_message_id_uses_the_sending_accounts_domain(self):
        (ok, _err), server = _send()
        self.assertTrue(ok)
        message_id = server.parsed().get("Message-ID")
        self.assertIn("@vanguardadj.online>", message_id)
        self.assertNotIn("sietch.local", message_id)

    def test_each_account_uses_its_own_domain_no_default_account(self):
        (ok1, _e1), server1 = _send()
        self.assertTrue(ok1)
        (ok2, _e2), server2 = _send(
            smtp_user="estimates@baneyconstruction.online",
            from_addr="estimates@baneyconstruction.online",
            from_name="Baney Construction",
        )
        self.assertTrue(ok2)
        mid1 = server1.parsed().get("Message-ID")
        mid2 = server2.parsed().get("Message-ID")
        self.assertIn("@vanguardadj.online>", mid1)
        self.assertIn("@baneyconstruction.online>", mid2)
        self.assertNotEqual(mid1, mid2)

    def test_message_id_is_returned_in_result_meta(self):
        meta: dict = {}
        (ok, _err), server = _send(result_meta=meta)
        self.assertTrue(ok)
        self.assertEqual(meta.get("message_id"), server.parsed().get("Message-ID"))

    def test_result_meta_is_optional(self):
        (ok, _err), server = _send()
        self.assertTrue(ok)
        self.assertTrue(server.parsed().get("Message-ID"))

    def test_attachment_wrapper_carries_date_and_message_id(self):
        """The multipart/mixed switch used to drop both headers."""
        attachments = [{"filename": "estimate.pdf", "content": b"%PDF-1.4 test"}]
        (ok, err), server = _send(attachments=attachments)
        self.assertTrue(ok, err)
        payload = server.parsed()
        self.assertTrue(payload.is_multipart())
        self.assertEqual(payload.get_content_type(), "multipart/mixed")
        self.assertTrue(payload.get("Date"),
                        "attachments must not drop the Date header")
        self.assertTrue(payload.get("Message-ID"),
                        "attachments must not drop the Message-ID header")
        self.assertIn("@vanguardadj.online>", payload.get("Message-ID"))
        # The alternative part is still attached and intact.
        inner = [p for p in payload.get_payload() if p.get_content_type() == "multipart/alternative"]
        self.assertEqual(len(inner), 1)
        self.assertEqual(payload.get("From"), "Vanguard Adjusting <crm@vanguardadj.online>")


class TestSendEmailRelay(unittest.TestCase):
    """The SMTP_USER relay path (`send_email`) used by password-reset fallback."""

    def _relay(self, **kw):
        _FakeSMTP.instances = []
        with patch.object(smtp_client.smtplib, "SMTP", _FakeSMTP), \
            patch.object(smtp_client, "SMTP_HOST", "smtp.vanguardadj.online"), \
            patch.object(smtp_client, "SMTP_USER", SENT_FROM), \
            patch.object(smtp_client, "SMTP_PASSWORD", "not-a-real-password"), \
            patch.object(smtp_client, "SMTP_USE_TLS", True):
            ok = smtp_client.send_email("user@example.invalid", "Subject", "<p>x</p>",
                                        "x", **kw)
        return ok, _FakeSMTP.last()

    def test_relay_sets_date_and_message_id(self):
        ok, server = self._relay()
        self.assertTrue(ok)
        payload = server.parsed()
        self.assertTrue(payload.get("Date"))
        self.assertIn("@vanguardadj.online>", payload.get("Message-ID"))
        self.assertGreater(parsedate_to_datetime(payload.get("Date")).year, 2000)

    def test_password_reset_smtp_config_path_gets_headers(self):
        _FakeSMTP.instances = []
        cfg = {
            "email": SENT_FROM, "smtp_host": "smtp.vanguardadj.online",
            "smtp_port": 587, "smtp_user": SENT_FROM,
            "smtp_password_encrypted": "not-a-real-password",
            "oauth_provider": None, "oauth_access_token": None,
        }
        with patch.object(smtp_client.smtplib, "SMTP", _FakeSMTP):
            ok = smtp_client.send_password_reset_email(
                "user@example.invalid", "https://crm.example/reset?token=x",
                smtp_config=cfg)
        self.assertTrue(ok)
        payload = _FakeSMTP.last().parsed()
        self.assertTrue(payload.get("Date"))
        self.assertTrue(payload.get("Message-ID"))


class TestExistingHeadersArePreserved(unittest.TestCase):
    def test_existing_date_and_message_id_are_not_overwritten(self):
        msg = MIMEText("body")
        msg["From"] = SENT_FROM
        msg["Date"] = "Mon, 9 Nov 2026 13:20:03 -0500"
        msg["Message-ID"] = "<keep-me@vanguardadj.online>"
        returned = smtp_client.ensure_message_headers(msg, SENT_FROM)
        self.assertEqual(msg["Date"], "Mon, 9 Nov 2026 13:20:03 -0500")
        self.assertEqual(msg["Message-ID"], "<keep-me@vanguardadj.online>")
        self.assertEqual(returned, "<keep-me@vanguardadj.online>")

    def test_missing_headers_are_filled_with_the_sender_domain(self):
        msg = MIMEText("body")
        returned = smtp_client.ensure_message_headers(msg, "someone@example.org")
        self.assertTrue(msg["Date"])
        self.assertEqual(msg["Message-ID"], returned)
        self.assertIn("@example.org>", returned)

    def test_domain_helper_never_invents_an_account(self):
        self.assertEqual(smtp_client.message_domain("crm@vanguardadj.online"),
                         "vanguardadj.online")
        self.assertEqual(smtp_client.message_domain("Name <a@b.co>"), "b.co")
        self.assertIsNone(smtp_client.message_domain(""))
        self.assertIsNone(smtp_client.message_domain("no-at-sign"))


class TestAutomationSendHeaders(unittest.TestCase):
    """Automation rules build their own MIME message + smtplib send."""

    def test_automation_send_has_date_and_message_id(self):
        import automation_engine

        crm_row = {
            "id": 2, "email": SENT_FROM, "smtp_host": "smtp.vanguardadj.online",
            "smtp_port": 587, "smtp_user": SENT_FROM,
            "smtp_password_encrypted": "not-a-real-password", "oauth_provider": None,
        }
        config = {
            "from_type": "crm_mail",
            "to_type": "project_owner",
            "subject": "Hi",
            "body": "Hello",
        }
        sent = []

        class _AutoSMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                pass

            def sendmail(self, from_addr, recipients, raw):
                sent.append(raw)

        with patch.object(automation_engine, "db") as db, \
            patch("mail_identity.system_account", return_value=crm_row), \
            patch.object(automation_engine.smtplib, "SMTP", _AutoSMTP):
            db.query_one.side_effect = [{"email": "owner@example.invalid"}]
            automation_engine._action_send_email(
                config, {"id": 5, "title": "P", "responsible_user_id": 100},
                {"project": "P"}, dry_run=False)
        self.assertEqual(len(sent), 1)
        payload = message_from_string(sent[0])
        self.assertTrue(payload.get("Date"))
        self.assertIn("@vanguardadj.online>", payload.get("Message-ID"))


class TestSentRowPersistence(unittest.TestCase):
    """Source guard for migration 056 + the HTTP handler write (needs a DB)."""

    def setUp(self):
        self.server_src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.init_sql = (ROOT / "init.sql").read_text(encoding="utf-8")

    def test_migration_056_adds_both_columns(self):
        sql = (ROOT / "migrations" /
               "056_mail_date_source_and_outgoing_message_id.sql").read_text(encoding="utf-8")
        self.assertIn("ALTER TABLE mail_messages ADD COLUMN IF NOT EXISTS date_source TEXT", sql)
        self.assertIn("ALTER TABLE mail_outgoing ADD COLUMN IF NOT EXISTS message_id TEXT", sql)
        self.assertNotIn("DROP COLUMN", sql)
        self.assertNotIn("DELETE FROM", sql)

    def test_init_sql_declares_and_stamps_the_columns(self):
        self.assertIn("056_mail_date_source_and_outgoing_message_id.sql", self.init_sql)
        self.assertIn("date_source TEXT", self.init_sql)
        self.assertIn("message_id TEXT", self.init_sql)

    def test_handler_persists_the_message_id_on_the_outgoing_row(self):
        start = self.server_src.index("def _handle_mail_send(self)")
        end = self.server_src.index("def _handle_mail_send_undo(self)")
        body = self.server_src[start:end]
        self.assertIn('send_meta.get("message_id")', body)
        self.assertIn("message_id)", body.lower())

    def test_mail_send_forwards_result_meta_to_the_smtp_client(self):
        src = (ROOT / "mail_send.py").read_text(encoding="utf-8")
        self.assertIn("result_meta=result_meta", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
