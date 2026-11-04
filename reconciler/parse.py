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
    """'ROBERT RAE' -> 'Robert Rae' (used for invoice descriptions)."""
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
                lm = _SUMMARY_LINE_RE.match(l)
                if lm:
                    label, val = lm.group(1).strip(), parse_money(lm.group(2))
                    if re.search(r"\bTax\b", label, re.I) and not label.lower().startswith("less"):
                        block["taxes"][label] = val
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


def parse_items(pages: List[Tuple[int, str]], warnings: List[str]) -> Tuple[List[Dict], List[Dict]]:
    """Parse the line-item table.

    Returns ``(items, totals_lines)``.  ``totals_lines`` holds the printed
    "Totals:" / "Area Totals:" / "Total:" / "Line Item Totals:" lines.
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

    real = [it for it in items if it.get("ok") or it.get("revised")]
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
        joined = " ".join(num_text + notes[:1])
        if re.search(r"REVISED|SEE REVISION", joined + " " + first):
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
def parse_text(text: str, path: str = "<text>") -> Dict:
    """Parse the text of a carrier estimate.  Returns the estimate dict."""
    warnings: List[str] = []
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
        }

    pages, skipped = pages_of(text)
    if skipped:
        warnings.append(f"skipped carrier guide/sample pages: {skipped}")
    meta = parse_meta("\f".join(p for _, p in pages))
    meta["revision"] = detect_revision(text)
    cov = parse_coverage_table(pages)
    sums = parse_summaries(pages, warnings)
    items, totals = parse_items(pages, warnings)

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
    }

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
    item_rcv = sum((it["rcv"] for it in items), ZERO)
    item_pre = sum((it.get("pretax", ZERO) for it in items), ZERO)
    lit_line = next((t for t in totals if t["kind"] == "Line Item Totals"), None)
    est["checks"] = {
        "items_parsed": len(items),
        "items_rcv_sum": item_rcv,
        "items_pretax_sum": item_pre,
    }
    if lit_line:
        ok = item_rcv in lit_line["nums"]
        est["checks"]["line_item_totals_printed"] = lit_line["nums"]
        est["checks"]["items_match_printed_total"] = ok
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
    est["item_flags"], est["text_flags"] = find_flags(pages, items)
    return est


def parse_estimate(path: str, force_text: bool = False) -> Dict:
    """Parse a carrier estimate from a ``.txt`` (pdftotext output) or PDF path.

    For PDFs the text is extracted with :func:`reconciler.pdftext.extract_text`;
    a missing/garbled text layer returns an estimate dict with
    ``status == "needs_ocr"`` and a human-readable ``fatal``/``reason``.
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
            }
        return parse_text(text, path=path)

    from .pdftext import extract_text

    result = extract_text(path)
    if result.status != "ok":
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
        }
    est = parse_text(result.text, path=path)
    est["pdf_text_status"] = result.status
    est["pdf_pages"] = result.pages
    return est
