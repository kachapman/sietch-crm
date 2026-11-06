"""Sietch-side compatibility record for the estimate reconciler.

This module does not parse estimates and does not change
``parse_estimate``, ``parse_text``, ``parse_supplement``, ``pdftext``,
``diff``, or ``tieout``. The Sherwood branch ``reconciler-parser-v30``
(off ``e04750d``, local to Tumnus) owns the single core design for
alternate-table dispatch, wrapped rows, depreciation, the completeness
gate, tie-out changes, and the ESX/XML entry route. Those behaviors are
intentionally absent here so this tree does not grow a second parser.

Nothing in this module starts a worker, a queue, a model, a network
call, or a database write. The running dashboard does not import
``reconciler`` (the image Dockerfile never copies the package, and
``server.py`` has no estimate-parser route). CRM ``full-reconcile`` is
an unrelated OnlyOffice sync and is not wired to this package.

``needs_review`` on a diff result is the existing line-match review
list. It is not a parse-completeness status. This package does not emit
an ``"accepted"`` status.
"""
from __future__ import annotations

import inspect
from typing import Dict, Mapping, Optional

from . import __version__

# Production dashboard commit this package was cut from. Not a Sherwood SHA.
SIETCH_BASELINE_COMMIT = "ee6e40145c2329c81be3aa43c7b304d51428cd21"

# Reported Sherwood vendor baseline. This agent could not read that tree.
SHERWOOD_BASELINE_COMMIT = "e04750d"
SHERWOOD_BASELINE_REPO = "https://github.com/kachapman/sherwood-toolbox"
SHERWOOD_VENDOR_PREFIX = "vendor/sietch-reconciler/reconciler"
SHERWOOD_BASELINE_REVIEWED = False
SHERWOOD_BASELINE_REVIEW_BLOCK = (
    "kachapman/sherwood-toolbox is not readable with this agent's GitHub "
    "credential (HTTP 404). The public fork claudchereji/sherwood-toolbox "
    "does not contain e04750d or vendor/sietch-reconciler. No Sherwood "
    "delta was ported."
)

# Tumnus's core patch. Not applied in this tree.
V30_BRANCH = "reconciler-parser-v30"
V30_BASE = "e04750d"
V30_STATUS = "waiting_on_tumnus_patch"
V30_OWNED_WORK = (
    "alternate-table header dispatch, including QTY / REMOVE / REPLACE / TOTAL",
    "wrapped-row recovery",
    "depreciation column handling",
    "completeness gate that refuses or marks needs_review when reliable "
    "completeness evidence fails",
    "tie-out rule changes",
    "ESX/XML structured entry route, including untrusted zip/xml safety",
)

# Status strings the parsers already return. No new status is introduced.
EXISTING_PARSE_STATUSES = ("ok", "needs_ocr", "error")

# Spec-only. Not produced by parse_estimate, and no worker consumes them.
OCR_HANDOFF_SPEC = {
    "implemented": False,
    "worker": None,
    "queue": None,
    "cloud_fallback": None,
    "model_inference": False,
    "encrypted_xactdoc_decryption": False,
    "existing_document_statuses": list(EXISTING_PARSE_STATUSES),
    "existing_provenance_fields": (
        "status",
        "reason",
        "fatal",
        "parser_path",
        "pdf_text_status",
        "pdf_pages",
        "pdf_bbox_continuations",
        "ocr_fix_applied",
        "schema_fallback",
    ),
    "pending_fields_not_emitted": (
        "page_disposition",
        "ocr_needed_pages",
        "unresolved_estimate_pages",
        "ocr_provenance",
    ),
}

# Symbols supplement.py imports from parse.py. A header change in parse.py
# is visible to the supplement parser because these are the same objects.
SUPPLEMENT_IMPORTS_FROM_PARSE = (
    "HEADER_RE",
    "ITEM_RE",
    "NUMTOK",
    "QTY_RE",
    "TOTALS_RE",
    "_parse_qty",
    "is_numeric_line",
)

# Files Tumnus reported as divergent or unchanged versus ee6e401.
# Unverified: the Sherwood tree was not readable from this VM.
REPORTED_DIVERGENT = ("parse.py", "pdftext.py", "diff.py")
REPORTED_UNCHANGED = (
    "tieout.py",
    "money.py",
    "notes.py",
    "commission.py",
    "__init__.py",
)
REPORTED_SUPPLEMENT = "upstream-only"


def baseline_record() -> Dict:
    """Facts a later integration commit should read before applying v30.

    ``sherwood_baseline_reviewed`` stays false until someone with access to
    ``e04750d`` checks the vendor files. This function does not fetch them.
    """
    return {
        "package": "reconciler",
        "version": __version__,
        "sietch_baseline_commit": SIETCH_BASELINE_COMMIT,
        "sherwood_baseline_commit": SHERWOOD_BASELINE_COMMIT,
        "sherwood_baseline_repo": SHERWOOD_BASELINE_REPO,
        "sherwood_vendor_prefix": SHERWOOD_VENDOR_PREFIX,
        "sherwood_baseline_reviewed": SHERWOOD_BASELINE_REVIEWED,
        "sherwood_baseline_review_block": SHERWOOD_BASELINE_REVIEW_BLOCK,
        "ported_sherwood_delta": False,
        "v30_branch": V30_BRANCH,
        "v30_base": V30_BASE,
        "v30_status": V30_STATUS,
        "v30_owned_work": list(V30_OWNED_WORK),
        "reported_divergent_files": list(REPORTED_DIVERGENT),
        "reported_unchanged_files": list(REPORTED_UNCHANGED),
        "reported_supplement": REPORTED_SUPPLEMENT,
        "deployment": deployment_gap(),
        "ocr_handoff": dict(OCR_HANDOFF_SPEC),
        "public_api": public_signatures(),
    }


def deployment_gap() -> Dict:
    """The running image and the CRM sync route do not call this package.

    The booleans are the intended deployment facts. Tests read ``Dockerfile``
    and ``server.py`` directly so a comment-only drift in this dict is caught.
    This function does not edit either file.
    """
    return {
        "dockerfile_copies_reconciler": False,
        "poppler_utils_installed_in_image": True,
        "server_imports_reconciler": False,
        "estimate_parser_route": False,
        "crm_full_reconcile_is_estimate_parser": False,
        "crm_full_reconcile_route": "/api/v2/admin/sync/full-reconcile",
    }


def public_signatures() -> Dict[str, str]:
    """Call signatures that a v30 patch must keep or explicitly migrate."""
    from . import diff as diff_mod
    from . import parse as parse_mod
    from . import supplement as supplement_mod

    return {
        "parse_estimate": str(inspect.signature(parse_mod.parse_estimate)),
        "parse_text": str(inspect.signature(parse_mod.parse_text)),
        "parse_supplement": str(inspect.signature(supplement_mod.parse_supplement)),
        "parse_supplement_text": str(inspect.signature(supplement_mod.parse_supplement_text)),
        "diff_items": str(inspect.signature(diff_mod.diff_items)),
        "diff_summary": str(inspect.signature(diff_mod.diff_summary)),
    }


def supplement_coupling() -> Dict:
    """Show that the supplement parser shares the carrier header matcher.

    ``parse_supplement`` does not have its own header regex. Until a single
    shared design says otherwise, changing ``parse.HEADER_RE`` changes
    supplement dispatch in the same process.
    """
    from . import parse as parse_mod
    from . import supplement as supplement_mod

    shared = {}
    for name in SUPPLEMENT_IMPORTS_FROM_PARSE:
        shared[name] = getattr(supplement_mod, name) is getattr(parse_mod, name)
    return {
        "imports": list(SUPPLEMENT_IMPORTS_FROM_PARSE),
        "same_object": shared,
        "all_shared": all(shared.values()),
    }


def acceptance_signals(estimate: Optional[Mapping]) -> Dict:
    """Read an existing parse result. Do not decide final acceptance.

    The completeness gate is v30 work. ``final_acceptance`` is therefore
    ``not_decided_waiting_on_v30`` for every input, including a tie-out that
    passed. A matching sum is not evidence that omitted lines cancelled
    each other out; ``sum_tie_detects_offsetting_omissions`` stays false.

    The estimate mapping is not modified.
    """
    estimate = estimate or {}
    checks = estimate.get("checks") or {}
    warnings = list(estimate.get("warnings") or [])
    items = list(estimate.get("items") or [])
    return {
        "parser_status": estimate.get("status"),
        "parser_path": estimate.get("parser_path"),
        "kind": estimate.get("kind"),
        "item_count": len(items),
        "items_match_printed_total": checks.get("items_match_printed_total"),
        "schema_fallback": estimate.get("schema_fallback"),
        "missing_line_numbers_warned": any(
            "line numbers not parsed" in w for w in warnings
        ),
        "header_not_recognized": any(
            "no line-item table header" in w for w in warnings
        ),
        "final_acceptance": "not_decided_waiting_on_v30",
        "completeness_gate": "not_implemented",
        "per_page_ocr_disposition": "not_implemented",
        "scan_class_distinctions": "not_implemented",
        "structured_source_route": "not_implemented",
        "sum_tie_detects_offsetting_omissions": False,
    }
