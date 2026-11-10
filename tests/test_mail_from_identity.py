#!/usr/bin/env python3
"""From identity comes ONLY from the selected account row (item 4, 2026-11-10).

THE DEFECT
----------
The provider's From display name is what an adjuster sees, and it did not come
from the account row that was selected:

  * `mail_graph_client.send_email_via_graph` never set `message.from`, so Graph
    used the *mailbox profile* name (Outlook showed "A-Plus Restoration GC"
    while the CRM row said "A Plus Restoration GC");
  * `mail_send.try_send_for_account` accepted a caller-supplied `from_name`,
    which is an invitation to put a global/default/other name on the envelope.

THE RULE THESE TESTS PIN
------------------------
1. Graph: `message.from` = the selected account's `email` + `smtp_from_name`,
   and the explicit From must equal the Graph token's verified mailbox —
   anything else is refused BEFORE the request.
2. SMTP/XOAUTH2: the `From` header is built from the same two fields. A blank
   `smtp_from_name` means address-only, never a borrowed name.
3. No cross-account and no global bleed: two accounts send in turn/parallel and
   each payload/header carries only its own name and address.
4. `Sender`/`Return-Path` are documented as unavailable in stored evidence
   (the scanner keeps only a fixed header subset) — the helpers must not
   invent them.

Run with: python3 tests/test_mail_from_identity.py
"""
import io
import json
import sys
import threading
import unittest
from email import message_from_string
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mail_graph_client as gc
import mail_identity as mi
import mail_send
import smtp_client


def make_acct(**kw):
    """A mail_accounts row shaped like prod account 9 (A Plus / Outlook)."""
    acct = {
        "id": 9,
        "email": "aplus.estimates@outlook.com",
        "display_name": "Aplus Estimates",          # tab label — never a header
        "smtp_from_name": "A Plus Restoration GC",
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "smtp_user": "aplus.estimates@outlook.com",
        "smtp_password_encrypted": "enc",
        "smtp_use_tls": True,
        "smtp_auth_disabled": False,
        "oauth_provider": None,
        "oauth_access_token": None,
        "oauth_refresh_token": None,
        "oauth_token_expires": None,
        "oauth_identity": None,
        "oauth_graph_access_token": None,
        "oauth_graph_refresh_token": None,
        "oauth_graph_identity": None,
    }
    acct.update(kw)
    return acct


def graph_acct(**kw):
    base = dict(
        oauth_provider="microsoft",
        oauth_access_token="EXCHANGE-AT",
        oauth_refresh_token="EXCHANGE-RT",
        oauth_identity="aplus.estimates@outlook.com",
        oauth_graph_access_token="GRAPH-AT",
        oauth_graph_refresh_token="GRAPH-RT",
        oauth_graph_identity="aplus.estimates@outlook.com",
    )
    base.update(kw)
    return make_acct(**base)


class _FakeResponse(io.BytesIO):
    status = 202


class _CaptureUrlopen:
    """Stand-in for urllib.request.urlopen that records the Graph request."""

    def __init__(self):
        self.payloads = []
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        self.payloads.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse(b"")

    def __enter__(self):        # pragma: no cover - defensive
        return self

    def __exit__(self, *a):     # pragma: no cover - defensive
        return False


class _FakeSMTP:
    """Minimal smtplib.SMTP stand-in; records the From header + envelope."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.envelope_from = None
        self.recipients = None
        self.raw = None
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

    def from_header(self):
        msg = message_from_string(self.raw or "")
        return msg.get("From", "")


def send_via_graph(acct, **kw):
    """Call the real Graph client with a captured HTTP layer."""
    capture = _CaptureUrlopen()
    with patch.object(gc.urllib.request, "urlopen", capture):
        ok, err = gc.send_email_via_graph(
            graph_access_token=kw.pop("graph_access_token", "GRAPH-AT"),
            from_addr=acct.get("email", ""),
            from_name=kw.pop("from_name", acct.get("smtp_from_name")),
            verified_identity=kw.pop("verified_identity", acct.get("oauth_graph_identity")),
            to_addr=kw.pop("to_addr", "adjuster@example.invalid"),
            subject=kw.pop("subject", "Claim 600-1333671"),
            html_body=kw.pop("html_body", "<p>hi</p>"),
            **kw,
        )
    return ok, err, capture


class TestGraphExplicitFrom(unittest.TestCase):
    def test_from_is_the_account_row_name_and_address(self):
        acct = graph_acct()
        ok, err, capture = send_via_graph(acct)
        self.assertTrue(ok, err)
        self.assertEqual(capture.calls, 1)
        message = capture.payloads[0]["message"]
        self.assertEqual(message["from"], {"emailAddress": {
            "address": "aplus.estimates@outlook.com",
            "name": "A Plus Restoration GC",
        }})

    def test_sender_property_is_not_set(self):
        """`sender` needs Send As permission; /me/sendMail must not carry it."""
        ok, _, capture = send_via_graph(graph_acct())
        self.assertTrue(ok)
        self.assertNotIn("sender", capture.payloads[0]["message"])

    def test_display_name_is_never_used_for_the_header(self):
        acct = graph_acct(display_name="Aplus Estimates")
        ok, _, capture = send_via_graph(acct)
        self.assertTrue(ok)
        self.assertEqual(capture.payloads[0]["message"]["from"]["emailAddress"]["name"],
                         "A Plus Restoration GC")

    def test_empty_smtp_from_name_sends_address_only(self):
        acct = graph_acct(email="estimates.masterx@outlook.com",
                          oauth_graph_identity="estimates.masterx@outlook.com",
                          smtp_from_name="")
        ok, err, capture = send_via_graph(acct)
        self.assertTrue(ok, err)
        email_address = capture.payloads[0]["message"]["from"]["emailAddress"]
        self.assertEqual(email_address, {"address": "estimates.masterx@outlook.com"})
        self.assertNotIn("name", email_address)

    def test_explicit_from_mismatch_is_refused_without_a_request(self):
        ok, err, capture = send_via_graph(graph_acct(), verified_identity="mdm.estimates@outlook.com")
        self.assertFalse(ok)
        self.assertIn("Refusing to send", err)
        self.assertIn("mdm.estimates@outlook.com", err)
        self.assertEqual(capture.calls, 0, "nothing may be sent when the From is not the token's mailbox")

    def test_unverified_identity_is_refused_without_a_request(self):
        ok, err, capture = send_via_graph(graph_acct(), verified_identity=None)
        self.assertFalse(ok)
        self.assertIn("Refusing to send", err)
        self.assertEqual(capture.calls, 0)

    def test_address_compare_is_case_insensitive(self):
        ok, err, capture = send_via_graph(graph_acct(email="Aplus.Estimates@Outlook.com"),
                                          verified_identity="aplus.estimates@outlook.com")
        self.assertTrue(ok, err)
        self.assertEqual(capture.payloads[0]["message"]["from"]["emailAddress"]["name"],
                         "A Plus Restoration GC")


class TestGraphNoCrossAccountBleed(unittest.TestCase):
    def test_each_account_sends_its_own_name_and_address(self):
        accounts = [
            graph_acct(),
            graph_acct(id=10, email="mdm.estimates@outlook.com",
                       oauth_identity="mdm.estimates@outlook.com",
                       oauth_graph_identity="mdm.estimates@outlook.com",
                       smtp_from_name="MDM Construction"),
            graph_acct(id=8, email="estimates.masterx@outlook.com",
                       oauth_identity="estimates.masterx@outlook.com",
                       oauth_graph_identity="estimates.masterx@outlook.com",
                       smtp_from_name=""),
        ]
        seen = []
        for acct in accounts:
            ok, err, capture = send_via_graph(acct)
            self.assertTrue(ok, err)
            seen.append(capture.payloads[0]["message"]["from"]["emailAddress"])
        self.assertEqual(seen, [
            {"address": "aplus.estimates@outlook.com", "name": "A Plus Restoration GC"},
            {"address": "mdm.estimates@outlook.com", "name": "MDM Construction"},
            {"address": "estimates.masterx@outlook.com"},
        ])

    def test_parallel_sends_do_not_cross_names(self):
        accts = [
            graph_acct(),
            graph_acct(id=10, email="mdm.estimates@outlook.com",
                       oauth_identity="mdm.estimates@outlook.com",
                       oauth_graph_identity="mdm.estimates@outlook.com",
                       smtp_from_name="MDM Construction"),
        ]
        results = []
        lock = threading.Lock()

        def run(acct):
            ok, err, capture = send_via_graph(acct)
            address = capture.payloads[0]["message"]["from"]["emailAddress"]
            with lock:
                results.append((acct["email"], address.get("name"), ok))

        threads = [threading.Thread(target=run, args=(a,)) for a in accts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [
            ("aplus.estimates@outlook.com", "A Plus Restoration GC", True),
            ("mdm.estimates@outlook.com", "MDM Construction", True),
        ])


class TestMailSendPassesTheAccountRowOnly(unittest.TestCase):
    def test_graph_path_passes_the_account_row_identity(self):
        acct = graph_acct()
        with patch("mail_graph_client.send_email_via_graph", return_value=(True, None)) as send:
            ok, err, method = mail_send.try_send_for_account(
                acct, to_addr="x@example.invalid", subject="s",
                refresh_graph_token=lambda aid: ("TOKEN", None))
        self.assertTrue(ok, err)
        self.assertEqual(method, "graph")
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["from_addr"], "aplus.estimates@outlook.com")
        self.assertEqual(kwargs["from_name"], "A Plus Restoration GC")
        self.assertEqual(kwargs["verified_identity"], "aplus.estimates@outlook.com")

    def test_smtp_path_uses_account_name_and_ignores_a_global_default(self):
        acct = make_acct()
        with patch.object(smtp_client, "SMTP_FROM_NAME", "Sietch CRM"), \
             patch("smtp_client.send_email_from_account", return_value=(True, None)) as send:
            ok, err, method = mail_send.try_send_for_account(
                acct, to_addr="x@example.invalid", subject="s")
        self.assertTrue(ok, err)
        self.assertEqual(method, "smtp_password")
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["from_name"], "A Plus Restoration GC")
        self.assertEqual(kwargs["from_addr"], "aplus.estimates@outlook.com")

    def test_smtp_path_blank_name_is_none_not_a_borrowed_name(self):
        acct = make_acct(smtp_from_name="", display_name="Aplus Estimates")
        with patch("smtp_client.send_email_from_account", return_value=(True, None)) as send:
            ok, err, _ = mail_send.try_send_for_account(
                acct, to_addr="x@example.invalid", subject="s")
        self.assertTrue(ok, err)
        self.assertIsNone(send.call_args.kwargs["from_name"])

    def test_shared_helper_no_longer_accepts_a_from_name_override(self):
        with self.assertRaises(TypeError):
            mail_send.try_send_for_account(
                make_acct(), to_addr="x@example.invalid", subject="s",
                from_name="Someone Else Ltd")


class TestSmtpHeaderFromAccountRow(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.instances = []
        self.addCleanup(_FakeSMTP.instances.clear)

    def _send(self, **kw):
        params = dict(
            smtp_host="smtp-mail.outlook.com", smtp_port=587,
            smtp_user="aplus.estimates@outlook.com", smtp_password="enc",
            from_name="A Plus Restoration GC", from_addr="aplus.estimates@outlook.com",
            to_addr="adjuster@example.invalid", subject="Claim 600-1333671",
            html_body="<p>hi</p>",
        )
        params.update(kw)
        with patch.object(smtp_client.smtplib, "SMTP", _FakeSMTP):
            ok, err = smtp_client.send_email_from_account(**params)
        return ok, err, _FakeSMTP.instances[-1]

    def test_from_header_carries_the_account_name_and_address(self):
        ok, err, server = self._send()
        self.assertTrue(ok, err)
        self.assertEqual(server.from_header(), "A Plus Restoration GC <aplus.estimates@outlook.com>")
        self.assertEqual(server.envelope_from, "aplus.estimates@outlook.com")

    def test_empty_name_produces_address_only(self):
        ok, err, server = self._send(from_name="", from_addr="estimates.masterx@outlook.com",
                                     smtp_user="estimates.masterx@outlook.com")
        self.assertTrue(ok, err)
        self.assertEqual(server.from_header(), "estimates.masterx@outlook.com")

    def test_second_account_does_not_inherit_the_first_name(self):
        self._send()
        ok, err, server = self._send(from_name="MDM Construction",
                                     from_addr="mdm.estimates@outlook.com",
                                     smtp_user="mdm.estimates@outlook.com")
        self.assertTrue(ok, err)
        self.assertEqual(server.from_header(), "MDM Construction <mdm.estimates@outlook.com>")

    def test_foreign_login_never_opens_a_socket(self):
        with patch.object(smtp_client.smtplib, "SMTP", _FakeSMTP):
            ok, err = smtp_client.send_email_from_account(
                smtp_host="smtp-mail.outlook.com", smtp_port=587,
                smtp_user="mdm.estimates@outlook.com", smtp_password="enc",
                from_name="A Plus Restoration GC", from_addr="aplus.estimates@outlook.com",
                to_addr="adjuster@example.invalid", subject="s", html_body="<p>hi</p>")
        self.assertFalse(ok)
        self.assertIn("Refusing to send", err)
        self.assertEqual(_FakeSMTP.instances, [])

    def test_display_name_with_a_comma_is_quoted(self):
        ok, err, server = self._send(from_name="Sherwood Estimates, LLC",
                                     from_addr="requests@example.invalid",
                                     smtp_user="requests@example.invalid")
        self.assertTrue(ok, err)
        self.assertEqual(server.from_header(),
                         '"Sherwood Estimates, LLC" <requests@example.invalid>')

    def test_build_from_identity_helper(self):
        self.assertEqual(gc.build_from_identity("a@example.invalid", " A Brand "),
                         {"emailAddress": {"address": "a@example.invalid", "name": "A Brand"}})
        self.assertEqual(gc.build_from_identity("a@example.invalid", "   "),
                         {"emailAddress": {"address": "a@example.invalid"}})


class TestObservedNameHelpers(unittest.TestCase):
    RAW = ("from: A-Plus Restoration GC <aplus.estimates@outlook.com>\n"
           "subject: Claim\n"
           "date: Mon, 9 Nov 2026 18:00:00 +0000\n"
           "message-id: <x@example.invalid>\n")

    def test_parse_from_header_reads_name_and_address(self):
        self.assertEqual(mi.parse_from_header(self.RAW),
                         ("A-Plus Restoration GC", "aplus.estimates@outlook.com"))

    def test_parse_from_header_bare_address(self):
        self.assertEqual(mi.parse_from_header("from: a@example.invalid\n"),
                         ("", "a@example.invalid"))

    def test_parse_from_header_absent(self):
        self.assertEqual(mi.parse_from_header(""), ("", ""))
        self.assertEqual(mi.parse_from_header(None), ("", ""))

    def test_punctuation_only_difference_is_not_drift(self):
        self.assertFalse(mi.from_name_drift("A Plus Restoration GC", "A-Plus Restoration GC"))
        self.assertTrue(mi.display_names_match("A Plus Restoration GC", "A-Plus  restoration gc"))

    def test_real_difference_is_drift(self):
        self.assertTrue(mi.from_name_drift("A Plus Restoration GC", "MDM Construction"))

    def test_empty_configured_name_with_provider_name_is_drift(self):
        self.assertTrue(mi.from_name_drift("", "MasterX Construction"))

    def test_no_observed_name_is_not_drift(self):
        self.assertFalse(mi.from_name_drift("A Plus Restoration GC", ""))
        self.assertFalse(mi.from_name_drift("", ""))

    def test_sender_and_return_path_are_documented_as_unavailable(self):
        """Evidence cannot show Sender/Return-Path: the scanner stores a subset."""
        raw = ("from: a@example.invalid\nsender: b@example.invalid\n"
               "return-path: <b@example.invalid>\n")
        # The parser reads the From line only and never invents the others.
        self.assertEqual(mi.parse_from_header(raw), ("", "a@example.invalid"))


class TestSourceGuards(unittest.TestCase):
    """Pieces that need a live browser/DB: assert the wiring by source."""

    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.server = (root / "server.py").read_text(encoding="utf-8")
        self.app = (root / "public" / "app.js").read_text(encoding="utf-8")
        self.html = (root / "public" / "index.html").read_text(encoding="utf-8")
        self.scanner = (root / "scanner" / "mail_scanner.py").read_text(encoding="utf-8")
        self.graph = (root / "mail_graph_client.py").read_text(encoding="utf-8")

    def test_accounts_api_reports_the_observed_from_name(self):
        at = self.server.index("def _handle_mail_accounts")
        region = self.server[at:at + 12000]
        self.assertIn("observedFromName", region)
        self.assertIn("fromNameDrift", region)
        self.assertIn("mail_identity.parse_from_header", region)
        self.assertIn("raw_headers", region)

    def test_settings_shows_the_hint_next_to_the_display_name(self):
        self.assertIn('id="account-settings-from-name-hint"', self.html)
        at = self.app.index("account-settings-from-name-hint")
        region = self.app[at:at + 1600]
        self.assertIn("acct.observedFromName", region)
        self.assertIn("acct.fromNameDrift", region)

    def test_sent_copy_reconciliation_keeps_the_provider_headers(self):
        at = self.scanner.index("if account_id is not None and _is_sent_folder(folder):")
        region = self.scanner[at:at + 3000]
        self.assertIn("UPDATE mail_messages SET raw_headers", region,
                      "the CRM sent copy must keep the provider's real From header")

    def test_graph_send_asserts_the_from_identity(self):
        at = self.graph.index("def send_email_via_graph")
        region = self.graph[at:at + 4000]
        self.assertIn("verified_identity", region)
        self.assertIn('"from": build_from_identity(from_addr, from_name)', region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
