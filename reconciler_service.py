"""Reconciler Phase 1 service: build a draft run, persist it, approve it.

This is the thin orchestration layer between the HTTP handlers in
``server.py`` and the deterministic ``reconciler`` library.  It:

* parses the three picked documents (prior carrier revision, new carrier
  revision, our estimate),
* runs the diff, the tie-out **draft gate**, the draft note, the billing
  summary and the explanation features,
* stores the run, and
* approves it in **one transaction** (history note + tag removal + audit).

Nothing here posts a note on its own.  Approval is a human Admin click, and it
is refused unless ``gate_allowed`` is true - a note may never quote a number
from a parse that did not tie out (K1).

OCR is deliberately **not** run in the request path: PaddleOCR is not a
production dependency (see requirements.txt / Dockerfile).  A scanned PDF
comes back as ``needs_ocr`` with a clear message; the Phase 2 worker is where
OCR belongs.
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from reconciler import __version__ as PARSER_VERSION
from reconciler import commission as commission_mod
from reconciler import diff as diff_mod
from reconciler import explain
from reconciler import notes as notes_mod
from reconciler import parse as parse_mod
from reconciler import supplement as supplement_mod
from reconciler import tieout as tieout_mod
from reconciler.money import fmt_cents

__all__ = [
    "SLOTS",
    "STATUS_DRAFTED",
    "STATUS_NEEDS_REVIEW",
    "STATUS_NEEDS_OCR",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "TAG_TITLE",
    "NOTE_CATEGORY_ID",
    "build_run",
    "file_sha256",
    "approval_guard",
    "create_run",
    "get_run",
    "list_runs",
    "update_draft_note",
    "approve_run",
    "reject_run",
]

SLOTS = ("prior", "revised", "our")
CARRIER_SLOTS = ("prior", "revised")

STATUS_DRAFTED = "drafted"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_NEEDS_OCR = "needs_ocr"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# The tag the reconciler removes on approve - and *only* this tag.  "Draft
# Review" and "$$$" are never touched.  Matched case-insensitively.
TAG_TITLE = "Needs reconciliation"

# "Note" in history_categories (seed id 8).  Ken chose Note for the posted
# reconciler note; categories 1/8/10 are the note-like ones for notifications.
NOTE_CATEGORY_ID = 8

_NEEDS_OCR_MESSAGE = (
    "This PDF has no readable text layer (it is a scan/obfuscated export). "
    "OCR is not available on the CRM server - a scanned estimate needs the "
    "Phase 2 OCR worker, or upload a text-based PDF."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def file_sha256(path: str | os.PathLike) -> Optional[str]:
    """sha256 of a file, or None when it cannot be read (tests use fake paths)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _json_safe(value: Any) -> Any:
    """Make parsed output JSON-serialisable (Decimal qty, Paths, sets)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, Path):
        return str(value)
    return value


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _parse_slot(slot: str, path: str) -> Dict:
    """Parse one picked document for its slot.  Never raises for a bad PDF."""
    if slot == "our":
        return supplement_mod.parse_supplement(path, ocr=False)
    return parse_mod.parse_estimate(path, ocr=False)


def _totals(est: Dict) -> Dict:
    return dict(est.get("totals") or {})


def _status_of(est: Dict) -> str:
    return str(est.get("status") or "ok")


def _slot_summary(est: Dict) -> Dict:
    return {
        "status": _status_of(est),
        "reason": est.get("reason") or est.get("fatal") or "",
        "parserPath": est.get("parser_path"),
        "totals": _json_safe(_totals(est)),
        "itemCount": len(est.get("items") or []),
        "warnings": list(est.get("warnings") or [])[:25],
        "warningsTotal": len(est.get("warnings") or []),
        "needsOcr": _status_of(est) == "needs_ocr",
    }


def _empty_estimate() -> Dict:
    return {
        "status": "ok",
        "items": [],
        "totals": {},
        "checks": {},
        "summaries": [],
        "meta": {},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# build a draft run (pure; parsed= is the test seam)
# ---------------------------------------------------------------------------
def build_run(
    project_id: int,
    docs: Dict[str, Dict],
    parsed: Optional[Dict[str, Dict]] = None,
) -> Dict:
    """Parse, tie out and draft one reconciliation.  No DB access.

    ``docs`` maps slot -> ``{"docRef": "doc:12"|"att:34", "title": ...,
    "path": ...}``.  ``parsed`` may supply already-parsed estimates (tests) and
    defaults to parsing the files with OCR disabled.

    Returns ``{"projectId", "status", "gateAllowed", "confidence",
    "draftNote", "billingSummary", "inputs", "result"}`` - exactly what the
    run row stores.
    """
    estimates: Dict[str, Dict] = {}
    inputs: Dict[str, Dict] = {}
    messages: List[str] = []
    needs_ocr: List[str] = []
    blockers: List[str] = []

    for slot in SLOTS:
        doc = (docs or {}).get(slot) or {}
        inputs[slot] = {
            "docRef": doc.get("docRef"),
            "title": doc.get("title"),
            "sha256": doc.get("sha256") or (file_sha256(doc["path"]) if doc.get("path") else None),
        }
        if parsed is not None and slot in parsed:
            est = parsed[slot]
        elif doc.get("path"):
            est = _parse_slot(slot, str(doc["path"]))
        else:
            est = {
                **_empty_estimate(),
                "status": "error",
                "reason": f"{slot} document not supplied",
            }
        estimates[slot] = est
        st = _status_of(est)
        if st != "ok":
            # The prior and revised carriers are required to draft an increase.
            # "Our" estimate is optional: without it the gap comparison is
            # unavailable, but the draft note can still be reviewed.
            if slot != "our":
                blockers.append(f"{slot} estimate: {est.get('reason') or est.get('fatal') or st}")
            if st == "needs_ocr":
                needs_ocr.append(slot)
                messages.append(f"{slot.title()}: {_NEEDS_OCR_MESSAGE}")

    prior = estimates["prior"]
    revised = estimates["revised"]
    our = estimates["our"]

    result: Dict[str, Any] = {
        "parserVersion": PARSER_VERSION,
        "estimates": {slot: _slot_summary(estimates[slot]) for slot in SLOTS},
        "needsOcr": needs_ocr,
        "messages": messages,
        "diff": None,
        "holdbacks": None,
        "gap": None,
        "attribution": None,
        "priorRcv": None,
        "checks": {},
    }

    gate_allowed = False
    confidence = 0.0
    billing: List[str] = []
    draft = ""
    note_event_id = None

    parse_ok = not blockers
    if parse_ok:
        diff = diff_mod.diff_items(prior.get("items") or [], revised.get("items") or [])
        summary = diff_mod.diff_summary(diff)
        gate = tieout_mod.draft_gate(prior, revised, diff)
        gate_allowed = bool(gate.get("allowed"))
        blockers = list(gate.get("blockers") or [])
        draft = notes_mod.draft_note(
            old=prior, new=revised, diff=diff, gate=gate, driver_limit=10
        )
        attribution = explain.attribute_changes(diff)
        holdbacks = explain.holdback_releases(revised.get("items") or [], prior.get("items") or [])
        result.update(
            diff={
                "summary": _json_safe(summary),
                "drivers": _json_safe(notes_mod.change_drivers(diff, limit=10)),
                "needsReview": [
                    {
                        "how": m.get("how"),
                        "sim": m.get("sim"),
                        "a": (m.get("a") or [{}])[0].get("desc"),
                        "b": (m.get("b") or [{}])[0].get("desc"),
                    }
                    for m in (diff.get("needs_review") or [])[:25]
                ],
                "needsReviewTotal": len(diff.get("needs_review") or []),
                "sectionRenames": _json_safe(diff.get("alias") or {}),
            },
            holdbacks=_json_safe(
                {
                    "confirmedTotal": holdbacks["confirmed_total"],
                    "conditionalTotal": holdbacks["conditional_total"],
                    "count": holdbacks["count"],
                    "items": holdbacks["items"],
                    "mismatches": holdbacks["mismatches"],
                }
            ),
            attribution=_json_safe(
                {
                    "rows": attribution["rows"],
                    "buildableTotal": attribution["buildable_total"],
                    "nonBuildableTotal": attribution["non_buildable_total"],
                    "conditionalTotal": attribution["conditional_total"],
                    "opOnlyGroups": attribution["op_only_groups"],
                }
            ),
            priorRcv=int((prior.get("totals") or {}).get("rcv") or 0),
            checks={
                "priorExact": bool(tieout_mod.tieout_estimate(prior).get("exact")),
                "revisedExact": bool(tieout_mod.tieout_estimate(revised).get("exact")),
                "deltaExact": bool((gate.get("diff") or {}).get("exact")),
            },
        )
        if _status_of(our) == "ok":
            result["gap"] = _json_safe(explain.gap_vs_our_estimate(our, revised))
        else:
            result["messages"].append(
                "Our estimate was not parsed, so the gap comparison is unavailable."
            )
        if gate_allowed:
            already_noted = 0  # Phase 2 reads prior runs / notes; MVP is info-only
            comm = commission_mod.commission(
                prior.get("totals") or {},
                revised.get("totals") or {},
                already_noted=already_noted,
            )
            billing = notes_mod.billing_summary_lines(
                prior.get("totals") or {}, revised.get("totals") or {}, comm
            )
            confidence = 0.95 if (gate.get("diff") or {}).get("exact") else 0.85
            confidence -= min(0.20, 0.01 * len(diff.get("needs_review") or []))
        confidence = round(max(0.0, min(1.0, confidence)), 3)

    if not parse_ok or not gate_allowed:
        billing = []
        if not draft:
            draft = notes_mod.needs_review_note(
                gate={
                    "allowed": False,
                    "blockers": blockers or ["reconciler did not run"],
                    "estimate_old": {"results": []},
                    "estimate_new": {"results": []},
                },
                old_rcv=int((prior.get("totals") or {}).get("rcv") or 0),
                new_rcv=int((revised.get("totals") or {}).get("rcv") or 0),
            )

    result["blockers"] = list(blockers)
    result["noteEventId"] = note_event_id

    if any(slot in needs_ocr for slot in CARRIER_SLOTS):
        status = STATUS_NEEDS_OCR
    elif gate_allowed:
        status = STATUS_DRAFTED
    else:
        status = STATUS_NEEDS_REVIEW

    return {
        "projectId": int(project_id),
        "status": status,
        "gateAllowed": bool(gate_allowed),
        "confidence": 0.0 if not gate_allowed else confidence,
        "draftNote": draft,
        "billingSummary": list(billing),
        "inputs": inputs,
        "result": _json_safe(result),
        "blockers": list(blockers),
    }


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
_RUN_COLUMNS = (
    "id, opportunity_id, status, created_by, inputs, parser_version, gate_allowed, "
    "confidence, draft_note, billing_summary, result, approved_by, approved_at, "
    "rejected_by, rejected_at, created_at, updated_at"
)


def _run_dict(row: Dict) -> Dict:
    """Serialise a reconciler_runs row for the API (camelCase)."""
    if not row:
        return {}
    return {
        "id": row.get("id"),
        "projectId": row.get("opportunity_id"),
        "status": row.get("status"),
        "createdBy": row.get("created_by"),
        "inputs": row.get("inputs") or {},
        "parserVersion": row.get("parser_version"),
        "gateAllowed": bool(row.get("gate_allowed")),
        "confidence": float(row.get("confidence") or 0),
        "draftNote": row.get("draft_note") or "",
        "billingSummary": row.get("billing_summary") or [],
        "result": row.get("result") or {},
        "approvedBy": row.get("approved_by"),
        "approvedAt": _iso(row.get("approved_at")),
        "rejectedBy": row.get("rejected_by"),
        "rejectedAt": _iso(row.get("rejected_at")),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def create_run(project_id: int, user_id: int, payload: Dict) -> Dict:
    """Persist a built run and audit its creation.  Returns the run dict."""
    import db  # lazy: keeps the pure helpers importable without a DB

    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO reconciler_runs
                   (opportunity_id, status, created_by, inputs, parser_version,
                    gate_allowed, confidence, draft_note, billing_summary, result)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
               RETURNING id""",
            (
                int(project_id),
                payload["status"],
                user_id,
                json.dumps(payload.get("inputs") or {}),
                payload.get("result", {}).get("parserVersion") or PARSER_VERSION,
                bool(payload.get("gateAllowed")),
                float(payload.get("confidence") or 0),
                payload.get("draftNote") or "",
                json.dumps(payload.get("billingSummary") or []),
                json.dumps(payload.get("result") or {}),
            ),
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO reconciler_audit (run_id, opportunity_id, user_id, action, after)
               VALUES (%s, %s, %s, 'created', %s::jsonb)""",
            (run_id, int(project_id), user_id, json.dumps({
                "status": payload["status"],
                "gateAllowed": bool(payload.get("gateAllowed")),
                "confidence": payload.get("confidence"),
                "inputs": payload.get("inputs"),
            })),
        )
    return get_run(run_id) or {}


def get_run(run_id: int) -> Optional[Dict]:
    import db

    row = db.query_one(
        f"SELECT {_RUN_COLUMNS} FROM reconciler_runs WHERE id = %s", (int(run_id),)
    )
    return _run_dict(row) if row else None


def list_runs(project_id: int, user: Optional[Dict] = None, limit: int = 20) -> List[Dict]:
    import db

    params: List[Any] = [int(project_id)]
    where = "opportunity_id = %s"
    if user and user.get("role") == "limited":
        where += (
            " AND opportunity_id IN (SELECT id FROM opportunities "
            "WHERE responsible_user_id = %s OR created_by = %s)"
        )
        params.extend([user["id"], user["id"]])
    params.append(max(1, min(int(limit or 20), 100)))
    rows = db.query_dicts(
        f"SELECT {_RUN_COLUMNS} FROM reconciler_runs WHERE {where} "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        tuple(params),
    )
    return [_run_dict(r) for r in rows]


def update_draft_note(run_id: int, note: str, user_id: int) -> Dict:
    """Reviewer edit of the draft note.  Audited; refused after approval."""
    import db

    run = get_run(run_id)
    if not run:
        return {"ok": False, "code": 404, "error": "Run not found"}
    if run["status"] in (STATUS_APPROVED, STATUS_REJECTED):
        return {"ok": False, "code": 409, "error": f"Run is already {run['status']}"}
    with db.db_cursor() as cur:
        cur.execute(
            "UPDATE reconciler_runs SET draft_note = %s, updated_at = NOW() WHERE id = %s",
            (str(note or ""), int(run_id)),
        )
        cur.execute(
            """INSERT INTO reconciler_audit (run_id, opportunity_id, user_id, action, before, after)
               VALUES (%s, %s, %s, 'note_edited', %s::jsonb, %s::jsonb)""",
            (
                int(run_id),
                run["projectId"],
                user_id,
                json.dumps({"draftNote": run["draftNote"]}),
                json.dumps({"draftNote": str(note or "")}),
            ),
        )
    return {"ok": True, "run": get_run(run_id)}


# ---------------------------------------------------------------------------
# approval (the safety-critical path)
# ---------------------------------------------------------------------------
def approval_guard(run: Optional[Dict]) -> Dict:
    """May this run post a note?  Pure, so the route tests can pin it.

    Returns ``{"allowed": bool, "code": int, "reason": str,
    "alreadyApproved": bool}``.
    """
    if not run:
        return {"allowed": False, "code": 404, "reason": "Run not found"}
    if run.get("status") == STATUS_APPROVED:
        return {"allowed": True, "code": 200, "reason": "", "alreadyApproved": True}
    if run.get("status") == STATUS_REJECTED:
        return {"allowed": False, "code": 409, "reason": "Run was rejected"}
    if not run.get("gateAllowed"):
        blockers = (run.get("result") or {}).get("blockers") or []
        reason = "The tie-out gate blocked this run"
        if blockers:
            reason += ": " + "; ".join(str(b) for b in blockers)
        return {"allowed": False, "code": 409, "reason": reason, "alreadyApproved": False}
    if not (run.get("draftNote") or "").strip():
        return {"allowed": False, "code": 409, "reason": "Run has no draft note"}
    return {"allowed": True, "code": 200, "reason": ""}


def _resolve_tag_row(cur, title: str) -> Optional[int]:
    cur.execute("SELECT id FROM tag_definitions WHERE lower(title) = lower(%s)", (title,))
    row = cur.fetchone()
    return row[0] if row else None


def approve_run(run_id: int, user: Dict, note_override: Optional[str] = None) -> Dict:
    """Post the draft note and clear only the reconciler tag - one transaction.

    Idempotent per run: a second approve returns ``alreadyApproved`` and posts
    nothing.  Refused when the draft gate did not pass, so no note can quote a
    number from a failed parse.  Never writes custom fields (RCV is untouchable).
    """
    run = get_run(run_id)
    guard = approval_guard(run)
    if not guard["allowed"]:
        return {"ok": False, "code": guard["code"], "error": guard["reason"]}
    if guard.get("alreadyApproved"):
        return {
            "ok": True,
            "alreadyApproved": True,
            "run": run,
            "noteEventId": (run.get("result") or {}).get("noteEventId"),
        }

    note = str(note_override if note_override is not None else run["draftNote"]).strip()
    if not note:
        return {"ok": False, "code": 409, "error": "Draft note is empty"}

    import db

    with db.db_cursor() as cur:
        # Atomic claim: only one request may move the run to 'approved'.
        cur.execute(
            """UPDATE reconciler_runs
                  SET status = 'approved', approved_by = %s, approved_at = NOW(),
                      draft_note = %s, updated_at = NOW()
                WHERE id = %s AND status NOT IN ('approved', 'rejected')
                RETURNING id""",
            (user["id"], note, int(run_id)),
        )
        claimed = cur.fetchone()
        if not claimed:
            return {"ok": True, "alreadyApproved": True, "run": get_run(run_id)}

        cur.execute(
            """INSERT INTO history_events (opportunity_id, category_id, content, created_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (run["projectId"], NOTE_CATEGORY_ID, note, user["id"]),
        )
        event_id = cur.fetchone()[0]

        # Notify the user who started the run (Ken's answer #2); no one else.
        creator = run.get("createdBy")
        if creator:
            cur.execute(
                """INSERT INTO history_notify_users (event_id, user_id)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                (event_id, int(creator)),
            )

        # Remove ONLY "Needs reconciliation"; never "$$$", "Draft Review" or
        # anything else.  A missing tag is fine (nothing to clear).
        tag_id = _resolve_tag_row(cur, TAG_TITLE)
        tag_removed = False
        if tag_id is not None:
            cur.execute(
                "DELETE FROM opportunity_tags WHERE opportunity_id = %s AND tag_id = %s",
                (run["projectId"], tag_id),
            )
            tag_removed = cur.rowcount > 0

        cur.execute(
            """INSERT INTO reconciler_audit
                   (run_id, opportunity_id, user_id, action, before, after)
               VALUES (%s, %s, %s, 'approved', %s::jsonb, %s::jsonb)""",
            (
                int(run_id),
                run["projectId"],
                user["id"],
                json.dumps({"status": run["status"], "draftNote": run["draftNote"]}),
                json.dumps({
                    "status": STATUS_APPROVED,
                    "noteEventId": event_id,
                    "tagRemoved": tag_removed,
                    "tagId": tag_id,
                    "note": note,
                }),
            ),
        )
        cur.execute(
            """UPDATE reconciler_runs
                  SET result = jsonb_set(
                          jsonb_set(COALESCE(result, '{}'::jsonb), '{noteEventId}', %s::jsonb),
                          '{tagRemoved}', %s::jsonb)
                WHERE id = %s""",
            (json.dumps(event_id), json.dumps(bool(tag_removed)), int(run_id)),
        )

    return {
        "ok": True,
        "alreadyApproved": False,
        "noteEventId": event_id,
        "tagRemoved": tag_removed,
        "run": get_run(run_id) or {},
    }


def reject_run(run_id: int, user: Dict, reason: str = "") -> Dict:
    """Mark a run rejected (never posts).  Audited."""
    run = get_run(run_id)
    if not run:
        return {"ok": False, "code": 404, "error": "Run not found"}
    if run["status"] == STATUS_APPROVED:
        return {"ok": False, "code": 409, "error": "Run is already approved"}
    import db

    with db.db_cursor() as cur:
        cur.execute(
            """UPDATE reconciler_runs
                  SET status = 'rejected', rejected_by = %s, rejected_at = NOW(), updated_at = NOW()
                WHERE id = %s AND status <> 'approved'""",
            (user["id"], int(run_id)),
        )
        cur.execute(
            """INSERT INTO reconciler_audit (run_id, opportunity_id, user_id, action, before, after)
               VALUES (%s, %s, %s, 'rejected', %s::jsonb, %s::jsonb)""",
            (
                int(run_id),
                run["projectId"],
                user["id"],
                json.dumps({"status": run["status"]}),
                json.dumps({"status": STATUS_REJECTED, "reason": str(reason or "")}),
            ),
        )
    return {"ok": True, "run": get_run(run_id)}
