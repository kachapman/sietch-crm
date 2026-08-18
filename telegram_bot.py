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

_DASHBOARD_URL_DEFAULT = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORTAL = os.environ.get("ONLYOFFICE_PORTAL_URL", "")

def _get_dashboard_url() -> str:
    """Read DASHBOARD_URL lazily to pick up .env changes."""
    return os.getenv("DASHBOARD_URL", _DASHBOARD_URL_DEFAULT)

# Last search results per chat, used to interpret "1", "2", ... replies.
_last_search: dict[int, str] = {}
_last_deals: dict[int, list[dict]] = {}
_last_employee: dict[int, bool] = {}
_pending_note: dict[int, dict] = {}
_last_viewed_project: dict[int, int] = {}  # chat_id → project_id for file uploads
_pending_upload: dict[int, dict] = {}  # chat_id → {projectId, step}
_pending_media: dict[str, dict] = {}  # media_group_id → {chat_id, project_id, files: []}

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
                f"{_get_dashboard_url()}{path}",
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
                f"{_get_dashboard_url()}{path}",
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


# ── Background Tasks (heartbeat + config polling) ──

_bg_config_version: int = 0
_bg_bot_name: str = ""


async def log_activity(
    chat_id: int | None,
    user_name: str,
    action: str,
    detail: str = "",
    project_id: int | None = None,
    is_error: bool = False,
) -> None:
    """Fire-and-forget activity log to server."""
    try:
        await _post("/api/bot/activity", {
            "chatId": chat_id,
            "userName": user_name,
            "action": action,
            "detail": detail,
            "projectId": project_id,
            "isError": is_error,
        })
    except Exception:
        pass


async def _heartbeat_loop() -> None:
    """POST /api/bot/heartbeat every 60s."""
    while True:
        try:
            await _post("/api/bot/heartbeat", {})
        except Exception:
            pass
        await asyncio.sleep(60)


async def _config_poll_loop() -> None:
    """Poll /api/bot/config-check every 60s; reload settings on change."""
    global _bg_config_version, _bg_bot_name, TOKEN
    while True:
        await asyncio.sleep(60)
        try:
            resp = await _post("/api/bot/config-check", {"configVersion": _bg_config_version})
            if resp and resp.get("changed"):
                new_ver = resp.get("configVersion", 0)
                logger.info("Config changed (v%d → v%d), reloading…", _bg_config_version, new_ver)
                _bg_config_version = new_ver
                cfg = await _get("/api/v2/admin/bot-settings")
                if cfg:
                    new_token = cfg.get("botToken", "")
                    if new_token and new_token != TOKEN:
                        logger.info("Bot token changed — restarting process to apply")
                        import sys, os
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    _bg_bot_name = cfg.get("botName", _bg_bot_name)
                    logger.info("Config reloaded: botName=%s", _bg_bot_name)
        except Exception as e:
            logger.debug("Config poll failed: %s", e)


async def _startup_init(application) -> None:
    """Fetch initial config version and start background tasks."""
    global _bg_config_version, _bg_bot_name
    try:
        cfg = await _get("/api/v2/admin/bot-settings")
        if cfg:
            _bg_config_version = cfg.get("configVersion", 0)
            _bg_bot_name = cfg.get("botName", "")
            logger.info("Initial config: version=%d botName=%s", _bg_config_version, _bg_bot_name)
            # Auto-populate bot username and link if empty
            if not cfg.get("botUsername") or not cfg.get("botLink"):
                try:
                    me = application.bot_data.get("me")
                    if not me:
                        import httpx as _httpx
                        resp = await _httpx.AsyncClient(timeout=10).get(
                            f"https://api.telegram.org/bot{TOKEN}/getMe"
                        )
                        me = resp.json().get("result", {})
                    username = me.get("username", "")
                    if username:
                        await _post("/api/v2/admin/bot-settings", {
                            "botUsername": f"@{username}",
                            "botLink": f"https://t.me/{username}",
                        })
                        logger.info("Auto-populated bot username: @%s", username)
                except Exception as e:
                    logger.debug("Could not auto-populate bot username: %s", e)
    except Exception:
        pass
    asyncio.create_task(_heartbeat_loop(), name="bot-heartbeat")
    asyncio.create_task(_config_poll_loop(), name="bot-config-poll")
    logger.info("Background tasks started (heartbeat 60s, config poll 5min)")


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
    if isinstance(result, list):
        return [{"tag": t} for t in result]
    if isinstance(result, dict) and "tags" in result:
        return result["tags"]
    return None


async def verify_code(code: str, chat_id: int) -> dict | None:
    return await _post("/api/bot-customers/verify-code", {
        "code": code,
        "chatId": chat_id,
        "portal": "sietch",
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
    "Send a project name to search across all projects.\n"
    "Commands:\n"
    "  /today     — today's tasks & events\n"
    "  /tasks     — open tasks assigned to you\n"
    "  /task_done — mark a task complete\n"
    "  /new_task  — create a new task\n"
    "  /my_projects — your assigned projects\n"
    "  /overdue   — overdue projects\n"
    "  /due_this_week — projects due this week\n"
    "  /calendar  — upcoming calendar events\n"
    "  /pipeline  — project pipeline overview\n"
    "  /stats     — project statistics\n"
    "  /recent    — recent activity (7 days)\n"
    "  /team      — team members\n"
    "  /tag       — list projects by tag\n"
    "  /search    — search projects\n"
    "  /reset_password — reset dashboard password\n"
    "  /help      — this message\n"
    "  /cancel    — abort current action\n"
    "  /start     — restart the bot"
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
    deal_id = d.get("id", "")
    project_url = f"{_get_dashboard_url()}/project/{deal_id}" if deal_id else ""
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
        if project_url:
            lines.append("")
            lines.append(f'<a href="{project_url}">View project in CRM</a>')
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
    tags = d.get("tags") or []
    if tags:
        lines.append(f"Tags: {_esc(', '.join(tags))}")
    desc = d.get("description", "")
    if desc:
        lines.append(f"Description: {_esc(desc[:200])}")
    contact = d.get("contact", "")
    if contact:
        lines.append(f"Contact: {_esc(contact)}")
    responsible = d.get("responsible", "")
    if responsible:
        lines.append(f"Responsible: {_esc(responsible)}")
    created = d.get("created", "")
    if created:
        lines.append(f"Created: {_esc(_fmt_date(created))}")
    due = d.get("dueDate", "")
    if due:
        lines.append(f"Due Date: {_esc(_fmt_date(due))}")
    if project_url:
        lines.append("")
        lines.append(f'<a href="{project_url}">View project in CRM</a>')
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
    deal_id = deal.get("id", "")
    project_url = f"{_get_dashboard_url()}/project/{deal_id}" if deal_id else ""
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
    if deal.get("isPrivate"):
        lines.append(f"<b>Private:</b> Yes")
    # User fields (all non-empty, no header)
    uf = deal.get("userFields") or {}
    for key, val in uf.items():
        if val:
            lines.append(f"<b>{_esc(key)}:</b> {_esc(val)}")
    if project_url:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f'<a href="{project_url}">View project in CRM</a>')
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
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    except ImportError:
        logger.error("python-telegram-bot not installed: pip install python-telegram-bot")
        sys.exit(1)

    app = Application.builder().token(TOKEN).post_init(_startup_init).build()

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

    def _build_deal_detail_keyboard(deal_id: int, is_employee: bool, phone: str = "", address: str = "") -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if is_employee:
            rows.append([
                InlineKeyboardButton("📋 Info", callback_data=f"act:info:{deal_id}"),
                InlineKeyboardButton("📊 Stage", callback_data=f"act:stage:{deal_id}"),
                InlineKeyboardButton("📝 Note", callback_data=f"act:note:{deal_id}"),
            ])
            action_row: list[InlineKeyboardButton] = []
            if phone:
                action_row.append(InlineKeyboardButton("📞 Call", url=f"tel:{phone}"))
            if address:
                import urllib.parse
                maps_url = f"https://maps.google.com/?q={urllib.parse.quote(address)}"
                action_row.append(InlineKeyboardButton("📍 Address", url=maps_url))
            if action_row:
                rows.append(action_row)
        if deal_id:
            project_url = f"{_get_dashboard_url()}/project/{deal_id}"
            rows.append([
                InlineKeyboardButton("🔗", url=project_url),
                InlineKeyboardButton("🔍 New Search", callback_data="act:home"),
            ])
        else:
            rows.append([InlineKeyboardButton("🔍 New Search", callback_data="act:home")])
        return InlineKeyboardMarkup(rows)

    def _build_reply_keyboard(is_employee: bool) -> ReplyKeyboardMarkup:
        """Persistent keyboard buttons below the chat input."""
        if is_employee:
            keyboard = [
                [KeyboardButton("Tasks"), KeyboardButton("Tag")],
                [KeyboardButton("Search"), KeyboardButton("Help")],
            ]
        else:
            keyboard = [
                [KeyboardButton("Search"), KeyboardButton("Help")],
            ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

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

    def _build_user_picker_keyboard(selected_users: list[int], all_users: list[dict]) -> InlineKeyboardMarkup:
        """Build multi-select keyboard for notification user picker."""
        keyboard: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for u in all_users:
            uid = u.get("id", 0)
            name = u.get("displayName") or u.get("email") or f"User {uid}"
            if len(name) > 18:
                name = name[:15] + "..."
            prefix = "✅ " if uid in selected_users else ""
            row.append(InlineKeyboardButton(f"{prefix}{name}", callback_data=f"notify_toggle:{uid}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        # Done button
        count = len(selected_users)
        keyboard.append([InlineKeyboardButton(f"✅ Done ({count} selected)", callback_data="notify_done")])
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
        name = ""
        if mapping:
            is_employee = mapping.get("employee", False)
            name = _esc(mapping.get("contactName") or mapping.get("nickname") or "")
            asyncio.create_task(log_activity(chat_id, name, "start", f"employee={is_employee}"))
            if is_employee:
                greeting = f"Welcome back, <b>{name}</b>!" if name else "Welcome back!"
                await _reply_html(
                    update,
                    f"{greeting} Send a project name to search across all projects.\n\n"
                    "Quick commands:\n"
                    "  /today  — today's tasks &amp; events\n"
                    "  /tasks  — open tasks\n"
                    "  /my_projects — your projects\n"
                    "  /help   — all commands",
                )
                await update.message.reply_text(
                    "Use the buttons below for quick access:",
                    reply_markup=_build_reply_keyboard(is_employee=True),
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
            name = tag.get("tag") or tag.get("name") or tag.get("title") or ""
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

        if data.startswith("tdone:"):
            try:
                task_id = int(data[6:])
            except (ValueError, IndexError):
                await query.edit_message_text("Invalid task.")
                return
            resp = await _post("/api/bot/task-done", {"chatId": chat_id, "taskId": task_id})
            if resp and resp.get("ok"):
                await query.edit_message_text(f"✅ Task completed: {_esc(resp.get('title', ''))}")
            else:
                await query.edit_message_text("Failed to complete task.")
            return

        if data == "newtask":
            await _reply_html(update, "Send the task title. It will be assigned to you.")
            _pending_note[chat_id] = {"step": "awaiting_new_task"}
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
            phone = deal.get("contactPhone") or deal.get("contactMobile") or ""
            address = deal.get("address") or ""
            _last_viewed_project[chat_id] = deal_id
            keyboard = _build_deal_detail_keyboard(deal_id, emp, phone=phone, address=address)
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
            _last_viewed_project[chat_id] = deal_id  # Set for file upload context
            await query.message.reply_text(
                "Send text or photos (one or multiple). Type /cancel to abort."
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
            _last_viewed_project[chat_id] = deal_id
            await query.message.reply_text(msg, parse_mode="HTML")
            return

        if data.startswith("act:stage:"):
            try:
                deal_id = int(data[10:])
            except (ValueError, IndexError):
                await query.edit_message_text("Invalid selection.")
                return
            resp = await _get("/api/v2/stages")
            if not resp:
                await query.edit_message_text("Could not load stages.")
                return
            stages = resp if isinstance(resp, list) else resp.get("stages", [])
            if not stages:
                await query.edit_message_text("No stages found.")
                return
            _last_viewed_project[chat_id] = deal_id
            buttons = [[InlineKeyboardButton(s.get("title", ""), callback_data=f"setstage:{deal_id}:{s.get('id', '')}")] for s in stages]
            await query.edit_message_text(
                f"Select a new stage for this project:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if data.startswith("setstage:"):
            parts = data.split(":")
            if len(parts) != 3:
                return
            deal_id = int(parts[1])
            stage_id = int(parts[2])
            resp = await _post("/api/bot/stage", {"chatId": chat_id, "opportunityId": deal_id, "stageId": stage_id})
            if resp and resp.get("ok"):
                await query.edit_message_text(f"✅ Stage updated to: {resp.get('stage', 'Unknown')}")
            else:
                await query.edit_message_text("Failed to update stage.")
            return

        if data.startswith("cat:"):
            pending = _pending_note.get(chat_id)
            if not pending or pending.get("step") != "awaiting_category":
                await query.edit_message_text("Nothing pending. Send a project name to search.")
                return
            category_id = int(data[4:])
            deal_id = pending["dealId"]
            text = pending["text"]
            # Store category and move to user selection step
            pending["categoryId"] = category_id
            pending["step"] = "awaiting_notify"
            pending["notifyUsers"] = []
            # Fetch all CRM users for the picker
            all_users = await _get("/api/v2/users") or []
            # Filter out the current user and customers
            current_user_id = None
            mapping = await get_mapping(chat_id)
            if mapping and mapping.get("userId"):
                current_user_id = int(mapping["userId"])
            picker_users = [u for u in all_users if u.get("isActive") and u.get("id") != current_user_id]
            if not picker_users:
                # No users to notify — submit immediately
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
                    await query.edit_message_text("❌ Failed to add note.")
                return
            # Store users for the picker
            pending["pickerUsers"] = picker_users
            keyboard = _build_user_picker_keyboard([], picker_users)
            await query.edit_message_text(
                "Who should be notified?",
                reply_markup=keyboard,
            )
            return

        if data.startswith("notify_toggle:"):
            pending = _pending_note.get(chat_id)
            if not pending or pending.get("step") != "awaiting_notify":
                return
            try:
                uid = int(data[13:])
            except (ValueError, IndexError):
                return
            selected = pending.get("notifyUsers", [])
            if uid in selected:
                selected.remove(uid)
            else:
                selected.append(uid)
            pending["notifyUsers"] = selected
            picker_users = pending.get("pickerUsers", [])
            keyboard = _build_user_picker_keyboard(selected, picker_users)
            await query.edit_message_reply_markup(keyboard)
            return

        if data == "notify_done":
            pending = _pending_note.get(chat_id)
            if not pending or pending.get("step") != "awaiting_notify":
                return
            deal_id = pending["dealId"]
            text = pending["text"]
            category_id = pending["categoryId"]
            notify_users = pending.get("notifyUsers", [])
            result = await _post("/api/bot/note", {
                "chatId": chat_id,
                "dealId": deal_id,
                "content": text,
                "categoryId": category_id,
                "notifyUserList": notify_users,
            })
            del _pending_note[chat_id]
            if result and result.get("ok"):
                count = len(notify_users)
                suffix = f" — {count} user(s) notified" if count else ""
                await query.edit_message_text(f"✅ Note added!{suffix}")
                mapping = await get_mapping(chat_id)
                uname = (mapping.get("contactName") or mapping.get("nickname") or "User") if mapping else "User"
                asyncio.create_task(log_activity(chat_id, uname, "note_added", f"deal={deal_id}", project_id=deal_id))
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

            # ── Reply keyboard button presses ──
            mapping = await get_mapping(chat_id)
            is_emp = mapping.get("employee", False) if mapping else False
            if text == "Tasks" and is_emp:
                await tasks_command(update, _ctx)
                return
            elif text == "Tag" and is_emp:
                await tag_command(update, _ctx)
                return
            elif text == "Search":
                await _reply_html(update, "Send a project name to search.")
                return
            elif text == "Help":
                await help_command(update, _ctx)
                return

            # ── Pending new task flow ──
            pending_task = _pending_note.get(chat_id)
            if pending_task and pending_task.get("step") == "awaiting_new_task":
                title = text.strip()
                if title:
                    resp = await _post("/api/bot/task-create", {"chatId": chat_id, "title": title})
                    if resp and resp.get("ok"):
                        await _reply_html(update, f"✅ Task created: <b>{_esc(title)}</b>")
                    else:
                        await _reply_html(update, "Failed to create task.")
                else:
                    await _reply_html(update, "Please provide a task title.")
                del _pending_note[chat_id]
                return

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
                        phone = deals[idx].get("contactPhone") or deals[idx].get("contactMobile") or ""
                        address = deals[idx].get("address") or ""
                        kb = _build_deal_detail_keyboard(deal_id, emp, phone=phone, address=address) if deal_id else None
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
                    phone = deals[0].get("contactPhone") or deals[0].get("contactMobile") or ""
                    address = deals[0].get("address") or ""
                    kb = _build_deal_detail_keyboard(deal_id, is_employee, phone=phone, address=address) if deal_id else None
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

    async def _process_media_group(media_group_id: str) -> None:
        """Process collected media group files after a short delay."""
        await asyncio.sleep(2)  # Wait for all photos in the group
        group = _pending_media.pop(media_group_id, None)
        if not group:
            return
        chat_id = group["chat_id"]
        project_id = group["project_id"]
        files = group["files"]
        mapping = group["mapping"]
        if not project_id:
            return
        uname = (mapping.get("contactName") or "Employee")
        uploaded = 0
        for f in files:
            try:
                tg_token = TOKEN
                url = f"https://api.telegram.org/bot{tg_token}/getFile?file_id={f['file_id']}"
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200 or not resp.json().get("ok"):
                        continue
                    file_path = resp.json()["result"]["file_path"]
                    dl_url = f"https://api.telegram.org/file/bot{tg_token}/{file_path}"
                    file_resp = await client.get(dl_url, timeout=60)
                    if file_resp.status_code != 200:
                        continue
                    file_data = file_resp.content
                import io
                files_payload = {"file": (f["file_name"], io.BytesIO(file_data), "application/octet-stream")}
                form_data = {"oppId": str(project_id), "chatId": str(chat_id), "fileType": f["file_type"]}
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{_get_dashboard_url()}/api/bot/upload",
                        headers=_auth_headers(),
                        files=files_payload,
                        data=form_data,
                    )
                    if resp.status_code == 200 and resp.json().get("ok"):
                        uploaded += 1
            except Exception:
                logger.exception("Failed to upload file in media group")
        if uploaded:
            # Create a lightweight history event
            in_note_flow = _pending_note.get(chat_id, {}).get("step") == "awaiting_text"
            if in_note_flow:
                try:
                    await _post("/api/bot/note", {
                        "chatId": chat_id,
                        "dealId": project_id,
                        "content": f"<p>{_esc(uname)} added {uploaded} file(s) via Telegram</p>",
                        "categoryId": 12,  # "Files are uploaded"
                    })
                except Exception:
                    pass
            proj_name = ""
            for d in _last_deals.get(chat_id, []):
                if (d.get("id") or d.get("ID")) == project_id:
                    proj_name = d.get("title", "")
                    break
            proj_label = f"<b>{_esc(proj_name)}</b>" if proj_name else f"project #{project_id}"
            # Send confirmation to the chat
            # Note: we can't reply to the media group directly, so we send a new message
            try:
                from telegram import Bot
                bot = Bot(token=TOKEN)
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"\u2705 {uploaded} file(s) uploaded to {proj_label}",
                    parse_mode="HTML",
                )
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
                    f"{_get_dashboard_url()}/api/bot/notification-by-message",
                    headers=_auth_headers(),
                    params={"chatId": chat_id, "messageId": parent_message_id},
                )
                if resp.status_code != 200:
                    # Not a notification reply — treat as search
                    return
                data = resp.json()
        except Exception:
            logger.exception("Failed to look up notification for message %s", parent_message_id)
            return

        notification_id = data.get("notificationId")
        opp_id = data.get("opportunityId")
        project_title = data.get("projectTitle", "Unknown Project")
        actor_user_id = data.get("actorUserId")
        if not notification_id or not opp_id:
            return

        # Post note to project — notify the original tagger
        content = update.message.text
        notify_list = [actor_user_id] if actor_user_id else []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_get_dashboard_url()}/api/bot/note",
                    headers=_auth_headers(),
                    json={
                        "opportunityId": opp_id,
                        "chatId": chat_id,
                        "content": content,
                        "categoryId": 10,  # Note category (ID 10 in history_categories)
                        "notifyUserList": notify_list,
                    },
                )
                if resp.status_code == 200:
                    await _reply_html(
                        update,
                        f"\u2705 <b>Note added</b>\nProject: {_esc(project_title)}",
                    )
                else:
                    err = resp.json().get("error", "Failed") if resp.status_code < 500 else "Server error"
                    await _reply_html(update, f"\u274c Failed to add note: {_esc(err)}")
        except Exception:
            logger.exception("Failed to post note for reply to notification %s", notification_id)
            await _reply_html(update, "\u274c Something went wrong. Please try again.")

    async def handle_document(update: Update, _ctx) -> None:
        """Handle photo/document uploads from employees."""
        if not update.message:
            return
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            return  # silently ignore non-employees

        msg = update.message

        # ── Media group handling (multiple photos at once) ──
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            if media_group_id not in _pending_media:
                _pending_media[media_group_id] = {
                    "chat_id": chat_id,
                    "project_id": _last_viewed_project.get(chat_id),
                    "files": [],
                    "mapping": mapping,
                }
            # Add this photo to the group
            if msg.photo:
                file_id = msg.photo[-1].file_id
                _pending_media[media_group_id]["files"].append({
                    "file_id": file_id,
                    "file_name": f"photo_{chat_id}_{file_id[:8]}.jpg",
                    "file_type": "photo",
                })
            elif msg.document:
                file_id = msg.document.file_id
                mime = msg.document.mime_type or ""
                _pending_media[media_group_id]["files"].append({
                    "file_id": file_id,
                    "file_name": msg.document.file_name or f"doc_{chat_id}_{file_id[:8]}",
                    "file_type": "photo" if mime.startswith("video/") else "document",
                })
            # Process group after a short delay (collect all photos)
            if media_group_id not in _pending_media or len(_pending_media[media_group_id]["files"]) == 1:
                asyncio.create_task(_process_media_group(media_group_id))
            return

        # ── Single file upload ──
        # Determine project ID
        project_id = _last_viewed_project.get(chat_id) or _pending_upload.get(chat_id, {}).get("projectId")
        if not project_id:
            await _reply_html(update, "Select a project first (search for it), then send the file.")
            return
        # Determine file info
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file_name = f"photo_{chat_id}_{file_id[:8]}.jpg"
            file_type = "photo"
        elif msg.document:
            file_id = msg.document.file_id
            file_name = msg.document.file_name or f"doc_{chat_id}_{file_id[:8]}"
            mime = msg.document.mime_type or ""
            if mime.startswith("video/"):
                file_type = "photo"  # videos go to photo gallery
            else:
                file_type = "document"
        else:
            return

        # Check if we're in note flow — upload to gallery AND create history event
        in_note_flow = _pending_note.get(chat_id, {}).get("step") == "awaiting_text"

        # Check file size
        cfg_resp = await _get("/api/v2/admin/bot-settings")
        max_mb = 20
        if cfg_resp:
            max_mb = cfg_resp.get("maxFileSizeMb", 20)
        # Download from Telegram
        try:
            tg_token = TOKEN
            url = f"https://api.telegram.org/bot{tg_token}/getFile?file_id={file_id}"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                if resp.status_code != 200 or not resp.json().get("ok"):
                    await _reply_html(update, "Failed to download file from Telegram.")
                    return
                file_path = resp.json()["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{tg_token}/{file_path}"
                file_resp = await client.get(dl_url, timeout=60)
                if file_resp.status_code != 200:
                    await _reply_html(update, "Failed to download file from Telegram.")
                    return
                file_data = file_resp.content
        except Exception:
            logger.exception("Failed to download file from Telegram")
            await _reply_html(update, "Failed to download file. Please try again.")
            return
        # Check size
        if len(file_data) > max_mb * 1024 * 1024:
            await _reply_html(update, f"File too large. Maximum size is {max_mb}MB.")
            return
        # Upload to server
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                import io
                files = {"file": (file_name, io.BytesIO(file_data), "application/octet-stream")}
                form_data = {"oppId": str(project_id), "chatId": str(chat_id), "fileType": file_type}
                resp = await client.post(
                    f"{_get_dashboard_url()}/api/bot/upload",
                    headers=_auth_headers(),
                    files=files,
                    data=form_data,
                )
                if resp.status_code == 200 and resp.json().get("ok"):
                    proj_name = ""
                    for d in _last_deals.get(chat_id, []):
                        if (d.get("id") or d.get("ID")) == project_id:
                            proj_name = d.get("title", "")
                            break
                    proj_label = f"<b>{_esc(proj_name)}</b>" if proj_name else f"project #{project_id}"
                    await _reply_html(
                        update,
                        f"\u2705 File uploaded to {proj_label}: <b>{_esc(file_name)}</b>",
                    )
                    uname = (mapping.get("contactName") or "Employee")
                    asyncio.create_task(log_activity(chat_id, uname, "file_uploaded", file_name, project_id=project_id))
                    # If in note flow, create a history event and continue
                    if in_note_flow:
                        try:
                            await _post("/api/bot/note", {
                                "chatId": chat_id,
                                "dealId": project_id,
                                "content": f"<p>{_esc(uname)} added a file via Telegram: {_esc(file_name)}</p>",
                                "categoryId": 12,  # "Files are uploaded"
                            })
                        except Exception:
                            pass
                else:
                    err = resp.json().get("error", "Upload failed") if resp.status_code < 500 else "Server error"
                    await _reply_html(update, f"Upload failed: {_esc(err)}")
        except Exception:
            logger.exception("Failed to upload file to server")
            await _reply_html(update, "Failed to upload file. Please try again.")

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

    async def today_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/today", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        tasks = resp.get("tasks", [])
        events = resp.get("events", [])
        lines = ["<b>Today's Overview:</b>", ""]
        if tasks:
            lines.append("<b>Tasks:</b>")
            for t in tasks:
                lines.append(f"  \u2022 {_esc(t.get('title', 'Untitled'))}")
            lines.append("")
        if events:
            lines.append("<b>Calendar:</b>")
            for e in events:
                title = e.get("title", "")
                time_str = e.get("startTime", "")
                lines.append(f"  \U0001f4c5 {_esc(title)}" + (f" at {_esc(time_str)}" if time_str else ""))
            lines.append("")
        if not tasks and not events:
            lines.append("Nothing scheduled for today.")
        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = _truncate_html(msg, 4000)
        await _reply_html(update, msg)

    async def tasks_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/tasks", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server. Try again shortly.")
            return
        if not resp:
            await _reply_html(update, "No open tasks assigned to you.")
            return
        lines = [f"<b>Your open tasks</b> ({len(resp)}):", ""]
        buttons = []
        for i, t in enumerate(resp, 1):
            due = ""
            if t.get("dueDate"):
                due = f" — due {_fmt_date(t['dueDate'])}"
            proj = f" [{_esc(t['projectTitle'])}]" if t.get("projectTitle") else ""
            lines.append(f"{i}. <b>{_esc(t['title'])}</b>{due}{proj}")
            if t.get("description"):
                lines.append(f"   <i>{_esc(t['description'][:80])}</i>")
            tid = t.get("id", "")
            if tid:
                buttons.append([InlineKeyboardButton(f"✅ Done: {t['title'][:30]}", callback_data=f"tdone:{tid}")])
        buttons.append([InlineKeyboardButton("➕ New Task", callback_data="newtask")])
        await _reply_html(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    async def task_done_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            await _reply_html(update, "Usage: /task_done &lt;task_id&gt;\nUse /tasks to see your open tasks with their IDs.")
            return
        task_id = int(parts[1])
        resp = await _post("/api/bot/task-done", {"chatId": chat_id, "taskId": task_id})
        if resp and resp.get("ok"):
            await _reply_html(update, f"Task completed: <b>{_esc(resp.get('title', ''))}</b>")
            asyncio.create_task(log_activity(chat_id, (mapping.get("contactName") or "Employee"), "task_done", f"task={task_id}"))
        else:
            msg = resp.get("error", "Task not found or not assigned to you.") if resp else "Could not reach server."
            await _reply_html(update, f"Failed: {_esc(msg)}")

    async def new_task_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await _reply_html(update, "Usage: /new_task &lt;task title&gt;")
            return
        title = parts[1]
        resp = await _post("/api/bot/task-create", {"chatId": chat_id, "title": title})
        if resp and resp.get("ok"):
            await _reply_html(update, f"Task created: <b>{_esc(resp.get('title', ''))}</b> (ID: {resp.get('taskId')})")
            asyncio.create_task(log_activity(chat_id, (mapping.get("contactName") or "Employee"), "task_created", title))
        else:
            msg = resp.get("error", "Failed") if resp else "Could not reach server."
            await _reply_html(update, f"Failed: {_esc(msg)}")

    async def my_projects_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/my-projects", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        if not resp:
            await _reply_html(update, "No projects assigned to you.")
            return
        lines = [f"<b>Your projects</b> ({len(resp)}):", ""]
        for i, p in enumerate(resp, 1):
            amt = _fmt_money(p.get("amount", 0), p.get("currency", ""))
            due = f" — due {_fmt_date(p['dueDate'])}" if p.get("dueDate") else ""
            contact = f" — {_esc(p['contact'])}" if p.get("contact") else ""
            lines.append(f"{i}. <b>{_esc(p['title'])}</b> [{_esc(p.get('stage', ''))}]{due}{contact}")
        await _reply_html(update, "\n".join(lines))

    async def overdue_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/overdue", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        if not resp:
            await _reply_html(update, "No overdue projects.")
            return
        lines = [f"<b>Overdue projects</b> ({len(resp)}):", ""]
        for i, p in enumerate(resp, 1):
            lines.append(f"{i}. <b>{_esc(p['title'])}</b> — due {_fmt_date(p.get('dueDate', ''))}")
        await _reply_html(update, "\n".join(lines))

    async def due_this_week_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/due-this-week", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        if not resp:
            await _reply_html(update, "No projects due this week.")
            return
        lines = [f"<b>Due this week</b> ({len(resp)}):", ""]
        for i, p in enumerate(resp, 1):
            lines.append(f"{i}. <b>{_esc(p['title'])}</b> — due {_fmt_date(p.get('dueDate', ''))}")
        await _reply_html(update, "\n".join(lines))

    async def calendar_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/calendar", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        if not resp:
            await _reply_html(update, "No upcoming events.")
            return
        lines = ["<b>Upcoming events:</b>", ""]
        for ev in resp[:10]:
            when = ev.get("dtstart", "")
            if when:
                try:
                    when = when[:16].replace("T", " ")
                except Exception:
                    pass
            loc = f" — {_esc(ev['location'])}" if ev.get("location") else ""
            lines.append(f"\U0001f4c5 <b>{_esc(ev.get('summary', 'Event'))}</b>\n   {when}{loc}")
        await _reply_html(update, "\n".join(lines))

    async def pipeline_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/pipeline", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        if not resp:
            await _reply_html(update, "No pipeline data.")
            return
        lines = ["<b>Pipeline Overview:</b>", ""]
        for s in resp:
            lines.append(f"<b>{_esc(s['stage'])}</b>: {s['count']} projects — {_fmt_money(s['total'], 'USD')}")
        await _reply_html(update, "\n".join(lines))

    async def stats_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/stats")
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        lines = [
            "<b>Dashboard Stats:</b>",
            "",
            f"Open projects: <b>{resp.get('totalProjects', 0)}</b>",
            f"Total pipeline: <b>{_fmt_money(resp.get('totalValue', 0), 'USD')}</b>",
            f"Closed won: <b>{resp.get('closedWon', 0)}</b>",
            f"Open tasks: <b>{resp.get('openTasks', 0)}</b>",
        ]
        await _reply_html(update, "\n".join(lines))

    async def recent_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/recent", {"chatId": chat_id})
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        if not resp:
            await _reply_html(update, "No recent activity.")
            return
        lines = ["<b>Recent activity (7 days):</b>", ""]
        for ev in resp[:10]:
            when = ev.get("createdAt", "")[:16].replace("T", " ") if ev.get("createdAt") else ""
            proj = f" [{_esc(ev['projectTitle'])}]" if ev.get("projectTitle") else ""
            snippet = re.sub(r'<[^>]+>', '', (ev.get("content") or "")[:80])
            snippet = _esc(snippet)
            lines.append(f"\U0001f4dd {when}{proj}\n   {snippet}")
        await _reply_html(update, "\n".join(lines))

    async def team_command(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        mapping = await get_mapping(chat_id)
        if not mapping or not mapping.get("employee"):
            await _reply_html(update, "Employee access required.")
            return
        resp = await _get("/api/bot/team")
        if resp is None:
            await _reply_html(update, "Could not reach server.")
            return
        lines = [f"<b>Team members</b> ({len(resp)}):", ""]
        for m in resp:
            lines.append(f"\U0001f464 <b>{_esc(m.get('displayName', ''))}</b> — {_esc(m.get('email', ''))}")
        await _reply_html(update, "\n".join(lines))

    async def search_command(update: Update, _ctx) -> None:
        await _reply_html(update, "Send a project name to search across all projects.")

    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("tag", tag_command))
    app.add_handler(CommandHandler("reset_password", reset_password_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("task_done", task_done_command))
    app.add_handler(CommandHandler("new_task", new_task_command))
    app.add_handler(CommandHandler("my_projects", my_projects_command))
    app.add_handler(CommandHandler("overdue", overdue_command))
    app.add_handler(CommandHandler("due_this_week", due_this_week_command))
    app.add_handler(CommandHandler("calendar", calendar_command))
    app.add_handler(CommandHandler("pipeline", pipeline_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("team", team_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_reply))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot polling...")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
