"""Tie-out checks: do the parsed numbers add up?

Every check compares two integer-cent amounts and reports the delta.  The
tie-out slack is **$0.01 per line** (1 cent per parsed line item), as required
by the reconciler design: a rounding difference on one line is tolerated, a
missing or mis-parsed line is not.  A check that matches within zero cents is
also flagged ``exact`` so a caller can show the stronger signal.

Checks:

* items vs the printed Line Item Totals        (:func:`tieout_items`)
* each summary block: LIT + tax + O&P == RCV   (:func:`tieout_summary`)
* each printed section / area total            (:func:`tieout_sections`)
* each row: qty x price + tax + O&P == RCV,
  and RCV - depreciation == ACV when printed   (:func:`tieout_rows`)
* the delta between two estimates:
  Sigma section = Sigma row = grand = printed difference
                                               (:func:`tieout_diff`)

A failed per-estimate tie-out or a failed delta tie-out blocks the draft -
see :func:`draft_gate`.

The result object is informational only - nothing is written anywhere.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from .money import ZERO, fmt_cents, fmt_signed

__all__ = [
    "SLACK_PER_LINE_CENTS",
    "ROW_MATH_TOLERANCE_CENTS",
    "DIFF_FIELDS",
    "TieOutResult",
    "tieout_estimate",
    "tieout_items",
    "tieout_summary",
    "tieout_sections",
    "tieout_rows",
    "tieout_diff",
    "draft_gate",
]

# $0.01 per line of slack.
SLACK_PER_LINE_CENTS = 1

# qty x price is rounded to a cent by Xactimate; allow 5 cents for that
# rounding on a single row (same tolerance the standalone reconciler used).
ROW_MATH_TOLERANCE_CENTS = 5

# Fields compared by the delta tie-out.  ``pretax`` is the Line Item Total.
DIFF_FIELDS = ("rcv", "pretax", "tax", "op", "deprec", "acv")

FIELD_LABELS = {
    "rcv": "RCV",
    "pretax": "Line Item Total",
    "tax": "tax",
    "op": "O&P",
    "deprec": "depreciation",
    "acv": "ACV",
}


class TieOutResult:
    """The outcome of one tie-out check."""

    __slots__ = ("name", "passed", "exact", "expected", "actual", "delta", "slack", "detail")

    def __init__(
        self,
        name: str,
        passed: bool,
        expected: int,
        actual: int,
        slack: int,
        detail: str = "",
        exact: Optional[bool] = None,
    ):
        self.name = name
        self.passed = bool(passed)
        self.expected = int(expected)
        self.actual = int(actual)
        self.delta = self.actual - self.expected
        self.slack = int(slack)
        self.detail = detail
        self.exact = bool(self.delta == 0) if exact is None else bool(exact)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "exact": self.exact,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
            "slack": self.slack,
            "detail": self.detail,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<TieOutResult {self.name} passed={self.passed} exact={self.exact} "
            f"expected={self.expected} actual={self.actual} delta={self.delta}>"
        )


def _slack(line_count: int) -> int:
    return max(0, int(line_count)) * SLACK_PER_LINE_CENTS


def _item_sum(items: List[Dict], field: str) -> int:
    return sum((int(it.get(field) or 0) for it in items), ZERO)


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
    item_sum = _item_sum(parsed, "rcv")
    item_pre = _item_sum(parsed, "pretax")

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

    A printed ``Total: <area>`` line is *advisory* on our legacy parser:
    Xactimate sometimes prints the area totals out of content order, so the
    items collected for an area can belong to a sibling area.  Those checks do
    not fail the tie-out.  The schema-driven parser builds an explicit
    area->section tree and marks its printed totals ``strict``; those area
    checks are blocking.

    Schema-parsed entries carry a ``fields`` dict (tax / op / rcv / deprec /
    acv) and are checked field by field; legacy entries carry a single
    ``nums`` list and are checked on RCV.
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
        kind = t.get("kind")
        strict = bool(t.get("strict"))
        blocking = strict or kind == "Totals"
        advisory = "" if blocking else " (advisory - area totals may print out of order)"
        label = "section" if kind == "Totals" else "area"
        fields = t.get("fields")
        if fields:
            for field in ("tax", "op", "rcv", "deprec", "acv"):
                if field not in fields:
                    continue
                printed = int(fields[field])
                actual = _item_sum(parsed, field)
                results.append(
                    TieOutResult(
                        name=f"{label} '{t['name']}' {FIELD_LABELS.get(field, field)} "
                        f"(p{t.get('page', 0)})",
                        passed=abs(actual - printed) <= _slack(len(parsed)),
                        expected=printed,
                        actual=actual,
                        slack=_slack(len(parsed)),
                        detail=f"{len(parsed)} items{advisory}",
                    )
                )
            continue

        actual = _item_sum(parsed, "rcv")
        nums = [n for n in t["nums"] if n >= 0]
        if not nums:
            continue
        expected = min(nums, key=lambda p: abs(p - actual))
        results.append(
            TieOutResult(
                name=f"{label} '{t['name']}' (p{t.get('page', 0)})",
                passed=actual in t["nums"],
                expected=expected,
                actual=actual,
                slack=_slack(len(parsed)),
                detail=f"{len(parsed)} items{advisory}",
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


def _as_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def tieout_rows(estimate: Dict) -> List[TieOutResult]:
    """Check each line item's arithmetic.

    * ``qty x price + tax + O&P == RCV`` (5 cent rounding tolerance)
    * ``RCV - depreciation == ACV`` - only when the estimate actually prints
      an ACV column (``acv_printed``); the legacy parser reads ACV from a
      looser column layout, so it is not checked there.

    Revised / not-incurred rows are carried at $0 and skipped.  The checks are
    reported as one aggregate result each so a 1,900-row estimate does not
    produce 1,900 result objects.
    """
    items = estimate.get("items") or []
    bad_math: List[Tuple[int, int]] = []
    bad_acv: List[Tuple[int, int]] = []
    checked = 0
    acv_checked = 0
    for it in items:
        if not it.get("ok") or it.get("revised") or it.get("not_incurred"):
            continue
        q = _as_decimal(it.get("qty"))
        price = it.get("unit_price")
        if q is None or price is None:
            continue
        checked += 1
        calc = int((q * Decimal(int(price))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        calc += int(it.get("tax") or 0) + int(it.get("op") or 0)
        rcv = int(it.get("rcv") or 0)
        if abs(calc - rcv) > ROW_MATH_TOLERANCE_CENTS:
            bad_math.append((int(it.get("no") or 0), calc - rcv))
        if it.get("acv_printed") and it.get("acv") is not None:
            acv_checked += 1
            acv = int(it.get("acv") or 0)
            if rcv - int(it.get("deprec") or 0) != acv:
                bad_acv.append((int(it.get("no") or 0), rcv - int(it.get("deprec") or 0) - acv))

    def detail(bad: List[Tuple[int, int]]) -> str:
        return ", ".join(f"#{no} ({fmt_signed(delta)})" for no, delta in bad[:8])

    results = [
        TieOutResult(
            name="row math qty x price + tax + O&P == RCV",
            passed=not bad_math,
            expected=0,
            actual=len(bad_math),
            slack=ROW_MATH_TOLERANCE_CENTS,
            detail=f"{checked} rows checked; failures: {detail(bad_math)}" if bad_math
            else f"{checked} rows checked",
            exact=not bad_math,
        )
    ]
    if acv_checked:
        results.append(
            TieOutResult(
                name="row math RCV - depreciation == ACV",
                passed=not bad_acv,
                expected=0,
                actual=len(bad_acv),
                slack=0,
                detail=f"{acv_checked} rows checked; failures: {detail(bad_acv)}" if bad_acv
                else f"{acv_checked} rows checked",
                exact=not bad_acv,
            )
        )
    return results


def tieout_estimate(estimate: Dict) -> Dict:
    """Run every tie-out check for an estimate.

    Returns a dict with ``passed`` (bool), ``exact`` (bool), ``results`` (list
    of :class:`TieOutResult`), and a human-readable ``summary`` string.
    """
    if estimate.get("status") and estimate.get("status") != "ok":
        return {
            "passed": False,
            "exact": False,
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
    results.extend(tieout_rows(estimate))
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
        "exact": passed and all(r.exact for r in blocking),
        "skipped": False,
        "results": results,
        "advisories": [r for r in results if "advisory" in (r.detail or "")],
        "summary": "\n".join(lines) if lines else "no tie-out checks could be run",
    }


# --------------------------------------------------------------------------
# delta tie-out (old vs new)
# --------------------------------------------------------------------------
def _grand_delta(old_items: List[Dict], new_items: List[Dict], field: str) -> int:
    return _item_sum(new_items, field) - _item_sum(old_items, field)


def tieout_diff(old: Dict, new: Dict, diff: Dict) -> Dict:
    """Three-way check of an old-vs-new diff, per field.

    ``Sigma section delta == Sigma row delta == grand delta == printed(B) -
    printed(A)`` for RCV, Line Item Total, tax, O&P, depreciation and ACV.
    The printed comparison is skipped (reported as not available) when either
    side does not print a grand total - e.g. an estimate with no depreciation
    /ACV column makes those two deltas not like-for-like.

    ``diff`` is the dict returned by :func:`reconciler.diff.diff_items`; it
    supplies ``row_deltas`` and the per-section records.
    """
    old_items = old.get("items") or []
    new_items = new.get("items") or []
    n = len(old_items) + len(new_items)
    slack = _slack(n)

    sections = (diff or {}).get("sections") or {}
    row_deltas = (diff or {}).get("row_deltas") or {}

    old_printed = (old.get("checks") or {}).get("printed_grand") or {}
    new_printed = (new.get("checks") or {}).get("printed_grand") or {}

    fields: Dict[str, Dict] = {}
    results: List[TieOutResult] = []
    all_exact = True
    passed = True

    for field in DIFF_FIELDS:
        grand = _grand_delta(old_items, new_items, field)
        section_sum = sum(
            (
                int((rec or {}).get(f"new_{field}") or 0) - int((rec or {}).get(f"old_{field}") or 0)
                for rec in sections.values()
            ),
            ZERO,
        )
        row_sum = int(row_deltas.get(field, grand))
        pa = old_printed.get(field)
        pb = new_printed.get(field)
        printed = int(pb) - int(pa) if pa is not None and pb is not None else None

        sections_ok = abs(section_sum - grand) <= slack
        rows_ok = abs(row_sum - grand) <= slack
        printed_ok = None if printed is None else abs(printed - grand) <= slack
        field_passed = sections_ok and rows_ok and (printed_ok is not False)
        if not field_passed:
            passed = False
        if not (
            sections_ok
            and rows_ok
            and section_sum == grand
            and row_sum == grand
            and (printed is None or printed == grand)
        ):
            all_exact = False

        fields[field] = {
            "grand_delta": grand,
            "section_sum": section_sum,
            "row_sum": row_sum,
            "printed_delta": printed,
            "sections_ok": sections_ok,
            "rows_ok": rows_ok,
            "printed_ok": printed_ok,
        }
        label = FIELD_LABELS.get(field, field)
        results.append(
            TieOutResult(
                name=f"delta {label}: section sum == grand",
                passed=sections_ok,
                expected=grand,
                actual=section_sum,
                slack=slack,
                detail="",
            )
        )
        results.append(
            TieOutResult(
                name=f"delta {label}: row sum == grand",
                passed=rows_ok,
                expected=grand,
                actual=row_sum,
                slack=slack,
                detail="",
            )
        )
        if printed is not None:
            results.append(
                TieOutResult(
                    name=f"delta {label}: printed difference == grand",
                    passed=printed_ok is not False,
                    expected=grand,
                    actual=printed,
                    slack=slack,
                    detail="",
                )
            )

    def acv_printed_flag(est: Dict) -> Optional[bool]:
        flags = [bool(it.get("acv_printed")) for it in (est.get("items") or [])]
        if not flags:
            return None
        return all(flags)

    acv_caveat = [
        label
        for label, est in (("old", old), ("new", new))
        if acv_printed_flag(est) is False
    ]
    lines = []
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(
            f"{mark}  {r.name}: expected {fmt_cents(r.expected)} actual {fmt_cents(r.actual)} "
            f"delta {fmt_signed(r.delta)} (slack {fmt_cents(r.slack)})"
        )
    return {
        "passed": passed,
        "exact": passed and all_exact,
        "fields": fields,
        "results": results,
        "slack": slack,
        "acv_caveat": acv_caveat,
        "summary": "\n".join(lines) if lines else "no delta checks could be run",
    }


def draft_gate(old: Dict, new: Dict, diff: Dict) -> Dict:
    """Decide whether a draft may be produced.

    A failed per-estimate tie-out **or** a failed delta tie-out blocks the
    draft.  Nothing is approved here - the caller must keep a blocked run in
    Needs review.
    """
    to_old = tieout_estimate(old)
    to_new = tieout_estimate(new)
    td = tieout_diff(old, new, diff or {})
    blockers: List[str] = []
    if not to_old.get("passed"):
        blockers.append("old estimate failed tie-out")
    if not to_new.get("passed"):
        blockers.append("new estimate failed tie-out")
    if not td.get("passed"):
        blockers.append("delta tie-out failed")
    return {
        "allowed": not blockers,
        "blockers": blockers,
        "estimate_old": to_old,
        "estimate_new": to_new,
        "diff": td,
        # The tied-out RCVs - the only RCVs a draft may quote.
        "old_rcv": int((old.get("totals") or {}).get("rcv") or 0),
        "new_rcv": int((new.get("totals") or {}).get("rcv") or 0),
    }
