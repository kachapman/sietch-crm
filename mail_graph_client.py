"""Microsoft Graph sendMail client — stdlib-only (urllib).

Bypasses SMTP AUTH when Microsoft disables it on a mailbox
(5.7.139 "SmtpClientAuthentication is disabled"). Microsoft refuses to
re-enable SMTP AUTH on many consumer accounts via support, but the
Graph sendMail endpoint with delegated Mail.Send scope still works
through a separate code path.

Body content type is chosen as HTML when an HTML body is provided,
otherwise Text. Attachments are uploaded as base64 fileAttachment items
(Graph caps total message size at ~25MB including attachments).
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("sietch.mailgraph")

_GRAPH_SENDMAIL = "https://graph.microsoft.com/v1.0/me/sendMail"


def _recipient(address: str) -> dict:
    return {"emailAddress": {"address": address}}


def _build_recipients(
    to_addr: str,
    cc_addr: str | None = None,
    bcc_addr: str | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    to_recipients = [_recipient(a) for a in _split_addresses(to_addr)]
    cc_recipients = [_recipient(a) for a in _split_addresses(cc_addr)] if cc_addr else []
    bcc_recipients = [_recipient(a) for a in _split_addresses(bcc_addr)] if bcc_addr else []
    return to_recipients, cc_recipients, bcc_recipients


def _split_addresses(addr: str | None) -> list[str]:
    if not addr:
        return []
    return [a.strip() for a in addr.split(",") if a.strip()]


def _build_attachments(attachments: list[dict] | None) -> list[dict]:
    """attachments: [{filename, content (bytes), mime_type}] -> Graph fileAttachment items."""
    if not attachments:
        return []
    out: list[dict] = []
    for att in attachments:
        filename = att.get("filename", "attachment")
        content = att.get("content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8")
        mime_type = att.get("mime_type") or "application/octet-stream"
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentType": mime_type,
            "contentBytes": base64.b64encode(content).decode("ascii"),
        })
    return out


def send_email_via_graph(
    graph_access_token: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    cc_addr: str | None = None,
    bcc_addr: str | None = None,
    attachments: list[dict] | None = None,
) -> tuple[bool, str | None]:
    """Send mail via Graph /me/sendMail. Returns (success, error_message).

    `from_addr` is informational only — Graph derives the sender from the
    access token. Used for logging and for the caller to record the
    sending account.
    """
    if not graph_access_token:
        return False, "Graph access token is missing"

    to_recipients, cc_recipients, bcc_recipients = _build_recipients(to_addr, cc_addr, bcc_addr)
    if not to_recipients:
        return False, "At least one recipient (to) is required"

    if html_body:
        body = {"contentType": "HTML", "content": html_body}
    elif text_body:
        body = {"contentType": "Text", "content": text_body}
    else:
        body = {"contentType": "Text", "content": ""}

    message: dict = {
        "subject": subject or "",
        "body": body,
        "toRecipients": to_recipients,
    }
    if cc_recipients:
        message["ccRecipients"] = cc_recipients
    if bcc_recipients:
        message["bccRecipients"] = bcc_recipients

    graph_attachments = _build_attachments(attachments)
    if graph_attachments:
        message["attachments"] = graph_attachments

    payload = {"message": message, "saveToSentItems": True}
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(_GRAPH_SENDMAIL, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {graph_access_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # Graph sendMail returns 202 Accepted with an empty body on success.
            if 200 <= resp.status < 300:
                logger.info("Graph sendMail OK from=%s to=%s subj=%r", from_addr, to_addr, subject)
                return True, None
            return False, f"Graph returned HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error("Graph sendMail HTTP %s from=%s: %s", e.code, from_addr, err_body)
        msg = _summarize_graph_error(e.code, err_body)
        return False, msg
    except Exception as e:
        logger.error("Graph sendMail failed from=%s: %s", from_addr, e)
        return False, str(e)


def _summarize_graph_error(code: int, body: str) -> str:
    """Map the most common Graph error codes to actionable user messages."""
    try:
        parsed = json.loads(body)
        err = parsed.get("error") or {}
        msg = err.get("message") or ""
        inner = err.get("innerError") or {}
        code_inner = inner.get("code") or ""
    except Exception:
        msg = ""
        code_inner = ""

    if code == 401 or code_inner in ("InvalidAuthenticationToken", "AuthenticationFailed", "Unauthorized"):
        return (
            "Microsoft Graph authentication failed. Reconnect the Outlook account "
            "from the mail account settings to grant the Mail.Send permission."
        )
    if code == 403 or code_inner in ("ErrorItemNotFound", "ErrorAccessDenied", "insufficientPermissions"):
        if "Mail.Send" in (msg + code_inner):
            return (
                "Microsoft Graph refused Mail.Send. The delegated permission is missing — "
                "reconnect the Outlook account from mail account settings and accept "
                "the Mail.Send consent prompt."
            )
        return f"Graph access denied (HTTP {code}): {msg or code_inner or 'unknown'}"
    if code == 429 or code_inner in ("ErrorMailboxMove", "throttledRequest"):
        return "Graph rate limit hit. Try again in a minute."
    if code >= 500:
        return f"Microsoft Graph temporarily unavailable (HTTP {code}). Try again in a few minutes."
    return f"Graph send failed (HTTP {code}): {msg or code_inner or body[:200]}"
