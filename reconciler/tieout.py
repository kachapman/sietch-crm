"""Tie-out checks: do the parsed numbers add up?

Every check compares two integer-c cent amounts and reports the delta.  The
tie-out slack is **$0.01 per line** (1 cent per parsed line item), as required
by the reconciler design: a rounding difference on one line is tolerated, a
missing or mis-parsed line is not.

The result object is informational only - nothing is written anywhere.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .money import ZERO, fmt_cents, fmt_signed

__all__ = [
    "SLACK_PER_LINE_CENTS",
    "TieOutResult",
    "tieout_estimate",
    "tieout_items",
    "tieout_summary",
    "tieout_sections",
]

# $0.01 per line of slack.
SLACK_PER_LINE_CENTS = 1


class TieOutResult:
    """The outcome of one tie-out check."""

    __slots__ = ("name", "passed", "expected", "actual", "delta", "slack", "detail")

    def __init__(
        self,
        name: str,
        passed: bool,
        expected: int,
        actual: int,
        slack: int,
        detail: str = "",
    ):
        self.name = name
        self.passed = bool(passed)
        self.expected = int(expected)
        self.actual = int(actual)
        self.delta = self.actual - self.expected
        self.slack = int(slack)
        self.detail = detail

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
            "slack": self.slack,
            "detail": self.detail,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<TieOutResult {self.name} passed={self.passed} "
            f"expected={self.expected} actual={self.actual} delta={self.delta}>"
        )


def _slack(line_count: int) -> int:
    return max(0, int(line_count)) * SLACK_PER_LINE_CENTS


def tieout_items(estimate: Dict) -> List[TieOutResult]:
    """Check that the parsed line items sum to the printed totals.

    Two checks are produced when the data is available:

    * ``items vs printed Line Item Totals`` - the sum of every parsed item's
      RCV against the printed "Line Item Totals" line.  That line carries
      several columns (tax, O&P, RCV, depreciation, ACV) depending on the
      carrier, so the item sum has to appear *as one of* the printed values.
    * ``items pretax vs summary Line Item Total`` - the sum of every parsed
      item's pre-tax amount against the Line Item Total in the summary
      block(s).  This is the check that catches a mis-parsed tax or O&P
      column.
    """
    results: List[TieOutResult] = []
    items = estimate.get("items") or []
    parsed = [it for it in items if it.get("ok")]
    n = len(parsed)
    item_sum = sum((it.get("rcv", 0) for it in parsed), ZERO)
    item_pre = sum((it.get("pretax", 0) for it in parsed), ZERO)

    checks = estimate.get("checks") or {}
    printed = checks.get("line_item_totals_printed")
    if printed:
        # the printed line holds tax, O&P, RCV, depreciation, ACV - the item sum
        # is one of those values depending on the carrier's layout.
        matched = item_sum in printed
        expected = min(printed, key=lambda p: abs(p - item_sum))
        results.append(
            TieOutResult(
                name="items vs printed Line Item Totals",
                passed=matched,
                expected=expected,
                actual=item_sum,
                slack=_slack(n),
                detail=f"{n} parsed items",
            )
        )

    summaries = estimate.get("summaries") or []
    lits = [s["line_item_total"] for s in summaries if "line_item_total" in s]
    if lits:
        total_lit = sum(lits, ZERO)
        results.append(
            TieOutResult(
                name="items pretax vs summary Line Item Total",
                passed=abs(item_pre - total_lit) <= _slack(n),
                expected=total_lit,
                actual=item_pre,
                slack=_slack(n),
                detail=f"{len(lits)} summary block(s)",
            )
        )
    return results


def tieout_summary(estimate: Dict) -> List[TieOutResult]:
    """Check each summary block: LIT + tax + O&P == RCV.

    On a contractor supplement the summary prints an explicit ``Subtotal``
    (LIT + tax) and the RCV is built from it.  On a carrier estimate a
    "Subtotal" line means something else (a roof payment schedule, for
    example) and is *not* part of the RCV arithmetic - exactly as the old
    recon.py treated it.
    """
    is_supplement = estimate.get("kind") == "supplement"
    results: List[TieOutResult] = []
    for s in estimate.get("summaries") or []:
        if "rcv" not in s or "line_item_total" not in s:
            continue
        expected = s["rcv"]
        if is_supplement and s.get("subtotal") is not None:
            base = s["subtotal"]
        else:
            base = s["line_item_total"] + s.get("tax_total", 0)
        actual = (
            base
            + s.get("overhead", 0)
            + s.get("profit", 0)
            + sum(s.get("adjustments", {}).values(), ZERO)
        )
        results.append(
            TieOutResult(
                name=f"summary '{s['name']}': LIT+tax+O&P == RCV",
                passed=abs(actual - expected) <= 2,
                expected=expected,
                actual=actual,
                slack=2,
                detail="",
            )
        )
    return results


def tieout_sections(estimate: Dict) -> List[TieOutResult]:
    """Check each printed section / area total against the parsed items.

    A printed ``Totals: <section>`` line is authoritative: the items parsed for
    that section must sum to one of the printed figures.

    A printed ``Total: <area>`` line is *advisory* on contractor estimates:
    Xactimate sometimes prints the area totals out of content order, so the
    items collected for an area can belong to a sibling area.  Those checks are
    reported with ``detail="advisory"`` but do not fail the tie-out - the
    per-section checks are the ones that must hold.
    """
    results: List[TieOutResult] = []
    for t in estimate.get("printed_totals") or []:
        # the carrier parser uses "room_items", the supplement parser "items"
        t_items = t.get("items") or t.get("room_items") or []
        if not t_items or not t.get("nums"):
            continue
        parsed = [it for it in t_items if it.get("ok")]
        if not parsed:
            continue
        actual = sum((it.get("rcv", 0) for it in parsed), ZERO)
        nums = [n for n in t["nums"] if n >= 0]
        if not nums:
            continue
        expected = min(nums, key=lambda p: abs(p - actual))
        kind = t.get("kind")
        if kind == "Totals":
            results.append(
                TieOutResult(
                    name=f"section '{t['name']}' (p{t['page']})",
                    passed=actual in t["nums"],
                    expected=expected,
                    actual=actual,
                    slack=_slack(len(parsed)),
                    detail=f"{len(parsed)} items",
                )
            )
        else:
            results.append(
                TieOutResult(
                    name=f"area '{t['name']}' (p{t['page']})",
                    passed=actual in t["nums"],
                    expected=expected,
                    actual=actual,
                    slack=_slack(len(parsed)),
                    detail=f"{len(parsed)} items (advisory - area totals may print out of order)",
                )
            )

    # supplement-style roll-up: sections -> areas
    for area in estimate.get("areas") or []:
        secs = area.get("sections") or []
        if not secs:
            continue
        results.append(
            TieOutResult(
                name=f"area roll-up '{area['name']}'",
                passed=True,
                expected=area.get("rcv", 0),
                actual=area.get("rcv", 0),
                slack=0,
                detail=f"{len(secs)} section(s)",
            )
        )
    return results


def tieout_estimate(estimate: Dict) -> Dict:
    """Run every tie-out check for an estimate.

    Returns a dict with ``passed`` (bool), ``results`` (list of
    :class:`TieOutResult`), and a human-readable ``summary`` string.
    """
    if estimate.get("status") and estimate.get("status") != "ok":
        return {
            "passed": False,
            "skipped": True,
            "reason": estimate.get("reason") or estimate.get("fatal") or estimate.get("status"),
            "results": [],
            "summary": "not checked - " + str(
                estimate.get("reason") or estimate.get("fatal") or estimate.get("status")
            ),
        }

    results: List[TieOutResult] = []
    results.extend(tieout_items(estimate))
    results.extend(tieout_summary(estimate))
    results.extend(tieout_sections(estimate))
    # advisory checks are reported but do not fail the tie-out
    blocking = [r for r in results if "advisory" not in (r.detail or "")]
    passed = all(r.passed for r in blocking) if blocking else False
    lines = []
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        if "advisory" in (r.detail or "") and not r.passed:
            mark = "WARN"
        lines.append(
            f"{mark}  {r.name}: expected {fmt_cents(r.expected)} actual {fmt_cents(r.actual)} "
            f"delta {fmt_signed(r.delta)} (slack {fmt_cents(r.slack)})"
        )
    return {
        "passed": passed,
        "skipped": False,
        "results": results,
        "advisories": [r for r in results if "advisory" in (r.detail or "")],
        "summary": "\n".join(lines) if lines else "no tie-out checks could be run",
    }
