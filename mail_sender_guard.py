"""Send-time sender guard: which mailbox may send for which job.

Ken's model (2026-11-10): a job's **Contact** is the contractor identity, and
an admin maps that Contact to the mailbox branded for it
(`mail_accounts.contractor_contact_id`). Rules:

1. If the job's Contact has one or more mapped accounts, the send MUST come
   from one of them (409 otherwise, logged in `mail_send_refusals`).
2. If the Contact has no mapped account, the user picks any account manually
   (no alphabetical default, but the send is allowed).
3. A reply/forward must come from the account the original message was
   received on (or sent from). If that account is not the job's mapped account,
   the send is blocked with a clear "thread conflict" reason.
4. No silent fallback: the guard never picks a different account.

Pure-ish: takes the project `db` module so it is testable with a fake.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sietch.mail_sender_guard")

REASON_NO_DEAL = "no_deal"
REASON_MAPPED_MISMATCH = "mapped_mismatch"
REASON_ORIGINAL_MISMATCH = "original_account_mismatch"
REASON_THREAD_CONFLICT = "thread_conflict"
REASON_ACCOUNT_NOT_FOUND = "account_not_found"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def mapped_accounts_for_contact(db, contact_id: int | None) -> list[dict]:
    """Accounts explicitly mapped to a Contact (id + email only)."""
    cid = _as_int(contact_id)
    if not cid:
        return []
    try:
        rows = db.query_dicts(
            "SELECT id, email FROM mail_accounts WHERE contractor_contact_id = %s "
            "AND sync_enabled IS NOT FALSE ORDER BY email",
            (cid,),
        )
    except Exception:
        logger.exception("Could not read contractor mappings for contact %s", cid)
        return []
    return [{"id": int(r["id"]), "email": r["email"]} for r in rows]


def _contact_name(db, contact_id: int | None) -> str:
    cid = _as_int(contact_id)
    if not cid:
        return ""
    row = db.query_one(
        "SELECT COALESCE(NULLIF(company, ''), NULLIF(TRIM(COALESCE(first_name, '') || ' ' || "
        "COALESCE(last_name, '')), ''), '') AS name FROM contacts WHERE id = %s",
        (cid,),
    )
    return (row or {}).get("name") or ""


def _linked_deal_for_message(db, message_id: int | None) -> int | None:
    mid = _as_int(message_id)
    if not mid:
        return None
    row = db.query_one(
        "SELECT opportunity_id FROM mail_deal_links WHERE message_id = %s "
        "ORDER BY id LIMIT 1",
        (mid,),
    )
    return _as_int((row or {}).get("opportunity_id"))


def evaluate(db, *, account: dict, deal_id: Any = None,
             reply_to_message_id: Any = None) -> dict:
    """Decide whether `account` may send for this job/thread.

    Returns a dict:
      allowed, reason, message, deal_id, contact_id, contact_name,
      mapped (list of {id,email}), original_account_id
    """
    account_id = _as_int(account.get("id"))
    account_email = account.get("email") or ""
    deal = _as_int(deal_id)
    reply_mid = _as_int(reply_to_message_id)

    original_account_id = None
    if reply_mid:
        row = db.query_one(
            "SELECT account_id FROM mail_messages WHERE id = %s", (reply_mid,)
        )
        original_account_id = _as_int((row or {}).get("account_id"))
        if deal is None:
            deal = _linked_deal_for_message(db, reply_mid)

    contact_id = None
    if deal:
        row = db.query_one(
            "SELECT contact_id FROM opportunities WHERE id = %s", (deal,)
        )
        contact_id = _as_int((row or {}).get("contact_id"))

    mapped = mapped_accounts_for_contact(db, contact_id)
    mapped_ids = {m["id"] for m in mapped}

    result = {
        "allowed": True,
        "reason": "",
        "message": "",
        "deal_id": deal,
        "contact_id": contact_id,
        "contact_name": _contact_name(db, contact_id),
        "mapped": mapped,
        "original_account_id": original_account_id,
        "account_email": account_email,
    }

    # 3. thread conflict: the thread lives in an account that is not the job's
    # mapped mailbox. Sending "from the right contractor" would fork the
    # thread, so refuse with an explicit reason.
    if mapped and original_account_id and original_account_id not in mapped_ids:
        result.update(
            allowed=False,
            reason=REASON_THREAD_CONFLICT,
            message=(
                "This thread was received on a mailbox that is not the one mapped "
                f"to this job's contact ({', '.join(m['email'] for m in mapped)}). "
                "Sending would fork the conversation. Fix the mapping in "
                "Admin → Email accounts, or send a new email instead of a reply."
            ),
        )
        return result

    # 1. job mapping exists → the chosen account must be one of the mapped ones.
    if mapped and account_id not in mapped_ids:
        result.update(
            allowed=False,
            reason=REASON_MAPPED_MISMATCH,
            message=(
                f"This job's contact is mapped to {', '.join(m['email'] for m in mapped)}. "
                "Choose that mailbox to send this email."
            ),
        )
        return result

    # 5. replies/forwards must come from the original message's account.
    if original_account_id and account_id != original_account_id:
        result.update(
            allowed=False,
            reason=REASON_ORIGINAL_MISMATCH,
            message=(
                "A reply/forward must be sent from the mailbox that received the "
                "original message. Nothing was sent."
            ),
        )
        return result

    return result


def log_refusal(db, *, user_id: Any, account_id: Any, deal_id: Any,
                reason: str) -> None:
    """Durable refusal audit row. Never raises."""
    try:
        db.execute(
            "INSERT INTO mail_send_refusals (user_id, account_id, deal_id, reason) "
            "VALUES (%s, %s, %s, %s)",
            (_as_int(user_id), _as_int(account_id), _as_int(deal_id),
             str(reason)[:300]),
        )
    except Exception:
        logger.exception("Could not persist mail send refusal")


__all__ = [
    "REASON_ACCOUNT_NOT_FOUND",
    "REASON_MAPPED_MISMATCH",
    "REASON_NO_DEAL",
    "REASON_ORIGINAL_MISMATCH",
    "REASON_THREAD_CONFLICT",
    "evaluate",
    "log_refusal",
    "mapped_accounts_for_contact",
]
