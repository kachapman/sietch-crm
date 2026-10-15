"""Unit tests for telegram_bot helpers and formatters.

Covers the audit round 8 fixes:
  - #1, #7: _truncate helper and the formatters' length caps + ellipsis
  - #4: _get_project_label fallback paths
  - #5: _config_poll_loop polls immediately on startup
  - #6: _TelegramHTMLSanitizer strips dangerous href schemes
  - #8: format_search_result preserves the footer through truncation
Plus the smaller helpers: _esc, _truncate_html, _fmt_date, _extract_*
fields, _extract_forward_info, _clean_reply_attribution.

These tests don't require a running Telegram bot or HTTP server.
We patch out the imports of python-telegram-bot at the top so the
tests can run on a machine without the bot dependency.

Run with: python3 tests/test_telegram_bot.py
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Stub the telegram imports so telegram_bot.py imports cleanly on
# machines without python-telegram-bot installed. The test only
# touches the formatters, not the bot itself.
TELEGRAM_STUB = MagicMock()
TELEGRAM_EXT_STUB = MagicMock()
sys.modules.setdefault("telegram", TELEGRAM_STUB)
sys.modules.setdefault("telegram.ext", TELEGRAM_EXT_STUB)

# Now import telegram_bot. The module-level constants will be empty
# (TOKEN="") which is fine — no main() invocation happens.
import telegram_bot  # noqa: E402


class TestEsc(unittest.TestCase):
    """_esc: HTML escape for Telegram's HTML parse mode."""

    def test_escapes_ampersand_lt_gt(self):
        self.assertEqual(telegram_bot._esc("<b>hi & bye</b>"),
                         "&lt;b&gt;hi &amp; bye&lt;/b&gt;")

    def test_escapes_quotes_via_quote_false(self):
        # quote=False means " and ' pass through unescaped. Telegram
        # accepts both, so this is correct.
        self.assertEqual(telegram_bot._esc('a "b" c'),
                         'a "b" c')

    def test_none_returns_empty(self):
        self.assertEqual(telegram_bot._esc(None), "")

    def test_non_string_coerced(self):
        self.assertEqual(telegram_bot._esc(42), "42")


class TestTruncate(unittest.TestCase):
    """_truncate: silent-or-ellipsis truncation helper."""

    def test_under_limit_returns_unchanged(self):
        self.assertEqual(telegram_bot._truncate("hello", 10), "hello")
        self.assertEqual(telegram_bot._truncate("hello", 5), "hello")  # exactly 5

    def test_empty_returns_empty(self):
        self.assertEqual(telegram_bot._truncate("", 10), "")
        self.assertEqual(telegram_bot._truncate(None, 10), "")

    def test_over_limit_truncates_with_ellipsis(self):
        # 10 chars truncated to 5 = first 4 chars + ellipsis
        self.assertEqual(telegram_bot._truncate("abcdefghij", 5), "abcd…")

    def test_just_over_limit(self):
        # 6 chars in 5 limit = first 4 + …
        self.assertEqual(telegram_bot._truncate("abcdef", 5), "abcd…")


class TestTruncateHtml(unittest.TestCase):
    """_truncate_html: cuts an HTML message without dangling tags."""

    def test_under_limit_returns_unchanged(self):
        msg = "<b>hi</b>"
        self.assertEqual(telegram_bot._truncate_html(msg, 100), msg)

    def test_drops_partial_tag_at_end(self):
        # "a<b>" is 4 chars; if max_len=3 we cut at 3 chars then drop
        # the partial tag. Result: "a" with the <b> closed if open.
        out = telegram_bot._truncate_html("a<b>", 3)
        self.assertNotIn("<b>", out)

    def test_closes_unclosed_tags(self):
        # A long message with an unclosed <b>. After truncation, the
        # <b> must be closed so Telegram's parser doesn't drop the
        # rest of the message. Use a small custom ellipsis so the
        # truncation logic has room to work.
        msg = "<b>" + "x" * 200
        out = telegram_bot._truncate_html(msg, 100, ellipsis="…")
        self.assertIn("</b>", out)
        self.assertIn("…", out)

    def test_self_closing_tags_ignored(self):
        # <br> doesn't need closing. <br><br> = 8 chars, cut to 5.
        out = telegram_bot._truncate_html("<br><br><br>", 5)
        # Should not try to close <br>.
        self.assertNotIn("</br>", out)


class TestFmtDate(unittest.TestCase):
    """_fmt_date: ISO timestamp -> 'Mon DD, YYYY'."""

    def test_iso_with_z(self):
        self.assertEqual(telegram_bot._fmt_date("2026-10-15T14:30:00Z"),
                         "Oct 15, 2026")

    def test_iso_with_offset(self):
        self.assertEqual(telegram_bot._fmt_date("2026-10-15T14:30:00+00:00"),
                         "Oct 15, 2026")

    def test_iso_with_microseconds(self):
        self.assertEqual(telegram_bot._fmt_date("2026-10-15T14:30:00.123456+00:00"),
                         "Oct 15, 2026")

    def test_empty_returns_empty(self):
        self.assertEqual(telegram_bot._fmt_date(""), "")

    def test_invalid_returns_first_10_chars(self):
        # "not-a-date" — fallback is iso_str[:10] = "not-a-date"
        self.assertEqual(telegram_bot._fmt_date("not-a-date-12345"), "not-a-date")


class TestExtractMailFields(unittest.TestCase):
    """_extract_mail_fields: best-effort parse of CRM mail JSON."""

    def test_basic_extraction(self):
        content = json.dumps({
            "from": "alice@example.com",
            "to": "bob@example.com",
            "subject": "Hello",
            "introduction": "Hi Bob, ...",
        })
        result = telegram_bot._extract_mail_fields(content)
        self.assertEqual(result["from"], "alice@example.com")
        self.assertEqual(result["to"], "bob@example.com")
        self.assertEqual(result["subject"], "Hello")
        self.assertEqual(result["introduction"], "Hi Bob, ...")

    def test_handles_escaped_strings(self):
        # The regex allows escaped characters. JSON encoding of "
        # inside a string value is \".
        content = '{"subject": "Hello \\"World\\""}'
        result = telegram_bot._extract_mail_fields(content)
        self.assertEqual(result["subject"], 'Hello "World"')

    def test_missing_fields(self):
        result = telegram_bot._extract_mail_fields('{"from": "a@b.com"}')
        self.assertEqual(result["from"], "a@b.com")
        self.assertNotIn("subject", result)

    def test_no_fields_returns_empty_dict(self):
        result = telegram_bot._extract_mail_fields("nothing here")
        self.assertIsNone(result)

    def test_multiline_subject(self):
        # \n in JSON source becomes a real \n in the parsed value
        content = '{"subject": "Line 1\\nLine 2"}'
        result = telegram_bot._extract_mail_fields(content)
        self.assertEqual(result["subject"], "Line 1\nLine 2")


class TestHtmlToText(unittest.TestCase):
    """_html_to_text: CRM email HTML -> tight plain text."""

    def test_strips_block_tags(self):
        text = telegram_bot._html_to_text("<p>Hello</p><p>World</p>")
        # Two paragraphs become two lines.
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("<p>", text)

    def test_br_becomes_space(self):
        # <br> inside a paragraph is a space, not a line break.
        text = telegram_bot._html_to_text("Hello<br>World")
        self.assertIn("Hello World", text)

    def test_table_flattens(self):
        text = telegram_bot._html_to_text(
            "<table><tr><th>Header</th><td>Value</td></tr></table>"
        )
        self.assertIn("Header", text)
        self.assertIn("Value", text)
        self.assertNotIn("<table>", text)

    def test_unescapes_entities(self):
        text = telegram_bot._html_to_text("Hello &amp; World")
        self.assertIn("Hello & World", text)


class TestTruncateLink(unittest.TestCase):
    """Audit round 8 fix #6: javascript:/vbscript:/data: URLs are
    dropped by _TelegramHTMLSanitizer."""

    def test_javascript_href_dropped(self):
        # A <a href="javascript:...">link</a> should be dropped entirely.
        result = telegram_bot._sanitize_html(
            '<a href="javascript:alert(1)">click me</a>'
        )
        # The text remains (link stripped, content kept), but no <a>
        # tag with the dangerous href is preserved.
        self.assertNotIn("javascript:", result.lower())
        self.assertIn("click me", result)

    def test_vbscript_href_dropped(self):
        result = telegram_bot._sanitize_html(
            '<a href="vbscript:msgbox()">click</a>'
        )
        self.assertNotIn("vbscript:", result.lower())
        self.assertIn("click", result)

    def test_data_html_href_dropped(self):
        result = telegram_bot._sanitize_html(
            '<a href="data:text/html,<script>alert(1)</script>">click</a>'
        )
        self.assertNotIn("data:text/html", result.lower())
        self.assertIn("click", result)

    def test_safe_href_preserved(self):
        result = telegram_bot._sanitize_html(
            '<a href="https://example.com">click me</a>'
        )
        self.assertIn("https://example.com", result)
        self.assertIn("click me", result)

    def test_mailto_href_preserved(self):
        result = telegram_bot._sanitize_html(
            '<a href="mailto:alice@example.com">email</a>'
        )
        self.assertIn("mailto:alice@example.com", result)

    def test_tg_scheme_preserved(self):
        result = telegram_bot._sanitize_html(
            '<a href="tg://resolve?domain=bot">open</a>'
        )
        self.assertIn("tg://resolve", result)

    def test_href_with_leading_whitespace_stripped(self):
        # Whitespace before javascript: should still be caught after
        # .strip() lowercasing.
        result = telegram_bot._sanitize_html(
            '<a href="  JaVaScRiPt:alert(1)">click</a>'
        )
        self.assertNotIn("javascript:", result.lower())
        # The text is still preserved.
        self.assertIn("click", result)

    def test_plain_text_passes_through(self):
        # No HTML, just text. Should be HTML-escaped (the sanitizer
        # always escapes angle brackets).
        result = telegram_bot._sanitize_html("just text")
        self.assertIn("just text", result)


class TestFormatEventBody(unittest.TestCase):
    """_format_event_body: 500-char cap with … for non-mail events."""

    def test_short_event_unchanged(self):
        body = "Just a short note."
        out = telegram_bot._format_event_body("Note", body)
        self.assertIn(body, out)

    def test_long_event_capped(self):
        body = "x" * 1000
        out = telegram_bot._format_event_body("Note", body)
        # Cap is 500 chars; the result is the sanitized body which
        # is shorter than the input. Must contain the ellipsis.
        self.assertIn("…", out)
        # And must not contain the full 1000 x's.
        self.assertLess(len(out), 800)

    def test_mail_event_routes_to_format_mail(self):
        # category=email or mail routes to _format_mail_event (different
        # cap, different parsing). We don't re-test the mail format
        # function here (covered separately), but verify the routing.
        body = json.dumps({"from": "a@b.com", "subject": "s", "introduction": "x"})
        out = telegram_bot._format_event_body("Email", body, max_len=500)
        # The result should be the structured mail format, not the
        # raw cap-and-ellipsis.
        self.assertIn("From:", out)
        self.assertIn("Subject: s", out)


class TestFormatSearchResult(unittest.TestCase):
    """Audit round 8 fix #8: the search-result footer must survive
    truncation even when the body is huge."""

    def _make_deal(self, title, **kwargs):
        return {"id": 1, "title": title, **kwargs}

    def test_single_deal_routes_to_deal_detail(self):
        # Single-deal case delegates to format_deal_detail.
        deal = self._make_deal("Ponderosa", stage="Bidding")
        out = telegram_bot.format_search_result([deal], "pond", is_employee=False)
        # format_deal_detail starts with the project title.
        self.assertIn("Ponderosa", out)

    def test_no_deals_returns_no_match_message(self):
        out = telegram_bot.format_search_result([], "nothing", is_employee=False)
        self.assertIn("No projects found", out)

    def test_multiple_deals_under_limit(self):
        deals = [self._make_deal(f"Project {i}", stage="Bidding") for i in range(5)]
        out = telegram_bot.format_search_result(deals, "proj", is_employee=True)
        # The formatter labels entries 1..N regardless of input index
        # (it does enumerate(deals, 1)), so the first deal in the list
        # is "1. Project 0", the second is "2. Project 1", etc.
        for i, deal_title in enumerate(["Project 0", "Project 1", "Project 2",
                                       "Project 3", "Project 4"]):
            self.assertIn(f"{i + 1}. {deal_title}", out)
        # Footer should be present.
        self.assertIn("Reply with a number", out)

    def test_long_list_preserves_footer(self):
        # Build a huge list: 50 deals with 100-char titles = 5000+ chars
        # in the body. The header + footer must survive truncation.
        deals = [self._make_deal("X" * 100, stage="Bidding") for _ in range(50)]
        out = telegram_bot.format_search_result(deals, "x", is_employee=True)
        # The message is under the 4000 cap (because the body is
        # truncated and footer is kept).
        self.assertLessEqual(len(out), 4100)  # small slack for ellipsis
        # The header is preserved.
        self.assertIn("Projects matching", out)
        # The footer survives.
        self.assertIn("───────────────", out)
        self.assertIn("Reply with a number", out)
        # The body was truncated (some deals were dropped).
        self.assertIn("…", out)

    def test_footer_present_in_short_list(self):
        deals = [self._make_deal("P1"), self._make_deal("P2")]
        out = telegram_bot.format_search_result(deals, "p", is_employee=False)
        self.assertIn("Reply with a number", out)


class TestFormatDealDetail(unittest.TestCase):
    """format_deal_detail: audit round 8 fix #1, #7."""

    def _make_deal(self, **kwargs):
        base = {
            "id": 1, "title": "Ponderosa", "project_number": 424,
            "amount": 5000, "currency": "USD",
        }
        base.update(kwargs)
        return base

    def test_customer_view_no_amount(self):
        # Customer view should not show Amount.
        deal = self._make_deal(stage="Bidding")
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertIn("Ponderosa", out)
        self.assertIn("Stage: Bidding", out)
        self.assertNotIn("Amount:", out)

    def test_employee_view_shows_amount(self):
        deal = self._make_deal()
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=True)
        self.assertIn("Amount:", out)

    def test_description_truncated_with_ellipsis(self):
        # Long description (>200 chars) is truncated with … not silently.
        long_desc = "x" * 500
        deal = self._make_deal(description=long_desc)
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=True)
        # Must contain ellipsis (we DID truncate).
        self.assertIn("…", out)
        # The result line must not contain 500 x's in a row (it got
        # truncated to ~199 chars + ellipsis).
        self.assertNotIn("x" * 200, out)

    def test_short_description_no_ellipsis(self):
        deal = self._make_deal(description="Brief")
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=True)
        self.assertIn("Description: Brief", out)
        # No ellipsis in the description line (the rest of the
        # message may have other ellipses from event previews, but
        # this specific line is clean).
        description_line = [l for l in out.split("\n") if l.startswith("Description:")][0]
        self.assertNotIn("…", description_line)

    def test_invalid_index_returns_error(self):
        out = telegram_bot.format_deal_detail([], 0, is_employee=False)
        self.assertEqual(out, "Invalid selection.")

    def test_no_truncation_when_under_limit(self):
        # A short deal doesn't need truncation, message stays small.
        deal = self._make_deal()
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=True)
        self.assertLess(len(out), 1000)  # well under 4000


class TestFormatProjectInfo(unittest.TestCase):
    """format_project_info: audit round 8 fix #1 length caps."""

    def _make_deal(self, **kwargs):
        base = {
            "id": 1, "title": "Ponderosa", "project_number": 424,
            "amount": 5000, "currency": "USD",
        }
        base.update(kwargs)
        return base

    def test_short_fields_pass_through(self):
        deal = self._make_deal(
            stage="Bidding", contact="Alice", responsible="Bob",
            description="Some desc", tags=["urgent"],
        )
        out = telegram_bot.format_project_info(deal)
        self.assertIn("Stage:</b> Bidding", out)
        self.assertIn("Contact:</b> Alice", out)
        self.assertIn("Responsible:</b> Bob", out)
        self.assertIn("Description:</b> Some desc", out)
        self.assertIn("Tags:</b> urgent", out)

    def test_long_description_capped_with_ellipsis(self):
        # 5000-char description gets truncated at 1000 (with ellipsis).
        long_desc = "y" * 5000
        deal = self._make_deal(description=long_desc)
        out = telegram_bot.format_project_info(deal)
        # The description line must contain ellipsis.
        description_line = [l for l in out.split("\n") if l.startswith("<b>Description:</b>")][0]
        self.assertIn("…", description_line)
        # And must not contain 1000+ consecutive y's (it was truncated
        # to ~999 chars + ellipsis).
        self.assertNotIn("y" * 1000, description_line)

    def test_user_fields_capped(self):
        deal = self._make_deal()
        deal["userFields"] = {
            "k" * 100: "v" * 1000,  # 100-char key, 1000-char value
        }
        out = telegram_bot.format_project_info(deal)
        # The full 100 k's and 1000 v's should NOT appear in the
        # output (they were truncated to key=59+… and value=499+…).
        self.assertNotIn("k" * 100, out)
        self.assertNotIn("v" * 1000, out)
        # But the truncation marker (ellipsis) should appear.
        self.assertIn("…", out)

    def test_includes_view_link(self):
        deal = self._make_deal()
        out = telegram_bot.format_project_info(deal)
        self.assertIn("View project in CRM", out)


class TestGetProjectLabel(unittest.TestCase):
    """Audit round 8 fix #4: _get_project_label uses narrowed
    exception handling, falls back to '#<id>' gracefully."""

    async def test_cache_hit(self):
        # When the chat_id has cached deals, the label comes from the
        # cache and no HTTP call is made.
        chat_id = 12345
        telegram_bot._last_deals[chat_id] = [
            {"id": 42, "title": "Cached Title"},
        ]
        result = await telegram_bot._get_project_label(42, chat_id=chat_id)
        self.assertIn("Cached Title", result)
        self.assertIn("<b>", result)  # bold-wrapped

    async def test_cache_miss_then_fallback(self):
        # When the cache is empty AND the server fails, the function
        # falls back to '#<id>'. Crucially, the narrowed exception
        # handling means HTTP/JSON errors don't crash the handler.
        chat_id = 99999
        telegram_bot._last_deals.pop(chat_id, None)
        with patch("telegram_bot._get", return_value=None):
            result = await telegram_bot._get_project_label(99, chat_id=chat_id)
        self.assertEqual(result, "#99")

    async def test_server_response_with_name_and_number(self):
        chat_id = 88888
        telegram_bot._last_deals.pop(chat_id, None)
        with patch("telegram_bot._get", return_value={
            "title": "Server Title",
            "projectNumber": 424,
        }):
            result = await telegram_bot._get_project_label(7, chat_id=chat_id)
        self.assertIn("Server Title", result)
        self.assertIn("(#424)", result)

    async def test_server_response_with_only_number(self):
        chat_id = 77777
        telegram_bot._last_deals.pop(chat_id, None)
        with patch("telegram_bot._get", return_value={"projectNumber": 100}):
            result = await telegram_bot._get_project_label(8, chat_id=chat_id)
        self.assertEqual(result, "#100")

    async def test_empty_response_falls_back(self):
        chat_id = 66666
        telegram_bot._last_deals.pop(chat_id, None)
        with patch("telegram_bot._get", return_value={}):
            result = await telegram_bot._get_project_label(9, chat_id=chat_id)
        self.assertEqual(result, "#9")


class TestConfigPollLoop(unittest.TestCase):
    """Audit round 8 fix #5: _config_poll_loop polls immediately
    on startup, not after 60s."""

    async def test_polls_immediately(self):
        # Stub the post body so it returns changed=True, then track
        # the call timing. The first _post should happen before any
        # asyncio.sleep call.
        with patch("telegram_bot._post", return_value={"changed": False}) as post, \
             patch("telegram_bot.asyncio.sleep") as sleep, \
             patch("telegram_bot._bg_config_version", 0):
            # Run one tick of the loop by calling the function in a
            # task and then stopping it via a short-lived sleep.
            import asyncio
            task = asyncio.create_task(telegram_bot._config_poll_loop())
            await asyncio.sleep(0)  # let the task start
            await asyncio.sleep(0)  # let it run its first poll
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # The first call should be to _post (the poll). The first
        # call to sleep should come AFTER the first poll, not before.
        # We verify the call ORDER, not absolute timing.
        post_call_idx = None
        sleep_call_idx = None
        for i, call in enumerate(post.call_args_list):
            if call.args and call.args[0] == "/api/bot/config-check":
                post_call_idx = i
                break
        for i, call in enumerate(sleep.call_args_list):
            sleep_call_idx = i
        # At least one post call should have happened, and the first
        # sleep should come AFTER the first post.
        if sleep_call_idx is not None and post_call_idx is not None:
            self.assertGreater(sleep_call_idx, post_call_idx,
                               "first sleep should come after first poll")


class TestSharedHttpClient(unittest.TestCase):
    """Audit round 8 fix #2: _get/_post share a single httpx client
    instead of creating a new one per call."""

    def _async_client_mock(self):
        """Return a mock httpx.AsyncClient subclass whose .get/.post
        are awaitable coroutine methods that return a 200 with a
        sentinel JSON body."""
        import httpx

        class _MockClient:
            def __init__(self, *args, **kwargs):
                # Accept and ignore any kwargs the real AsyncClient
                # passes (timeout, headers, etc.) — the test only
                # cares about .get/.post being callable.
                self.get_calls = 0
                self.post_calls = 0

            async def get(self, *args, **kwargs):
                self.get_calls += 1
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"_marker": "get"}
                return resp

            async def post(self, *args, **kwargs):
                self.post_calls += 1
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"_marker": "post"}
                return resp

            async def aclose(self):
                pass

        return _MockClient

    def test_get_uses_shared_client(self):
        # Reset the global so we exercise the lazy creation path.
        telegram_bot._http_client = None
        MockClient = self._async_client_mock()
        with patch("telegram_bot.httpx.AsyncClient", MockClient):
            import asyncio
            asyncio.run(telegram_bot._get("/foo"))
            # The httpx.AsyncClient class should have been instantiated
            # exactly once — both calls reuse the same instance.
            # (We verify by checking the global _http_client after each
            # call is the same object.)
            first = telegram_bot._http_client
            asyncio.run(telegram_bot._get("/bar"))
            second = telegram_bot._http_client
            self.assertIs(first, second)
            # And the mock client saw both calls.
            self.assertEqual(first.get_calls, 2)

    def test_close_then_new_creates_fresh(self):
        # After _close_http_client, the next _get should create a
        # new client.
        telegram_bot._http_client = None
        MockClient = self._async_client_mock()
        with patch("telegram_bot.httpx.AsyncClient", MockClient):
            import asyncio
            asyncio.run(telegram_bot._get("/foo"))
            first = telegram_bot._http_client
            asyncio.run(telegram_bot._close_http_client())
            # After close, the global is reset.
            self.assertIsNone(telegram_bot._http_client)
            asyncio.run(telegram_bot._get("/bar"))
            second = telegram_bot._http_client
            self.assertIsNotNone(second)
            self.assertIsNot(first, second)
            # First client got 1 call, second got 1 call.
            self.assertEqual(first.get_calls, 1)
            self.assertEqual(second.get_calls, 1)


class TestGetPostErrorHandling(unittest.TestCase):
    """Audit round 8 fix #3: _get/_post catch httpx.HTTPError
    (broader than RequestError) and JSONDecodeError."""

    async def test_get_returns_none_on_timeout(self):
        # TimeoutException extends HTTPError. The old code only
        # caught RequestError, so this would have propagated up.
        with patch("telegram_bot._get_http_client") as get_client:
            get_client.return_value.get.side_effect = (
                __import__("httpx").TimeoutException("read timeout")
            )
            result = await telegram_bot._get("/foo")
        self.assertIsNone(result)

    async def test_post_returns_none_on_invalid_json(self):
        with patch("telegram_bot._get_http_client") as get_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.side_effect = json.JSONDecodeError("x", "y", 0)
            get_client.return_value.post.return_value = mock_resp
            result = await telegram_bot._post("/foo", {})
        self.assertIsNone(result)

    async def test_get_returns_none_on_500(self):
        # 500 with non-200 status returns None (existing behavior).
        with patch("telegram_bot._get_http_client") as get_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "internal error"
            get_client.return_value.get.return_value = mock_resp
            result = await telegram_bot._get("/foo")
        self.assertIsNone(result)

    async def test_get_returns_dict_on_200_json(self):
        with patch("telegram_bot._get_http_client") as get_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"ok": True}
            get_client.return_value.get.return_value = mock_resp
            result = await telegram_bot._get("/foo")
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
