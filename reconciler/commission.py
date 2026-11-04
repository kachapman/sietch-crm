"""Commission calculation.

Information only - this never bills anything and never talks to an
accounting system.  The result is a number a human can review.

The commission rules (from the reconciler plan, Nov 4 2026):

* **First increase**: ``max(250, 10% x increase - 250)`` - i.e. 10% of the
  increase minus the $250 estimate fee, but never less than the $250 fee
  itself.
* **Later increase**: 10% of the increase.
* **Already noted**: the amount already noted on the project is subtracted
  from the commission, so the same increase is never billed twice.

All amounts are integer cents.  ``$250`` is ``25_000`` cents and ``10%`` is
computed with half-up rounding to whole cents.
"""
from __future__ import annotations

from typing import Dict, Optional

from .money import ZERO, fmt_cents, fmt_signed, mul_pct

__all__ = [
    "ESTIMATE_FEE_CENTS",
    "COMMISSION_RATE_PCT",
    "commission",
    "commission_from_totals",
]

# The $250 estimate fee.
ESTIMATE_FEE_CENTS = 25_000

# 10% of the increase.
COMMISSION_RATE_PCT = 10


def commission(
    old_totals: Dict,
    new_totals: Dict,
    already_noted: int = 0,
    first: bool = False,
) -> Dict:
    """Compute the commission for an increase.

    ``old_totals`` / ``new_totals`` are the ``totals`` dicts of two parsed
    estimates (see :mod:`reconciler.parse`).  ``already_noted`` is the amount
    already noted/billed for this claim; it is subtracted from the commission
    so nothing is counted twice.

    The returned dict is informational only.
    """
    old_rcv = int((old_totals or {}).get("rcv", 0) or 0)
    new_rcv = int((new_totals or {}).get("rcv", 0) or 0)
    old_lit = int((old_totals or {}).get("line_item_total", 0) or 0)
    new_lit = int((new_totals or {}).get("line_item_total", 0) or 0)

    increase = new_rcv - old_rcv
    increase_pretax = new_lit - old_lit

    result: Dict = {
        "old_rcv": old_rcv,
        "new_rcv": new_rcv,
        "increase_rcv_with_tax": increase,
        "increase_pretax": increase_pretax,
        "tax_component": increase - increase_pretax,
        "already_noted": int(already_noted or 0),
        "mode": "first increase" if first else "later increase",
    }

    if increase <= 0:
        result["gross_commission"] = ZERO
        result["commission"] = ZERO
        result["note"] = "NO increase - nothing to bill"
        return result

    tenth = mul_pct(increase, COMMISSION_RATE_PCT)
    if first:
        # max(250, 10% x increase - 250)
        after_fee = tenth - ESTIMATE_FEE_CENTS
        gross = max(ESTIMATE_FEE_CENTS, after_fee)
        result.update(
            gross_commission=gross,
            ten_percent=tenth,
            estimate_fee=ESTIMATE_FEE_CENTS,
            minimum_applied=after_fee < ESTIMATE_FEE_CENTS,
        )
    else:
        gross = tenth
        result.update(gross_commission=gross, ten_percent=tenth)

    # never bill the same increase twice
    net = gross - int(already_noted or 0)
    result["commission"] = net if net > 0 else ZERO
    if net <= 0:
        result["note"] = (
            f"commission already fully noted ({fmt_cents(already_noted)}) - nothing further to bill"
        )
    return result


def commission_from_totals(
    old_totals: Dict,
    new_totals: Dict,
    already_noted: int = 0,
    first: bool = False,
) -> Dict:
    """Alias of :func:`commission` kept for readability at call sites."""
    return commission(old_totals, new_totals, already_noted=already_noted, first=first)
