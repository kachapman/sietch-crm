# Reconciler Phase 1: tie-out-gated notes, PP-OCRv6, and the G1 golden case

**Date:** 2026-11-04
**Branch:** `reconciler-phase1`
**Base:** `main` @ `ee6e401`
**Status:** implemented and tested locally. **Nothing merged, pushed or
deployed.** No database writes, no production access, no UI changes.

This phase adds the pieces the plan's **G1 golden example** needs:

1. a draft note that cannot state an increase when the parse failed;
2. a PP-OCRv6 path for scanned / obfuscated-text-layer PDFs, with GPU→CPU
   device selection;
3. the Phase 1 explanation features (prior RCV from payments, PWARR/code
   releases, payment-vs-increase, buildable attribution, bid-superseded gap,
   REVISED and O&P-only noise, `PER INVOICE` lines);
4. G1 golden tests.

The client PDFs stay in the gitignored `tests/fixtures/reconciler/golden_g1/`.
This document contains **no customer names, claim numbers or addresses**.

---

## 1. The draft note is now gated on the tie-out

`reconciler.notes.draft_note(old=..., new=..., diff=...)` runs
`reconciler.tieout.draft_gate` and:

- **when the gate passes**, states the increase using the **tied-out RCVs**
  from `draft_gate` (`old_rcv`/`new_rcv` are now part of its result), not the
  caller's arguments;
- **when the gate fails**, returns a `DRAFT - NEEDS REVIEW: claim increase NOT
  stated` note: the blockers, the failing check names, the *unverified* parsed
  RCVs and the *unverified* line-item diff total. It never says "Claim
  increased by", so nothing downstream can bill from it;
- **when the line diff disagrees** with the RCV change, the note carries
  `WARNING: the line-item diff totals +$X but the RCV change is +$Y`.

### K1 (the known failed parse)

K1's 2020 estimate parses to RCV **-$203.53**. Before this change the draft
read:

> Claim increased by $45,694.63 (-$203.53 to $45,491.10, no PP) …

Now it reads (abridged):

> DRAFT - NEEDS REVIEW: claim increase NOT stated
> - old estimate failed tie-out
> Parsed RCV (unverified): -$203.53 -> $45,491.10 (+$45,694.63)
> Line-item diff total (unverified): +$25,772.90 - disagrees with the RCV
> change +$45,694.63
> A person must confirm the parse … before this claim increase is noted or
> billed.

Tests: `tests/test_reconciler_g1.py::TestK1DraftNoteGate` (fixture) and
`tests/test_reconciler_notes.py::TestDraftNoteGate` (synthetic).

---

## 2. PP-OCRv6 for scans and obfuscated text layers

- **Engine: PaddleOCR 3.7.0 / PP-OCRv6 only. Tesseract is never used.**
- `reconciler/ocr.py` is the bridge; `reconciler/ocr_worker.py` runs inside the
  separate PaddleOCR venv (`RECONCILER_OCR_PYTHON`, default
  `~/venvs/paddleocr/bin/python3`). The main app never imports `paddle*`.
- **Device selection** (`ocr_worker.device_info`): GPU only when Paddle is a
  CUDA build with a visible device; otherwise CPU. The decision, the reason,
  the Paddle version and the ONNX Runtime providers are stored on the result
  (`est["ocr"]`). On chapmanserver: `device=cpu`, reason
  *"no CUDA runtime in the Paddle build; AMD Vega 8 (gfx902) is not supported
  by ROCm/Paddle, OpenVINO is Intel-only"*.
- **Row reconstruction** (`ocr.boxes_to_text`): OCR boxes are clustered into
  visual rows by vertical overlap and joined left-to-right; a horizontal gap
  wider than ~1.1× the row height becomes a 2+-space column separator, which
  is what the parser's column regexes expect. This is pure Python and unit
  tested without PaddleOCR installed.
- **Caching:** results are cached on disk by file hash + DPI + model
  (`~/.cache/sietch-reconciler/ocr/`), so a second run is free.
- **Opt-in:** OCR is off by default (`RECONCILER_OCR=1` or `parse_estimate(...,
  ocr=True)`), so the library stays deterministic/fast and existing scans
  still refuse cleanly when OCR is not requested. A failed OCR still returns a
  clean `needs_ocr` result with the OCR reason appended.
- **Text-layer fix:** `pdftext.has_line_item_structure()` overrules the
  "mostly non-letter garbage" heuristic when a real line-item header **and**
  printed totals are present. Our own R1 estimate is full of drawing labels
  (letters < 20% of printable characters) and was previously misread as a
  scan. A genuinely obfuscated text layer cannot produce that structure, so
  the two are cleanly separated.

### Measured on chapmanserver (CPU)

| Document | Pages | DPI | Model | Wall time | Result |
|---|---|---|---|---|---|
| G1 revised carrier estimate | 46 | 200 | PP-OCRv6_small | **759 s** (~16.5 s/page, 4 cores, ~360% CPU) | RCV ties exactly |
| G1 Summary of Loss | 2 | 200 | PP-OCRv6_small | seconds | 11 payments parsed |

Init is ~0.9 s; a single 200-DPI page is ~17 s. `enable_mkldnn=False` is
required on this CPU build: PP-OCRv6's oneDNN path raises
`ConvertPirAttribute2RuntimeAttribute` here.

---

## 3. Phase 1 explanation features (`reconciler/explain.py`)

All deterministic, integer cents, no model.

| Feature | API | G1 result |
|---|---|---|
| Prior carrier RCV = payments + deductible | `prior_rcv_from_payments` | 840,638.92 + 10,000.00 = **850,638.92** |
| Payment vs RCV increase | `payment_explanation` | cheque 104,450.96 = 955,089.88 - 850,638.92; -57,065.99 release = **47,384.97** |
| PWARR / code holdback release | `holdback_releases` | **57,065.99** (line 65; its own note equals its RCV). Line 4's note (12,695.73) does not match its RCV (119,747.88) and is reported as a mismatch, not guessed. |
| Per-item buildable attribution | `attribute_changes` | every change labelled; buildable + non-buildable + revised = row delta |
| O&P-only noise | `op_only_changes`, `notes.change_drivers(collapse_op_only=True)` | N rows whose only movement is O&P/tax become one driver, not N "changed" rows |
| REVISED lines | `diff` flags `revised`; `attribute_changes` labels them "value moved to the revision line" | 11 revised rows on G1's carrier revision; never counted as removals/buildable |
| Gap to our estimate, bid-superseded excluded | `gap_vs_our_estimate` | **83,360.41**; HVAC excess 56,511.55 is bid-superseded; realistic **26,848.86** |
| `PER INVOICE` / `PER BID` lines | `parse.build_item` / schema path | K1's **24** roof lines kept at $0 as `unpriced`, one aggregate warning, excluded from totals |
| Composed note | `claim_change_note` | reproduces the G1 note's amounts; says "unconfirmed without the carrier's prior estimate" when the prior revision is absent |

### Why HVAC is excluded by section first

`gap_vs_our_estimate` matches a superseded group by **section label first**
(our R1 prints `HVAC`), then by description patterns. Using the descriptions
alone over-matches: HVAC equipment is also named on roof and repair lines
(our R1 has two such lines outside the HVAC section). With the section rule
our side is exactly $165,769.85 and the carrier side $109,258.30.

---

## 4. G1 golden numbers — honest results

All four targets match **exactly**, from the real PDFs (carrier RCV is from
the PP-OCRv6 parse):

| Target | Expected | Actual | Status |
|---|---|---|---|
| New RCV | $955,089.88 | $955,089.88 | **match** (tie-out exact) |
| Additional increase over $907,704.91 | $47,384.97 | $47,384.97 | **match** |
| PWARR release | $57,065.99 | $57,065.99 | **match** |
| Gap vs our R1 | $83,360.41 | $83,360.41 | **match** |
| Our R1 RCV | $1,038,450.29 | $1,038,450.29 | **match** (tie-out exact) |
| Cheque | $104,450.96 | $104,450.96 | **match** |
| Prior payments (11 cheques) | $840,638.92 | $840,638.92 | **match** (sum == printed total) |
| Realistic remaining target | ~$26,848.86 | $26,848.86 | **match** |

No golden test needed `xfail`.

**Still unconfirmed (honestly):** the split of the ~$26k between the
standing-seam metal roof and the shingle quantity needs SF's prior (9/11)
estimate. `TestG1PriorCarrierEstimate` skips with that reason until the file is
fetched; nothing invents the split. The report's own note about the ICC
Worker Comp line (note $12,695.73 vs line $119,747.88) is surfaced as a
mismatch by `holdback_releases`, not silently resolved.

---

## 5. Tests and how to run them

```bash
# fast (no fixtures needed for the pure suites)
python3 tests/test_reconciler_notes.py
python3 tests/test_reconciler_ocr.py        # row layout + clean refusal
python3 tests/test_reconciler_explain.py    # G1 maths, pure
python3 tests/test_reconciler_service.py    # build_run + approval policy
python3 tests/test_reconciler_parse.py      # fixtures: skips cleanly if absent
python3 tests/test_reconciler_matcher.py
python3 tests/test_reconciler_pairs.py
python3 tests/test_reconciler_commission.py
python3 tests/test_reconciler_regressions.py  # O&P noise + REVISED
python3 tests/test_reconciler_g1.py         # fast tier; OCR tier skips
python3 tests/test_reconciler_routes.py     # skips without a database

# the heavy tier (minutes on CPU, then cached)
RECONCILER_OCR_TEST=1 python3 tests/test_reconciler_g1.py
```

CI now runs the reconciler suites. They need no database; the client fixtures
are gitignored so fixture-dependent tests skip on CI, and the OCR tier is
env-gated.

## 6. Open items (state after round 2)

- **Prior SF estimate**: resolved in round 2 — the 8/27 prior is in
  `golden_g1/prior_sf/` and the split test now runs (see §9).
- **Note composer wiring**: done in round 2 (`reconciler_service` + UI); posting
  stays a human Admin action.
- **OCR cost**: 46 pages ≈ 12.6 minutes on this CPU. That is fine for a
  background draft but too slow for a synchronous request; the Phase 2 worker
  queue is still the right home for it.
- **PP-OCRv6 medium** is cached and selectable (`--model medium`); it was not
  benchmarked here. Small tied out exactly on G1, so medium is unnecessary for
  now.

---

# Round 2 (same branch, 2026-11-04): gate hardening, real prior split, API + UI

Stilgar reviewed `ebec4d3`. This round addresses the review items below. All
fixtures remain gitignored; no client surname or claim number appears in the
committed code or test names.

## 7. K1 fixtures are neutral now

`tests/fixtures/reconciler/k1/` (renamed from the customer-surname folder)
with neutral filenames:

| before | after |
|---|---|
| `<claim#> - Customer Copy Final Draft.pdf` | `k1/k1_carrier_revised.pdf` |
| `<surname>-R_Exterior.Interior 9.15.26.pdf` | `k1/k1_ours.pdf` |
| `new.txt` / `og_all.txt` / `ours.txt` | `k1/k1_new.txt` / `k1/k1_og.txt` / `k1/k1_ours.txt` |

The golden ids are `k1_og`, `k1_new`, `k1_ours`, `k1_revised_pdf`,
`k1_ours_pdf`. No customer surname or claim-number string remains in committed
code or test names.

## 8. The draft-note gate is mandatory

`notes.draft_note()` no longer has an ungated path. Pass `old=`/`new=`
estimates (or a ready-made `gate=`); calling it with neither returns:

```
DRAFT - NEEDS REVIEW: claim increase NOT stated
- no tie-out gate supplied - a draft cannot state an increase without a passed tie-out
...
```

Tests: `tests/test_reconciler_notes.py::TestDraftNoteGate.test_no_gate_and_no_estimates_is_needs_review`
(both `draft_note()` and the old positional `draft_note(100_000, 150_000)`
form), plus the existing allowed/blocked cases.

## 9. The prior State Farm estimates — the split is computed, not assumed

The direct prior (8/27) is an obfuscated text layer, so it needs PP-OCRv6.
It parses to **RCV $850,638.92** with **exact** tie-out, which equals the
Summary of Loss **$840,638.92 prior payments + $10,000 deductible** (both
parsed).

`TestG1PriorCarrierEstimate` is no longer skipped for a missing fixture. It
asserts, from parsed output only:

- prior RCV == parsed prior payments + parsed deductible;
- payment ($104,450.96) == revised RCV − prior RCV;
- PWARR release (with the prior supplied) == **$57,065.99**, line 65, and the
  Worker Comp ISO line's note-vs-RCV mismatch is still reported;
- the metal-roof lines' delta is **$0**;
- the remainder is fully attributed per line.

### The golden report's inferred split was wrong — here are the real numbers

The report inferred "~$14.1k standing-seam metal + ~$11.9k shingle quantity"
for the ~$26k remainder, because it did not have the 8/27 estimate. With the
prior parsed, the metal-roof lines were **already present at the same prices**
(delta $0), and the ~$26,021.39 remainder is:

| Parsed item group | Delta |
|---|---|
| 11 D&R condensers (one line, qty 11) | $9,996.10 |
| D&R black pipe with fitting and hanger | $9,481.81 |
| Interior 401A drywall/paint/finish | $5,473.86 (plus ~$237 other interior) |
| Apartment Roof soffit + metal-roof felt | $887.55 |
| Small price drift / labor minimums | ≈ +$182 |
| **Remainder after PWARR + HVAC O&P + Garage 1 shingles** | **$26,021.39** |

The three confirmed big items remain PWARR $57,065.99, HVACi GC O&P
$16,543.70 and Garage 1 shingles $4,819.88. This is a correction to the
report, not a disagreement with the totals: every number still sums to the
$104,450.96 cheque exactly (`row_deltas.rcv == payment`).

## 10. Phase 1 API + UI (draft-only, Admin approve)

### Migration

**`migrations/046_reconciler_runs.sql`** — the next number after 045, and the
only migration this phase adds. It creates:

- `reconciler_runs` (project, status, creator, inputs with doc refs + sha256,
  parser version, `gate_allowed`, confidence, draft note, billing summary,
  result JSON, approved/rejected by/at, timestamps);
- `reconciler_audit` (run, project, user, action, before/after JSON).

The plan split this into 046/047/048; the MVP keeps the parsed artifacts as
JSON on the run row, which the plan explicitly allows.

### Routes (`server.py` → thin handlers, logic in `reconciler_service.py`)

| Method | Path | Auth |
|---|---|---|
| POST | `/api/v2/reconciler/runs` | any logged-in user (limited users: own deals only) |
| GET | `/api/v2/reconciler/runs/{id}` | any logged-in user, scoped |
| GET | `/api/v2/projects/{id}/reconciler/runs` | any logged-in user, scoped |
| PATCH | `/api/v2/reconciler/runs/{id}` | any logged-in user, scoped, audited |
| POST | `/api/v2/reconciler/runs/{id}/approve` | **Admin only** (`_require_admin`) |
| POST | `/api/v2/reconciler/runs/{id}/reject` | any logged-in user, scoped |
| GET | `/api/v2/reconciler/health` | any logged-in user |

`POST /runs` resolves `{kind: "doc"|"attachment", id}` refs against the
project (the two id spaces overlap, so the ref is explicit), parses the prior
and revised carriers with `parse_estimate` and our estimate with
`parse_supplement`, runs the diff + gate + note + billing + holdbacks + gap +
attribution, and stores a draft. **It parses with `ocr=False`**: OCR is not a
production dependency.

**Approval** (`reconciler_service.approve_run`) is one transaction:
atomic status claim → history note (category 8 = "Note") → notify the run
creator → delete **only** the "Needs reconciliation" tag → audit → mark
approved. A second approve is idempotent (no second note). A run whose
`gate_allowed` is false returns **409** and posts nothing, even if the draft
text was edited. Custom fields (notably "Final RCV") are never written.

### UI (`public/app.js` + `public/index.html` + `public/styles.css`)

- A **Reconcile** icon button in the project Documents toolbar (next to
  Upload/Create).
- Modal with three document pickers (prior carrier / revised carrier / our
  estimate), pre-filled from filenames (revised/original/supplement
  heuristics).
- Draft note textarea with **Save draft**, **Discard**, and an Admin-only
  **Approve & post** button that is disabled when the gate blocked the run.
- Deterministic summary: RCV by slot, top drivers, line-diff counts,
  PWARR/code holdbacks, buildable attribution, gap (with bid-superseded
  exclusion), and the info-only billing summary.
- Previous runs for the project, clickable to reopen.
- A scanned PDF shows the "needs OCR" explanation and no approve button; it
  does not fail.

Tests: `tests/test_reconciler_routes.py` (handler shim: auth 401, non-admin
approve 403, gate-blocked approve 409, doc scoping, limited-user 403, health)
and DB-backed `TestApprovalPersistence` (real note, tag removal only,
creator notified, audit, idempotency, no custom-field writes).
`tests/test_reconciler_service.py` covers `build_run` and `approval_guard`
without a DB.

## 11. OCR stays out of production; cold-run timing

`requirements.txt` and the `Dockerfile` contain **no** PaddleOCR /
paddlepaddle. The image only gains `COPY reconciler ./reconciler` (pure
stdlib). `parse_estimate(..., ocr=False)` is what the API uses, so a scanned
PDF returns `needs_ocr` with a human message instead of an error or a crash.

A **cold** PP-OCRv6 run (fresh `RECONCILER_OCR_CACHE_DIR`, no cache reuse) was
done on the 46-page G1 carrier revision through the parent bridge
(`python3 -m reconciler.ocr`):

- device: **CPU** (device reason recorded: Paddle CPU build; Vega 8 gfx902
  unsupported);
- 46 pages @ 200 DPI, PP-OCRv6_small: **789.41 s cold** (13 min 09 s,
  ~17.2 s/page; per-page range 5.3–29.8 s);
- the recognised text still ties out to **$955,089.88** (verified from the
  cold text, not the cache).

**The heavy G1 tests depend on the OCR disk cache** (`~/.cache/sietch-reconciler/ocr/`,
override with `RECONCILER_OCR_CACHE_DIR`). The first `RECONCILER_OCR_TEST=1`
run on a new machine pays the ~13-minute CPU cost per scanned document; after
that the same file hash is cached and the suite runs in seconds. CI does not
set `RECONCILER_OCR_TEST`, so it never pays that cost (the heavy tier skips).

