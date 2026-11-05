"""Parser for OUR (contractor) Xactimate supplement estimates.

The old ``recon.py`` only handled *carrier* estimates.  Our own supplement
estimates (Vanguard / MDM Construction Xactimate exports) have a different
shape:

* no coverage table and no "Summary for <coverage>" blocks - instead a single
  ``Summary`` block at the end of the document;
* three levels of grouping: **area** (``Exterior``, ``Interior``,
  ``Detached Garage``) > **section** (``Roof``, ``Front Elevation``,
  ``Debris Removal``) > **sub-section** (``Roof Surface``, ``Cornice``);
* printed totals: ``Totals: <section>`` for a section and ``Total: <area>``
  for an area;
* the line-item header is ``DESCRIPTION QUANTITY UNIT PRICE TAX O&P RCV
  DEPREC. ACV`` (8 columns) or the 6-column variant without O&P;
* items may carry free-text notes (code citations, photo references, calc
  breakdowns) under the numbered line;
* items may be printed as ``SEE REVISION`` with no amount.

The result uses the same shape as the carrier parser so the two can be
compared: ``meta``, ``items``, ``sections``, ``totals``, ``warnings``.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from .money import ZERO, fmt_cents, parse_money
from .parse import (
    HEADER_RE,
    ITEM_RE,
    NUMTOK,
    QTY_RE,
    TOTALS_RE,
    _parse_qty,
    is_numeric_line,
)

__all__ = ["parse_supplement", "parse_supplement_text"]

_NUM = r"\(?-?\$?[\d,]*\d\.\d{2}\)?"
_NUM_TOKEN_RE = re.compile(_NUM)

# "ESTIMATOR-JOB                             9/16/2026                Page: 10"
_PAGE_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s+Page:\s*(\d+)")
# "Line Item Totals: <job name>    0.00   8,090.08   48,540.25 ..."
_LIT_RE = re.compile(r"^\s*Line Item Totals?:", re.I)

# Lines that look like a heading but are really report furniture.
_NOT_A_HEADING = re.compile(
    r"^(MDM Construction|https?://|Summary$|Recap|Grand Total Areas|"
    r"Line Item Totals|Estimate:|Price List:|Estimator:|Operator:|Business:|E-mail:|"
    r"Type of Estimate|Date Entered|Date Assigned|Cellular:|Client:|Property:|"
    r"File Number:|Note:|Yellow:|Blue:|Xactimate EULA|Labor Efficiency)",
    re.I,
)

# A section heading: a short line with no digits and no money.
_HEADING_RE = re.compile(r"^[A-Z][A-Za-z0-9 &/,'()\-.]*$")
_HEADING_MAX_LEN = 60


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > _HEADING_MAX_LEN:
        return False
    if _NOT_A_HEADING.match(s):
        return False
    if not _HEADING_RE.match(s):
        return False
    if any(ch.isdigit() for ch in s):
        return False
    return True


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _heading_level(line: str) -> int:
    """Hierarchy level of a heading, derived from its indentation.

    Xactimate centres its headings on tab stops: area headings sit around
    column 65-80, section headings around column 40 and sub-section headings
    at column 5.  We bucket the indentation into three levels and replace a
    previous heading of the same level.
    """
    ind = _indent(line)
    if ind >= 65:
        return 0
    if ind >= 25:
        return 1
    return 2


def _numbers_in(text: str) -> List[int]:
    return [parse_money(x) for x in _NUM_TOKEN_RE.findall(text)]


def _parse_meta(text: str) -> Dict[str, Optional[str]]:
    head = text[:8000]
    meta: Dict[str, Optional[str]] = {}
    m = re.search(r"Client:\s+(.+?)(?:\s{2,}|$)", head, re.M)
    if not m:
        m = re.search(r"(?:Insured|Customer):\s+(.+?)(?:\s{2,}|$)", head, re.M)
    meta["insured"] = m.group(1).strip() if m else None
    m = re.search(r"File Number:\s*(\S+)", head)
    if not m:
        m = re.search(r"Estimate:\s+(\S+)", head)
    meta["claim_number"] = m.group(1) if m else None
    m = re.search(r"Estimate:\s+(\S+)", head)
    meta["estimate_number"] = m.group(1) if m else None
    m = re.search(r"Type of Estimate:\s*(.+?)(?:\s{2,}|$)", head, re.M)
    meta["type_of_estimate"] = m.group(1).strip() if m else None
    m = re.search(r"Date Entered:\s*(\d{1,2}/\d{1,2}/\d{4})", head)
    meta["estimate_date"] = m.group(1) if m else None
    dates = _PAGE_DATE_RE.findall(text)
    if dates:
        meta["estimate_date"] = max(dates, key=lambda d: d[0])[0]
    meta["carrier"] = "us"
    meta["revision"] = "supplement"
    return meta


def _parse_summary(text: str, warnings: List[str]) -> Dict:
    """Parse the trailing ``Summary`` block of a supplement estimate."""
    block: Dict = {"name": "Summary", "page": 0, "taxes": {}}
    lines = text.splitlines()
    # The summary block is a bare "Summary" heading followed by label/value lines.
    idx = None
    for i, l in enumerate(lines):
        if re.match(r"^\s*Summary\s*$", l):
            idx = i
    if idx is None:
        return {}
    keys = [
        ("line_item_total", r"^Line Item Total"),
        ("subtotal", r"^Subtotal"),
        ("overhead", r"^Overhead\b"),
        ("profit", r"^Profit\b"),
        ("rcv", r"^Replacement Cost Value"),
        ("depreciation", r"^Less (Recoverable )?Depreciation"),
        ("acv", r"^Actual Cash Value"),
        ("deductible", r"^Less Deductible"),
        ("prior_payments", r"^Less Prior Payment"),
        ("net_claim", r"^Net (Claim|Payment)"),
    ]
    for l in lines[idx + 1:]:
        s = l.strip()
        if not s:
            continue
        nums = _numbers_in(s)
        if not nums:
            # end of the summary block
            if re.match(r"^(Recap|Grand Total|Estimate|Estimate Totals|Grand Total Areas)", s, re.I):
                break
            continue
        val = nums[-1]
        matched = False
        for key, rx in keys:
            if re.match(rx, s, re.I):
                block[key] = abs(val) if key in ("deductible", "prior_payments", "depreciation") else val
                matched = True
                break
        if not matched and re.search(r"\bTax\b", s, re.I) and not s.lower().startswith("less"):
            # a tax line ("Material Sales Tax", "Sales Tax", ...) - it counts
            # towards the RCV exactly like the carrier parser does.
            block["taxes"][s] = val
    block["tax_total"] = sum(block["taxes"].values(), ZERO)
    block["adjustments"] = {}
    if "rcv" not in block and "line_item_total" not in block:
        warnings.append("supplement Summary block had no Line Item Total/RCV - skipped")
        return {}
    return block


def parse_supplement_text(text: str, path: str = "<supplement>") -> Dict:
    """Parse the text of one of our supplement estimates."""
    warnings: List[str] = []
    if len(text.strip()) < 200:
        return {
            "path": path,
            "status": "needs_ocr",
            "fatal": "no text layer (scanned image PDF?) - a text-based estimate is required",
            "reason": "no text layer (scanned image PDF?) - a text-based estimate is required",
            "warnings": [],
            "items": [],
            "sections": [],
            "areas": [],
            "summaries": [],
            "meta": {},
            "totals": {},
            "checks": {},
        }

    pages = text.split("\f")
    meta = _parse_meta(text)
    lines: List[Tuple[int, str]] = []
    for pno, p in enumerate(pages, 1):
        for l in p.splitlines():
            lines.append((pno, l))

    # locate the line-item table region
    start = next((k for k, (_, l) in enumerate(lines) if HEADER_RE.search(l)), None)
    if start is None:
        warnings.append("no line-item table header (QUANTITY ... RCV) found - line items not parsed")
        return {
            "path": path,
            "status": "ok",
            "meta": meta,
            "warnings": warnings,
            "items": [],
            "sections": [],
            "areas": [],
            "summaries": [],
            "totals": {},
            "checks": {},
        }
    end_idx = [k for k, (_, l) in enumerate(lines) if _LIT_RE.match(l)]
    end = end_idx[-1] if end_idx else len(lines)
    if not end_idx:
        warnings.append("no 'Line Item Totals:' line found - parsing to end of document")

    items: List[Dict] = []
    printed_totals: List[Dict] = []
    cur: Optional[Dict] = None
    header = ""
    pending: List[Dict] = []

    def finish(it: Optional[Dict]) -> None:
        if it is None:
            return
        items.append(it)
        pending.append(it)

    k = start
    # heading stack: index -> heading text (0 = area, 1 = section, 2 = sub-section)
    stack: Dict[int, str] = {}
    # items grouped by the section they belong to
    section_items: List[Dict] = []
    area_items: List[Dict] = []

    def current_labels() -> Tuple[Optional[str], Optional[str], Optional[str]]:
        return stack.get(0), stack.get(1), stack.get(2)

    def assign(it: Dict) -> None:
        a, sec, sub = current_labels()
        it["area"] = a
        it["section"] = sec
        it["subsection"] = sub

    # an area total closes the area: the next section starts a fresh block
    # unless a new area heading appears first.
    area_closed = False

    while k <= end:
        pno, l = lines[k]
        s = l.strip()
        if HEADER_RE.search(l):
            header = l
            k += 1
            continue

        tm = TOTALS_RE.match(l)
        if tm:
            finish(cur)
            cur = None
            kind, name = tm.group(1), tm.group(2).strip()
            nums = _numbers_in(l[tm.end():])
            if kind == "Line Item Totals":
                printed_totals.append({"kind": kind, "name": name, "nums": nums, "page": pno})
                k += 1
                break
            if kind == "Totals":
                # a section total - the section name is authoritative
                if name:
                    stack[1] = name
                stack.pop(2, None)
                for it in pending:
                    assign(it)
                    section_items.append(it)
                printed_totals.append(
                    {"kind": kind, "name": name, "nums": nums, "page": pno, "items": list(pending)}
                )
                pending.clear()
            else:  # "Total:" - an area total
                # Xactimate prints a nested area twice: the sub-area first and
                # then the parent with identical figures (e.g. "Total: Exterior"
                # followed by "Total: Detached Garage").  When that happens the
                # parent name is the area and the first name is a sub-area.
                area_name = name
                sub_area = None
                nxt = next((x for x in lines[k + 1: k + 4] if x[1].strip()), None)
                if nxt is not None:
                    ntm = TOTALS_RE.match(nxt[1])
                    if ntm and ntm.group(1) == "Total" and _numbers_in(nxt[1][ntm.end():]) == nums:
                        area_name = ntm.group(2).strip() or area_name
                        sub_area = name
                if area_name:
                    stack[0] = area_name
                stack.pop(1, None)
                stack.pop(2, None)
                for it in pending:
                    assign(it)
                    section_items.append(it)
                for it in section_items:
                    it["area"] = area_name or it.get("area")
                    if sub_area:
                        it["sub_area"] = sub_area
                printed_totals.append(
                    {"kind": kind, "name": name, "nums": nums, "page": pno, "items": list(section_items)}
                )
                pending.clear()
                section_items.clear()
                area_closed = True
            k += 1
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
            }
            k += 1
            continue

        if cur is not None:
            if "Page:" in l:
                k += 1
                continue
            cur["rest"].append((pno, l))
            k += 1
            continue

        # not inside an item: this may be an area / section / sub-section heading
        if _is_heading(s):
            level = _heading_level(l)
            if level == 0:
                # a new area heading re-opens the area
                area_closed = False
            elif area_closed and level == 1:
                # a section that follows a closed area is top-level: no area
                stack.pop(0, None)
            stack[level] = s
            for deeper in [x for x in stack if x > level]:
                stack.pop(deeper, None)
        k += 1

    finish(cur)

    for it in items:
        _build_supplement_item(it, warnings)

    real = [it for it in items if it.get("ok") or it.get("revised")]
    for it in real:
        it.setdefault("area", None)
        it.setdefault("section", None)
        it.setdefault("subsection", None)
        it["section_label"] = " / ".join(
            p for p in (it.get("area"), it.get("section"), it.get("subsection")) if p
        ) or "(no section)"

    summary = _parse_summary(text, warnings)
    summaries = [summary] if summary else []

    # totals: use the printed Line Item Totals when present, else sum the items
    lit_line = next((t for t in printed_totals if t["kind"] == "Line Item Totals"), None)
    item_rcv = sum((it["rcv"] for it in real if it.get("ok")), ZERO)
    item_tax = sum((it.get("tax", ZERO) for it in real if it.get("ok")), ZERO)
    item_op = sum((it.get("op", ZERO) for it in real if it.get("ok")), ZERO)
    item_pretax = sum((it.get("pretax", ZERO) for it in real if it.get("ok")), ZERO)

    totals: Dict[str, int] = {
        "line_item_total": item_pretax,
        "tax": item_tax,
        "overhead_profit": item_op,
        "adjustments": 0,
        "rcv": item_rcv,
        "deductible": summary.get("deductible", 0),
        "acv": summary.get("acv", 0),
        "excluded_personal_property_rcv": 0,
    }
    if summary:
        if "line_item_total" in summary:
            totals["line_item_total"] = summary["line_item_total"]
        if "rcv" in summary:
            totals["rcv"] = summary["rcv"]
        if "overhead" in summary:
            totals["overhead_profit"] = summary.get("overhead", 0) + summary.get("profit", 0)
        if summary.get("tax_total"):
            totals["tax"] = summary["tax_total"]

    checks: Dict = {
        "items_parsed": len(real),
        "items_rcv_sum": item_rcv,
        "items_pretax_sum": item_pretax,
    }
    if lit_line:
        checks["line_item_totals_printed"] = lit_line["nums"]
        checks["items_match_printed_total"] = item_rcv in lit_line["nums"]
        if not checks["items_match_printed_total"]:
            warnings.append(
                f"sum of parsed item RCV {fmt_cents(item_rcv)} not found in printed 'Line Item Totals' "
                f"{[fmt_cents(x) for x in lit_line['nums']]} - some items missed or mis-parsed"
            )
    for t in printed_totals:
        if t.get("items") and t["nums"]:
            ssum = sum((it["rcv"] for it in t["items"] if it.get("ok")), ZERO)
            if ssum not in t["nums"]:
                warnings.append(
                    f"section '{t['name']}' (p{t['page']}): parsed items sum {fmt_cents(ssum)} not in printed "
                    f"totals {[fmt_cents(x) for x in t['nums']]}"
                )

    # section roll-up
    sections: List[Dict] = []
    by_section: Dict[str, Dict] = {}
    for it in real:
        if not it.get("ok"):
            continue
        key = it.get("section_label") or it.get("section") or "(no section)"
        sec = by_section.setdefault(
            key,
            {
                "name": key,
                "area": it.get("area"),
                "section": it.get("section"),
                "subsection": it.get("subsection"),
                "rcv": 0,
                "pretax": 0,
                "items": [],
            },
        )
        sec["rcv"] += it["rcv"]
        sec["pretax"] += it.get("pretax", 0)
        sec["items"].append(it)
    sections = list(by_section.values())

    areas: Dict[str, Dict] = {}
    for sec in sections:
        a = sec.get("area") or "(no area)"
        rec = areas.setdefault(a, {"name": a, "rcv": 0, "pretax": 0, "sections": []})
        rec["rcv"] += sec["rcv"]
        rec["pretax"] += sec["pretax"]
        rec["sections"].append(sec["name"])

    return {
        "path": path,
        "status": "ok",
        "kind": "supplement",
        "meta": meta,
        "items": real,
        "sections": sections,
        "areas": list(areas.values()),
        "summaries": summaries,
        "totals": totals,
        "checks": checks,
        "warnings": warnings,
        "printed_totals": printed_totals,
        "coverage_table": [],
        "coverage_flags": [],
        "item_flags": [],
        "text_flags": [],
    }


def _build_supplement_item(it: Dict, warnings: List[str]) -> None:
    """Build one supplement line item (same field names as the carrier parser)."""
    first = it["first"]
    m = QTY_RE.search(first)
    desc_parts: List[str] = []
    num_text: List[str] = []
    seen_qty = bool(m)
    qty: Optional[Decimal] = None
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
            if (mm and is_numeric_line(l[: mm.start()] + " " + l[mm.end():])) or (
                mm and not l[: mm.start()].strip()
            ):
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
            and ":" not in s
            and not (s.isupper() and len(s) > 3)
            and not re.search(r"\s{3,}", s)
            and not re.match(r"^(Supplement & Revision|Auto Calculated|Options:|Note:)", s, re.I)
        ):
            desc_parts.append(s)
            continue
        stop_desc = True
        notes.append(s)

    it["desc"] = re.sub(r"\s+", " ", " ".join(p for p in desc_parts if p)).strip()
    it["desc_first"] = re.sub(
        r"\s+", " ", desc_parts[0] if desc_parts and desc_parts[0] else it["desc"]
    ).strip()
    it["notes"] = notes
    it["qty"] = qty
    it["unit"] = unit

    toks = [t for t in " ".join(num_text).split() if NUMTOK.match(t)]
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
                pretax=ZERO,
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
        for j in range(1, len(plain)):
            n = Decimal(plain[j] - sum(plain[1:j], ZERO)) / Decimal(up) if up else Decimal(0)
            if n >= 1 and abs(n - n.to_integral_value()) * up <= 2:
                qty = n.to_integral_value()
                it["qty"] = qty
                warnings.append(
                    f"p{it['page']} item {it['no']} '{it['desc'][:40]}': quantity unreadable; "
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


def parse_supplement(
    path: str,
    force_text: bool = False,
    ocr: Optional[bool] = None,
    ocr_dpi: Optional[int] = None,
    ocr_max_pages: int = 0,
) -> Dict:
    """Parse our supplement estimate from a ``.txt`` or PDF path.

    ``ocr`` behaves as in :func:`reconciler.parse.parse_estimate`: a scanned
    supplement can be read with the PP-OCRv6 worker.
    """
    path = str(path)
    if force_text or path.lower().endswith(".txt"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            reason = f"could not read {path}: {exc}"
            return {
                "path": path,
                "status": "error",
                "fatal": reason,
                "reason": reason,
                "warnings": [],
                "items": [],
                "sections": [],
                "areas": [],
                "summaries": [],
                "meta": {},
                "totals": {},
                "checks": {},
            }
        return parse_supplement_text(text, path=path)

    from .pdftext import extract_text

    result = extract_text(path)
    if result.status != "ok":
        from .ocr import DEFAULT_DPI, ocr_enabled, ocr_pdf

        if ocr_enabled(ocr):
            ores = ocr_pdf(path, dpi=ocr_dpi or DEFAULT_DPI, max_pages=ocr_max_pages)
            if ores.ok:
                est = parse_supplement_text(ores.text, path=path)
                est["pdf_text_status"] = "ocr"
                est["pdf_pages"] = ores.pages
                est["ocr"] = ores.to_dict()
                if est.get("status") != "ok":
                    est["reason"] = (
                        f"OCR ran ({ores.device}) but the recognised text could not be "
                        f"parsed as an estimate: {est.get('reason') or est.get('fatal')}"
                    )
                return est
        return {
            "path": path,
            "status": result.status,
            "fatal": result.reason,
            "reason": result.reason,
            "warnings": [],
            "items": [],
            "sections": [],
            "areas": [],
            "summaries": [],
            "meta": {},
            "totals": {},
            "checks": {},
        }
    est = parse_supplement_text(result.text, path=path)
    est["pdf_text_status"] = result.status
    est["pdf_pages"] = result.pages
    return est
