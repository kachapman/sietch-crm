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
python3 tests/test_reconciler_parse.py      # fixtures: skips cleanly if absent
python3 tests/test_reconciler_matcher.py
python3 tests/test_reconciler_pairs.py
python3 tests/test_reconciler_commission.py
python3 tests/test_reconciler_g1.py         # fast tier; OCR tier skips

# the heavy tier (minutes on CPU, then cached)
RECONCILER_OCR_TEST=1 python3 tests/test_reconciler_g1.py
```

CI now runs the reconciler suites. They need no database; the client fixtures
are gitignored so fixture-dependent tests skip on CI, and the OCR tier is
env-gated.

## 6. Open items

- **Prior SF (9/11) estimate**: needed for the true line-by-line split of the
  ~$26k and to turn the "unconfirmed" sentence into a confirmed one.
- **Note composer wiring**: `claim_change_note` is a library function; posting
  stays a human Admin action in Phase 1's UI (not built here).
- **OCR cost**: 46 pages ≈ 12.6 minutes on this CPU. That is fine for a
  background draft but too slow for a synchronous request; the Phase 2 worker
  queue is still the right home for it.
- **PP-OCRv6 medium** is cached and selectable (`--model medium`); it was not
  benchmarked here. Small tied out exactly on G1, so medium is unnecessary for
  now.
