"""Sietch Estimate Reconciler - Phase 0 parser library.

Standalone, deterministic (no AI/LLM) parsing of Xactimate carrier estimates
and contractor (our supplement) estimates.

All money is represented as integer cents. Nothing in this package writes to
the CRM, the database, or any external service - it is a pure parsing/reporting
library.

Modules:
    money      - integer-cents helpers (parse / format)
    pdftext    - pdftotext -layout extraction with a clean ``needs_ocr`` result
    parse      - carrier estimate parser (meta, coverage, summaries, items)
    supplement - our (contractor) supplement estimate parser
    tieout     - line-item / section / summary tie-out checks
    diff       - old-vs-new line item diff
    commission - commission calculation
    notes      - draft note text generation
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

__version__ = "0.1.0"
