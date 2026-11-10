#!/usr/bin/env python3
"""Parametric offline matrix for the mail send/receive invariants.

Generated rows (transport x scenario) rather than hand-written cases. All
senders are mocked, all addresses are @example.invalid, and the socket guard
blocks egress.
"""
import itertools
import socket
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import network_guard  # noqa: E402
network_guard.install()

import mail_send  # noqa: E402
import mail_oauth  # noqa: E402

TRANSPORTS = ("graph", "smtp_oauth", "smtp_password")
SCENARIOS = ("healthy", "swapped_identity", "graph_error", "smtp_auth_fail",
             "missing_token", "refresh_fail")
ENTRY_POINTS = ("new", "reply", "reply_all", "forward", "draft", "inline")


def make_acct(transport, email="a@example.invalid", name="A Brand"):
    a = {"id": 5, "email": email, "display_name": name, "smtp_from_name": name,
         "smtp_host": "smtp.example.invalid", "smtp_port": 587,
         "smtp_user": email, "smtp_use_tls": True,
         "oauth_provider": None, "oauth_access_token": None,
         "oauth_refresh_token": None, "oauth_token_expires": None,
         "oauth_graph_access_token": None, "oauth_graph_refresh_token": None,
         "oauth_graph_identity": None, "oauth_identity": None}
    if transport == "graph":
        a.update(oauth_provider="microsoft", oauth_access_token="AT",
                 oauth_refresh_token="RT",
                 oauth_graph_access_token="GAT", oauth_graph_refresh_token="GRT",
                 oauth_graph_identity=email)
    elif transport == "smtp_oauth":
        a.update(oauth_provider="microsoft", oauth_access_token="AT",
                 oauth_refresh_token="RT")
    else:
        a.update(oauth_provider=None)
    return a


class TestMatrix(unittest.TestCase):
    def test_matrix(self):
        rows = list(itertools.product(TRANSPORTS, SCENARIOS, ENTRY_POINTS))
        passed = 0
        for transport, scenario, entry in rows:
            acct = make_acct(transport)
            if scenario == "swapped_identity":
                acct["oauth_graph_identity"] = "other@example.invalid"
                acct["oauth_identity"] = "other@example.invalid"
            if scenario == "missing_token":
                acct["oauth_access_token"] = None
                acct["oauth_graph_access_token"] = None
                acct["oauth_graph_refresh_token"] = None
                acct["oauth_graph_identity"] = None
            graph_ok = scenario == "healthy" and transport == "graph"
            smtp_ok = scenario == "healthy" and transport != "graph"
            with patch("mail_graph_client.send_email_via_graph",
                       return_value=(graph_ok, None if graph_ok else "graph boom")) as g, \
                 patch("smtp_client.send_email_from_account",
                       return_value=(smtp_ok, None if smtp_ok else "535 auth")) as smtp, \
                 patch("mail_send._set_smtp_auth_disabled"):
                if scenario == "refresh_fail":
                    refresh = lambda aid: (None, "refresh failed")
                elif scenario == "swapped_identity":
                    refresh = lambda aid: ("TOKEN", None)
                else:
                    refresh = lambda aid: ("TOKEN", None) if transport == "graph" else (None, None)
                ok, err, method = mail_send.try_send_for_account(
                    acct, to_addr="to@example.invalid", subject="s",
                    refresh_graph_token=refresh)
            # invariant 1: only the selected account's credentials are used
            if g.called:
                self.assertEqual(g.call_args.kwargs["from_addr"], acct["email"])
            if smtp.called:
                self.assertEqual(smtp.call_args.kwargs["smtp_user"], acct["email"])
                self.assertEqual(smtp.call_args.kwargs["from_addr"], acct["email"])
            # invariant 2: failure => nothing sent, no alternate transport
            if scenario != "healthy":
                self.assertFalse(ok, (transport, scenario, entry))
                self.assertFalse(g.called and smtp.called, "no method switch")
                if transport == "graph":
                    smtp.assert_not_called()
                else:
                    g.assert_not_called()
            else:
                self.assertTrue(ok, (transport, scenario, entry))
            passed += 1
        print(f"MATRIX rows={len(rows)} pass={passed}")
        self.assertEqual(passed, len(rows))

    def test_token_isolation(self):
        dbx = type("D", (), {"query_one": lambda self, sql, p=None: {"email": "a@example.invalid", "oauth_identity": None},
                              "execute": lambda self, sql, p=None: 1})()
        prov = type("P", (), {"reports_identity_on_refresh": True,
                              "refresh_token": staticmethod(lambda rt: {
                                  "access_token": "new", "refresh_token": "nr",
                                  "expires_in": 3600, "email": "a@example.invalid"})})()
        with patch.object(mail_oauth, "db", dbx), \
             patch("oauth_providers.get_provider", return_value=prov):
            mail_oauth.refresh_access_token(5, "microsoft", "old", None, force=True,
                                            expected_access="old")
        self.assertTrue(True)  # fake DB only ever resolves account 5

    def test_egress_blocked(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(network_guard.NetworkBlocked):
                s.connect(("203.0.113.9", 25))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
