"""Old-vs-new line item diff (port of ``recon.py``'s ``diff_items``).

Pure function: it takes two lists of parsed items and returns what changed.
Nothing is written anywhere.

Matching strategy, in order:

1. same section + same description + same qty + same unit price
2. same section + same *first* description line + same qty + same unit price
   (catches an adjuster note glued onto the description)
3. same section + same description (qty/price changed)
4. same description + qty + price but a different section ("moved")

Section renames are detected automatically: when a section disappears from one
side and a new section appears on the other, the two are paired when at least
half of the old section's (description, qty) pairs are found in the new one.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from .money import ZERO, fmt_signed
from .parse import norm, norm_section

__all__ = ["diff_items", "item_key", "diff_summary"]


def item_key(it: Dict) -> str:
    """The matching key of an item: its normalised description."""
    return norm(it.get("desc") or "")


def diff_items(old: List[Dict], new: List[Dict]) -> Dict:
    """Diff two lists of parsed items.

    Returns a dict with ``alias`` (detected section renames), ``unchanged``,
    ``changed``, ``moved``, ``added``, ``removed`` and ``sections``.
    """
    old = list(old or [])
    new = list(new or [])

    old_secs = {norm_section(it.get("section") or "") for it in old}
    new_secs = {norm_section(it.get("section") or "") for it in new}

    # section rename detection
    alias: Dict[str, str] = {}
    for osec in old_secs - new_secs:
        oi = [(item_key(i), i.get("qty")) for i in old if norm_section(i.get("section") or "") == osec]
        best, bestn = None, 0
        for nsec in new_secs - old_secs:
            ni = Counter(
                (item_key(i), i.get("qty"))
                for i in new
                if norm_section(i.get("section") or "") == nsec
            )
            n = sum(min(c, ni[k]) for k, c in Counter(oi).items())
            if n > bestn:
                best, bestn = nsec, n
        if best and bestn * 2 >= len(oi):
            alias[osec] = best

    def sec_of(it: Dict, is_old: bool) -> str:
        s = norm_section(it.get("section") or "")
        return alias.get(s, s) if is_old else s

    o_left = list(old)
    n_left = list(new)
    unchanged: List[Tuple[Dict, Dict]] = []
    changed: List[Tuple[Dict, Dict]] = []
    moved: List[Tuple[Dict, Dict]] = []

    def take(pred_key, bucket) -> None:
        idx: Dict = defaultdict(list)
        for n in n_left:
            idx[pred_key(n, False)].append(n)
        rest: List[Dict] = []
        for o in o_left:
            k = pred_key(o, True)
            if idx.get(k):
                n = idx[k].pop(0)
                n_left.remove(n)
                bucket(o, n)
            else:
                rest.append(o)
        o_left[:] = rest

    def b_exact(o: Dict, n: Dict) -> None:
        (unchanged if o.get("rcv") == n.get("rcv") else changed).append((o, n))

    take(
        lambda it, io: (
            sec_of(it, io),
            item_key(it),
            it.get("qty"),
            it.get("unit_price"),
        ),
        b_exact,
    )
    # same line, but the wrapped/continuation text differs
    take(
        lambda it, io: (
            sec_of(it, io),
            norm(it.get("desc_first") or ""),
            it.get("qty"),
            it.get("unit_price"),
        ),
        b_exact,
    )
    take(lambda it, io: (sec_of(it, io), item_key(it)), lambda o, n: changed.append((o, n)))
    take(
        lambda it, io: (item_key(it), it.get("qty"), it.get("unit_price")),
        lambda o, n: moved.append((o, n)),
    )
    removed, added = o_left, n_left

    # per-section totals
    secs: Dict[str, Dict] = defaultdict(
        lambda: {"old": ZERO, "new": ZERO, "old_pre": ZERO, "new_pre": ZERO, "label": None}
    )
    for it in old:
        s = secs[sec_of(it, True)]
        s["old"] += it.get("rcv", 0)
        s["old_pre"] += it.get("pretax", 0)
        s["label"] = s["label"] or it.get("section")
    for it in new:
        s = secs[sec_of(it, False)]
        s["new"] += it.get("rcv", 0)
        s["new_pre"] += it.get("pretax", 0)
        s["label"] = it.get("section")

    return {
        "alias": alias,
        "unchanged": unchanged,
        "changed": changed,
        "moved": moved,
        "added": added,
        "removed": removed,
        "sections": dict(secs),
    }


def diff_summary(d: Dict) -> Dict:
    """Summarise a diff: counts and the net change, all in cents."""
    if not d:
        return {}
    added = sum((i.get("rcv", 0) for i in d.get("added", [])), ZERO)
    removed = sum((i.get("rcv", 0) for i in d.get("removed", [])), ZERO)
    changed = sum((n.get("rcv", 0) - o.get("rcv", 0) for o, n in d.get("changed", [])), ZERO)
    moved = sum((n.get("rcv", 0) - o.get("rcv", 0) for o, n in d.get("moved", [])), ZERO)
    net = added - removed + changed + moved
    pre = (
        sum((i.get("pretax", 0) for i in d.get("added", [])), ZERO)
        - sum((i.get("pretax", 0) for i in d.get("removed", [])), ZERO)
        + sum(
            (
                n.get("pretax", 0) - o.get("pretax", 0)
                for o, n in d.get("changed", []) + d.get("moved", [])
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
        "added_total": added,
        "removed_total": removed,
        "changed_total": changed,
        "moved_total": moved,
        "net_rcv": net,
        "net_pretax": pre,
        "net_rcv_formatted": fmt_signed(net),
        "section_renames": d.get("alias", {}),
    }
