"""Old-vs-new line item diff - a pass pipeline.

Pure function: it takes two lists of parsed items and returns what changed.
Nothing is written anywhere and nothing is approved: every non-exact match is
listed in ``needs_review`` and must be reviewed by a person before a draft is
approved.

Pass order:

1. our exact passes (same section + description + qty + unit price; same
   section + first description line + qty + price; same section + description
   with a qty/price change; same description/qty/price in a different section
   = moved)
2. Spock's fuzzy in-section match (SequenceMatcher plus small bonuses for
   equal qty / price / RCV / unit)
3. fuzzy moved match (across sections)
4. R&R X  <->  Remove X + Replace X split / merge
5. possible replacement (same section, qty and unit, related description)

Section mapping happens before the passes: our 50%-overlap rename rule runs
first, then Spock's scored mapping (name, parent, Jaccard of descriptions,
position, occurrence) picks up renamed / re-parented sections.

Thresholds are named module constants (they were hand-tuned by the standalone
reconciler on its own corpus; tune them on our combined corpus, not inline).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import re
from typing import Dict, List, Optional, Set, Tuple

from .money import ZERO, fmt_signed
from .parse import norm, norm_section

__all__ = [
    "diff_items",
    "item_key",
    "diff_summary",
    "ndesc",
    "SECTION_MIN_NAME_SIM",
    "SECTION_MIN_SCORE",
    "FUZZY_IN_SECTION_MIN_SIM",
    "FUZZY_MOVED_MIN_SIM",
    "RR_SPLIT_MIN_SIM",
    "REPLACEMENT_MIN_SIM",
    "AMBIGUITY_SIM_MARGIN",
]

# --- thresholds (named, not inline) --------------------------------------
SECTION_MIN_NAME_SIM = 0.60  # section name similarity floor for a candidate
SECTION_MIN_SCORE = 0.55  # combined section score floor
FUZZY_IN_SECTION_MIN_SIM = 0.80  # description similarity floor, same section
FUZZY_MOVED_MIN_SIM = 0.92  # description similarity floor for a fuzzy move
RR_SPLIT_MIN_SIM = 0.85  # R&R core similarity for split/merge
REPLACEMENT_MIN_SIM = 0.55  # possible-replacement description similarity
AMBIGUITY_SIM_MARGIN = 0.02  # two candidates within this are "ambiguous"

# Matches of these kinds are exact identity matches and may be treated as
# unchanged by a caller.  Everything else is needs-review.
EXACT_MATCH_KINDS = frozenset(
    {
        "same section",
        "same section (first line)",
        "same section (qty/price changed)",
    }
)

# Money fields compared across a match / section / row.
FIELDS = ("rcv", "pretax", "tax", "op", "deprec", "acv")


def item_key(it: Dict) -> str:
    """The matching key of an item: its normalised description."""
    return norm(it.get("desc") or "")


def ndesc(s: str) -> str:
    """Description normalisation used by the fuzzy passes.

    Lowercase, ``&`` -> ``and``, punctuation stripped (this absorbs OCR
    ``','`` vs ``'.'`` swaps and spacing noise such as ``Roofdrain-PVC`` vs
    ``Roof drain - PVC``).  Digits are kept - sizes matter.
    """
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r'[^a-z0-9/"]+', " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _nsec(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"^continued\s*-\s*", "", s)
    s = re.sub(r"\(direct items\)\s*", "direct ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _pref_sim(a: str, b: str) -> float:
    """Similarity tolerant of truncated area names (``Garage 2 - By The``)."""
    if a and b and (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 6:
        return 0.97
    return _sim(a, b)


def _section_parts(it: Dict) -> Tuple[str, str, str]:
    """Return ``(label, parent, name)`` for an item's section."""
    label = it.get("section_label") or it.get("section") or "(no section)"
    area = it.get("area")
    if area and label.startswith(str(area) + " / "):
        name = label[len(str(area)) + 3:]
        return label, str(area), name
    if " / " in label:
        parent, name = label.rsplit(" / ", 1)
        return label, parent, name
    return label, "", label


def _section_records(items: List[Dict]) -> List[Dict]:
    """Build the section metadata Spock's mapping scores against."""
    recs: Dict[str, Dict] = {}
    order: List[str] = []
    for it in items:
        label, parent, name = _section_parts(it)
        key = norm_section(label)
        if key not in recs:
            recs[key] = {
                "key": key,
                "label": label,
                "name": _nsec(name),
                "parent": _nsec(parent),
                "descs": set(),
                "order": len(order),
                "occ": 0,
            }
            order.append(key)
        recs[key]["descs"].add(ndesc(it.get("desc") or ""))
    seen: Counter = Counter()
    for key in order:
        rec = recs[key]
        k = (rec["parent"], rec["name"])
        seen[k] += 1
        rec["occ"] = seen[k]
    return [recs[k] for k in order]


def map_sections(old: List[Dict], new: List[Dict]) -> Tuple[Dict[str, str], List[Dict]]:
    """Map old section keys to new section keys.

    Our original 50%-overlap rename rule runs first (it is exact and keeps the
    historical behaviour), then Spock's scored mapping fills in renamed and
    re-parented sections.  Returns ``(mapping, renamed_pairs)``.
    """
    old_recs = _section_records(old)
    new_recs = _section_records(new)
    old_keys = {r["key"] for r in old_recs}
    new_keys = {r["key"] for r in new_recs}
    mapping: Dict[str, str] = {}
    used_new: Set[str] = set()

    # 1. our 50%-overlap rename rule
    vanished = sorted(old_keys - new_keys)
    appeared = sorted(new_keys - old_keys)
    for okey in vanished:
        oi = Counter(
            (ndesc(i.get("desc") or ""), i.get("qty"))
            for i in old
            if norm_section(_section_parts(i)[0]) == okey
        )
        total = sum(oi.values())
        best, bestn = None, 0
        for nkey in appeared:
            if nkey in used_new:
                continue
            ni = Counter(
                (ndesc(i.get("desc") or ""), i.get("qty"))
                for i in new
                if norm_section(_section_parts(i)[0]) == nkey
            )
            n = sum(min(c, ni[k]) for k, c in oi.items())
            if n > bestn:
                best, bestn = nkey, n
        if best and total and bestn * 2 >= total:
            mapping[okey] = best
            used_new.add(best)

    # 2. Spock's scored mapping for what is left
    na = max(len(old_recs), 1)
    nb = max(len(new_recs), 1)
    unmatched_old = [r for r in old_recs if r["key"] not in mapping and r["key"] not in new_keys]
    unmatched_new = [
        r for r in new_recs if r["key"] not in used_new and r["key"] not in old_keys
    ]
    cands = []
    for a in unmatched_old:
        for b in unmatched_new:
            ns = _pref_sim(a["name"], b["name"])
            ps = _pref_sim(a["parent"], b["parent"])
            if ns < SECTION_MIN_NAME_SIM:
                continue
            union = a["descs"] | b["descs"]
            jac = len(a["descs"] & b["descs"]) / len(union) if union else 1.0
            pos = 1 - abs(a["order"] / na - b["order"] / nb)
            score = (
                0.45 * ns
                + 0.2 * ps
                + 0.25 * jac
                + 0.1 * pos
                + (0.05 if a["occ"] == b["occ"] else 0.0)
            )
            cands.append((score, ns, jac, a, b))
    cands.sort(key=lambda c: -c[0])
    for score, _ns, _jac, a, b in cands:
        if score < SECTION_MIN_SCORE or a["key"] in mapping or b["key"] in used_new:
            continue
        mapping[a["key"]] = b["key"]
        used_new.add(b["key"])

    by_key = {r["key"]: r for r in new_recs}
    renamed = [
        {"old": next(r for r in old_recs if r["key"] == ok)["label"],
         "new": by_key[nk]["label"]}
        for ok, nk in mapping.items()
        if ok != nk
    ]
    return mapping, renamed


def _item_score(a: Dict, b: Dict, floor: float = 0.0) -> Tuple[float, float]:
    x, y = ndesc(a.get("desc") or ""), ndesc(b.get("desc") or "")
    if x == y:
        s = 1.0
    else:
        sm = SequenceMatcher(None, x, y, autojunk=False)
        s = 0.0 if (sm.real_quick_ratio() < floor or sm.quick_ratio() < floor) else sm.ratio()
    bonus = 0.0
    if a.get("qty") == b.get("qty"):
        bonus += 0.06
    if a.get("unit_price") == b.get("unit_price"):
        bonus += 0.03
    if a.get("rcv") == b.get("rcv"):
        bonus += 0.03
    if a.get("unit") == b.get("unit"):
        bonus += 0.01
    return s, s + bonus


def _rr_core(d: str) -> Tuple[str, str]:
    """``r and r x`` -> ``('rr', 'x')``; remove/replace/tear off likewise."""
    for prefix, kind in (
        ("r and r ", "rr"),
        ("remove ", "remove"),
        ("replace ", "replace"),
        ("tear off ", "remove"),
    ):
        if d.startswith(prefix):
            return kind, d[len(prefix):]
    return "", d


def _nums(s: str) -> Set[str]:
    return set(re.findall(r"\d+(?: \d+)?", s or ""))


def _take(
    o_left: List[Dict], n_left: List[Dict], keyfn
) -> Tuple[List[Tuple[Dict, Dict]], List[Dict], List[Dict]]:
    """Greedy exact bucket match; returns (pairs, remaining old, remaining new)."""
    idx: Dict = defaultdict(list)
    for pos, n in enumerate(n_left):
        idx[keyfn(n, False)].append(pos)
    used: Set[int] = set()
    pairs: List[Tuple[Dict, Dict]] = []
    rest: List[Dict] = []
    for o in o_left:
        bucket = idx.get(keyfn(o, True))
        if bucket:
            pos = bucket.pop(0)
            used.add(pos)
            pairs.append((o, n_left[pos]))
        else:
            rest.append(o)
    left_n = [n for pos, n in enumerate(n_left) if pos not in used]
    return pairs, rest, left_n


def _greedy(cands: List[Tuple], o_left: List[Dict], n_left: List[Dict]):
    """Apply score-sorted candidates; returns (matches, remaining old/new)."""
    oid = {id(it) for it in o_left}
    nid = {id(it) for it in n_left}
    out = []
    for cand in cands:
        a, b = cand[-2], cand[-1]
        if id(a) in oid and id(b) in nid:
            oid.discard(id(a))
            nid.discard(id(b))
            out.append(cand)
    return (
        out,
        [it for it in o_left if id(it) in oid],
        [it for it in n_left if id(it) in nid],
    )


def diff_items(old: List[Dict], new: List[Dict]) -> Dict:
    """Diff two lists of parsed items.

    Returns ``alias`` (section renames), ``unchanged``, ``changed``,
    ``moved``, ``split``, ``added``, ``removed``, ``sections``,
    ``matches`` (every match with ``how`` + ``sim`` + ``exact``),
    ``needs_review`` (all non-exact matches plus ambiguous near-ties),
    ``ambiguous`` and ``row_deltas``.
    """
    old = list(old or [])
    new = list(new or [])
    mapping, renamed = map_sections(old, new)

    def sec_of(it: Dict) -> str:
        return mapping.get(norm_section(_section_parts(it)[0]), norm_section(_section_parts(it)[0]))

    matches: List[Dict] = []
    needs_review: List[Dict] = []
    ambiguous: List[Dict] = []

    o_left = list(old)
    n_left = list(new)

    def record(a_items, b_items, how, sim, exact, **extra):
        m = {
            "kind": "match",
            "a": list(a_items),
            "b": list(b_items),
            "how": how,
            "sim": sim,
            "exact": bool(exact),
        }
        m.update(extra)
        matches.append(m)
        if not m["exact"]:
            needs_review.append(m)
        return m

    def exact_bucket(kind: str, how: str):
        nonlocal o_left, n_left
        pairs, rest, left_n = _take(o_left, n_left, kind)
        o_left, n_left = rest, left_n
        for o, n in pairs:
            record([o], [n], how, 1.0, True)
        return pairs

    # --- 1. our exact passes ---------------------------------------------
    exact_bucket(
        lambda it, io: (sec_of(it), ndesc(it.get("desc") or ""), it.get("qty"), it.get("unit_price")),
        "same section",
    )
    exact_bucket(
        lambda it, io: (
            sec_of(it),
            ndesc(it.get("desc_first") or ""),
            it.get("qty"),
            it.get("unit_price"),
        ),
        "same section (first line)",
    )
    # same section, exact description, qty/price moved
    pairs, rest, left_n = _take(
        o_left, n_left, lambda it, io: (sec_of(it), ndesc(it.get("desc") or ""))
    )
    o_left, n_left = rest, left_n
    for o, n in pairs:
        record([o], [n], "same section (qty/price changed)", 1.0, True, forced_changed=True)

    # exact description + qty + price across sections = moved
    pairs, rest, left_n = _take(
        o_left,
        n_left,
        lambda it, io: (ndesc(it.get("desc") or ""), it.get("qty"), it.get("unit_price")),
    )
    o_left, n_left = rest, left_n
    for o, n in pairs:
        record([o], [n], "moved section", 1.0, False)

    # --- 2. fuzzy in-section ---------------------------------------------
    b_by_sec: Dict[str, List[Dict]] = defaultdict(list)
    for b in n_left:
        b_by_sec[sec_of(b)].append(b)
    cands = []
    for a in o_left:
        for b in b_by_sec.get(sec_of(a), []):
            s, sc = _item_score(a, b, FUZZY_IN_SECTION_MIN_SIM)
            if s >= FUZZY_IN_SECTION_MIN_SIM:
                cands.append((sc, s, a, b))
    cands.sort(key=lambda c: -c[0])

    # ambiguity: one old item with two near-equal new candidates
    by_a: Dict[int, List[Tuple]] = defaultdict(list)
    for c in cands:
        by_a[id(c[2])].append(c)
    for _aid, cs in by_a.items():
        if len(cs) > 1 and cs[0][0] - cs[1][0] < AMBIGUITY_SIM_MARGIN:
            a, b1, b2 = cs[0][2], cs[0][3], cs[1][3]
            same_copy = (
                ndesc(b1.get("desc") or "") == ndesc(b2.get("desc") or "")
                and b1.get("qty") == b2.get("qty")
                and b1.get("rcv") == b2.get("rcv")
            )
            if not same_copy:
                entry = {
                    "kind": "ambiguous",
                    "a": a,
                    "chosen": b1,
                    "alternative": b2,
                    "sim_chosen": cs[0][1],
                    "sim_alt": cs[1][1],
                }
                ambiguous.append(entry)
                needs_review.append(entry)

    picked, o_left, n_left = _greedy(cands, o_left, n_left)
    for sc, s, a, b in picked:
        record([a], [b], "fuzzy in-section", round(s, 4), False)

    # --- 3. fuzzy moved ---------------------------------------------------
    # (a) exact description with same qty or same non-zero RCV
    mov = []
    for a in o_left:
        da = ndesc(a.get("desc") or "")
        for b in n_left:
            if ndesc(b.get("desc") or "") != da:
                continue
            same_qty = a.get("qty") == b.get("qty")
            same_rcv = a.get("rcv") == b.get("rcv") and a.get("rcv") not in (0, None)
            if same_qty or same_rcv:
                mov.append((1.0, 1.0, a, b))
    mov.sort(key=lambda c: -c[0])
    picked, o_left, n_left = _greedy(mov, o_left, n_left)
    for _sc, s, a, b in picked:
        record([a], [b], "moved section", s, False)

    # (b) strong fuzzy + same qty
    mov = []
    for a in o_left:
        for b in n_left:
            if a.get("qty") != b.get("qty"):
                continue
            s, sc = _item_score(a, b, FUZZY_MOVED_MIN_SIM)
            if s >= FUZZY_MOVED_MIN_SIM:
                mov.append((sc, s, a, b))
    mov.sort(key=lambda c: -c[0])
    picked, o_left, n_left = _greedy(mov, o_left, n_left)
    for _sc, s, a, b in picked:
        record([a], [b], "moved section, fuzzy desc", round(s, 4), False)

    # --- 4. R&R split / merge --------------------------------------------
    def rr_pairs(src: List[Dict], dst: List[Dict], src_is_old: bool):
        """Greedy ``R&R X`` <-> ``Remove X`` + ``Replace X`` pairs (no mutation)."""
        used_src: Set[int] = set()
        used_dst: Set[int] = set()
        out = []
        for a in src:
            if id(a) in used_src:
                continue
            kind, core = _rr_core(ndesc(a.get("desc") or ""))
            if kind != "rr":
                continue

            def part(x: Dict) -> Tuple[str, str]:
                return _rr_core(ndesc(x.get("desc") or ""))

            rem = [
                x
                for x in dst
                if id(x) not in used_dst
                and part(x)[0] == "remove"
                and _sim(part(x)[1], core) >= RR_SPLIT_MIN_SIM
                and x.get("qty") == a.get("qty")
            ]
            rep = [
                x
                for x in dst
                if id(x) not in used_dst
                and part(x)[0] in ("replace", "")
                and _sim(part(x)[1], core) >= RR_SPLIT_MIN_SIM
                and x.get("qty") == a.get("qty")
            ]
            if not (rem and rep):
                continue

            def near(x: Dict):
                if src_is_old:
                    same_sec = mapping.get(norm_section(_section_parts(a)[0])) == norm_section(
                        _section_parts(x)[0]
                    )
                else:
                    same_sec = mapping.get(norm_section(_section_parts(x)[0])) == norm_section(
                        _section_parts(a)[0]
                    )
                return (not same_sec, -_sim(part(x)[1], core))

            r1 = sorted(rem, key=near)[0]
            r2 = sorted(rep, key=near)[0]
            used_src.add(id(a))
            used_dst.add(id(r1))
            used_dst.add(id(r2))
            out.append((a, [r1, r2]))
        return out, used_src, used_dst

    pairs, used_o, used_n = rr_pairs(o_left, n_left, True)
    for a, parts in pairs:
        sim = min(
            _sim(
                _rr_core(ndesc(a.get("desc") or ""))[1],
                _rr_core(ndesc(p.get("desc") or ""))[1],
            )
            for p in parts
        )
        record([a], parts, "R&R split into Remove + Replace", round(sim, 4), False)
    o_left = [x for x in o_left if id(x) not in used_o]
    n_left = [x for x in n_left if id(x) not in used_n]

    pairs, used_n, used_o = rr_pairs(n_left, o_left, False)
    for rr_new, old_parts in pairs:
        sim = min(
            _sim(
                _rr_core(ndesc(p.get("desc") or ""))[1],
                _rr_core(ndesc(rr_new.get("desc") or ""))[1],
            )
            for p in old_parts
        )
        record(old_parts, [rr_new], "Remove + Replace merged into R&R", round(sim, 4), False)
    o_left = [x for x in o_left if id(x) not in used_o]
    n_left = [x for x in n_left if id(x) not in used_n]

    # --- 5. possible replacement -----------------------------------------
    cands = []
    for a in o_left:
        for b in n_left:
            if sec_of(a) != sec_of(b):
                continue
            if a.get("qty") != b.get("qty") or a.get("unit") != b.get("unit"):
                continue
            if not a.get("qty"):
                continue
            da, db = ndesc(a.get("desc") or ""), ndesc(b.get("desc") or "")
            s = _sim(da, db)
            if s < REPLACEMENT_MIN_SIM:
                continue
            if _nums(da) != _nums(db):
                continue
            if _rr_core(da)[0] != _rr_core(db)[0]:
                continue
            cands.append((s, s, a, b))
    cands.sort(key=lambda c: -c[0])
    picked, o_left, n_left = _greedy(cands, o_left, n_left)
    for _sc, s, a, b in picked:
        record([a], [b], "possible replacement", round(s, 4), False)

    # --- leftovers --------------------------------------------------------
    removed, added = o_left, n_left

    # --- classify 1:1 matches for the historical result shape ------------
    unchanged: List[Tuple[Dict, Dict]] = []
    changed: List[Tuple[Dict, Dict]] = []
    moved: List[Tuple[Dict, Dict]] = []
    split: List[Dict] = []
    for m in matches:
        a_items, b_items = m["a"], m["b"]
        if len(a_items) != 1 or len(b_items) != 1:
            split.append(m)
            continue
        o, n = a_items[0], b_items[0]
        what: List[str] = []
        if o.get("qty") != n.get("qty"):
            what.append("qty")
        if o.get("unit_price") != n.get("unit_price"):
            what.append("unit price")
        if ndesc(o.get("desc") or "") != ndesc(n.get("desc") or ""):
            what.append("description")
        for f, label in (("tax", "tax"), ("op", "O&P"), ("rcv", "RCV"), ("deprec", "depreciation")):
            if (o.get(f) or 0) != (n.get(f) or 0):
                what.append(label)
        if _section_parts(o)[0] != _section_parts(n)[0]:
            what.append("moved")
        m["what_changed"] = list(dict.fromkeys(what))
        if m["how"].startswith("moved"):
            moved.append((o, n))
        elif m["exact"] and o.get("rcv") == n.get("rcv") and not m.get("forced_changed"):
            unchanged.append((o, n))
        else:
            changed.append((o, n))

    # --- per-section totals + row deltas (for tieout_diff) ---------------
    secs: Dict[str, Dict] = defaultdict(
        lambda: {
            "old": ZERO,
            "new": ZERO,
            "old_pre": ZERO,
            "new_pre": ZERO,
            "label": None,
            **{f"old_{f}": ZERO for f in FIELDS},
            **{f"new_{f}": ZERO for f in FIELDS},
        }
    )
    for it in old:
        s = secs[sec_of(it)]
        s["old"] += it.get("rcv", 0)
        s["old_pre"] += it.get("pretax", 0)
        s["label"] = s["label"] or it.get("section")
        for f in FIELDS:
            s[f"old_{f}"] += int(it.get(f) or 0)
    for it in new:
        s = secs[sec_of(it)]
        s["new"] += it.get("rcv", 0)
        s["new_pre"] += it.get("pretax", 0)
        s["label"] = it.get("section")
        for f in FIELDS:
            s[f"new_{f}"] += int(it.get(f) or 0)

    row_deltas = {f: ZERO for f in FIELDS}
    for m in matches:
        for f in FIELDS:
            row_deltas[f] += sum(int(it.get(f) or 0) for it in m["b"]) - sum(
                int(it.get(f) or 0) for it in m["a"]
            )
    for it in added:
        for f in FIELDS:
            row_deltas[f] += int(it.get(f) or 0)
    for it in removed:
        for f in FIELDS:
            row_deltas[f] -= int(it.get(f) or 0)

    return {
        "alias": {k: v for k, v in mapping.items() if k != v},
        "section_map": mapping,
        "sections_renamed": renamed,
        "unchanged": unchanged,
        "changed": changed,
        "moved": moved,
        "split": split,
        "added": added,
        "removed": removed,
        "sections": dict(secs),
        "matches": matches,
        "needs_review": needs_review,
        "needs_review_count": len(needs_review),
        "ambiguous": ambiguous,
        "row_deltas": row_deltas,
    }


def diff_summary(d: Dict) -> Dict:
    """Summarise a diff: counts, the net change and the review count.

    Backward compatible: every key that existed before is still present.
    ``needs_review_count`` and the split/tax/op/depreciation/ACV nets are
    additive.
    """
    if not d:
        return {}

    def total(items: List[Dict], field: str) -> int:
        return sum((int(it.get(field) or 0) for it in items), ZERO)

    added = total(d.get("added", []), "rcv")
    removed = total(d.get("removed", []), "rcv")
    changed = sum((n.get("rcv", 0) - o.get("rcv", 0) for o, n in d.get("changed", [])), ZERO)
    moved = sum((n.get("rcv", 0) - o.get("rcv", 0) for o, n in d.get("moved", [])), ZERO)
    split = sum(
        (
            total(m.get("b", []), "rcv") - total(m.get("a", []), "rcv")
            for m in d.get("split", [])
        ),
        ZERO,
    )
    net = added - removed + changed + moved + split

    pre = (
        total(d.get("added", []), "pretax")
        - total(d.get("removed", []), "pretax")
        + sum(
            (
                n.get("pretax", 0) - o.get("pretax", 0)
                for o, n in d.get("changed", []) + d.get("moved", [])
            ),
            ZERO,
        )
        + sum(
            (
                total(m.get("b", []), "pretax") - total(m.get("a", []), "pretax")
                for m in d.get("split", [])
            ),
            ZERO,
        )
    )
    return {
        "added_count": len(d.get("added", [])),
        "removed_count": len(d.get("removed", [])),
        "changed_count": len(d.get("changed", [])),
        "moved_count": len(d.get("moved", [])),
        "unchanged_count": len(d.get("unchanged", [])),
        "split_count": len(d.get("split", [])),
        "added_total": added,
        "removed_total": removed,
        "changed_total": changed,
        "moved_total": moved,
        "split_total": split,
        "net_rcv": net,
        "net_pretax": pre,
        "net_rcv_formatted": fmt_signed(net),
        "section_renames": d.get("alias", {}),
        "review_count": len(d.get("needs_review", [])),
        "needs_review_count": len(d.get("needs_review", [])),
        "ambiguous_count": len(d.get("ambiguous", [])),
        "row_deltas": dict(d.get("row_deltas") or {}),
    }
