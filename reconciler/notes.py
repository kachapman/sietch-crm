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

import re
from typing import Dict, Iterable, List, Optional

from .money import ZERO, fmt_cents, fmt_signed, parse_money

__all__ = [
    "claim_increase_note",
    "billing_summary_lines",
    "note_text",
    "draft_note",
    "change_drivers",
    "change_driver_lines",
    "change_summary_line",
    "explanation_numbers_are_consistent",
    "model_why_layer",
    "WHY_LAYER_ENABLED",
    "NO_PP_MARKER",
    "DRIVER_TOP_N",
]

NO_PP_MARKER = "no PP"

# How many change drivers the note lists (top by |RCV change|).
DRIVER_TOP_N = 5


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


# ---------------------------------------------------------------------------
# change drivers (top-N)
# ---------------------------------------------------------------------------
_MONEY_TOKEN_RE = re.compile(r"\(?-?\$?\d[\d,]*\.\d{2}\)?")


def _rcv(it: Dict) -> int:
    return int(it.get("rcv") or 0)


def _desc(items: List[Dict]) -> str:
    return " + ".join(str(it.get("desc") or "") for it in items if it.get("desc"))


def change_drivers(diff: Dict, limit: int = DRIVER_TOP_N) -> List[Dict]:
    """The largest changes in a diff, biggest |RCV| first.

    Every number and label here comes from the diff rows (code), never from a
    model.  Used by the note's "main changes" lines.
    """
    diff = diff or {}
    rows: List[Dict] = []
    for m in diff.get("matches") or []:
        a_items = m.get("a") or []
        b_items = m.get("b") or []
        delta = sum(_rcv(x) for x in b_items) - sum(_rcv(x) for x in a_items)
        if delta == 0:
            continue
        ref = (b_items or a_items)[0]
        rows.append(
            {
                "kind": "changed" if a_items and b_items else ("added" if b_items else "removed"),
                "description": _desc(b_items) or _desc(a_items),
                "section": ref.get("section_label") or ref.get("section") or "",
                "qty": ref.get("qty"),
                "unit": ref.get("unit"),
                "delta": delta,
                "how": m.get("how"),
                "sim": m.get("sim"),
                "exact": bool(m.get("exact")),
            }
        )
    for it in diff.get("added") or []:
        rows.append(
            {
                "kind": "added",
                "description": str(it.get("desc") or ""),
                "section": it.get("section_label") or it.get("section") or "",
                "qty": it.get("qty"),
                "unit": it.get("unit"),
                "delta": _rcv(it),
                "how": "only in new",
                "sim": None,
                "exact": False,
            }
        )
    for it in diff.get("removed") or []:
        rows.append(
            {
                "kind": "removed",
                "description": str(it.get("desc") or ""),
                "section": it.get("section_label") or it.get("section") or "",
                "qty": it.get("qty"),
                "unit": it.get("unit"),
                "delta": -_rcv(it),
                "how": "only in old",
                "sim": None,
                "exact": False,
            }
        )
    rows.sort(key=lambda r: (-abs(int(r["delta"])), str(r["description"])))
    return rows[: max(0, int(limit))]


def _qty_text(row: Dict) -> str:
    qty = row.get("qty")
    unit = row.get("unit")
    if qty in (None, ""):
        return ""
    q = str(qty)
    if q.endswith(".00"):
        q = q[:-3]
    return f" {q} {unit}".rstrip() if unit else f" {q}"


def change_driver_lines(diff: Dict, limit: int = DRIVER_TOP_N) -> List[str]:
    """Top-N change drivers as note lines (deterministic template)."""
    lines: List[str] = []
    for row in change_drivers(diff, limit):
        desc = row["description"].strip()
        if len(desc) > 90:
            desc = desc[:87].rstrip() + "..."
        delta = fmt_signed(int(row["delta"]))
        kind = row["kind"]
        if kind == "added":
            lines.append(f"Added: {desc}{_qty_text(row)}, {delta}")
        elif kind == "removed":
            lines.append(f"Removed: {desc}{_qty_text(row)}, {delta}")
        else:
            lines.append(f"Changed: {desc}{_qty_text(row)}, {delta}")
    return lines


def change_summary_line(diff: Dict) -> str:
    """One line of counts, straight from :func:`reconciler.diff.diff_summary`."""
    from .diff import diff_summary

    s = diff_summary(diff or {})
    if not s:
        return ""
    parts = [
        f"{s.get('unchanged_count', 0)} unchanged",
        f"{s.get('changed_count', 0)} changed",
        f"{s.get('moved_count', 0)} moved",
        f"{s.get('added_count', 0)} added",
        f"{s.get('removed_count', 0)} removed",
    ]
    return "Items: " + ", ".join(parts) + "."


def draft_note(
    old_rcv: int,
    new_rcv: int,
    diff: Optional[Dict] = None,
    already_noted: int = 0,
    exclude_personal_property: bool = True,
    driver_limit: int = DRIVER_TOP_N,
) -> str:
    """The claim-increase note plus optional code-generated change drivers.

    The first line is always our agreed format
    ``Claim increased by $X ($orig to $new, no PP)``.  Driver lines are added
    only from ``diff`` and are plain templates - no model is involved.
    """
    extra: List[str] = []
    if diff:
        summary = change_summary_line(diff)
        if summary:
            extra.append(summary)
        extra.extend(change_driver_lines(diff, driver_limit))
    return claim_increase_note(
        old_rcv,
        new_rcv,
        already_noted=already_noted,
        exclude_personal_property=exclude_personal_property,
        extra_lines=extra,
    )


# ---------------------------------------------------------------------------
# FUTURE (DISABLED): model-written "why" layer
#
# The reconciler is deterministic on purpose: numbers, totals and the
# added/removed/changed lists come only from code (see above).  A later phase
# may add a small local model that writes *explanations only* ("why did the
# roofer add decking?") - never a number.  It is off by default, no model
# client is imported or called anywhere in this package, and every sentence it
# returns must pass ``explanation_numbers_are_consistent`` against the
# code-generated numbers before it is shown.
# ---------------------------------------------------------------------------
WHY_LAYER_ENABLED = False


def explanation_numbers_are_consistent(
    sentences: Iterable[str], allowed_numbers: Iterable[int]
) -> bool:
    """True when every money amount in ``sentences`` appears in the code numbers.

    ``allowed_numbers`` is a collection of integer cents (the totals, deltas
    and driver amounts the code produced).  A sentence with any other amount
    is rejected - the future "why" layer may explain, never invent.
    """
    allowed = {int(x) for x in allowed_numbers}
    for sentence in sentences or []:
        for token in _MONEY_TOKEN_RE.findall(str(sentence)):
            if parse_money(token) not in allowed:
                return False
    return True


def model_why_layer(
    diff: Optional[Dict] = None,
    sentences: Optional[Iterable[str]] = None,
    *,
    enabled: Optional[bool] = None,
) -> Dict:
    """DISABLED hook for a future model-written explanation layer.

    Returns ``{"enabled": False, "sentences": []}`` while disabled.  No model
    call is implemented; this function is intentionally a placeholder so a
    future phase has one obvious integration point.
    """
    active = WHY_LAYER_ENABLED if enabled is None else bool(enabled)
    if not active:
        return {"enabled": False, "sentences": []}
    raise RuntimeError(
        "model_why_layer is a disabled placeholder: implement a local model and "
        "validate every sentence with explanation_numbers_are_consistent first"
    )
