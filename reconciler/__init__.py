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
    ocr        - PaddleOCR PP-OCRv6 bridge for scans / obfuscated text layers
                 (GPU when available, else CPU), with layout-text
                 reconstruction; the worker runs in a separate venv
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
    notes      - draft note text (tie-out gated: a failed parse never states an
                 increase), top-N change drivers and a disabled hook for a
                 future explanation-only "why" layer
    explain    - why the payment differs from the increase: prior RCV from
                 payments + deductible, PWARR/code holdback releases,
                 per-item buildable attribution, O&P-only noise, and the gap
                 to our estimate excluding bid-superseded scope
"""

__all__ = [
    "money",
    "pdftext",
    "ocr",
    "parse",
    "supplement",
    "tieout",
    "diff",
    "commission",
    "notes",
    "explain",
]

__version__ = "0.3.0"
