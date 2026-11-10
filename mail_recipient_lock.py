"""Per-user recipient lock for the live acceptance test (2026-11-10, Ken).

A user flagged with `users.mail_send_locked_recipient = <address>` may only send
to exactly that address — every To/Cc/Bcc recipient (including auto-added BCC)
must normalize to it. Normal users are unaffected. No bypass: the check runs in
`_handle_mail_send`, which every compose/reply/forward path uses.
"""
from __future__ import annotations

import re
from email.utils import getaddresses

_ANGLE = re.compile(r"<([^>]+)>")


def normalize(address: str) -> str:
    text = str(address or "").strip().lower()
    m = _ANGLE.search(text)
    if m:
        text = m.group(1).strip().lower()
    return text


def parse_recipients(to_addr, cc_addr=None, bcc_addr=None) -> list[str]:
    raw = ",".join(str(x) for x in (to_addr, cc_addr, bcc_addr) if x)
    if not raw.strip():
        return []
    return [a.strip().lower() for _, a in getaddresses([raw]) if a and a.strip()]


def check_lock(locked: str, recipients: list[str]) -> tuple[bool, str]:
    target = normalize(locked)
    if not target:
        return True, ""
    got = [normalize(r) for r in recipients if r]
    if not got:
        return False, "no recipients"
    if len(got) != 1:
        return False, f"exactly one recipient required ({target}); got {len(got)}"
    if got[0] != target:
        return False, f"recipient {got[0]} is not the locked address {target}"
    return True, ""
