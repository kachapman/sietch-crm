"""Explanation layer: why the payment differs from the increase, what is new,
and how far our own estimate still is above the carrier's.

Everything here is deterministic and unit-tested.  The numbers come from the
parser (integer cents) - never from a model.  The module exists because a
revenue figure alone is not enough for the person writing the CRM note (the G1
golden example): they need to know

* what the carrier's *prior* RCV was (prior payments + deductible),
* which code / PWARR holdbacks were released in this revision,
* why the cheque is bigger than the increase over the last RCV we noted,
* which changes are buildable work and which are only O&P/tax movement,
* and how much of the gap to our own estimate is already moot because an
  approved bid superseded our line items.

Nothing in this module writes to the CRM or bills anything.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

from .money import ZERO, fmt_cents, fmt_signed, parse_money

__all__ = [
    "prior_rcv_from_payments",
    "payment_explanation",
    "holdback_releases",
    "attribute_changes",
    "op_only_changes",
    "gap_vs_our_estimate",
    "claim_change_note",
    "parse_payment_letter",
    "parse_summary_of_loss",
    "DEFAULT_SUPERSEDED_GROUPS",
    "CODE_HOLDBACK_RE",
]

# ---------------------------------------------------------------------------
# 1. prior carrier RCV = prior payments + deductible
# ---------------------------------------------------------------------------
def prior_rcv_from_payments(
    payments: Iterable[int],
    deductible: int = 0,
    printed_total: Optional[int] = None,
) -> Dict:
    """The carrier's prior RCV, rebuilt from the payment history.

    Xactimate's prior RCV is not printed on the new revision; it is the sum of
    the prior payments plus the deductible (the deductible is withheld from
    every payment but is part of RCV).  ``printed_total`` is the optional
    printed **payments** total (the Summary of Loss ``Totals:`` line under
    "Previous payments") and is only used to verify the sum of the cheques.
    """
    checks: List[int] = [int(p or 0) for p in payments]
    total = sum(checks, ZERO)
    deductible = int(deductible or 0)
    prior = total + deductible
    ties = True if printed_total is None else abs(total - int(printed_total)) <= 1
    return {
        "payments_total": total,
        "payments_count": len(checks),
        "payments": checks,
        "deductible": deductible,
        "prior_rcv": prior,
        "printed_prior_rcv": None if printed_total is None else int(printed_total),
        "ties": ties,
    }


# ---------------------------------------------------------------------------
# 2. why the payment differs from the RCV increase
# ---------------------------------------------------------------------------
def payment_explanation(
    new_rcv: int,
    payment: int,
    *,
    prior_payments: int = 0,
    deductible: int = 0,
    already_noted_rcv: Optional[int] = None,
    releases: Iterable[Dict] = (),
    printed_prior_rcv: Optional[int] = None,
) -> Dict:
    """Explain the cheque: released holdbacks, deductible, already-noted RCV.

    The G1 shape (all integer cents)::

        new RCV                 95,508,988
        prior payments          84,063,892
        deductible               1,000,000
        prior RCV               85,063,892   = payments + deductible
        cheque                  10,445,096   = new RCV - prior RCV
        PWARR released           5,706,599
        already-noted RCV       90,770,491   = prior RCV + released
        additional increase      4,738,497   = cheque - released

    Returns the numbers, named checks and human-readable summary lines.
    """
    new_rcv = int(new_rcv or 0)
    payment = int(payment or 0)
    prior = prior_rcv_from_payments([prior_payments], deductible, printed_prior_rcv)
    prior_rcv = prior["prior_rcv"]

    stated_increase = new_rcv - prior_rcv
    released_total = sum(int((r or {}).get("rcv") or (r or {}).get("amount") or 0) for r in releases)
    if already_noted_rcv is None:
        already_noted_rcv = prior_rcv + released_total
    already_noted_rcv = int(already_noted_rcv)
    additional = new_rcv - already_noted_rcv
    payment_less_releases = payment - released_total

    checks = [
        {
            "name": "cheque == new RCV - prior RCV",
            "ok": abs(payment - stated_increase) <= 1,
            "expected": stated_increase,
            "actual": payment,
        },
        {
            "name": "already-noted RCV == prior RCV + released holdbacks",
            "ok": abs(already_noted_rcv - (prior_rcv + released_total)) <= 1,
            "expected": prior_rcv + released_total,
            "actual": already_noted_rcv,
        },
        {
            "name": "cheque - released == additional increase",
            "ok": abs(payment_less_releases - additional) <= 1,
            "expected": additional,
            "actual": payment_less_releases,
        },
    ]
    ties = all(c["ok"] for c in checks) and prior["ties"]

    lines = [
        f"Carrier's prior RCV: {fmt_cents(prior_rcv)} "
        f"({fmt_cents(prior['payments_total'])} in prior payments + {fmt_cents(deductible)} deductible).",
        f"Cheque {fmt_cents(payment)} = new RCV {fmt_cents(new_rcv)} - prior RCV {fmt_cents(prior_rcv)}.",
    ]
    if released_total:
        lines.append(
            f"{fmt_cents(released_total)} of the cheque is a released holdback; "
            f"that amount was already counted in the {fmt_cents(already_noted_rcv)} RCV already noted."
        )
    lines.append(
        f"Additional increase since the {fmt_cents(already_noted_rcv)} RCV: {fmt_cents(additional)} "
        f"({fmt_cents(payment)} cheque - {fmt_cents(released_total)} released)."
    )
    return {
        "new_rcv": new_rcv,
        "payment": payment,
        "prior_payments": int(prior["payments_total"]),
        "deductible": int(deductible or 0),
        "prior_rcv": prior_rcv,
        "stated_increase": stated_increase,
        "released_total": released_total,
        "already_noted_rcv": already_noted_rcv,
        "additional_increase": additional,
        "payment_less_releases": payment_less_releases,
        "checks": checks,
        "ties": ties,
        "summary": lines,
    }


# ---------------------------------------------------------------------------
# 3. PWARR / code holdback releases
# ---------------------------------------------------------------------------
# Xactimate prints a holdback note next to the line: "Paid when applied to roof
# surfaces in amount of $57,065.99." (PWARR = paid when actually repaired or
# replaced).  The amount in the note is the release for that line.
HOLDBACK_NOTE_RE = re.compile(
    r"(?:paid\s+when|p\.?w\.?a\.?r\.?r|pwarr)[^.]*?"
    r"(?:amount\s+of\s+)?\$?\s*([\d,]+\.\d{2})",
    re.I,
)
CODE_HOLDBACK_RE = re.compile(
    r"\bISO\s+board\b|ordinance|building\s+law|\bcode\b|ice\s*&\s*water|"
    r"increased\s+cost|ICC|paid\s+when",
    re.I,
)


def _item_text(it: Dict) -> str:
    notes = it.get("notes") or []
    return " ".join([str(it.get("desc") or "")] + [str(n or "") for n in notes])


def holdback_releases(
    new_items: Sequence[Dict],
    old_items: Optional[Sequence[Dict]] = None,
) -> Dict:
    """Holdback / PWARR lines in the new revision, and what was released.

    A holdback line is one whose own text says it is paid when the work is
    actually applied/repaired/replaced, or a code/ordinance upgrade line.

    * with ``old_items``: an item is *released* when it was absent or carried
      $0 in the old revision and carries money now (the certain case);
    * without the prior estimate: only a line whose note amount equals its own
      printed RCV is counted as a **confirmed** release - the note and the line
      agree, so the money is demonstrably in this revision.  A note amount that
      disagrees with the line (G1's Worker Comp ISO 4" line: note $12,695.73 vs
      line $119,747.88) is reported as a mismatch, never guessed.

    ``confirmed_total`` is the number a note may quote; ``conditional_total``
    is every holdback line's RCV and is only for the reviewer.
    """
    old_by_desc: Dict[str, List[Dict]] = {}
    for it in old_items or []:
        key = str(it.get("desc") or "").strip().lower()
        old_by_desc.setdefault(key, []).append(it)

    def _find_old(it: Dict) -> Optional[Dict]:
        """Match the same line in the prior estimate.

        Descriptions are not unique (the same ISO board line appears under
        both the Worker Comp and Apartment Roof sections), so the quantity and
        then the unit disambiguate; falling back to the first same-description
        row would compare two different sections and hide a release.
        """
        cands = old_by_desc.get(str(it.get("desc") or "").strip().lower()) or []
        if not cands:
            return None
        qty = str(it.get("qty"))
        for c in cands:
            if str(c.get("qty")) == qty and c.get("unit") == it.get("unit"):
                return c
        for c in cands:
            if str(c.get("qty")) == qty:
                return c
        return cands[0]

    rows: List[Dict] = []
    mismatches: List[Dict] = []
    for it in new_items or []:
        text = _item_text(it)
        m = HOLDBACK_NOTE_RE.search(text)
        code = bool(CODE_HOLDBACK_RE.search(text))
        if not m and not code:
            continue
        note_amount = parse_money(m.group(1)) if m else None
        line_rcv = int(it.get("rcv") or 0)
        old = _find_old(it)
        if old_items is not None:
            released = bool(line_rcv) and (old is None or not int(old.get("rcv") or 0))
            basis = "absent-or-zero in the prior estimate"
        else:
            released = bool(note_amount is not None and abs(note_amount - line_rcv) <= 1)
            basis = "note amount equals the line RCV"
        consistent = note_amount is None or abs(note_amount - line_rcv) <= 1
        row = {
            "no": it.get("no"),
            "description": it.get("desc") or "",
            "section": it.get("section") or "",
            "qty": it.get("qty"),
            "unit": it.get("unit"),
            "rcv": line_rcv,
            "note_amount": note_amount,
            "note_consistent": consistent,
            "code_upgrade": code,
            "released": released,
            "release_basis": basis,
        }
        rows.append(row)
        if not consistent:
            mismatches.append(row)

    confirmed = sum(r["rcv"] for r in rows if r["released"])
    conditional = sum(r["rcv"] for r in rows)
    return {
        "items": rows,
        "confirmed_total": confirmed,
        "conditional_total": conditional,
        "mismatches": mismatches,
        "needs_prior_estimate": old_items is None,
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# 4. per-item buildable attribution + O&P-only noise
# ---------------------------------------------------------------------------
_BUILDABLE_CONDITIONAL_RE = re.compile(
    r"paid\s+when|ordinance|building\s+law|\bcode\b|increased\s+cost|ICC|ISO\s+board",
    re.I,
)
_BID_RE = re.compile(r"bid\s+item|will\s+submit|per\s+.{0,20}\bbid\b|approved\s+bid", re.I)


def _row_delta(a_items: Sequence[Dict], b_items: Sequence[Dict], field: str = "rcv") -> int:
    return sum(int(x.get(field) or 0) for x in b_items) - sum(
        int(x.get(field) or 0) for x in a_items
    )


def _row_op_only(a_items: Sequence[Dict], b_items: Sequence[Dict]) -> bool:
    """True when the row's only movement is O&P/tax (pretax identical, non-zero)."""
    if not a_items or not b_items:
        return False
    pre = _row_delta(a_items, b_items, "pretax")
    op = _row_delta(a_items, b_items, "op")
    tax = _row_delta(a_items, b_items, "tax")
    rcv = _row_delta(a_items, b_items, "rcv")
    return pre == 0 and (op or tax) and rcv != 0


def op_only_changes(diff: Dict) -> List[Dict]:
    """Matched rows whose only change is O&P/tax, collapsed into one group.

    A carrier that adds general-contractor O&P to every line changes every row's
    RCV, but that is one commercial change, not N scope changes.  Returning it
    as a single row keeps the note honest and readable.
    """
    rows: List[Dict] = []
    for m in (diff or {}).get("matches") or []:
        a, b = m.get("a") or [], m.get("b") or []
        if not _row_op_only(a, b):
            continue
        rows.append(
            {
                "kind": "op_only",
                "line_count": 1,
                "description": (b or a)[0].get("desc") or "",
                "section": (b or a)[0].get("section") or "",
                "op_delta": _row_delta(a, b, "op"),
                "tax_delta": _row_delta(a, b, "tax"),
                "rcv_delta": _row_delta(a, b, "rcv"),
                "what_changed": list(m.get("what_changed") or []),
            }
        )
    if not rows:
        return []
    return [
        {
            "kind": "op_only_group",
            "line_count": len(rows),
            "description": f"O&P / tax only on {len(rows)} matched line(s)",
            "op_delta": sum(r["op_delta"] for r in rows),
            "tax_delta": sum(r["tax_delta"] for r in rows),
            "rcv_delta": sum(r["rcv_delta"] for r in rows),
            "lines": rows,
        }
    ]


def attribute_changes(diff: Optional[Dict] = None) -> Dict:
    """Label every change buildable / non-buildable / conditional.

    Rules (deterministic):

    * O&P-only or tax-only movement -> **non-buildable** ("overhead movement,
      not scope") and collapsed into one group by :func:`op_only_changes`;
    * a ``REVISED`` / ``SEE REVISION`` row -> **not a removal**: the value moved
      to the revision line, so the row is listed as ``revised`` with no
      buildable verdict and excluded from the buildable total;
    * code/ordinance/PWARR lines -> **buildable, conditional** (payable only as
      the code work is actually done);
    * an approved-bid line -> **buildable** but flagged ``bid`` (the bid sets
      the price);
    * plain added/changed -> **buildable**; removed -> **not buildable**.
    """
    diff = diff or {}
    rows: List[Dict] = []

    def add(kind: str, a: Sequence[Dict], b: Sequence[Dict], how: str = "") -> None:
        ref = (b or a or [{}])[0]
        text = " ".join(str(x.get("desc") or "") for x in list(a) + list(b))
        delta = _row_delta(a, b)
        revised = any(x.get("revised") for x in list(a) + list(b))
        buildable: Optional[bool]
        reason: str
        if revised:
            buildable = None
            reason = "REVISED line - value moved to the revision line, not a real removal"
            kind = "revised"
        elif _row_op_only(a, b):
            buildable = False
            reason = "O&P/tax only - overhead movement, not scope"
            kind = "op_only"
        elif kind == "removed":
            buildable = False
            reason = "removed from the revision"
        elif _BUILDABLE_CONDITIONAL_RE.search(text):
            buildable = True
            reason = "buildable, conditional - code/holdback work paid when performed"
        elif _BID_RE.search(text):
            buildable = True
            reason = "buildable - priced by the approved/included bid"
        else:
            buildable = True
            reason = "buildable work"
        rows.append(
            {
                "kind": kind,
                "description": ref.get("desc") or "",
                "section": ref.get("section") or "",
                "delta": delta,
                "op_delta": _row_delta(a, b, "op"),
                "tax_delta": _row_delta(a, b, "tax"),
                "buildable": buildable,
                "reason": reason,
                "how": how,
                "revised": revised,
                "bid": bool(_BID_RE.search(text)),
            }
        )

    for m in diff.get("matches") or []:
        a, b = m.get("a") or [], m.get("b") or []
        if _row_delta(a, b) == 0 and not any(x.get("revised") for x in list(a) + list(b)):
            continue
        add("changed", a, b, m.get("how") or "")
    for it in diff.get("added") or []:
        add("added", [], [it], "only in new")
    for it in diff.get("removed") or []:
        add("removed", [it], [], "only in old")

    rows.sort(key=lambda r: -abs(int(r["delta"])))
    buildable_total = sum(r["delta"] for r in rows if r["buildable"] is True)
    conditional_total = sum(
        r["delta"] for r in rows if r["buildable"] is True and "conditional" in r["reason"]
    )
    non_buildable_total = sum(r["delta"] for r in rows if r["buildable"] is False)
    return {
        "rows": rows,
        "buildable_total": buildable_total,
        "conditional_total": conditional_total,
        "non_buildable_total": non_buildable_total,
        "revised_rows": [r for r in rows if r["revised"]],
        "op_only_groups": op_only_changes(diff),
    }


# ---------------------------------------------------------------------------
# 5. gap vs our estimate, excluding bid-superseded scope
# ---------------------------------------------------------------------------
DEFAULT_SUPERSEDED_GROUPS: List[Dict] = [
    {
        "name": "HVAC",
        # A named section is the precise signal (our R1 prints "HVAC"); the
        # description patterns are the fallback when a side has no section
        # labels.  Using both at once over-matches: HVAC equipment is also
        # named on roof/repair lines.
        "sections": ["hvac"],
        "patterns": [
            r"air\s*condition",
            r"\bhvac\w*",
            r"condens",
            r"\bcoil\b",
            r"refrigerant",
            r"\bduct\b",
            r"air\s+handler",
        ],
    },
]


def _matches(patterns: Sequence[str], text: str) -> bool:
    return any(re.search(p, text or "", re.I) for p in patterns)


def _group_amount(items: Sequence[Dict], group: Dict) -> int:
    """Total for one superseded group: section labels first, patterns second."""
    sections = [str(s).strip().lower() for s in group.get("sections") or [] if s]
    if sections:
        total = 0
        matched = False
        for it in items:
            sec = str(it.get("section") or "").strip().lower()
            if sec and any(sec == s or s in sec for s in sections):
                total += int(it.get("rcv") or 0)
                matched = True
        if matched:
            return total
    patterns = group.get("patterns") or []
    return sum(
        int(it.get("rcv") or 0)
        for it in items
        if _matches(patterns, str(it.get("desc") or ""))
    )


def gap_vs_our_estimate(
    our: Dict,
    carrier: Dict,
    groups: Optional[Sequence[Dict]] = None,
) -> Dict:
    """Gap between our latest estimate and the carrier RCV, by superseded group.

    ``our``/``carrier`` are parsed estimate dicts.  The total gap is simply the
    tied-out RCV difference.  A group (default: HVAC) is *superseded* when an
    approved bid replaced our line items: our spend on that scope is moot, so
    the realistic remaining target is the gap minus the group's excess.

    G1: our R1 $1,038,450.29 - SF $955,089.88 = $83,360.41; HVAC is
    $56,511.55 of it (our $165,769.85 vs SF $109,258.30), leaving ~$26,848.86.
    """
    our_rcv = int((our.get("totals") or {}).get("rcv") or 0)
    carrier_rcv = int((carrier.get("totals") or {}).get("rcv") or 0)
    gap = our_rcv - carrier_rcv
    out_groups: List[Dict] = []
    for g in groups if groups is not None else DEFAULT_SUPERSEDED_GROUPS:
        our_amt = _group_amount(our.get("items") or [], g)
        carrier_amt = _group_amount(carrier.get("items") or [], g)
        excluded = max(0, our_amt - carrier_amt)
        out_groups.append(
            {
                "name": g.get("name") or "group",
                "our_total": our_amt,
                "carrier_total": carrier_amt,
                "excluded": excluded,
            }
        )
    excluded_total = sum(g["excluded"] for g in out_groups)
    return {
        "our_rcv": our_rcv,
        "carrier_rcv": carrier_rcv,
        "gap": gap,
        "groups": out_groups,
        "excluded_total": excluded_total,
        "realistic_gap": gap - excluded_total,
    }


# ---------------------------------------------------------------------------
# 6. the note itself (deterministic, G1-shaped)
# ---------------------------------------------------------------------------
def claim_change_note(
    new_rcv: int,
    *,
    original_rcv: Optional[int] = None,
    already_noted_rcv: Optional[int] = None,
    already_noted_date: Optional[str] = None,
    payment: Optional[int] = None,
    prior_payments: int = 0,
    deductible: int = 0,
    releases: Iterable[Dict] = (),
    gap: Optional[Dict] = None,
    our_label: str = "our latest estimate",
    confirmed_new_items: Optional[Sequence[Dict]] = None,
    carrier_label: str = "the carrier",
    exclude_personal_property: bool = True,
) -> Dict:
    """Compose the G1-shaped explanation note from the deterministic pieces.

    Every number is supplied by the parser/explain layer - this function only
    orders sentences.  When the carrier's **prior estimate** is missing, the
    split of the remaining change is explicitly left unconfirmed; nothing is
    invented.

    Returns ``{"lines": [...], "unconfirmed": bool, "numbers": {...}}``.
    """
    new_rcv = int(new_rcv or 0)
    no_pp = ", no PP" if exclude_personal_property else ""
    lines: List[str] = []
    unconfirmed = False

    if original_rcv is not None:
        original_rcv = int(original_rcv)
        lines.append(
            f"The claim increased by {fmt_cents(new_rcv - original_rcv)}, with RCV going from "
            f"{fmt_cents(original_rcv)} to {fmt_cents(new_rcv)}{no_pp}."
        )
    else:
        lines.append(
            f"New RCV is {fmt_cents(new_rcv)}{no_pp}. The increase from the original estimate is "
            f"not stated here because the original RCV was not supplied."
        )

    additional = None
    if already_noted_rcv is not None:
        already_noted_rcv = int(already_noted_rcv)
        additional = new_rcv - already_noted_rcv
        since = f" on {already_noted_date}" if already_noted_date else ""
        lines.append(
            f"That's an additional {fmt_cents(additional)} since the "
            f"{fmt_cents(already_noted_rcv)} RCV{since}."
        )

    if payment is not None:
        expl = payment_explanation(
            new_rcv,
            int(payment),
            prior_payments=prior_payments,
            deductible=deductible,
            already_noted_rcv=already_noted_rcv,
            releases=releases,
        )
        # the cheque/prior-RCV sentences; the additional-increase sentence is
        # already stated above, so it is not repeated
        lines.extend(
            ln for ln in expl["summary"] if not ln.startswith("Additional increase since")
        )
        if additional is not None and abs(expl["additional_increase"] - additional) > 1:
            lines.append(
                "Check: the cheque breakdown gives an additional increase of "
                f"{fmt_cents(expl['additional_increase'])}, which does not match "
                f"{fmt_cents(additional)} - review before posting."
            )
            unconfirmed = True

    released = sum(int((r or {}).get("rcv") or 0) for r in releases or [])
    if released and payment is None:
        lines.append(
            f"{fmt_cents(released)} of the payment is a PWARR/code holdback being released, "
            f"not new scope."
        )

    if confirmed_new_items:
        parts = [
            f"{str(it.get('description') or '')} {fmt_signed(int(it.get('delta') or 0))}"
            for it in confirmed_new_items
        ]
        lines.append(f"What's confirmed new: {'; '.join(parts)}.")
    else:
        unconfirmed = True
        lines.append(
            f"What is new cannot be split line by line without {carrier_label}'s prior estimate; "
            f"the holdbacks above are the only confirmed items."
        )

    if gap:
        gap_amt = int(gap.get("gap") or 0)
        relation = "under" if gap_amt > 0 else "over"
        line = (
            f"{our_label} is {fmt_cents(abs(gap_amt))} {relation} the carrier RCV "
            f"({fmt_cents(int(gap.get('our_rcv') or 0))} vs {fmt_cents(int(gap.get('carrier_rcv') or 0))})."
        )
        excluded = int(gap.get("excluded_total") or 0)
        if excluded:
            names = ", ".join(g.get("name") or "group" for g in gap.get("groups") or [])
            line += (
                f" {fmt_cents(excluded)} of it is bid-superseded scope ({names}), so the realistic "
                f"remaining target is {fmt_cents(int(gap.get('realistic_gap') or 0))}."
            )
        lines.append(line)

    return {
        "lines": lines,
        "unconfirmed": unconfirmed,
        "numbers": {
            "new_rcv": new_rcv,
            "original_rcv": None if original_rcv is None else int(original_rcv),
            "already_noted_rcv": None if already_noted_rcv is None else int(already_noted_rcv),
            "additional_increase": additional,
            "released": released,
            "gap": None if gap is None else int(gap.get("gap") or 0),
            "realistic_gap": None if gap is None else int(gap.get("realistic_gap") or 0),
        },
    }


# ---------------------------------------------------------------------------
# 7. payment letter / summary of loss (text in, numbers out)
# ---------------------------------------------------------------------------
_MONEY = r"\$?\s*([\d,]+\.\d{2})"


def _find_money(text: str, pattern: str) -> Optional[int]:
    m = re.search(pattern, text or "", re.I | re.S)
    return parse_money(m.group(1)) if m else None


def parse_payment_letter(text: str) -> Dict:
    """Read the payment letter: cheque amount, claim number, date of loss.

    Works on a text layer or on OCR output - the letter is short and its
    wording is stable ("Our payment in the amount of $104,450.96 in settlement
    of your Dwelling claim").
    """
    text = text or ""
    amount = _find_money(
        text,
        r"payment\s+in\s+the\s+amount\s+of\s+" + _MONEY,
    ) or _find_money(text, r"payment\s+amount[^\d]{0,20}" + _MONEY)
    claim = None
    m = re.search(r"Claim\s+Number[:\s]+([A-Z0-9][A-Z0-9-]{4,})", text, re.I)
    if m:
        claim = m.group(1)
    dol = None
    m = re.search(r"Date\s+of\s+Loss[:\s]+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", text)
    if m:
        dol = m.group(1)
    return {
        "payment": amount,
        "claim_number": claim,
        "date_of_loss": dol,
        "ok": amount is not None,
    }


def parse_summary_of_loss(text: str) -> Dict:
    """Read the Summary of Loss: new RCV, deductible, prior payments, cheque.

    Labels vary a little between carriers but the Xactimate Summary of Loss is
    consistent: the Comments/Supplements **Subtotal** is the new RCV,
    ``Less deductible``, ``Less total payments made`` (or ``Less Prior
    Payments``) and ``Net payment``.  The individual payment rows are returned
    too, so the prior RCV can be rebuilt from the cheques that actually went
    out (11 of them on G1).
    """
    text = text or ""
    rcv = _find_money(text, r"Replacement\s+Cost\s+Value[^\d\-]{0,60}" + _MONEY)
    if rcv is None:
        rcv = _find_money(
            text, r"Comments\s*/\s*Supplements[^\n]{0,80}?Subtotal[^\d\-]{0,30}" + _MONEY
        )
    if rcv is None:
        rcv = _find_money(text, r"^\s*Subtotal[^\d\-]{0,20}" + _MONEY)
    deductible = _find_money(text, r"Less\s+Deductible[^\d\-]{0,40}" + _MONEY)
    prior = _find_money(text, r"Less\s+total\s+payments?\s+made[^\d\-]{0,40}" + _MONEY)
    if prior is None:
        prior = _find_money(text, r"(?:Less\s+)?Prior\s+Payments?[^\d\-]{0,40}" + _MONEY)
    net = _find_money(text, r"Net\s+Payment[^\d\-]{0,40}" + _MONEY)

    payments: List[int] = []
    month = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    for m in re.finditer(
        rf"(?m)^\s*{month}\s+\d{{1,2}},\s+\d{{4}}\b[^\n]*?([\d,]+\.\d{{2}})\s*$", text
    ):
        payments.append(parse_money(m.group(1)))
    payments_total = sum(payments, ZERO) if payments else None
    payments_totals_printed = _find_money(
        text, r"Previous\s+payments[\s\S]{0,4000}?Totals:[^\d\-]{0,20}" + _MONEY
    )
    return {
        "rcv": rcv,
        "deductible": deductible,
        "prior_payments": prior,
        "net_payment": net,
        "payments": payments,
        "payments_total": payments_total,
        "payments_totals_printed": payments_totals_printed,
        "ok": rcv is not None or net is not None,
    }
