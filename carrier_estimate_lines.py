"""Read-only carrier estimate line projection for sanctioned API callers.

Sietch stores carrier estimate PDFs/text as project documents. The reconciler
package can parse them, but historically nothing exposed that parse over the
read-only API. MCP tools (and local/Privatemode estimate work) need the lines
to build supplements, so this module:

* selects a carrier-estimate document for a project,
* runs the deterministic reconciler parser,
* returns a JSON-safe projection with **no filesystem paths**.

Nothing here writes to the CRM or calls an LLM. Authz and audit stay in the
HTTP handler that calls these helpers.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from reconciler.parse import parse_estimate, parse_text

# Titles that strongly suggest a carrier Xactimate export (not our supplement).
_CARRIER_TITLE_RE = re.compile(
    r"(carrier|insurance|xactimate|\bestimate\b|allstate|state\s*farm|"
    r"farmers|nationwide|usaa|progressive|libertymutual|travelers)",
    re.IGNORECASE,
)
_SUPPLEMENT_TITLE_RE = re.compile(r"supplement|contractor|vanguard|mdm", re.IGNORECASE)

_SAFE_ITEM_KEYS = (
    "no",
    "desc",
    "qty",
    "unit",
    "unit_price",
    "tax",
    "op",
    "rcv",
    "deprec",
    "acv",
    "room",
    "area",
    "section",
    "notes",
    "ok",
)


def _json_money(value: Any) -> int | None:
    """Money is integer cents in the reconciler; coerce safely for JSON."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_qty(value: Any) -> str | float | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def is_likely_carrier_estimate(title: str | None, mime_type: str | None = None) -> bool:
    """Heuristic used when the caller does not pass document_id."""
    name = (title or "").strip()
    if not name:
        return False
    if _SUPPLEMENT_TITLE_RE.search(name):
        # Our own supplement exports often contain the word "estimate"; only
        # treat them as carrier docs when an explicit carrier/insurer token wins.
        explicit = re.compile(
            r"(carrier|insurance|allstate|state\\s*farm|farmers|nationwide|usaa|progressive|libertymutual|travelers)",
            re.IGNORECASE,
        )
        return bool(explicit.search(name))
    if _CARRIER_TITLE_RE.search(name):
        return True
    lower = name.lower()
    if lower.endswith((".pdf", ".txt")) and "supp" not in lower:
        # Generic estimate.pdf / estimate.txt — treat as candidate.
        return "estimate" in lower or "xact" in lower
    mime = (mime_type or "").lower()
    return mime in ("application/pdf", "text/plain") and "estimate" in lower


def select_carrier_document(
    documents: list[dict[str, Any]],
    *,
    document_id: int | None = None,
) -> dict[str, Any] | None:
    """Pick one project_documents row (or history-attachment shaped dict).

    Each document dict must expose at least ``id``, ``title``, ``filePath``
    (or ``file_path``), and optionally ``mimeType`` / ``mime_type``.
    """
    if not documents:
        return None
    if document_id is not None:
        for doc in documents:
            if int(doc.get("id") or 0) == int(document_id):
                return doc
        return None

    candidates = [
        doc
        for doc in documents
        if is_likely_carrier_estimate(
            doc.get("title") or doc.get("filename"),
            doc.get("mimeType") or doc.get("mime_type"),
        )
    ]
    pool = candidates or list(documents)
    # Prefer PDFs, then most recently uploaded when uploadedAt is present.
    def sort_key(doc: dict[str, Any]) -> tuple:
        mime = (doc.get("mimeType") or doc.get("mime_type") or "").lower()
        title = (doc.get("title") or "").lower()
        pdf = 0 if ("pdf" in mime or title.endswith(".pdf")) else 1
        return (pdf, str(doc.get("uploadedAt") or doc.get("uploaded_at") or ""), -int(doc.get("id") or 0))

    return sorted(pool, key=sort_key)[0]


def resolve_document_path(file_path: str, *, root: Path) -> Path:
    """Resolve a stored relative file_path under ROOT; reject traversal."""
    raw = (file_path or "").strip()
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        raise ValueError("invalid_document_path")
    root = root.resolve()
    full = (root / raw).resolve()
    if not str(full).startswith(str(root) + "/") and full != root:
        raise ValueError("invalid_document_path")
    if not full.is_file():
        raise FileNotFoundError("document_file_missing")
    return full


def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Project one reconciler item to a cloud/MCP-safe dict (no paths)."""
    out: dict[str, Any] = {
        "no": item.get("no"),
        "desc": item.get("desc") or item.get("description") or item.get("first"),
        "qty": _json_qty(item.get("qty")),
        "unit": item.get("unit"),
        "unit_price_cents": _json_money(item.get("unit_price")),
        "tax_cents": _json_money(item.get("tax")),
        "op_cents": _json_money(item.get("op")),
        "rcv_cents": _json_money(item.get("rcv")),
        "deprec_cents": _json_money(item.get("deprec")),
        "acv_cents": _json_money(item.get("acv")),
        "room": item.get("room"),
        "area": item.get("area"),
        "section": item.get("section"),
        "ok": bool(item.get("ok")),
    }
    notes = item.get("notes")
    if notes:
        # notes may be a list of strings or tuples; keep short strings only.
        if isinstance(notes, list):
            out["notes"] = [str(n)[:500] for n in notes[:20]]
        else:
            out["notes"] = [str(notes)[:500]]
    return out


def serialize_estimate(est: dict[str, Any], *, document_id: int, title: str) -> dict[str, Any]:
    """Strip filesystem paths and Decimal values from a reconciler result."""
    meta = est.get("meta") if isinstance(est.get("meta"), dict) else {}
    items = [serialize_item(it) for it in (est.get("items") or []) if isinstance(it, dict)]
    totals = est.get("totals") if isinstance(est.get("totals"), dict) else {}
    safe_totals = {k: _json_money(v) if k != "kind" else v for k, v in totals.items()}
    return {
        "document_id": document_id,
        "document_title": title,
        "status": est.get("status") or "ok",
        "parser_path": est.get("parser_path"),
        "kind": est.get("kind") or "carrier",
        "meta": {
            "insured": meta.get("insured"),
            "claim_number": meta.get("claim_number"),
            "carrier": meta.get("carrier"),
            "estimate_date": meta.get("estimate_date"),
            "date_est_completed": meta.get("date_est_completed"),
            "revision": meta.get("revision"),
        },
        "items": items,
        "item_count": len(items),
        "totals": safe_totals,
        "warnings": [str(w)[:300] for w in (est.get("warnings") or [])[:50]],
        "fatal": est.get("fatal"),
    }


def parse_carrier_file(path: Path) -> dict[str, Any]:
    """Parse a carrier estimate file (PDF or text) via the reconciler."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".text"):
        text = path.read_text(encoding="utf-8", errors="replace")
        return parse_text(text, path=str(path.name))
    return parse_estimate(str(path))


def build_carrier_lines_payload(
    *,
    job_id: int,
    documents: list[dict[str, Any]],
    root: Path,
    document_id: int | None = None,
) -> dict[str, Any]:
    """End-to-end helper used by the HTTP handler (and unit tests)."""
    doc = select_carrier_document(documents, document_id=document_id)
    if doc is None:
        return {
            "source": "sietch",
            "job_id": str(job_id),
            "available": False,
            "blocked_reason": "no_carrier_document",
            "detail": "No project document matched a carrier estimate for this job.",
            "items": [],
            "item_count": 0,
        }
    rel = doc.get("filePath") or doc.get("file_path") or ""
    title = doc.get("title") or doc.get("filename") or ""
    doc_id = int(doc.get("id") or 0)
    try:
        full = resolve_document_path(str(rel), root=root)
    except ValueError:
        return {
            "source": "sietch",
            "job_id": str(job_id),
            "available": False,
            "blocked_reason": "invalid_document_path",
            "document_id": doc_id,
            "items": [],
            "item_count": 0,
        }
    except FileNotFoundError:
        return {
            "source": "sietch",
            "job_id": str(job_id),
            "available": False,
            "blocked_reason": "document_file_missing",
            "document_id": doc_id,
            "items": [],
            "item_count": 0,
        }
    try:
        est = parse_carrier_file(full)
    except Exception:  # noqa: BLE001 — fail closed with a stable reason, no traceback to clients
        return {
            "source": "sietch",
            "job_id": str(job_id),
            "available": False,
            "blocked_reason": "parse_failed",
            "document_id": doc_id,
            "document_title": title,
            "items": [],
            "item_count": 0,
        }
    body = serialize_estimate(est, document_id=doc_id, title=title)
    body.update({"source": "sietch", "job_id": str(job_id), "available": True})
    # Defense: never echo a filesystem path.
    dumped = str(body)
    if str(full) in dumped or str(root) in dumped:
        raise RuntimeError("carrier_lines_path_leak")
    return body
