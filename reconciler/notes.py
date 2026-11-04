"""Draft note text.

Information only - this module produces *text*.  It never posts anything,
never writes to the CRM and never touches an accounting system.  A human
reviews the draft before anything happens to a project.

The note format agreed for the reconciler is::

    Claim increased by $X ($orig to $new, no PP)

``no PP`` means "no personal property" - personal property coverage is never
billed, so it is excluded from the increase.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .money import ZERO, fmt_cents, fmt_signed

__all__ = [
    "claim_increase_note",
    "billing_summary_lines",
    "note_text",
    "NO_PP_MARKER",
]

NO_PP_MARKER = "no PP"


def claim_increase_note(
    old_rcv: int,
    new_rcv: int,
    already_noted: int = 0,
    exclude_personal_property: bool = True,
    extra_lines: Optional[List[str]] = None,
) -> str:
    """Build the "Claim increased by ..." note text.

    ``old_rcv`` and ``new_rcv`` are integer cents (the RCV of the original and
    the revised estimate).  ``already_noted`` is the amount already noted for
    this claim; when it is non-zero the note reports the *additional*
    increase.
    """
    old_rcv = int(old_rcv or 0)
    new_rcv = int(new_rcv or 0)
    already_noted = int(already_noted or 0)

    increase = new_rcv - old_rcv
    if already_noted:
        increase = increase - already_noted

    inner = f"{fmt_cents(old_rcv)} to {fmt_cents(new_rcv)}"
    if exclude_personal_property:
        inner = f"{inner}, {NO_PP_MARKER}"

    text = f"Claim increased by {fmt_cents(increase)} ({inner})"
    if extra_lines:
        extra = [ln for ln in extra_lines if ln]
        if extra:
            text = text + "\n" + "\n".join(extra)
    return text


def note_text(
    old_rcv: int,
    new_rcv: int,
    already_noted: int = 0,
    exclude_personal_property: bool = True,
    extra_lines: Optional[List[str]] = None,
) -> str:
    """Alias of :func:`claim_increase_note`."""
    return claim_increase_note(
        old_rcv,
        new_rcv,
        already_noted=already_noted,
        exclude_personal_property=exclude_personal_property,
        extra_lines=extra_lines,
    )


def billing_summary_lines(
    old_totals: Dict,
    new_totals: Dict,
    commission_result: Optional[Dict] = None,
) -> List[str]:
    """Human-readable billing summary lines (information only).

    No invoice is written and no invoice number is invented.
    """
    old_totals = old_totals or {}
    new_totals = new_totals or {}
    old_rcv = int(old_totals.get("rcv", 0) or 0)
    new_rcv = int(new_totals.get("rcv", 0) or 0)
    increase = new_rcv - old_rcv
    old_lit = int(old_totals.get("line_item_total", 0) or 0)
    new_lit = int(new_totals.get("line_item_total", 0) or 0)

    lines = [
        f"RCV: {fmt_cents(old_rcv)} -> {fmt_cents(new_rcv)} ({fmt_signed(increase)})",
        f"Line items (pre-tax): {fmt_cents(old_lit)} -> {fmt_cents(new_lit)} "
        f"({fmt_signed(new_lit - old_lit)})",
    ]
    if commission_result:
        comm = int(commission_result.get("commission", 0) or 0)
        lines.append(f"Commission (info only): {fmt_cents(comm)}")
        if commission_result.get("note"):
            lines.append(str(commission_result["note"]))
    return lines
