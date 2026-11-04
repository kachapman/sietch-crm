"""Sietch Estimate Reconciler - Phase 0 parser library.

Standalone, deterministic (no AI/LLM) parsing of Xactimate carrier estimates
and contractor (our supplement) estimates.

All money is represented as integer cents. Nothing in this package writes to
the CRM, the database, or any external service - it is a pure parsing/reporting
library.

Modules:
    money      - integer-cents helpers (parse / format)
    pdftext    - pdftotext -layout extraction with a clean ``needs_ocr`` result,
                 plus optional PDF-only bbox continuations for wrapped text
    parse      - carrier estimate parser (meta, coverage, summaries, items);
                 the primary parser plus a schema-driven fallback (Spock's
                 row-layout parser) chosen only when the primary cannot tie
                 the estimate out
    supplement - our (contractor) supplement estimate parser
    tieout     - line-item / section / area / row tie-out, the old-vs-new
                 delta tie-out and the draft gate
    diff       - old-vs-new line item diff (exact passes then fuzzy / moved /
                 R&R / replacement passes; every non-exact match is
                 ``needs_review``)
    commission - commission calculation
    notes      - draft note text, top-N change drivers and a disabled hook for
                 a future explanation-only "why" layer
"""

__all__ = [
    "money",
    "pdftext",
    "parse",
    "supplement",
    "tieout",
    "diff",
    "commission",
    "notes",
]

__version__ = "0.2.0"
