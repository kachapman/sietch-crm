"""Telegram bot for customer project status queries — with inline keyboards.

Usage:
  TELEGRAM_BOT_TOKEN=xxx DASHBOARD_URL=http://127.0.0.1:8765 python3 telegram_bot.py
  Or installed as systemd service (see docs/crm-telegram-bot.service).

Flow (once linked):
  Customers: send a project name → tap result → see deal detail with latest update.
  Employees:  send a project name → tap result → see full history + add notes.
  Both:       old-style number-reply fallback still works during transition.
"""

from __future__ import annotations

import asyncio
import email.utils
import html
import json
import logging
from html.parser import HTMLParser
import os
import re
import sys

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("crm_bot")

# Load .env file like server.py does
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    for line in open(_env_path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORTAL = os.environ.get("ONLYOFFICE_PORTAL_URL", "")

# Last search results per chat, used to interpret "1", "2", ... replies.
_last_search: dict[int, str] = {}
_last_deals: dict[int, list[dict]] = {}
_last_employee: dict[int, bool] = {}
_pending_note: dict[int, dict] = {}

# ── Helpers ──

BOT_AUTH_HEADERS: dict[str, str] = {}


def _auth_headers() -> dict[str, str]:
    if not BOT_AUTH_HEADERS and TOKEN:
        BOT_AUTH_HEADERS["Authorization"] = f"Bearer {TOKEN}"
    return BOT_AUTH_HEADERS


def _fmt_money(amount: float, currency: str) -> str:
    if amount <= 0:
        return ""
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "": "$"}
    sym = symbols.get(currency.upper(), f"{currency} ")
    if amount == int(amount):
        return f"{sym}{int(amount):,}"
    return f"{sym}{amount:,.2f}"


def _fmt_date(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = iso_str.replace("T", " ").replace("Z", "").split(".")[0]
        parts = dt.split(" ")
        date_part = parts[0]
        if len(date_part) >= 10:
            y, m, d = date_part[:4], date_part[5:7], date_part[8:10]
            months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            m_name = months[int(m)] if 1 <= int(m) <= 12 else m
            return f"{m_name} {int(d)}, {y}"
    except Exception:
        pass
    return iso_str[:10]


# ── API Calls ──

async def _get(path: str, params: dict | None = None) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{DASHBOARD_URL}{path}",
                params=params,
                headers=_auth_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("GET %s -> %d %s", path, resp.status_code, resp.text[:200])
            return None
    except httpx.RequestError as e:
        logger.error("GET %s failed: %s", path, e)
        return None


async def _post(path: str, data: dict) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{DASHBOARD_URL}{path}",
                json=data,
                headers=_auth_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("POST %s -> %d %s", path, resp.status_code, resp.text[:200])
            return None
    except httpx.RequestError as e:
        logger.error("POST %s failed: %s", path, e)
        return None


async def get_mapping(chat_id: int) -> dict | None:
    return await _get("/api/bot/me", {"chatId": chat_id})


async def get_deals(contact_id: int | None, chat_id: int | None = None, search: str | None = None, employee: bool = False, tag: str | None = None) -> list[dict] | None:
    params: dict = {}
    if contact_id is not None:
        params["contactId"] = contact_id
    if chat_id:
        params["chatId"] = chat_id
    if search:
        params["search"] = search
    if employee:
        params["employee"] = "true"
    if tag:
        params["tag"] = tag
    result = await _get("/api/bot/deals", params)
    if result is None:
        return None  # API error (unreachable / timeout)
    if "deals" in result:
        return result["deals"]
    return []


async def get_categories() -> list[dict] | None:
    result = await _get("/api/bot/categories")
    if isinstance(result, list):
        return result
    return None


async def get_tags() -> list[dict] | None:
    result = await _get("/api/bot/tags")
    if isinstance(result, dict) and "tags" in result:
        return result["tags"]
    return None


async def verify_code(code: str, chat_id: int) -> dict | None:
    return await _post("/api/bot-customers/verify-code", {
        "code": code,
        "chatId": chat_id,
        "portal": PORTAL,
    })


# ── Message Formatting ──

def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


_TELEGRAM_HTML_TAGS = frozenset({
    'b', 'strong', 'i', 'em', 'u', 'ins',
    's', 'strike', 'del',
    'a', 'code', 'pre',
})


class _TelegramHTMLSanitizer(HTMLParser):
    """Strip unsupported tags (keep their text) and preserve allowed Telegram tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.result: list[str] = []
        self.open_tags: list[str] = []

    def _escape_text(self, text: str) -> str:
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _build_attrs(self, attrs: list[tuple[str, str | None]]) -> str:
        parts: list[str] = []
        for key, value in attrs:
            if value is None:
                continue
            # href for <a> is handled separately; skip it here.
            if key.lower() == 'href':
                continue
            escaped = (value
                       .replace('&', '&amp;')
                       .replace('"', '&quot;')
                       .replace("'", '&#39;')
                       .replace('<', '&lt;')
                       .replace('>', '&gt;'))
            parts.append(f' {key.lower()}="{escaped}"')
        return ''.join(parts)

    def _rebuild_a_tag(self, attrs: list[tuple[str, str | None]]) -> str | None:
        href = None
        for key, value in attrs:
            if key.lower() == 'href' and value:
                href = value
                break
        if not href:
            return None
        href = (href
                .replace('&', '&amp;')
                .replace('"', '&quot;')
                .replace("'", '&#39;')
                .replace('<', '&lt;')
                .replace('>', '&gt;'))
        return f'<a href="{href}">'

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower == 'br':
            self.result.append('\n')
            return
        if tag_lower not in _TELEGRAM_HTML_TAGS:
            return
        if tag_lower == 'a':
            rebuilt = self._rebuild_a_tag(attrs)
            if rebuilt:
                self.result.append(rebuilt)
                self.open_tags.append('a')
            return
        attrs_str = self._build_attrs(attrs)
        self.result.append(f'<{tag_lower}{attrs_str}>')
        self.open_tags.append(tag_lower)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower == 'br':
            self.result.append('\n')
            return
        if tag_lower not in _TELEGRAM_HTML_TAGS:
            return
        if tag_lower == 'a':
            # Self-closing <a /> is invalid in Telegram; strip it.
            return
        attrs_str = self._build_attrs(attrs)
        self.result.append(f'<{tag_lower}{attrs_str}/>')

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower not in _TELEGRAM_HTML_TAGS:
            return
        if self.open_tags and self.open_tags[-1] == tag_lower:
            self.open_tags.pop()
            self.result.append(f'</{tag_lower}>')
        # Dangling closers are dropped.

    def handle_data(self, data: str) -> None:
        self.result.append(self._escape_text(data))

    def handle_entityref(self, name: str) -> None:
        ch = html.unescape(f'&{name};')
        self.result.append(self._escape_text(ch))

    def handle_charref(self, name: str) -> None:
        ch = html.unescape(f'&#{name};')
        self.result.append(self._escape_text(ch))

    def close(self) -> str:  # type: ignore[override]
        super().close()
        for tag in reversed(self.open_tags):
            self.result.append(f'</{tag}>')
        return ''.join(self.result)


def _sanitize_html(text: str) -> str:
    """Sanitize CRM HTML for Telegram's HTML parse mode.

    - Strip unsupported tags but keep their text content.
    - Preserve allowed tags: b, strong, i, em, u, ins, s, strike, del, a, code, pre.
    - Escape stray <, >, & in text.
    - Convert <br> to newlines.
    """
    if not text:
        return ""
    parser = _TelegramHTMLSanitizer()
    parser.feed(text)
    return parser.close()


def _truncate_html(msg: str, max_len: int, ellipsis: str = "\n\n… (Telegram message limit reached)") -> str:
    """Truncate an HTML message without leaving dangling tags.

    Removes any partial tag at the truncation point and closes any tags
    that are still open, so Telegram's HTML parse mode stays valid.
    """
    if len(msg) <= max_len:
        return msg
    limit = max_len - len(ellipsis)
    truncated = msg[:limit]
    # Drop a partial HTML tag at the end.
    truncated = re.sub(r"<[^>]*$", "", truncated)
    # Close any tags that are still open.
    open_tags: list[str] = []
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>", truncated):
        is_close = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag in ("br", "img", "hr"):
            continue
        if is_close:
            if open_tags and open_tags[-1] == tag:
                open_tags.pop()
        else:
            open_tags.append(tag)
    for tag in reversed(open_tags):
        truncated += f"</{tag}>"
    return truncated + ellipsis


def _strip_html_tags(text: str) -> str:
    """Remove HTML tags and decode entities for plain-text fallback."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _html_to_text(text: str) -> str:
    """Convert CRM email HTML to tight plain text.

    Preserves paragraph breaks, flattens tables to "Header: Value" lines,
    and removes the stray newlines that break words across lines.
    """
    if not text:
        return ""
    PARA = "\x00"        # regular block boundary -> single newline
    TABLE_END = "\x02"    # end of table -> blank line
    ROW = "\x01"
    # <br> inside a paragraph should be a space, not a line break.
    text = re.sub(r"<\s*br\s*/?\s*>", " ", text, flags=re.IGNORECASE)
    # Block element boundaries become paragraph markers.
    text = re.sub(r"<\s*/\s*(?:p|div|h[1-6]|li)\s*>", PARA, text, flags=re.IGNORECASE)
    # End of a table marks a stronger break so the following body text is
    # separated from the forwarded-message header rows.
    text = re.sub(r"<\s*/\s*(?:table|tbody|thead|tfoot)\s*>", TABLE_END, text, flags=re.IGNORECASE)
    # Flatten simple table rows to single lines.
    text = re.sub(r"<\s*/\s*th\s*>\s*<\s*td\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*/\s*td\s*>\s*<\s*td\s*>", ", ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*/\s*tr\s*>", ROW, text, flags=re.IGNORECASE)
    # Drop remaining table/structural tags.
    text = re.sub(r"<\s*(?:table|tbody|thead|tfoot|tr|th|td|p|div|h[1-6]|li).*?>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*/\s*(?:table|tbody|thead|tfoot|tr|th|td|p|div|h[1-6]|li)\s*>", "", text, flags=re.IGNORECASE)
    # Strip any other tags (anchors lose their href, but link text remains).
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape entities.
    text = html.unescape(text)
    # Collapse runs of whitespace to single spaces, but keep our markers.
    text = re.sub(r"[ \t\r\n]+", " ", text)
    # Restore row breaks and paragraph breaks, trimming surrounding spaces.
    text = re.sub(rf" ?{re.escape(ROW)} ?", "\n", text)
    text = re.sub(rf" ?{re.escape(PARA)} ?", "\n", text)
    text = re.sub(rf" ?{re.escape(TABLE_END)} ?", "\n\n", text)
    # Clean up excessive blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_mail_fields(content: str) -> dict[str, str] | None:
    """Best-effort extraction from possibly truncated CRM mail JSON."""
    fields: dict[str, str] = {}
    patterns = {
        "from": r'"from"\s*:\s*"((?:\\.|[^"\\])*)"',
        "to": r'"to"\s*:\s*"((?:\\.|[^"\\])*)"',
        "subject": r'"subject"\s*:\s*"((?:\\.|[^"\\])*)"',
        "introduction": r'"introduction"\s*:\s*"((?:\\.|[^"\\])*)"',
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            value = m.group(1)
            value = value.replace('\\\\', '\\').replace('\\"', '"').replace('\\n', '\n').replace('\\r', '').replace('\\t', '\t')
            fields[key] = value
    return fields if fields else None


def _extract_forward_info(body: str) -> tuple[str, str | None]:
    """Find a Forwarded/Original Message header block, extract From/Date, and return (cleaned_body, info_line)."""
    marker_re = re.compile(r'--------\s*(Forwarded|Original)\s*Message\s*--------\s*', re.IGNORECASE)
    m = marker_re.search(body)
    if not m:
        return body, None

    marker_type = m.group(1).lower()
    header_field_re = re.compile(
        r'(Subject|Date|From|Reply-To|To|Cc|Bcc):\s*',
        re.IGNORECASE,
    )
    pos = m.end()
    fields: dict[str, str] = {}

    MAX_HEADER_VALUE_LEN = 250

    while True:
        fm = header_field_re.match(body, pos)
        if not fm:
            break
        key = fm.group(1).lower()
        value_start = fm.end()
        next_fm = header_field_re.search(body, value_start)

        if next_fm:
            # More headers follow; stop exactly at the next header.
            end = next_fm.start()
            pos = next_fm.start()
        else:
            # Last header: look for the body start (blank line, reply attribution,
            # quoted line, or a hard cap).
            body_indicator_pos = len(body)
            blank_line = re.search(r'\n\s*\n', body[value_start:])
            if blank_line:
                body_indicator_pos = value_start + blank_line.start()
            reply_indicator = re.search(r'\s+On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)', body[value_start:], re.IGNORECASE)
            if reply_indicator:
                body_indicator_pos = min(body_indicator_pos, value_start + reply_indicator.start())
            body_indicator = re.search(r'(?<=\s)[\[>]', body[value_start:])
            if body_indicator:
                body_indicator_pos = min(body_indicator_pos, value_start + body_indicator.start())
            # Cap individual header values so a long body doesn't get swallowed.
            max_pos = value_start + MAX_HEADER_VALUE_LEN
            if body_indicator_pos > max_pos:
                body_indicator_pos = max_pos
            end = body_indicator_pos
            pos = end

        value = body[value_start:end].strip()
        if key in ("from", "date", "subject", "to"):
            fields[key] = value

    info_parts: list[str] = []
    if "from" in fields:
        info_parts.append(f"from {fields['from']}")
    if "date" in fields:
        info_parts.append(f"on {fields['date']}")

    if info_parts:
        info = "Forwarded " + " ".join(info_parts)
    else:
        info = "Forwarded message" if marker_type == "forwarded" else "Original message"

    before = body[:m.start()].rstrip()
    after = body[pos:].lstrip()
    cleaned = (before + ("\n\n" if before else "") + after).strip()
    return cleaned, _esc(f"[{info}]")


def _clean_reply_attribution(body: str) -> tuple[str, str | None]:
    """Convert email-style 'On <date>, <sender> wrote:' into a clean [On ... wrote] line."""
    reply_re = re.compile(r'\s+On\s+(.+?)\s+wrote:\s*', re.IGNORECASE)
    starts_with_date = re.compile(
        r'^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*(?:,\s+))?'
        r'(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})',
        re.IGNORECASE,
    )
    pos = 0
    while True:
        m = reply_re.search(body, pos)
        if not m:
            return body, None
        attribution = m.group(1).strip()
        if starts_with_date.match(attribution):
            info = _esc(f"On {attribution} wrote")
            before = body[:m.start()].rstrip()
            after = body[m.end():].lstrip()
            cleaned = (before + f"\n\n[{info}]" + ('\n\n' + after if after else '')).strip()
            return cleaned, info
        pos = m.start() + 1


def _format_mail_event(content: str, max_len: int = 1500) -> str:
    """Parse CRM mail JSON and return a concise readable block."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = _extract_mail_fields(content)
        if data is None:
            return _sanitize_html(content)

    def _parse_address(raw: str) -> str:
        if not raw:
            return ""
        name, addr = email.utils.parseaddr(raw)
        if name and addr:
            return _esc(f"{name} <{addr}>")
        if addr:
            return _esc(addr)
        return _esc(raw)

    from_addr = _parse_address(str(data.get("from") or ""))
    to_addr = _parse_address(str(data.get("to") or ""))
    subject = _esc(str(data.get("subject") or ""))
    body = str(data.get("introduction") or data.get("body") or "")
    # The CRM mail handler returns raw HTML; convert it to readable text before
    # we apply forward/reply cleanup and truncation.
    if "<" in body and ">" in body:
        body = _html_to_text(body)
    body, forward_info = _extract_forward_info(body)
    body, _ = _clean_reply_attribution(body)
    # Convert markdown links to bare URLs so they don't render as raw `[text](url)`.
    body = re.sub(r'\[([^\]]*)\]\(([^)]+)\)', r'\2', body)
    # Put each quoted email line on its own line for readability.
    body = re.sub(r'\s*>\s*', '\n> ', body).strip()
    if len(body) > max_len:
        marker = " [truncated]"
        body = body[:max_len - len(marker)] + marker
    body = _sanitize_html(body)

    lines: list[str] = []
    if from_addr:
        lines.append(f"From: {from_addr}")
    if to_addr:
        lines.append(f"To: {to_addr}")
    if subject:
        lines.append(f"Subject: {subject}")
    if forward_info:
        lines.append("")
        lines.append(forward_info)
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


def _format_event_body(category: str, content: str, max_len: int = 500) -> str:
    cat_lower = category.lower()
    if 'mail' in cat_lower or 'email' in cat_lower:
        return _format_mail_event(content, max_len=max_len)
    if len(content) > max_len:
        content = content[:max_len - 1] + "…"
    return _sanitize_html(content)


HELP_TEXT_CUSTOMER = (
    "Send a project name (or part of it) to look it up.\n"
    "If several match, tap a button — or reply with the number — to see full details.\n"
    "Need a new invite code? Contact Sietch CRM."
)
HELP_TEXT_EMPLOYEE = (
    "Send a project name (or part of it) to search across all projects.\n"
    "If several match, tap a button — or reply with the number — to see full details.\n"
    "From the detail screen you can also add notes (choose a category and type your text).\n"
    "Commands:\n"
    "  /tag    — list projects by tag\n"
    "  /reset-password — reset your dashboard password\n"
    "  /help   — this message\n"
    "  /cancel — abort the current action\n"
    "  /start  — restart the bot"
)
HELP_TEXT_UNLINKED = (
    "To use this bot you need an invite code from Sietch CRM.\n"
    "Send the code here to link your account, then you can search projects."
)


def format_search_result(deals: list[dict], search: str, is_employee: bool = False) -> str:
    if not deals:
        return f"No projects found matching '{_esc(search)}'."
    if len(deals) == 1:
        return format_deal_detail(deals, 0, is_employee=is_employee)
    lines = [f"Projects matching '{_esc(search)}':", ""]
    for i, d in enumerate(deals, 1):
        parts = [f"{i}. {_esc(d['title'])}"]
        stage = d.get("stage", "")
        if stage:
            parts.append(f"   Status: {_esc(stage)}")
        lines.append("\n".join(parts))
    lines.append("")
    lines.append("───────────────")
    lines.append("<i>Reply with a number (1, 2, ...) to see full details.</i>")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = _truncate_html(msg, 4000)
    return msg


def format_deal_detail(deals: list[dict], index: int, is_employee: bool = False) -> str:
    if index < 0 or index >= len(deals):
        return "Invalid selection."
    d = deals[index]
    amt = _fmt_money(d.get("amount", 0), d.get("currency", ""))
    lines = [
        _esc(d["title"]),
        "─" * 30,
    ]
    # Customer view: show key user fields below title
    if not is_employee:
        uf = d.get("userFields") or {}
        for field_name in ("Address", "Customer Phone", "Insurance Carrier", "Claim #"):
            val = uf.get(field_name, "")
            if val:
                lines.append(f"{field_name}: {_esc(val)}")
        stage = d.get("stage", "")
        if stage:
            lines.append(f"Stage: {_esc(stage)}")
        update = d.get("latestUpdate")
        if update:
            content = update.get("content", "")
            created = _fmt_date(update.get("created", ""))
            if content:
                lines.append("")
                header = "Latest customer update"
                if created:
                    header += f" — <i>{_esc(created)}</i>"
                lines.append(header + ":")
                lines.append(_sanitize_html(content))
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("───────────────")
        lines.append("<i>Send another project name to search again.</i>")
        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = _truncate_html(msg, 4000)
        return msg
    # Employee view below
    stage = d.get("stage", "")
    if stage:
        lines.append(f"Status: {_esc(stage)}")
    if amt:
        lines.append(f"Amount: {_esc(amt)}")
    events = d.get("events", [])
    if events:
        lines.append("")
        for idx, ev in enumerate(events):
            if idx > 0:
                lines.append("───────────────")
            cat = ev.get("categoryName") or ""
            content = ev.get("content", "")
            created = _fmt_date(ev.get("created", ""))
            author = str(ev.get("author") or "").strip()
            cat_lower = cat.lower()
            is_mail = "mail" in cat_lower or "email" in cat_lower
            is_customer_update = "customer update" in cat_lower
            header_parts: list[str] = []
            if cat:
                header_parts.append(f"<b>[{_esc(cat)}]</b>")
            if created:
                header_parts.append(f"<i>{_esc(created)}</i>")
            header = " — ".join(header_parts) if header_parts else ""
            # Show author for event types except:
            # - mail messages (already show From/To lines)
            # - customer updates (customers should not see employee names)
            if author and not is_mail and not is_customer_update:
                header = f"{header} ({_esc(author)})" if header else f"({_esc(author)})"
            if header:
                lines.append(header)
            if content:
                lines.append(_format_event_body(cat, content, max_len=1200))
            lines.append("")
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("───────────────")
    lines.append("<i>Send another project name to search again.</i>")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = _truncate_html(msg, 4000)
    return msg


def format_project_info(deal: dict) -> str:
    """Format all deal fields and user fields for employee Project Info view."""
    lines = [
        f"<b>{_esc(deal.get('title', 'Deal'))}</b> — Project Info",
        "─" * 30,
    ]
    # Standard CRM fields
    stage = deal.get("stage", "")
    if stage:
        lines.append(f"<b>Stage:</b> {_esc(stage)}")
    amt = _fmt_money(deal.get("amount", 0), deal.get("currency", ""))
    if amt:
        lines.append(f"<b>Amount:</b> {_esc(amt)}")
    tags = deal.get("tags") or []
    if tags:
        lines.append(f"<b>Tags:</b> {_esc(', '.join(tags))}")
    desc = deal.get("description", "")
    if desc:
        lines.append(f"<b>Description:</b> {_esc(desc)}")
    contact = deal.get("contact", "")
    if contact:
        lines.append(f"<b>Contact:</b> {_esc(contact)}")
    responsible = deal.get("responsible", "")
    if responsible:
        lines.append(f"<b>Responsible:</b> {_esc(responsible)}")
    created = deal.get("created", "")
    if created:
        lines.append(f"<b>Created:</b> {_esc(_fmt_date(created))}")
    due = deal.get("dueDate", "")
    if due:
        lines.append(f"<b>Due Date:</b> {_esc(_fmt_date(due))}")
    bid = deal.get("bidType", "")
    if bid:
        lines.append(f"<b>Bid Type:</b> {_esc(bid)}")
    if deal.get("isPrivate"):
        lines.append(f"<b>Private:</b> Yes")
    # User fields (all non-empty, no header)
    uf = deal.get("userFields") or {}
    for key, val in uf.items():
        if val:
            lines.append(f"<b>{_esc(key)}:</b> {_esc(val)}")
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("───────────────")
    lines.append("<i>Tap a button below to continue.</i>")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = _truncate_html(msg, 4000)
    return msg


# ── Main Setup ──

def main() -> None:
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not PORTAL:
        logger.error("ONLYOFFICE_PORTAL_URL not set")
        sys.exit(1)

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    except ImportError:
        logger.error("python-telegram-bot not installed: pip install python-telegram-bot")
        sys.exit(1)

    app = Application.builder().token(TOKEN).build()

    # ── Inline Keyboard Builders ──

    def _build_search_results_keyboard(deals: list[dict]) -> InlineKeyboardMarkup | None:
        keyboard: list[list[InlineKeyboardButton]] = []
        for d in deals:
            deal_id = d.get("id") or d.get("ID")
            if not deal_id:
                continue
            title = str(d.get("title") or d.get("Title") or f"Deal #{deal_id}")
            if len(title) > 40:
                title = title[:37] + "..."
            keyboard.append([InlineKeyboardButton(title, callback_data=f"sel:{deal_id}")])
        return InlineKeyboardMarkup(keyboard) if keyboard else None

    def _build_deal_detail_keyboard(deal_id: int, is_employee: bool) -> InlineKeyboardMarkup:
        row: list[InlineKeyboardButton] = []
        if is_employee:
            row.append(InlineKeyboardButton("📋 Project Info", callback_data=f"act:info:{deal_id}"))
            row.append(InlineKeyboardButton("📝 Add Note", callback_data=f"act:note:{deal_id}"))
        row.append(InlineKeyboardButton("🔍 New Search", callback_data="act:home"))
        return InlineKeyboardMarkup([row])

    _PREFERRED_CATEGORY_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r"quick.?context", re.I), "Quick Context"),
        (re.compile(r"note|event|comment", re.I), "Note"),
        (re.compile(r"text", re.I), "Text"),
        (re.compile(r"phone|call|voicemail", re.I), "Call"),
        (re.compile(r"mail|email", re.I), "Email"),
        (re.compile(r"customer.?update", re.I), "Customer Update"),
    ]

    def _build_categories_keyboard(categories: list[dict]) -> InlineKeyboardMarkup:
        placed: set[int] = set()
        ordered: list[tuple[str, int]] = []
        for pattern, label in _PREFERRED_CATEGORY_PATTERNS:
            for cat in categories:
                cid = cat["id"]
                if cid in placed:
                    continue
                if pattern.search(cat["title"]):
                    ordered.append((label, cid))
                    placed.add(cid)
                    break
        # Fill remaining slots with unmatched categories
        for cat in categories:
            cid = cat["id"]
            if cid in placed:
                continue
            ordered.append((cat["title"], cid))
            placed.add(cid)
            if len(ordered) >= 6:
                break
        # Build 2-column keyboard
        keyboard: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for label, cid in ordered[:6]:
            row.append(InlineKeyboardButton(label, callback_data=f"cat:{cid}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        return InlineKeyboardMarkup(keyboard)

    async def _reply_html(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Send a message in HTML mode, falling back to plain text if Telegram rejects it."""
        try:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as exc:
            logger.warning("HTML reply failed for chat %s: %s", update.effective_chat.id, exc)
            sample = text[:500].replace("\n", "\\n")
            logger.warning("Offending message sample: %s", sample)
            try:
                await update.message.reply_text(_strip_html_tags(text), parse_mode=None, reply_markup=reply_markup)
            except Exception as exc2:
                logger.exception("Plain-text fallback also failed for chat %s", update.effective_chat.id)

    async def start(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if mapping:
            is_employee = mapping.get("employee", False)
            name = _esc(mapping.get("contactName") or mapping.get("nickname") or "")
            if is_employee:
                greeting = f"Welcome back, <b>{name}</b>!" if name else "Welcome back!"
                await _reply_html(
                    update,
                    f"{greeting} Send a project name to search across all projects.\n\n"
                    "Commands:\n"
                    "  /tag    — list projects by tag\n"
                    "  /help   — show available commands\n"
                    "  /cancel  — abort the current action",
                    reply_markup=_build_deal_detail_keyboard(0, is_employee=True),
                )
            else:
                greeting = f"Welcome back, <b>{name}</b>!" if name else "Welcome back!"
                await _reply_html(
                    update,
                    f"{greeting} Send a project name to look up your project.\n\n"
                    "Need a new invite code? Contact Sietch CRM.",
                )
        else:
            await _reply_html(
                update,
                "Welcome! To get started, you need an invite code from Sietch CRM.\n\n"
                "Send the invite code here to link your account, then you can search projects.",
            )

    async def help_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if mapping:
            is_employee = mapping.get("employee", False)
            await _reply_html(update, HELP_TEXT_EMPLOYEE if is_employee else HELP_TEXT_CUSTOMER)
        else:
            await _reply_html(update, HELP_TEXT_UNLINKED)

    async def cancel(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        if chat_id in _pending_note:
            del _pending_note[chat_id]
            await _reply_html(update, "Cancelled.")
        else:
            await _reply_html(update, "Nothing to cancel.")

    async def tag_command(update: Update, _ctx) -> None:
        """List all tags (employee only)."""
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "This command is only available to employees.")
            return
        tags = await get_tags()
        if tags is None:
            await _reply_html(update, "I'm having trouble reaching the server. Please try again in a couple of minutes.")
            return
        if not tags:
            await _reply_html(update, "No tags found.")
            return
        keyboard: list[list[InlineKeyboardButton]] = []
        for tag in tags:
            name = tag.get("name") or tag.get("title") or ""
            if not name:
                continue
            if len(name) > 40:
                name = name[:37] + "..."
            keyboard.append([InlineKeyboardButton(name, callback_data=f"tag:{name}")])
        await _reply_html(
            update,
            "Select a tag to see matching projects:",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        )

    async def handle_callback(update: Update, _ctx) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        data = query.data

        if data == "act:home":
            await query.edit_message_text("Send a project name to search.")
            return

        if data.startswith("sel:"):
            try:
                deal_id = int(data[4:])
            except (ValueError, IndexError):
                await query.edit_message_text("Invalid selection.")
                return
            deals = _last_deals.get(chat_id, [])
            deal = None
            idx = -1
            for i, d in enumerate(deals):
                if (d.get("id") or d.get("ID")) == deal_id:
                    deal = d
                    idx = i
                    break
            if not deals or deal is None:
                await query.edit_message_text("This search has expired. Send a new project name.")
                return
            emp = _last_employee.get(chat_id, False)
            msg = format_deal_detail(deals, idx, is_employee=emp)
            keyboard = _build_deal_detail_keyboard(deal_id, emp)
            await query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
            return

        if data.startswith("tag:"):
            tag_name = data[4:]
            if not tag_name:
                await query.edit_message_text("Invalid tag.")
                return
            deals = await get_deals(None, chat_id, tag=tag_name, employee=True)
            if deals is None:
                await query.edit_message_text("I'm having trouble reaching the server. Please try again.")
                return
            _last_deals[chat_id] = deals
            _last_search[chat_id] = f"tag:{tag_name}"
            _last_employee[chat_id] = True
            msg = format_search_result(deals, f"tag: {tag_name}", is_employee=True)
            kb = _build_search_results_keyboard(deals)
            await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)
            return

        if data.startswith("act:note:"):
            try:
                deal_id = int(data[9:])
            except (ValueError, IndexError):
                await query.edit_message_text("Invalid selection.")
                return
            _pending_note[chat_id] = {"dealId": deal_id, "step": "awaiting_text"}
            await query.message.reply_text(
                "Send the note text you want to add for this project.\n"
                "You can cancel by sending /cancel."
            )
            return

        if data.startswith("act:info:"):
            try:
                deal_id = int(data[9:])
            except (ValueError, IndexError):
                await query.edit_message_text("Invalid selection.")
                return
            deals = _last_deals.get(chat_id, [])
            deal = None
            for d in deals:
                if (d.get("id") or d.get("ID")) == deal_id:
                    deal = d
                    break
            if not deal:
                await query.edit_message_text("This search has expired. Send a new project name.")
                return
            msg = format_project_info(deal)
            await query.message.reply_text(msg, parse_mode="HTML")
            return

        if data.startswith("cat:"):
            pending = _pending_note.get(chat_id)
            if not pending or pending.get("step") != "awaiting_category":
                await query.edit_message_text("Nothing pending. Send a project name to search.")
                return
            category_id = int(data[4:])
            deal_id = pending["dealId"]
            text = pending["text"]
            result = await _post("/api/bot/note", {
                "chatId": chat_id,
                "dealId": deal_id,
                "content": text,
                "categoryId": category_id,
            })
            del _pending_note[chat_id]
            if result and result.get("ok"):
                await query.edit_message_text("✅ Note added!")
            else:
                await query.edit_message_text("❌ Failed to add note. Please try again.")
            return

    async def handle_message(update: Update, _ctx) -> None:
        try:
            if not update.message or not update.message.text:
                return
            chat_id = update.effective_chat.id
            text = update.message.text.strip()
            lower = text.lower()

            logger.info("Message from chat %d: %s", chat_id, text[:50])

            # ── Pending note flow ──
            pending = _pending_note.get(chat_id)
            if pending:
                if pending.get("step") == "awaiting_text":
                    escaped = _esc(text)
                    pending["text"] = f"<p>{escaped}</p>"
                    pending["step"] = "awaiting_category"
                    categories = await get_categories()
                    if categories:
                        keyboard = _build_categories_keyboard(categories)
                        await _reply_html(update, "What type of note is this?", reply_markup=keyboard)
                    else:
                        del _pending_note[chat_id]
                        result = await _post("/api/bot/note", {
                            "chatId": chat_id,
                            "dealId": pending["dealId"],
                            "content": f"<p>{escaped}</p>",
                        })
                        if result and result.get("ok"):
                            await _reply_html(update, "✅ Note added!")
                        else:
                            await _reply_html(update, "❌ Failed to add note. Please try again.")
                    return
                # Unknown step — clear and continue normally
                del _pending_note[chat_id]

            # Check if already mapped
            mapping = await get_mapping(chat_id)

            if mapping:
                contact_id = mapping.get("contactId")
                is_employee = mapping.get("employee", False)
                if not contact_id and not is_employee:
                    await _reply_html(update, "Your account is not fully set up. Please contact support.")
                    return

                _last_employee[chat_id] = is_employee

                # Help request
                if lower in ("/help", "help", "?"):
                    await _reply_html(update, HELP_TEXT_EMPLOYEE if is_employee else HELP_TEXT_CUSTOMER)
                    return

                # Number reply: read from cached results (no re-fetch)
                if text.isdigit():
                    deals = _last_deals.get(chat_id, [])
                    emp = _last_employee.get(chat_id, False)
                    if not deals:
                        await _reply_html(update, "No projects to select from. Send a project name to search.")
                        return
                    idx = int(text) - 1
                    if 0 <= idx < len(deals):
                        msg = format_deal_detail(deals, idx, is_employee=emp)
                        deal_id = deals[idx].get("id") or deals[idx].get("ID")
                        kb = _build_deal_detail_keyboard(deal_id, emp) if deal_id else None
                        await _reply_html(update, msg, reply_markup=kb)
                    else:
                        search = _last_search.get(chat_id, "")
                        msg = format_search_result(deals, search, is_employee=emp)
                        await _reply_html(update, msg)
                    return

                # Treat any other text as a title search
                search = text
                deals = await get_deals(contact_id, chat_id, search=search, employee=is_employee)
                if deals is None:
                    await _reply_html(update, "I'm having trouble reaching the server. Please try again in a couple of minutes.")
                    return
                _last_deals[chat_id] = deals
                _last_search[chat_id] = search
                msg = format_search_result(deals, search, is_employee=is_employee)
                if len(deals) == 1 and deals[0]:
                    deal_id = deals[0].get("id") or deals[0].get("ID")
                    kb = _build_deal_detail_keyboard(deal_id, is_employee) if deal_id else None
                else:
                    kb = _build_search_results_keyboard(deals)
                await _reply_html(update, msg, reply_markup=kb)
                return
            else:
                # Not linked yet — treat message as invite code
                result = await verify_code(text, chat_id)
                if result:
                    contact_id = result.get("contactId")
                    is_employee = result.get("employee", False)
                    if contact_id:
                        msg = (
                            "✅ You've been linked! Send a project name to find it.\n\n"
                            "Example: <b>roof</b> or <b>Smith</b>"
                        )
                    elif is_employee:
                        msg = (
                            "✅ You've been linked as an employee! Send a project name to search across all projects.\n\n"
                            "Example: <b>roof</b> or <b>Smith</b>"
                        )
                    else:
                        msg = "✅ You've been linked! But we couldn't find any projects yet."
                    await _reply_html(update, msg)
                    logger.info("Chat %d linked to contact %s (employee=%s)", chat_id, contact_id, is_employee)
                else:
                    await _reply_html(
                        update,
                        "That code wasn't recognized or has expired. "
                        "Please check with your administrator for a new invite code.",
                    )
        except Exception:
            logger.exception("Unhandled error in handle_message for chat %d", chat_id)
            try:
                await _reply_html(update, "Something went wrong. Please try again in a couple of minutes.")
            except Exception:
                pass

    async def handle_reply(update: Update, _ctx) -> None:
        """Handle replies to notification messages — post note on project."""
        if not update.message or not update.message.reply_to_message:
            return
        if not update.message.text:
            return

        chat_id = update.effective_chat.id
        reply_to = update.message.reply_to_message
        parent_message_id = reply_to.message_id

        # Look up notification from telegram_notification_log via dashboard
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{DASHBOARD_URL}/api/bot/notification-by-message",
                    headers=_auth_headers(),
                    params={"chatId": chat_id, "messageId": parent_message_id},
                )
                if resp.status_code != 200:
                    return  # Not a notification reply
                data = resp.json()
        except Exception:
            logger.exception("Failed to look up notification for message %s", parent_message_id)
            return

        notification_id = data.get("notificationId")
        opp_id = data.get("opportunityId")
        project_title = data.get("projectTitle", "Unknown Project")
        if not notification_id or not opp_id:
            return

        # Post note to project
        content = update.message.text
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{DASHBOARD_URL}/api/bot/note",
                    headers=_auth_headers(),
                    json={
                        "opportunityId": opp_id,
                        "content": content,
                        "categoryId": 1,  # Note category
                    },
                )
                if resp.status_code == 200:
                    await _reply_html(
                        update,
                        f"\u2705 <b>Note added</b>\nProject: {_escape_html(project_title)}",
                    )
                else:
                    await _reply_html(update, "Failed to add note. Please try again.")
        except Exception:
            logger.exception("Failed to post note for reply to notification %s", notification_id)
            await _reply_html(update, "Something went wrong. Please try again.")

    async def reset_password_command(update: Update, _ctx) -> None:
        """Generate a temporary password for the linked employee's dashboard account."""
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "This command is only available to linked employees.")
            return
        resp = await _post("/api/bot/reset-password", {"chatId": chat_id})
        if resp and resp.get("tempPassword"):
            await _reply_html(update, f"Your temporary password is:\n<code>{html.escape(resp['tempPassword'])}</code>\nUse it to sign in, then change your password.")
        else:
            await _reply_html(update, "Failed to generate a temporary password. Please try again later.")

    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("tag", tag_command))
    app.add_handler(CommandHandler("reset-password", reset_password_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot polling...")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
