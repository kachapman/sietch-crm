"""Microsoft Graph sendMail client — stdlib-only (urllib).

Bypasses SMTP AUTH when Microsoft disables it on a mailbox
(5.7.139 "SmtpClientAuthentication is disabled"). Microsoft refuses to
re-enable SMTP AUTH on many consumer accounts via support, but the
Graph sendMail endpoint with delegated Mail.Send scope still works
through a separate code path.

Body content type is chosen as HTML when an HTML body is provided,
otherwise Text. Attachments are uploaded as base64 fileAttachment items
(Graph caps total message size at ~25MB including attachments).

From identity (2026-11-10, Ken): the message's `from` is set explicitly
from the SELECTED mail_accounts row — that row's address plus its
`smtp_from_name` — never from a global default, another account, or the
token owner's profile. Graph only honours `from` when it names the
signed-in mailbox itself, so `verified_identity` (the identity stored for
the Graph token) is REQUIRED and must equal `from_addr`; a mismatch or an
unknown identity is refused before any HTTP request. This is what stops a
send going out under another mailbox's name/address.

The provider may still rewrite the display name to the mailbox profile
name (Outlook does this for consumer mailboxes); setting `from` is the
most we can do from the sending API, so the CRM also surfaces the
display name the provider actually applied (Settings → account).
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


def _norm_addr(value: str | None) -> str:
    return (value or "").strip().lower()


def build_from_identity(from_addr: str, from_name: str | None) -> dict:
    """Build the Graph `from` (and `sender`) emailAddress from an account row.

    The display name comes ONLY from the selected account's `smtp_from_name`.
    An empty/blank name yields the address with no `name` key at all, so the
    provider decides — it never borrows a name from another account.
    """
    email_address: dict[str, str] = {"address": (from_addr or "").strip()}
    name = (from_name or "").strip()
    if name:
        email_address["name"] = name
    return {"emailAddress": email_address}


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
    from_name: str | None = None,
    verified_identity: str | None = None,
) -> tuple[bool, str | None]:
    """Send mail via Graph /me/sendMail. Returns (success, error_message).

    `from_addr` / `from_name` must come from the selected mail_accounts row.
    `verified_identity` is the mailbox the Graph token was verified to
    belong to; Graph only permits `from` == that mailbox, so this is
    asserted BEFORE the request and a mismatch is refused (never sent
    under another mailbox's identity).
    """
    if not graph_access_token:
        return False, "Graph access token is missing"

    sender_addr = _norm_addr(from_addr)
    identity = _norm_addr(verified_identity)
    if not sender_addr:
        return False, (
            "Refusing to send: this mail account has no address, so no From "
            "identity can be asserted. Nothing was sent."
        )
    if not identity:
        return False, (
            "Refusing to send: the Graph connection's mailbox identity is not "
            "verified, so the From address cannot be asserted. Reconnect Graph "
            "for this mailbox. Nothing was sent."
        )
    if identity != sender_addr:
        logger.warning(
            "Refusing Graph send: from=%s but the token belongs to %s", sender_addr, identity)
        return False, (
            f"Refusing to send: the From address is {from_addr} but this mailbox's "
            f"Graph token belongs to {verified_identity}. Nothing was sent."
        )

    to_recipients, cc_recipients, bcc_recipients = _build_recipients(to_addr, cc_addr, bcc_addr)
    if not to_recipients:
        return False, "At least one recipient (to) is required"

    if html_body:
        body = {"contentType": "HTML", "content": html_body}
    elif text_body:
        body = {"contentType": "Text", "content": text_body}
    else:
        body = {"contentType": "Text", "content": ""}

    # Explicit From identity from the account row. `sender` is intentionally
    # NOT set: for /me/sendMail the authenticated mailbox is the sender, and
    # `sender` is only meaningful with shared-mailbox (Send As) permission —
    # setting it here can be rejected with ErrorSendAsDenied. `from` is the
    # one identity the caller selects, and it is proven equal to the token's
    # mailbox above.
    message: dict = {
        "subject": subject or "",
        "body": body,
        "from": build_from_identity(from_addr, from_name),
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
