"""Send-time sender guard: a reply/forward must use the original message's account.

Rules (2026-11-10, revised):

1. A new email may be sent from any account the user can access.
2. A reply/forward must be sent from the account that received (or sent) the
   original message. Any other choice is refused with a plain, visible 409.
3. No silent fallback: the guard never picks a different account.

Pure-ish: takes the project `db` module so it is testable with a fake.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sietch.mail_sender_guard")

REASON_ORIGINAL_MISMATCH = "original_account_mismatch"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate(db, *, account: dict, deal_id: Any = None,
             reply_to_message_id: Any = None) -> dict:
    """Decide whether `account` may send for this job/thread.

    Returns a dict:
      allowed, reason, message, deal_id, original_account_id, account_email
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

    result = {
        "allowed": True,
        "reason": "",
        "message": "",
        "deal_id": deal,
        "original_account_id": original_account_id,
        "account_email": account_email,
    }

    # A reply/forward must come from the original message's account.
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


__all__ = [
    "REASON_ORIGINAL_MISMATCH",
    "evaluate",
]
