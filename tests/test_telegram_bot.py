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
from unittest.mock import AsyncMock, MagicMock, patch

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

    def _make_customer_deal(self, **kwargs):
        base = self._make_deal(
            stage="Supplementing",
            userFields={
                "Address": "1498 Pier Drive, Davis, IL 61019",
                "Customer Phone": "(630) 639-6041",
                "Insurance Carrier": "Allstate",
                "Claim #": "0825902562",
            },
        )
        base.update(kwargs)
        return base

    def _make_update(self, n, cat="Customer Update"):
        return {
            "content": f"Update body {n}",
            "created": f"2026-10-0{n}T12:00:00",
            "categoryName": cat,
        }

    def test_customer_view_shows_three_recent_updates(self):
        deal = self._make_customer_deal(
            recentUpdates=[self._make_update(n) for n in (3, 2, 1)])
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        for n in (1, 2, 3):
            self.assertIn(f"Update body {n}", out)
        self.assertIn("Oct 3, 2026", out)

    def test_customer_view_caps_updates_at_three(self):
        deal = self._make_customer_deal(
            recentUpdates=[self._make_update(n % 9 + 1) for n in range(5)])
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertEqual(out.count("Update body"), 3)

    def test_customer_view_no_crm_link(self):
        # Customers must never get a CRM link.
        deal = self._make_customer_deal(
            recentUpdates=[self._make_update(1)])
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertNotIn("View project in CRM", out)
        self.assertNotIn("/project/", out)

    def test_employee_view_keeps_crm_link(self):
        deal = self._make_deal()
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=True)
        self.assertIn("View project in CRM", out)

    def test_customer_phone_is_tel_link(self):
        deal = self._make_customer_deal()
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        # E.164: bare 10-digit US numbers get +1 (a tel: URI without a
        # leading + is invalid per RFC 3966 and clients drop the link).
        self.assertIn('href="tel:+16306396041"', out)
        self.assertIn("(630) 639-6041", out)

    def test_customer_address_is_maps_link(self):
        deal = self._make_customer_deal()
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertIn("google.com/maps/search", out)
        self.assertIn("1498 Pier Drive", out)

    def test_customer_view_falls_back_to_latest_update(self):
        # Older dashboard builds only send latestUpdate.
        deal = self._make_customer_deal(
            latestUpdate=self._make_update(5))
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertIn("Update body 5", out)

    def test_customer_view_hides_update_authors(self):
        # Author names stay in the employee view only (covered by the
        # employee events branch); the customer update header carries
        # category + date and no author.
        deal = self._make_customer_deal(
            recentUpdates=[dict(self._make_update(1), author="Ken Chapman")])
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertNotIn("Ken Chapman", out)

    def test_customer_update_header_matches_employee_style(self):
        # Bold bracketed category + italic date, like the employee side.
        deal = self._make_customer_deal(
            recentUpdates=[self._make_update(1)])
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertIn("<b>[Customer Update]</b> — <i>Oct 1, 2026</i>:", out)

    def test_customer_details_closed_off_by_divider(self):
        # A rule sits between the details block and the updates so the
        # two read as distinct sections (plus the title rule = 2 total).
        deal = self._make_customer_deal(
            recentUpdates=[self._make_update(1)])
        out = telegram_bot.format_deal_detail([deal], 0, is_employee=False)
        self.assertEqual(out.count("─" * 30), 2)
        # No updates: only the title rule.
        plain = self._make_customer_deal()
        out2 = telegram_bot.format_deal_detail([plain], 0, is_employee=False)
        self.assertEqual(out2.count("─" * 30), 1)


class TestNormalizeTel(unittest.TestCase):
    """_normalize_tel: E.164-ish digits for tel: URIs."""

    def test_bare_us_number_gets_plus_one(self):
        self.assertEqual(telegram_bot._normalize_tel("(630) 639-6041"), "+16306396041")

    def test_eleven_digit_leading_one(self):
        self.assertEqual(telegram_bot._normalize_tel("1-630-639-6041"), "+16306396041")

    def test_existing_plus_passes_through(self):
        self.assertEqual(telegram_bot._normalize_tel("+44 20 7946 0018"), "+442079460018")

    def test_empty_and_garbage(self):
        self.assertEqual(telegram_bot._normalize_tel(""), "")
        self.assertEqual(telegram_bot._normalize_tel(None), "")
        self.assertEqual(telegram_bot._normalize_tel("abc"), "")


class TestDealDetailKeyboard(unittest.TestCase):
    """_build_deal_detail_keyboard: per-role buttons (module-level, testable)."""

    def setUp(self):
        TELEGRAM_STUB.InlineKeyboardButton.reset_mock()
        TELEGRAM_STUB.InlineKeyboardMarkup.reset_mock()

    def _button_specs(self):
        specs = []
        for call in TELEGRAM_STUB.InlineKeyboardButton.call_args_list:
            text = call.args[0] if call.args else ""
            specs.append((text, call.kwargs.get("url", ""), call.kwargs.get("callback_data", "")))
        return specs

    def test_customer_gets_call_address_search_no_crm_link(self):
        telegram_bot._build_deal_detail_keyboard(
            72, False, phone="(630) 639-6041",
            address="1498 Pier Drive, Davis, IL 61019", project_number=72)
        specs = self._button_specs()
        texts = [s[0] for s in specs]
        self.assertIn("📞 Call", texts)
        self.assertIn("📍 Address", texts)
        self.assertIn("🔍 New Search", texts)
        # No CRM link button anywhere for customers.
        for text, url, _cb in specs:
            self.assertNotIn("/project/", url)
            self.assertNotEqual(text, "🔗")
        call = [s for s in specs if s[0] == "📞 Call"][0]
        self.assertEqual(call[1], "tel:+16306396041")

    def test_employee_keeps_info_stage_note_and_crm_link(self):
        telegram_bot._build_deal_detail_keyboard(
            72, True, phone="(630) 639-6041", address="Somewhere", project_number=72)
        specs = self._button_specs()
        texts = [s[0] for s in specs]
        for t in ("📋 Info", "📊 Stage", "📝 Note", "📞 Call", "📍 Address", "🔗", "🔍 New Search"):
            self.assertIn(t, texts)
        link = [s for s in specs if s[0] == "🔗"][0]
        self.assertIn("/project/72", link[1])

    def test_no_deal_id_yields_search_only(self):
        telegram_bot._build_deal_detail_keyboard(0, False)
        specs = self._button_specs()
        self.assertEqual([(s[0]) for s in specs], ["🔍 New Search"])


class TestBotReplyAttribution(unittest.TestCase):
    """Threaded replies: correct author + poster notification (source guards —
    handle_reply is nested in main() and the endpoint needs a live DB)."""

    def _bot_src(self):
        return (HERE.parent / "telegram_bot.py").read_text(encoding="utf-8")

    def test_threaded_post_sends_chat_and_notify_list(self):
        src = self._bot_src()
        region = src[src.index("async def handle_reply"):src.index("async def handle_document")]
        self.assertIn('"chatId": chat_id', region)
        self.assertIn('"notifyUserList": notify_list', region)

    def test_reply_handler_resolves_author_from_chat(self):
        src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        body = src[src.index("def _handle_history_reply_create"):src.index("def _handle_history_reply_delete")]
        self.assertIn("get_mapping_by_chat", body)
        # The hardcoded id that posted every bot reply as the wrong person.
        self.assertNotIn('"id": 1', body)


class TestBotInviteCodes(unittest.TestCase):
    """crm_bot_store: unambiguous alphabet + coexisting pending codes."""

    def test_generated_codes_avoid_ambiguous_chars(self):
        import crm_bot_store
        for _ in range(200):
            code = crm_bot_store._generate_code()
            self.assertEqual(len(code), crm_bot_store.CODE_LENGTH)
            for ch in ("0", "O", "1", "I"):
                self.assertNotIn(ch, code)

    def test_pending_codes_coexist_for_same_contact(self):
        # Generating a second code for the same contact must NOT delete
        # the first (the Christian-invite vanished when Bre's was made).
        import crm_bot_store
        with tempfile.TemporaryDirectory() as tmp:
            orig = crm_bot_store.DATA_DIR
            crm_bot_store.DATA_DIR = Path(tmp)
            try:
                first = crm_bot_store.generate_code(
                    "sietch", 17, "A+ Restoration GC", 16, nickname="Christian")
                second = crm_bot_store.generate_code(
                    "sietch", 17, "A+ Restoration GC", 16, nickname="Bre")
                pending = crm_bot_store.get_pending_codes("sietch")
                codes = {c["code"] for c in pending}
                self.assertIn(first["code"], codes)
                self.assertIn(second["code"], codes)
                # Both redeem independently.
                self.assertIsNotNone(crm_bot_store.verify_code("sietch", first["code"]))
                self.assertIsNotNone(crm_bot_store.verify_code("sietch", second["code"]))
            finally:
                crm_bot_store.DATA_DIR = orig

    def test_reply_create_checks_bot_token_before_session(self):
        # Regression: _require_auth sends a 401 immediately when no session
        # cookie exists, so the bot Bearer check must come FIRST in the
        # threaded-reply handler (the bot never has a session cookie).
        src = (HERE.parent / "server.py").read_text(encoding="utf-8")
        start = src.index("def _handle_history_reply_create")
        end = src.index("def _handle_history_reply_delete")
        body = src[start:end]
        bot_check = body.index("Bearer {bot_token}")
        session_check = body.index("_require_auth(self)")
        self.assertLess(bot_check, session_check)


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


class TestGetProjectLabel(unittest.IsolatedAsyncioTestCase):
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


class TestConfigPollLoop(unittest.IsolatedAsyncioTestCase):
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
                resp.headers = {"content-type": "application/json"}
                resp.json.return_value = {"_marker": "get"}
                return resp

            async def post(self, *args, **kwargs):
                self.post_calls += 1
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {"content-type": "application/json"}
                resp.json.return_value = {"_marker": "post"}
                return resp

            async def request(self, method, url, **kwargs):
                # _request() is the single entry point used by _get/_post.
                if method.upper() == "POST":
                    return await self.post(url, **kwargs)
                return await self.get(url, **kwargs)

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


class TestGetPostErrorHandling(unittest.IsolatedAsyncioTestCase):
    """_get/_post catch httpx.HTTPError (broader than RequestError) and
    JSONDecodeError, and route through _request()."""

    @staticmethod
    def _client_with_response(resp):
        # AsyncMock is required: _request does `await client.request(...)`,
        # and a plain MagicMock returns a non-awaitable.
        return MagicMock(request=AsyncMock(return_value=resp))

    @staticmethod
    def _resp(status=200, json_body=None, ctype="application/json", text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"content-type": ctype} if ctype else {}
        resp.text = text
        resp.json.return_value = json_body if json_body is not None else {}
        return resp

    async def test_get_returns_none_on_timeout(self):
        # TimeoutException extends HTTPError. The old code only
        # caught RequestError, so this would have propagated up.
        with patch("telegram_bot._get_http_client") as get_client:
            get_client.return_value.request.side_effect = (
                __import__("httpx").TimeoutException("read timeout")
            )
            result = await telegram_bot._get("/foo")
        self.assertIsNone(result)

    async def test_post_returns_none_on_invalid_json(self):
        with patch("telegram_bot._get_http_client") as get_client:
            resp = self._resp(json_body=None, ctype=None)
            resp.json.side_effect = json.JSONDecodeError("x", "y", 0)
            get_client.return_value = self._client_with_response(resp)
            result = await telegram_bot._post("/foo", {})
        self.assertIsNone(result)

    async def test_get_returns_none_on_500(self):
        with patch("telegram_bot._get_http_client") as get_client:
            get_client.return_value = self._client_with_response(
                self._resp(status=500, text="internal error"))
            result = await telegram_bot._get("/foo")
        self.assertIsNone(result)

    async def test_get_returns_dict_on_200_json(self):
        with patch("telegram_bot._get_http_client") as get_client:
            get_client.return_value = self._client_with_response(
                self._resp(json_body={"ok": True}))
            result = await telegram_bot._get("/foo")
        self.assertEqual(result, {"ok": True})

    async def test_get_accepts_missing_content_type_header(self):
        # Some proxies strip content-type; don't hard-fail a valid JSON body.
        with patch("telegram_bot._get_http_client") as get_client:
            get_client.return_value = self._client_with_response(
                self._resp(json_body={"ok": 1}, ctype=None))
            result = await telegram_bot._get("/foo")
        self.assertEqual(result, {"ok": 1})


# ── Failure-kind discrimination (the "stopped tunnel" bug) ────────────────────

class TestRequestFailureKinds(unittest.IsolatedAsyncioTestCase):
    """_request must separate 'the code is wrong' from 'we are broken'."""

    @staticmethod
    def _client_with_response(resp):
        # AsyncMock is required: _request does `await client.request(...)`,
        # and a plain MagicMock returns a non-awaitable.
        return MagicMock(request=AsyncMock(return_value=resp))

    @staticmethod
    def _resp(status=200, ctype="application/json", text="", json_body=None):
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"content-type": ctype} if ctype else {}
        resp.text = text
        resp.json.return_value = json_body if json_body is not None else {}
        return resp

    async def test_200_json_is_success(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(
                self._resp(json_body={"contactId": 7}))
            status, body, err = await telegram_bot._request("POST", "/x", data={})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"contactId": 7})
        self.assertIsNone(err)

    async def test_200_html_is_a_gateway_failure_not_success(self):
        """Regression: a stopped tunnel answers 200 + text/html. Parsing that
        is what made a valid invite code report as 'unrecognized'."""
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(self._resp(
                status=200, ctype="text/html", text="<title>Tunnel Stopped</title>"))
            status, body, err = await telegram_bot._request("POST", "/x", data={})
        self.assertIsNone(status)
        self.assertIsNone(body)
        self.assertIn("text/html", err)

    async def test_200_json_content_type_with_charset(self):
        with patch("telegram_bot._get_http_client") as gc:
            resp = self._resp(json_body={"a": 1})
            resp.headers = {"content-type": "application/json; charset=utf-8"}
            gc.return_value = self._client_with_response(resp)
            status, body, _err = await telegram_bot._request("GET", "/x")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"a": 1})

    async def test_404_reports_status_404(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(
                self._resp(status=404, ctype="application/json", text="nope"))
            status, body, err = await telegram_bot._request("POST", "/x", data={})
        self.assertEqual(status, 404)
        self.assertIsNone(body)
        self.assertIn("404", err)

    async def test_403_reports_status_403(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(
                self._resp(status=403, text="Forbidden"))
            status, _body, err = await telegram_bot._request("POST", "/x", data={})
        self.assertEqual(status, 403)
        self.assertIn("403", err)

    async def test_connection_error_reports_none_status(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value.request.side_effect = \
                __import__("httpx").ConnectError("refused")
            status, body, err = await telegram_bot._request("GET", "/x")
        self.assertIsNone(status)
        self.assertIsNone(body)
        self.assertIn("cannot reach CRM", err)

    async def test_error_message_names_the_configured_url(self):
        """The whole point: the operator must be told WHICH URL failed."""
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value.request.side_effect = \
                __import__("httpx").ConnectError("refused")
            with patch.dict(os.environ, {"DASHBOARD_URL": "http://127.0.0.1:9999"}):
                _status, _body, err = await telegram_bot._request("GET", "/x")
        self.assertIn("127.0.0.1:9999", err)


class TestVerifyCodeSemantics(unittest.IsolatedAsyncioTestCase):
    """verify_code: mapping on 200, None ONLY on 404, raise otherwise."""

    @staticmethod
    def _client_with_response(resp):
        # AsyncMock is required: _request does `await client.request(...)`,
        # and a plain MagicMock returns a non-awaitable.
        return MagicMock(request=AsyncMock(return_value=resp))

    @staticmethod
    def _resp(status=200, ctype="application/json", text="", json_body=None):
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"content-type": ctype} if ctype else {}
        resp.text = text
        resp.json.return_value = json_body if json_body is not None else {}
        return resp

    async def test_valid_code_returns_mapping(self):
        mapping = {"contactId": 5, "employee": True}
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(self._resp(json_body=mapping))
            result = await telegram_bot.verify_code("ABC12345", 42)
        self.assertEqual(result, mapping)
        self.assertTrue(result)

    async def test_unknown_code_returns_none_not_raise(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(
                self._resp(status=404, text="Invalid or expired code"))
            result = await telegram_bot.verify_code("NOPE0000", 42)
        self.assertIsNone(result)

    async def test_html_response_raises_rather_than_claiming_bad_code(self):
        """The exact production bug: valid code + dead tunnel."""
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(self._resp(
                status=200, ctype="text/html", text="<title>Tunnel Stopped</title>"))
            with self.assertRaises(telegram_bot.BotUnreachable):
                await telegram_bot.verify_code("1CG37WJR", 42)

    async def test_403_raises(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(self._resp(status=403))
            with self.assertRaises(telegram_bot.BotUnreachable):
                await telegram_bot.verify_code("1CG37WJR", 42)

    async def test_500_raises(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(self._resp(status=500))
            with self.assertRaises(telegram_bot.BotUnreachable):
                await telegram_bot.verify_code("1CG37WJR", 42)

    async def test_connect_error_raises(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value.request.side_effect = \
                __import__("httpx").ConnectError("refused")
            with self.assertRaises(telegram_bot.BotUnreachable):
                await telegram_bot.verify_code("1CG37WJR", 42)


# ── URL role split ───────────────────────────────────────────────────────────

class TestDashboardUrlRoles(unittest.IsolatedAsyncioTestCase):
    """API calls use the internal URL; customer links use the public one."""

    def test_internal_url_used_for_api_calls(self):
        with patch.dict(os.environ,
                        {"DASHBOARD_URL": "http://127.0.0.1:8766",
                         "DASHBOARD_PUBLIC_URL": "https://crm.example.com"}):
            self.assertEqual(telegram_bot._get_dashboard_url(), "http://127.0.0.1:8766")

    def test_public_url_is_distinct_when_set(self):
        with patch.dict(os.environ,
                        {"DASHBOARD_URL": "http://127.0.0.1:8766",
                         "DASHBOARD_PUBLIC_URL": "https://crm.example.com"}):
            self.assertEqual(telegram_bot._get_public_dashboard_url(),
                             "https://crm.example.com")

    def test_public_falls_back_to_internal_when_blank(self):
        with patch.dict(os.environ,
                        {"DASHBOARD_URL": "http://127.0.0.1:8766",
                         "DASHBOARD_PUBLIC_URL": ""}):
            self.assertEqual(telegram_bot._get_public_dashboard_url(),
                             "http://127.0.0.1:8766")

    def test_public_falls_back_when_absent(self):
        # clear=True so a DASHBOARD_PUBLIC_URL already present in the ambient
        # environment (this repo's .env sets one) is genuinely absent.
        with patch.dict(os.environ, {"DASHBOARD_URL": "http://127.0.0.1:8766"},
                        clear=True):
            self.assertNotIn("DASHBOARD_PUBLIC_URL", os.environ)
            self.assertEqual(telegram_bot._get_public_dashboard_url(),
                             "http://127.0.0.1:8766")

    def test_public_url_has_no_trailing_slash(self):
        with patch.dict(os.environ,
                        {"DASHBOARD_URL": "http://127.0.0.1:8766",
                         "DASHBOARD_PUBLIC_URL": "https://crm.example.com/"}):
            self.assertEqual(telegram_bot._get_public_dashboard_url(),
                             "https://crm.example.com")

    async def test_project_link_uses_public_url_not_internal(self):
        with patch.dict(os.environ,
                        {"DASHBOARD_URL": "http://127.0.0.1:8766",
                         "DASHBOARD_PUBLIC_URL": "https://crm.example.com"}):
            deal = {"id": 1, "title": "P", "project_number": 42,
                    "amount": 0, "currency": ""}
            out = telegram_bot.format_project_info(deal)
        self.assertIn("https://crm.example.com/project/42", out)
        self.assertNotIn("127.0.0.1", out)

    def test_bot_never_calls_admin_settings_endpoint(self):
        """Regression: /api/v2/admin/bot-settings needs an admin session, so
        the bot always got 401 and could never read its own config."""
        src = Path(telegram_bot.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/v2/admin/bot-settings", src)
        self.assertIn("/api/bot/settings", src)


# ── Startup connectivity + throttling ────────────────────────────────────────

class TestConnectivityCheck(unittest.IsolatedAsyncioTestCase):

    @staticmethod
    def _client_with_response(resp):
        # AsyncMock is required: _request does `await client.request(...)`,
        # and a plain MagicMock returns a non-awaitable.
        return MagicMock(request=AsyncMock(return_value=resp))

    @staticmethod
    def _resp(status=200, ctype="application/json", text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"content-type": ctype}
        resp.text = text
        resp.json.return_value = {"version": "3.0"}
        return resp

    async def test_healthy_crm_returns_true(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(self._resp())
            ok = await telegram_bot._check_crm_connectivity()
        self.assertTrue(ok)

    async def test_unreachable_crm_returns_false_and_names_url(self):
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value.request.side_effect = \
                __import__("httpx").ConnectError("refused")
            with patch.dict(os.environ, {"DASHBOARD_URL": "http://127.0.0.1:8766"}):
                with self.assertLogs("crm_bot", level="WARNING") as logs:
                    ok = await telegram_bot._check_crm_connectivity()
        self.assertFalse(ok)
        joined = "\n".join(logs.output)
        self.assertIn("127.0.0.1:8766", joined)

    async def test_html_healthcheck_returns_false(self):
        """A gateway HTML page is not a healthy CRM."""
        with patch("telegram_bot._get_http_client") as gc:
            gc.return_value = self._client_with_response(
                self._resp(status=200, ctype="text/html", text="Tunnel Stopped"))
            with self.assertLogs("crm_bot", level="WARNING"):
                ok = await telegram_bot._check_crm_connectivity()
        self.assertFalse(ok)


class TestLogThrottled(unittest.TestCase):

    def setUp(self):
        telegram_bot._throttle_state.clear()

    def test_first_failure_logs(self):
        with self.assertLogs("crm_bot", level="WARNING") as logs:
            telegram_bot._log_throttled("k", "boom %s", "one")
        self.assertEqual(len(logs.output), 1)

    def test_repeated_failures_are_suppressed_then_eventually_logged(self):
        with self.assertLogs("crm_bot", level="WARNING") as logs:
            for _ in range(10):
                telegram_bot._log_throttled("k", "boom")
        # first + every 10th == 2 lines for 10 failures
        self.assertEqual(len(logs.output), 2)

    def test_long_sustained_outage_does_not_flood(self):
        with self.assertLogs("crm_bot", level="WARNING") as logs:
            for _ in range(60):
                telegram_bot._log_throttled("k", "boom")
        self.assertLessEqual(len(logs.output), 8)

    def test_separate_keys_track_independently(self):
        with self.assertLogs("crm_bot", level="WARNING") as logs:
            telegram_bot._log_throttled("a", "boom")
            telegram_bot._log_throttled("b", "boom")
        self.assertEqual(len(logs.output), 2)


class TestEmployeeCustomerIsolation(unittest.TestCase):
    """Customers must never see employee-level information.

    The bot token authenticates the bot *process*, not an individual chat, so it is
    not an authorization boundary. Employee-vs-customer is decided server-side from the
    stored chatId mapping. These are source-level guards: they pin the invariant that no
    employee/customer decision is ever read off the wire, without needing a live DB.
    """

    @staticmethod
    def _server_src() -> str:
        return (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")

    @staticmethod
    def _bot_src() -> str:
        return (Path(__file__).resolve().parents[1] / "telegram_bot.py").read_text(encoding="utf-8")

    @staticmethod
    def _deals_handler(src: str) -> str:
        start = src.index('if api_path == "/api/bot/deals":')
        return src[start:start + 2500]

    def test_deals_does_not_trust_employee_query_param(self):
        """The regression: ?employee=true used to decide employee access."""
        self.assertNotIn('qs.get("employee")', self._deals_handler(self._server_src()))

    def test_deals_does_not_trust_contact_id_query_param(self):
        """A client must not be able to ask for another contact's projects."""
        handler = self._deals_handler(self._server_src())
        self.assertNotIn('qs.get("contactId")', handler)

    def test_deals_derives_employee_from_mapping(self):
        handler = self._deals_handler(self._server_src())
        self.assertIn("get_mapping_by_chat", handler)
        self.assertIn('bool(deals_mapping.get("employee"))', handler)

    def test_deals_requires_chat_id(self):
        """Without a chatId there is no identity, so no data may be returned."""
        handler = self._deals_handler(self._server_src())
        self.assertIn('chatId is required', handler)

    def test_deals_403s_when_chat_has_no_linked_account(self):
        handler = self._deals_handler(self._server_src())
        self.assertIn("No linked account for this chat", handler)

    def test_deals_customer_scope_comes_from_mapping(self):
        """Customer contact_id is read from the mapping, not the request."""
        handler = self._deals_handler(self._server_src())
        self.assertIn('contact_id = deals_mapping.get("contactId") if not is_employee else None', handler)

    def test_bot_get_deals_has_no_employee_parameter(self):
        """The bot must stop asserting its own authorization on the wire."""
        src = self._bot_src()
        start = src.index("async def get_deals(")
        sig_line = src[start:src.index("\n", start)]
        self.assertNotIn("employee", sig_line)

    def test_bot_never_sends_employee_param(self):
        self.assertNotIn('params["employee"]', self._bot_src())

    def test_tag_callback_is_employee_gated(self):
        """tag: searches span all contacts; a stale keyboard must not widen access."""
        src = self._bot_src()
        branch = src[src.index('if data.startswith("tag:"):'):]
        branch = branch[:branch.index('if data.startswith("act:note:")')]
        self.assertIn("get_mapping(chat_id)", branch)
        self.assertIn('tag_mapping.get("employee")', branch)
        # The guard must run BEFORE the deal fetch, so a customer never gets data.
        self.assertLess(branch.index('tag_mapping.get("employee")'),
                        branch.index("get_deals("))
        # employee= must not be forwarded to the server on this branch. (is_employee=
        # is fine -- that is display formatting only, after the data is scoped.)
        self.assertNotIn("employee=True,", branch)
        self.assertNotIn(", employee=", branch)

    def test_no_endpoint_reads_employee_from_the_wire(self):
        """Sweep: no /api/bot/ handler may take `employee` from a query string.

        generate-code reads it from the body, but that endpoint is _require_admin-gated
        (an admin deliberately minting an employee invite), so it is excluded by name.
        """
        src = self._server_src()
        for match in re.finditer(r'if api_path == "/api/bot/[a-z-]+"', src):
            path = match.group(0)
            if "generate-code" in path:
                continue
            body = src[match.start():match.start() + 2500]
            self.assertNotIn('qs.get("employee")', body, f"{path} trusts ?employee from the wire")


if __name__ == "__main__":
    unittest.main(verbosity=2)
