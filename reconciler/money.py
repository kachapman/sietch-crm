"""Integer-cents money helpers for the reconciler.

Every monetary amount in the reconciler is an ``int`` number of cents.  No
floats, no ``Decimal`` - floating point money is how you get a report that is
off by a penny.  Parsing is done on strings (the pdftotext output) and results
are integers.

The approach is copied from Ook's ``money.py`` (sherwood-estimator); the module
is not imported because Ook is a separate project.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "ZERO",
    "to_cents",
    "parse_money",
    "fmt_cents",
    "fmt_signed",
    "add",
    "sub",
    "mul_pct",
    "is_close",
]

ZERO = 0

# A money-ish token: optional $, optional thousands separators, optional (.),
# optional trailing asterisk (carrier estimates print '*' next to amounts).
_NUM_RE = re.compile(r"^\(?-?\$?\s*[\d,]*\d(?:\.\d+)?\)?\*?$")


def to_cents(amount) -> int:
    """Convert a number/Decimal/str to integer cents (half-up rounding)."""
    if amount is None:
        return ZERO
    if isinstance(amount, int):
        return amount
    if isinstance(amount, Decimal):
        d = amount
    elif isinstance(amount, float):
        d = Decimal(repr(amount))
    else:
        s = str(amount).strip().replace("$", "").replace(",", "").replace("*", "")
        if not s:
            return ZERO
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()").strip()
        if s.startswith("-"):
            neg = True
            s = s[1:].strip()
        try:
            d = Decimal(s)
        except (InvalidOperation, ValueError):
            return ZERO
        if neg:
            d = -d
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_money(token) -> int:
    """Parse a money token (possibly parenthesised / $-prefixed) to cents."""
    if token is None:
        return ZERO
    if isinstance(token, int):
        return token
    s = str(token).strip()
    if not s:
        return ZERO
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip().replace("$", "").replace(",", "").rstrip("*").strip()
    if s.startswith("-"):
        neg = True
        s = s[1:].strip()
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return ZERO
    if neg:
        d = -d
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_cents(cents, symbol: str = "$") -> str:
    """Format integer cents as ``$1,234.56`` (or ``-`` prefix for negatives)."""
    if cents is None:
        return "n/a"
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{symbol}{whole:,}.{frac:02d}"


def fmt_signed(cents, symbol: str = "$") -> str:
    """Format integer cents with an explicit ``+`` for positives."""
    if cents is None:
        return "n/a"
    cents = int(cents)
    if cents > 0:
        return "+" + fmt_cents(cents, symbol)
    return fmt_cents(cents, symbol)


def add(*amounts) -> int:
    return sum(int(a or 0) for a in amounts)


def sub(a, b) -> int:
    return int(a or 0) - int(b or 0)


def mul_pct(cents, percent: Decimal | float | int) -> int:
    """cents * percent/100, rounded half-up to whole cents."""
    p = Decimal(str(percent)) if not isinstance(percent, Decimal) else percent
    return int((Decimal(int(cents or 0)) * p / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def is_close(a, b, slack_cents: int = 1) -> bool:
    """True when two cent amounts are within ``slack_cents`` of each other."""
    return abs(int(a or 0) - int(b or 0)) <= int(slack_cents)
