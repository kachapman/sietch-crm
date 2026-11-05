"""Carrier Xactimate estimate parser (port of the old ``recon.py``).

Parses the ``pdftotext -layout`` text of a carrier estimate into:

* ``meta``          - insured, claim number, carrier, estimate date, revision
* ``coverage_table`` - the "Coverage Item Total / % / ACV Total" table
* ``summaries``     - per-coverage "Summary for ..." blocks (LIT, tax, O&P,
                      RCV, depreciation, ACV, deductible, net claim)
* ``items``         - line items (number, section/area, description, qty,
                      unit, unit price, tax, O&P, RCV, depreciation, ACV)
* ``totals``        - the billable totals (personal property excluded)
* ``warnings``      - the "PARSE WARNINGS" self-checks from recon.py

All money is integer cents.  Nothing is guessed silently: anything that cannot
be parsed is reported through ``warnings`` (exactly as recon.py did).

The parsing logic follows recon.py closely so the numbers reproduce; the
arithmetic was moved from ``Decimal`` to integer cents.
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from .money import ZERO, fmt_cents, fmt_signed, parse_money, to_cents

__all__ = [
    "parse_estimate",
    "parse_text",
    "parse_meta",
    "parse_coverage_table",
    "parse_summaries",
    "parse_items",
    "parse_items_schema",
    "header_schema",
    "ocr_fix",
    "looks_ocr",
    "find_flags",
    "pages_of",
    "split_revisions",
    "CARRIERS",
    "GUIDE_MARKERS",
]

# --------------------------------------------------------------------------
# carriers
# --------------------------------------------------------------------------
CARRIERS = [
    "Allstate",
    "State Farm",
    "Travelers",
    "Farmers",
    "American Family",
    "Erie",
    "Liberty Mutual",
    "USAA",
    "Nationwide",
    "Progressive",
    "Safeco",
    "Auto-Owners",
    "Westfield",
    "Country Financial",
    "Citizens",
    "Universal",
    "Heritage",
    "Chubb",
    "Hanover",
    "Frankenmuth",
    "Pekin",
    "West Bend",
    "Secura",
    "Encompass",
    "Homesite",
    "Kemper",
    "Grange",
    "Cincinnati",
    "MetLife",
    "Foremost",
]

# The carriers this project recognises.  Kept in scope deliberately
# (Auto-Owners and Universal are already verified by the old parser).
SUPPORTED_CARRIERS = [
    "Allstate",
    "State Farm",
    "Travelers",
    "Auto-Owners",
    "Farmers",
    "Universal",
]

# --------------------------------------------------------------------------
# text / pages
# --------------------------------------------------------------------------
GUIDE_MARKERS = re.compile(
    r"Summary Guide|GUIDE_EXAMPLE|sample estimate and is provided|Your Estimate Summary|"
    r"Understanding Your (Property )?Estimate|How to read your estimate",
    re.I,
)


def pages_of(text: str) -> Tuple[List[Tuple[int, str]], List[int]]:
    """Split text into pages, dropping carrier guide/sample pages.

    Returns ``(kept_pages, skipped_page_numbers)`` where kept_pages is a list
    of ``(page_number, page_text)``.
    """
    pages = text.split("\f")
    keep: List[Tuple[int, str]] = []
    skipped: List[int] = []
    for i, p in enumerate(pages, 1):
        if GUIDE_MARKERS.search(p):
            skipped.append(i)
        else:
            keep.append((i, p))
    return keep, skipped


# --------------------------------------------------------------------------
# OCR repair (gated - never touches clean text)
# --------------------------------------------------------------------------
# A real text layer that came from OCR carries distinctive damage: ``1 .00``
# for a decimal point, ``LOO`` / ``I.00`` / ``l.OO`` for ``1.00``.  Clean
# Xactimate text never contains the ``LOO`` forms.  ``ocr_fix`` is applied by
# ``parse_text`` only when ``looks_ocr`` says the document has those markers,
# so a clean estimate is byte-for-byte unchanged.  (The benign ``1 .00``
# column layout is *not* used as the trigger - it occurs in clean carrier
# exports; the row-layout regexes tolerate the space instead.)
_OCR_DAMAGE_RE = re.compile(r"(?<![\w.])(?:LOO|l\.OO|1\.OO|I\.00|l\.00)(?=\s+[A-Z]{1,3}\b)")
_OCR_DECIMAL_RE = re.compile(r"(?<=\d) \.(?=\d{2}(?!\d))")
_OCR_ONE_RE = re.compile(r"(?<![\w.])(?:LOO|l\.OO|1\.OO|I\.00|l\.00)(?=\s+[A-Z]{1,3}\b)")


def looks_ocr(text: str) -> bool:
    """True when the text layer carries OCR-damage markers (``LOO`` etc.).

    Deliberately conservative: the ``1 .00`` layout is a normal column
    artefact and is not a trigger.  A document that only has that stays
    untouched (the schema row regexes accept it).
    """
    return bool(text) and bool(_OCR_DAMAGE_RE.search(text))


def ocr_fix(text: str, enabled: Optional[bool] = None) -> str:
    """Repair common OCR damage in a text layer.

    Only runs when ``enabled`` is true (default: :func:`looks_ocr` on the same
    text), and even then only rewrites lines that actually carry damage.  A
    clean fixture is returned unchanged.
    """
    if enabled is None:
        enabled = looks_ocr(text)
    if not enabled:
        return text
    out: List[str] = []
    for line in text.split("\n"):
        if _OCR_ONE_RE.search(line) or _OCR_DECIMAL_RE.search(line):
            line = _OCR_DECIMAL_RE.sub(".", line)
            line = _OCR_ONE_RE.sub("1.00", line)
        out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------
def parse_meta(text: str) -> Dict[str, Optional[str]]:
    """Extract insured / claim number / carrier / estimate date."""
    head = text[:15000]
    meta: Dict[str, Optional[str]] = {}
    m = re.search(r"(?:Insured|Customer):\s+(.+?)(?:\s{2,}|$)", head, re.M)
    if m:
        meta["insured"] = m.group(1).strip()
    else:
        meta["insured"] = None
    m = re.search(r"Claim (?:Number|#|No\.?):?\s+([A-Z0-9-]{5,})", head, re.I)
    if m:
        meta["claim_number"] = m.group(1)
    else:
        meta["claim_number"] = None
    best = None
    for c in CARRIERS:
        mm = re.search(re.escape(c), head, re.I)
        if mm and (best is None or mm.start() < best[0]):
            best = (mm.start(), c)
    meta["carrier"] = best[1] if best else "unknown"
    dates = re.findall(
        r"(\d{1,2}/\d{1,2}/\d{4})(?:\s+\d{1,2}:\d{2}\s*[AP]M)?\s.{0,140}?Page:\s*\d+", text
    )
    meta["estimate_date"] = Counter(dates).most_common(1)[0][0] if dates else None
    m = re.search(r"Date Est\. Completed:\s+(\d{1,2}/\d{1,2}/\d{4})", head)
    meta["date_est_completed"] = m.group(1) if m else None
    return meta


def pretty_name(insured: Optional[str]) -> str:
    """'DOE, JANE' -> 'Jane Doe' (used for invoice descriptions)."""
    if not insured:
        return "<Name>"
    n = insured.split("&")[0].strip()
    if "," in n:
        last, first = [x.strip() for x in n.split(",", 1)]
        n = f"{first} {last}"
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in n.split())


# --------------------------------------------------------------------------
# coverage table + summaries
# --------------------------------------------------------------------------
PP_RE = re.compile(
    r"(^|\b)(CC|C9|RC)-|contents|personal property|\bCoverage C\b|\bCov\.? C\b", re.I
)
CODE_RE = re.compile(r"ordinance|\bcode\b|building law|\bO&L\b", re.I)
NUM = r"\(?-?\$?[\d,]*\d\.\d{2}\)?"
_NUM_TOKEN_RE = re.compile(NUM)


def parse_coverage_table(pages: List[Tuple[int, str]]) -> List[Dict]:
    """Parse the "Coverage Item Total  %  ACV Total" table."""
    rows: List[Dict] = []
    for _, p in pages:
        lines = p.splitlines()
        for i, l in enumerate(lines):
            if re.match(r"\s*Coverage\s+Item Total\s+%\s+ACV Total", l):
                for l2 in lines[i + 1:]:
                    m = re.match(
                        r"\s*(.+?)\s{2,}(" + NUM + r")\s+([\d.]+)%\s+(" + NUM + r")\s+([\d.]+)%", l2
                    )
                    if not m:
                        if l2.strip() and not re.match(r"\s*[A-Za-z ,]+$", l2):
                            break
                        continue
                    name = m.group(1).strip()
                    if name.lower() == "total":
                        rows.append(
                            {
                                "coverage": "Total",
                                "item_total": parse_money(m.group(2)),
                                "acv_total": parse_money(m.group(4)),
                            }
                        )
                        break
                    rows.append(
                        {
                            "coverage": name,
                            "item_total": parse_money(m.group(2)),
                            "acv_total": parse_money(m.group(4)),
                        }
                    )
    return rows


SUMMARY_KEYS = [
    ("line_item_total", r"Line Item Total"),
    ("subtotal", r"Subtotal"),
    ("overhead", r"(General Contractor )?Overhead\b(?!.*Profit on)"),
    ("profit", r"(General Contractor )?Profit\b"),
    ("rcv", r"Replacement Cost Value"),
    ("depreciation", r"Less (Recoverable |Non-?\s?recoverable )?Depreciation"),
    ("acv", r"Actual Cash Value"),
    ("deductible", r"Less Deductible"),
    ("prior_payments", r"Less Prior Payment"),
    ("net_claim", r"Net (Claim|Payment|Actual Cash Value Payment)(?! if)"),
    ("recoverable_depreciation", r"Total Recoverable Depreciation"),
    ("net_if_recovered", r"Net Claim (Remaining )?if Depreciation"),
]

# A summary label followed by a number at the end of the line.
_SUMMARY_LINE_RE = re.compile(
    r"\s*([A-Za-z][A-Za-z &().,'/%-]*?[A-Za-z)%])\s*(?:\d\s+)?(?:@.*?)?\s{2,}.*?(" + NUM + r")\s*$"
)

# A money value that pdftotext split with spaces ("14,78 1.8 1").  Only the
# numeric tail of a line is repaired, and only when the joined form is a
# valid money value - a clean line is untouched.
_SPLIT_MONEY_TAIL_RE = re.compile(r"(\d[\d,.\s]*\d)\s*$")


def _repair_split_number_tail(line: str) -> str:
    m = _SPLIT_MONEY_TAIL_RE.search(line)
    if not m:
        return line
    tail = m.group(1)
    if " " not in tail:
        return line
    # Only repair a value whose digits were split across tokens by the narrow
    # column ("14,78 1.8 1", "1,23 1.86").  When the tail already ends in a
    # complete money token and the first fragment has no thousands separator
    # ("1 9,829.79") the leading fragment is left alone - the old parser read
    # that layout and we must not change it silently.
    first = tail.split()[0]
    if re.search(r"\d[\d,]*\.\d{2}", tail) and "," not in first:
        return line
    joined = re.sub(r"\s+", "", tail)
    if re.fullmatch(r"\(?-?\$?[\d,]+\.\d{2}\)?", joined):
        return line[: m.start(1)] + joined
    return line


def parse_summaries(pages: List[Tuple[int, str]], warnings: List[str]) -> List[Dict]:
    """Parse the "Summary for ..." blocks."""
    sums: List[Dict] = []
    for pno, p in pages:
        lines = p.splitlines()
        i = 0
        while i < len(lines):
            m = re.match(r"\s*Summary for\s*(.*)$", lines[i])
            if not m or re.match(r"All Items", m.group(1).strip(), re.I):
                i += 1
                continue
            name = re.split(r"\s{3,}", m.group(1).strip())[0]
            j = i + 1
            if not name:
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    name = re.split(r"\s{3,}", lines[j].strip())[0]
                    j += 1
            block: Dict = {"name": name, "page": pno, "taxes": {}}
            while j < len(lines):
                l = lines[j]
                mm = re.match(r"\s*Summary for\s*(.*)$", l)
                if mm and not re.match(r"All Items", mm.group(1).strip(), re.I):
                    break
                lm = _SUMMARY_LINE_RE.match(_repair_split_number_tail(l))
                if lm:
                    label, val = lm.group(1).strip(), parse_money(lm.group(2))
                    if re.search(r"\bTax\b", label, re.I) and not label.lower().startswith("less"):
                        # A carrier may print a second "Paid When Incurred"
                        # sub-block under the summary with its own tax lines.
                        # Those must not overwrite the main block's amounts
                        # (they are the PWI portion, not extra tax).
                        block["taxes"].setdefault(label, val)
                    else:
                        for key, rx in SUMMARY_KEYS:
                            if key not in block and re.match(rx, label, re.I):
                                block[key] = abs(val) if key in ("depreciation", "deductible", "prior_payments") else val
                                break
                        else:
                            # unrecognised line between Line Item Total and RCV (e.g. a roof payment
                            # schedule, other charges) - keep it so the block still reconciles
                            if "line_item_total" in block and "rcv" not in block:
                                block.setdefault("adjustments", {})[label] = val
                j += 1
            block["tax_total"] = sum(block["taxes"].values(), ZERO)
            if "rcv" in block or "line_item_total" in block:
                sums.append(block)
            else:
                warnings.append(
                    f"page {pno}: 'Summary for {name}' block had no Line Item Total/RCV - skipped"
                )
            i = j
    # de-dupe identical summaries (some carriers print them twice)
    seen = set()
    out: List[Dict] = []
    for s in sums:
        k = (s["name"], s.get("rcv"), s.get("line_item_total"))
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


# --------------------------------------------------------------------------
# line items
# --------------------------------------------------------------------------
UNIT = (
    r"(?:SQ|SF|LF|EA|SY|HR|CY|CF|DA|WK|MO|LS|TN|GL|BF|KT|RL|BX|PR|YR|MH|DY|CT|ST|PK|TH|FT|IN|LB|BG|CS)"
)
QTY_RE = re.compile(r"(?<![\d/.,])(-?\d{1,3}(?:,\d{3})*\.\d{2}|\.\d{2})\s+(" + UNIT + r")\b")
ITEM_RE = re.compile(r"^\s*\*?\s*(\d{1,4})\.\s+(\S.*)$")
TOTALS_RE = re.compile(r"^\s*(Area Totals|Totals|Total|Line Item Totals):\s*(.*?)(?:\s{2,}|$)")
HEADER_RE = re.compile(r"\bQUANTITY\b.*\bRCV\b")
NOTE_PREFIX = re.compile(
    r"^(This line item|The above|Auto Calculated|Options:|Bundle Rounding|Component |Pricing from|"
    r"\*|Note|Includes|Material|Labor|Price|Settlement|Per |\d{1,2}/\d{1,2}/\d{2,4}|"
    r"Supplement|BEGIN|End rev|ROOF COVERING|CONTINUED)",
    re.I,
)
NUMTOK = re.compile(r"^\(?-?[\d,]*\d\.\d{2}\)?\*?$")
# Status rows: Xactimate prints an all-caps marker (REVISED, PWARR, OMITTED,
# ...) where the amounts would be.  Spock's parser detected this from the
# numeric tail; the same rule is merged into ``build_item``.
STATUS_ONLY_RE = re.compile(r"^\s*([A-Z][A-Z&/.'-]{3,})\s*$")
STRIKE_WORDS = re.compile(
    r"\bREVISED\b|\bSEE\s+REVISION\b|\bSTRUCK\b|\bREMOVED\b|\bOMITTED\b|\bDELETED\b|"
    r"\bR&R\b.*\bREVISED\b|\bPWARR\b",
    re.I,
)
NOT_INCURRED_RE = re.compile(
    r"(?:has|have|had)?\s*not\s+(?:yet\s+)?(?:been\s+)?incurred\b", re.I
)
# State Farm prints "PER INVOICE" (or "PER BID") instead of a price when the
# line is billed from the contractor's invoice/bid (the 24 roof lines on the K1
# revision).  These rows are deliberately unpriced: they are excluded from the
# printed totals, so they are not a parse failure and must not look like one.
PER_INVOICE_RE = re.compile(r"\bPER\s+(INVOICE|BID)\b", re.I)
FILLER_TOK = re.compile(
    r"^(\d+/\d+|\d+/NA|NA|yrs?\.?|Avg\.?|Good|Fair|Poor|Above|Below|Excellent|\*+|E|\*E|"
    r"-?[\d.]+%|\[%\]|\[M\]|CONDITION|DEP|%|[A-Z]{2}|REVISED|SEE|REVISION|·|\||Avg\.\*?)$"
)


def is_numeric_line(s: str) -> bool:
    toks = s.split()
    return bool(toks) and all(NUMTOK.match(t) or FILLER_TOK.match(t) for t in toks)


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("”", '"').replace("“", '"')
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    return s.strip(" *")


def norm_section(s: str) -> str:
    s = re.sub(r"^CONTINUED\s*-\s*", "", s.strip(), flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def _numbers_in(text: str) -> List[int]:
    """All money tokens in a string, as cents."""
    return [parse_money(x) for x in _NUM_TOKEN_RE.findall(text)]


def _parse_qty(token: str) -> Decimal:
    """Parse a quantity token as an exact Decimal (quantities are not money).

    Xactimate quantities are frequently fractional (e.g. ``23.56 SF``), so the
    value is kept as a ``Decimal`` rather than an integer.  Money is what is
    integer cents; quantities are not money.
    """
    s = str(token).strip().replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    if s.startswith("-"):
        neg = True
        s = s[1:].strip()
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return -d if neg else d


def _apply_bbox_continuations(it: Dict, bbox: Optional[Dict]) -> None:
    """Append bbox-detected continuation text to an item's description.

    Only text that is already part of the item's own block (a wrapped line or
    a note line) is joined, so the layout text remains the source of truth.
    """
    if not bbox:
        return
    conts = bbox.get((int(it.get("page") or 1) - 1, str(it.get("no"))))
    if not conts:
        return
    have = norm(it.get("desc") or "")
    rest_txt = " ".join(norm(s) for _, s in it.get("rest") or [])
    extra: List[str] = []
    for c in conts:
        n = norm(c or "")
        if not n or n in have or n in extra:
            continue
        if n in rest_txt:
            extra.append(n)
    if extra:
        it["desc"] = re.sub(r"\s+", " ", (it.get("desc") or "") + " " + " ".join(extra)).strip()


def parse_items(
    pages: List[Tuple[int, str]],
    warnings: List[str],
    bbox: Optional[Dict] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """Parse the line-item table.

    Returns ``(items, totals_lines)``.  ``totals_lines`` holds the printed
    "Totals:" / "Area Totals:" / "Total:" / "Line Item Totals:" lines.

    ``bbox`` is the optional ``{(page_index, line_id): [continuation]}`` map
    from :func:`reconciler.pdftext.extract_bbox_continuations` (PDF input
    only).  It is used to recover description text that wraps across a page
    break; it can only *add* text that is already present in the item block.
    """
    lines: List[Tuple[int, str]] = []
    for pno, p in pages:
        for l in p.splitlines():
            lines.append((pno, l))

    start = next((k for k, (_, l) in enumerate(lines) if HEADER_RE.search(l)), None)
    if start is None:
        warnings.append("no line-item table header (QUANTITY ... RCV) found - line items not parsed")
        return [], []
    end_idx = [k for k, (_, l) in enumerate(lines) if re.match(r"\s*Line Item Totals?:", l)]
    end = end_idx[-1] if end_idx else len(lines)
    if not end_idx:
        warnings.append("no 'Line Item Totals:' line found - parsing to end of document")

    items: List[Dict] = []
    totals_lines: List[Dict] = []
    cur: Optional[Dict] = None
    header = ""
    pending: List[Dict] = []
    room_done: List[Dict] = []

    def finish(it: Optional[Dict]) -> None:
        if it is None:
            return
        items.append(it)
        pending.append(it)

    for k in range(start, end + 1):
        pno, l = lines[k]
        if HEADER_RE.search(l):
            header = l
            if cur:
                cur["closed_desc"] = True
            continue
        tm = TOTALS_RE.match(l)
        if tm:
            finish(cur)
            cur = None
            kind, name = tm.group(1), tm.group(2).strip()
            nums = _numbers_in(l[tm.end():])
            totals_lines.append({"kind": kind, "name": name, "nums": nums, "page": pno})
            if kind == "Line Item Totals":
                break
            if pending and kind in ("Totals", "Total"):
                for it in pending:
                    it["room"] = name
                totals_lines[-1]["room_items"] = list(pending)
                room_done.extend(pending)
                pending.clear()
            elif kind in ("Total", "Area Totals") and not pending:
                for it in room_done:
                    it.setdefault("area", name)
                room_done.clear()
            continue
        im = ITEM_RE.match(l)
        if im and (QTY_RE.search(l) or not is_numeric_line(im.group(2))):
            finish(cur)
            cur = {
                "no": int(im.group(1)),
                "first": im.group(2),
                "rest": [],
                "page": pno,
                "header": header,
                "closed_desc": False,
            }
            continue
        if cur is not None:
            if "Page:" in l:
                cur["closed_desc"] = True
                continue
            cur["rest"].append((pno, l))
    finish(cur)

    for it in items:
        build_item(it, warnings)
        _apply_bbox_continuations(it, bbox)

    real = [it for it in items if it.get("ok") or it.get("revised") or it.get("unpriced")]
    for it in real:
        it.setdefault("room", "(no section)")
        it["section"] = (it.get("area") + " / " if it.get("area") else "") + it["room"]
    return real, totals_lines


def build_item(it: Dict, warnings: List[str]) -> None:
    """Turn a raw item block into typed fields (all money in cents)."""
    first = it["first"]
    m = QTY_RE.search(first)
    desc_parts: List[str] = []
    num_text: List[str] = []
    seen_qty = bool(m)
    qty: Optional[int] = None
    unit: Optional[str] = None
    if m:
        desc_parts.append(first[: m.start()].strip())
        qty = _parse_qty(m.group(1))
        unit = m.group(2)
        num_text.append(first[m.end():])
    else:
        desc_parts.append(first.strip())

    stop_desc = False
    notes: List[str] = []
    for pno, l in it["rest"]:
        s = l.strip()
        if not s:
            continue
        if pno != it["page"]:
            stop_desc = True
        if not seen_qty:
            mm = QTY_RE.search(l)
            if (mm and is_numeric_line(l[: mm.start()] + " " + l[mm.end():])) or (mm and not l[: mm.start()].strip()):
                qty = _parse_qty(mm.group(1))
                unit = mm.group(2)
                num_text.append(l[mm.end():])
                seen_qty = True
                continue
        elif is_numeric_line(s) and not stop_desc:
            num_text.append(s)
            continue
        if (
            not stop_desc
            and len(s) <= 62
            and not NOTE_PREFIX.match(s)
            and ":" not in s
            and not (s.isupper() and len(s) > 3)
            and not re.search(r"\s{3,}", s)
        ):
            desc_parts.append(s)
            continue
        stop_desc = True
        notes.append(s)

    it["desc"] = re.sub(r"\s+", " ", " ".join(p for p in desc_parts if p)).strip()
    it["desc_first"] = re.sub(r"\s+", " ", desc_parts[0] if desc_parts and desc_parts[0] else it["desc"]).strip()
    it["notes"] = notes
    it["qty"] = qty
    it["unit"] = unit

    toks: List[str] = []
    for t in " ".join(num_text).split():
        if NUMTOK.match(t):
            toks.append(t)
    vals = [(parse_money(t), t.startswith("(")) for t in toks]
    it["raw_numbers"] = toks

    if qty is None:
        it["ok"] = False
        return
    if not vals:
        joined = " ".join([first, *num_text, *notes[:2]])
        if PER_INVOICE_RE.search(joined):
            # Unpriced by design (billed from the contractor invoice).  Kept in
            # the item list at $0 so a reviewer sees it; never a parse warning.
            it.update(
                ok=False,
                unpriced=True,
                pricing=str(PER_INVOICE_RE.search(joined).group(1)).lower(),
                unit_price=None,
                tax=ZERO,
                op=ZERO,
                rcv=ZERO,
                deprec=ZERO,
                acv=ZERO,
            )
            return
        marker_text = next(
            (t.strip() for t in reversed(num_text) if t.strip()),
            notes[0].strip() if notes else "",
        )
        if STRIKE_WORDS.search(joined) or STATUS_ONLY_RE.match(marker_text):
            it.update(
                revised=True,
                ok=False,
                unit_price=None,
                tax=ZERO,
                op=ZERO,
                rcv=ZERO,
                deprec=ZERO,
                acv=ZERO,
            )
            if STATUS_ONLY_RE.match(marker_text):
                it["status"] = STATUS_ONLY_RE.match(marker_text).group(1)
        elif NOT_INCURRED_RE.search(joined):
            it.update(
                not_incurred=True,
                ok=False,
                unit_price=None,
                tax=ZERO,
                op=ZERO,
                rcv=ZERO,
                deprec=ZERO,
                acv=ZERO,
            )
        else:
            it["ok"] = False
            warnings.append(
                f"p{it['page']} item {it['no']} '{it['desc'][:50]}': qty found but no prices - skipped"
            )
        return

    plain = [v for v, paren in vals if not paren]
    paren = [v for v, p in vals if p]
    up = plain[0]

    if qty == 0 and up:
        # OCR dropped the integer part of the quantity (".00 EA"); recover it only if a whole-number
        # qty reproduces a printed RCV exactly (qty x price + tax/O&P), otherwise skip the item
        for j in range(1, len(plain)):
            n = (Decimal(plain[j] - sum(plain[1:j], ZERO)) / Decimal(up)) if up else Decimal(0)
            if n >= 1 and abs(n - n.to_integral_value()) * up <= 2:
                qty = n.to_integral_value()
                it["qty"] = qty
                warnings.append(
                    f"p{it['page']} item {it['no']} '{it['desc'][:40]}': quantity unreadable (OCR '.00'); "
                    f"recovered {qty} from price/RCV arithmetic"
                )
                break

    base = int((Decimal(qty) * Decimal(up)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    tol = max(5, int(abs(base) * 0.006))
    rcv_i = None
    for j in range(1, len(plain)):
        extra = sum(plain[1:j], ZERO)
        if abs(base + extra - plain[j]) <= tol:
            rcv_i = j
            break
    if rcv_i is None:
        it["ok"] = False
        warnings.append(
            f"p{it['page']} item {it['no']} '{it['desc'][:50]}': qty x price ({qty} x {up}) does not "
            f"match any RCV in {toks} - skipped"
        )
        return

    hdr = it["header"].upper()
    between = plain[1:rcv_i]
    tax = op = ZERO
    if len(between) == 1:
        if "O&P" in hdr and "TAX" not in hdr:
            op = between[0]
        else:
            tax = between[0]
    elif len(between) >= 2:
        tax, op = between[0], between[1]
    it.update(
        ok=True,
        unit_price=up,
        tax=tax,
        op=op,
        rcv=plain[rcv_i],
        deprec=paren[0] if paren else ZERO,
        acv=plain[rcv_i + 1] if len(plain) > rcv_i + 1 else None,
    )
    it["pretax"] = it["rcv"] - tax - op

    # "not yet incurred" / "not incurred" rows are carried at $0 so they never
    # reach the billable totals (Xactimate prints an amount but does not count
    # it until it is incurred).
    if NOT_INCURRED_RE.search(" ".join(notes)):
        it["not_incurred"] = True
        it["printed_rcv"] = it["rcv"]
        it.update(tax=ZERO, op=ZERO, rcv=ZERO, deprec=ZERO, acv=ZERO, pretax=ZERO)


# --------------------------------------------------------------------------
# schema-driven row parser (fallback path)
#
# Port of Spock's standalone ``xparse.py``.  It reads the column order from
# each table header (QUANTITY / UNIT PRICE / TAX / GCO&P / RCV / DEPREC / ACV)
# and handles both the "sameline" and "nextline" row layouts.  Money is kept
# as ``Decimal`` while parsing and converted to integer cents at the boundary.
# Spock's parser fails most of our legacy corpus, so it is only used when our
# own parser cannot tie the estimate out.
# --------------------------------------------------------------------------
_SCHEMA_FIELDS = ("tax", "gcop", "rcv", "dep", "acv")
_SCHEMA_MONEY = r"-?\d[\d,]*\.\d{2}"
_SCHEMA_ITEM_START = re.compile(
    r"^\s*(?P<star>\*\s*)?(?P<line>(?:\d{1,2},\d{3}|\d(?: ?\d){0,3})[a-z]?) ?\.\s+(?P<rest>\S.*)$"
)
_SCHEMA_PUNCT_ONLY = re.compile(r"^[\s\-\u2013\u2014.,'\u2019|_~:;]*$")
_SCHEMA_QTYUNIT = re.compile(
    r"(?:^|\s)(?P<qty>-?\d[\d,]* ?\.\d{2})\s*(?P<unit>[A-Z]{1,3})(?=\s|$)"
)
_SCHEMA_NUMLINE = re.compile(
    r"^\s*(?P<qty>-?\d[\d,]* ?\.\d{2})\s*(?P<unit>[A-Z]{1,3})(?:\s+(?P<rest>.*))?$"
)
_SCHEMA_AGELIFE = re.compile(
    r"\d+\s*/\s*(?:\d(?: ?\d)*\s*yrs?|\d+|NA)\s*(?:yrs?)?|\bNA\b|\bAvg\.|"
    r"\bGood\b|\bAbove Avg\.|\bBelow Avg\.|\bNew\b",
    re.I,
)
_SCHEMA_TOT = re.compile(r"^\s*(?P<kind>Totals|Total|Line Item Totals):\s*(?P<body>.*)$")
_SCHEMA_KEYMAP = {"tax": "tax", "gcop": "op", "rcv": "rcv", "dep": "deprec", "acv": "acv"}


def _D(token: str) -> Decimal:
    return Decimal(re.sub(r"\s+", "", str(token)).replace(",", "").replace("$", ""))


def _schema_norm(s: str) -> str:
    """Collapse whitespace only - preserve case (unlike the legacy ``norm``)."""
    return re.sub(r"\s+", " ", s or "").strip()


def header_schema(line: str) -> Optional[List[str]]:
    """Read the money-column order from a line-item table header.

    Returns e.g. ``['price', 'tax', 'gcop', 'rcv', 'dep', 'acv']`` or ``None``
    when the line is not a table header.
    """
    if "QUANTITY" not in line or "RCV" not in line:
        return None
    pos = []
    q = line.index("QUANTITY")
    for key, pat in (
        ("price", r"UNIT(?:\s+PRICE)?|PRICE"),
        ("tax", r"\bTAX\b"),
        ("gcop", r"GCO&P|\bO&P\b"),
        ("rcv", r"\bRCV\b"),
        ("dep", r"DEPREC"),
        ("acv", r"\bACV\b"),
    ):
        window = line[q + 8:] if key == "price" else line
        m = re.search(pat, window)
        if m:
            pos.append((m.start() + (q + 8 if key == "price" else 0), key))
    return [k for _, k in sorted(pos)]


def _schema_money_tokens(s: str) -> List[Tuple[Optional[Decimal], bool, bool]]:
    """Money tokens from a numeric tail, merging OCR-split digits.

    Returns ``(value-or-None, in_parens, nonrecoverable)`` triples.  A ``None``
    value marks an unparsable token so :func:`_schema_assign` can reject it.
    """
    s = re.sub(r"(\.\d{2})(\d+\s*/\s*(?:\d+|NA)\s*yrs?)", r"\1 \2", s)
    s = _SCHEMA_AGELIFE.sub(" ", s)
    s = re.sub(r"\*[A-Z]{1,3}\b", " ", s)  # price-source flags such as "*EN"
    s = re.sub(r"\d+(?:\.\d+)?%", " ", s)
    out: List[Tuple[Optional[Decimal], bool, bool]] = []
    frag = ""
    for tok in s.split():
        t2 = frag + tok
        m = re.fullmatch(r"([(<]?)(-?\d[\d,]*\.\d{2})([)>]?)", t2)
        if m:
            out.append((_D(m.group(2)), bool(m.group(1) or m.group(3)), m.group(1) == "<"))
            frag = ""
        elif re.fullmatch(r"[(<]?\d{1,3}", t2) and len(t2) < 5:
            frag = t2  # OCR split like "1 1,782.56"
        elif t2 in ("$",):
            continue
        else:
            frag = ""
            out.append((None, False, False))
    return out


def _schema_assign(
    cols: List[str], toks: List[Tuple[Optional[Decimal], bool, bool]]
) -> Tuple[Optional[Dict[str, Decimal]], Optional[str]]:
    """Map money tokens onto the schema columns.  Returns ``(values, problem)``."""
    if any(v is None for v, _, _ in toks):
        return None, "unparsed token"
    vals: Dict[str, Decimal] = {k: Decimal(0) for k in _SCHEMA_FIELDS + ("price",)}
    if not toks:
        return None, "no money"
    has_dep = "dep" in cols
    paren = [i for i, (_, p, _) in enumerate(toks) if p]
    nonrec = False
    if has_dep:
        if paren:
            i = paren[0]
            before, after = toks[:i], toks[i + 1:]
            vals["dep"] = toks[i][0]
            nonrec = toks[i][2]
        else:
            before, after = toks[:-1], toks[-1:]  # dep blank => 0
        pre = cols[: cols.index("dep")]
        post = cols[cols.index("dep") + 1:]
        if len(before) != len(pre) or len(after) != len(post):
            return None, f"column count {len(toks)} vs schema {cols}"
        for k, (v, _, _) in zip(pre + post, before + after):
            vals[k] = v
    else:
        if len(toks) != len(cols):
            return None, f"column count {len(toks)} vs schema {cols}"
        for k, (v, _, _) in zip(cols, toks):
            vals[k] = v
    if "acv" not in cols:
        vals["acv"] = vals["rcv"] - vals["dep"]
    vals["nonrec"] = Decimal(1) if nonrec else Decimal(0)
    return vals, None


def _schema_line_no(line_id: str) -> int:
    m = re.match(r"\d+", str(line_id).replace(" ", "").replace(",", ""))
    return int(m.group(0)) if m else 0


def parse_items_schema(
    pages: List[Tuple[int, str]],
    warnings: List[str],
    bbox: Optional[Dict] = None,
) -> Dict:
    """Parse the line-item table with the schema-driven (fallback) parser.

    ``pages`` is the same ``[(page_number, page_text)]`` list the legacy parser
    uses, so the caller can keep the guide-page skipping and summary parsing.
    ``bbox`` is the optional PDF continuation map.  All money is returned as
    integer cents; quantities stay ``Decimal``.
    """
    lines: List[str] = []
    line_page: List[int] = []  # 0-based page index for bbox lookup
    for pno, pg in pages:
        for L in pg.split("\n"):
            lines.append(L)
            line_page.append(pno - 1)

    items: List[Dict] = []
    nodes: List[Dict] = []
    unparented: List[Dict] = []
    pending: List[Dict] = []
    formats: set = set()
    schema: Optional[List[str]] = None
    line_item_totals: Optional[Dict] = None

    def close_node(kind, name, vals, children_items=None, children_nodes=None, idx=None):
        node = {
            "kind": kind,
            "name": name,
            "printed": vals,
            "items": children_items or [],
            "children": children_nodes or [],
            "parent": None,
            "id": len(nodes),
            "src_line": idx,
        }
        nodes.append(node)
        return node

    def node_sum(node: Dict, field: str) -> Decimal:
        if node["kind"] == "section":
            return sum(
                (Decimal(0) if it.get("not_incurred") else it[field] for it in node["items"]),
                Decimal(0),
            )
        return sum((node_sum(nodes[c], field) for c in node["children"]), Decimal(0))

    i = 0
    while i < len(lines):
        L = lines[i]
        sch = header_schema(L)
        if sch:
            schema = sch
            i += 1
            continue
        t = _SCHEMA_TOT.match(L)
        if t and schema:
            body = t.group("body")
            toks = re.findall(rf"[(<]?{_SCHEMA_MONEY}[)>]?", body)
            m = re.match(
                rf"^(?P<name>.*?)\s*((?:[(<]?{_SCHEMA_MONEY}[)>]?\s*)+)$", body
            )
            name = _schema_norm(m.group("name")) if m else _schema_norm(body)
            vals_l = [_D(x.strip("()<>")) for x in toks]
            tcols = [c for c in schema if c != "price"]
            if len(vals_l) == len(tcols):
                printed = dict(zip(tcols, vals_l))
            else:
                byc = {
                    5: ["tax", "gcop", "rcv", "dep", "acv"],
                    4: ["tax", "rcv", "dep", "acv"],
                    3: ["tax", "gcop", "rcv"],
                }
                printed = dict(zip(byc.get(len(vals_l), []), vals_l))
                warnings.append(
                    f"schema totals line {i + 1} has {len(vals_l)} numbers vs header "
                    f"{tcols}: {name!r}"
                )
            if t.group("kind") == "Line Item Totals":
                line_item_totals = {
                    "name": name,
                    "printed": printed,
                    "src_line": i + 1,
                }
            elif t.group("kind") == "Totals":
                n = close_node("section", name, printed, children_items=pending, idx=i + 1)
                for it in pending:
                    it["_node"] = n["id"]
                pending = []
                unparented.append(n)
            else:  # area "Total:"
                if pending:
                    # items directly under an area (no room Totals line)
                    n = close_node(
                        "section", f"(direct items) {name}", None, children_items=pending, idx=i + 1
                    )
                    for it in pending:
                        it["_node"] = n["id"]
                    pending = []
                    unparented.append(n)
                    if all(node_sum(n, f) == printed.get(f) for f in printed):
                        # "Total: <area>" right after direct items is that
                        # group's subtotal; the area's own Total comes later.
                        n["printed"] = printed
                        n["subtotal_of_area"] = name
                        i += 1
                        continue
                area = close_node("area", name, printed, idx=i + 1)
                # shortest suffix of unparented nodes whose sums match
                chosen = None
                startk = 0 if all(v == 0 for v in printed.values()) else 1
                for k in range(startk, len(unparented) + 1):
                    suf = unparented[len(unparented) - k:]
                    if all(
                        sum((node_sum(c, f) for c in suf), Decimal(0)) == printed.get(f)
                        for f in printed
                    ):
                        chosen = suf
                        break
                if chosen is None:
                    warnings.append(
                        f"schema area Total {name!r} (line {i + 1}) does not equal any "
                        f"suffix of preceding sections; attaching sections since last area"
                    )
                    k0 = max(
                        (ix for ix, u in enumerate(unparented) if u["kind"] == "area"),
                        default=-1,
                    ) + 1
                    chosen = unparented[k0:] or list(unparented)
                for c in chosen:
                    c["parent"] = area["id"]
                    area["children"].append(c["id"])
                unparented = unparented[: len(unparented) - len(chosen)] + [area]
                if not chosen:
                    area["empty"] = True
            i += 1
            continue

        m = _SCHEMA_ITEM_START.match(L)
        if m and schema:
            rest = m.group("rest")
            star = bool(m.group("star"))
            lid = m.group("line").replace(" ", "").replace(",", "")
            qu = None
            for q in _SCHEMA_QTYUNIT.finditer(rest):
                qu = q
                break
            item = None
            if qu and qu.start() > 0:  # sameline layout
                desc = rest[: qu.start()]
                tail = rest[qu.end():]
                formats.add("sameline")
                parts = (bbox or {}).get((line_page[i], lid), [])
                j = i + 1
                for part in parts:
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and _schema_norm(lines[j]).startswith(_schema_norm(part)[:20]):
                        desc += " " + _schema_norm(part)
                        j += 1
                    else:
                        desc += " " + _schema_norm(part)
                        warnings.append(
                            f"schema continuation text for item {lid} not aligned in layout text"
                        )
                        break
                item = {
                    "line": lid,
                    "description": _schema_norm(desc),
                    "quantity": _D(qu.group("qty")),
                    "unit": qu.group("unit"),
                    "_tail": tail,
                    "_end": j,
                    "wrapped": bool(parts),
                }
            else:  # nextline layout (State Farm style)
                j = i + 1
                while j < len(lines) and _SCHEMA_PUNCT_ONLY.match(lines[j]):
                    j += 1
                nm = _SCHEMA_NUMLINE.match(lines[j]) if j < len(lines) else None
                if nm:
                    formats.add("nextline")
                    tail, j2 = nm.group("rest") or "", j + 1
                    if (
                        j2 < len(lines)
                        and re.search(_SCHEMA_MONEY, lines[j2])
                        and re.fullmatch(
                            r"[\s\d.,()<>/%-]*(?:yrs?)?[\s\d.,()<>/%-]*", lines[j2]
                        )
                        and "%" not in lines[j2]
                    ):
                        tail += " " + lines[j2]
                        j2 += 1
                    item = {
                        "line": lid,
                        "description": _schema_norm(rest),
                        "quantity": _D(nm.group("qty")),
                        "unit": nm.group("unit"),
                        "_tail": tail,
                        "_end": j2,
                        "wrapped": False,
                    }
            if item:
                tail = item.pop("_tail")
                j = item.pop("_end")
                item["starred"] = star
                item["revised_out"] = False
                mk = re.fullmatch(r"\s*([A-Z]{4,})\s*", tail)
                if re.fullmatch(r"\s*PER\s+(?:INVOICE|BID)\s*", tail, re.I):
                    # Unpriced by design: billed from the contractor invoice/bid.
                    vals = {k: Decimal(0) for k in _SCHEMA_FIELDS + ("price",)}
                    vals["nonrec"] = Decimal(0)
                    item["unpriced"] = True
                    item["pricing"] = re.sub(r"\s+", "_", tail.strip().lower())
                    item["parse_error"] = "unpriced (PER INVOICE/BID)"
                elif mk:
                    # a status marker instead of money (REVISED / PWARR / ...)
                    vals = {k: Decimal(0) for k in _SCHEMA_FIELDS + ("price",)}
                    vals["nonrec"] = Decimal(0)
                    item["revised_out"] = True
                    item["status"] = mk.group(1)
                else:
                    vals, prob = _schema_assign(schema, _schema_money_tokens(tail))
                    if prob:
                        warnings.append(
                            f"schema item {item['line']} (text line {i + 1}): {prob}: "
                            f"{_schema_norm(tail)[:80]!r}"
                        )
                        vals = {k: Decimal(0) for k in _SCHEMA_FIELDS + ("price",)}
                        vals["nonrec"] = Decimal(0)
                        item["parse_error"] = prob
                item.update(
                    price=vals["price"],
                    **{k: vals[k] for k in _SCHEMA_FIELDS},
                    nonrecoverable=bool(vals["nonrec"]),
                )
                item["acv_printed"] = "acv" in schema
                item["page"] = line_page[i] + 1
                notes: List[str] = []
                k = j
                while k < len(lines) and len(notes) < 8:
                    s = lines[k].strip()
                    if (
                        not s
                        or _SCHEMA_ITEM_START.match(lines[k])
                        or _SCHEMA_TOT.match(lines[k])
                        or header_schema(lines[k])
                        or s.startswith("Date:")
                    ):
                        if not s and notes:
                            break
                        if s:
                            break
                        k += 1
                        continue
                    if not re.match(r"^(Avg\.|Good|Below|Above|\d+\.\d+%)", s):
                        notes.append(_schema_norm(s))
                    k += 1
                item["notes"] = notes
                item["not_incurred"] = any(NOT_INCURRED_RE.search(n) for n in notes)
                item["src_line"] = i + 1
                items.append(item)
                pending.append(item)
                i = j
                continue
        i += 1

    if pending:
        n = close_node("section", "(items after last Totals)", None, children_items=pending)
        for it in pending:
            it["_node"] = n["id"]
        unparented.append(n)
        warnings.append(f"schema: {len(pending)} items not followed by a Totals line")

    # section paths
    for n in nodes:
        path: List[str] = []
        p = n
        while p["parent"] is not None:
            p = nodes[p["parent"]]
            path.append(p["name"])
        n["path"] = list(reversed(path))

    # convert Decimal dollars to integer cents and assign section labels
    for it in items:
        node = nodes[it.pop("_node")]
        path = node["path"]
        it["section"] = node["name"]
        it["section_label"] = " / ".join(path + [node["name"]])
        it["area"] = " / ".join(path) if path else None
        it["desc"] = it.pop("description")
        it["desc_first"] = it["desc"]
        it["no"] = _schema_line_no(it["line"])
        it["qty"] = it["quantity"]
        it["unit_price"] = to_cents(it["price"])
        it["tax"] = to_cents(it["tax"])
        it["op"] = to_cents(it["gcop"])
        it["rcv"] = to_cents(it["rcv"])
        it["deprec"] = to_cents(it["dep"])
        it["acv"] = to_cents(it["acv"])
        it["pretax"] = it["rcv"] - it["tax"] - it["op"]
        it["ok"] = not it.get("parse_error")
        it["revised"] = bool(it.get("revised_out"))
        it["parser"] = "schema"
        if it.get("not_incurred"):
            it["printed_rcv"] = it["rcv"]
            it.update(tax=ZERO, op=ZERO, rcv=ZERO, deprec=ZERO, acv=ZERO, pretax=ZERO)

    def node_items(n: Dict) -> List[Dict]:
        if n["kind"] == "section":
            return list(n["items"])
        out: List[Dict] = []
        for c in n["children"]:
            out.extend(node_items(nodes[c]))
        return out

    def node_fields(n: Dict) -> Optional[Dict[str, int]]:
        if not n.get("printed"):
            return None
        return {
            _SCHEMA_KEYMAP[k]: to_cents(v)
            for k, v in n["printed"].items()
            if k in _SCHEMA_KEYMAP
        }

    def node_page(n: Dict) -> int:
        src = n.get("src_line")
        if src and 0 < src <= len(line_page):
            return line_page[src - 1] + 1
        return 0

    sections: List[Dict] = []
    areas: List[Dict] = []
    printed_totals: List[Dict] = []
    for n in nodes:
        fields = node_fields(n)
        if fields is None:
            continue
        its = node_items(n)
        label = " / ".join(n["path"] + [n["name"]])
        rec = {
            "kind": "Totals" if n["kind"] == "section" else "Total",
            "name": label,
            "nums": [fields.get("rcv", ZERO)],
            "fields": fields,
            "items": its,
            "page": node_page(n),
            "strict": True,
            "parser": "schema",
        }
        printed_totals.append(rec)
        roll = {
            "name": n["name"],
            "label": label,
            "area": n["path"][-1] if n["path"] else None,
            "rcv": sum((it["rcv"] for it in its), ZERO),
            "pretax": sum((it["pretax"] for it in its), ZERO),
            "items": its,
            "printed": fields,
            "page": node_page(n),
        }
        if n["kind"] == "section":
            sections.append(roll)
        else:
            roll["sections"] = [" / ".join(nodes[c]["path"] + [nodes[c]["name"]]) for c in n["children"]]
            areas.append(roll)

    lit = None
    if line_item_totals:
        lit = {
            "name": line_item_totals["name"],
            "printed": {
                _SCHEMA_KEYMAP[k]: to_cents(v)
                for k, v in line_item_totals["printed"].items()
                if k in _SCHEMA_KEYMAP
            },
        }

    return {
        "items": [it for it in items],
        "printed_totals": printed_totals,
        "sections": sections,
        "areas": areas,
        "line_item_totals": lit,
        "formats": sorted(formats),
    }


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------
FLAG_RE = re.compile(
    r"\bPWI\b|paid when incurred|when incurred|if incurred|until .{0,40}incurred|"
    r"\bcode\b|ordinance|\bO\s*&\s*P\b|overhead\s*(and|&)\s*profit",
    re.I,
)
BOILER_IGNORE = re.compile(
    r"considered once they are incurred|lead[- ]based paint|lead safe practices|EPA|"
    r"Additional Amounts? Available If Incurred|Claim If Incurred|zip ?code",
    re.I,
)


def find_flags(pages: List[Tuple[int, str]], items: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Find PWI / code / ordinance / O&P wording on items and in narrative text."""
    item_flags: List[Dict] = []
    for it in items:
        txt = " ".join([it["desc"]] + it.get("notes", []))
        hits = sorted(
            {
                h.group(0)
                for h in FLAG_RE.finditer(txt)
                if not BOILER_IGNORE.search(txt[max(0, h.start() - 120): h.end() + 120])
            }
        )
        if hits:
            item_flags.append(
                {
                    "section": it["section"],
                    "no": it["no"],
                    "desc": it["desc"],
                    "rcv": it["rcv"],
                    "hits": hits,
                    "context": [n for n in it.get("notes", []) if FLAG_RE.search(n)][:3],
                }
            )
    item_line_texts = set()
    for it in items:
        item_line_texts.update(n for n in it.get("notes", []))
    other: List[Dict] = []
    for pno, p in pages:
        plines = p.splitlines()
        for i, l in enumerate(plines):
            s = l.strip()
            if not FLAG_RE.search(s) or s in item_line_texts or ITEM_RE.match(l):
                continue
            ctx = " ".join(x.strip() for x in plines[max(0, i - 2): i + 3])
            if BOILER_IGNORE.search(ctx) or re.match(r"Overhead door", s, re.I):
                continue
            if (
                re.search(r"\bO\s*&\s*P\b|overhead|profit", s, re.I)
                and re.search(r"\(0%\)|0\.0%|\b0\.00\b", ctx)
                and not re.search(r"[1-9][\d,]*\.\d\d", re.sub(r"\(\d+(\.\d+)?%\)", "", s))
            ):
                continue
            other.append({"page": pno, "text": re.sub(r"\s{2,}", "  ", s)[:160]})
    return item_flags, other


# --------------------------------------------------------------------------
# revisions
# --------------------------------------------------------------------------
_REVISION_MARKERS = [
    ("revision", re.compile(r"\bREVISION\b", re.I)),
    ("revised", re.compile(r"\bREVISED\b", re.I)),
    ("supplement", re.compile(r"\bSUPPLEMENT\b", re.I)),
    ("final_draft", re.compile(r"FINAL\s*_?\s*DRAFT", re.I)),
    ("estimate", re.compile(r"\bESTIMATE\b", re.I)),
]


def detect_revision(text: str) -> Optional[str]:
    """Best-effort detection of a revision label in the document header."""
    head = text[:8000]
    for key, rx in _REVISION_MARKERS:
        m = rx.search(head)
        if m:
            return key
    return None


def split_revisions(text: str) -> List[Dict]:
    """Split a document that contains multiple revision blocks.

    A revision block starts at a page whose header contains a revision marker
    ("REVISION", "REVISED", "SUPPLEMENT", "FINAL DRAFT").  The first block always
    exists, so a document with no markers yields a single block.

    Each block: ``{"label": str|None, "start_page": int, "pages": [(n, text)]}``
    """
    raw_pages = text.split("\f")
    blocks: List[Dict] = []
    current: Optional[Dict] = None
    for i, page in enumerate(raw_pages, 1):
        label = None
        head = page[:600]
        for key, rx in _REVISION_MARKERS:
            if rx.search(head):
                label = key
                break
        if current is None:
            current = {"label": label, "start_page": i, "pages": []}
        elif label and label != current.get("label"):
            blocks.append(current)
            current = {"label": label, "start_page": i, "pages": []}
        current["pages"].append((i, page))
    if current is not None:
        blocks.append(current)
    return blocks


# --------------------------------------------------------------------------
# estimate
# --------------------------------------------------------------------------
def _finalize_estimate(
    pages: List[Tuple[int, str]],
    path: str,
    meta: Dict,
    cov: List[Dict],
    sums: List[Dict],
    items: List[Dict],
    totals: List[Dict],
    warnings: List[str],
    parser_path: str,
    schema: Optional[Dict] = None,
) -> Dict:
    """Build the estimate dict around a set of parsed items.

    Shared by the primary parser and the schema fallback so both produce the
    exact same shape (coverage flags, billable totals, checks, flags).
    """
    est: Dict = {
        "path": path,
        "status": "ok",
        "kind": "carrier",
        "meta": meta,
        "coverage_table": cov,
        "summaries": sums,
        "items": items,
        "printed_totals": totals,
        "warnings": warnings,
        "parser_path": parser_path,
    }
    if schema is not None:
        est["sections"] = schema.get("sections") or []
        est["areas"] = schema.get("areas") or []
        est["formats"] = schema.get("formats") or []

    # coverage flags
    cov_flags: List[str] = []
    for row in cov:
        if row["coverage"] != "Total" and row["item_total"] != 0:
            if PP_RE.search(row["coverage"]):
                cov_flags.append(
                    f"PERSONAL PROPERTY coverage '{row['coverage']}' has {fmt_cents(row['item_total'])}"
                    f" - never billed, excluded"
                )
            if CODE_RE.search(row["coverage"]):
                cov_flags.append(
                    f"CODE/ORDINANCE coverage '{row['coverage']}' has {fmt_cents(row['item_total'])}"
                    f" - check whether billable"
                )
    for s in sums:
        s["personal_property"] = bool(PP_RE.search(s["name"]))
        s["code"] = bool(CODE_RE.search(s["name"]))
        if s["code"]:
            cov_flags.append(
                f"CODE/ORDINANCE summary '{s['name']}' RCV {fmt_cents(s.get('rcv'))}"
                f" - check whether billable"
            )
    est["coverage_flags"] = sorted(set(cov_flags))

    # item sums (all parsed rows; revised / not-incurred rows carry $0)
    item_rcv = sum((it["rcv"] for it in items), ZERO)
    item_pre = sum((it.get("pretax", ZERO) for it in items), ZERO)
    item_tax = sum((it.get("tax", ZERO) for it in items), ZERO)
    item_op = sum((it.get("op", ZERO) for it in items), ZERO)
    item_acv = sum((it.get("acv") or ZERO for it in items), ZERO)

    bill = [s for s in sums if not s["personal_property"]]
    est["totals"] = {
        "line_item_total": sum((s.get("line_item_total", ZERO) for s in bill), ZERO),
        "tax": sum((s["tax_total"] for s in bill), ZERO),
        "overhead_profit": sum((s.get("overhead", ZERO) + s.get("profit", ZERO) for s in bill), ZERO),
        "adjustments": sum((sum(s.get("adjustments", {}).values(), ZERO) for s in bill), ZERO),
        "rcv": sum((s.get("rcv", ZERO) for s in bill), ZERO),
        "deductible": sum((s.get("deductible", ZERO) for s in bill), ZERO),
        "acv": sum((s.get("acv", ZERO) for s in bill), ZERO),
        "excluded_personal_property_rcv": sum(
            (s.get("rcv", ZERO) for s in sums if s["personal_property"]), ZERO
        ),
    }
    if not sums and parser_path == "schema":
        # A no-summary estimate (e.g. a price-list style interior) has no
        # coverage block to read; the tied-out item grand total is the
        # billable total.  Personal property is handled by the summary table
        # whenever one exists, so nothing is silently dropped here.
        est["totals"].update(
            line_item_total=item_pre,
            tax=item_tax,
            overhead_profit=item_op,
            rcv=item_rcv,
            acv=item_acv,
        )

    if not sums:
        warnings.append("no 'Summary for ...' blocks parsed - RCV totals unavailable")
    for s in sums:
        if "rcv" in s and "line_item_total" in s:
            calc = (
                s["line_item_total"]
                + s["tax_total"]
                + s.get("overhead", ZERO)
                + s.get("profit", ZERO)
                + sum(s.get("adjustments", {}).values(), ZERO)
            )
            if abs(calc - s["rcv"]) > 2:
                warnings.append(
                    f"summary '{s['name']}': LIT+tax+O&P = {fmt_cents(calc)} but RCV = {fmt_cents(s['rcv'])}"
                    f" (other charges/taxes not parsed?)"
                )

    # line-item cross checks
    est["checks"] = {
        "items_parsed": len(items),
        "items_rcv_sum": item_rcv,
        "items_pretax_sum": item_pre,
        "items_tax_sum": item_tax,
        "items_op_sum": item_op,
        "items_acv_sum": item_acv,
    }
    schema_grand = (schema or {}).get("line_item_totals") or {}
    schema_printed = schema_grand.get("printed") or {}
    if schema_printed:
        est["checks"]["printed_grand"] = dict(schema_printed)
        est["checks"]["line_item_totals_printed"] = [schema_printed.get("rcv", item_rcv)]
        ok = item_rcv == schema_printed.get("rcv", item_rcv)
        est["checks"]["items_match_printed_total"] = ok
        if not ok:
            warnings.append(
                f"sum of parsed item RCV {fmt_cents(item_rcv)} does not equal the printed "
                f"'Line Item Totals' RCV {fmt_cents(schema_printed.get('rcv'))} - "
                f"some items missed or mis-parsed"
            )
    else:
        lit_line = next((t for t in totals if t["kind"] == "Line Item Totals"), None)
        if lit_line:
            ok = item_rcv in lit_line["nums"]
            est["checks"]["line_item_totals_printed"] = lit_line["nums"]
            est["checks"]["items_match_printed_total"] = ok
            if ok:
                est["checks"]["printed_grand"] = {"rcv": item_rcv}
            if not ok:
                warnings.append(
                    f"sum of parsed item RCV {fmt_cents(item_rcv)} not found in printed 'Line Item Totals' "
                    f"{[fmt_cents(x) for x in lit_line['nums']]} - some items missed or mis-parsed"
                )
    for t in totals:
        if t.get("room_items"):
            ssum = sum((it["rcv"] for it in t["room_items"] if it.get("ok")), ZERO)
            if t["nums"] and ssum not in t["nums"]:
                warnings.append(
                    f"section '{t['name']}' (p{t['page']}): parsed items sum {fmt_cents(ssum)} not in printed "
                    f"totals {[fmt_cents(x) for x in t['nums']]}"
                )
    nums_seen = [it["no"] for it in items]
    missing = sorted(set(range(1, max(nums_seen) + 1)) - set(nums_seen)) if nums_seen else []
    if missing:
        warnings.append(
            f"line numbers not parsed: {missing[:40]}{' ...' if len(missing) > 40 else ''}"
        )
    rev = [it for it in items if it.get("revised")]
    if rev:
        warnings.append(
            f"{len(rev)} item(s) printed as 'REVISED' with no amount (counted as 0): "
            + "; ".join(f"#{it['no']} {it['desc'][:40]}" for it in rev)
        )
    unpriced = [it for it in items if it.get("unpriced")]
    if unpriced:
        warnings.append(
            f"{len(unpriced)} line(s) printed 'PER INVOICE'/'PER BID' with no price - unpriced by "
            f"the carrier, excluded from its totals, bill when the invoice is received: "
            + "; ".join(f"#{it['no']} {it['desc'][:40]}" for it in unpriced[:8])
            + (" ..." if len(unpriced) > 8 else "")
        )
    est["item_flags"], est["text_flags"] = find_flags(pages, items)
    return est


def parse_text(
    text: str,
    path: str = "<text>",
    bbox: Optional[Dict] = None,
    allow_schema: bool = True,
) -> Dict:
    """Parse the text of a carrier estimate.  Returns the estimate dict.

    The primary (legacy ``recon.py``) parser runs first.  If its tie-out
    fails, the schema-driven fallback parser is run and the result that ties
    out is kept (the legacy result wins a tie).  ``parser_path`` records which
    one produced the returned items.  A failed tie-out on both paths leaves
    the legacy result in place and the caller must not draft anything.
    """
    if len(text.strip()) < 200:
        return {
            "path": path,
            "fatal": "no text layer (scanned image PDF?) - a text-based estimate is required",
            "status": "needs_ocr",
            "warnings": [],
            "items": [],
            "summaries": [],
            "coverage_table": [],
            "meta": {},
            "totals": {},
            "checks": {},
            "coverage_flags": [],
            "item_flags": [],
            "text_flags": [],
            "parser_path": None,
        }

    ocr_applied = looks_ocr(text)
    if ocr_applied:
        text = ocr_fix(text, enabled=True)

    warnings: List[str] = []
    pages, skipped = pages_of(text)
    if skipped:
        warnings.append(f"skipped carrier guide/sample pages: {skipped}")
    meta = parse_meta("\f".join(p for _, p in pages))
    meta["revision"] = detect_revision(text)
    cov = parse_coverage_table(pages)
    sums = parse_summaries(pages, warnings)
    base_warnings = list(warnings)

    legacy_warnings = list(base_warnings)
    items, totals = parse_items(pages, legacy_warnings, bbox=bbox)
    est = _finalize_estimate(
        pages, path, meta, cov, sums, items, totals, legacy_warnings, "legacy"
    )
    est["ocr_fix_applied"] = ocr_applied

    if allow_schema:
        from .tieout import tieout_estimate  # local import avoids a cycle

        legacy_to = tieout_estimate(est)
        if not legacy_to["passed"]:
            schema = parse_items_schema(pages, list(base_warnings), bbox=bbox)
            schema_est = _finalize_estimate(
                pages, path, meta, cov, sums, schema["items"], schema["printed_totals"],
                list(base_warnings), "schema", schema=schema,
            )
            schema_est["ocr_fix_applied"] = ocr_applied
            schema_to = tieout_estimate(schema_est)
            info = {
                "attempted": True,
                "tied": bool(schema_to["passed"]),
                "legacy_failures": [
                    r.name for r in legacy_to["results"] if not r.passed
                ],
            }
            est["schema_fallback"] = info
            if schema_to["passed"]:
                schema_est["schema_fallback"] = info
                est = schema_est
    return est


def parse_estimate(
    path: str,
    force_text: bool = False,
    ocr: Optional[bool] = None,
    ocr_dpi: Optional[int] = None,
    ocr_max_pages: int = 0,
) -> Dict:
    """Parse a carrier estimate from a ``.txt`` (pdftotext output) or PDF path.

    For PDFs the text is extracted with :func:`reconciler.pdftext.extract_text`.
    A missing/garbled text layer normally returns ``status == "needs_ocr"``;
    when ``ocr`` is true (or ``RECONCILER_OCR=1``) the document is sent to the
    PaddleOCR PP-OCRv6 worker first and the recognised text is parsed instead.
    A failed OCR still returns a clean ``needs_ocr`` result.
    """
    path = str(path)
    if force_text or path.lower().endswith(".txt"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            return {
                "path": path,
                "status": "error",
                "fatal": f"could not read {path}: {exc}",
                "reason": f"could not read {path}: {exc}",
                "warnings": [],
                "items": [],
                "summaries": [],
                "coverage_table": [],
                "meta": {},
                "totals": {},
                "checks": {},
                "coverage_flags": [],
                "item_flags": [],
                "text_flags": [],
                "parser_path": None,
            }
        return parse_text(text, path=path)

    from .pdftext import extract_bbox_continuations, extract_text

    # The bbox pass is only needed if the primary parser cannot tie the
    # estimate out, but starting it now overlaps its pdftotext run with the
    # layout extraction (and the primary parse) on big files.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        bbox_future = pool.submit(extract_bbox_continuations, path)
        result = extract_text(path)
        if result.status != "ok":
            bbox_future.cancel()
            from .ocr import DEFAULT_DPI, ocr_enabled, ocr_pdf

            if ocr_enabled(ocr):
                ores = ocr_pdf(
                    path,
                    dpi=ocr_dpi or DEFAULT_DPI,
                    max_pages=ocr_max_pages,
                )
                if ores.ok:
                    est = parse_text(ores.text, path=path)
                    est["pdf_text_status"] = "ocr"
                    est["pdf_pages"] = ores.pages
                    est["pdf_bbox_continuations"] = 0
                    est["ocr"] = ores.to_dict()
                    if est.get("status") != "ok":
                        est["reason"] = (
                            f"OCR ran ({ores.device}) but the recognised text could not be "
                            f"parsed as an estimate: {est.get('reason') or est.get('fatal')}"
                        )
                    return est
                result = type(result)(
                    path=result.path,
                    status="needs_ocr",
                    text=result.text,
                    reason=f"{result.reason} (OCR: {ores.reason})",
                    pages=result.pages,
                    page_stats=result.page_stats,
                    returncode=result.returncode,
                )
            return {
                "path": path,
                "status": result.status,
                "fatal": result.reason,
                "reason": result.reason,
                "warnings": [],
                "items": [],
                "summaries": [],
                "coverage_table": [],
                "meta": {},
                "totals": {},
                "checks": {},
                "coverage_flags": [],
                "item_flags": [],
                "text_flags": [],
                "parser_path": None,
            }
        bbox = bbox_future.result()
    est = parse_text(result.text, path=path, bbox=bbox or None)
    est["pdf_text_status"] = result.status
    est["pdf_pages"] = result.pages
    est["pdf_bbox_continuations"] = len(bbox or {})
    return est
